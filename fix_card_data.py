"""
fix_card_data.py — layout / power / toughness を最新 Scryfall バルク(oracle_cards.json)の
正準値に差分同期する。

- layout: 新メカニズム(prepare 等)を Scryfall が後から訂正したのに DB が import 時点の
  古いバルクのまま。embed 非対象＝reembed 不要。
- power/toughness: enrich_cards.py が「サイズの違う同名トークン」(Eternalize の 4/4 等)を
  誤参照して本体 P/T を上書き／一部欠損。embed_text の "P/T: x/y" に入る＝修正分は reembed 要。

oracle_cards.json は token を含まない oracle 代表版なので本体の正準値。差分のみ UPDATE。

使い方:
    python fix_card_data.py            # 差分表示のみ（非破壊）
    python fix_card_data.py --apply    # bulk 値で UPDATE・P/T 変更 id を pt_fixed_ids.txt へ
"""
import sys
import json
import psycopg2
from psycopg2.extras import execute_values
from db_config import get_db_config

BULK = "/mnt/new_hdd/oracle_cards.json"
APPLY = "--apply" in sys.argv
IDS_OUT = "/mnt/mtg_rag/pt_fixed_ids.txt"


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
            if n and n not in seen:
                seen.add(n)
                rows.append((n, c.get("layout"), c.get("power"), c.get("toughness")))

    cur.execute("CREATE TEMP TABLE _b(name text PRIMARY KEY, layout text, power text, toughness text) ON COMMIT DROP;")
    execute_values(cur, "INSERT INTO _b VALUES %s ON CONFLICT DO NOTHING", rows, page_size=2000)

    cur.execute("SELECT count(*) FROM mtg_cards_v2 c JOIN _b b ON c.card_name=b.name WHERE c.layout IS DISTINCT FROM b.layout;")
    print(f"layout 差分: {cur.fetchone()[0]} 件")

    cur.execute(
        "SELECT c.id, c.card_name, c.power, c.toughness, b.power, b.toughness "
        "FROM mtg_cards_v2 c JOIN _b b ON c.card_name=b.name "
        "WHERE c.power IS DISTINCT FROM b.power OR c.toughness IS DISTINCT FROM b.toughness "
        "ORDER BY c.card_name;"
    )
    pt = cur.fetchall()
    print(f"power/toughness 差分: {len(pt)} 件")
    for cid, name, dp, dt, bp, bt in pt:
        print(f"  [{name}] {dp}/{dt} -> {bp}/{bt}")

    if APPLY:
        cur.execute("UPDATE mtg_cards_v2 c SET layout=b.layout FROM _b b WHERE c.card_name=b.name AND c.layout IS DISTINCT FROM b.layout;")
        print(f"layout UPDATE: {cur.rowcount} 件")
        cur.execute("UPDATE mtg_cards_v2 c SET power=b.power, toughness=b.toughness FROM _b b WHERE c.card_name=b.name AND (c.power IS DISTINCT FROM b.power OR c.toughness IS DISTINCT FROM b.toughness);")
        print(f"power/toughness UPDATE: {cur.rowcount} 件")
        conn.commit()
        with open(IDS_OUT, "w") as f:
            for r in pt:
                f.write(f"{r[0]}\n")
        print(f"P/T 変更 {len(pt)} 件の id を {IDS_OUT} に出力（reembed 用）")
        cur.execute("SELECT count(*) FROM mtg_cards_v2 WHERE layout='adventure' AND card_faces_json IS NOT NULL AND card_faces_json->1->>'type_line' NOT ILIKE '%adventure%';")
        print(f"検証: layout=adventure だが f1 非Adventure = {cur.fetchone()[0]} 件（Omen のみ残るはず）")
    else:
        print("(差分表示のみ。UPDATE は --apply)")
    conn.close()


if __name__ == "__main__":
    main()
