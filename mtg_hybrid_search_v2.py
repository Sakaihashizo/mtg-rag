"""
mtg_hybrid_search_v2.py — ハイブリッド検索 v2（日本語 FTS + フォーマット絞り込み対応）
===================================================================================
使い方:
  # 通常実行（ターミナル出力のみ）
  python mtg_hybrid_search_v2.py SMALL_V2

  # フォーマット指定
  python mtg_hybrid_search_v2.py SMALL_V2 modern

  # ファイル出力（JSON + テキストを自動生成）
  python mtg_hybrid_search_v2.py SMALL_V2 --output results
  python mtg_hybrid_search_v2.py SMALL_V2 modern --output modern_results
  → results_YYYYMMDD_HHMMSS.json / results_YYYYMMDD_HHMMSS.txt が生成される
"""

import sys
import json
import os
import time
import datetime
import argparse
from dataclasses import dataclass, asdict
from typing import Optional

import psycopg2
from sentence_transformers import SentenceTransformer
from mtg_removal_rules import apply_removal_penalties
from mtg_counter_rules import apply_counter_penalties

# DB 接続設定は db_config.py に一元化（.env から読み込む）。
# 既存の import 互換のためここで再エクスポートする。
from db_config import (
    DB_CONFIG,
    DB_CONFIG_PRIMARY,
    DB_CONFIG_STANDBY,
    FLAG_FILE,
    get_db_config,
)

MODEL_REGISTRY = {
    "SMALL_V2": {
        "model_name": "intfloat/multilingual-e5-small",
        "prefix": "query: ",
        "cards_table": "mtg_cards_v2",
        "embeddings_table": "mtg_embeddings_small_v2",
    },
    "BASE_V2": {
        "model_name": "intfloat/multilingual-e5-base",
        "prefix": "query: ",
        "cards_table": "mtg_cards_v2",
        "embeddings_table": "mtg_embeddings_base_v2",
    },
}

# 対応フォーマット一覧
VALID_FORMATS = {
    "standard", "pioneer", "modern", "legacy", "vintage",
    "commander", "pauper", "historic", "timeless", "brawl",
    "standardbrawl", "oathbreaker", "gladiator", "duel",
    "paupercommander", "premodern", "predh", "penny",
}

# ─── クエリ拡張マップ ─────────────────────────────────────────
# en: 英語FTS用キーワード
# ja: 日本語LIKE検索用キーワード
# type_filter: このキーワードが含まれる場合に type_line を絞り込む（任意）
# 戦場に存在できるパーマネントタイプ
# exile/destroy の対象がこれらの場合のみ除去として認識する
PERMANENT_TYPES = [
    "creature", "artifact", "enchantment", "planeswalker",
    "permanent", "land", "battle", "token",
]

# 除去系クエリ専用の英語FTS SQL を生成する
def build_removal_tsquery() -> str:
    """
    以下のいずれかにヒットする tsquery:
      1. destroy target [パーマネントタイプ]
      2. exile target [パーマネントタイプ]
      3. target opponent/player sacrifices [パーマネントタイプ]
      4. deals X damage to any target（稲妻・火力系除去）
      5. deals X damage to target creature/planeswalker

    'exile target card from graveyard' 等はヒットしない。
    """
    types_or = " | ".join(PERMANENT_TYPES)
    return (
        f"(destroy & target & ({types_or})) | "
        f"(exile & target & ({types_or})) | "
        f"(sacrifices & ({types_or}) & (opponent | player)) | "
        f"(deals & damage & any & target) | "
        f"(deals & damage & target & (creature | planeswalker))"
    )

REMOVAL_TSQUERY = build_removal_tsquery()

