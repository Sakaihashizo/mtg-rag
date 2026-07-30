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
import removal_direct   # 除去直行路（卒業レジストリ＝検証終了クエリ・2026-07-17）
import counter_direct   # 確定カウンター直行路（卒業レジストリ・2026-07-19）
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

# ablation（切除実験）用スイッチ・2026-07-30。**既定 False＝本番挙動は完全に不変**。
# 用途: 腕を 1 本ずつ外して「その腕が実際に何点ぶん働いているか」を測る（足す側の
# A/B〔門の ON/OFF・id=98/99〕の逆向き）。動機は panel 第二戦の縮小派の自白
# 「ベクトル腕を単独で走らせた実測が存在しない＝決着は腕を殺して看板が何点落ちるか」。
#   ABLATE_VECTOR=1 → クエリ埋め込みのベクトル腕を外す（_embed も呼ばない）
#   ABLATE_HYDE=1   → HyDE 腕（理想カード文の埋め込み）を外す
#   両方 1          → 埋め込みを一度も使わない＝FTS＋構造化列だけの世界
# 環境変数で渡すのは、呼び出し側の署名を汚さず eval 走行だけで切り替えるため。
ABLATE_VECTOR = os.environ.get("ABLATE_VECTOR") == "1"
ABLATE_HYDE   = os.environ.get("ABLATE_HYDE") == "1"

