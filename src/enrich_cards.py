"""
enrich_cards.py — mtg_cards_v2 にゲームフィールドと日本語テキストを追加
==========================================================================
all_cards.json を1パスで読み、以下を mtg_cards_v2 に格納する:

  ゲームフィールド:
    power, toughness, loyalty, mana_cost, cmc,
    colors, color_identity, rarity, set_code, set_name,
    collector_number, keywords, card_faces_json

  日本語フィールド:
    japanese_name, japanese_oracle_text

処理方式:
  - all_cards.json を1回だけ読む（API 不使用）
  - 英語カード（lang=en）からゲームフィールドを取得
  - 日本語カード（lang=ja）から japanese_* を取得
  - 既存カラムは ALTER TABLE IF NOT EXISTS で安全に追加
  - 同じ英語名が複数ある場合は最初の1件を採用

使い方:
  python enrich_cards.py            # 全件処理
  python enrich_cards.py --status   # 取得状況確認
"""

import argparse
import json
import ijson
import psycopg2
import psycopg2.extras
from tqdm import tqdm

from db_config import DB_CONFIG

JSON_FILE    = "/mnt/new_hdd/all_cards.json"
BATCH_COMMIT = 500


# ─── カラム追加 ───────────────────────────────────────────────

def add_columns(conn):
    """必要なカラムを mtg_cards_v2 に追加する（既存なら何もしない）"""
    additions = [
        ("power",             "TEXT"),
        ("toughness",         "TEXT"),
        ("loyalty",           "TEXT"),
        ("cmc",               "NUMERIC"),
        ("color_identity",    "TEXT[]"),
        ("set_code",          "TEXT"),
        ("set_name",          "TEXT"),
        ("collector_number",  "TEXT"),
        ("card_faces_json",   "JSONB"),
        ("japanese_name",         "TEXT"),
        ("japanese_oracle_text",  "TEXT"),
        # mana_cost / colors / rarity / keywords は import_cards.py で既に追加済み
        # 念のため IF NOT EXISTS で追加しておく
        ("mana_cost",         "TEXT"),
        ("colors",            "TEXT[]"),
        ("rarity",            "TEXT"),
        ("keywords",          "TEXT[]"),
        ("legalities", "JSONB"),
    ]
    with conn.cursor() as cur:
        for col, col_type in additions:
            cur.execute(f"""
                ALTER TABLE mtg_cards_v2
                ADD COLUMN IF NOT EXISTS {col} {col_type};
            """)
    conn.commit()
    print(f"カラム追加完了: {[c for c, _ in additions]}")


# ─── テキスト抽出ヘルパー ─────────────────────────────────────

def extract_oracle_text(card: dict) -> str:
    top = card.get("oracle_text", "").strip()
    if top:
        return top
    faces = card.get("card_faces") or []
    texts = [f.get("oracle_text", "").strip() for f in faces
             if f.get("oracle_text", "").strip()]
    return " // ".join(texts) if texts else ""


def extract_printed_text(card: dict) -> tuple[str | None, str | None]:
    """日本語名・日本語テキストを抽出"""
    ja_name = card.get("printed_name", "").strip() or None
    ja_text = card.get("printed_text", "").strip()
    if not ja_text:
        faces = card.get("card_faces") or []
        texts = [f.get("printed_text", "").strip() for f in faces
                 if f.get("printed_text", "").strip()]
        ja_text = " // ".join(texts) if texts else ""
    return ja_name, ja_text or None


def extract_game_fields(card: dict) -> dict:
    """ゲームプレイに関わるフィールドをすべて抽出"""
    # keywords は配列で格納
    keywords = card.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]

    # colors / color_identity
    colors         = card.get("colors") or []
    color_identity = card.get("color_identity") or []

    # card_faces は JSONB で丸ごと保存
    card_faces = card.get("card_faces")
    card_faces_json = json.dumps(card_faces, ensure_ascii=False) if card_faces else None

    # cmc は float → Decimal に変換（psycopg2 は Python float をそのまま渡せる）
    cmc_raw = card.get("cmc")
    try:
        cmc = float(cmc_raw) if cmc_raw is not None else None
    except (TypeError, ValueError):
        cmc = None

    return {
        "power":            card.get("power"),
        "toughness":        card.get("toughness"),
        "loyalty":          card.get("loyalty"),
        "mana_cost":        card.get("mana_cost") or None,
        "cmc":              cmc,
        "colors":           colors,
        "color_identity":   color_identity,
        "rarity":           card.get("rarity") or None,
        "set_code":         card.get("set") or None,
        "set_name":         card.get("set_name") or None,
        "collector_number": card.get("collector_number") or None,
        "keywords":         keywords,
        "card_faces_json":  card_faces_json,
        "legalities": json.dumps(card.get("legalities")) if card.get("legalities") else None,
    }


# ─── メイン処理 ───────────────────────────────────────────────