# ja は文字列（1つ）またはリスト（複数）で指定可能
# extract_keywords() でリストに正規化される
QUERY_EXPAND = {
    # カウンター系
    "カウンター呪文":  {"en": "counter target spell",
                       "ja": ["呪文１つを対象とする。それを打ち消す",
                              "呪文１つを対象とし、それを打ち消す",
                              "ないかぎり、それを打ち消す",
                              "を打ち消してもよい"],
                       "counter_mode": True},
    "打ち消し":        {"en": "counter target spell",
                       "ja": ["呪文１つを対象とする。それを打ち消す",
                              "呪文１つを対象とし、それを打ち消す",
                              "ないかぎり、それを打ち消す"],
                       "counter_mode": True},
    "カウンター":      {"en": "counter target spell",
                       "ja": ["呪文１つを対象とする。それを打ち消す",
                              "呪文１つを対象とし、それを打ち消す",
                              "ないかぎり、それを打ち消す"],
                       "counter_mode": True},
    "対抗呪文":        {"en": "counter target spell",
                       "ja": ["呪文１つを対象とする。それを打ち消す",
                              "呪文１つを対象とし、それを打ち消す",
                              "ないかぎり、それを打ち消す"],
                       "counter_mode": True},
    # ドロー系
    "カードを引く":    {"en": "draw cards",             "ja": ["カードを引く"]},
    "手札補充":        {"en": "draw cards",             "ja": ["カードを引く"]},
    "ドロー":          {"en": "draw a card",            "ja": ["カードを引く"]},
    "2枚引く":         {"en": "draw two cards",         "ja": ["カードを２枚引く"]},
    "二枚引く":        {"en": "draw two cards",         "ja": ["カードを２枚引く"]},
    # 除去系
    # 「除去」= 対戦相手のパーマネントを戦場から別の領域に移動させること
    # 墓地のカードを追放する（歩く彫像の攪乱者等）は除去ではない
    # removal_mode: True の場合、英語FTSで REMOVAL_TSQUERY を使用する
    "除去":     {"en": "destroy target creature exile target creature deals damage any target",
                 "ja": ["クリーチャー１体を対象とし、それを破壊する",
                        "クリーチャー１体を対象とし、それを追放する",
                        "クリーチャー１体を対象とし、そのオーナーの手札に戻す",
                        "点のダメージを与える",
                        "任意の対象"],
                 "removal_mode": True},
    "単体除去": {"en": "destroy target creature exile target creature deals damage any target",
                 "ja": ["クリーチャー１体を対象とし、それを破壊する",
                        "クリーチャー１体を対象とし、それを追放する",
                        "点のダメージを与える",
                        "任意の対象"],
                 "removal_mode": True},
    "火力":     {"en": "deals damage any target",
                 "ja": ["点のダメージを与える", "任意の対象"],
                 "removal_mode": True},
    "追放除去": {"en": "exile target creature",
                 "ja": ["クリーチャー１体を対象とし、それを追放する"],
                 "removal_mode": True},
    "全体除去": {"en": "destroy all creatures exile all creatures",
                 "ja": ["すべてのクリーチャーを破壊する",
                        "すべてのクリーチャーを追放する"],
                 "removal_mode": True},
    "バウンス": {"en": "return target creature to its owner hand",
                 "ja": ["クリーチャー１体を対象とし、そのオーナーの手札に戻す"]},
    # マナ系
    "マナ加速":        {"en": "add mana",               "ja": ["マナを加える"]},
    "ランプ":          {"en": "search your library for a land", "ja": ["土地を戦場に出す"]},
    "土地加速":        {"en": "search your library for a land", "ja": ["あなたのライブラリーから土地"]},
    # クリーチャー能力（type_filter で Creature に絞る）
    "飛行を持つクリーチャー": {"en": "flying", "ja": ["飛行"],
                               "type_filter": "Creature"},
    "飛行持ち":        {"en": "flying", "ja": ["飛行"],
                        "type_filter": "Creature"},
    "速攻":            {"en": "haste",        "ja": ["速攻"]},
    "破壊不能":        {"en": "indestructible","ja": ["破壊不能"]},
    "絆魂":            {"en": "lifelink",      "ja": ["絆魂"]},
    "接死":            {"en": "deathtouch",    "ja": ["接死"]},
    "先制攻撃":        {"en": "first strike",  "ja": ["先制攻撃"]},
    "トランプル":      {"en": "trample",       "ja": ["トランプル"]},
    # 飛行（単体キーワード → type_filter なし）
    "飛行":            {"en": "flying",        "ja": ["飛行"]},
    # トークン系
    "トークン":        {"en": "create",        "ja": ["トークン"]},
    # コンボ系
    "無限コンボ":      {"en": "whenever untap","ja": ["たび"]},
    "シナジー":        {"en": "whenever",      "ja": ["たび"]},
    # tournament_boost フラグ（大会実績を強く反映する）
    # 「強さ」を意図するワード群
    "最強":    {"en": "", "ja": [], "tournament_boost": True},
    "強い":    {"en": "", "ja": [], "tournament_boost": True},
    "強力":    {"en": "", "ja": [], "tournament_boost": True},
    "強め":    {"en": "", "ja": [], "tournament_boost": True},
    "環境":    {"en": "", "ja": [], "tournament_boost": True},
    "パワカ":  {"en": "", "ja": [], "tournament_boost": True},
    "おすすめ":{"en": "", "ja": [], "tournament_boost": True},
    "採用率":  {"en": "", "ja": [], "tournament_boost": True},
    "採用":    {"en": "", "ja": [], "tournament_boost": True},
    "定番":    {"en": "", "ja": [], "tournament_boost": True},
    "必須":    {"en": "", "ja": [], "tournament_boost": True},
    "tier":    {"en": "", "ja": [], "tournament_boost": True},
    "Tier":    {"en": "", "ja": [], "tournament_boost": True},
    "メタ":    {"en": "", "ja": [], "tournament_boost": True},
    "勝てる":  {"en": "", "ja": [], "tournament_boost": True},
    "優勝":    {"en": "", "ja": [], "tournament_boost": True},
    "入賞":    {"en": "", "ja": [], "tournament_boost": True},
    "競技":    {"en": "", "ja": [], "tournament_boost": True},
    "純粋に":  {"en": "", "ja": [], "tournament_boost": True},
    "コスパ":  {"en": "", "ja": [], "tournament_boost": True},
    "軽い":    {"en": "", "ja": [], "tournament_boost": True},
    "効率":    {"en": "", "ja": [], "tournament_boost": True},
}


