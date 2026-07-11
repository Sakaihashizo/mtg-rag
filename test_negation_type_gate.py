#!/usr/bin/env python
"""test_negation_type_gate.py — 否定キーワード検出＋日本語 type 語検出の安全試験（2026-07-11）。

extract_keywords() の2拡張（本人の実地テスト「速攻を持たないクリーチャー」
「速攻を持つアーティファクト」が発見した穴への対応）:
  (1) 否定形「〈kw〉を持たない／がない／無し／以外」→ neg_kw_abilities（SQL NOT 門。
      embedding は否定が原理的に見えない＝crisp な否定は構造化で解く）
  (2) クエリ末尾の type 語「〜アーティファクト」→ type_filter（末尾＝名詞句主要部。
      「アーティファクトを破壊する」の を格＝対象語では立てない）
が「発動すべきで発動し、発動すべきでないところで誤発動しない」ことを検証。
純 Python・LLM 不要・決定的。

失敗の向き（test_color_gate.py と同じ非対称設計）:
  誤発動 = 持つ側クエリに NOT を掛ける／対象語を type にする ＝ 有害（必須ゼロ）
  取り逃し = 従来どおりルーター/意味検索に落ちるだけ ＝ 無害（記録のみ）
回帰ガード: 本線 30 クエリ（eval_queries.json）で
  ・否定検出は全て不発（本線に否定形クエリは無い）
  ・type 検出の発火は EXPECTED_MAINLINE_TYPES と完全一致（意図した改善だけを通す）
"""
import json
import sys

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_hybrid_search_v2 import extract_keywords

# (クエリ, 期待 neg_kw_abilities, 期待 type_filter, 期待 kw_abilities)
CASES = [
    # ─ 否定: 発動すべき ─
    ("速攻を持たないクリーチャー",        ["Haste"],      "Creature", []),
    ("威迫を持たないクリーチャー",        ["Menace"],     "Creature", []),
    ("飛行を持たないクリーチャー",        ["Flying"],     "Creature", []),
    ("接死を持っていないクリーチャー",    ["Deathtouch"], "Creature", []),
    ("トランプル無しのクリーチャー",      ["Trample"],    "Creature", []),
    ("速攻以外のクリーチャー",            ["Haste"],      "Creature", []),
    ("警戒を持たないアーティファクト",    ["Vigilance"],  "Artifact", []),
    # ─ 否定＋肯定の複合（否定は NOT・肯定は @> が同時に立つ） ─
    ("速攻を持たない飛行クリーチャー",    ["Haste"],      "Creature", ["Flying"]),
    # ─ 肯定は従来どおり（否定の誤発動ゼロ） ─
    ("速攻を持つクリーチャー",            [], "Creature", ["Haste"]),
    ("速攻を持つアーティファクト",        [], "Artifact", ["Haste"]),
    ("破壊不能を持つクリーチャー",        [], "Creature", ["Indestructible"]),
    ("飛行を持つクリーチャー",            [], "Creature", ["Flying"]),  # 既存辞書エントリ優先
    # ─ type: 末尾ルールの不発（対象語・複合語・非 type 末尾） ─
    ("アーティファクトを破壊するカード",  [], None, []),
    ("クリーチャーを追放する除去",        [], None, []),   # removal ガードで kw も空のまま
    ("土地加速",                          [], None, []),
    ("クリーチャーを対象とする火力",      [], None, []),
    ("土地をサーチするカード",            [], None, []),
    # ─ 極性ガード維持（付与・除去意図では kw/neg とも降ろす） ─
    ("破壊不能を付与するカード",          [], None, []),
    ("速攻を持たせるエンチャント",        [], "Enchantment", []),  # 付与ガード・type は立つ
    ("破壊不能を除去できるカード",        [], None, []),
]

# 本線 30 クエリのうち、type 検出の発火を「意図した改善」として許可するもの。
# いずれも答えがクリーチャーであるべきクエリ（例:「速攻を持つクリーチャー」の
# top から機体〔Vehicle・非クリーチャー〕が消える方向）。ここに無い発火は回帰違反。
EXPECTED_MAINLINE_TYPES = {
    "飛行を持つクリーチャー":     "Creature",  # 従来から（辞書エントリ）
    "トランプルを持つクリーチャー": "Creature",  # 新規（末尾検出）
    "接死を持つクリーチャー":     "Creature",  # 新規
    "速攻を持つクリーチャー":     "Creature",  # 新規
    "破壊不能を持つクリーチャー": "Creature",  # 新規
    "1マナのマナクリーチャー":    "Creature",  # 新規
    "環境で強いクリーチャー":     "Creature",  # 新規（eval 経路はキャッシュ override 同値）
}


def main() -> int:
    failures = []

    # 1) ユニット: 発動・不発の両方向
    for query, want_neg, want_type, want_kw in CASES:
        (_, _, type_f, _tb, _rm, _cm, kw, neg, _kw_only) = extract_keywords(query)
        if neg != want_neg:
            failures.append(f"[neg] {query!r}: got {neg}, want {want_neg}")
        if type_f != want_type:
            failures.append(f"[type] {query!r}: got {type_f!r}, want {want_type!r}")
        if kw != want_kw:
            failures.append(f"[kw] {query!r}: got {kw}, want {want_kw}")

    # 2) 本線 30 クエリの回帰
    with open("/mnt/mtg_rag/eval_queries.json", encoding="utf-8") as f:
        mainline = [e["query"] for e in json.load(f)]
    for q in mainline:
        (_, _, type_f, _tb, _rm, _cm, _kw, neg, _kw_only) = extract_keywords(q)
        if neg:
            failures.append(f"[本線回帰: 否定誤発動] {q!r}: {neg}")
        want = EXPECTED_MAINLINE_TYPES.get(q)
        if type_f != want:
            failures.append(f"[本線回帰: type] {q!r}: got {type_f!r}, want {want!r}")

    print(f"CASES {len(CASES)} 本 + 本線 {len(mainline)} 本")
    if failures:
        print(f"\nFAIL {len(failures)} 件:")
        for x in failures:
            print("  " + x)
        return 1
    print("ALL PASS（誤発動ゼロ・期待発火一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
