"""
import_decks.py — MTGJSON AllDeckFiles を PostgreSQL に取り込む
=============================================================
テーブル構成:
  deck_list  ... デッキのメタ情報
  deck_cards ... デッキ内のカード（mainBoard / sideBoard / commander）

source カラムで将来の大会データと区別できる:
  'mtgjson_precon' ... 今回取り込むプリコンデッキ
  'mtgtop8'        ... 将来取り込む大会入賞デッキ（同じテーブルに追加可能）

使い方:
  python import_decks.py            # 全件取り込み
  python import_decks.py --status   # 取り込み状況確認
  python import_decks.py --cooccur  # 共起集計テーブルを更新
"""

import argparse
import json
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
from tqdm import tqdm

from db_config import DB_CONFIG

DECK_DIR = Path("/mnt/new_hdd/AllDecks/AllDeckFiles")
SOURCE   = "mtgjson_precon"


# ─── テーブル作成 ─────────────────────────────────────────────

def create_tables(conn):
    with conn.cursor() as cur:
        # デッキ情報テーブル
        cur.execute("""
            CREATE TABLE IF NOT EXISTS deck_list (
                id         SERIAL PRIMARY KEY,
                deck_name  TEXT NOT NULL UNIQUE,
                set_code   TEXT,
                source     TEXT NOT NULL DEFAULT 'mtgjson_precon',
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        # デッキ内カードテーブル
        cur.execute("""
            CREATE TABLE IF NOT EXISTS deck_cards (
                id        SERIAL PRIMARY KEY,
                deck_id   INTEGER REFERENCES deck_list(id) ON DELETE CASCADE,
                card_name TEXT NOT NULL,
                count     INTEGER NOT NULL DEFAULT 1,
                board     TEXT NOT NULL DEFAULT 'main'
            );
        """)

        # 検索用インデックス
        cur.execute("""
            CREATE INDEX IF NOT EXISTS deck_cards_card_name_idx
            ON deck_cards (card_name);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS deck_cards_deck_id_idx
            ON deck_cards (deck_id);
        """)

        # 共起集計テーブル（後で更新する）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS card_cooccurrence (
                card_name_a    TEXT NOT NULL,
                card_name_b    TEXT NOT NULL,
                co_count       INTEGER NOT NULL DEFAULT 0,
                source         TEXT NOT NULL DEFAULT 'mtgjson_precon',
                PRIMARY KEY (card_name_a, card_name_b, source)
            );
        """)

    conn.commit()
    print("テーブル作成完了")


# ─── デッキ取り込み ───────────────────────────────────────────

def extract_cards(board: list, board_name: str) -> list[tuple[str, int, str]]:
    """ボードからカード情報を抽出する"""
    results = []
    for card in board:
        name  = card.get("name", "").strip()
        count = card.get("count", 1)
        if name:
            results.append((name, count, board_name))
    return results


