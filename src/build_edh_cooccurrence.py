#!/usr/bin/env python3
"""edh_card_cooccurrence_v2 — EDH デッキの共起集計テーブル新設（2026-07-30）。

旧 edh_card_cooccurrence（51万行・初期に一度焼かれたきりの化石）の二つの欠陥を
修正する:
  1. 統率者（board='commander'）との対を持たない
  2. card_name ベースで `[] ` プレフィックスの名寄せ割れがある

新テーブルは card_id ベース（ほぼ全行解決済み）・commander を main と合流させて
対を作る。旧テーブルは触らず残置。

## 集計の定義
- 対象デッキ: deck_list.source IN ('moxfield_edh','mtgtop8_edh')（それぞれ約
  4,046 / 959 本）
- 対象カード行: deck_cards.board IN ('main','commander') かつ card_id IS NOT NULL
- 同一デッキ内の card_id の全ペア（card_id_a < card_id_b）を source 別に集計
- **deck_count >= 2 のみ保存**（1 は統計信号ゼロ・サイズ削減の閾値）
- 冪等: 再実行時は TRUNCATE してから INSERT（DROP はしない）
"""
import time

import psycopg2

from db_config import get_db_config

DECK_COUNT_MIN = 2  # 1同居は統計信号ゼロ・サイズ削減のため足切り

DDL = """
CREATE TABLE IF NOT EXISTS edh_card_cooccurrence_v2 (
  card_id_a  integer NOT NULL,
  card_id_b  integer NOT NULL,
  source     text    NOT NULL,
  deck_count integer NOT NULL,
  PRIMARY KEY (card_id_a, card_id_b, source)
);
CREATE INDEX IF NOT EXISTS idx_edh_cooc_v2_a ON edh_card_cooccurrence_v2 (card_id_a);
CREATE INDEX IF NOT EXISTS idx_edh_cooc_v2_b ON edh_card_cooccurrence_v2 (card_id_b);
"""

# deck_cards を対象条件で絞った上で自己 JOIN → デッキ内ペアを source 別に集計。
# card_id_a < card_id_b の正規順を JOIN 条件で強制。
AGG_SQL = """
INSERT INTO edh_card_cooccurrence_v2 (card_id_a, card_id_b, source, deck_count)
SELECT a.card_id AS card_id_a, b.card_id AS card_id_b, dl.source, COUNT(*) AS deck_count
FROM deck_cards a
JOIN deck_cards b
  ON a.deck_id = b.deck_id
 AND a.card_id < b.card_id
JOIN deck_list dl ON dl.id = a.deck_id
WHERE dl.source IN ('moxfield_edh', 'mtgtop8_edh')
  AND a.board IN ('main', 'commander') AND a.card_id IS NOT NULL
  AND b.board IN ('main', 'commander') AND b.card_id IS NOT NULL
GROUP BY a.card_id, b.card_id, dl.source
HAVING COUNT(*) >= %s
"""


def main():
    t0 = time.time()
    conn = psycopg2.connect(**get_db_config())
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            print("[1/4] DDL (CREATE TABLE IF NOT EXISTS + 索引)...")
            cur.execute(DDL)
            conn.commit()

            print("[2/4] TRUNCATE (冪等の再実行に備える)...")
            cur.execute("TRUNCATE TABLE edh_card_cooccurrence_v2")
            conn.commit()

            print("[3/4] work_mem 引き上げ + 集計 INSERT...")
            cur.execute("SET work_mem = '512MB'")
            cur.execute(AGG_SQL, (DECK_COUNT_MIN,))
            inserted = cur.rowcount
            conn.commit()
            print(f"  inserted rows: {inserted}")

            print("[4/4] ANALYZE...")
            cur.execute("ANALYZE edh_card_cooccurrence_v2")
            conn.commit()
    finally:
        conn.close()

    elapsed = time.time() - t0
    print(f"完了。所要時間: {elapsed:.1f}秒")


if __name__ == "__main__":
    main()
