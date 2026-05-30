"""
add_new_cards.py — Scryfall oracle_cards.json から新カードを追加
================================================================
mtg_cards_v2 に存在しないカードのみ INSERT する。
既存カードは触らない（update_oracle.py が担当）。

embedding は追加しない（追加後に rebuild_embed_text.py --reembed で対応）。

使い方:
  # 新カードの確認（INSERT しない）
  python add_new_cards.py --dry_run

  # 新カードを追加
  python add_new_cards.py
"""

import argparse
import json
import re
import psycopg2
from tqdm import tqdm

from db_config import DB_CONFIG

ORACLE_JSON = "/mnt/new_hdd/oracle_cards.json"

EXCLUDE_LAYOUTS = {
    "art_series", "token", "emblem",
    "double_faced_token", "vanguard", "planar", "scheme",
}

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


def extract_oracle_text(card: dict) -> str:
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


def build_embed_text(card: dict) -> str:
    name       = card.get("name", "")
    type_line  = extract_type_line(card)
    oracle     = clean_text(extract_oracle_text(card))
    colors     = card.get("colors") or []
    keywords   = card.get("keywords") or []
    rarity     = card.get("rarity", "") or ""
    power      = card.get("power")
    toughness  = card.get("toughness")
    loyalty    = card.get("loyalty")

    color_words = [COLOR_NAMES.get(c, c) for c in colors if c in COLOR_NAMES]
    main_types  = [t for t in IMPORTANT_TYPES if t in type_line]

    parts = [name]
    if main_types:
        parts.append("Type: " + " ".join(main_types))
    if color_words:
        parts.append("Color: " + " ".join(color_words))
    if keywords:
        parts.append("Keywords: " + ", ".join(str(k) for k in keywords if k))
    if type_line:
        parts.append(type_line)
    if oracle:
        parts.append(oracle)
    if power is not None and toughness is not None:
        parts.append(f"P/T: {power}/{toughness}")
    elif loyalty is not None:
        parts.append(f"Loyalty: {loyalty}")
    if rarity in ("rare", "mythic"):
        parts.append(f"Rarity: {rarity}")

    return "passage: " + " | ".join(p for p in parts if p)


def run(dry_run: bool = False):
    print("oracle_cards.json を読み込み中...")
    with open(ORACLE_JSON, "r", encoding="utf-8") as f:
        scryfall_cards = json.load(f)
    print(f"Scryfall カード数: {len(scryfall_cards)}")

    conn = psycopg2.connect(**DB_CONFIG)

    # 既存カード名を取得
    with conn.cursor() as cur:
        cur.execute("SELECT card_name FROM mtg_cards_v2")
        existing = {row[0] for row in cur.fetchall()}
    print(f"既存カード数: {len(existing)}")

    # 新カードを抽出
    new_cards = []
    skipped_layout = 0
    for card in scryfall_cards:
        layout = card.get("layout", "")
        if layout in EXCLUDE_LAYOUTS:
            skipped_layout += 1
            continue
        name = card.get("name", "").strip()
        if not name or name in existing:
            continue
        new_cards.append(card)

    print(f"layout 除外: {skipped_layout} 件")
    print(f"新規追加対象: {len(new_cards)} 件")

    if dry_run:
        print("\n--- dry_run モード: 追加されるカードのサンプル ---")
        for card in new_cards[:20]:
            print(f"  [{card.get('set')}] {card.get('name')} "
                  f"({card.get('layout')}) {card.get('type_line', '')[:40]}")
        print("※ 実際には追加していません")
        conn.close()
        return

    # INSERT
    inserted = 0
    with conn.cursor() as cur:
        for card in tqdm(new_cards, desc="新カード追加", mininterval=5):
            oracle_text    = extract_oracle_text(card)
            type_line      = extract_type_line(card)
            mana_cost      = card.get("mana_cost") or ""
            cmc            = card.get("cmc")
            colors         = card.get("colors") or []
            color_identity = card.get("color_identity") or []
            keywords       = card.get("keywords") or []
            legalities     = json.dumps(card.get("legalities") or {})
            rarity         = card.get("rarity") or ""
            set_code       = card.get("set") or ""
            set_name       = card.get("set_name") or ""
            layout         = card.get("layout") or ""
            power          = card.get("power")
            toughness      = card.get("toughness")
            loyalty        = card.get("loyalty")
            embed_text     = build_embed_text(card)
            card_faces     = json.dumps(card.get("card_faces")) \
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
                ON CONFLICT (card_name) DO NOTHING;
            """, (
                card.get("name"), type_line, oracle_text, mana_cost, cmc,
                colors, color_identity, keywords, legalities,
                rarity, set_code, set_name, layout,
                power, toughness, loyalty, card_faces, embed_text,
            ))
            inserted += 1
            if inserted % 500 == 0:
                conn.commit()

    conn.commit()
    conn.close()
    print(f"\n完了: {inserted} 件を追加しました")
    print("※ embedding は未追加です。")
    print("  次のステップ: extract_japanese.py → update_oracle.py → rebuild_embed_text.py --reembed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry_run", action="store_true",
                        help="追加されるカードを確認するだけ（INSERT しない）")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
