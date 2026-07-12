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
import re
import time
import datetime
import argparse
from dataclasses import dataclass, asdict
from typing import Optional

import psycopg2
from sentence_transformers import SentenceTransformer

from db import make_db  # DB ドライバ切替層（psycopg2 / Aurora Data API・2026-07-12）
# 役割判定（removal/counter）は構造化列 target_types / removal（enrich_removal.py 由来）
# へ移行済み（P1: 正しさ＞点数）。旧 mtg_removal_rules / mtg_counter_rules の手書き
# 文字列マッチはもう使わない。

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
    # mana_struct: このカードは is_mana_boost（構造化列・ネットマナ判定）で表現できる＝
    #   意味検索を必要としない "構造化に落ちる" 意味語。EDH 直行路の fuzzy 判定で構造化扱い。
    #   （「ランプ」「土地加速」＝土地サーチは is_mana_boost の net-mana 定義と一致しない
    #    ことがあるので tag しない＝fuzzy 扱いのまま・保守的に）
    "マナ加速":        {"en": "add mana",               "ja": ["マナを加える"], "mana_struct": True},
    "ランプ":          {"en": "search your library for a land", "ja": ["土地を戦場に出す"]},
    "土地加速":        {"en": "search your library for a land", "ja": ["あなたのライブラリーから土地"]},
    # クリーチャー能力（type_filter で Creature に絞る）
    # "keyword" = Scryfall keywords 配列の表記（自身の生得能力のみ載る＝R8補足a の
    # crisp な代理）。keyword_filter_sql() が全検索腕の WHERE に生得持ち条件を足す
    # （ハードフィルタ。付与/除去意図のクエリでは extract_keywords 側のガードで不発）。
    "飛行を持つクリーチャー": {"en": "flying", "ja": ["飛行"],
                               "type_filter": "Creature", "keyword": "Flying"},
    "飛行持ち":        {"en": "flying", "ja": ["飛行"],
                        "type_filter": "Creature", "keyword": "Flying"},
    "速攻":            {"en": "haste",        "ja": ["速攻"], "keyword": "Haste"},
    "破壊不能":        {"en": "indestructible","ja": ["破壊不能"], "keyword": "Indestructible"},
    "絆魂":            {"en": "lifelink",      "ja": ["絆魂"], "keyword": "Lifelink"},
    "接死":            {"en": "deathtouch",    "ja": ["接死"], "keyword": "Deathtouch"},
    "先制攻撃":        {"en": "first strike",  "ja": ["先制攻撃"], "keyword": "First strike"},
    "トランプル":      {"en": "trample",       "ja": ["トランプル"], "keyword": "Trample"},
    "威迫":            {"en": "menace",        "ja": ["威迫"], "keyword": "Menace"},
    "到達":            {"en": "reach",         "ja": ["到達"], "keyword": "Reach"},
    "警戒":            {"en": "vigilance",     "ja": ["警戒"], "keyword": "Vigilance"},
    # 飛行（単体キーワード → type_filter なし）
    "飛行":            {"en": "flying",        "ja": ["飛行"], "keyword": "Flying"},
    # ─ 生得キーワード第2弾（2026-07-11 本人裁定「キーワードは全部入れていい」・
    #   常盤木＋廃止済み戦闘/回避系23語。訳語は japanese_oracle_text との照合で
    #   全 94〜100% 一致を機械検証済み。廃止語も勝手に現行語へ正規化しない＝
    #   質問者の語彙を上書きしない原則〔威嚇=Intimidate 21枚は威迫=Menace と別物〕）─
    "瞬速":            {"en": "flash",         "ja": ["瞬速"], "keyword": "Flash"},
    "防衛":            {"en": "defender",      "ja": ["防衛"], "keyword": "Defender"},
    "プロテクション":  {"en": "protection",    "ja": ["プロテクション"], "keyword": "Protection"},
    "護法":            {"en": "ward",          "ja": ["護法"], "keyword": "Ward"},
    "二段攻撃":        {"en": "double strike", "ja": ["二段攻撃"], "keyword": "Double strike"},
    "呪禁":            {"en": "hexproof",      "ja": ["呪禁"], "keyword": "Hexproof"},
    "果敢":            {"en": "prowess",       "ja": ["果敢"], "keyword": "Prowess"},
    "感染":            {"en": "infect",        "ja": ["感染"], "keyword": "Infect"},
    "畏怖":            {"en": "fear",          "ja": ["畏怖"], "keyword": "Fear"},
    "毒性":            {"en": "toxic",         "ja": ["毒性"], "keyword": "Toxic"},
    "シャドー":        {"en": "shadow",        "ja": ["シャドー"], "keyword": "Shadow"},
    "被覆":            {"en": "shroud",        "ja": ["被覆"], "keyword": "Shroud"},
    "賛美":            {"en": "exalted",       "ja": ["賛美"], "keyword": "Exalted"},
    "側面攻撃":        {"en": "flanking",      "ja": ["側面攻撃"], "keyword": "Flanking"},
    "馬術":            {"en": "horsemanship",  "ja": ["馬術"], "keyword": "Horsemanship"},
    "萎縮":            {"en": "wither",        "ja": ["萎縮"], "keyword": "Wither"},
    "バンド":          {"en": "banding",       "ja": ["バンド"], "keyword": "Banding"},
    "頑強":            {"en": "persist",       "ja": ["頑強"], "keyword": "Persist"},
    "不死":            {"en": "undying",       "ja": ["不死"], "keyword": "Undying"},
    "威嚇":            {"en": "intimidate",    "ja": ["威嚇"], "keyword": "Intimidate"},
    "消失":            {"en": "vanishing",     "ja": ["消失"], "keyword": "Vanishing"},
    "潜伏":            {"en": "skulk",         "ja": ["潜伏"], "keyword": "Skulk"},
    "滅殺":            {"en": "annihilator",   "ja": ["滅殺"], "keyword": "Annihilator"},
    # ストップ語（空エントリ＝部分文字列サプレッションで内側のキーを無効化するだけ）:
    # 「不死鳥」で「不死」(Undying) が誤発火するとフェニックスが検索から消える
    "不死鳥":          {},
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


# 日本語のカードタイプ語 → type_line フィルタ（2026-07-11・本人の実地テストが発見した
# 「直行路は type 語が見えない」穴への対応）。検出は「クエリ末尾の名詞句主要部」に限る:
#   「速攻を持つアーティファクト」  → 末尾＝答えのタイプ ＝ 立てる
#   「アーティファクトを破壊するカード」→ 末尾は「カード」＝ 立てない（答えは呪文側。
#     対象語（を格）を type にすると 7/9 Nova の有害誤付与と同じ間違いを決定的コードで犯す）
#   「土地加速」→ 末尾は「加速」＝ 複合語も自然に不発
TYPE_WORDS_JA = {
    "クリーチャー":         "Creature",
    "アーティファクト":     "Artifact",
    "エンチャント":         "Enchantment",
    "インスタント":         "Instant",
    "ソーサリー":           "Sorcery",
    "プレインズウォーカー": "Planeswalker",
    "土地":                 "Land",
    "バトル":               "Battle",
}

# キーワード能力の否定表現（「〈kw〉を持たない」等・キーワードキー直後のみ）。
# embedding/FTS は否定が原理的に見えない（「持つ」と「持たない」がほぼ同じベクトル）
# ＝ crisp な否定は SQL の NOT で解く（2026-07-11・設計思想どおりの置き場所）
_NEG_AFTER_KW = r'(?:を|は)?(?:持たない|持ってない|持っていない|が\s*無い|がない|無し|なし|以外)'

# P/T の列間関係（「パワーとタフネスが同じ」等・2026-07-12 本人要望「答えが明確
# だからできてほしかった」）。filters スキーマは絶対値の範囲しか持たず「列同士の
# 関係」を表現できない＝ルーターにも embedding にも解けない層。決定的検出で
# SQL に直結する（EDH 色検出と同じパターン・ルーター無改修・キャッシュ不要）
_PT_EQ_RE  = re.compile(r'(?:パワーとタフネス|タフネスとパワー|Ｐ?/?Ｔ|P/?T)\s*が?\s*(?:同じ|等し|一緒)')
_PT_PGT_RE = re.compile(r'パワー\s*(?:の方)?が?\s*タフネスより\s*(?:大き|高|上)'
                        r'|タフネスより\s*パワー\s*(?:の方)?が?\s*(?:大き|高|上)'
                        r'|パワーの方が(?:大き|高)いクリーチャー')