MODEL_REGISTRY = {
    "SMALL_V2": {
        "model_name": "intfloat/multilingual-e5-small",
        "prefix": "query: ",
        "cards_table": "mtg_cards_v2",
        "embeddings_table": "mtg_embeddings_small_v2",
    },
    # BASE_V2 は 2026-07-20 退役（一対比較 eval id=92 vs 93 でベクトル所有層の
    # 全敗を確認・本人裁定）。テーブルは DROP 済み＝このキーの指定は実行時に落ちる。
    # 復元: pg_restore -d rag_dev /mnt/new_hdd/db_archives/mtg_embeddings_base_v2_archive_20260720.dump
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
    # 英語定型キー（mode 退役 2026-07-30 で発覚した非対称の是正）: カウンター族の
    # キーは日本語 4 本だけで、英語クエリの意図はルーターの bit だけが立てていた
    # （bit 退役で「counter target spell」の護法減点が消え Artifact Blast[0] が流入
    # した実測）。draw 族の英語キー（2026-07-23）と同じ是正。旗だけ＝FTS 展開は
    # 足さない（bit が担っていた仕事の忠実な移植・展開の追加は別便で測ってから）
    # 2026-07-30 の初版は「bit の仕事の忠実な移植」として旗だけにしたが、ablation で
    # 埋め込みを外すと FTS 腕が空＝返却ゼロになると判明（id=143「counter target spell」
    # 1.000→0.000）。日本語キーと同じ展開語を与える（英語クエリだけ FTS が動かない
    # 非対称の是正＝draw 族 7/23・flying と同型の三例目）
    "counter target spell": {"en": "counter target spell",
                       "ja": ["呪文１つを対象とする。それを打ち消す",
                              "呪文１つを対象とし、それを打ち消す",
                              "ないかぎり、それを打ち消す"],
                       "counter_mode": True},
    "counterspell":         {"en": "counter target spell",
                       "ja": ["呪文１つを対象とする。それを打ち消す",
                              "呪文１つを対象とし、それを打ち消す",
                              "ないかぎり、それを打ち消す"],
                       "counter_mode": True},
    # 「〜に対応できる/対処できる」＝除去意図の語彙（mode 退役で発覚した取り逃し是正）:
    # 錨クエリ「相手のウラモグに対応できるカード」は除去の字が無く、ルーターの bit
    # だけが除去門を立てていた。語彙として決定的に立てる。「対策」は入れない
    # （「墓地対策」＝ヘイトカードの意味が混ざる＝誤発動側・非対称試験で固定）
    "に対応できる": {"removal_mode": True},
    "に対処できる": {"removal_mode": True},
    # ドロー系
    "カードを引く":    {"en": "draw cards",             "ja": ["カードを引く"]},
    "手札補充":        {"en": "draw cards",             "ja": ["カードを引く"]},
    "ドロー":          {"en": "draw a card",            "ja": ["カードを引く"]},
    "2枚引く":         {"en": "draw two cards",         "ja": ["カードを２枚引く"]},
    "二枚引く":        {"en": "draw two cards",         "ja": ["カードを２枚引く"]},
    # 英語定型キー（照合は substring なので言語不問。日本語キーだけだと素の英語
    # クエリが辞書に掛からず FTS 展開ゼロ＝ベクトル一本腕で走る非対称があった
    # ——「draw two cards」の en_fts:0/ja_fts:0 実測から発見・2026-07-23 是正）
    "draw two cards":  {"en": "draw two cards",         "ja": ["カードを２枚引く"]},
    "draw a card":     {"en": "draw a card",            "ja": ["カードを引く"]},
    "draw cards":      {"en": "draw cards",             "ja": ["カードを引く"]},
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
    # 英語キー（2026-07-30・ablation で発覚）: 英語クエリは辞書に載らない限り FTS 腕が
    # 空で、埋め込みが落ちると返却ゼロだった（id=143「flying creature」0.936→0.000）。
    # **keyword フィールドは意図的に持たせない**——test_structured_gate が
    # ("flying creature", False)＝「辞書は沈黙してルーターに譲る」を裁定として縫って
    # おり、keyword を付けると構造化オンリー直行路が発火して誤発動になる（実測で捕獲）。
    # ここで足すのは FTS 展開語だけ＝腕を空にしない目的に限定する。英語クエリに
    # keyword 門を開くかは別の裁定（本人待ち）。
    "flying":          {"en": "flying",        "ja": ["飛行"]},
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

# 生得キーワードの英語キーを日本語エントリから自動生成する（2026-07-31・本人指示
# 「flying だけじゃなく他のキーワード能力も日本語と同じのを足して」）。
# 動機: 英語クエリは辞書に載らない限り FTS 腕が空になる（ablation で実測=id=143 の
# 「flying creature」0.936→0.000）。draw 族 7/23・counter/flying 7/30 と同じ非対称の
# 一括是正。日本語キー側を足せば英語キーも自動で付く＝二重管理を作らない。
#
# **keyword フィールドは意図的に写さない**: 付けると構造化オンリー直行路が英語クエリで
# 発火し、test_structured_gate が縫っている ("flying creature", False)＝「辞書は沈黙して
# ルーターに譲る」の裁定を破る（実測で捕獲済み）。ここで足すのは FTS 展開語だけ。
# 英語クエリに keyword 門を開くかは別の裁定（本人待ち）。
#
# 既知の弱点（部分一致ゆえ・FTS 展開のみなので実害は腕の雑音に限定）: "flash" は
# "flashback"、"ward" は "toward/warden"、"reach" は "reaches" 等に部分一致しうる。
# 最長一致の勝ち抜き規則は「両方が辞書キーのとき」しか効かないため、実クエリで
# 害が観測されたら個別に除外リストへ落とす（人間レビュー昇格方式の逆向き運用）。
_KW_EN_KEYS = {}
for _jp, _v in list(QUERY_EXPAND.items()):
    if not _v.get("keyword"):
        continue
    _en_key = (_v.get("en") or "").strip().lower()
    if not _en_key or _en_key in QUERY_EXPAND or _en_key in _KW_EN_KEYS:
        continue
    _KW_EN_KEYS[_en_key] = {"en": _v["en"], "ja": list(_v.get("ja") or [])}
QUERY_EXPAND.update(_KW_EN_KEYS)


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

# カード名部分一致検索（「カード名にナヒリとつくカード」等・2026-07-12）。
# ルーターの filters に name 系スロットは無く、LLM は search_query 圧縮＋type 幻出で
# 壊しがち（ナヒリ事故: 7B が Creature を幻出し PW が全滅）。正解集合は
# name LIKE '%X%' で完全定義できる crisp なジャンル＝決定的検出→SQL 直行。
_NAME_SEARCH_RES = [
    # 「カード名/名前に X (と/が/を)つく・含む・入る」
    re.compile(r'(?:カード名|名前)に\s*「?([^「」、。\s]+?)」?\s*(?:と|が|を)?\s*(?:付く|つく|入る|入って|含む|含ま)'),
    # 「X という名前」
    re.compile(r'「?([^「」、。\s]+?)」?とい?う名前'),
    # 「X とつくカード/名前」（「カード名に」の省略形・「とつく」は名前参照でしか使われない）
    re.compile(r'([^「」、。\s]+?)」?と(?:付|つ)く(?:カード|名前)'),
]


def detect_name_search(query: str):
    """カード名部分一致の検索意図の決定的検出。検索語（部分文字列）か None を返す。"""
    for pat in _NAME_SEARCH_RES:
        m = pat.search(query)
        if m and m.group(1):
            return m.group(1)
    return None


def name_contains_sql(term) -> str:
    """カード名（日英）の部分一致フィルタ。' と LIKE ワイルドカードをエスケープ
    （term はユーザー入力由来の任意文字列）。日本語名は LIKE・英名は大文字小文字を
    無視する ILIKE。"""
    if not term:
        return ""
    t = (term.replace("\\", "\\\\").replace("'", "''")
             .replace("%", "\\%").replace("_", "\\_"))
    return (f" AND (c.japanese_name LIKE '%{t}%'"
            f" OR c.card_name ILIKE '%{t}%')")


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
# 日本語セット名 → セットコード集合（2026-07-27 セット検索・本人裁定 2 点:
# 「set_codes を配列で持つ」「エルドレインは王権∨森の OR＝細かく指定しない方が悪い」）。
# 検出は原文（gate_q）への部分一致・最長キー優先。曖昧な短縮呼称（「エルドレイン」等）は
# 該当しうる全セットへ広く展開する（システムが勝手に一つへ絞らない）。
# 辞書は手動の主要セットから開始＝部族辞書と同じ人間レビュー昇格方式。
# コードの実在は test_set_gate.py が DB と突き合わせて検証する。
SET_WORDS_JA = {
    # 近年スタンダード（新しい順）
    "久遠の終端":            ["eoe"],
    "FINAL FANTASY":         ["fin"],
    "ファイナルファンタジー": ["fin"],
    "タルキール：龍嵐録":     ["tdm"],
    "タルキール:龍嵐録":      ["tdm"],
    "龍嵐録":                ["tdm"],
    "霊気走破":              ["dft"],
    "ファウンデーションズ":   ["fdn"],
    "ダスクモーン":          ["dsk"],
    "ブルームバロウ":        ["blb"],
    "サンダー・ジャンクション": ["otj"],
    "サンダージャンクション": ["otj"],
    "カルロフ邸殺人事件":     ["mkm"],
    "カルロフ邸":            ["mkm"],
    "イクサラン：失われし洞窟": ["lci"],
    "イクサラン:失われし洞窟": ["lci"],
    "失われし洞窟":          ["lci"],
    "エルドレインの森":      ["woe"],
    "機械兵団の進軍":        ["mom"],
    "ファイレクシア：完全なる統一": ["one"],
    "ファイレクシア:完全なる統一": ["one"],
    "完全なる統一":          ["one"],
    "兄弟戦争":              ["bro"],
    "団結のドミナリア":      ["dmu"],
    "ニューカペナの街角":    ["snc"],
    "ニューカペナ":          ["snc"],
    "神河：輝ける世界":      ["neo"],
    "神河:輝ける世界":       ["neo"],
    "輝ける世界":            ["neo"],
    "真紅の契り":            ["vow"],
    "真夜中の狩り":          ["mid"],
    "ストリクスヘイヴン":    ["stx"],
    "カルドハイム":          ["khm"],
    "ゼンディカーの夜明け":  ["znr"],
    "イコリア":              ["iko"],
    "エルドレインの王権":    ["eld"],
    "灯争大戦":              ["war"],
    "ラヴニカの献身":        ["rna"],
    "ラヴニカのギルド":      ["grn"],
    "ドミナリア・リマスター": ["dmr"],
    "ラヴニカ・リマスター":  ["rvr"],
    "イニストラード・リマスター": ["inr"],
    # 特殊セット・場外セット
    "モダンホライゾン3":     ["mh3"],
    "モダンホライゾン2":     ["mh2"],
    "モダンホライゾン":      ["mh1", "mh2", "mh3"],
    "指輪物語":              ["ltr"],
    "ウォーハンマー":        ["40k"],
    "アサシンクリード":      ["acr"],
    "フォールアウト":        ["pip"],
    "ジャンプスタート":      ["jmp"],
    # 短縮・ブロック呼称（広く OR＝細かく指定しない方が悪い・本人裁定）
    "エルドレイン":          ["eld", "woe"],
    "イクサラン":            ["xln", "rix", "lci"],
    "ドミナリア":            ["dom", "dmu", "dmr"],
    "神河":                  ["chk", "bok", "sok", "neo"],
    "イニストラード":        ["isd", "dka", "avr", "soi", "emn", "mid", "vow", "dbl", "inr"],
    "ゼンディカー":          ["zen", "wwk", "roe", "bfz", "ogw", "znr"],
    # 「ラヴニカ」は次元（舞台）を指す語として解釈（2026-07-28 本人裁定「ラヴニカと
    # 言ったときに灯争大戦が丸ごと射程に入ってほしい」）＝ラヴニカが舞台のセットを
    # 全部含める: 初代3・回帰3・ギルド2・灯争大戦・リマスター
    # ＋カルロフ邸(mkm)・Clue Edition(clu) も舞台がラヴニカ（2026-07-28 本人 GO
    # 「その辺も含めて。最近は次元の名前がエキスパンションに入らないのが多い」）
    "ラヴニカ":              ["rav", "gpt", "dis", "rtr", "gtc", "dgm", "grn", "rna",
                              "war", "rvr", "mkm", "clu"],
    # ローウィン＝シャドウムーアは同一次元の表裏だが、プレイヤーの語感では
    # ブロック単位で呼び分けるため区別して登録（2026-07-28 本人の問いから）
    "ローウィン":            ["lrw", "mor"],
    "モーニングタイド":      ["mor"],
    "シャドウムーア":        ["shm", "eve"],
    "イーヴンタイド":        ["eve"],
    "テーロス":              ["ths", "bng", "jou", "thb"],
    "タルキール":            ["ktk", "frf", "dtk", "tdm"],
    "アモンケット":          ["akh", "hou"],
    "カラデシュ":            ["kld", "aer"],
    "ミラディン":            ["mrd", "dst", "5dn", "som", "mbs", "nph"],
    # 旧セット単体（大会・統率者で言及頻度が高いもの）
    "タルキール覇王譚":      ["ktk"],
    "タルキール龍紀伝":      ["dtk"],
    "運命再編":              ["frf"],
    "破滅の刻":              ["hou"],
    "霊気紛争":              ["aer"],
    "戦乱のゼンディカー":    ["bfz"],
    "エルドラージ覚醒":      ["roe"],
    "異界月":                ["emn"],
    "イニストラードを覆う影": ["soi"],
}
_SET_KEYS_BY_LEN = sorted(SET_WORDS_JA.keys(), key=len, reverse=True)


def detect_set_ja(query: str):
    """日本語セット名の決定的検出（原文への部分一致・最長キー優先）。
    複数のセット名が同居したら和集合（「エルドレインとイコリアの…」）。
    ヒットが無ければ None。返り値はコードの昇順リスト。"""
    if not query:
        return None
    codes: set = set()
    consumed = query
    for key in _SET_KEYS_BY_LEN:
        if key in consumed:
            codes.update(SET_WORDS_JA[key])
            # 最長一致を勝たせる: 拾ったキーは潰して短いキーの重複検出を防ぐ
            # （「エルドレインの王権」→ eld のみ・「エルドレイン」の [eld,woe] を足さない）
            consumed = consumed.replace(key, "◇")
    return sorted(codes) if codes else None


def set_filter_sql(codes) -> str:
    """収録セットのハードフィルタ。set_codes は「一度でも収録された全セット」の配列
    （enrich_printings.py・代表印刷 set_code の再録上書き問題への答え）。"""
    if not codes:
        return ""
    arr = ", ".join("'" + c.replace("'", "") + "'" for c in codes)
    return f" AND c.set_codes && ARRAY[{arr}]::text[]"


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
    "ローグ": "Rogue",
    # ならず者=Rogue（2026-07-29 本人報告「ならず者で部族がほぼ出なかった」＝辞書の欠け。
    # 暗殺者・忍者は居るのに Rogue だけ不在だった）
    "ならず者": "Rogue",
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


# 型の否定（「非クリーチャーカード」「土地以外のカード」等・2026-07-13 本人発見。
# query_log id=38:「マナコスト7以上の…非クリーチャーカード」にクリーチャーが 5/10 混入）。
# type_filter スキーマは肯定8種のみで否定を表現する語彙が無く、embedding は否定を
# 原理的に見ない（「速攻を持たない」と同構図）＝決定的検出で SQL の NOT に直結する。
# 発動はクエリ末尾に限る（「非クリーチャー呪文を打ち消すカード」のような対象語では
# 立てない＝type 語の末尾ルールと同じ思想・誤発動ゼロ優先の非対称設計）。
_TYPE_ALT = '|'.join(TYPE_WORDS_JA.keys())
_NEG_TYPE_TAIL_RE = re.compile(
    rf'(?:非({_TYPE_ALT})|({_TYPE_ALT})(?:以外|では?ない|じゃない))'
    rf'(?:の)?(?:カード)?$')


def detect_neg_type(query: str):
    """型の否定意図の決定的検出（クエリ末尾のみ）。英語 type か None を返す。"""
    stripped = query.strip().rstrip('?？。！!、．.　 ')
    m = _NEG_TYPE_TAIL_RE.search(stripped)
    if m:
        return TYPE_WORDS_JA.get(m.group(1) or m.group(2))
    return None


def neg_type_filter_sql(en_type) -> str:
    """型の否定フィルタの SQL 断片。判定列は face_types＝「手札から直接唱えられる面の
    type_line 集合」（add_face_types.py・face_cmcs と同一の mana_cost 非空面規則）。
    唱えられる面に 1 つでも非該当型の面があれば適格: modal_dfc の Valki//Tibalt は
    Tibalt 面（PW）で「非クリーチャー」を通過し、transform の鏡割りの寓話は表面 Saga
    で通過（裏面クリーチャーは手札から唱えられない＝2026-07-13 本人の言語化
    「手札から直接唱えられるか」が判定基準）。COALESCE は populate 前の環境でも
    type_line 単面判定に落ちる防御。"""
    if not en_type:
        return ""
    return (" AND EXISTS (SELECT 1 FROM"
            " unnest(COALESCE(c.face_types, ARRAY[c.type_line])) ft"
            f" WHERE ft NOT LIKE '%{en_type}%')")


# ドロー枚数ゲート（R14「ドロー族＝行為ベース」2026-07-23 の検索側・決定的検出）。
# 判定列は draw_count/draw_x（enrich_draw.py＝命令形 Draw N のみ数えた列）。
# ルーターの写しでも主要トークン（N枚引く/ドロー/draw N cards）は保存される実測
# （eval_router_cache 確認済み）なので、既存ゲート同様 gate_q（原文優先）を見る。
_DRAW_EN_RE = re.compile(
    r'\bdraw(?:s|ing)?\s+(a|an|one|two|three|four|five|six|seven|eight|nine|'
    r'ten|\d+)\s+cards?\b', re.I)
_DRAW_JA_N_RE = re.compile(r'([0-9０-９一二三四五六七八九十]+)\s*枚\s*(?:カードを?)?(?:引|ドロー)')
# 「手札補充」は対象外（GT 実測 2026-07-23: 引かずに手札へ加える選別カード〔Narset・
# Consult the Star Charts 型〕が正解に含まれる＝「補充」は「引く」より広い語）。
# 「フィルタリング」等の選別文脈も対象外（R14 問3「選別は別クエリの担当」・
# GT 実測で grade2 の過半が非ドローの選別カード）。
_DRAW_JA_ANY_RE = re.compile(r'ドロー|カードを引')
_DRAW_EXCLUDE_RE = re.compile(r'フィルタ|ルーティング|選別')
_DRAW_NUM = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4,
             'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
_JA_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7,
           '八': 8, '九': 9, '十': 10}


