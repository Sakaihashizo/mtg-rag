"""
fix_deck_links.py — deck_cards.card_id を正規化マッチで埋める（非破壊）
================================================================================
MTGTop8 スクレイプ由来の名前ゆれで deck_cards.card_id が大量に NULL（≈80%）。
card_name は壊さず、正規化した名前を mtg_cards_v2.card_name に当てて card_id だけ充填する。

正規化:
  - 先頭 "[]" 除去（スクレイプ屑）
  - " / " → " // "（旧 split の区切り）
  - 両面/Adventure の表面名 → DB の "表面 // 裏面" 名へ写像

card_id IS NULL の行だけ対象（precon の連結済みは触らない）。可逆（card_id を NULL に戻せば元通り）。

使い方:
  python fix_deck_links.py --dry_run   # 回復量の確認のみ
  python fix_deck_links.py             # card_id を充填
"""

import argparse
from collections import Counter

import psycopg2

from db_config import DB_CONFIG


def build_maps(cur):
    cur.execute("SELECT id, card_name FROM mtg_cards_v2")
    name2id = {}
    front2id = {}
    for cid, nm in cur.fetchall():
        name2id[nm] = cid
        if " // " in nm:
            front2id.setdefault(nm.split(" // ")[0], cid)
    return name2id, front2id


def canon_id(name, name2id, front2id):
    n = name
    if n.startswith("[]"):
        n = n[2:].strip()
    if n in name2id:
        return name2id[n]
    if " / " in n:
        alt = n.replace(" / ", " // ")
        if alt in name2id:
            return name2id[alt]
    if n in front2id:
        return front2id[n]
    return None


def main(dry_run: bool):
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    name2id, front2id = build_maps(cur)
    print(f"DB: {len(name2id):,} 名 / 両面表面 {len(front2id):,}")

    # card_id NULL の distinct 名と行数
    cur.execute("""
        SELECT card_name, count(*) FROM deck_cards
        WHERE card_id IS NULL GROUP BY card_name
    """)
    rows = cur.fetchall()
    null_names = {nm: cnt for nm, cnt in rows}
    null_rows = sum(null_names.values())
    print(f"card_id NULL: {len(null_names):,} 名 / {null_rows:,} 行")

    mapping = {}          # raw_name -> id
    rows_recoverable = 0
    reason = Counter()
    unmatched_rows = 0
    unmatched_ex = []
    for nm, cnt in null_names.items():
        cid = canon_id(nm, name2id, front2id)
        if cid is not None:
            mapping[nm] = cid
            rows_recoverable += cnt
            if nm.startswith("[]"):
                reason["[]除去"] += cnt
            elif " / " in nm:
                reason["スラッシュ"] += cnt
            elif nm in front2id:
                reason["DFC表面"] += cnt
            else:
                reason["完全一致(連結漏れ)"] += cnt
        else:
            unmatched_rows += cnt
            if len(unmatched_ex) < 20:
                unmatched_ex.append(nm)

    print(f"\n回復可能: {len(mapping):,} 名 / {rows_recoverable:,} 行")
    print("内訳(行):", dict(reason))
    print(f"残り不一致: {unmatched_rows:,} 行（次元カード等）")
    print("不一致サンプル:", unmatched_ex)

    if dry_run:
        print("\n※ dry_run: UPDATE していません")
        conn.close()
        return

    updated = 0
    for raw, cid in mapping.items():
        cur.execute(
            "UPDATE deck_cards SET card_id=%s WHERE card_name=%s AND card_id IS NULL",
            (cid, raw),
        )
        updated += cur.rowcount
    conn.commit()

    cur.execute("SELECT count(*) FILTER (WHERE card_id IS NULL), count(*) FROM deck_cards")
    nn, tot = cur.fetchone()
    print(f"\n完了: {updated:,} 行に card_id 充填。")
    print(f"card_id NULL: {tot-nn:,} 連結済 / 残 NULL {nn:,}/{tot:,} ({nn/tot*100:.1f}%)")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry_run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
