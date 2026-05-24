"""
update_oracle.py — Scryfall oracle_cards.json で最新オラクルテキストに更新
=========================================================================
Scryfall の oracle_cards.json は各カードの最新オラクルテキストを1件だけ収録。
MTGJSON の all_cards.json は古いセットのテキストが混在するため、
こちらで上書きすることで最新テキストに統一する。

更新対象:
  oracle_text, type_line, mana_cost, cmc,
  colors, color_identity, keywords, legalities,
  rarity, set_code, set_name

更新しないもの:
  japanese_oracle_text（all_cards.json から取得済みのまま維持）

使い方:
  python update_oracle.py
  python update_oracle.py --dry_run   # 実際には更新しない（確認用）
"""

import argparse
import json
import re
import psycopg2
from tqdm import tqdm

DB_CONFIG = {
    "dbname": "rag_dev",
    "user": "devuser",
    "password": "***REMOVED***",
    "host": "localhost",
    "port": 5435,
}

ORACLE_JSON = "/mnt/new_hdd/oracle_cards.json"
HF_CACHE    = "/mnt/new_hdd/hf_cache"
BATCH_COMMIT = 500

MANA_MAP = {
    r'\{W\}': 'white', r'\{U\}': 'blue', r'\{B\}': 'black',
    r'\{R\}': 'red',   r'\{G\}': 'green', r'\{C\}': 'colorless',
    r'\{T\}': 'tap',   r'\{Q\}': 'untap',
}
COLOR_NAMES = {
    'W': 'white', 'U': 'blue', 'B': 'black', 'R': 'red', 'G': 'green',
}
IMPORTANT_TYPES = [
    "Legendary", "Instant", "Sorcery", "Creature", "Enchantment",
    "Artifact", "Planeswalker", "Land",
]


def clean_text(text: str) -> str:
    for pattern, replacement in MANA_MAP.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r'\{\d+\}', 'N', text)
    return text.replace('\n', ' ').strip()


def build_embed_text(
    name: str, type_line: str, oracle_text: str,
    colors: list, keywords: list, rarity: str,
    power: str, toughness: str, loyalty: str,
    ja_name: str, ja_text: str,
    cooc_partners: list,
) -> str:
    oracle_clean = clean_text(oracle_text or "")
    ja_clean     = clean_text(ja_text or "")

    color_words = [COLOR_NAMES.get(c, c) for c in (colors or []) if c in COLOR_NAMES]
    main_types  = [t for t in IMPORTANT_TYPES if t in (type_line or "")]

    parts = [name]
    if main_types:
        parts.append("Type: " + " ".join(main_types))
    if color_words:
        parts.append("Color: " + " ".join(color_words))
    if keywords:
        kw = keywords if isinstance(keywords, list) else [keywords]
        parts.append("Keywords: " + ", ".join(str(k) for k in kw if k))
    if type_line:
        parts.append(type_line)
    if oracle_clean:
        parts.append(oracle_clean)
    if power is not None and toughness is not None:
        parts.append(f"P/T: {power}/{toughness}")
    elif loyalty is not None:
        parts.append(f"Loyalty: {loyalty}")
    if rarity in ("rare", "mythic"):
        parts.append(f"Rarity: {rarity}")
    if ja_name:
        parts.append(ja_name)
    if ja_clean:
        parts.append(ja_clean)
    if cooc_partners:
        parts.append("Often used with: " + ", ".join(cooc_partners[:5]))

    return "passage: " + " | ".join(p for p in parts if p)


def extract_oracle_text(card: dict) -> str:
    """両面カードは card_faces から結合"""
    top = card.get("oracle_text", "").strip()
    if top:
        return top
    faces = card.get("card_faces") or []
    texts = [f.get("oracle_text", "").strip() for f in faces
             if f.get("oracle_text", "").strip()]
    return " // ".join(texts) if texts else ""


def extract_type_line(card: dict) -> str:
    top = card.get("type_line", "").strip()
    if top:
        return top
    faces = card.get("card_faces") or []
    types = [f.get("type_line", "").strip() for f in faces
             if f.get("type_line", "").strip()]
    return " // ".join(types) if types else ""


