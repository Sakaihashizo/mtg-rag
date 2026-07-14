"""
Recompute per-format tournament strength from linked MTGTop8 deck data.

v3（2026-07-14・EDH を物理分離）:
  - **card_format_strength = 1v1 構築フォーマット（60枚・4積み）のみ**
    （Standard/Pioneer/Modern/Legacy/Vintage/Pauper・母数 984〜2,227 で同じ桁）。
  - **edh_card_strength = シングルトン系（Duel Commander）を別テーブルに分離**。
    同じ列構成＝合併したくなったら UNION で足せる（2026-07-14 本人裁定）。
    分離の理由: デッキ構造（100枚シングルトン）が違い「採用」の意味論が別物・
    母数が一桁小さく（214）、率の横断比較に混ぜると MAX を支配して本線を汚染する
    （eval id=65〜68 で実測・7/8 の「card_format_strength に EDH を混ぜない」
    ガードの正しさが実証された）。
  - format_deck_counts.total_decks = フォーマット別の総デッキ数（率の分母・
    構築も EDH も持つ＝分母は事実の記録で、分離は strength テーブル側で行う）。
  - 検索側は play_decks / total_decks ＝ フォーマット内採用率で読む。
    横断フォールバックは card_format_strength（構築のみ）の率 MAX＝EDH は
    物理的に不在なので WHERE 除外に頼らない。EDH 指定クエリだけが
    edh_card_strength を参照する。
  - TRUNCATE は lock_timeout 付き（2026-07-13 ロック渋滞事件の教訓）。

Usage:
  python recompute_card_format_strength.py
  python recompute_card_format_strength.py --status
"""

import argparse

import psycopg2

from db_config import DB_CONFIG


# 1v1 構築（card_format_strength に入る・率の横断比較に参加する）
CONSTRUCTED_SOURCES = ("mtgtop8", "mtgtop8_vintage", "mtgtop8_pauper")
# シングルトン系（edh_card_strength に入る・EDH 指定クエリ専用）
EDH_SOURCES = ("mtgtop8_edh",)

SAMPLE_CARDS = (
    "Fatal Push",
    "Lightning Bolt",
    "Swords to Plowshares",
    "Sol Ring",
    "Counterspell",
    "Cut Down",
)

_STRENGTH_INSERT = """
    INSERT INTO {table} (card_id, format_name, play_decks)
    SELECT
        dc.card_id,
        dl.format_name,
        COUNT(DISTINCT dc.deck_id) AS play_decks
    FROM deck_cards dc
    JOIN deck_list dl ON dc.deck_id = dl.id
    JOIN mtg_cards_v2 m ON m.id = dc.card_id
    WHERE dl.source = ANY(%s)
      AND dc.board = 'main'
      AND dc.card_id IS NOT NULL
      AND dl.format_name IS NOT NULL
      AND m.type_line NOT ILIKE '%%Land%%'
    GROUP BY dc.card_id, dl.format_name;
"""


def create_table(conn) -> None:
    with conn.cursor() as cur:
        for table in ("card_format_strength", "edh_card_strength"):
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    card_id     INTEGER REFERENCES mtg_cards_v2(id) ON DELETE CASCADE,
                    format_name TEXT NOT NULL,
                    play_decks  INTEGER NOT NULL,
                    PRIMARY KEY (card_id, format_name)
                );
                """
            )
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {table}_format_score_idx
                ON {table} (format_name, play_decks DESC);
                """
            )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS format_deck_counts (
                format_name TEXT PRIMARY KEY,
                total_decks INTEGER NOT NULL
            );
            """
        )
    conn.commit()


def recompute(conn) -> tuple[int, int]:
    create_table(conn)
    all_sources = list(CONSTRUCTED_SOURCES) + list(EDH_SOURCES)
    with conn.cursor() as cur:
        # 黙って待たない（ロックが取れなければ 10 秒で失敗させ、原因を探す方に倒す）
        cur.execute("SET lock_timeout = '10s';")

        cur.execute("TRUNCATE format_deck_counts;")
        cur.execute(
            """
            INSERT INTO format_deck_counts (format_name, total_decks)
            SELECT format_name, COUNT(*)
            FROM deck_list
            WHERE source = ANY(%s) AND format_name IS NOT NULL
            GROUP BY format_name;
            """,
            (all_sources,),
        )

        cur.execute("TRUNCATE card_format_strength;")
        cur.execute(_STRENGTH_INSERT.format(table="card_format_strength"),
                    (list(CONSTRUCTED_SOURCES),))
        constructed = cur.rowcount

        cur.execute("TRUNCATE edh_card_strength;")
        cur.execute(_STRENGTH_INSERT.format(table="edh_card_strength"),
                    (list(EDH_SOURCES),))
        edh = cur.rowcount
    conn.commit()
    return constructed, edh


def print_status(conn) -> None:
    create_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM card_format_strength;")
        constructed = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM edh_card_strength;")
        edh = cur.fetchone()[0]

        cur.execute(
            """
            SELECT f.format_name, f.total_decks,
                   COALESCE(c.cards, e.cards, 0) AS cards,
                   CASE WHEN e.cards IS NOT NULL THEN 'EDH' ELSE '構築' END AS kind
            FROM format_deck_counts f
            LEFT JOIN (SELECT format_name, COUNT(*) AS cards
                       FROM card_format_strength GROUP BY format_name) c
                   ON c.format_name = f.format_name
            LEFT JOIN (SELECT format_name, COUNT(*) AS cards
                       FROM edh_card_strength GROUP BY format_name) e
                   ON e.format_name = f.format_name
            ORDER BY f.total_decks DESC;
            """
        )
        by_format = cur.fetchall()

        cur.execute(
            """
            SELECT m.card_name, s.format_name, s.play_decks,
                   ROUND(100.0 * s.play_decks / f.total_decks, 1) AS rate
            FROM (SELECT * FROM card_format_strength
                  UNION ALL SELECT * FROM edh_card_strength) s
            JOIN format_deck_counts f ON f.format_name = s.format_name
            JOIN mtg_cards_v2 m ON m.id = s.card_id
            WHERE m.card_name = ANY(%s)
            ORDER BY m.card_name, rate DESC;
            """,
            (list(SAMPLE_CARDS),),
        )
        samples = cur.fetchall()

    print(f"card_format_strength（構築）: {constructed:,} 行 / "
          f"edh_card_strength: {edh:,} 行")

    print("\nBy format (total_decks = 率の分母):")
    for format_name, total_decks, cards, kind in by_format:
        print(f"  [{kind}] {format_name}: decks={total_decks:,}, cards={cards:,}")

    print("\nSanity samples（採用率順・構築+EDH の UNION 表示）:")
    current = None
    for card_name, format_name, play_decks, rate in samples:
        if card_name != current:
            current = card_name
            print(f"  {card_name}")
        print(f"    {format_name}: {play_decks} decks ({rate}%)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true", help="show current table status")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        if args.status:
            print_status(conn)
            return

        constructed, edh = recompute(conn)
        print(f"recomputed: card_format_strength {constructed:,} 行 / "
              f"edh_card_strength {edh:,} 行")
        print_status(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