def detect_draw_min(query: str):
    """ドロー意図の決定的検出。最小 Draw 枚数 N(int) か None（不発）を返す。
    枚数指定（「2枚引く」「draw two cards」）→ N、枚数なしのドロー語
    （「ドロー」「カードを引く」）→ 1。誤発動ゼロが必須の非対称設計
    （draw 語が無いクエリでは絶対に発動しない）。
    DRAW_GATE=off で不発化（対照実験用・HNSW_SCAN と同じ流儀）。"""
    if os.environ.get('DRAW_GATE') == 'off':
        return None
    q = query or ''
    m = _DRAW_EN_RE.search(q)
    if m:
        w = m.group(1).lower()
        return _DRAW_NUM.get(w) or int(w)
    m = _DRAW_JA_N_RE.search(q)
    if m:
        w = m.group(1)
        # 全角→半角→int、漢数字は辞書（十一以上の合成は枚数クエリに実在しない想定）
        h = w.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
        if h.isdigit():
            return int(h)
        if w in _JA_NUM:
            return _JA_NUM[w]
    # 枚数なしのドロー語は選別文脈（フィルタリング等）では発動しない（上の定数の注記）
    if _DRAW_JA_ANY_RE.search(q) and not _DRAW_EXCLUDE_RE.search(q):
        return 1
    return None


def draw_filter_sql(n) -> str:
    """ドロー枚数ゲートの SQL 断片。draw_count >= N（命令形の実ドロー）に加え、
    draw_x（可変枚数＝X を N 以上で選べば引ける）も通す＝R14 モード裁定
    「選択2枚でも2枚引けることはひける」の同族。全腕＋直行路に掛かる attr_sql 用。"""
    if not n:
        return ""
    return f" AND (c.draw_count >= {int(n)} OR c.draw_x)"


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


def detect_intent_modes(query: str) -> tuple[bool, bool]:
    """除去/カウンター意図だけを QUERY_EXPAND から決定的に検出する軽量版。
    用途は原文（gate_q）での補完（2026-07-30・mode 退役の対）: ルーターが写しを
    壊すと extract_keywords（書き換え後を見る）の検出が不発になるため
    （実測: 7B が錨ウラモグを「アトラクサ対策」へ書き換え＝「対応できる」が消えた）。
    型肯定の原文補完（2026-07-27）と同じ様式。戻り値 (removal_mode, counter_mode)。"""
    if not query:
        return False, False
    matched = [jp for jp in QUERY_EXPAND if jp in query]
    rm = any(QUERY_EXPAND[k].get("removal_mode") for k in matched)
    cm = any(QUERY_EXPAND[k].get("counter_mode") for k in matched)
    return rm, cm


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


def mana_direct_gate(query: str, mana_producer: bool) -> bool:
    """マナ加速クエリの構造化オンリー直行路ゲート（2026-07-26 新設）。

    正解集合が `is_mana_boost=TRUE ∧ cmc/type/format の WHERE` で完全に定義できる
    なら意味検索を通さず SQL 直行（並び＝play-rate 降順）。除去 0.33→直行路と同型。

    由来: 本人の指摘「構造的に判別できる定義と 1 マナと構造化のど真ん中を合わせた
    だけなのに、なぜ的中率が低いんだ？」（2026-07-26）。診断の結果、門
    （is_mana_boost）は正しく効いていたが、直行路の発動条件が edh_intent を
    要求していたためハイブリッドに落ち、絞り込み後の並びが意味の近さだけ＝
    品質信号ゼロだった（「1マナのマナクリーチャー」で Birds of Paradise・
    Llanowar Elves が top-10 圏外・NDCG 0.703）。

    ゲートの失敗の向き（既存ゲート試験と同じ非対称）:
      誤発動 = 意味の残余を落として間違った集合を返す ＝ 有害（ゼロを必須とする）
      取り逃し = ハイブリッドに落ちるだけ ＝ 無害（遅く・正しく）

    境界（既知の残余リスク・edh_direct と同じ露出）: mana_producer はルーター
    （LLM）の出力なので、土地サーチ系（「ランプ」「土地加速」＝ net-mana 定義と
    一致しない）に誤って True が立つと直行してしまう。辞書側では両語を意図的に
    mana_struct に tag していない＝ has_fuzzy_semantic が fuzzy と判定して門前払い
    する二重の守りになっている。
    """
    if not mana_producer:
        return False
    if has_fuzzy_semantic(query):
        return False
    # 局所ガード（2026-07-26・試験が捕まえた穴の応急処置）: QUERY_EXPAND に
    # 見出しが無いために has_fuzzy_semantic が素通りさせる意味語を、この門でだけ
    # 塞ぐ。「コンボ」は辞書に "無限コンボ" しか無く「コンボパーツ」等が拾えない。
    # 本筋の直しは辞書側に見出しを足すことだが、QUERY_EXPAND への追加は FTS の
    # 展開語まで変えて「コンボに使えるカード」(NDCG 0.606) の挙動を動かすため、
    # 別便の実験として分離する（ここでは直行路の安全だけ確保する）。
    lowered = query.lower()
    if "コンボ" in query or "combo" in lowered:
        return False
    return True


