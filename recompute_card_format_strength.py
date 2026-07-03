"""
Recompute per-format tournament strength from linked MTGTop8 deck data.

The metric is the raw number of distinct MTGTop8 maindeck decks containing a
card, grouped by format. Lands are excluded for v1.

Usage:
  python recompute_card_format_strength.py
  python recompute_card_format_strength.py --status
"""

import argparse

import psycopg2

from db_config import DB_CONFIG


SAMPLE_CARDS = (
    "Fatal Push",
    "Lightning Bolt",
    "Swords to Plowshares",
    "Abrade",
    "Go for the Throat",
    "Cut Down",
)


def create_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS card_format_strength (
                card_id     INTEGER REFERENCES mtg_cards_v2(id) ON DELETE CASCADE,
                format_name TEXT NOT NULL,
                play_decks  INTEGER NOT NULL,
                PRIMARY KEY (card_id, format_name)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS card_format_strength_format_score_idx
            ON card_format_strength (format_name, play_decks DESC);
            """
        )
    conn.commit()


def recompute(conn) -> int:
    create_table(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE card_format_strength;")
        cur.execute(
            """
            INSERT INTO card_format_strength (card_id, format_name, play_decks)
            SELECT
                dc.card_id,
                dl.format_name,
                COUNT(DISTINCT dc.deck_id) AS play_decks
            FROM deck_cards dc
            JOIN deck_list dl ON dc.deck_id = dl.id
            JOIN mtg_cards_v2 m ON m.id = dc.card_id
            WHERE dl.source = 'mtgtop8'
              AND dc.board = 'main'
              AND dc.card_id IS NOT NULL
              AND dl.format_name IS NOT NULL
              AND m.type_line NOT ILIKE '%%Land%%'
            GROUP BY dc.card_id, dl.format_name;
            """
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


def print_status(conn) -> None:
    create_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM card_format_strength;")
        total = cur.fetchone()[0]

        cur.execute(
            """
            SELECT format_name, COUNT(*) AS cards, MAX(play_decks) AS max_play_decks
            FROM card_format_strength
            GROUP BY format_name
            ORDER BY format_name;
            """
        )
        by_format = cur.fetchall()

        cur.execute(
            """
            SELECT
                m.card_name,
                cfs.format_name,
                cfs.play_decks
            FROM card_format_strength cfs
            JOIN mtg_cards_v2 m ON m.id = cfs.card_id
            WHERE m.card_name = ANY(%s)
            ORDER BY m.card_name, cfs.play_decks DESC, cfs.format_name;
            """,
            (list(SAMPLE_CARDS),),
        )
        samples = cur.fetchall()

    print(f"card_format_strength rows: {total:,}")

    print("\nBy format:")
    for format_name, cards, max_play_decks in by_format:
        print(f"  {format_name}: {cards:,} cards, max={max_play_decks:,}")

    print("\nSanity samples:")
    current = None
    for card_name, format_name, play_decks in samples:
        if card_name != current:
            current = card_name
            print(f"  {card_name}")
        print(f"    {format_name}: {play_decks}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true", help="show current table status")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        if args.status:
            print_status(conn)
            return

        inserted = recompute(conn)
        print(f"recomputed card_format_strength: {inserted:,} rows")
        print_status(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