def run(dry_run: bool = False):
    print("Scryfall oracle_cards.json を読み込み中...")
    with open(ORACLE_JSON, "r", encoding="utf-8") as f:
        scryfall_cards = json.load(f)

    # name → card の辞書を作成
    scryfall_map: dict[str, dict] = {}
    for card in scryfall_cards:
        name = card.get("name", "").strip()
        if name and name not in scryfall_map:
            scryfall_map[name] = card
    print(f"Scryfall カード数: {len(scryfall_map)}")

    conn = psycopg2.connect(**DB_CONFIG)

    # mtg_cards_v2 の全カードを取得
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, card_name, power, toughness, loyalty,
                   japanese_name, japanese_oracle_text
            FROM mtg_cards_v2
            ORDER BY id
        """)
        rows = cur.fetchall()
    print(f"mtg_cards_v2 カード数: {len(rows)}")

    # 共起情報を取得
    print("共起情報を取得中...")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT card_name, array_agg(partner ORDER BY co_count DESC) AS partners
            FROM (
                SELECT card_name_a AS card_name, card_name_b AS partner, co_count
                FROM card_cooccurrence WHERE source = 'mtgtop8'
                UNION ALL
                SELECT card_name_b AS card_name, card_name_a AS partner, co_count
                FROM card_cooccurrence WHERE source = 'mtgtop8'
            ) t
            GROUP BY card_name
        """)
        cooc_map = {row[0]: list(row[1])[:5] for row in cur.fetchall()}
    print(f"共起情報あり: {len(cooc_map)} 件")

    updated   = 0
    not_found = 0

    with conn.cursor() as cur:
        for card_id, card_name, power, toughness, loyalty, ja_name, ja_text in tqdm(
            rows, desc="oracle 更新", mininterval=5
        ):
            sc = scryfall_map.get(card_name)
            if not sc:
                not_found += 1
                continue

            oracle_text = extract_oracle_text(sc)
            type_line   = extract_type_line(sc)
            mana_cost   = sc.get("mana_cost") or ""
            cmc         = sc.get("cmc")
            colors      = sc.get("colors") or []
            color_id    = sc.get("color_identity") or []
            keywords    = sc.get("keywords") or []
            legalities  = json.dumps(sc.get("legalities") or {})
            rarity      = sc.get("rarity") or ""
            set_code    = sc.get("set") or ""
            set_name    = sc.get("set_name") or ""

            partners   = cooc_map.get(card_name, [])
            embed_text = build_embed_text(
                card_name, type_line, oracle_text,
                colors, keywords, rarity,
                power, toughness, loyalty,
                ja_name or "", ja_text or "",
                partners,
            )

            if not dry_run:
                cur.execute("""
                    UPDATE mtg_cards_v2 SET
                        oracle_text    = %s,
                        type_line      = %s,
                        mana_cost      = %s,
                        cmc            = %s,
                        colors         = %s,
                        color_identity = %s,
                        keywords       = %s,
                        legalities     = %s,
                        rarity         = %s,
                        set_code       = %s,
                        set_name       = %s,
                        embed_text     = %s
                    WHERE id = %s
                """, (
                    oracle_text, type_line, mana_cost, cmc,
                    colors, color_id, keywords, legalities,
                    rarity, set_code, set_name, embed_text,
                    card_id,
                ))

            updated += 1
            if not dry_run and updated % BATCH_COMMIT == 0:
                conn.commit()

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\n完了!")
    print(f"  更新: {updated} 件")
    print(f"  Scryfall に存在しない: {not_found} 件")
    if dry_run:
        print("  ※ dry_run モードのため実際には更新していません")

    # サンプル確認
    if not dry_run:
        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT card_name, oracle_text, set_code
                FROM mtg_cards_v2
                WHERE card_name IN (
                    'Llanowar Elves', 'Counterspell',
                    'Lightning Bolt', 'Badgermole Cub'
                )
            """)
            print("\nサンプル確認:")
            for name, oracle, setc in cur.fetchall():
                print(f"  [{setc}] {name}: {oracle[:60]}")
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry_run", action="store_true",
                        help="実際には更新しない（件数確認用）")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