def dig_draw_gate(query: str) -> bool:
    """「ドローしながらフィルタリング」クエリの構造化オンリー直行路ゲート（2026-07-31）。

    正解集合が `dig IS NOT NULL ∧ draw_count >= 1` で完全に定義できる＝
    **入れ替えてから引く**（本人裁定の原文「例外って思案とか定業くらいのもんなんだよな、
    入れ替えてから引いてる」）。並びは play-rate 降順で、実測の顔ぶれは
    思案 1,995 デッキ / 定業 827 / 選択 650 / 考慮 438 / 師範の占い独楽 366。

    列の線（同日制定・enrich_tutor.py と grading_conventions の対応節）:
      dig  = 上から N 枚という**位置**に縛られる掘削（占術・諜報・切削して拾う型を含む）
      draw = 「Draw N」の行為（R14）
      **引くだけ（渦まく知識 = Ancestral Recall の下位互換）は dig=NULL**＝本人裁定。
      ルーティング（捨てて引く・鏡割りの寓話 II 章）も dig でない＝この門は拾わない。

    ゲートの失敗の向き（既存ゲート試験と同じ非対称）:
      誤発動 = 意味の残余を落として間違った集合を返す ＝ 有害（ゼロを必須とする）
      取り逃し = ハイブリッドに落ちるだけ ＝ 無害（遅く・正しく）

    発動条件は**両方の語が原文に在ること**に限定する（片方だけの「カードを引く
    カード」「占術できるカード」は既存の族の担当＝この門は沈黙する）。
    """
    if not query:
        return False
    q = query.lower()
    dig_words = ('フィルタリング', 'フィルター', '掘れる', '掘る', 'filtering', 'filter')
    draw_words = ('ドロー', '引く', '引ける', '引き', 'draw')
    if not any(w in query or w in q for w in dig_words):
        return False
    if not any(w in query or w in q for w in draw_words):
        return False
    # 意味の残余の見方（既存門と同じ二重の守りだが、掛け方が違う）: この門を起こす語
    # （ドロー等）自体が QUERY_EXPAND に載っていて fuzzy と数えられるため、素の
    # has_fuzzy_semantic(query) は必ず True になり門が永久に不発になる（実測）。
    # **この門が説明できる語を落とした残りに** fuzzy が在るかを見る＝「意味の残余が
    # 構造化フラグだけか」という EDH 直行路の判定と同じ思想の、語を引いた版。
    residue = query
    for w in dig_words + draw_words + ('できる', 'カード', 'しながら', 'ながら', 'と', 'や', 'する'):
        residue = residue.replace(w, '')
    if has_fuzzy_semantic(residue):
        return False
    return True


def dig_draw_filter_sql() -> str:
    """dig ∧ draw の共通集合フィルタ（本人裁定「共通集合とってもらわなきゃね」）。"""
    return " AND c.dig IS NOT NULL AND c.draw_count >= 1"


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
# 集計は大会系 7 フォーマット（2026-07-14 に Vintage/Pauper/Duel Commander を追加・
# 使用率化）。map に無いフォーマットは横断フォールバック＝各フォーマット採用率の
# MAX（「どこかの環境で一線級なら強い」・新フォーマット追加で既存値が壊れない単調性）。
# 2026-07-22（本人GO・moxfield_edh 導入）: "commander" は Duel Commander の近似を
# 卒業し、実データの多人数 Commander（moxfield_edh・母数431）を見る。"duel" は
# Duel Commander（mtgtop8_edh・母数959）のまま＝2人用/4人用は format_name 列で
# 区別（テーブルは edh_card_strength を共用・EDH_FORMAT_NAMES 参照）。
CFS_FORMAT_MAP = {
    "legacy": "Legacy", "modern": "Modern",
    "pioneer": "Pioneer", "standard": "Standard",
    "vintage": "Vintage", "pauper": "Pauper",
    "duel": "Duel Commander", "commander": "Commander",
}

# edh_card_strength（シングルトン系・物理分離テーブル）に属す format_name。
# card_format_strength（60枚構築）と混ざらないための判定に使う。
EDH_FORMAT_NAMES = ("Duel Commander", "Commander")

# フォーマット横断フォールバック（format 指定なし）の集計対象＝本線 4 フォーマット。
# GT の R11 機械採点（「全 4F 合計 250 デッキ」閾値）と同じ土俵に固定する。
# 2026-07-14 の使用率化実験（id=65〜70）の結論: 横断比較へ新フォーマットを
# 入れる・率に変える、はどちらも「GT が知らない新顔」を大量に連れてくるため、
# R11 の率版再設計（GT の機械再採点＝物差し更新）とセットでないと評価できない。
# per-format の率（Vintage/Pauper/EDH 指定時）だけ先行導入し、横断は現状維持。
MAINLINE_FORMATS = ("Standard", "Pioneer", "Modern", "Legacy")


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


# ─── 統率者名→固有色ゲート（R13 拡張・2026-07-20 本人裁定） ──────────────
# 「アトラクサで使える除去」= 統率者名から固有色を解決して同じ ⊆ ゲートへ流す。
# 前提（design-premise-ledger 流に明示）:
#   - 検索ツールに統率者名を打つ人の圧倒的多数は構築文脈（EDHREC の構造そのもの）
#     ＝既定 ON。「統率者は目的語になりえない」は文法では偽（「〜を使う」=構築・
#     「〜対策」=敵対）だが事前確率として採用＝敵対語彙の明示があるときだけ不発
#     （推定無罪の構造）。敵対語彙は閉集合＝列挙可能な側をリストにする。
#   - 敵対判定は名前ごとの近傍（クエリ全域にしない）。錨クエリ第1号=「アトラクサを
#     使ったデッキで相手のウラモグに対応できるカード」→ アトラクサ発動・ウラモグ除外。
#     全域判定だと「対応」の一語がゲート丸ごと殺す（2026-07-20 設計格上げ）。
#   - 名前キー: 日本語正式名・読点短縮名（「法務官の声、アトラクサ」→「アトラクサ」）・
#     無読点名の末尾カタカナ（「寛大なるゼドルー」→「ゼドルー」）・英語正式名（小文字）。
#   - 同名候補の固有色が単一なら発動（アトラクサ型・無害）・割れたら不発（オムナス型）。
#     複数の統率者名で固有色が食い違っても不発（安全側）。
#   - 実測（2026-07-20）: 伝説 3,125 枚・読点短縮名 1,722 種＝一意 85%・
#     同名同色 5.7%・色割れ 9.2% → 91% を機械で安全に解決できる。
#   - 既知の限界（v1）: 伝説名を含む非伝説カード名（「ウラモグの手先」等）がクエリに
#     そのまま書かれると伝説側の名前として誤検知しうる。query_log から実例を拾って
#     本人レビューで敵対語彙/例外を昇格させる運用（辞書昇格と同じ human-in-the-loop）。

_CMD_HOSTILE_BEFORE = re.compile(r'(?:相手|敵|対戦相手)の$')
_CMD_HOSTILE_AFTER = re.compile(
    r'^(?:を|に|への|の)?'
    r'(?:対策|対応|対処|倒|討|除去|破壊|追放|退場|効く|効き|回答|対象|止め|殺)')
_CMD_MIN_KEY = 3          # 2文字短縮名（アン等）は一般語と衝突するため索引に載せない
_CMD_KATAKANA_TAIL = re.compile(r'[ァ-ヴヶー]{3,}$')


