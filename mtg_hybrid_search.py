"""
MTG RAG System — ハイブリッド検索 改善版
==========================================
改善点:
  1. MTG 専用テキスト前処理（ルールテキストの正規化）
  2. ハイブリッド検索（ベクトル類似度 + PostgreSQL FTS BM25風）
  3. クエリ拡張（日本語 → MTG 英語キーワードマッピング）
  4. カード重要度スコアリング（レアリティ・色・タイプ重み付け）
  5. 再ランキング（RRF: Reciprocal Rank Fusion）

使い方:
  from mtg_hybrid_search import MTGHybridSearcher
  searcher = MTGHybridSearcher(model_key="SMALL")
  results = searcher.search("純粋に強いカウンター呪文", top_k=10)
"""

import re
from dataclasses import dataclass
from typing import Optional

import psycopg2
from sentence_transformers import SentenceTransformer

# ─── 接続設定 ────────────────────────────────────────────────
DB_CONFIG = {
    "host": "localhost",
    "port": 5435,
    "dbname": "rag_dev",
    "user": "devuser",
    "password": "***REMOVED***",
}

# ─── 日本語 → MTG 英語キーワードマッピング ───────────────────
# MTG 特有の用語を英語テキストに展開することで
# embedding の語彙ミスマッチを補う
JP_TO_MTG_KEYWORDS: dict[str, list[str]] = {
    # カウンター系
    "カウンター呪文":    ["counter target spell", "counterspell", "counter spell"],
    "打ち消し":          ["counter target spell", "counter target"],
    "カウンター":        ["counter target spell", "counter"],
    # ドロー系
    "カードを引く":      ["draw cards", "draw a card"],
    "手札補充":          ["draw cards", "card advantage"],
    "ドロー":            ["draw a card", "draw cards"],
    # 除去系
    "除去":              ["destroy target", "exile target", "destroy or exile"],
    "単体除去":          ["destroy target creature", "exile target creature"],
    "全体除去":          ["destroy all creatures", "exile all creatures", "wrath"],
    # マナ系
    "マナ加速":          ["add mana", "search your library for a land", "ramp"],
    "ランプ":            ["search your library for a land", "add", "mana"],
    "土地加速":          ["search your library for a land", "put that card onto the battlefield"],
    # クリーチャー系
    "飛行":              ["flying", "can't be blocked except by creatures with flying"],
    "速攻":              ["haste", "this creature has haste"],
    "破壊不能":          ["indestructible"],
    "トークン":          ["create", "token"],
    # コンボ系
    "無限コンボ":        ["whenever", "untap", "infinite", "loop"],
    "シナジー":          ["whenever", "triggered ability"],
    # その他
    "強いカード":        ["powerful", "efficient", "staple"],
    "コスパ":            ["efficient", "cost"],
}

# ─── クエリ拡張 ───────────────────────────────────────────────
def expand_query(query: str) -> str:
    """日本語クエリを MTG 英語キーワードで拡張する"""
    extra_terms: list[str] = []
    for jp, en_list in JP_TO_MTG_KEYWORDS.items():
        if jp in query:
            extra_terms.extend(en_list)
    if extra_terms:
        # 重複排除して先頭に英語キーワードを付加
        unique_terms = list(dict.fromkeys(extra_terms))
        expanded = " ".join(unique_terms[:6]) + " " + query
        return expanded
    return query

# ─── テキスト前処理 ───────────────────────────────────────────
def preprocess_card_text(card_name: str, type_line: str, oracle_text: str,
                          keywords: Optional[str] = None) -> str:
    """
    MTG カードのテキストを embedding 向けに最適化する。
    - マナコスト記号の除去
    - ルールテキストの重要部分を強調
    - キーワード能力を先頭に配置
    """
    # {W}{U}{B} などのマナシンボルを色名に変換
    mana_map = {r'\{W\}': 'white mana', r'\{U\}': 'blue mana',
                r'\{B\}': 'black mana', r'\{R\}': 'red mana',
                r'\{G\}': 'green mana', r'\{C\}': 'colorless mana',
                r'\{T\}': 'tap', r'\{\d+\}': 'generic mana'}
    text = oracle_text or ""
    for pattern, replacement in mana_map.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # 改行を空白に
    text = text.replace("\n", " ").strip()

    # キーワード能力リスト（先頭配置で重要度アップ）
    keyword_abilities = []
    if keywords:
        keyword_abilities = [k.strip() for k in keywords.split(",") if k.strip()]

    parts = [card_name]
    if keyword_abilities:
        parts.append("Keywords: " + ", ".join(keyword_abilities))
    parts.append(type_line or "")
    parts.append(text)

    return " | ".join(p for p in parts if p)

