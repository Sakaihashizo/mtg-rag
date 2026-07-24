#!/usr/bin/env python
"""test_draw_gate.py — ドロー枚数ゲート（R14・2026-07-23）の安全試験。

検証対象:
  (1) detect_draw_min() — 決定的検出が「発動すべきで発動し、発動すべきでない
      ところで誤発動しない」（test_negation_type_gate と同じ非対称設計:
      誤発動＝非ドロークエリに門を掛ける＝有害・必須ゼロ／
      取り逃し＝従来どおり意味検索に落ちるだけ＝無害）。
      本線 30 クエリ（eval_queries.json）＋ルーター写し（eval_router_cache の
      sq 実測形）を全数掃く。「手札補充」「フィルタリング」は対象外が正
      （GT 実測: 引かずに手札へ加える選別カードが正解に含まれる）。
  (2) parse_draw() — R14 錨カードの列値（DB 実測）。審査材料
      docs/me/draw_anchor_candidates_20260718.md の各類型から抽出。
  (3) ゲート SQL — draw_filter_sql の断片が実 DB で罠（Sheoldred 型誘発・
      Dredge 型置換）を弾き、本業（Brainstorm・Truce の up to）を通すこと。
純 Python + 読み取り SQL・LLM 不要・決定的。
"""
import json
import sys

sys.path.insert(0, '/mnt/mtg_rag/src')
import psycopg2

from db_config import get_db_config
from mtg_hybrid_search_v2 import detect_draw_min, draw_filter_sql

# (1) 検出: (クエリ, 期待 N or None)
DETECT_CASES = [
    # 本線ドロー族（原文）
    ("カードを2枚引く",                      2),
    ("draw two cards",                       2),
    ("インスタントでドローできるカード",     1),
    # ルーター写し（eval_router_cache の search_query 実測形＝eval 経路で見る文字列）
    ("インスタント ドロー",                  1),
    # 対象外が正: 選別系・補充系（GT 実測 2026-07-23）
    ("ドローしながらフィルタリングできるカード", None),
    ("ドロー フィルタリング カード",         None),
    ("手札補充できる青いカード",             None),
    ("手札補充 青いカード",                  None),
    # 表記ゆれ
    ("2枚引く",                              2),
    ("カードを二枚引く",                     2),
    ("３枚引くカード",                       3),
    ("draw a card",                          1),
    ("instant that draws three cards",       3),
    # 誤発動してはいけない近傍
    ("土地をサーチするカード",               None),
    ("墓地から回収するカード",               None),
    ("手札に戻すバウンス",                   None),
]

# 本線 30 クエリで発動してよいのはこの3つだけ（値も固定）
EXPECTED_MAINLINE = {
    "カードを2枚引く": 2,
    "draw two cards": 2,
    "インスタントでドローできるカード": 1,
}

