"""
fix_layout.py — mtg_cards_v2 の layout を最新 Scryfall バルクの正準値に同期する。

背景: 新メカニズム（prepare 等）を Scryfall が後から訂正した分が、import 時点の古いバルク
由来で DB に反映されておらず、layout が実態とズレている（例: prepare 呪文が adventure 分類）。
import_cards.py は layout をバルクからそのままコピーするだけ（バグではない）ので、最新バルクで
DB を同期すれば直る。layout は embed_text に含めない構造化メタ＝reembed 不要・検索影響なし。

使い方:
    python fix_layout.py          # 差分表示のみ（DB vs バルク・非破壊）
    python fix_layout.py --apply  # 差分を Scryfall バルクの正準値で UPDATE
"""
import sys
import json
import psycopg2
from psycopg2.extras import execute_values
from db_config import get_db_config

BULK = "/mnt/new_hdd/oracle_cards.json"
APPLY = "--apply" in sys.argv


def main():
    conn = psycopg2.connect(**get_db_config())
    cur = conn.cursor()

    rows, seen = [], set()
    with open(BULK, encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            try:
                c = json.loads(line)
            except Exception:
                continue
            n = c.get("name")
            if not n or n in seen:
                continue
            seen.add(n)
            rows.append((n, c.get("layout")))
    print(f"バルクから {len(rows)} 件の (name, layout) を読み込み")

    cur.execute("CREATE TEMP TABLE _bulk(name text PRIMARY KEY, layout text) ON COMMIT DROP;")
    execute_values(cur,
                   "INSERT INTO _bulk(name, layout) VALUES %s ON CONFLICT DO NOTHING",
                   rows, page_size=2000)

    print("=== 差分マトリクス (DB layout -> バルク layout : 件数) ===")
    cur.execute("""
        SELECT c.layout AS db, b.layout AS bulk, count(*)
        FROM mtg_cards_v2 c JOIN _bulk b ON c.card_name = b.name
        WHERE c.layout IS DISTINCT FROM b.layout
        GROUP BY 1, 2 ORDER BY 3 DESC;
    """)
    diffs = cur.fetchall()
    total = sum(n for _, _, n in diffs)
    for db, bulk, n in diffs:
        print(f"  {db!r:>14} -> {bulk!r:<14} : {n}")
    print(f"差分合計: {total} 件")

    if APPLY and total > 0:
        cur.execute("""
            UPDATE mtg_cards_v2 c SET layout = b.layout FROM _bulk b
            WHERE c.card_name = b.name AND c.layout IS DISTINCT FROM b.layout;
        """)
        print(f"UPDATE: {cur.rowcount} 件")
        conn.commit()
        print("コミット完了")
        # 検証: adventure に非Adventure が残らないか
        cur.execute("""
            SELECT count(*) FROM mtg_cards_v2
            WHERE layout='adventure' AND card_faces_json IS NOT NULL
              AND card_faces_json->1->>'type_line' NOT ILIKE '%adventure%';
        """)
        print(f"検証: layout=adventure だが f1 が非Adventure = {cur.fetchone()[0]} 件（Omen 等のみ残るはず）")
    else:
        print("(差分表示のみ。UPDATE するには --apply)")

    conn.close()


if __name__ == "__main__":
    main()