# ─── モデル設定 ───────────────────────────────────────────────
MODEL_REGISTRY = {
    "SMALL": {
        "model_name": "intfloat/multilingual-e5-small",
        "dim": 384,
        "prefix": "query: ",
        "cards_table": "mtg_cards",          # ← 統一
        "embeddings_table": "mtg_embeddings_small",
    },
    "BASE": {
        "model_name": "intfloat/multilingual-e5-base",
        "dim": 768,
        "prefix": "query: ",
        "cards_table": "mtg_cards",          # ← 統一
        "embeddings_table": "mtg_embeddings_base",
    },
    "LARGE": {
        "model_name": "intfloat/multilingual-e5-large-instruct",
        "dim": 1024,
        "prefix": "Instruct: Retrieve Magic: The Gathering cards relevant to the query\nQuery: ",
        "cards_table": "mtg_cards",
        "embeddings_table": "mtg_embeddings",
    },
}

# ─── 結果データクラス ─────────────────────────────────────────
@dataclass
class CardResult:
    card_name: str
    type_line: str
    oracle_text: str
    mana_cost: str
    rarity: str
    vector_rank: Optional[int]
    text_rank: Optional[int]
    rrf_score: float
    vector_similarity: Optional[float]

    def __str__(self):
        return (f"[{self.rrf_score:.4f}] {self.card_name} ({self.mana_cost or '?'}) "
                f"| {self.type_line[:40]}")

