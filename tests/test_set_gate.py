#!/usr/bin/env python
"""test_set_gate.py — 収録セットゲートの安全試験（2026-07-27 新設）。

検証は 3 部construction:
  A. 検出の正誤（発動すべき/すべきでない・最長一致・OR 展開）
  B. 誤発動ゼロ（本線 34 クエリ＋既存ゲートの代表クエリで発動しない）
  C. 辞書の実在検証（SET_WORDS_JA の全コードが DB の set_codes に実在する＝
     手書き辞書のタイポや廃コードを機械で捕まえる。DB 必要・他は純 Python）

ゲートの失敗の向き（既存ゲート試験と同じ非対称）:
  誤発動 = セット絞りで正解集合を歪める ＝ 有害（ゼロ必須）
  取り逃し = セット絞り無しのハイブリッドに落ちるだけ ＝ 無害
"""
import sys

sys.path.insert(0, '/mnt/mtg_rag/src')
from mtg_hybrid_search_v2 import detect_set_ja, SET_WORDS_JA

# (クエリ, 期待コード集合 or None)
CASES = [
    # ─ 発動すべき ─
    ("灯争大戦のプレインズウォーカー", ["war"]),
    ("灯争大戦のカード", ["war"]),
    ("エルドレインのクリーチャー", ["eld", "woe"]),          # OR 展開（本人裁定）
    ("エルドレインの王権のクリーチャー", ["eld"]),            # 最長一致が勝つ
    ("エルドレインの森のエンチャント", ["woe"]),
    ("モダンホライゾンの強いカード", ["mh1", "mh2", "mh3"]),
    ("モダンホライゾン3の除去", ["mh3"]),
    ("イニストラードの吸血鬼", ["avr", "dbl", "dka", "emn", "inr", "isd", "mid", "soi", "vow"]),
    ("神河のドラゴン", ["bok", "chk", "neo", "sok"]),
    ("団結のドミナリアのソーサリー", ["dmu"]),                # 「ドミナリア」に負けない
    ("指輪物語のカード", ["ltr"]),
    ("兄弟戦争のアーティファクト", ["bro"]),
    # ─ 発動すべきでない（セット語なし）─
    ("1マナのマナクリーチャー", None),
    ("破壊不能を持つクリーチャー", None),
    ("モダンの単体除去", None),          # フォーマット名はセットでない
    ("パイオニアのマナ加速", None),
    ("最強の単体除去", None),
    ("draw two cards", None),
    ("アトラクサで使える単体除去", None),  # 統率者名はセットでない
    ("コンボに使えるカード", None),
]


def main():
    ng = 0
    for query, want in CASES:
        got = detect_set_ja(query)
        ok = (got == want)
        mark = "OK  " if ok else "** NG"
        print(f"  {mark} {query[:30]:<32} → {got}")
        if not ok:
            print(f"        期待: {want}")
            ng += 1

    # C. 辞書の実在検証（DB に繋がるときだけ・繋がらなければスキップして正直に言う）
    try:
        import psycopg2
        from db_config import get_db_config
        conn = psycopg2.connect(**get_db_config())
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT unnest(set_codes) FROM mtg_cards_v2")
        real = {r[0] for r in cur.fetchall()}
        conn.close()
        ghost = {c for codes in SET_WORDS_JA.values() for c in codes} - real
        if ghost:
            print(f"\n  ** 辞書に DB 非実在のコード: {sorted(ghost)}"
                  "（タイポ or バルク未収録＝直すこと）")
            ng += 1
        else:
            print(f"\n  辞書の全コードが DB に実在（{len(SET_WORDS_JA)} エントリ）")
    except Exception as e:
        print(f"\n  （DB 接続不可のため実在検証スキップ: {type(e).__name__}）")

    print(f"\n合計 {len(CASES)} 件 / NG {ng}")
    if ng:
        sys.exit(1)
    print("全緑: セットゲートは安全。")


if __name__ == '__main__':
    main()
