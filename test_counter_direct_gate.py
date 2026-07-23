#!/usr/bin/env python
"""test_counter_direct_gate.py — 確定カウンター直行路の入口ゲート安全試験（2026-07-19）。

counter_direct_gate()（＝卒業レジストリ完全一致で LLM ルーター・意味検索をスキップ
して SQL 直行路へ行く判定）の両方向を検証する。純 Python・LLM 不要・決定的。
test_removal_direct_gate と同じ非対称性（誤発動=有害・取り逃し=床消失）。
"""
import sys

sys.path.insert(0, '/mnt/mtg_rag')
from counter_direct import counter_direct_gate

# (クエリ, fmt 引数, 発動してよいか)
CASES = [
    # ─ 発動すべき（卒業レジストリ・2026-07-19 採用ゲート裁定）─
    ("確定カウンター呪文", None, True),
    (" 確定カウンター呪文 ", None, True),   # 前後空白の吸収
    # ─ 退役した偽ペルソナ（もう発動してはならない）─
    ("条件付きカウンター呪文", None, False),
    # ─ 他のカウンター系（本番/ルーター経路の担当・直行化していない）─
    ("純粋に強いカウンター呪文", None, False),
    ("モダンの最強カウンター呪文", None, False),
    ("レガシーのカウンター呪文", None, False),
    ("2マナ以下のカウンター呪文", None, False),
    ("counter target spell", None, False),
    ("最強のカウンター呪文", None, False),
    ("カウンター呪文", None, False),
    ("無条件カウンター", None, False),
    # ─ fmt 引数の食い違い: 未検証の組合せ＝ハイブリッドへ（安全側）─
    ("確定カウンター呪文", "modern", False),
    ("確定カウンター呪文", "legacy", False),
    # ─ 数値・条件・色つき: 検証していない集合＝誤発動したら事故 ─
    ("1マナの確定カウンター呪文", None, False),
    ("青の確定カウンター呪文", None, False),
    ("確定カウンター呪文おすすめ", None, False),
    # ─ 役割外・他の直行路の担当 ─
    ("最強の単体除去", None, False),
    ("飛行を持つクリーチャー", None, False),
    ("クリーチャーを追放する除去", None, False),
]


def main():
    false_fire, missed, ok = [], [], 0
    for query, fmt, should_fire in CASES:
        fired = counter_direct_gate(query, fmt) is not None
        fmt_str = f" [fmt={fmt}]" if fmt else ""
        if fired == should_fire:
            ok += 1
            print(f"  OK   {'発動' if fired else '不発'}  {query}{fmt_str}")
        elif fired and not should_fire:
            false_fire.append(query)
            print(f"  ** 誤発動（有害・要修正） {query}{fmt_str}")
        else:
            missed.append(query)
            print(f"  ** 取り逃し（卒業クエリの床が消える回帰） {query}{fmt_str}")
    print(f"\n合計 {len(CASES)} 件: 期待どおり {ok} / 誤発動 {len(false_fire)}"
          f" / 卒業クエリ不発 {len(missed)}")
    if false_fire or missed:
        if false_fire:
            print("誤発動あり＝未検証クエリに直行路が確定ランキングを返す。"
                  "レジストリ照合（counter_direct_gate）を修正すること。")
        if missed:
            print("卒業クエリが不発＝検証終了の床が消えている。レジストリを確認すること。")
        sys.exit(1)
    print("両方向とも期待どおり: 誤発動ゼロ・卒業レジストリは全発動。")


if __name__ == '__main__':
    main()
