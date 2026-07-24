"""
enrich_scryfall_meta.py — Scryfall の構造化メタデータを mtg_cards_v2 に取り込む手動更新スクリプト。

取り込むフィールド:
  - produced_mana (text[])   : マナ生成（マナクリーチャー判定用・手書きルール不要）
  - edhrec_rank   (integer)  : EDH 人気度ランク（小さいほど人気）
  - game_changer  (boolean)  : Commander ブラケットの高影響カードフラグ

プロトタイプ段階の「手動更新」用。新セットが出たら oracle_cards.json を最新にして再実行する。
冪等（ADD COLUMN IF NOT EXISTS / UPDATE 上書き）なので何度でも安全に回せる。
構造化列であり embed_text に入れないため reembed は不要。

使い方:
    python enrich_scryfall_meta.py [oracle_cards.json のパス]
"""
import sys
import json
import psycopg2
from psycopg2.extras import execute_values
from db_config import get_db_config

BULK = sys.argv[1] if len(sys.argv) > 1 else "/mnt/new_hdd/oracle_cards.json"

ALTER = """
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS produced_mana text[];
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS edhrec_rank   integer;
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS game_changer  boolean;
"""


def main():
    conn = psycopg2.connect(**get_db_config())
    cur = conn.cursor()
    cur.execute(ALTER)
    conn.commit()
    print("カラム追加（IF NOT EXISTS）完了")

    # Scryfall バルクから name -> (produced_mana, edhrec_rank, game_changer)
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
            rows.append((n, c.get("produced_mana"),
                         c.get("edhrec_rank"), c.get("game_changer")))
    print(f"Scryfall から {len(rows)} 件のメタを読み込み")

    # 一時テーブルに入れて UPDATE FROM（33k行を1文で更新）
    cur.execute("""
        CREATE TEMP TABLE _meta(
            name text, produced_mana text[], edhrec_rank int, game_changer bool
        ) ON COMMIT DROP;
    """)
    execute_values(
        cur,
        "INSERT INTO _meta(name, produced_mana, edhrec_rank, game_changer) VALUES %s",
        rows, page_size=1000,
    )
    cur.execute("""
        UPDATE mtg_cards_v2 c
        SET produced_mana = s.produced_mana,
            edhrec_rank   = s.edhrec_rank,
            game_changer  = s.game_changer
        FROM _meta s
        WHERE c.card_name = s.name;
    """)
    print(f"mtg_cards_v2 を更新: {cur.rowcount} 行")
    conn.commit()

    # 検証
    for label, sql in [
        ("produced_mana 保有", "SELECT count(*) FROM mtg_cards_v2 WHERE produced_mana IS NOT NULL"),
        ("edhrec_rank 保有",   "SELECT count(*) FROM mtg_cards_v2 WHERE edhrec_rank IS NOT NULL"),
        ("game_changer=true",  "SELECT count(*) FROM mtg_cards_v2 WHERE game_changer IS TRUE"),
    ]:
        cur.execute(sql)
        print(f"  {label}: {cur.fetchone()[0]}")
    cur.execute("SELECT card_name, produced_mana, edhrec_rank, game_changer "
                "FROM mtg_cards_v2 WHERE card_name='Llanowar Elves'")
    print("  Llanowar Elves:", cur.fetchone())

    conn.close()


if __name__ == "__main__":
    main()