def extract_keywords(query: str) -> tuple[list[str], list[str], Optional[str], bool, bool, bool]:
    """
    クエリからキーワードと各フラグを抽出する。
    戻り値: (英語キーワードリスト, 日本語キーワードリスト, type_filter,
             tournament_boost, removal_mode, counter_mode)
    """
    en_keywords: list[str] = []
    ja_keywords: list[str] = []
    type_filter: Optional[str] = None
    tournament_boost: bool = False
    removal_mode: bool = False
    counter_mode: bool = False

    for jp, terms in QUERY_EXPAND.items():
        if jp in query:
            en = terms.get("en", "")
            if en:
                en_keywords.append(en)
            ja = terms.get("ja", [])
            if isinstance(ja, list):
                ja_keywords.extend(ja)
            elif ja:
                ja_keywords.append(ja)
            if "type_filter" in terms and type_filter is None:
                type_filter = terms["type_filter"]
            if terms.get("tournament_boost"):
                tournament_boost = True
            if terms.get("removal_mode"):
                removal_mode = True
            if terms.get("counter_mode"):
                counter_mode = True

    return en_keywords, ja_keywords, type_filter, tournament_boost, removal_mode, counter_mode


def expand_query(query: str) -> str:
    en_kws, _, _, _, _, _ = extract_keywords(query)
    if en_kws:
        return " ".join(en_kws[:3]) + " " + query
    return query


def format_filter_sql(fmt: Optional[str]) -> str:
    """legalities フィルタの SQL 断片を生成する"""
    if not fmt:
        return ""
    fmt = fmt.lower()
    if fmt not in VALID_FORMATS:
        print(f"  [警告] 不明なフォーマット: {fmt}。フィルタを無効にします。")
        return ""
    return f"AND c.legalities->>'{fmt}' = 'legal'"


VALID_TYPE_FILTERS = {
    "Creature", "Instant", "Sorcery",
    "Enchantment", "Artifact", "Land", "Planeswalker", "Battle",
}

def type_filter_sql(type_filter: Optional[str]) -> str:
    """type_line フィルタの SQL 断片を生成する"""
    if not type_filter:
        return ""
    # バリデーション: 既知のタイプ以外は無視
    if type_filter not in VALID_TYPE_FILTERS:
        print(f"  [WARN] 無効な type_filter: '{type_filter}' → 無視します")
        return ""
    return f"AND c.type_line LIKE '%%{type_filter}%%'"


# ─── 結果データクラス ─────────────────────────────────────────