def import_decks(conn):
    deck_files = sorted(DECK_DIR.glob("*.json"))
    total      = len(deck_files)
    print(f"デッキファイル数: {total}")

    imported = 0
    skipped  = 0
    errors   = 0

    for deck_file in tqdm(deck_files, desc="デッキ取り込み", mininterval=5):
        deck_name = deck_file.stem  # ファイル名から拡張子を除いたもの

        try:
            with open(deck_file, "r", encoding="utf-8") as f:
                data = json.load(f).get("data", {})

            set_code = data.get("code", "")

            # カードリスト抽出
            cards: list[tuple[str, int, str]] = []
            cards += extract_cards(data.get("mainBoard", []),      "main")
            cards += extract_cards(data.get("sideBoard", []),      "side")
            cards += extract_cards(data.get("commander", []),      "commander")
            cards += extract_cards(data.get("displayCommander", []), "commander")

            if not cards:
                skipped += 1
                continue

            with conn.cursor() as cur:
                # デッキを INSERT（重複はスキップ）
                cur.execute("""
                    INSERT INTO deck_list (deck_name, set_code, source)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (deck_name) DO NOTHING
                    RETURNING id;
                """, (deck_name, set_code, SOURCE))
                result = cur.fetchone()

                if result is None:
                    skipped += 1
                    continue

                deck_id = result[0]

                # カードを一括 INSERT
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO deck_cards (deck_id, card_name, count, board)
                    VALUES %s
                    """,
                    [(deck_id, name, count, board) for name, count, board in cards],
                )

            conn.commit()
            imported += 1

        except Exception as e:
            conn.rollback()
            errors += 1
            if errors <= 5:
                print(f"  エラー: {deck_file.name} — {e}")

    print(f"\n完了: {imported} 件取り込み / {skipped} 件スキップ / {errors} 件エラー")


# ─── 共起集計 ─────────────────────────────────────────────────

def update_cooccurrence(conn, source: str = SOURCE):
    """
    同じデッキに含まれるカードの共起回数を集計して card_cooccurrence に格納する。
    card_name_a < card_name_b になるよう正規化（重複防止）。
    時間がかかるので実行後にインデックスを張る。
    """
    print(f"共起集計中（source={source}）...")

    with conn.cursor() as cur:
        # 既存データをクリア
        cur.execute("""
            DELETE FROM card_cooccurrence WHERE source = %s
        """, (source,))

        # 共起集計（同じデッキのカードペアをカウント）
        # 共起集計（同じデッキのカードペアをカウント）
        cur.execute("""
            INSERT INTO card_cooccurrence (card_name_a, card_name_b, co_count, source)
            SELECT
                a.card_name,
                b.card_name,
                COUNT(DISTINCT a.deck_id) AS co_count,
                %s AS source
            FROM deck_cards a
            JOIN deck_cards b
                ON a.deck_id = b.deck_id
                AND a.card_name < b.card_name
            JOIN deck_list d ON a.deck_id = d.id
            WHERE d.source = %s
            AND a.board = 'main'
            AND b.board = 'main'
            -- 基本土地を除外
            AND a.card_name NOT IN (
                'Plains', 'Island', 'Swamp', 'Mountain', 'Forest',
                'Wastes', 'Snow-Covered Plains', 'Snow-Covered Island',
                'Snow-Covered Swamp', 'Snow-Covered Mountain', 'Snow-Covered Forest'
            )
            AND b.card_name NOT IN (
                'Plains', 'Island', 'Swamp', 'Mountain', 'Forest',
                'Wastes', 'Snow-Covered Plains', 'Snow-Covered Island',
                'Snow-Covered Swamp', 'Snow-Covered Mountain', 'Snow-Covered Forest'
            )
            GROUP BY a.card_name, b.card_name
            HAVING COUNT(DISTINCT a.deck_id) >= 2
            ORDER BY co_count DESC;
        """, (source, source))

    conn.commit()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM card_cooccurrence WHERE source = %s
        """, (source,))
        count = cur.fetchone()[0]

    print(f"共起ペア数: {count:,} 件")

    # サンプル表示
    with conn.cursor() as cur:
        cur.execute("""
            SELECT card_name_a, card_name_b, co_count
            FROM card_cooccurrence
            WHERE source = %s
            ORDER BY co_count DESC
            LIMIT 10
        """, (source,))
        rows = cur.fetchall()

    print("\nTOP10 共起ペア:")
    for a, b, c in rows:
        print(f"  {c:4d}回  {a} ↔ {b}")


# ─── 状況確認 ─────────────────────────────────────────────────

def check_status(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM deck_list")
        deck_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM deck_cards")
        card_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM card_cooccurrence")
        cooc_count = cur.fetchone()[0]

        cur.execute("""
            SELECT source, COUNT(*) FROM deck_list GROUP BY source
        """)
        by_source = cur.fetchall()

        # Counterspell の共起カードを表示
        cur.execute("""
            SELECT
                CASE WHEN card_name_a = 'Counterspell' THEN card_name_b
                     ELSE card_name_a END AS partner,
                co_count
            FROM card_cooccurrence
            WHERE card_name_a = 'Counterspell' OR card_name_b = 'Counterspell'
            ORDER BY co_count DESC
            LIMIT 10
        """)
        counterspell_cooc = cur.fetchall()

    print(f"デッキ数:     {deck_count:,}")
    print(f"カード総行数: {card_count:,}")
    print(f"共起ペア数:   {cooc_count:,}")
    print(f"\nソース別:")
    for source, count in by_source:
        print(f"  {source}: {count}")

    if counterspell_cooc:
        print(f"\nCounterspell と共起するカード TOP10:")
        for partner, count in counterspell_cooc:
            print(f"  {count:3d}回  {partner}")
    else:
        print("\nCounterspell の共起データなし（共起集計を実行してください）")


# ─── エントリーポイント ───────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--status",  action="store_true", help="取り込み状況確認")
    parser.add_argument("--cooccur", action="store_true", help="共起集計テーブルを更新")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    create_tables(conn)

    if args.status:
        check_status(conn)
    elif args.cooccur:
        update_cooccurrence(conn, source="mtgtop8")
    else:
        import_decks(conn)
        print("\n共起集計を実行しますか？（数分かかります）")
        ans = input("実行する場合は y を入力: ").strip().lower()
        if ans == "y":
            update_cooccurrence(conn)

    conn.close()
