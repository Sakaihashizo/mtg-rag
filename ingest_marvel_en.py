"""
ingest_marvel_en.py — Marvel Super Heroes (msh / msc) 英語版を Scryfall API から取得し追加
================================================================================
add_new_cards.py のロジックを再利用する（重複排除=card_name / token系layout除外 /
build_embed_text / embedding は付けない）。embedding 未付与なので、検索で完全に出すには
後で rebuild_embed_text.py --reembed が要る（co-occ の card_id 結合には不要＝行が在れば良い）。

バルク全更新を避け Marvel のみを直接取得することで、(1) 他セットの巻き込み回避
(2) 170MB級バルクDLの回避（/ が逼迫しているため）を狙う。

使い方:
  python ingest_marvel_en.py --dry_run   # 取得＋スコープ確認のみ（INSERT しない）
  python ingest_marvel_en.py             # 追加（card_id を marvel_card_ids.txt に保存）
"""

import argparse
import json
import time
import urllib.request
import urllib.parse
from collections import Counter

import psycopg2

from db_config import DB_CONFIG
from add_new_cards import (
    build_embed_text,
    extract_oracle_text,
    extract_type_line,
    EXCLUDE_LAYOUTS,
)

SETS = ["msh", "msc"]
HEADERS = {"User-Agent": "mtg-rag-portfolio/1.0", "Accept": "application/json"}
IDS_OUT = "/mnt/mtg_rag/marvel_card_ids.txt"


def fetch_set(setcode: str) -> list:
    q = urllib.parse.quote(f"e:{setcode} lang:en")
    url = f"https://api.scryfall.com/cards/search?q={q}&unique=cards"
    out = []
    while url:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        if d.get("object") == "error":
            raise RuntimeError(f"{setcode}: {d.get('details')}")
        out.extend(d.get("data", []))
        url = d.get("next_page")
        time.sleep(0.12)  # Scryfall レート制限への配慮
    return out


def main(dry_run: bool):
    print("Scryfall から Marvel(msh/msc) 英語を取得中...")
    allcards = []
    for s in SETS:
        c = fetch_set(s)
        print(f"  {s}(en): {len(c)} 件")
        allcards.extend(c)
    print(f"取得合計: {len(allcards)} 件")

    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("SELECT card_name FROM mtg_cards_v2")
        existing = {row[0] for row in cur.fetchall()}
        # INSERT 列が実在することの軽い確認
        cur.execute("SELECT * FROM mtg_cards_v2 LIMIT 0")
        cols = [d[0] for d in cur.description]
    print(f"既存カード数: {len(existing)} / mtg_cards_v2 列数: {len(cols)}")
    assert "id" in cols, f"id 列が無い: {cols[:8]}..."

    new_cards, skip_layout, skip_exist = [], 0, 0
    seen = set()
    for card in allcards:
        if card.get("layout", "") in EXCLUDE_LAYOUTS:
            skip_layout += 1
            continue
        name = (card.get("name") or "").strip()
        if not name or name in existing or name in seen:
            skip_exist += 1
            continue
        seen.add(name)
        new_cards.append(card)

    by_set = Counter(c.get("set") for c in new_cards)
    print(f"layout除外={skip_layout} / 既存・重複除外={skip_exist} / 新規={len(new_cards)}")
    print(f"新規の set 内訳: {dict(by_set)}")
    print("サンプル(先頭15):")
    for c in new_cards[:15]:
        print(f"  [{c.get('set')}] {c.get('name')} "
              f"({c.get('layout')}) {c.get('type_line','')[:45]}")

    if dry_run:
        print("\n※ dry_run: INSERT していません")
        conn.close()
        return

    ids = []
    with conn.cursor() as cur:
        for card in new_cards:
            oracle_text = extract_oracle_text(card)
            type_line = extract_type_line(card)
            mana_cost = card.get("mana_cost") or ""
            cmc = card.get("cmc")
            colors = card.get("colors") or []
            color_identity = card.get("color_identity") or []
            keywords = card.get("keywords") or []
            legalities = json.dumps(card.get("legalities") or {})
            rarity = card.get("rarity") or ""
            set_code = card.get("set") or ""
            set_name = card.get("set_name") or ""
            layout = card.get("layout") or ""
            power = card.get("power")
            toughness = card.get("toughness")
            loyalty = card.get("loyalty")
            embed_text = build_embed_text(card)
            card_faces = json.dumps(card.get("card_faces")) \
                if card.get("card_faces") else None

            cur.execute("""
                INSERT INTO mtg_cards_v2 (
                    card_name, type_line, oracle_text, mana_cost, cmc,
                    colors, color_identity, keywords, legalities,
                    rarity, set_code, set_name, layout,
                    power, toughness, loyalty, card_faces_json, embed_text
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (card_name) DO NOTHING
                RETURNING id;
            """, (
                card.get("name"), type_line, oracle_text, mana_cost, cmc,
                colors, color_identity, keywords, legalities,
                rarity, set_code, set_name, layout,
                power, toughness, loyalty, card_faces, embed_text,
            ))
            row = cur.fetchone()
            if row:
                ids.append(row[0])

    conn.commit()
    conn.close()
    with open(IDS_OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(str(i) for i in ids) + ("\n" if ids else ""))
    print(f"\n完了: {len(ids)} 件を追加。id を {IDS_OUT} に保存。")
    print("※ embedding 未付与。co-occ のカード結合は可能（行が在る）。")
    print("  検索フル対応にするには後で rebuild_embed_text.py --reembed --card_ids_file marvel_card_ids.txt")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry_run", action="store_true",
                        help="取得とスコープ確認のみ（INSERT しない）")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