@dataclass
class CardResult:
    card_name: str
    type_line: str
    oracle_text: str
    japanese_name: str
    japanese_oracle_text: str
    mana_cost: str
    rarity: str
    vector_rank: Optional[int]
    en_text_rank: Optional[int]
    ja_text_rank: Optional[int]
    rrf_score: float

    def display(self, i: int):
        ja = f" ({self.japanese_name})" if self.japanese_name else ""
        v  = f"vec:{self.vector_rank}"  if self.vector_rank  else "      "
        e  = f"en:{self.en_text_rank}"  if self.en_text_rank else "     "
        j  = f"ja:{self.ja_text_rank}"  if self.ja_text_rank else "     "
        print(f"  [{i:2d}] {self.rrf_score:.4f} {v} {e} {j}  "
              f"{self.card_name}{ja}")
        print(f"       {self.type_line[:50]}  {self.mana_cost or ''}")
        if self.oracle_text:
            print(f"       {self.oracle_text[:80]}")

    def format_text(self, i: int) -> str:
        ja = f" ({self.japanese_name})" if self.japanese_name else ""
        v  = f"vec:{self.vector_rank}"  if self.vector_rank  else "      "
        e  = f"en:{self.en_text_rank}"  if self.en_text_rank else "     "
        j  = f"ja:{self.ja_text_rank}"  if self.ja_text_rank else "     "
        lines = [
            f"  [{i:2d}] {self.rrf_score:.4f} {v} {e} {j}  {self.card_name}{ja}",
            f"       {self.type_line[:50]}  {self.mana_cost or ''}",
        ]
        if self.oracle_text:
            lines.append(f"       {self.oracle_text[:80]}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)


# ─── 検索クラス ───────────────────────────────────────────────

class MTGHybridSearcherV2:
    def __init__(self, model_key: str = "SMALL_V2", rrf_k: int = 60):
        cfg = MODEL_REGISTRY[model_key]
        self.cfg           = cfg
        self.model_key     = model_key
        self.rrf_k         = rrf_k
        self.weight_vector = 1.0  # ベクトル検索の重み
        self.weight_en_fts = 1.0  # 英語FTSの重み
        self.weight_ja_fts = 1.0  # 日本語FTSの重み
        self.model      = SentenceTransformer(
            cfg["model_name"], cache_folder="/mnt/new_hdd/hf_cache"
        )
        self.conn = psycopg2.connect(**get_db_config())
        print(f"[MTGHybridSearcherV2] {model_key} ({cfg['model_name']})")

    def _embed(self, text: str) -> list[float]:
        vec = self.model.encode(
            self.cfg["prefix"] + text, normalize_embeddings=True
        )
        return vec.tolist()

    def _vector_search(
        self, query_vec: list[float], top_k: int,
        fmt_sql: str, type_sql: str,
    ) -> list[dict]:
        cfg     = self.cfg
        vec_str = "[" + ",".join(f"{v:.8f}" for v in query_vec) + "]"
        sql = f"""
            SELECT
                c.card_name, c.type_line, c.oracle_text,
                c.japanese_name, c.japanese_oracle_text,
                c.mana_cost, c.rarity, c.tournament_score,
                1 - (e.embedding <=> '{vec_str}'::vector) AS similarity,
                ROW_NUMBER() OVER (
                    ORDER BY e.embedding <=> '{vec_str}'::vector
                ) AS rank
            FROM {cfg['embeddings_table']} e
            JOIN {cfg['cards_table']} c ON e.card_id = c.id
            WHERE 1=1 {fmt_sql} {type_sql}
            ORDER BY e.embedding <=> '{vec_str}'::vector
            LIMIT {top_k * 3};
        """
        with self.conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _en_text_search(
        self, en_keywords: list[str], top_k: int,
        fmt_sql: str, type_sql: str,
        removal_mode: bool = False,
    ) -> list[dict]:
        """
        英語 oracle_text に対する全文検索。
        removal_mode=True の場合、REMOVAL_TSQUERY を使用して
        exile/destroy の対象がパーマネントタイプの場合のみヒットさせる。
        これにより墓地追放・自己生け贄等を除去クエリから排除できる。
        """
        cfg = self.cfg

        if removal_mode:
            # 除去専用クエリ：パーマネントタイプへの destroy/exile/sacrifice のみヒット
            tsquery = REMOVAL_TSQUERY
            sql = f"""
                SELECT
                    c.card_name, c.type_line, c.oracle_text,
                    c.japanese_name, c.japanese_oracle_text,
                    c.mana_cost, c.rarity, c.tournament_score,
                    ts_rank(
                        to_tsvector('english', COALESCE(c.oracle_text, '')),
                        to_tsquery('english', $tsq$) 
                    ) AS text_score,
                    ROW_NUMBER() OVER (ORDER BY ts_rank(
                        to_tsvector('english', COALESCE(c.oracle_text, '')),
                        to_tsquery('english', $tsq$)
                    ) DESC) AS rank
                FROM {cfg['cards_table']} c
                WHERE to_tsvector('english', COALESCE(c.oracle_text, ''))
                      @@ to_tsquery('english', $tsq$)
                  {fmt_sql} {type_sql}
                ORDER BY text_score DESC
                LIMIT {top_k * 3};
            """
            try:
                with self.conn.cursor() as cur:
                    # $tsq$ dollar quoting で特殊文字を安全に渡す
                    cur.execute(
                        sql.replace("$tsq$", "%s"),
                        (tsquery, tsquery, tsquery)
                    )
                    if cur.description is None:
                        return []
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, row)) for row in cur.fetchall()]
            except Exception as e:
                self.conn.rollback()
                print(f"  [en_fts removal] エラー: {e}")
                return []
        else:
            if not en_keywords:
                return []
            primary = en_keywords[0].replace("'", "''")
            sql = f"""
                SELECT
                    c.card_name, c.type_line, c.oracle_text,
                    c.japanese_name, c.japanese_oracle_text,
                    c.mana_cost, c.rarity, c.tournament_score,
                    ts_rank(
                        to_tsvector('english', COALESCE(c.oracle_text, '')),
                        plainto_tsquery('english', '{primary}')
                    ) AS text_score,
                    ROW_NUMBER() OVER (ORDER BY ts_rank(
                        to_tsvector('english', COALESCE(c.oracle_text, '')),
                        plainto_tsquery('english', '{primary}')
                    ) DESC) AS rank
                FROM {cfg['cards_table']} c
                WHERE to_tsvector('english', COALESCE(c.oracle_text, ''))
                      @@ plainto_tsquery('english', '{primary}')
                  {fmt_sql} {type_sql}
                ORDER BY text_score DESC
                LIMIT {top_k * 3};
            """
            try:
                with self.conn.cursor() as cur:
                    cur.execute(sql)
                    if cur.description is None:
                        return []
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, row)) for row in cur.fetchall()]
            except Exception as e:
                self.conn.rollback()
                print(f"  [en_fts] エラー: {e}")
                return []

    def _ja_text_search(
        self, ja_keywords: list[str], top_k: int,
        fmt_sql: str, type_sql: str,
    ) -> list[dict]:
        if not ja_keywords:
            return []
        cfg = self.cfg

        # パラメータバインディングを使うことで \n 等の特殊文字が正しく渡される
        # f文字列でLIKEを組み立てると \n がバックスラッシュnになってしまう
        kws = ja_keywords[:5]
        placeholders = " OR ".join(
            "c.japanese_oracle_text LIKE %s" for _ in kws
        )
        params = [f"%{kw}%" for kw in kws]

        sql = f"""
            SELECT
                c.card_name, c.type_line, c.oracle_text,
                c.japanese_name, c.japanese_oracle_text,
                c.mana_cost, c.rarity, c.tournament_score,
                ROW_NUMBER() OVER (ORDER BY c.tournament_score DESC NULLS LAST) AS rank
            FROM {cfg['cards_table']} c
            WHERE c.japanese_oracle_text IS NOT NULL
              AND ({placeholders})
              {fmt_sql} {type_sql}
            LIMIT {top_k * 3};
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            self.conn.rollback()
            print(f"  [ja_fts] エラー: {e}")
            return []

    def _rrf_merge(
        self,
        v_rows: list[dict], en_rows: list[dict], ja_rows: list[dict],
        top_k: int,
        tournament_boost: bool = False,
        removal_mode: bool = False,
        counter_mode: bool = False,
    ) -> list[CardResult]:
        k      = self.rrf_k
        w_vec  = self.weight_vector
        w_en   = self.weight_en_fts
        w_ja   = self.weight_ja_fts
        scores: dict[str, dict] = {}

        for row in v_rows:
            name = row["card_name"]
            r    = int(row["rank"])
            if name not in scores:
                scores[name] = {"row": row, "rrf": 0.0,
                                "vr": None, "er": None, "jr": None}
            scores[name]["rrf"] += w_vec / (k + r)
            scores[name]["vr"]   = r

        for row in en_rows:
            name = row["card_name"]
            r    = int(row["rank"])
            if name not in scores:
                scores[name] = {"row": row, "rrf": 0.0,
                                "vr": None, "er": None, "jr": None}
            scores[name]["rrf"] += w_en / (k + r)
            scores[name]["er"]   = r

        for row in ja_rows:
            name = row["card_name"]
            r    = int(row["rank"])
            if name not in scores:
                scores[name] = {"row": row, "rrf": 0.0,
                                "vr": None, "er": None, "jr": None}
            scores[name]["rrf"] += w_ja / (k + r)
            scores[name]["jr"]   = r

        # 除去ルールのペナルティを適用（removal_mode の場合のみ）
        scores = apply_removal_penalties(scores, removal_mode)
        # カウンター呪文ルールのペナルティを適用（counter_mode の場合のみ）
        scores = apply_counter_penalties(scores, counter_mode)

        # tournament_score ボーナスを RRF スコアに加算
        # tournament_boost=True の場合（「最強」「環境」等のクエリ）は強く反映
        boost_coef = 0.10 if tournament_boost else 0.03
        max_ts = max(
            (data["row"].get("tournament_score") or 0)
            for data in scores.values()
        ) or 1
        for data in scores.values():
            ts = data["row"].get("tournament_score") or 0
            data["rrf"] += (ts / max_ts) * boost_coef

        sorted_items = sorted(
            scores.items(), key=lambda x: x[1]["rrf"], reverse=True
        )

        results = []
        for name, data in sorted_items[:top_k]:
            row = data["row"]
            results.append(CardResult(
                card_name=row["card_name"],
                type_line=row.get("type_line") or "",
                oracle_text=(row.get("oracle_text") or ""),
                japanese_name=row.get("japanese_name") or "",
                japanese_oracle_text=(row.get("japanese_oracle_text") or ""),
                mana_cost=row.get("mana_cost") or "",
                rarity=row.get("rarity") or "",
                vector_rank=data["vr"],
                en_text_rank=data["er"],
                ja_text_rank=data["jr"],
                rrf_score=round(data["rrf"], 5),
            ))
        return results

    def search_with_hyde(
        self, query: str, hyde_text: str,
        top_k: int = 10,
        format: Optional[str] = None,
        tournament_boost_override: bool = False,
        removal_mode_override: bool = False,
        counter_mode_override: bool = False,
        type_filter_override: Optional[str] = None,
    ) -> list[CardResult]:
        """
        HyDE（Hypothetical Document Embeddings）を使った検索。
        通常の検索結果と HyDE ベクトル検索結果を RRF でマージする。
        """
        # 通常の検索結果を取得
        normal_results = self.search(
            query, top_k=top_k * 2, format=format,
            tournament_boost_override=tournament_boost_override,
            removal_mode_override=removal_mode_override,
            counter_mode_override=counter_mode_override,
            type_filter_override=type_filter_override,
        )

        # HyDE ベクトル検索（hyde_text を embedding してベクトル検索）
        fmt_sql  = format_filter_sql(format)
        type_sql = type_filter_sql(type_filter_override)
        hyde_vec  = self._embed(hyde_text)
        hyde_rows = self._vector_search(hyde_vec, top_k * 2, fmt_sql, type_sql)

        # 通常検索結果を dict に変換
        normal_scores: dict[str, float] = {}
        for i, r in enumerate(normal_results):
            normal_scores[r.card_name] = 1.0 / (self.rrf_k + i + 1)

        # HyDE 検索結果を RRF でマージ
        hyde_scores: dict[str, float] = {}
        for row in hyde_rows:
            name = row["card_name"]
            r    = int(row["rank"])
            hyde_scores[name] = 1.0 / (self.rrf_k + r)

        # 統合スコア
        all_names = set(normal_scores) | set(hyde_scores)
        merged = []
        for name in all_names:
            score = normal_scores.get(name, 0) + hyde_scores.get(name, 0)
            merged.append((name, score))

        merged.sort(key=lambda x: x[1], reverse=True)

        # 通常検索結果から CardResult を取得
        result_map = {r.card_name: r for r in normal_results}

        # HyDE でのみヒットしたカードを追加取得
        hyde_only = [n for n, _ in merged[:top_k] if n not in result_map]
        if hyde_only:
            placeholders = ",".join(["%s"] * len(hyde_only))
            with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT card_name, type_line, oracle_text, japanese_name,
                           japanese_oracle_text, mana_cost, rarity, tournament_score,
                           colors, keywords
                    FROM {self.cfg['cards_table']}
                    WHERE card_name IN ({placeholders})
                """, hyde_only)
                for row in cur.fetchall():
                    result_map[row["card_name"]] = CardResult(
                        rank=0,
                        card_name=row["card_name"],
                        type_line=row.get("type_line") or "",
                        oracle_text=row.get("oracle_text") or "",
                        japanese_name=row.get("japanese_name") or "",
                        japanese_oracle_text=row.get("japanese_oracle_text") or "",
                        mana_cost=row.get("mana_cost") or "",
                        rarity=row.get("rarity") or "",
                        rrf_score=0.0,
                        vector_rank=None,
                        en_text_rank=None,
                        ja_text_rank=None,
                    )

        # 最終結果を構築
        final = []
        for i, (name, score) in enumerate(merged[:top_k]):
            if name in result_map:
                r = result_map[name]
                r.rank      = i + 1
                r.rrf_score = round(score, 4)
                final.append(r)

        return final

    def search(
        self, query: str, top_k: int = 10,
        format: Optional[str] = None,
        tournament_boost_override: bool = False,
        removal_mode_override: bool = False,
        counter_mode_override: bool = False,
        type_filter_override: Optional[str] = None,
    ) -> list[CardResult]:
        print(f"\n[{self.model_key}] 検索: 「{query}」"
              + (f" [{format}]" if format else ""))
        t0 = time.perf_counter()

        en_kws, ja_kws, type_filter, tournament_boost, removal_mode, counter_mode = extract_keywords(query)

        # override フラグが True の場合は強制的に有効化
        tournament_boost = tournament_boost or tournament_boost_override
        removal_mode     = removal_mode     or removal_mode_override
        counter_mode     = counter_mode     or counter_mode_override
        # type_filter_override が指定された場合は上書き
        if type_filter_override:
            type_filter = type_filter_override
        expanded = expand_query(query)
        if expanded != query:
            print(f"  拡張: {expanded[:80]}")
        if ja_kws:
            print(f"  日本語KW: {ja_kws}")
        if type_filter:
            print(f"  type_filter: {type_filter}")
        if tournament_boost:
            print(f"  tournament_boost: ON（大会実績を強く反映）")
        if removal_mode:
            print(f"  removal_mode: ON（パーマネント除去のみヒット）")
        if counter_mode:
            print(f"  counter_mode: ON（護法カードをスコアダウン）")

        fmt_sql  = format_filter_sql(format)
        type_sql = type_filter_sql(type_filter)

        vec     = self._embed(expanded)
        v_rows  = self._vector_search(vec, top_k, fmt_sql, type_sql)
        en_rows = self._en_text_search(en_kws, top_k, fmt_sql, type_sql,
                                          removal_mode=removal_mode)
        ja_rows = self._ja_text_search(ja_kws, top_k, fmt_sql, type_sql)

        elapsed = (time.perf_counter() - t0) * 1000
        print(f"  vec:{len(v_rows)} en_fts:{len(en_rows)} "
              f"ja_fts:{len(ja_rows)} ({elapsed:.0f}ms)")

        return self._rrf_merge(v_rows, en_rows, ja_rows, top_k,
                               tournament_boost=tournament_boost,
                               removal_mode=removal_mode,
                               counter_mode=counter_mode)

    def close(self):
        self.conn.close()


