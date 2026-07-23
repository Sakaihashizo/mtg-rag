#!/usr/bin/env python
"""test_scope_gate.py — 守備範囲外検出（正直な不発）の安全試験（2026-07-20）。

detect_out_of_scope()（＝相性/コンボ/ヴォーソス等の未対応クエリ族を検出し、
検索は変えずに正直な注記を載せる判定）の両方向を検証する。純 Python・DB/LLM 不要。

非対称性: 誤発動＝守備範囲内のクエリに「守備範囲外」と言う（信用毀損・有害）／
取り逃し＝境界プローブの「自信満々に間違える」が残る（docs/me/boundary_probe_20260720.md）。
設計: ラベルのみ＝検索結果は不変＝eval 無風。「コンボに使えるカード」（正準クエリ）にも
注記が付くのは意図どおり（正準の 0.60 は単カード構造化の限界の標識＝正直に言う側）。
"""
import sys

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_rag_agent import detect_out_of_scope

# (クエリ, 期待 family / None)
CASES = [
    # ─ 発動すべき（境界プローブの 3 族＋近縁）─
    ("ムルドローサと相性がいいカード",                       "synergy"),
    ("シナジーのあるカード",                                 "synergy"),
    ("セドルーにいれられるコンボ",                           "combo"),
    ("ゼドルーにいれられるコンボ",                           "combo"),
    ("コンボに使えるカード",                                 "combo"),   # 正準クエリ＝ラベルのみで検索不変
    ("サーボタヴォークとヴォーソス的に相性がいいカード",     "vorthos"), # 相性も含むが特定側が勝つ
    ("フレーバーがいいカード",                               "vorthos"),
    # ─ 不発すべき（守備範囲内・誤発動は信用毀損）─
    ("アトラクサで使える単体除去",                           None),
    ("破壊除去",                                             None),
    ("確定カウンター呪文",                                   None),
    ("飛行を持つクリーチャー",                               None),
    ("モダンの最強カウンター呪文",                           None),
    ("ゴルガリカラーのマナ加速",                             None),
    ("カードを2枚引く",                                      None),
    ("コンバット・トリックに使えるカード",                   None),      # 「コンボ」を含まない字面の近縁
]


def main():
    bad, ok = [], 0
    for query, expected in CASES:
        got = detect_out_of_scope(query)
        got_key = got[0] if got else None
        if got_key == expected:
            ok += 1
            label = got_key or "不発"
            print(f"  OK   {label:10s} {query}")
        else:
            bad.append((query, expected, got_key))
            print(f"  NG   期待={expected!r} 実際={got_key!r}  {query}")

    # 注記文の検収: 全家族が「守備範囲外」を明言し、結果を隠さない旨を言う
    from mtg_rag_agent import OUT_OF_SCOPE_FAMILIES
    for _, key, msg in OUT_OF_SCOPE_FAMILIES:
        assert "守備範囲外" in msg, f"{key} の注記が守備範囲外を明言していない"
        assert "通常のカード検索の結果" in msg, f"{key} の注記が結果の素性を説明していない"

    print(f"\n{ok}/{len(CASES)} ケース通過・注記文検収 {len(OUT_OF_SCOPE_FAMILIES)} 家族通過")
    if bad:
        sys.exit(1)
    print("全緑")


if __name__ == "__main__":
    main()