# (2) 錨カードの列値: name -> (draw_count, draw_x)
ANCHOR_COLUMNS = {
    # 複数ドロー本業
    'Brainstorm': (3, None), 'Lórien Revealed': (3, None),
    'Winternight Stories': (3, None), 'Divination': (2, None),
    # 一枚（キャントリップ）とおまけ
    'Ponder': (1, None), "Mishra's Bauble": (1, None),
    'Boomerang Basics': (1, None),
    # ルーター/ランメイジ（字面 Draw N は数える＝R14）
    'Faithless Looting': (2, None), 'Seasoned Pyromancer': (2, None),
    'Three Steps Ahead': (2, None), 'Fear of Missing Out': (1, None),
    # 衝動（Draw テキスト無し＝対象外）
    'Chandra, Torch of Defiance': (None, None),
    'Glimpse the Impossible': (None, None),
    # 誘発（報酬側＝罠・数えない）
    'Sheoldred, the Apocalypse': (None, None),
    'Orcish Bowmasters': (None, None),
    # 置換: 消える向き（数えない）・倍化（数えない・ETB 命令だけ数える）
    'Stinkweed Imp': (None, None), 'Notion Thief': (None, None),
    'Thought Reflection': (None, None), 'Quantum Riddler': (1, None),
    # エンジン（命令形は数える＝列は機械的事実・grade は GT の仕事）
    'Sylvan Library': (2, None), 'Teferi, Hero of Dominaria': (1, None),
    "Proft's Eidetic Memory": (1, None),
    'Unholy Annex // Ritual Chamber': (1, None),
    # ホイール（each player draws＝自分も引く）
    'Burning Inquiry': (3, None),
    # 可変（X/that many/for each/上付き指数）
    "Blue Sun's Zenith": (None, True), "Sphinx's Revelation": (None, True),
    'Cut a Deal': (None, True), 'Mathemagics': (None, True),
    # up to（選べば引ける＝R14 モード裁定の同族）
    'Temporary Truce': (2, None), 'Trade Secrets': (4, None),
    # 大型固定値
    'Jace, Wielder of Mysteries': (7, None),
    'Jace, Memory Adept': (20, None),
}


def main():
    fails = []

    # (1) 検出ケース
    for q, exp in DETECT_CASES:
        got = detect_draw_min(q)
        if got != exp:
            fails.append(f"detect: {q!r} got={got} exp={exp}")

    # (1b) 本線 30 クエリ全数（誤発動ゼロ＋意図した発動のみ）
    with open('/mnt/mtg_rag/eval_queries.json') as f:
        mainline = [e['query'] for e in json.load(f)]
    for q in mainline:
        got = detect_draw_min(q)
        exp = EXPECTED_MAINLINE.get(q)
        if got != exp:
            fails.append(f"mainline: {q!r} got={got} exp={exp}")

    # (2) 錨カードの列値（enrich_draw.py の populate 結果を実測）
    conn = psycopg2.connect(**get_db_config())
    cur = conn.cursor()
    cur.execute("SELECT card_name, draw_count, draw_x FROM mtg_cards_v2"
                " WHERE card_name = ANY(%s)", (list(ANCHOR_COLUMNS),))
    got_cols = {n: (d, x) for n, d, x in cur.fetchall()}
    for name, exp in ANCHOR_COLUMNS.items():
        got = got_cols.get(name, '(DB に無し)')
        if got != exp:
            fails.append(f"column: {name} got={got} exp={exp}")

    # (3) ゲート SQL 断片の実効（N=2）: 罠は落ち・本業は通る
    frag = draw_filter_sql(2)
    probe = ['Brainstorm', 'Temporary Truce', 'Sheoldred, the Apocalypse',
             'Stinkweed Imp', "Blue Sun's Zenith"]
    cur.execute("SELECT card_name FROM mtg_cards_v2 c"
                " WHERE card_name = ANY(%s)" + frag, (probe,))
    passed = {r[0] for r in cur.fetchall()}
    for want in ('Brainstorm', 'Temporary Truce', "Blue Sun's Zenith"):
        if want not in passed:
            fails.append(f"sql: {want} が N=2 門を通らない")
    for trap in ('Sheoldred, the Apocalypse', 'Stinkweed Imp'):
        if trap in passed:
            fails.append(f"sql: 罠 {trap} が N=2 門を通過")
    conn.close()

    n_total = len(DETECT_CASES) + len(mainline) + len(ANCHOR_COLUMNS) + 5
    if fails:
        print(f"FAIL {len(fails)} 件 / 検証 {n_total} 件:")
        for f_ in fails:
            print("  " + f_)
        sys.exit(1)
    print(f"OK: 全 {n_total} 件（検出 {len(DETECT_CASES)}＋本線 {len(mainline)}"
          f"＋錨列 {len(ANCHOR_COLUMNS)}＋SQL 5）誤発動ゼロ・錨全一致")


if __name__ == "__main__":
    main()