# ─── ファイル出力 ─────────────────────────────────────────────

def save_results(
    all_results: list[dict],
    output_prefix: str,
    model_key: str,
    fmt: Optional[str],
):
    """JSON と読みやすいテキストの2形式で出力する"""
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = f"{output_prefix}_{ts}.json"
    txt_path  = f"{output_prefix}_{ts}.txt"

    # JSON 出力
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    # テキスト出力（人間が読みやすい形式）
    with open(txt_path, "w", encoding="utf-8") as f:
        header = f"MTG Hybrid Search Results\n"
        header += f"Model: {model_key}"
        if fmt:
            header += f"  Format: {fmt}"
        header += f"  Generated: {ts}\n"
        header += "=" * 70 + "\n"
        f.write(header)

        for entry in all_results:
            q      = entry["query"]
            fmt_q  = entry.get("format") or ""
            fmt_label = f" [{fmt_q}]" if fmt_q else ""
            f.write(f"\n【{q}】{fmt_label}\n")
            f.write(f"  拡張: {entry.get('expanded_query', '')[:80]}\n")
            f.write(f"  hits: vec={entry['vec_count']} "
                    f"en={entry['en_count']} ja={entry['ja_count']} "
                    f"({entry['elapsed_ms']:.0f}ms)\n")
            f.write("  " + "-" * 60 + "\n")
            for r in entry["results"]:
                ja      = f" ({r['japanese_name']})" if r.get("japanese_name") else ""
                v_rank  = f"vec:{r['vector_rank']}"  if r.get("vector_rank")  else "      "
                e_rank  = f"en:{r['en_text_rank']}"  if r.get("en_text_rank") else "     "
                j_rank  = f"ja:{r['ja_text_rank']}"  if r.get("ja_text_rank") else "     "
                f.write(
                    f"  [{r['rank']:2d}] {r['rrf_score']:.4f} "
                    f"{v_rank} {e_rank} {j_rank}  "
                    f"{r['card_name']}{ja}\n"
                )
                f.write(f"       {r['type_line'][:50]}  {r['mana_cost']}\n")
                if r.get("oracle_text"):
                    f.write(f"       {r['oracle_text'][:80]}\n")
            f.write("\n")

    print(f"\n出力完了:")
    print(f"  JSON: {json_path}")
    print(f"  TEXT: {txt_path}")