_PT_TGT_RE = re.compile(r'タフネス\s*(?:の方)?が?\s*パワーより\s*(?:大き|高|上)'
                        r'|パワーより\s*タフネス\s*(?:の方)?が?\s*(?:大き|高|上)'
                        r'|タフネスの方が(?:大き|高)いクリーチャー')


# 部族（クリーチャー・サブタイプ）検索の日英辞書（2026-07-12・本人発見「『蟹』の
# 正解率が芳しくない」＝蟹デッキを組みたい人の部族検索需要）。ルーターの type_filter
# はメジャータイプ8種のみでサブタイプの語彙が無く、embedding は「蟹」から水辺の動物
# 一般を返す（実測 2/10）＝正解集合 type_line LIKE '%Crab%' (44枚) は crisp に在るのに
# 届かない層 → 決定的辞書で SQL 直結（キーワード23語・type 語と同じ型）。
# 【第1弾=訳語の曖昧性と一般語衝突が無い安全系のみ】。多義系（人間/壁/英雄/悪魔/猿等）
# は本人レビュー待ちの第2弾（human-in-the-loop・語彙学習 v1 の運用）。
SUBTYPE_WORDS_JA = {
    # カタカナ系（公式訳が一意・衝突なし）
    "ゴブリン": "Goblin",   "エルフ": "Elf",         "ゾンビ": "Zombie",
    "ドラゴン": "Dragon",   "マーフォーク": "Merfolk", "スピリット": "Spirit",
    "ウィザード": "Wizard", "シャーマン": "Shaman",   "クレリック": "Cleric",
    "ドルイド": "Druid",    "ビースト": "Beast",      "エレメンタル": "Elemental",
    "デーモン": "Demon",    "フェアリー": "Faerie",   "ゴーレム": "Golem",
    "スフィンクス": "Sphinx", "ハイドラ": "Hydra",    "クラーケン": "Kraken",
    "リバイアサン": "Leviathan", "ミノタウルス": "Minotaur", "トロール": "Troll",
    "オーク": "Orc",        "インプ": "Imp",          "デビル": "Devil",
    "ドワーフ": "Dwarf",    "スリヴァー": "Sliver",   "マイア": "Myr",
    "エルドラージ": "Eldrazi", "ファイレクシアン": "Phyrexian", "アバター": "Avatar",
    "ツリーフォーク": "Treefolk", "ユニコーン": "Unicorn", "ペガサス": "Pegasus",
    "グリフィン": "Griffin", "フェニックス": "Phoenix", "ウーズ": "Ooze",
    "スケルトン": "Skeleton", "リス": "Squirrel",     "カエル": "Frog",
    "コウモリ": "Bat",      "トカゲ": "Lizard",       "ホラー": "Horror",
    "ネズミ": "Rat",        "ウサギ": "Rabbit",       "ブラッシュワグ": "Brushwagg",
    # 漢字複合系（部族以外の読みがまず来ない）
    "吸血鬼": "Vampire",    "恐竜": "Dinosaur",       "海賊": "Pirate",
    "騎士": "Knight",       "忍者": "Ninja",          "侍": "Samurai",
    "巨人": "Giant",        "昆虫": "Insect",         "天使": "Angel",
    "狼男": "Werewolf",     "植物": "Plant",          "構築物": "Construct",
    "多相の戦士": "Shapeshifter", "狂戦士": "Berserker", "暗殺者": "Assassin",
    "戦士": "Warrior",      "兵士": "Soldier",        "工匠": "Artificer",
    "同盟者": "Ally",       "山羊": "Goat",           "羊": "Sheep",
    "海蛇": "Serpent",      "蜘蛛": "Spider",
    # 漢字単字系（末尾ルール前提なら誤爆余地が小さい・長いキー優先で内部衝突解決:
    # 不死鳥→Phoenix が 鳥→Bird に勝つ / 海蛇→Serpent が 蛇→Snake に勝つ）
    "不死鳥": "Phoenix",
    "蟹": "Crab",   "鮫": "Shark",  "鯨": "Whale",  "狐": "Fox",
    "猪": "Boar",   "狼": "Wolf",   "熊": "Bear",   "猫": "Cat",
    "犬": "Dog",    "鳥": "Bird",   "蛇": "Snake",  "亀": "Turtle",
    "魚": "Fish",   "馬": "Horse",  "象": "Elephant",
}
# 部族意図の定型（末尾以外でも部族と確信できる形）
_TRIBAL_CONTEXT_RE = r'(?:デッキ|の統率者|部族|タイプ)'


def detect_tribal(query: str):
    """日本語の部族（サブタイプ）検索意図の決定的検出。英語 subtype か None を返す。
    発動条件（保守的）: ①クエリ全体が部族名 ②クエリ末尾が部族名（「青い蟹」）
    ③「〈部族〉デッキ/の統率者/部族」の定型。文中に埋まっただけでは立てない
    （「エルフを対象に…」等の対象語で誤爆させない＝type 語検出と同じ思想）。
    複数マッチは最長キー優先（「不死鳥」>「鳥」・「多相の戦士」>「戦士」）。"""
    stripped = query.strip().rstrip('?？。！!、．.　 ')
    hits = []
    for jp, en in SUBTYPE_WORDS_JA.items():
        if (stripped == jp or stripped.endswith(jp)
                or re.search(re.escape(jp) + _TRIBAL_CONTEXT_RE, query)):
            hits.append((len(jp), jp, en))
    if not hits:
        return None
    hits.sort(reverse=True)          # 最長キー優先
    longest = hits[0]
    # 最長キーの部分文字列でしかないヒットは捨てる（不死鳥 vs 鳥）
    return longest[2]


def tribal_filter_sql(subtype) -> str:
    """部族フィルタの SQL 断片。単語境界つき正規表現（\\m..\\M）で type_line に
    照合＝LIKE の部分一致事故（別語の巻き込み）を避ける。Creature に限定しない:
    部族シナジー呪文（Tribal Instant — Goblin 等）も type_line に部族名を持ち、
    部族デッキの検索意図に含まれるため。"""
    if not subtype:
        return ""
    return f" AND c.type_line ~ '\\m{subtype}\\M'"


def detect_pt_relation(query: str):
    """P/T の列間関係の決定的検出。'eq' / 'power_gt' / 'toughness_gt' / None。"""
    if _PT_EQ_RE.search(query):
        return 'eq'
    if _PT_PGT_RE.search(query):
        return 'power_gt'
    if _PT_TGT_RE.search(query):
        return 'toughness_gt'
    return None


def pt_relation_sql(rel) -> str:
    """P/T 関係フィルタの SQL 断片。power/toughness は text 列で '*' や 'X' 等の
    特殊値を含むため、両方が素の数値のカードに限って ::int 比較する（'*/*' 等の
    不定値は「同じ」と断定できない＝保守的に除外）。text のまま比較しないのは
    '10' < '9' の辞書順事故を避けるため。"""
    op = {'eq': '=', 'power_gt': '>', 'toughness_gt': '<'}.get(rel)
    if not op:
        return ""
    return (" AND c.power ~ '^[0-9]+$' AND c.toughness ~ '^[0-9]+$'"
            f" AND c.power::int {op} c.toughness::int")


