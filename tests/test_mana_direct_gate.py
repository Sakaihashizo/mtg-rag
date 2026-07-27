#!/usr/bin/env python
"""test_mana_direct_gate.py — マナ加速の構造化オンリー直行路の安全試験（2026-07-26 新設）。

mana_direct_gate(query, mana_producer) が「発動すべきでない場面で誤発動しない」ことを
検証する。純 Python・LLM 不要・DB 不要・決定的。

ゲートの失敗の向き（既存ゲート試験と同じ非対称）:
  誤発動（false fire）= 意味の残余を落として間違った集合を返す ＝ 有害
  取り逃し（no fire） = ハイブリッドに落ちるだけ ＝ 無害（遅く・正しく）
よって誤発動ゼロを必須とし、取り逃しは記録のみ。

背景: 本人の指摘「構造的に判別できる定義と1マナと構造化のど真ん中を合わせただけ
なのに、なぜ的中率が低いんだ？」→ 門（is_mana_boost）は正しかったが直行路が
edh_intent を要求していて意味検索に落ち、並びに品質信号が無かった（Birds of
Paradise・Llanowar Elves が top-10 圏外）。
"""
import sys

sys.path.insert(0, '/mnt/mtg_rag/src')
from mtg_hybrid_search_v2 import mana_direct_gate, extract_keywords

# (クエリ, ルーターの mana_producer, ゲートが発動してよいか)
CASES = [
    # ─ 発動すべき: 意味の残余が構造化フラグだけ（eval の実クエリ 3 本を含む）─
    ("マナ加速できるカード", True, True),
    ("パイオニアのマナ加速", True, True),
    ("1マナのマナクリーチャー", True, True),
    ("2マナのマナ加速", True, True),
    ("緑のマナ加速", True, True),
    ("マナを加えるクリーチャー", True, True),

    # ─ mana_producer が立たない場面は常に不発（ゲートの前提条件）─
    ("マナ加速できるカード", False, False),
    ("1マナのマナクリーチャー", False, False),
    ("飛行を持つクリーチャー", False, False),
    ("単体除去", False, False),

    # ─ 土地サーチ系: is_mana_boost の net-mana 定義と一致しない＝直行させない。
    #   辞書側で「ランプ」「土地加速」を意図的に mana_struct に tag していないので
    #   has_fuzzy_semantic が fuzzy と判定して門前払いする（二重の守り）─
    ("ランプできるカード", True, False),
    ("土地加速できるカード", True, False),
    ("ランプするクリーチャー", True, False),
    ("緑の土地加速", True, False),

    # ─ 意味の残余があるので直行させない（fuzzy 語との併記）─
    ("マナ加速しながらドローできるカード", True, False),
    ("マナ加速できるトークン生成カード", True, False),
    ("マナ加速もできるバウンス呪文", True, False),
    # コンボ語は QUERY_EXPAND に見出しが無く has_fuzzy_semantic が素通りさせるため、
    # mana_direct_gate の局所ガードで塞いでいる（2026-07-26・この試験が捕まえた穴）
    ("マナ加速できるコンボパーツ", True, False),
    ("マナ加速できるコンボ", True, False),
    ("mana ramp for combo", True, False),

    # ─ 役割意図つき（removal_mode / counter_mode / tournament_boost）─
    #   ゲート単体では発動するが、search() 側の
    #   `not (tournament_boost or removal_mode or counter_mode)` が最終防衛線で
    #   実際には直行しない。二段構えであることを試験の記録として残す。
    ("マナ加速できる除去", True, True),
    ("マナ加速を打ち消すカウンター", True, True),
    ("マナ加速できるカードを破壊する除去", True, True),
    ("最強のマナ加速", True, True),
    ("環境で強いマナクリーチャー", True, True),
]

# 「ゲート単体では True だが search() の役割/boost ガードで実際には直行しない」組。
DOWNSTREAM_GUARDED = {
    "マナ加速できる除去", "マナ加速を打ち消すカウンター",
    "マナ加速できるカードを破壊する除去",
    "最強のマナ加速", "環境で強いマナクリーチャー",
}


def main():
    false_fire, missed, ok = [], [], 0
    for query, mp, should_fire in CASES:
        fired = mana_direct_gate(query, mp)
        note = "  ※下流ガードで最終的に不発" if query in DOWNSTREAM_GUARDED else ""
        if fired == should_fire:
            ok += 1
            print(f"  OK   {'発動' if fired else '不発'}  mp={str(mp):<5} {query}{note}")
        elif fired and not should_fire:
            false_fire.append(query)
            print(f"  ** 誤発動（有害・要修正） mp={mp} {query}")
        else:
            missed.append(query)
            print(f"  -- 取り逃し（無害・ハイブリッド行き） mp={mp} {query}")

    print(f"\n合計 {len(CASES)} 件: 期待どおり {ok}"
          f" / 誤発動 {len(false_fire)} / 取り逃し {len(missed)}")

    # 二段構えの検証: 「下流ガードで止まる」を注記でなく実測で確かめる。
    # search() の条件は not (tournament_boost or removal_mode or counter_mode)。
    print("\n--- 下流ガードの実測（search() の役割/boost 条件が立つか）---")
    guard_fail = []
    for query in sorted(DOWNSTREAM_GUARDED):
        k = extract_keywords(query)
        tb, rm, cm = k[3], k[4], k[5]
        blocked = bool(tb or rm or cm)
        flags = f"boost={tb} removal={rm} counter={cm}"
        if blocked:
            print(f"  OK   下流で不発  {flags}  {query}")
        else:
            guard_fail.append(query)
            print(f"  ** 下流ガードも立たない（直行してしまう） {flags}  {query}")

    if false_fire or guard_fail:
        print("\n誤発動あり＝直行路が間違った集合を返す。has_fuzzy_semantic の辞書か"
              " mana_direct_gate のガードを修正すること。")
        sys.exit(1)
    print("\n誤発動ゼロ: ゲートは安全（取り逃しはハイブリッド経路が受ける）。")


if __name__ == '__main__':
    main()
