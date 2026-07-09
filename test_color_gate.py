#!/usr/bin/env python
"""test_color_gate.py — EDH 固有色・ブラケット検出の安全試験（R13・2026-07-08）。

detect_color_identity() / detect_bracket()（＝固有色ハードゲートの決定的検出）が
「発動すべきでないクエリで誤発動しない」ことを検証する。純 Python・LLM 不要・決定的。

ゲートの失敗の向き（test_structured_gate.py と同じ非対称設計）:
  誤発動（false fire）= 固有色でないクエリに ⊆ ゲートを掛けて候補集合を歪める ＝ 有害
  取り逃し（no fire） = ゲートなしのハイブリッドに落ちるだけ ＝ 無害
よって誤発動ゼロを必須とし、取り逃しは記録のみ。
回帰ガード: 本線 30 クエリ（eval_queries.json）は全て不発であること
（色ゲートが正準 id=47 の物差しを乱さない証明）。
"""
import json
import sys

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_hybrid_search_v2 import detect_color_identity, detect_bracket

# (クエリ, 期待する色集合 or None, 期待するブラケット or None)
CASES = [
    # ─ 発動すべき: ギルド名 ─
    ("ゴルガリカラーの単体除去",            ['B', 'G'], None),
    ("イゼットカラーのドロー呪文",          ['R', 'U'], None),
    ("ラクドスカラーのマナ加速",            ['B', 'R'], None),
    ("ボロスカラーの装備品",                ['R', 'W'], None),
    ("セレズニアで使えるトークン戦略",      ['G', 'W'], None),
    # ─ 発動すべき: 断片・楔（3色） ─
    ("エスパーカラーで使えるカウンター呪文", ['B', 'U', 'W'], None),
    ("グリクシスで使える除去",              ['B', 'R', 'U'], None),
    ("ティムールカラーの続唱",              ['G', 'R', 'U'], None),
    ("アブザンのミッドレンジ向けカード",    ['B', 'G', 'W'], None),
    # ─ 発動すべき: 色文字の連なり・◯単 ─
    ("青黒で使える打ち消し呪文",            ['B', 'U'], None),
    ("白青黒で使えるコントロール札",        ['B', 'U', 'W'], None),
    ("緑単の到達を持つクリーチャー",        ['G'], None),
    ("白単の全体除去",                      ['W'], None),
    ("赤緑のビートダウン",                  ['G', 'R'], None),
    # ─ 発動すべき: 無色・英語・ブラケット ─
    ("無色のマナ加速",                      [], None),
    ("izzet draw spell",                    ['R', 'U'], None),
    ("temur ramp",                          ['G', 'R', 'U'], None),
    ("ブラケット2で使えるラクドスカラーのマナ加速", ['B', 'R'], 2),
    ("ブラケット4で使えるイゼットカラーのドロー呪文", ['R', 'U'], 4),
    ("ブラケット２のデッキに入るカード",    None, 2),  # 全角数字
    # ─ 誤発動したら事故: 単独の色文字＝colors（カードの色）族で固有色でない ─
    ("手札補充できる青いカード",            None, None),
    ("赤いドラゴン",                        None, None),
    ("緑のクリーチャー",                    None, None),
    # ─ 誤発動したら事故: 非指定の色表現 ─
    ("好きな色のマナを加えるカード",        None, None),
    ("色を選ぶカード",                      None, None),
    ("多色のクリーチャー",                  None, None),
    ("単色のカード",                        None, None),
    ("無色マナを加えるカード",              None, None),  # マナ種の話＝固有色でない
]


def main() -> int:
    false_fire = []
    wrong_set = []
    # 本線 30 クエリ＝全て不発であるべき（回帰ガード）
    try:
        with open('/mnt/mtg_rag/eval_queries.json', encoding='utf-8') as f:
            canonical = [(e['query'], None, None) for e in json.load(f)]
    except FileNotFoundError:
        canonical = []
        print("[warn] eval_queries.json が見つからない＝回帰ガードをスキップ")

    for query, want_ci, want_br in CASES + canonical:
        got_ci = detect_color_identity(query)
        got_br = detect_bracket(query)
        ci_ok = got_ci == want_ci
        br_ok = got_br == want_br
        mark = "OK " if (ci_ok and br_ok) else "NG "
        print(f"{mark} {query!r:44s} ci={got_ci} br={got_br}"
              + ("" if ci_ok and br_ok else f"  ← 期待 ci={want_ci} br={want_br}"))
        if not (ci_ok and br_ok):
            if want_ci is None and got_ci is not None:
                false_fire.append(query)   # 有害: 誤発動
            elif want_br is None and got_br is not None:
                false_fire.append(query)
            else:
                wrong_set.append(query)    # 有害: 発動したが色集合/番号が違う

    total = len(CASES) + len(canonical)
    bad = len(false_fire) + len(wrong_set)
    print(f"\n{total - bad}/{total} 合格"
          f"（誤発動 {len(false_fire)} / 色集合違い {len(wrong_set)}）")
    if false_fire:
        print("誤発動（ゼロ必須）:", false_fire)
    if wrong_set:
        print("色集合・ブラケット違い（ゼロ必須）:", wrong_set)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