def extract_keywords(query: str) -> tuple[list[str], list[str], Optional[str], bool, bool, bool, list[str], list[str], bool]:
    """
    クエリからキーワードと各フラグを抽出する。
    戻り値: (英語キーワードリスト, 日本語キーワードリスト, type_filter,
             tournament_boost, removal_mode, counter_mode,
             kw_abilities, neg_kw_abilities, kw_only)
    kw_abilities     = クエリが「持つ」ことを求める生得キーワード（front_keywords @> の門）
    neg_kw_abilities = クエリが「持たない」ことを求める生得キーワード（NOT && の門・
                       2026-07-11 否定形対応）。keyword エントリ以外（除去等）の否定は
                       複雑度が高いため対象外＝従来どおりルーター/意味検索に任せる（保守的）
    kw_only = 辞書レベルで「キーワード能力以外の意味語が無い」＝構造化オンリー候補
    （最終判断は search() 側で boost/removal/counter の override 込みで行う）。
    """
    en_keywords: list[str] = []
    ja_keywords: list[str] = []
    type_filter: Optional[str] = None
    tournament_boost: bool = False
    removal_mode: bool = False
    counter_mode: bool = False
    kw_abilities: list[str] = []
    neg_kw_abilities: list[str] = []
    other_semantic: bool = False

    # 一致キーを集め、別の(より長い)一致キーの部分文字列であるキーは捨てる。
    # 例: 「トランプル」一致時に内部の「ランプ」(ramp→search for a land)を誤注入しない。
    matched = [jp for jp in QUERY_EXPAND if jp in query]
    matched = [k for k in matched if not any(k != o and k in o for o in matched)]
    for jp in matched:
        terms = QUERY_EXPAND[jp]
        kw = terms.get("keyword")
        # 否定文脈（「速攻を持たない」等）: keyword エントリに限り negative へ回す。
        # en/ja の意味検索注入もスキップ＝検索を正極性（持つ側）へ引っ張らない
        if kw and re.search(re.escape(jp) + _NEG_AFTER_KW, query):
            if kw not in neg_kw_abilities:
                neg_kw_abilities.append(kw)
            continue
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
        if kw:
            if kw not in kw_abilities:
                kw_abilities.append(kw)
        elif en or ja:
            # キーワード能力エントリ以外の意味語（除去/ドロー/マナ加速等）が混ざってる
            other_semantic = True

    # 日本語 type 語の検出（末尾ルール・辞書エントリ由来の type_filter が無いときだけ補完）
    if type_filter is None:
        stripped = query.strip().rstrip('?？。！!、．.　 ')
        for jp_type, en_type in TYPE_WORDS_JA.items():
            if stripped.endswith(jp_type):
                type_filter = en_type
                break

    # 生得キーワードのハードフィルタを発動しない条件（極性ガード）:
    # (1) 除去/カウンター意図（例:「破壊不能を除去できるカード」＝答えは持たない側の呪文）
    # (2) 付与意図（例:「破壊不能を付与するカード」＝答えは付与する側＝生得持ちでない）
    # negative 側も同時に消す（除去/付与と否定の複合クエリは複雑度が高い＝保守的に全降ろし）
    if removal_mode or counter_mode or any(
            w in query for w in ('付与', '与え', '得る', '得られ', '持たせ', '授け')):
        kw_abilities = []
        neg_kw_abilities = []

    kw_only = bool(kw_abilities or neg_kw_abilities) and not other_semantic

    return (en_keywords, ja_keywords, type_filter,
            tournament_boost, removal_mode, counter_mode,
            kw_abilities, neg_kw_abilities, kw_only)


def expand_query(query: str) -> str:
    en_kws, _, _, _, _, _, _, _, _ = extract_keywords(query)
    if en_kws:
        return " ".join(en_kws[:3]) + " " + query
    return query


def has_fuzzy_semantic(query: str) -> bool:
    """クエリに「構造化列で表現できない意味語」（fuzzy な意味）が在るか。
    fuzzy = ドロー・コンボ・トークン・バウンス・土地ランプ等＝意味検索が要る概念。
    構造化に落ちる語（キーワード能力=front_keywords / マナ加速=is_mana_boost /
    tournament_boost・removal・counter＝別途フラグで処理）は fuzzy に数えない。
    EDH 直行路（意味検索スキップ）を「意味の残余が構造化フラグだけ」のときに限る門番。
    extract_keywords の署名を変えずに読み取るための独立ヘルパー（呼び出し元を巻き込まない）。"""
    matched = [jp for jp in QUERY_EXPAND if jp in query]
    matched = [k for k in matched if not any(k != o and k in o for o in matched)]
    for jp in matched:
        terms = QUERY_EXPAND[jp]
        if (terms.get("keyword") or terms.get("mana_struct")
                or terms.get("tournament_boost")
                or terms.get("removal_mode") or terms.get("counter_mode")):
            continue  # 構造化フラグ or 別処理される役割意図＝fuzzy でない
        if terms.get("en") or terms.get("ja"):
            return True  # 構造化に落ちない意味語が在る
    return False


def format_filter_sql(fmt: Optional[str]) -> str:
    """legalities フィルタの SQL 断片を生成する"""
    if not fmt:
        return ""
    fmt = fmt.lower()
    if fmt not in VALID_FORMATS:
        print(f"  [警告] 不明なフォーマット: {fmt}。フィルタを無効にします。")
        return ""
    return f"AND c.legalities->>'{fmt}' = 'legal'"


# router の format 値（小文字）→ card_format_strength.format_name（先頭大文字）。
# 集計があるのは大会4フォーマットのみ。これ以外（vintage/pauper 等）は per-format 集計なし＝
# 全4F合計にフォールバック（None 扱い）。
CFS_FORMAT_MAP = {
    "legacy": "Legacy", "modern": "Modern",
    "pioneer": "Pioneer", "standard": "Standard",
}


# ─── EDH（統率者戦）固有色・ブラケット検出（R13・2026-07-08） ──────────
# 固有色（color identity）クエリはフォーマットゲートの同族＝crisp なハードゲート。
# 「◯◯カラーで使える」= color_identity ⊆ クエリ色集合（はみ出しは 0・単色/無色も
# 通過＝「デッキに入るか」の判定・R13 本人裁定）。検出は決定的な辞書/正規表現
# （フォーマット語の決定的フォールバックと同じ型＝ルーター無改修で効く）。
# 注意: 単独の色文字（「青いカード」）では発動しない。あれは colors（カードの色）族で
# 固有色とは別物（R13 の区別。実例: Bosh, Iron Golem は colors=[] / identity=[R]）。

_CI_LETTER = {'白': 'W', '青': 'U', '黒': 'B', '赤': 'R', '緑': 'G'}

COLOR_IDENTITY_MAP = {
    # ギルド（2色）
    'アゾリウス': 'WU', 'ディミーア': 'UB', 'ラクドス': 'BR', 'グルール': 'RG',
    'セレズニア': 'GW', 'オルゾフ': 'WB', 'イゼット': 'UR', 'ゴルガリ': 'BG',
    'ボロス': 'RW', 'シミック': 'GU',
    'azorius': 'WU', 'dimir': 'UB', 'rakdos': 'BR', 'gruul': 'RG',
    'selesnya': 'GW', 'orzhov': 'WB', 'izzet': 'UR', 'golgari': 'BG',
    'boros': 'RW', 'simic': 'GU',
    # 断片（アラーラの弧・3色）
    'バント': 'GWU', 'エスパー': 'WUB', 'グリクシス': 'UBR',
    'ジャンド': 'BRG', 'ナヤ': 'RGW',
    'bant': 'GWU', 'esper': 'WUB', 'grixis': 'UBR', 'jund': 'BRG', 'naya': 'RGW',
    # 楔（タルキール・3色）
    'アブザン': 'WBG', 'ジェスカイ': 'URW', 'スゥルタイ': 'BGU',
    'マルドゥ': 'RWB', 'ティムール': 'GUR',
    'abzan': 'WBG', 'jeskai': 'URW', 'sultai': 'BGU', 'mardu': 'RWB', 'temur': 'GUR',
}


def detect_color_identity(query: str) -> Optional[list[str]]:
    """クエリから固有色の色集合を決定的に検出する。
    発動: ギルド/断片/楔の名前・色文字の連なり（「青黒」）・「◯単」・「無色」。
    不発（誤発動ゼロ側に倒す非対称設計）: 単独の色文字（「青いカード」= colors 族）・
    「好きな色」「色を選ぶ」等の非指定表現。
    戻り値: WUBRG のソート済みリスト（無色クエリは []・不発は None）。"""
    # 非指定表現ガード（色の話だが特定の色集合を指定していない）
    for w in ('好きな色', '色を選', 'いずれかの色', '各色', '多色'):
        if w in query:
            return None
    q = query.lower()
    letters: set[str] = set()
    for name, ci in COLOR_IDENTITY_MAP.items():
        if name in q:
            letters.update(ci)
    # 色文字の連なり（2〜5色・「青黒」「白青黒」型）。単独の色文字では発動しない
    for m in re.findall(r'[白青黒赤緑]{2,5}', query):
        letters.update(_CI_LETTER[ch] for ch in m)
    # 「◯単」（緑単・白単デッキ型）
    for m in re.findall(r'([白青黒赤緑])単', query):
        letters.add(_CI_LETTER[m])
    if letters:
        return sorted(letters)
    # 無色（「無色マナ」はマナ種の話＝固有色指定ではないので除く）
    if '無色' in query.replace('無色マナ', ''):
        return []
    return None


