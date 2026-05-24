"""
extract_japanese.py — Scryfall all_cards.json から最新日本語テキストを抽出
=========================================================================
同名カードが複数の言語版・セット版がある場合、
「printed_text が存在する中で released_at が最新」のものを採用する。

printed_text が空のセット（eoc 等）は採用しない。
これにより Farseek 等で英語テキストが誤って格納される問題を解決。

使い方:
  python extract_japanese.py
  python extract_japanese.py --status
"""

import argparse
import re
import ijson
import psycopg2
from tqdm import tqdm


def is_japanese(text: str) -> bool:
    """テキストに日本語文字（ひらがな・カタカナ・漢字）が含まれているか"""
    return bool(re.search(r'[ぁ-んァ-ン一-龯]', text))

DB_CONFIG = {
    "dbname": "rag_dev",
    "user": "devuser",
    "password": "***REMOVED***",
    "host": "localhost",
    "port": 5435,
}

JSON_FILE    = "/mnt/new_hdd/all_cards_scryfall.json"
BATCH_COMMIT = 500


def add_japanese_columns(conn):
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE mtg_cards_v2
            ADD COLUMN IF NOT EXISTS japanese_name TEXT,
            ADD COLUMN IF NOT EXISTS japanese_oracle_text TEXT;
        """)
    conn.commit()
    print("カラム確認完了: japanese_name, japanese_oracle_text")


def extract_printed_text(card: dict) -> tuple[str | None, str | None]:
    """日本語名・日本語テキストを抽出（両面カード対応）"""
    ja_name = card.get("printed_name", "").strip() or None
    ja_text = card.get("printed_text", "").strip()
    if not ja_text:
        faces = card.get("card_faces") or []
        texts = [f.get("printed_text", "").strip() for f in faces
                 if f.get("printed_text", "").strip()]
        ja_text = " // ".join(texts) if texts else ""
    return ja_name, ja_text or None


def run():
    conn = psycopg2.connect(**DB_CONFIG)
    add_japanese_columns(conn)

    # mtg_cards_v2 の英語名 → id マップ
    with conn.cursor() as cur:
        cur.execute("SELECT id, card_name FROM mtg_cards_v2")
        name_to_id = {row[1]: row[0] for row in cur.fetchall()}
    total_cards = len(name_to_id)
    print(f"更新対象: {total_cards} 件")

    # all_cards_scryfall.json を1パスで読み込み
    # 「printed_text が存在する中で released_at が最新」のものを採用
    print(f"JSON スキャン中: {JSON_FILE}")
    # {name: (released_at, ja_name, ja_text)}
    ja_data: dict[str, tuple[str, str | None, str | None]] = {}
    skipped_empty = 0

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        for card in tqdm(ijson.items(f, "item"), desc="JSON scan", mininterval=10):
            if card.get("lang") != "ja":
                continue
            name = card.get("name", "").strip()
            if not name or name not in name_to_id:
                continue

            ja_name, ja_text = extract_printed_text(card)

            # printed_text が空、または日本語文字を含まない場合は採用しない
            # eoc 等のセットで英語テキストが誤って printed_text に格納されている問題を回避
            if not ja_text or not is_japanese(ja_text):
                skipped_empty += 1
                continue

            released_at = card.get("released_at", "1900-01-01")

            # printed_text が存在する中で最新のものを採用
            if name not in ja_data or released_at > ja_data[name][0]:
                ja_data[name] = (released_at, ja_name, ja_text)

    print(f"日本語データ収集完了: {len(ja_data)} 件")
    print(f"printed_text 空でスキップ: {skipped_empty} 件")

    # DB を一括 UPDATE（全件上書き）
    print("DB を更新中...")
    updated = 0
    not_found = 0

    with conn.cursor() as cur:
        for name, (released_at, ja_name, ja_text) in tqdm(
            ja_data.items(), desc="DB update", mininterval=5
        ):
            card_id = name_to_id.get(name)
            if not card_id:
                not_found += 1
                continue
            cur.execute("""
                UPDATE mtg_cards_v2
                SET japanese_name = %s, japanese_oracle_text = %s
                WHERE id = %s
            """, (ja_name, ja_text, card_id))
            updated += 1
            if updated % BATCH_COMMIT == 0:
                conn.commit()

    conn.commit()
    conn.close()

    print(f"\n完了!")
    print(f"  日本語テキスト更新: {updated} 件")
    print(f"  日本語版なし（NULL のまま）: {total_cards - updated} 件")


def check_status():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM mtg_cards_v2")
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM mtg_cards_v2
            WHERE japanese_oracle_text IS NOT NULL
        """)
        ja_text_count = cur.fetchone()[0]

        # 英語テキストが誤って入っていないか確認
        cur.execute("""
            SELECT COUNT(*) FROM mtg_cards_v2
            WHERE japanese_oracle_text IS NOT NULL
              AND japanese_oracle_text = oracle_text
        """)
        wrong_count = cur.fetchone()[0]

        cur.execute("""
            SELECT card_name, japanese_name, japanese_oracle_text
            FROM mtg_cards_v2
            WHERE card_name IN (
                'Farseek', 'Llanowar Elves', 'Counterspell',
                'Lightning Bolt', 'City of Traitors'
            )
        """)
        samples = cur.fetchall()

    conn.close()

    print(f"総カード数:                {total}")
    print(f"japanese_oracle_text あり: {ja_text_count}")
    print(f"日本語版なし（NULL）:      {total - ja_text_count}")
    print(f"英語テキストが誤って格納:  {wrong_count} 件")
    print("\nサンプル:")
    for en, ja_name, ja_text in samples:
        print(f"  {en} → {ja_name}")
        print(f"    {(ja_text or 'NULL')[:80]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true",
                        help="取得状況を確認する")
    args = parser.parse_args()

    if args.status:
        check_status()
    else:
        run()
