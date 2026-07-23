#!/usr/bin/env python
"""test_commander_gate.py — 統率者名→固有色ゲートの安全試験（2026-07-20）。

detect_commander_identity()（＝クエリ中の統率者名から固有色を解決し R13 の
⊆ ゲートへ流す判定）の両方向を検証する。純 Python・DB/LLM 不要・決定的。
test_removal_direct_gate / test_counter_direct_gate と同じ非対称性
（誤発動=有害〔最悪形: ウラモグ ⊆∅ で無色カードしか出ない〕・取り逃し=名前無視）。

設計の正本: docs/me/edh_query_design_20260720.md §1。
- 既定 ON（構築文脈の推定無罪）＋敵対語彙で名前ごとに不発
- 同名色割れ（オムナス）不発・複数名食い違い不発・最長一致（イクセル＞アトラクサ）
"""
import sys

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_hybrid_search_v2 import build_commander_index, detect_commander_identity


class FakeDB:
    """build_commander_index が読む形（japanese_name, card_name, color_identity）"""
    ROWS = [
        ("法務官の声、アトラクサ",     "Atraxa, Praetors' Voice",     ['B', 'G', 'U', 'W']),
        ("偉大なる統一者、アトラクサ", "Atraxa, Grand Unifier",       ['B', 'G', 'U', 'W']),
        ("アトラクサの後継、イクセル", "Ixhel, Scion of Atraxa",      ['B', 'G', 'W']),
        ("絶え間ない飢餓、ウラモグ",   "Ulamog, the Ceaseless Hunger", []),
        ("無限に廻るもの、ウラモグ",   "Ulamog, the Infinite Gyre",    []),
        ("寛大なるゼドルー",           "Zedruu the Greathearted",     ['R', 'U', 'W']),
        ("怒りの座、オムナス",         "Omnath, Locus of Rage",       ['G', 'R']),
        ("マナの座、オムナス",         "Omnath, Locus of Mana",       ['G']),
        ("ネル・トースの族長、メレン", "Meren of Clan Nel Toth",      ['B', 'G']),
        ("称号持ち、アン",             "Ann of the Title",            ['W']),  # 2文字短縮＝索引外
    ]

    def query(self, sql, params=None):
        return self.ROWS


INDEX = build_commander_index(FakeDB())

# (クエリ, 期待固有色 "BGUW" 形式 / 無色は "" / 不発は None)
CASES = [
    # ─ 発動すべき（構築文脈・既定 ON）─
    ("アトラクサで使える単体除去",                       "BGUW"),
    ("アトラクサ 除去",                                  "BGUW"),  # 裸の名前＝既定 ON の検収
    ("法務官の声、アトラクサで使える除去",               "BGUW"),  # 正式名
    ("アトラクサを使ったデッキに入るカウンター",         "BGUW"),  # を格でも構築文脈
    ("ゼドルーで使えるドロー",                           "RUW"),   # 無読点名の末尾カタカナ
    ("寛大なるゼドルーにいれられるコンボ",               "RUW"),
    ("メレンのデッキに入る除去",                         "BG"),
    ("Atraxa, Grand Unifierで使える除去",                "BGUW"),  # 英語正式名
    ("ウラモグで使えるカード",                           ""),      # 同名同色（∅×2）＝無色デッキは正当
    # ─ 錨クエリ第1号（2026-07-20 本人発案・名前ごとスコープの検収）─
    ("アトラクサを使ったデッキで相手のウラモグに対応できるカード", "BGUW"),
    # ─ 最長一致（アトラクサの後継、イクセル ≠ アトラクサ）─
    ("アトラクサの後継、イクセルで使える除去",           "BGW"),
    # ─ 不発すべき: 敵対文脈（誤発動の最悪形＝ウラモグ ⊆∅ 事故の防波堤）─
    ("ウラモグ対策",                                     None),
    ("ウラモグを倒せるカード",                           None),
    ("ウラモグに対応できるカード",                       None),
    ("相手のアトラクサに対応できる除去",                 None),
    ("敵のメレンを除去したい",                           None),
    # ─ 不発すべき: 色割れ・食い違い ─
    ("オムナスで使える除去",                             None),    # 同名色割れ
    ("アトラクサとメレンのデッキ",                       None),    # 複数名で食い違い
    # ─ 不発すべき: 名前なし・索引外 ─
    ("モダンの単体除去",                                 None),
    ("飛行を持つクリーチャー",                           None),
    ("アンコモンの除去",                                 None),    # 2文字短縮名は索引外
    ("確定カウンター呪文",                               None),
]


def main():
    # 索引構築の検収
    assert "アトラクサ" in INDEX, "読点短縮名が索引にない"
    assert "ゼドルー" in INDEX, "無読点名の末尾カタカナが索引にない"
    assert "アン" not in INDEX, "2文字短縮名が索引に載っている（一般語衝突の危険）"
    assert len(INDEX["アトラクサ"]) == 1, "同名同色が 1 候補に畳まれていない"
    assert len(INDEX["オムナス"]) == 2, "色割れが検出できる形になっていない"

    bad, ok = [], 0
    for query, expected in CASES:
        got = detect_commander_identity(query, INDEX)
        got_ci = "".join(got[0]) if got is not None else None
        exp_ci = expected
        if got_ci == exp_ci:
            ok += 1
            label = f"⊆{{{got_ci or '無色'}}}" if got is not None else "不発"
            print(f"  OK   {label:14s} {query}")
        else:
            bad.append((query, exp_ci, got_ci))
            print(f"  NG   期待={exp_ci!r} 実際={got_ci!r}  {query}")

    print(f"\n{ok}/{len(CASES)} ケース通過・索引検収 5 件通過")
    if bad:
        print("失敗あり:")
        for q, e, g in bad:
            print(f"  {q}: 期待 {e!r} / 実際 {g!r}")
        sys.exit(1)
    print("全緑")


if __name__ == "__main__":
    main()