def detect_bracket(query: str) -> Optional[int]:
    """公式ブラケット文言（「ブラケット2」等）を検出する（R13補足a）。"""
    m = re.search(r'(?:ブラケット|bracket)\s*([1-5１-５])', query, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1).translate(str.maketrans('１２３４５', '12345')))


def color_identity_filter_sql(ci: Optional[list]) -> str:
    """固有色ハードゲート（R13）: color_identity ⊆ クエリ色集合なら通過。
    単色・無色（空配列）も通過（Sol Ring はどの固有色クエリでも 2 になり得る＝
    本人裁定）。ci=[]（無色クエリ）は「固有色が空のカードのみ」に正しく縮む。"""
    if ci is None:
        return ""
    letters = [c for c in ci if c in ('W', 'U', 'B', 'R', 'G')]
    arr = ", ".join("'" + c + "'" for c in letters)
    return f" AND c.color_identity <@ ARRAY[{arr}]::text[]"


def edh_gate_sql(edh_intent: bool, bracket: Optional[int]) -> str:
    """EDH 意図クエリの合法性ゲート（R13）。
    commander banned はブラケット指定の有無に関わらず「使えない」＝除外（全ブラ
    ケット共通の前提）。ブラケット1〜2 指定時のみ game_changer もハードゲート。
    公式ブラケットの他の軸（マスランド破壊・チューター量・2枚コンボ等）は構造化列が
    無いため写さない＝crisp に写せる部分だけ厳格に（R13補足a・偽の精密さを避ける）。"""
    if not edh_intent:
        return ""
    sql = " AND c.legalities->>'commander' = 'legal'"
    if bracket is not None and bracket <= 2:
        sql += " AND c.game_changer IS NOT TRUE"
    return sql


def is_creature_removal(removal_entries: Optional[list],
                        target_types: Optional[list]) -> bool:
    """クリーチャーを討てる恒久除去メカを1つでも持つか（R10 の検索側の写し）。
    removal_mode の減点判定用。構造化列 removal(jsonb) / target_types で判定する。
    - sacrifice（エディクト型）は対象を取らせない機構＝それだけで除去（R6）。
    - destroy/exile/tuck は permanent が false（ブリンク・一時追放・R1で0）なら数えない。
      object（対象クラス）が creature/permanent ならクリーチャーを討てる（R10・Vindicate型OK、
      artifact/enchantment 専用の Naturalize 型は落ちる）。object 不明時は target_types で代用。
    - damage/minus は「クリーチャーに向くか」だけ target_types で確認（致死かどうかの
      スケール判定＝R7 はしない。順位づけは検索の上流と採点に任せ、ここは偽陽性の門番だけ）。
      minus の permanent は修整の持続時間であって死亡の恒久性でないため見ない。"""
    tt = set(target_types or [])
    can_hit = bool(tt & {'creature', 'any', 'permanent'})
    for e in (removal_entries or []):
        typ = e.get('type')
        if typ == 'sacrifice':
            return True
        if typ in ('destroy', 'exile', 'tuck'):
            if e.get('permanent') is False:
                continue
            objv = e.get('object')
            if objv in ('creature', 'permanent'):
                return True
            if objv is None and can_hit:
                return True
        if typ in ('damage', 'minus') and can_hit:
            return True
    return False


def removal_mech_filter_sql(query: str, removal_mode: bool) -> str:
    """機構指定つき除去クエリのハードフィルタ（2026-07-07）。
    「追放除去」「クリーチャーを破壊する除去」のようにクエリが除去の機構を明示して
    いるときは、`removal_types`（enrich_removal.py 由来・GIN）で候補集合ごと機構に
    絞る——キーワード層で確立した「crisp な条件は WHERE の門」の除去版。
    追放除去 0.265 の正体は候補生成（ブリンクを沈めても本物の追放除去が retrieval に
    居ない）だったため、門で機構を固定して意味検索を「機構内の並び順」係に縮める。
    ブリンクは removal_types に exile を持ち門を通るが、恒久性ペナルティ
    （is_creature_removal）が沈める二段構え。機構語が無い「単体除去」は R10 どおり
    機構不問＝フィルタなし。"""
    if not removal_mode:
        return ""
    # 「破壊不能（を除去…）」の「破壊」を機構と誤検知しない
    q = query.replace('破壊不能', '').lower()
    mechs = []
    if '追放' in q or 'exile' in q:
        mechs.append('exile')
    if '破壊' in q or 'destroy' in q:
        mechs.append('destroy')
    if not mechs:
        return ""
    ms = ", ".join("'" + m + "'" for m in mechs)
    return f" AND c.removal_types && ARRAY[{ms}]::text[]"


def keyword_filter_sql(kw_abilities: Optional[list],
                       neg_kw_abilities: Optional[list] = None) -> str:
    """「○○を持つクリーチャー」系クエリの生得キーワード・ハードフィルタ。
    front_keywords 配列（表面の生得能力のみ＝R8補足a/b の crisp 代理・
    enrich_front_keywords.py 由来）を WHERE で要求する。カード単位の keywords は
    裏面・変身後の能力を含む（デルバーの Flying 等）ため使わない＝「両面は表面の
    本質で判定」の検索側の写し。crisp な条件は減点でなく SQL の門で解く
    （cmc/is_mana_boost と同じ役割分担）＝ベクトル検索は「生得持ちの中の並び順」
    だけを担当し、意味的に似てるだけの非該当カードは入口で消える。

    neg_kw_abilities（2026-07-11 否定形対応）: 「○○を持たない」は NOT (&&) の門。
    front_keywords が NULL のカード（キーワード無し）こそ「持たない」正解集合の主役
    なので COALESCE で空配列に落としてから判定する（素の NOT(NULL &&) は NULL に
    なって行ごと消える＝正解を全滅させる罠）。"""
    sql = ""
    if kw_abilities:
        kws = ", ".join("'" + k.replace("'", "''") + "'" for k in kw_abilities)
        sql += f" AND c.front_keywords @> ARRAY[{kws}]::text[]"
    if neg_kw_abilities:
        kws = ", ".join("'" + k.replace("'", "''") + "'" for k in neg_kw_abilities)
        sql += (f" AND NOT (COALESCE(c.front_keywords, '{{}}') && "
                f"ARRAY[{kws}]::text[])")
    return sql


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


def _safe_int(v, lo: int = 0, hi: int = 99):
    """外部入力（LLM 等）を安全に int 化する。非整数・範囲外は None を返す。"""
    try:
        n = int(v)
    except (ValueError, TypeError):
        return None
    return n if lo <= n <= hi else None