def build_commander_index(db) -> dict[str, frozenset]:
    """伝説のクリーチャー全数から「名前キー → 固有色候補の集合」を構築する。
    値は固有色タプルの frozenset（要素 2 つ以上＝同名で色割れ＝解決不能の印）。
    起動時 1 回の全数ロード＝新セットの取り込みで自動追随する。"""
    rows = db.query(
        "SELECT japanese_name, card_name, color_identity FROM mtg_cards_v2 "
        "WHERE type_line LIKE '%Legendary Creature%'")
    index: dict[str, set] = {}
    for ja, en, ci in rows:
        ident = tuple(sorted(ci or []))
        keys = []
        if ja:
            keys.append(ja)
            if '、' in ja:
                keys.append(ja.rsplit('、', 1)[1])
            else:
                m = _CMD_KATAKANA_TAIL.search(ja)
                if m and m.group(0) != ja:
                    keys.append(m.group(0))
        if en:
            keys.append(en.lower())
        for k in keys:
            if len(k) >= _CMD_MIN_KEY:
                index.setdefault(k, set()).add(ident)
    return {k: frozenset(v) for k, v in index.items()}


def detect_commander_identity(
        query: str, index: dict[str, frozenset]
) -> Optional[tuple[list[str], list[str]]]:
    """クエリ中の統率者名から固有色を決定的に解決する。
    戻り値: (WUBRG ソート済みリスト, 根拠にした名前のリスト)。不発は None。
    非対称設計: 敵対文脈の名前は無視・色割れ/食い違いは不発（誤発動ゼロ側）。"""
    if not index:
        return None
    q_lower = query.lower()
    hits = []
    for key, idents in index.items():
        src = q_lower if key.isascii() else query
        pos = src.find(key)
        while pos != -1:
            hits.append((len(key), pos, key, idents))
            pos = src.find(key, pos + 1)
    if not hits:
        return None
    # 最長一致優先で重なりを解消（「アトラクサの後継、イクセル」はイクセル側が勝つ）
    hits.sort(key=lambda h: (-h[0], h[1]))
    taken: list[tuple[int, int, str, frozenset]] = []
    for ln, pos, key, idents in hits:
        if all(pos + ln <= s or pos >= e for s, e, _, _ in taken):
            taken.append((pos, pos + ln, key, idents))
    resolved = None
    names: list[str] = []
    for s, e, key, idents in sorted(taken):
        if (_CMD_HOSTILE_BEFORE.search(query[:s])
                or _CMD_HOSTILE_AFTER.match(query[e:])):
            continue          # 敵対文脈の名前は固有色の根拠にしない
        if len(idents) > 1:
            return None       # 同名で色割れ（オムナス型）＝解決不能＝不発
        ident = next(iter(idents))
        if resolved is not None and ident != resolved:
            return None       # 複数統率者名で食い違い＝不発（安全側）
        resolved = ident
        names.append(key)
    if resolved is None:
        return None
    return sorted(resolved), names


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
            # objects = 複数クラス列挙（2026-07-15 enrich 修理④で追加・
            # 「クリーチャー、エンチャント、PW」型。無ければ従来の object 単独）
            objset = set(e.get('objects') or ([e.get('object')]
                                              if e.get('object') else []))
            if objset & {'creature', 'permanent'}:
                return True
            if not objset and can_hit:
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
        # モデルの置き場所は環境変数で差し替え可能（2026-07-26・Lambda コンテナ化）。
        # 既定はローカル VM の実パス＝ここまでの挙動と同一。コンテナでは
        # MODEL_CACHE_DIR=/opt/models（イメージに焼いたモデル）を渡す＝実行時に
        # ダウンロードさせない（コールドスタートで数百 MB 取りに行かせない）
        self.model      = SentenceTransformer(
            cfg["model_name"],
            cache_folder=os.environ.get("MODEL_CACHE_DIR",
                                        "/mnt/new_hdd/hf_cache"),
        )
        # DB アクセスはドライバ切替層（db.py）経由＝ローカル psycopg2 / 本番 Data API を
        # DB_BACKEND 環境変数で切替（2026-07-12 移行・旧 self.conn 直書きは全廃）
        self.db = make_db()
        # HNSW 近似検索 + 構造化フィルタ併用時の取りこぼし対策（pgvector 0.8+）。
        # 既定の近似スキャンだと ef_search 件の近傍を見てから WHERE で絞るため、
        # cmc=1 等の選択的フィルタでは候補がほぼ脱落して数件しか残らない。
        # iterative_scan を有効化し、フィルタを満たす件数が揃うまで反復スキャンさせる。
        # フォーマット横断の採用率比較の平滑化定数（既定 0＝素の率）。
        # 経緯: 使用率化 v1（id=65）で母数 214 の EDH が率 MAX を支配する「小標本の罠」が
        # 実証され一時導入（id=66〜68）→ 根治は EDH の物理分離（edh_card_strength・
        # 2026-07-14 本人裁定）で行い、対症療法の平滑化は撤去。構築 6F は母数が
        # 同じ桁（984〜2,227）のため素の率で健全。対照実験用に機構だけ残す。
        self.rate_smooth_k = int(os.environ.get("RATE_SMOOTH_K", "0"))
        # スキャン順は HNSW_SCAN 環境変数で切替可（relaxed_order=既定 / strict_order）。
        # relaxed_order は距離近接行の返却順が DB の物理状態に敏感で、全行 UPDATE の後に
        # eval が微動する実測がある（2026-07-13 id=56・対照実験用に切替を残す）
        scan = os.environ.get("HNSW_SCAN", "relaxed_order")
        if scan not in ("relaxed_order", "strict_order"):
            scan = "relaxed_order"
        try:
            self.db.execute(f"SET hnsw.iterative_scan = {scan}")
        except Exception:
            pass  # pgvector < 0.8 では未対応 → 無視（rollback は db 層が実施済み）
        # 統率者名→固有色ゲートの名前索引（R13 拡張・2026-07-20）。
        # 失敗時は空索引＝名前ゲートだけ眠る（検索全体は殺さない・ただし声は出す）
        try:
            self._commander_index = build_commander_index(self.db)
        except Exception as e:
            print(f"  [警告] 統率者名索引の構築に失敗"
                  f"（固有色ゲートは名前検出なしで続行）: {e}")
            self._commander_index = {}
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
    ) -> dict[str, float]:
        """card_name → フォーマット内採用率（play_decks / total_decks）を1クエリで引く
        （#22 boost 用・2026-07-14 使用率化）。fmt が大会 7 フォーマットのいずれかなら
        その format の採用率、それ以外（None 等）は**各フォーマット採用率の MAX**
        （旧実装の全 F 合計 SUM は母数の大きいフォーマット偏重＝Standard 2,227 デッキと
        Duel Commander 214 デッキを同じ 1 票で足していた。率の MAX は「どこかの環境で
        一線級」の意味論で、新フォーマット追加でも既存値が動かない）。
        boost 側は結果内 max で正規化するため値のスケールには非依存。"""
        if not card_names:
            return {}
        cfs_fmt = CFS_FORMAT_MAP.get((fmt or "").lower())
        cfg = self.cfg
        if cfs_fmt:
            # EDH（シングルトン系）は物理分離テーブルを参照（構築の横断比較に混ぜない）
            table = ("edh_card_strength" if cfs_fmt in EDH_FORMAT_NAMES
                     else "card_format_strength")
            sql = f"""
                SELECT c.card_name,
                       cfs.play_decks::float / fdc.total_decks AS play_rate
                FROM {cfg['cards_table']} c
                JOIN {table} cfs ON cfs.card_id = c.id
                JOIN format_deck_counts fdc ON fdc.format_name = cfs.format_name
                WHERE c.card_name = ANY(%s) AND cfs.format_name = %s
            """
            params = (card_names, cfs_fmt)
        else:
            # 横断フォールバック＝本線 4F 合計（R11 の GT 機械採点と同じ土俵・上記注記）
            sql = f"""
                SELECT c.card_name, SUM(cfs.play_decks) AS play_decks
                FROM {cfg['cards_table']} c
                JOIN card_format_strength cfs ON cfs.card_id = c.id
                WHERE c.card_name = ANY(%s)
                  AND cfs.format_name = ANY(%s)
                GROUP BY c.card_name
            """
            params = (card_names, list(MAINLINE_FORMATS))
        try:
            return {r[0]: float(r[1]) for r in self.db.query(sql, params)}
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
            # EDH（シングルトン系）は物理分離テーブルを参照。同一フォーマット内は
            # 分母が共通なので play_decks の絶対数順＝採用率順（率換算は不要）
            table = ("edh_card_strength" if cfs_fmt in EDH_FORMAT_NAMES
                     else "card_format_strength")
            sql = f"""
                SELECT
                    c.card_name, c.type_line, c.oracle_text,
                    c.japanese_name, c.japanese_oracle_text,
                    c.mana_cost, c.rarity, c.tournament_score,
                    ROW_NUMBER() OVER (
                        ORDER BY cfs.play_decks DESC, c.id
                    ) AS rank
                FROM {cfg['cards_table']} c
                JOIN {table} cfs ON cfs.card_id = c.id
                WHERE cfs.format_name = %s
                  {fmt_sql} {type_sql} {attr_sql} {role_sql}
                ORDER BY cfs.play_decks DESC, c.id
                LIMIT {top_k * 3};
            """
            params: tuple = (cfs_fmt,)
        else:
            # 横断フォールバック＝本線 4F 合計（R11 と同じ土俵・CFS_FORMAT_MAP 上部の注記）
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
                WHERE cfs.format_name = ANY(%s)
                  {fmt_sql} {type_sql} {attr_sql} {role_sql}
                GROUP BY c.id
                ORDER BY SUM(cfs.play_decks) DESC, c.id
                LIMIT {top_k * 3};
            """
            params = (list(MAINLINE_FORMATS),)
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
        order = ("c.edhrec_rank ASC NULLS LAST, COALESCE(s.play_rate, 0) DESC, c.id"
                 if edh_order else
                 "COALESCE(s.play_rate, 0) DESC, c.edhrec_rank ASC NULLS LAST, c.id")
        sql = f"""
            SELECT
                c.card_name, c.type_line, c.oracle_text,
                c.japanese_name, c.japanese_oracle_text,
                c.mana_cost, c.rarity
            FROM {cfg['cards_table']} c
            LEFT JOIN (
                SELECT cfs.card_id, SUM(cfs.play_decks) AS play_rate
                FROM card_format_strength cfs
                WHERE cfs.format_name IN ('Standard', 'Pioneer', 'Modern', 'Legacy')
                GROUP BY cfs.card_id
            ) s ON s.card_id = c.id
            WHERE TRUE {fmt_sql} {type_sql} {attr_sql}
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

    def _removal_direct(self, spec: dict, top_k: int) -> list[CardResult]:
        """除去直行路（卒業レジストリのクエリ専用・2026-07-17 採用ゲート裁定）。
        WHERE＝役割 SQL＋フォーマット門＋機構門・並び＝レシピ固定
        （機能/フォーマット系=B: 採用率のみ／superlative=合成 β0.6）。
        意味検索・HyDE・RRF・reranker を使わない＝キーワード直行路と同族の
        「SQL に LIMIT を付けただけのもの」。詳細は removal_direct.py の正本参照。"""
        rows = removal_direct.fetch_direct(self.db, spec, top_k,
                                           cards_table=self.cfg['cards_table'])
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

    def _counter_direct(self, spec: dict, top_k: int) -> list[CardResult]:
        """確定カウンター直行路（卒業レジストリ・2026-07-19 採用ゲート裁定）。
        WHERE＝役割 SQL（target_types に spell）・並び＝レシピ T（確定段→採用率）。
        意味検索・HyDE・RRF・reranker を使わない。詳細は counter_direct.py の正本参照。"""
        rows = counter_direct.fetch_direct(self.db, spec, top_k,
                                           cards_table=self.cfg['cards_table'])
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
        type_filter_override: Optional[str] = None,
        cmc_min=None, cmc_max=None,
        power_min=None, power_max=None,
        toughness_min=None, toughness_max=None,
        mana_producer: bool = False,
        raw_query: Optional[str] = None,
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
        # 除去直行路（卒業レジストリ・2026-07-17）: 検証終了クエリは HyDE を重ねない
        # （直行の並びは決定的な上流信号＝意味の並べ替えで汚さない。kw_only/boost の
        #  分岐と同じ原則。search() 内の同じ門が SQL 直行まで面倒を見る）
        if removal_direct.removal_direct_gate(raw_query or query, format) is not None:
            return self.search(raw_query or query, top_k=top_k, format=format,
                               raw_query=raw_query)
        # 確定カウンター直行路も同様に HyDE を重ねない（2026-07-19）
        if counter_direct.counter_direct_gate(raw_query or query, format) is not None:
            return self.search(raw_query or query, top_k=top_k, format=format,
                               raw_query=raw_query)
        # 通常の検索結果を取得
        normal_results = self.search(
            query, top_k=top_k * 2, format=format,
            tournament_boost_override=tournament_boost_override,
            type_filter_override=type_filter_override,
            cmc_min=cmc_min, cmc_max=cmc_max,
            power_min=power_min, power_max=power_max,
            toughness_min=toughness_min, toughness_max=toughness_max,
            mana_producer=mana_producer,
            raw_query=raw_query,
        )
        if ABLATE_HYDE:
            # 切除実験（既定オフ）: HyDE 腕を重ねずに素の検索結果で返す
            return normal_results[:top_k]
        if getattr(self, "_last_route_direct", False) == 'dig_draw':
            # 構造化オンリー直行路が発火した＝並びは決定的な上流信号（play-rate /
            # edhrec 順）。removal/counter 直行路・kw_only・boost と同じ扱いで
            # HyDE を重ねない（2026-07-31・実測: 「ドローしながらフィルタリング
            # できるカード」で HyDE マージが思案 1,576 デッキを 4 位へ押し下げ、
            # Faerie Dreamthief〔9 デッキ〕を 1 位にしていた）
            return normal_results[:top_k]

        # HyDE ベクトル検索（hyde_text を embedding してベクトル検索）
        fmt_sql  = format_filter_sql(format)
        # 型は override が無ければ原文の末尾ルールで補完（2026-07-27・search() 本体と対）。
        # override だけを見ると、ルーターが型を落としたとき本体は補完で守られるのに
        # HyDE 単独ヒットだけ型なしで再流入する（Mage Hunters' Onslaught＝ソーサリーが
        # 「灯争大戦のプレインズウォーカー」に混入した実測＝R13 欠け〔7/21〕と同じ穴の型）
        _hyde_type = type_filter_override
        if _hyde_type is None:
            _stripped = (raw_query or query).strip().rstrip('?？。！!、．.　 ')
            for _jp, _en in TYPE_WORDS_JA.items():
                if _stripped.endswith(_jp):
                    _hyde_type = _en
                    break
        type_sql = type_filter_sql(_hyde_type)
        attr_sql = attr_filter_sql(cmc_min, cmc_max,
                                   power_min, power_max,
                                   toughness_min, toughness_max,
                                   mana_producer=mana_producer)
        # キーワード系クエリは HyDE 腕にも生得持ちハードフィルタを適用（search() 本体と対。
        # HyDE 単独ヒットは最終マージに入るため、ここに門が無いと非該当が再流入する）
        (_, _, _, _tb, _rm, _cm, _kw_abilities, _neg_kw,
         _kw_only) = extract_keywords(query)
        # 意図の原文補完（search() 本体と対・2026-07-30 mode 退役）
        if raw_query and raw_query != query:
            _rm_raw, _cm_raw = detect_intent_modes(raw_query)
            _rm = _rm or _rm_raw
            _cm = _cm or _cm_raw
        # 構造化オンリー直行路なら normal_results が既に SQL 直行の並び＝HyDE を重ねない
        # （重ねると意味検索の並びが play-rate 順を汚す・search() 本体の分岐と対）
        if _kw_only and not (_tb or tournament_boost_override) \
                and not _rm and not _cm:
            return normal_results[:top_k]
        # boost クエリ（「最強」「純粋に強い」等）も HyDE を重ねない（2026-07-15）。
        # 根拠: 公平 A/B（eval id=76/77・全行ラベル済み）で boost 5 クエリ全てが
        # HyDE 有害（−0.05〜−0.25）。このマージは normal 側を順位だけに潰すため、
        # search() 内の play-rate boost・役割減点が HyDE 単独ヒットに掛からず、
        # HyDE 文の言い回しに似た無実績カードが play-rate 順を埋める
        # （Closing Statement が Fatal Push と同点1位・Depower が減点素通りで5位）。
        # 「play-rate 順は上流信号・意味の並べ替えで汚さない」＝ reranker スキップ
        # （id=32）・直行路（上の分岐）と同じ原則の HyDE 版。
        if _tb or tournament_boost_override:
            return normal_results[:top_k]
        attr_sql += keyword_filter_sql(_kw_abilities, _neg_kw)
        # 機構指定つき除去クエリの門は HyDE 腕にも（search() 本体と対・単独ヒット再流入防止）
        attr_sql += removal_mech_filter_sql(query, _rm)
        # dig ∧ draw の門も HyDE 腕に（search() 本体と対・単独ヒット再流入防止。
        # 2026-07-27 の型フィルタ漏れ〔Mage Hunters' Onslaught 混入〕と同じ穴の予防）
        if dig_draw_gate(raw_query or query):
            attr_sql += dig_draw_filter_sql()
        # P/T 関係・部族・カード名・型否定ゲートも HyDE 腕に（search() 本体と対・
        # 決定的ゲートはルーターの写しでなく原文 gate_q を見る＝search() と同じ理由）
        gate_q = raw_query or query
        attr_sql += pt_relation_sql(detect_pt_relation(gate_q))
        attr_sql += tribal_filter_sql(detect_tribal(gate_q))
        attr_sql += name_contains_sql(detect_name_search(gate_q))
        attr_sql += neg_type_filter_sql(detect_neg_type(gate_q))
        # 収録セットゲートも HyDE 腕に（search() 本体と対・2026-07-27 同日配線）
        attr_sql += set_filter_sql(detect_set_ja(gate_q))
        # ドロー枚数ゲートも HyDE 腕に（search() 本体と対・単独ヒット再流入防止・R14）
        attr_sql += draw_filter_sql(detect_draw_min(gate_q))
        # EDH 固有色・ブラケットゲートも HyDE 腕に（search() 本体と対）。
        # 2026-07-21 追補: このリストに R13 だけ欠けており、HyDE 単独ヒットが
        # 固有色ゲートを素通りしていた（統率者名ゲートの e2e で栄誉=W が
        # ⊆{B,G,U} に混入して発覚＝R13 制定時からの穴・「残差は穴の検出器」の実例）
        _ci = detect_color_identity(gate_q)
        if _ci is None:
            _cmd = detect_commander_identity(gate_q, self._commander_index)
            if _cmd is not None:
                _ci = _cmd[0]
        _bracket = detect_bracket(gate_q)
        attr_sql += color_identity_filter_sql(_ci)
        attr_sql += edh_gate_sql(_ci is not None or _bracket is not None, _bracket)
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
        type_filter_override: Optional[str] = None,
        cmc_min=None, cmc_max=None,
        power_min=None, power_max=None,
        toughness_min=None, toughness_max=None,
        mana_producer: bool = False,
        raw_query: Optional[str] = None,
    ) -> list[CardResult]:
        print(f"\n[{self.model_key}] 検索: 「{query}」"
              + (f" [{format}]" if format else ""))
        t0 = time.perf_counter()
        self._last_route_direct = None    # 直行路の記録（search_with_hyde が読む）
        # 決定的ゲート（P/T・部族・カード名・型否定・EDH 色/ブラケット）はルーターの
        # 写し（search_query）でなく原文を見る。ルーターが写し間違えると門が不発になる
        # ため（実測: 7B が「非クリーチャー」→「非クリーチタ」と化かして型否定ゲートが
        # 素通り・2026-07-13）。raw_query 未指定（eval キャッシュ経路等）は従来どおり。
        gate_q = raw_query or query

        # 除去直行路（卒業レジストリ完全一致のみ・2026-07-17）: 検証終了クエリは
        # 意味検索もソフト層も通さず SQL 直行で確定。format 引数がレジストリの検証
        # 条件と食い違うときは発動しない（gate 側で不発＝ハイブリッドへ・安全側）
        rd_spec = removal_direct.removal_direct_gate(gate_q, format)
        if rd_spec is not None:
            print(f"  除去直行路（検証終了クエリ・レシピ={rd_spec['recipe']}・"
                  "意味検索スキップ）")
            return self._removal_direct(rd_spec, top_k)

        # 確定カウンター直行路（卒業レジストリ完全一致のみ・2026-07-19）: 同型の
        # 検証終了クエリ。レシピ T（確定段→採用率）で SQL 直行。
        cd_spec = counter_direct.counter_direct_gate(gate_q, format)
        if cd_spec is not None:
            print("  確定カウンター直行路（検証終了クエリ・意味検索スキップ）")
            return self._counter_direct(cd_spec, top_k)

        (en_kws, ja_kws, type_filter, tournament_boost,
         removal_mode, counter_mode, kw_abilities, neg_kw_abilities,
         kw_only) = extract_keywords(query)

        # 型肯定の末尾ルールを原文（gate_q）でも補完する（2026-07-27・故障②の修理）:
        # extract_keywords は書き換え後クエリしか見ないため、ルーターが search_query を
        # 壊すと型が落ちる（「灯争大戦のプレインズウォーカー」→ 7B が
        # 「灯争大战的 planeswalker」へ簡体字化けさせ日本語末尾ルールが不発だった実測）。
        # P/T・部族・カード名・型否定は 7/13 に raw_query 化済み＝肯定だけ漏れていた
        if type_filter is None and not type_filter_override and gate_q != query:
            _stripped = gate_q.strip().rstrip('?？。！!、．.　 ')
            for _jp, _en in TYPE_WORDS_JA.items():
                if _stripped.endswith(_jp):
                    type_filter = _en
                    print(f"  type_filter: {_en}（原文の末尾ルールで補完）")
                    break

        # override フラグが True の場合は強制的に有効化。
        # removal/counter の override は 2026-07-30 に退役（本人裁定「mode はどこにも
        # 要らない」）: LLM ルーターの自己申告 bit を信じる経路は 5/31 の手書き規則
        # ファイル（7/06 の構造化置換で埋葬済み・コミット 882646e）の消し忘れで、
        # 幻出の入口だった（実測: 「使われて嫌な気分になるカード」に removal_mode）。
        # 除去/カウンター意図は extract_keywords の QUERY_EXPAND（決定的・部分一致）
        # だけが立てる＝部族/セット/P/T ゲートと同じ「検出と動作が同じ場所」の様式。
        # 意図も原文で補完する（型肯定の 7/27 補完と同じ対）: ルーターが写しを壊すと
        # 書き換え後クエリの辞書検出が不発になる（実測: 錨ウラモグ→「アトラクサ対策」）
        if gate_q != query:
            _rm_raw, _cm_raw = detect_intent_modes(gate_q)
            removal_mode = removal_mode or _rm_raw
            counter_mode = counter_mode or _cm_raw
        tournament_boost = tournament_boost or tournament_boost_override
        # type_filter_override が指定された場合は上書き。ただし**関所を通す**
        # （2026-07-31・型幻出の再演から）: LLM 由来の型申告は、原文にその型を意味する
        # 語（日本語 or 英語型名）が無ければ捨てる。同じガードは 7/12 に
        # mtg_rag_agent.rewrite_query 側へ置かれたが「eval キャッシュ経路はこの関数を
        # 通らないため影響なし」という前提つきだった——直行路が type_filter を WHERE に
        # 使うようになった時点でその前提が腐り、「ドローしながらフィルタリングできる
        # カード」に Creature が幻出して思案・定業・選択が門の外に落ちた（実測 0.601→0.122）。
        # **経路ごとにガードを貼る設計だから貼り忘れた経路で再演する**＝信頼境界は
        # searcher 側の一点に置く（mode 退役 7/30 の完成形・LLM の申告より決定器が上位）。
        if type_filter_override:
            _tf = str(type_filter_override)
            _ja = [jp for jp, en in TYPE_WORDS_JA.items() if en == _tf]
            if any(w in gate_q for w in _ja) or _tf.lower() in gate_q.lower():
                type_filter = type_filter_override
            else:
                print(f"  type_filter: {_tf} を破棄（原文に型語なし・幻出ガード）")
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
        pt_rel = detect_pt_relation(gate_q)
        attr_sql += pt_relation_sql(pt_rel)
        if pt_rel:
            print(f"  P/T関係ゲート: {pt_rel}（数値P/Tのみ・::int 比較）")
        # 部族（サブタイプ）ゲート（決定的辞書・全腕+直行路に掛かる）
        tribal = detect_tribal(gate_q)
        attr_sql += tribal_filter_sql(tribal)
        if tribal:
            print(f"  部族ゲート: {tribal}（type_line 単語境界照合）")
        # 収録セットゲート（2026-07-27・決定的辞書・原文を見る・全腕+直行路に掛かる）。
        # set_codes は全印刷の集合＝再録カードも初出セットで拾える
        set_codes_hit = detect_set_ja(gate_q)
        attr_sql += set_filter_sql(set_codes_hit)
        if set_codes_hit:
            print(f"  セットゲート: {set_codes_hit}（収録セット・全印刷）")
        # カード名部分一致（「カード名に X とつく」・決定的検出・全腕+直行路に掛かる）
        name_term = detect_name_search(gate_q)
        attr_sql += name_contains_sql(name_term)
        if name_term:
            print(f"  カード名ゲート: 「{name_term}」を含む（日英 LIKE）")
        # 型の否定ゲート（「非クリーチャーカード」等・決定的検出・全腕+直行路に掛かる）
        neg_type = detect_neg_type(gate_q)
        attr_sql += neg_type_filter_sql(neg_type)
        if neg_type:
            print(f"  型否定ゲート: NOT {neg_type}（face_types＝唱えられる面の型で判定）")
        # ドロー枚数ゲート（R14・決定的検出・全腕+直行路に掛かる）
        draw_min = detect_draw_min(gate_q)
        attr_sql += draw_filter_sql(draw_min)
        if draw_min:
            print(f"  ドロー枚数ゲート: draw_count >= {draw_min}"
                  "（命令形の実ドローのみ・可変 X は draw_x で通過・R14）")
        # EDH 固有色・ブラケットゲート（R13・決定的検出＝ルーター無改修で効く）。
        # attr_sql に足すことで全腕（vec/FTS/強度腕）と直行路に同時に掛かる
        ci = detect_color_identity(gate_q)
        cmd_names = None
        if ci is None:
            # 色語が無いときだけ統率者名から固有色を解決（明示の色語が常に優先）
            cmd = detect_commander_identity(gate_q, self._commander_index)
            if cmd is not None:
                ci, cmd_names = cmd
        bracket = detect_bracket(gate_q)
        edh_intent = ci is not None or bracket is not None
        attr_sql += color_identity_filter_sql(ci)
        attr_sql += edh_gate_sql(edh_intent, bracket)
        if ci is not None:
            label = ",".join(ci) if ci else "無色"
            src = f"統率者名 {'/'.join(cmd_names)} → " if cmd_names else ""
            print(f"  固有色ゲート: {src}⊆ {{{label}}}（banned 除外"
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
        # カード名検索も同様（name LIKE で完全定義・並びは play-rate/edhrec）
        name_direct = name_term is not None and not has_fuzzy_semantic(query)
        # 型の否定クエリも同様（face_types の NOT で完全定義・並びは play-rate/edhrec）
        neg_type_direct = neg_type is not None and not has_fuzzy_semantic(query)
        # マナ加速クエリは EDH 意図が無くても構造化で完全定義される
        # （is_mana_boost ∧ cmc/type/format の WHERE）。2026-07-26 追加。
        # 由来: 本人の指摘「構造化のど真ん中を合わせただけなのになぜ的中率が低い」
        # →診断で、門（is_mana_boost=TRUE）は正しく効いていたのに直行路が
        # edh_intent を要求していたため意味検索へ落ち、絞り込み後の並びに品質信号が
        # 無かったと判明（「1マナのマナクリーチャー」で極楽鳥・ラノワールのエルフが
        # top-10 圏外・NDCG 0.703）。除去 0.33→直行路と同型の処方……を試したが
        # **実測で棄却（2026-07-26・id=113）**。配線は切ってある（下記 False）。
        #
        # 棄却の根拠（id=111 対照 → id=113・同一 DB/同一キャッシュ・変更はコードのみ）:
        #   看板 0.929 → 0.873。per-query: 1マナのマナクリーチャー 0.703→0.502
        #   （judged でも 0.52）・マナ加速できるカード 0.937→0.176・パイオニアの
        #   マナ加速 0.948→0.220。
        # 機序（数字より重要）: **is_mana_boost の偽陽性を play-rate 順が増幅する**。
        #   「死亡時に宝物」「生け贄で一回マナ」型（Greedy Freebooter・Shambling
        #   Ghast・Wild Cantor）は、マナ目的でなく生け贄の種や色調整として構築で
        #   実際によく使われる＝採用率が高い → 並べ替えると優先的に浮上する。
        #   ＝R14 の「誘発条件文の中の行為は本業と数えない」の**マナ版が未実装**で
        #   あることが露呈した（PHASE2 §11 の横展開候補・除去の Bowmasters と同型）。
        # 併存する交絡: 未ラベル 80〜90%（GT がハイブリッドの出力だけで作られている
        #   ＝7/23 ドロー直行プロトと同じ被覆の崖）。数字はその分不当に低いが、
        #   顔ぶれに明らかな偽陽性が居るため「採点すれば戻る」とは言い切れない。
        # 再挑戦の条件: 先に is_mana_boost へ「付随・一回きりは本業と数えない」を
        #   実装し（列仕事）、その上で採点便を回してから再測定する。順序は
        #   「列 → 並べ方」であって逆ではない（この便の教訓）。
        # ゲート関数と試験（tests/test_mana_direct_gate.py・25 件全緑・誤発動ゼロ）は
        # 正しく動いているので残す＝再挑戦時に再発明しない。
        # 2026-07-26 夕: 再挑戦の条件（列の偽陽性除去）を enrich_mana.py 新設で
        # 満たしたため True に復帰。列は「本業か付随か」線（本人裁定①倍化=本業・
        # ②繰り返し誘発=おまけ・③他者依存報酬=おまけ）＋net-mana 定義（6/24）で
        # 再導出済み（錨+境界 41 枚全一致・差分 963 行）。上の棄却記録は
        # 「列を直す前に並べ方だけ直すと害」の教訓として残す。
        MANA_DIRECT_ENABLED = True
        mana_direct = MANA_DIRECT_ENABLED and mana_direct_gate(query, mana_producer)
        # 収録セットクエリも意味の残余が無ければ直行（「灯争大戦のプレインズウォーカー」
        # ＝ set_codes && ∧ 型 で正解集合が完全定義・並びは採用率順・2026-07-27）。
        # fuzzy 判定は原文（gate_q）＝セット検出と同じ土俵で行う
        set_direct = set_codes_hit is not None and not has_fuzzy_semantic(gate_q)
        # 「入れ替えてから引く」＝ dig ∧ draw の共通集合（2026-07-31 本人裁定）。
        # 判定は原文（gate_q）＝他の決定的ゲートと同じ土俵
        dig_draw_direct = dig_draw_gate(gate_q)
        if dig_draw_direct:
            attr_sql += dig_draw_filter_sql()
        if not (tournament_boost or removal_mode or counter_mode) and (kw_only or edh_direct or pt_direct or tribal_direct or name_direct or neg_type_direct or mana_direct or set_direct or dig_draw_direct):
            print("  構造化オンリー直行路（意味検索スキップ・"
                  + ("EDH＝edhrec順" if edh_intent else "play-rate順") + "）")
            # 直行路を通ったことを記録する（search_with_hyde が HyDE を重ねないため。
            # 2026-07-31・dig∧draw 便で発覚した適用漏れ: removal/counter 直行路と
            # kw_only/boost には早期リターンがあるのに、構造化オンリー直行路
            # （マナ/セット/EDH/P-T/部族/カード名/型否定/dig∧draw）には無く、
            # 決定的な play-rate 順の上に HyDE の意味的並べ替えが乗っていた
            # ＝「直行の並びは上流信号・意味で汚さない」原則の穴）
            # 新設経路（dig∧draw）だけを HyDE の重ねから守る。既存の構造化直行路
            # （マナ/セット/EDH/P-T/部族/カード名/型否定）も原則としては同じ扱いに
            # すべきだが、実測で EDH が 0.963→0.877（未ラベル 12%）＝**採点が
            # HyDE 込みの顔ぶれで作られている**ため、切り替えは採点便とセットの
            # 別便にする（本人裁定待ち・今日の便では既存挙動を一切変えない）
            self._last_route_direct = 'dig_draw' if dig_draw_direct else 'legacy'
            return self._structured_search(top_k, fmt_sql, type_sql, attr_sql,
                                           edh_order=edh_intent)

        if ABLATE_VECTOR:
            v_rows = []          # 切除実験（既定オフ）: 埋め込みを作らず腕を空にする
        else:
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