def run():
    conn = psycopg2.connect(**DB_CONFIG)
    add_columns(conn)

    # mtg_cards_v2 の英語名 → id マップ
    with conn.cursor() as cur:
        cur.execute("SELECT id, card_name FROM mtg_cards_v2")
        name_to_id = {row[1]: row[0] for row in cur.fetchall()}
    total_cards = len(name_to_id)
    print(f"対象カード数: {total_cards} 件")

    # all_cards.json を1パスで読む
    # 英語カード（lang=en）: ゲームフィールドを収集
    # 日本語カード（lang=ja）: japanese_* を収集
    print("all_cards.json をスキャン中...")

    game_data: dict[str, dict]                    = {}  # name → game fields
    ja_data:   dict[str, tuple[str|None, str|None]] = {}  # name → (ja_name, ja_text)

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        for card in tqdm(ijson.items(f, "item"), desc="JSON scan", mininterval=10):
            name = card.get("name", "").strip()
            if not name or name not in name_to_id:
                continue

            lang = card.get("lang", "")

            # 英語カードからゲームフィールドを収集（最初の1件を採用）
            if lang == "en" and name not in game_data:
                game_data[name] = extract_game_fields(card)

            # 日本語カードから japanese_* を収集（最初の1件を採用）
            elif lang == "ja" and name not in ja_data:
                ja_name, ja_text = extract_printed_text(card)
                if ja_name or ja_text:
                    ja_data[name] = (ja_name, ja_text)

    print(f"ゲームフィールド収集: {len(game_data)} 件")
    print(f"日本語テキスト収集:   {len(ja_data)} 件")

    # DB を一括 UPDATE
    print("DB を更新中...")
    updated = 0

    with conn.cursor() as cur:
        for i, (name, fields) in enumerate(
            tqdm(game_data.items(), desc="DB update (game)", mininterval=5)
        ):
            card_id = name_to_id[name]
            ja_name, ja_text = ja_data.get(name, (None, None))

            cur.execute("""
                UPDATE mtg_cards_v2 SET
                    power             = %s,
                    toughness         = %s,
                    loyalty           = %s,
                    mana_cost         = %s,
                    cmc               = %s,
                    colors            = %s,
                    color_identity    = %s,
                    rarity            = %s,
                    set_code          = %s,
                    set_name          = %s,
                    collector_number  = %s,
                    keywords          = %s,
                    card_faces_json   = %s,
                    legalities = %s,
                    japanese_name         = %s,
                    japanese_oracle_text  = %s
                WHERE id = %s
            """, (
                fields["power"],
                fields["toughness"],
                fields["loyalty"],
                fields["mana_cost"],
                fields["cmc"],
                fields["colors"],
                fields["color_identity"],
                fields["rarity"],
                fields["set_code"],
                fields["set_name"],
                fields["collector_number"],
                fields["keywords"],
                fields["card_faces_json"],
                fields["legalities"],
                ja_name,
                ja_text,
                card_id,
            ))
            updated += 1
            if updated % BATCH_COMMIT == 0:
                conn.commit()

    conn.commit()
    conn.close()

    no_game  = total_cards - len(game_data)
    no_ja    = total_cards - len(ja_data)
    print(f"\n完了!")
    print(f"  ゲームフィールド更新: {updated} 件")
    print(f"  英語データなし:       {no_game} 件")
    print(f"  日本語テキストあり:   {len(ja_data)} 件")
    print(f"  日本語版なし（NULL）: {no_ja} 件")


# ─── 状況確認 ─────────────────────────────────────────────────

def check_status():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM mtg_cards_v2")
        total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM mtg_cards_v2 WHERE power IS NOT NULL")
        has_power = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM mtg_cards_v2 WHERE japanese_oracle_text IS NOT NULL")
        has_ja = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM mtg_cards_v2 WHERE set_code IS NOT NULL")
        has_set = cur.fetchone()[0]

        # サンプル：有名カード
        cur.execute("""
            SELECT card_name, japanese_name, mana_cost, cmc,
                   power, toughness, rarity, set_code, keywords
            FROM mtg_cards_v2
            WHERE card_name IN (
                'Counterspell', 'Lightning Bolt', 'Black Lotus',
                'Llanowar Elves', 'Snapcaster Mage'
            )
        """)
        samples = cur.fetchall()

    conn.close()

    print(f"総カード数:                {total}")
    print(f"power 格納済み:            {has_power}")
    print(f"japanese_oracle_text あり: {has_ja}")
    print(f"set_code 格納済み:         {has_set}")
    print("\nサンプル:")
    for row in samples:
        name, ja_name, mc, cmc, pw, tou, rar, setc, kw = row
        print(f"  {name} ({ja_name})")
        print(f"    cost={mc} cmc={cmc} P/T={pw}/{tou} "
              f"rarity={rar} set={setc}")
        print(f"    keywords={kw}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true",
                        help="取得状況を確認する")
    args = parser.parse_args()

    if args.status:
        check_status()
    else:
        run()