def attr_filter_sql(cmc_min=None, cmc_max=None,
                    power_min=None, power_max=None,
                    toughness_min=None, toughness_max=None,
                    mana_producer: bool = False) -> str:
    """数値属性（マナ総量 cmc・パワー・タフネス）と構造化フラグの SQL 断片を生成する。

    cmc フィルタは face_cmcs（撃てる cmc の集合）に対し EXISTS で判定する。
    「1つの面が指定範囲内に収まるか」を問うので、split カードの各面を独立して評価できる。
    cmc_min と cmc_max が両方ある場合は単一 EXISTS に AND でまとめる（別々の EXISTS にすると
    faces=[1,5] が範囲[2,4] に誤マッチするため）。
    power / toughness は '*' や 'X' 等の特殊値を含む text カラムなので、正規表現で
    「純粋な整数の行」だけを漉してから数値比較する（特殊値は数値フィルタの対象外＝正しい挙動）。
    値はすべて _safe_int で整数検証済みなので、f 文字列に埋めても SQL インジェクションは
    起きない（型で保証される）。断片に % を含まないため param/no-param どちらの実行でも安全。

    mana_producer=True のときは is_mana_boost=TRUE の行＝「マナブースト（ランプ）するカード」
    だけに絞る。is_mana_boost は oracle 解析で「出すマナ − 払うマナ（土地は −1）> 0」を満たすか
    で事前計算した構造化フラグ（TRUE=ブースト/誘発・儀式・宝物等も含む, FALSE=マナフィルター
    〔Ceta Disciple 等の払って出す札〕, NULL=非産出）。＝「マナを出すか(produced_mana)」でなく
    「マナを増やすか(boost)」で絞る。マナフィルターを排除し、マナクリーチャー/マナ加速クエリの
    精度を上げる。「マナを出す(広い)」が必要になったら produced_mana 直で別フラグを足す。
    """
    frags: list[str] = []
    if mana_producer:
        frags.append("AND c.is_mana_boost = TRUE")
    cmn, cmx = _safe_int(cmc_min), _safe_int(cmc_max)
    if cmn is not None or cmx is not None:
        conds = []
        if cmn is not None:
            conds.append(f"fc >= {cmn}")
        if cmx is not None:
            conds.append(f"fc <= {cmx}")
        frags.append("AND EXISTS (SELECT 1 FROM unnest(c.face_cmcs) fc "
                     f"WHERE {' AND '.join(conds)})")
    for col, vmin, vmax in (("power", power_min, power_max),
                            ("toughness", toughness_min, toughness_max)):
        lo, hi = _safe_int(vmin), _safe_int(vmax)
        if lo is None and hi is None:
            continue
        frags.append(f"AND c.{col} ~ '^[0-9]+$'")  # '*' や 'X' 等の特殊値を除外
        if lo is not None:
            frags.append(f"AND CAST(c.{col} AS INTEGER) >= {lo}")
        if hi is not None:
            frags.append(f"AND CAST(c.{col} AS INTEGER) <= {hi}")
    return (" " + " ".join(frags)) if frags else ""


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
        # DB アクセスはドライバ切替層（db.py）経由＝ローカル psycopg2 / 本番 Data API を
        # DB_BACKEND 環境変数で切替（2026-07-12 移行・旧 self.conn 直書きは全廃）
        self.db = make_db()
        # HNSW 近似検索 + 構造化フィルタ併用時の取りこぼし対策（pgvector 0.8+）。
        # 既定の近似スキャンだと ef_search 件の近傍を見てから WHERE で絞るため、
        # cmc=1 等の選択的フィルタでは候補がほぼ脱落して数件しか残らない。
        # iterative_scan を有効化し、フィルタを満たす件数が揃うまで反復スキャンさせる。
        try:
            self.db.execute("SET hnsw.iterative_scan = relaxed_order")
        except Exception:
            pass  # pgvector < 0.8 では未対応 → 無視（rollback は db 層が実施済み）
        print(f"[MTGHybridSearcherV2] {model_key} ({cfg['model_name']})")

    def _embed(self, text: str) -> list[float]:
        vec = self.model.encode(
            self.cfg["prefix"] + text, normalize_embeddings=True
        )
        return vec.tolist()

    def _vector_search(
        self, query_vec: list[float], top_k: int,
        fmt_sql: str, type_sql: str, attr_sql: str = "",
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
            WHERE 1=1 {fmt_sql} {type_sql} {attr_sql}
            ORDER BY e.embedding <=> '{vec_str}'::vector
            LIMIT {top_k * 3};
        """
        return self.db.query_dicts(sql)

    def _en_text_search(
        self, en_keywords: list[str], top_k: int,
        fmt_sql: str, type_sql: str, attr_sql: str = "",
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
                    ) DESC, c.id) AS rank
                FROM {cfg['cards_table']} c
                WHERE to_tsvector('english', COALESCE(c.oracle_text, ''))
                      @@ to_tsquery('english', $tsq$)
                  {fmt_sql} {type_sql} {attr_sql}
                  AND c.set_code NOT IN ('msh', 'msc')  -- Marvel(行のみ・未reembed)は検索不適格。reembed後に外す
                ORDER BY text_score DESC, c.id
                LIMIT {top_k * 3};
            """
            try:
                # $tsq$ dollar quoting で特殊文字を安全に渡す
                return self.db.query_dicts(sql.replace("$tsq$", "%s"),
                                           (tsquery, tsquery, tsquery))
            except Exception as e:
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
                    ) DESC, c.id) AS rank
                FROM {cfg['cards_table']} c
                WHERE to_tsvector('english', COALESCE(c.oracle_text, ''))
                      @@ plainto_tsquery('english', '{primary}')
                  {fmt_sql} {type_sql} {attr_sql}
                  AND c.set_code NOT IN ('msh', 'msc')  -- Marvel(行のみ・未reembed)は検索不適格。reembed後に外す
                ORDER BY text_score DESC, c.id
                LIMIT {top_k * 3};
            """
            try:
                return self.db.query_dicts(sql)
            except Exception as e:
                print(f"  [en_fts] エラー: {e}")
                return []

    def _ja_text_search(
        self, ja_keywords: list[str], top_k: int,
        fmt_sql: str, type_sql: str, attr_sql: str = "",
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

        # tournament_score は同点（0/NULL）が大半なので c.id をタイブレーカーに置く。
        # これが無いと同点の順序と LIMIT で拾う集合がヒープ順（物理配置）依存になり、
        # バルク UPDATE のたびに検索結果＝評価数値が変わってしまう（再現性バグ）。
        sql = f"""
            SELECT
                c.card_name, c.type_line, c.oracle_text,
                c.japanese_name, c.japanese_oracle_text,
                c.mana_cost, c.rarity, c.tournament_score,
                ROW_NUMBER() OVER (
                    ORDER BY c.tournament_score DESC NULLS LAST, c.id
                ) AS rank
            FROM {cfg['cards_table']} c
            WHERE c.japanese_oracle_text IS NOT NULL
              AND ({placeholders})
              {fmt_sql} {type_sql} {attr_sql}
            ORDER BY c.tournament_score DESC NULLS LAST, c.id
            LIMIT {top_k * 3};
        """
        try:
            return self.db.query_dicts(sql, params)
        except Exception as e:
            print(f"  [ja_fts] エラー: {e}")
            return []

    def _format_strength_map(
        self, card_names: list[str], fmt: Optional[str]
    ) -> dict[str, int]:
        """card_name → play_decks を1クエリで引く（#22 boost 用）。
        fmt が大会4フォーマットのいずれかならその format の play_decks、
        それ以外（None・vintage 等）は全4フォーマット合計（R11 の GT 機械採点と同じ土俵）。
        card_format_strength は card_id 基準なので card_name→id を JOIN で解決する。"""
        if not card_names:
            return {}
        cfs_fmt = CFS_FORMAT_MAP.get((fmt or "").lower())
        cfg = self.cfg
        if cfs_fmt:
            sql = f"""
                SELECT c.card_name, cfs.play_decks
                FROM {cfg['cards_table']} c
                JOIN card_format_strength cfs ON cfs.card_id = c.id
                WHERE c.card_name = ANY(%s) AND cfs.format_name = %s
            """
            params = (card_names, cfs_fmt)
        else:
            sql = f"""
                SELECT c.card_name, SUM(cfs.play_decks) AS play_decks
                FROM {cfg['cards_table']} c
                JOIN card_format_strength cfs ON cfs.card_id = c.id
                WHERE c.card_name = ANY(%s)
                GROUP BY c.card_name
            """
            params = (card_names,)
        try:
            return {r[0]: int(r[1]) for r in self.db.query(sql, params)}
        except Exception as e:
            print(f"  [format_strength] エラー: {e}")
            return {}

    def _strength_candidates(
        self, top_k: int, fmt: Optional[str],
        fmt_sql: str, type_sql: str, attr_sql: str = "",
        removal_mode: bool = False, counter_mode: bool = False,
    ) -> list[dict]:
        """tournament_boost クエリ用の第4候補腕（#22）。
        play-rate 上位を「強いカードの仮説リスト」として RRF 融合に参加させる。
        注意: play-rate 上位＝強い、ではない（Bowmasters 論）。ただし正解を含み
        やすい集合ではある＝候補生成（recall 装置）。判定は融合・ペナルティ・
        機能フィルタ（fmt/type/attr）の側が担う＝R11 の AND 構造の検索側の写し。
        boost だけでは retrieval が連れてこなかった強カードを上げられない
        （プール飢餓）ことへの対処。card_format_strength は土地除外済み。

        役割ゲート（#a・R11 の AND を検索側で完成）: 役割つき superlative
        （最強の"単体除去"・最強"カウンター"）では、強度腕にも役割フィルタを噛ませる。
        噛ませないと FoW/Thoughtseize 等のフォーマット強カードが除去プールに注入され、
        本人が正しく 0 採点する傷（最強の単体除去 0.33）になっていた。
        removal_mode → 除去メカ有り かつ クリーチャーを討てる対象（creature/any/permanent）。
        counter_mode → 呪文を対象に取る（target_types に spell）。"""
        cfg = self.cfg
        role_sql = ""
        if removal_mode:
            # 恒久除去のみ（bounce=soft は除外・本人判断 2026-07-06）。tuck（ライブラリ送り）は
            # バウンスより硬いので除去に含める。恒久性スペクトラムを役割ゲートに反映。
            role_sql = (" AND c.removal_types && ARRAY['destroy','exile','damage','minus','sacrifice','tuck']"
                        " AND c.target_types && ARRAY['creature','any','permanent']")
        elif counter_mode:
            role_sql = " AND c.target_types @> ARRAY['spell']"
        cfs_fmt = CFS_FORMAT_MAP.get((fmt or "").lower())
        if cfs_fmt:
            sql = f"""
                SELECT
                    c.card_name, c.type_line, c.oracle_text,
                    c.japanese_name, c.japanese_oracle_text,
                    c.mana_cost, c.rarity, c.tournament_score,
                    ROW_NUMBER() OVER (
                        ORDER BY cfs.play_decks DESC, c.id
                    ) AS rank
                FROM {cfg['cards_table']} c
                JOIN card_format_strength cfs ON cfs.card_id = c.id
                WHERE cfs.format_name = %s
                  {fmt_sql} {type_sql} {attr_sql} {role_sql}
                  AND c.set_code NOT IN ('msh', 'msc')  -- Marvel(未reembed)は検索不適格。reembed後に外す
                ORDER BY cfs.play_decks DESC, c.id
                LIMIT {top_k * 3};
            """
            params: tuple = (cfs_fmt,)
        else:
            sql = f"""
                SELECT
                    c.card_name, c.type_line, c.oracle_text,
                    c.japanese_name, c.japanese_oracle_text,
                    c.mana_cost, c.rarity, c.tournament_score,
                    ROW_NUMBER() OVER (
                        ORDER BY SUM(cfs.play_decks) DESC, c.id
                    ) AS rank
                FROM {cfg['cards_table']} c
                JOIN card_format_strength cfs ON cfs.card_id = c.id
                WHERE TRUE
                  {fmt_sql} {type_sql} {attr_sql} {role_sql}
                  AND c.set_code NOT IN ('msh', 'msc')  -- Marvel(未reembed)は検索不適格。reembed後に外す
                GROUP BY c.id
                ORDER BY SUM(cfs.play_decks) DESC, c.id
                LIMIT {top_k * 3};
            """
            params = ()
        try:
            return self.db.query_dicts(sql, params)
        except Exception as e:
            print(f"  [strength_arm] エラー: {e}")
            return []

    def _edh_candidates(
        self, top_k: int,
        fmt_sql: str, type_sql: str, attr_sql: str = "",
        removal_mode: bool = False, counter_mode: bool = False,
    ) -> list[dict]:
        """EDH 意図クエリ（固有色/ブラケット・R13）用の候補腕 v1（2026-07-09）。
        edhrec_rank 上位を「EDH で使われるカードの仮説リスト」として RRF 融合に
        参加させる。#22 の強度腕と同じ設計思想＝ベクトル/FTS は EDH 定番を連れて
        来ない（Sol Ring の日本語オラクルに「マナを加える」の字面が無い等）ため、
        ゲート内の候補プールが飢える——candidate generation（recall 装置）と
        判定（融合・ペナルティ）の分離で解く。
        注意: edhrec_rank は統率者不問のカジュアル人気＝「このデッキに合う」では
        ない。統率者別の接地は mtgtop8_edh デッキ共起（取り込み中）で後段 v2。
        役割ゲートは強度腕と同一（除去/カウンタークエリで非該当の定番注入を防ぐ）。
        attr_sql には色ゲート・banned・GC ゲートが入ってくる＝この腕も同じ門を通る。"""
        cfg = self.cfg
        role_sql = ""
        if removal_mode:
            role_sql = (" AND c.removal_types && ARRAY['destroy','exile','damage','minus','sacrifice','tuck']"
                        " AND c.target_types && ARRAY['creature','any','permanent']")
        elif counter_mode:
            role_sql = " AND c.target_types @> ARRAY['spell']"
        sql = f"""
            SELECT
                c.card_name, c.type_line, c.oracle_text,
                c.japanese_name, c.japanese_oracle_text,
                c.mana_cost, c.rarity, c.tournament_score,
                ROW_NUMBER() OVER (
                    ORDER BY c.edhrec_rank ASC, c.id
                ) AS rank
            FROM {cfg['cards_table']} c
            WHERE c.edhrec_rank IS NOT NULL
              {fmt_sql} {type_sql} {attr_sql} {role_sql}
              AND c.set_code NOT IN ('msh', 'msc')  -- Marvel(未reembed)は検索不適格
            ORDER BY c.edhrec_rank ASC, c.id
            LIMIT {top_k * 3};
        """
        try:
            return self.db.query_dicts(sql)
        except Exception as e:
            print(f"  [edh_arm] エラー: {e}")
            return []

    def _role_map(self, card_names: list[str]) -> dict[str, tuple]:
        """card_name → (target_types, removal)（構造化・enrich_removal.py 由来）を1クエリで引く。
        counter_mode（呪文を対象に取るか）と removal_mode（除去メカと恒久性）の減点判定用。"""
        if not card_names:
            return {}
        try:
            rows = self.db.query(
                f"SELECT card_name, target_types, removal FROM {self.cfg['cards_table']} "
                f"WHERE card_name = ANY(%s)", (card_names,))
            return {r[0]: (r[1] or [], r[2] or []) for r in rows}
        except Exception as e:
            print(f"  [role_map] エラー: {e}")
            return {}

    def _structured_search(
        self, top_k: int,
        fmt_sql: str, type_sql: str, attr_sql: str,
        edh_order: bool = False,
    ) -> list[CardResult]:
        """構造化オンリー・クエリの直行路（意味検索を通さない）。
        「破壊不能を持つクリーチャー」のように正解集合が構造化列（keywords/type/
        format/cmc 等）の WHERE で完全に定義できるクエリは、ベクトル・FTS・HyDE・
        RRF を使わない＝「事実上 SQL に LIMIT を付けただけのもの」（2026-07-06
        本人の設計指摘）。意味検索は集合を定義できない上に、意味的に似てるだけの
        非該当カードを注入する方向にしか働かないため。
        並び順＝大会 play-rate 降順 → EDHREC 人気昇順 → id（決定的）。
        EDH 意図クエリ（固有色/ブラケット・R13）は EDHREC 人気を主にする
        （play-rate は4フォーマット大会由来＝EDH の実勢とは別物）。"""
        cfg = self.cfg
        order = ("c.edhrec_rank ASC NULLS LAST, COALESCE(s.play_decks, 0) DESC, c.id"
                 if edh_order else
                 "COALESCE(s.play_decks, 0) DESC, c.edhrec_rank ASC NULLS LAST, c.id")
        sql = f"""
            SELECT
                c.card_name, c.type_line, c.oracle_text,
                c.japanese_name, c.japanese_oracle_text,
                c.mana_cost, c.rarity
            FROM {cfg['cards_table']} c
            LEFT JOIN (
                SELECT card_id, SUM(play_decks) AS play_decks
                FROM card_format_strength GROUP BY card_id
            ) s ON s.card_id = c.id
            WHERE TRUE {fmt_sql} {type_sql} {attr_sql}
              AND c.set_code NOT IN ('msh', 'msc')  -- Marvel(未reembed)は検索不適格
            ORDER BY {order}
            LIMIT {top_k};
        """
        try:
            rows = self.db.query_dicts(sql)
        except Exception as e:
            print(f"  [structured] エラー: {e}")
            return []
        return [CardResult(
            card_name=r["card_name"],
            type_line=r.get("type_line") or "",
            oracle_text=r.get("oracle_text") or "",
            japanese_name=r.get("japanese_name") or "",
            japanese_oracle_text=r.get("japanese_oracle_text") or "",
            mana_cost=r.get("mana_cost") or "",
            rarity=r.get("rarity") or "",
            vector_rank=None, en_text_rank=None, ja_text_rank=None,
            rrf_score=0.0,
        ) for r in rows]

    def _rrf_merge(
        self,
        v_rows: list[dict], en_rows: list[dict], ja_rows: list[dict],
        top_k: int,
        tournament_boost: bool = False,
        removal_mode: bool = False,
        counter_mode: bool = False,
        format: Optional[str] = None,
        st_rows: Optional[list[dict]] = None,
        counter_align: Optional[str] = None,
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

        # 強度腕（#22・boost クエリのみ非空）。重みは暫定 1.0＝均等 RRF（#23 で再検証）
        w_st = 1.0
        for row in (st_rows or []):
            name = row["card_name"]
            r    = int(row["rank"])
            if name not in scores:
                scores[name] = {"row": row, "rrf": 0.0,
                                "vr": None, "er": None, "jr": None}
            scores[name]["rrf"] += w_st / (k + r)

        # 役割ペナルティ（P2: 偽陽性は強く沈める・×0.1）。手書き文字列規則を構造化列で置換済み。
        # removal: クリーチャーを討てる恒久除去メカを持たないカードを沈める
        #          （ブリンク=permanent:false・墓地追放=対象が creature でない・置物専用、が自然に落ちる）。
        # counter: 本物のカウンターは「呪文を対象に取る」＝target_types に spell を持つ。護法は
        #          "counter that spell"（target を取らない誘発型）＝spell を持たない→自然に減点。
        # （キーワード系クエリの生得判定は keyword_filter_sql のハードフィルタ＝入口で解決。
        #   減点方式は採らない: crisp な条件は WHERE の門で、減点は曖昧な役割判定にだけ使う）
        if removal_mode or counter_mode:
            rmap = self._role_map(list(scores.keys()))
            for name, data in scores.items():
                tt, rem = rmap.get(name, ([], []))
                if removal_mode and not is_creature_removal(rem, tt):
                    data["rrf"] *= 0.1
                if counter_mode and 'spell' not in tt:
                    data["rrf"] *= 0.1
                elif counter_mode and counter_align and not tournament_boost:
                    # R12 の整合: 極性が合わないカードは grade 1 相当の降格
                    # （×0.5・偽陽性の ×0.1 より緩い）。crisp 修飾つきクエリでは
                    # counter_align=None で不発。boost クエリも R11 判定なので触らない。
                    is_cond = 'spell_conditional' in tt
                    if is_cond != (counter_align == 'conditional'):
                        data["rrf"] *= 0.5

        # 大会 play-rate ボーナスを RRF スコアに加算（#22: card_format_strength へ配線替え）。
        # 旧実装は stale な単一列 tournament_score を見ていた。fresh な per-format
        # play_decks（format 指定時）／全4F合計（format なし）へ差し替え。
        # tournament_boost=True（「最強」「環境」等）は強く、それ以外は弱く反映。
        boost_coef = 0.10 if tournament_boost else 0.03
        strength = self._format_strength_map(list(scores.keys()), format)
        max_ts = max(strength.values(), default=0) or 1
        for name, data in scores.items():
            ts = strength.get(name, 0)
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
        ja_hyde_text: str = "",
        top_k: int = 10,
        format: Optional[str] = None,
        tournament_boost_override: bool = False,
        removal_mode_override: bool = False,
        counter_mode_override: bool = False,
        type_filter_override: Optional[str] = None,
        cmc_min=None, cmc_max=None,
        power_min=None, power_max=None,
        toughness_min=None, toughness_max=None,
        mana_producer: bool = False,
    ) -> list[CardResult]:
        """
        HyDE（Hypothetical Document Embeddings）を使った検索。
        通常の検索結果と HyDE ベクトル検索結果を RRF でマージする。

        ja_hyde_text が与えられた場合は「日本語の理想カードテキスト」も embedding
        して3本目のランキングとして融合に足す。多言語 embedding なので、英語 HyDE
        は英語クエリで日本語なしカードに偏りやすい（実測: コーパス0.87% vs プール3%）
        のを、日本語 HyDE が日英両方を持つカードを公平に拾うことで相殺する狙い。
        空/不在のときは英語 HyDE のみ＝従来挙動と完全一致（id=11 を再現できる）。
        """
        # 通常の検索結果を取得
        normal_results = self.search(
            query, top_k=top_k * 2, format=format,
            tournament_boost_override=tournament_boost_override,
            removal_mode_override=removal_mode_override,
            counter_mode_override=counter_mode_override,
            type_filter_override=type_filter_override,
            cmc_min=cmc_min, cmc_max=cmc_max,
            power_min=power_min, power_max=power_max,
            toughness_min=toughness_min, toughness_max=toughness_max,
            mana_producer=mana_producer,
        )

        # HyDE ベクトル検索（hyde_text を embedding してベクトル検索）
        fmt_sql  = format_filter_sql(format)
        type_sql = type_filter_sql(type_filter_override)
        attr_sql = attr_filter_sql(cmc_min, cmc_max,
                                   power_min, power_max,
                                   toughness_min, toughness_max,
                                   mana_producer=mana_producer)
        # キーワード系クエリは HyDE 腕にも生得持ちハードフィルタを適用（search() 本体と対。
        # HyDE 単独ヒットは最終マージに入るため、ここに門が無いと非該当が再流入する）
        (_, _, _, _tb, _rm, _cm, _kw_abilities, _neg_kw,
         _kw_only) = extract_keywords(query)
        # 構造化オンリー直行路なら normal_results が既に SQL 直行の並び＝HyDE を重ねない
        # （重ねると意味検索の並びが play-rate 順を汚す・search() 本体の分岐と対）
        if _kw_only and not (_tb or tournament_boost_override) \
                and not (_rm or removal_mode_override) \
                and not (_cm or counter_mode_override):
            return normal_results[:top_k]
        attr_sql += keyword_filter_sql(_kw_abilities, _neg_kw)
        # 機構指定つき除去クエリの門は HyDE 腕にも（search() 本体と対・単独ヒット再流入防止）
        attr_sql += removal_mech_filter_sql(query, _rm or removal_mode_override)
        # P/T 関係・部族ゲートも HyDE 腕に（search() 本体と対）
        attr_sql += pt_relation_sql(detect_pt_relation(query))
        attr_sql += tribal_filter_sql(detect_tribal(query))
        hyde_vec  = self._embed(hyde_text)
        hyde_rows = self._vector_search(hyde_vec, top_k * 2, fmt_sql, type_sql, attr_sql)

        # 日本語 HyDE（任意）: 与えられたときだけ embedding して別ランキングを足す。
        ja_hyde_rows = None
        if ja_hyde_text:
            ja_hyde_vec  = self._embed(ja_hyde_text)
            ja_hyde_rows = self._vector_search(ja_hyde_vec, top_k * 2,
                                               fmt_sql, type_sql, attr_sql)

        # HyDE 総重みを保存する: 日本語を足すときは英/日それぞれ 0.5 にし、
        # 英語のみ(=従来)のときは英語 1.0。これで id=11→id=12 の A/B で変わる変数を
        # 「HyDE に日本語方向が入ったか」の一点に絞り、HyDE 全体の重み増という交絡を避ける。
        en_w = 0.5 if ja_hyde_rows is not None else 1.0
        ja_w = 0.5 if ja_hyde_rows is not None else 0.0

        # 通常検索結果を dict に変換
        normal_scores: dict[str, float] = {}
        for i, r in enumerate(normal_results):
            normal_scores[r.card_name] = 1.0 / (self.rrf_k + i + 1)

        # 英語 HyDE 検索結果を RRF でマージ
        hyde_scores: dict[str, float] = {}
        for row in hyde_rows:
            name = row["card_name"]
            r    = int(row["rank"])
            hyde_scores[name] = en_w * (1.0 / (self.rrf_k + r))

        # 日本語 HyDE 検索結果を RRF でマージ（あれば）
        ja_hyde_scores: dict[str, float] = {}
        if ja_hyde_rows is not None:
            for row in ja_hyde_rows:
                name = row["card_name"]
                r    = int(row["rank"])
                ja_hyde_scores[name] = ja_w * (1.0 / (self.rrf_k + r))

        # 統合スコア
        all_names = set(normal_scores) | set(hyde_scores) | set(ja_hyde_scores)
        merged = []
        for name in all_names:
            score = (normal_scores.get(name, 0)
                     + hyde_scores.get(name, 0)
                     + ja_hyde_scores.get(name, 0))
            merged.append((name, score))

        # 同点を決定的に並べる: スコア降順 → カード名昇順。
        # set 由来の並びはプロセス間でハッシュ乱択により変わるため、安定ソートだけでは
        # 同点カードの top_k 境界が非決定になる（normal rank=i と hyde rank=i が同値で衝突）。
        # 名前タイブレーカーで全順序にして再現性を担保する（FTS 側の c.id 同点処理と同型）。
        merged.sort(key=lambda x: (-x[1], x[0]))

        # 通常検索結果から CardResult を取得
        result_map = {r.card_name: r for r in normal_results}

        # HyDE でのみヒットしたカードを追加取得
        hyde_only = [n for n, _ in merged[:top_k] if n not in result_map]
        if hyde_only:
            placeholders = ",".join(["%s"] * len(hyde_only))
            hyde_rows = self.db.query_dicts(f"""
                    SELECT card_name, type_line, oracle_text, japanese_name,
                           japanese_oracle_text, mana_cost, rarity
                    FROM {self.cfg['cards_table']}
                    WHERE card_name IN ({placeholders})
                """, hyde_only)
            for row in hyde_rows:
                    result_map[row["card_name"]] = CardResult(
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
        cmc_min=None, cmc_max=None,
        power_min=None, power_max=None,
        toughness_min=None, toughness_max=None,
        mana_producer: bool = False,
    ) -> list[CardResult]:
        print(f"\n[{self.model_key}] 検索: 「{query}」"
              + (f" [{format}]" if format else ""))
        t0 = time.perf_counter()

        (en_kws, ja_kws, type_filter, tournament_boost,
         removal_mode, counter_mode, kw_abilities, neg_kw_abilities,
         kw_only) = extract_keywords(query)

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
        attr_sql = attr_filter_sql(cmc_min, cmc_max,
                                   power_min, power_max,
                                   toughness_min, toughness_max,
                                   mana_producer=mana_producer)
        if neg_kw_abilities:
            print(f"  否定キーワード: {neg_kw_abilities}（持たない側＝SQL NOT 門）")
        attr_sql += keyword_filter_sql(kw_abilities, neg_kw_abilities)
        attr_sql += removal_mech_filter_sql(query, removal_mode)
        # P/T 列間関係（「パワーとタフネスが同じ」等・決定的検出・全腕+直行路に掛かる）
        pt_rel = detect_pt_relation(query)
        attr_sql += pt_relation_sql(pt_rel)
        if pt_rel:
            print(f"  P/T関係ゲート: {pt_rel}（数値P/Tのみ・::int 比較）")
        # 部族（サブタイプ）ゲート（決定的辞書・全腕+直行路に掛かる）
        tribal = detect_tribal(query)
        attr_sql += tribal_filter_sql(tribal)
        if tribal:
            print(f"  部族ゲート: {tribal}（type_line 単語境界照合）")
        # EDH 固有色・ブラケットゲート（R13・決定的検出＝ルーター無改修で効く）。
        # attr_sql に足すことで全腕（vec/FTS/強度腕）と直行路に同時に掛かる
        ci = detect_color_identity(query)
        bracket = detect_bracket(query)
        edh_intent = ci is not None or bracket is not None
        attr_sql += color_identity_filter_sql(ci)
        attr_sql += edh_gate_sql(edh_intent, bracket)
        if ci is not None:
            label = ",".join(ci) if ci else "無色"
            print(f"  固有色ゲート: ⊆ {{{label}}}（banned 除外"
                  + (f"・ブラケット{bracket}" + ("＝GC 除外" if bracket <= 2 else "")
                     if bracket is not None else "") + "）")
        elif bracket is not None:
            print(f"  ブラケット{bracket}ゲート（banned 除外"
                  + ("・GC 除外" if bracket <= 2 else "・GC 可") + "）")
        # カウンター条件の整合（R12 の検索側・2026-07-08 採点で極性を精密化）:
        #   「条件付き〜」明示 → 条件付きがど真ん中（無条件を降格）
        #   無修飾（format も数値も無い）→ 無条件がど真ん中（条件付きを降格）
        #   crisp 修飾つき（「2マナ以下の」「レガシーの」等）→ 整合を発動しない。
        #     本人採点の実測: これらのクエリでは Keep Safe/Daze 等の条件付きも 2＝
        #     「crisp 制約を満たすカウンター」であることが本質で、条件性は grade に効かない
        #   固有色修飾（「青黒で使える」）も crisp 修飾＝同じ理由で不発（R13）
        counter_align = None
        if counter_mode:
            if '条件付き' in query or 'conditional' in query.lower():
                counter_align = 'conditional'
            elif (format is None and ci is None
                    and not re.search(r'[0-9０-９一二三四五六七八九十]', query)):
                counter_align = 'unconditional'
        if attr_sql:
            print(f"  構造化フィルタ:{attr_sql}")

        # 構造化オンリー直行路: 正解集合が構造化列の WHERE で完全に定義できる＝意味検索を
        # 通さない（override 込みの最終判断はここで行う）。2経路:
        #  (1) キーワード能力クエリ（kw_only・従来）
        #  (2) EDH 意図クエリで意味の残余が構造化フラグだけ（色⊆＋is_mana_boost 等・R13）。
        #      「ラクドスカラーのマナ加速」は色ゲート∧is_mana_boost で crisp＝ただの SQL。
        #      意味検索はここで Sol Ring 等の EDH 定番を連れて来られない（プール飢餓）ため、
        #      直行路（edhrec 順）の方が正しく返る（重み調整で殴らない＝周転円回避・design ledger）。
        edh_direct = (edh_intent
                      and (mana_producer or kw_only)
                      and not has_fuzzy_semantic(query))
        # P/T 関係クエリも意味の残余が無ければ直行（正解集合は WHERE で完全定義済み）
        pt_direct = pt_rel is not None and not has_fuzzy_semantic(query)
        # 部族クエリも同様（「蟹」= type_line 照合で完全定義・並びは play-rate/edhrec）
        tribal_direct = tribal is not None and not has_fuzzy_semantic(query)
        if not (tournament_boost or removal_mode or counter_mode) and (kw_only or edh_direct or pt_direct or tribal_direct):
            print("  構造化オンリー直行路（意味検索スキップ・"
                  + ("EDH＝edhrec順" if edh_intent else "play-rate順") + "）")
            return self._structured_search(top_k, fmt_sql, type_sql, attr_sql,
                                           edh_order=edh_intent)

        vec     = self._embed(expanded)
        v_rows  = self._vector_search(vec, top_k, fmt_sql, type_sql, attr_sql)
        en_rows = self._en_text_search(en_kws, top_k, fmt_sql, type_sql, attr_sql,
                                          removal_mode=removal_mode)
        ja_rows = self._ja_text_search(ja_kws, top_k, fmt_sql, type_sql, attr_sql)
        # #22: boost クエリは play-rate 上位を候補腕として追加（プール飢餓対策）
        st_rows = (self._strength_candidates(top_k, format,
                                             fmt_sql, type_sql, attr_sql,
                                             removal_mode=removal_mode,
                                             counter_mode=counter_mode)
                   if tournament_boost else [])
        # R13: EDH 意図クエリは edhrec_rank 上位を候補腕として追加（EDH 版プール飢餓対策）。
        # 複数腕は連結で RRF に参加（同一カードは両腕から寄与を受ける＝RRF の自然な挙動）
        edh_rows = (self._edh_candidates(top_k, fmt_sql, type_sql, attr_sql,
                                         removal_mode=removal_mode,
                                         counter_mode=counter_mode)
                    if edh_intent else [])

        elapsed = (time.perf_counter() - t0) * 1000
        print(f"  vec:{len(v_rows)} en_fts:{len(en_rows)} "
              f"ja_fts:{len(ja_rows)}"
              + (f" strength:{len(st_rows)}" if st_rows else "")
              + (f" edh:{len(edh_rows)}" if edh_rows else "")
              + f" ({elapsed:.0f}ms)")

        return self._rrf_merge(v_rows, en_rows, ja_rows, top_k,
                               tournament_boost=tournament_boost,
                               removal_mode=removal_mode,
                               counter_mode=counter_mode,
                               format=format,
                               st_rows=st_rows + edh_rows,
                               counter_align=counter_align)

    def close(self):
        self.db.close()


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
            # （2026-07-11 修正: 従来 8 タプルを 6 個で unpack する既存バグ＝
            #   --output 指定時のみ ValueError で落ちるデモ経路だった）
            en_kws, ja_kws, _, _, _, _, _, _, _ = extract_keywords(q)
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