# ─── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MTG ハイブリッド検索")
    parser.add_argument("model",   nargs="?", default="SMALL_V2",
                        choices=list(MODEL_REGISTRY.keys()))
    parser.add_argument("format",  nargs="?", default=None,
                        help="フォーマット絞り込み（modern / standard 等）")
    parser.add_argument("--output", "-o", default=None,
                        help="出力ファイルのプレフィックス（例: results）")
    parser.add_argument("--top_k", "-k", type=int, default=10)
    parser.add_argument("--query", "-q", default=None,
                        help="単一クエリを実行する場合に指定")
    args = parser.parse_args()

    model_key = args.model
    fmt       = args.format

    # デモクエリ一覧
    demo_queries = [
        ("純粋に強いカウンター呪文",   None),
        ("カードを2枚引く",             None),
        ("最強の単体除去",              None),
        ("飛行を持つクリーチャー",      None),
        ("モダンの最強カウンター呪文",  "modern"),
        ("スタンダードの単体除去",      "standard"),
        ("パイオニアのマナ加速",        "pioneer"),
    ]

    # 単一クエリ指定の場合
    if args.query:
        demo_queries = [(args.query, fmt)]
    elif fmt:
        # CLI からフォーマット指定がある場合は全クエリに適用
        demo_queries = [(q, fmt) for q, _ in demo_queries]

    searcher   = MTGHybridSearcherV2(model_key=model_key)
    all_output = []  # ファイル出力用

    for q, f in demo_queries:
        t0      = time.perf_counter()
        results = searcher.search(q, top_k=args.top_k, format=f)
        elapsed = (time.perf_counter() - t0) * 1000

        # ターミナル表示
        print(f"  TOP 5:")
        for i, r in enumerate(results[:5], 1):
            r.display(i)
        print()

        # ファイル出力用データ収集
        if args.output:
            en_kws, ja_kws, _, _, _, _ = extract_keywords(q)
            all_output.append({
                "query":          q,
                "format":         f,
                "model":          model_key,
                "expanded_query": expand_query(q),
                "elapsed_ms":     round(elapsed, 1),
                "vec_count":      len(results),
                "en_count":       len(en_kws),
                "ja_count":       len(ja_kws),
                "results": [
                    {
                        "rank":                 i + 1,
                        "card_name":            r.card_name,
                        "japanese_name":        r.japanese_name,
                        "type_line":            r.type_line,
                        "oracle_text":          r.oracle_text,
                        "japanese_oracle_text": r.japanese_oracle_text,
                        "mana_cost":            r.mana_cost,
                        "rarity":               r.rarity,
                        "rrf_score":            r.rrf_score,
                        "vector_rank":          r.vector_rank,
                        "en_text_rank":         r.en_text_rank,
                        "ja_text_rank":         r.ja_text_rank,
                    }
                    for i, r in enumerate(results)
                ],
            })

    if args.output and all_output:
        save_results(all_output, args.output, model_key, fmt)

    searcher.close()