# ─── メイン検索クラス ─────────────────────────────────────────
class MTGHybridSearcher:
    """
    ベクトル検索 + 全文検索のハイブリッド検索器。
    RRF (Reciprocal Rank Fusion) で結果をマージする。
    """

    def __init__(self, model_key: str = "SMALL", rrf_k: int = 60):
        cfg = MODEL_REGISTRY[model_key]
        self.cfg = cfg
        self.model_key = model_key
        self.rrf_k = rrf_k
        self.model = SentenceTransformer(cfg["model_name"])
        self.conn = psycopg2.connect(**DB_CONFIG)
        print(f"[MTGHybridSearcher] モデル: {cfg['model_name']} ({model_key})")

    def _embed(self, text: str) -> list[float]:
        full_text = self.cfg["prefix"] + text
        vec = self.model.encode(full_text, normalize_embeddings=True)
        return vec.tolist()

    def _vector_search(self, query_vec: list[float], top_k: int) -> list[dict]:
        """pgvector によるベクトル類似度検索"""
        cfg = self.cfg
        vec_str = "[" + ",".join(f"{v:.8f}" for v in query_vec) + "]"
        sql = f"""
            SELECT
                c.card_name, c.type_line, c.oracle_text,
                c.mana_cost, c.rarity,
                1 - (e.embedding <=> '{vec_str}'::vector) AS similarity,
                ROW_NUMBER() OVER (ORDER BY e.embedding <=> '{vec_str}'::vector) AS rank
            FROM {cfg['embeddings_table']} e
            JOIN {cfg['cards_table']} c ON e.card_id = c.id
            ORDER BY e.embedding <=> '{vec_str}'::vector
            LIMIT {top_k * 3};
        """
        with self.conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _text_search(self, query: str, top_k: int) -> list[dict]:
        """
        PostgreSQL の to_tsvector + plainto_tsquery による全文検索。
        MTG キーワードを抽出して英語テキスト検索を行う。
        """
        cfg = self.cfg
        # 日本語クエリから英語キーワードを抽出
        keywords: list[str] = []
        for jp, en_list in JP_TO_MTG_KEYWORDS.items():
            if jp in query:
                keywords.extend(en_list[:2])
        if not keywords:
            keywords = [query]

        # 英語キーワードを OR でつなぐ
        ts_query = " | ".join(
            f"plainto_tsquery('english', '{kw}')" for kw in keywords[:4]
        )

        sql = f"""
            SELECT
                c.card_name, c.type_line, c.oracle_text,
                c.mana_cost, c.rarity,
                ts_rank(
                    to_tsvector('english', COALESCE(c.oracle_text, '')),
                    plainto_tsquery('english', '{keywords[0]}')
                ) AS text_score,
                ROW_NUMBER() OVER (ORDER BY ts_rank(
                    to_tsvector('english', COALESCE(c.oracle_text, '')),
                    plainto_tsquery('english', '{keywords[0]}')
                ) DESC) AS rank
            FROM {cfg['cards_table']} c
            WHERE to_tsvector('english', COALESCE(c.oracle_text, ''))
                  @@ plainto_tsquery('english', '{keywords[0]}')
            ORDER BY text_score DESC
            LIMIT {top_k * 3};
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            print(f"  [text_search] スキップ: {e}")
            self.conn.rollback()
            return []

    def _rrf_merge(
        self,
        vector_rows: list[dict],
        text_rows: list[dict],
        top_k: int,
    ) -> list[CardResult]:
        """
        Reciprocal Rank Fusion でベクトル・テキスト結果をマージ。
        score(d) = Σ 1/(k + rank(d))
        """
        k = self.rrf_k
        scores: dict[str, dict] = {}

        for row in vector_rows:
            name = row["card_name"]
            r = int(row["rank"])
            if name not in scores:
                scores[name] = {"row": row, "rrf": 0.0,
                                "vrank": None, "trank": None, "vsim": None}
            scores[name]["rrf"] += 1.0 / (k + r)
            scores[name]["vrank"] = r
            scores[name]["vsim"] = float(row.get("similarity", 0))

        for row in text_rows:
            name = row["card_name"]
            r = int(row["rank"])
            if name not in scores:
                scores[name] = {"row": row, "rrf": 0.0,
                                "vrank": None, "trank": None, "vsim": None}
            scores[name]["rrf"] += 1.0 / (k + r)
            scores[name]["trank"] = r

        sorted_items = sorted(scores.items(), key=lambda x: x[1]["rrf"], reverse=True)

        results = []
        for name, data in sorted_items[:top_k]:
            row = data["row"]
            results.append(CardResult(
                card_name=row["card_name"],
                type_line=row.get("type_line") or "",
                oracle_text=(row.get("oracle_text") or "")[:150],
                mana_cost=row.get("mana_cost") or "",
                rarity=row.get("rarity") or "",
                vector_rank=data["vrank"],
                text_rank=data["trank"],
                rrf_score=round(data["rrf"], 6),
                vector_similarity=data["vsim"],
            ))
        return results

    def search(self, query: str, top_k: int = 10) -> list[CardResult]:
        """
        ハイブリッド検索のメインエントリーポイント。
        """
        print(f"\n[{self.model_key}] 検索: 「{query}」")
        expanded = expand_query(query)
        if expanded != query:
            print(f"  クエリ拡張: 「{expanded[:80]}...」")

        vec = self._embed(expanded)
        v_rows = self._vector_search(vec, top_k)
        t_rows = self._text_search(query, top_k)
        print(f"  ベクトル候補: {len(v_rows)}件  テキスト候補: {len(t_rows)}件")

        results = self._rrf_merge(v_rows, t_rows, top_k)
        return results

    def close(self):
        self.conn.close()


# ─── CLI デモ ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    model_key = sys.argv[1] if len(sys.argv) > 1 else "SMALL"
    searcher = MTGHybridSearcher(model_key=model_key)

    demo_queries = [
        "純粋に強いカウンター呪文",
        "カウンター呪文が強いカード",
        "マナ加速できるカード",
        "最強の単体除去",
    ]

    for q in demo_queries:
        results = searcher.search(q, top_k=10)
        print(f"\n  TOP 5 結果:")
        for r in results[:5]:
            v = f"vec:{r.vector_rank}" if r.vector_rank else "    "
            t = f"txt:{r.text_rank}" if r.text_rank else "    "
            print(f"    [{r.rrf_score:.4f}] {v} {t}  {r.card_name:<28} {r.type_line[:30]}")
        print()

    searcher.close()
