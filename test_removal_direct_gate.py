#!/usr/bin/env python
"""test_removal_direct_gate.py — 除去直行路の入口ゲート安全試験（2026-07-17）。

removal_direct_gate()（＝卒業レジストリ完全一致で LLM ルーター・意味検索を
スキップして SQL 直行路へ行く判定）の両方向を検証する。純 Python・LLM 不要・決定的。

ゲートの失敗の向き（test_structured_gate と同じ非対称性）:
  誤発動（false fire）= 未検証のクエリに検証済みの顔をした決定的ランキングを
                        返す ＝ 有害・ゼロ必須
  取り逃し（no fire） = ルーター経由のハイブリッドに落ちるだけ ＝ 本来は無害。
                        ただし卒業レジストリは閉集合（9 本・検証終了の床）なので、
                        レジストリのクエリが不発になるのは「床が消える」回帰＝
                        この試験ではこちらも失敗として扱う。
"""
import sys

sys.path.insert(0, '/mnt/mtg_rag')
from removal_direct import removal_direct_gate

# (クエリ, fmt 引数, 発動してよいか)
CASES = [
    # ─ 発動すべき（卒業レジストリ 9 本・2026-07-17 採用ゲート裁定）─
    ("クリーチャーを追放する除去", None, True),
    ("destroy target creature", None, True),
    ("モダンの単体除去", None, True),
    ("スタンダードの単体除去", None, True),
    ("パイオニアの単体除去", None, True),
    ("レガシーの単体除去", None, True),
    ("ヴィンテージの単体除去", None, True),
    ("パウパーの単体除去", None, True),
    ("最強の単体除去", None, True),
    # ─ fmt 引数がレジストリの検証条件と一致 → 発動（eval は GT の format を渡す）─
    ("モダンの単体除去", "modern", True),
    ("パウパーの単体除去", "pauper", True),
    # ─ 表記ゆれの吸収は空白と大小文字だけ ─
    (" 最強の単体除去 ", None, True),
    ("Destroy Target Creature", None, True),
    # ─ 本番残留（採用ゲート裁定の例外・最重要ケース）: 誤発動したら裁定違反 ─
    ("クリーチャーを破壊する除去", None, False),
    # ─ fmt 引数の食い違い: 未検証の組合せ＝ハイブリッドへ（安全側）─
    ("destroy target creature", "modern", False),
    ("モダンの単体除去", "legacy", False),
    ("最強の単体除去", "standard", False),
    # ─ 数値・条件・色・EDH つき: 検証していない集合＝誤発動したら事故 ─
    ("1マナの単体除去", None, False),
    ("2マナ以下の単体除去", None, False),
    ("黒の単体除去", None, False),
    ("白いクリーチャーを追放する除去", None, False),
    ("ゴルガリカラーの除去", None, False),
    ("条件付き単体除去", None, False),
    # ─ 近縁の言い換え・拡張: レジストリ外＝ルーターに譲る ─
    ("単体除去", None, False),
    ("単体除去のおすすめ", None, False),
    ("モダンの単体除去おすすめ", None, False),
    ("ヒストリックの単体除去", None, False),
    ("統率者の単体除去", None, False),
    ("最強の除去", None, False),
    ("最強の全体除去", None, False),
    ("全体除去", None, False),
    ("置物除去", None, False),
    ("クリーチャーを追放するカード", None, False),
    ("exile target creature", None, False),
    ("destroy target creature at instant speed", None, False),
    ("単体除去とドローができるカード", None, False),
    # ─ 役割外・他の直行路の担当 ─
    ("最強のカウンター呪文", None, False),
    ("飛行を持つクリーチャー", None, False),
]


def main():
    false_fire, missed, ok = [], [], 0
    for query, fmt, should_fire in CASES:
        fired = removal_direct_gate(query, fmt) is not None
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
                  "レジストリ照合（removal_direct_gate）を修正すること。")
        if missed:
            print("卒業クエリが不発＝検証終了の床が消えている。レジストリを確認すること。")
        sys.exit(1)
    print("両方向とも期待どおり: 誤発動ゼロ・卒業レジストリ 9 本は全発動。")


if __name__ == '__main__':
    main()
