"""
enrich_scryfall_meta.py — Scryfall の構造化メタデータを mtg_cards_v2 に取り込む手動更新スクリプト。

取り込むフィールド:
  - produced_mana (text[])   : マナ生成（マナクリーチャー判定用・手書きルール不要）
  - edhrec_rank   (integer)  : EDH 人気度ランク（小さいほど人気）
  - game_changer  (boolean)  : Commander ブラケットの高影響カードフラグ
  - image_url     (text)     : カード画像の URL（2026-07-26 追加・デモの見栄え用）。
      **画像そのものは保存しない**——Scryfall CDN への完全 URL 文字列だけ持つ
      （表示はブラウザ→Scryfall の直リンク＝自前の帯域・保管コストゼロ。
      Scryfall が公式に許容する標準的な使い方）。URL は image_uris.normal を
      実データからそのまま保存する（CDN のパス構造を推測して組み立てない）。
      両面カードはトップレベルに image_uris が無く card_faces[0] 側にあるので
      表面を採る（面選定の既存規約と同じ向き）。

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
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS image_url     text;
"""


def _image_url(c: dict):
    """image_uris.normal を取り出す（両面カードは表面）。無ければ None（番兵禁止）。"""
    uris = c.get("image_uris")
    if not uris:
        faces = c.get("card_faces") or []
        if faces and isinstance(faces, list):
            uris = faces[0].get("image_uris")
    return (uris or {}).get("normal")


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
                         c.get("edhrec_rank"), c.get("game_changer"),
                         _image_url(c)))
    print(f"Scryfall から {len(rows)} 件のメタを読み込み")

    # 一時テーブルに入れて UPDATE FROM（33k行を1文で更新）
    cur.execute("""
        CREATE TEMP TABLE _meta(
            name text, produced_mana text[], edhrec_rank int, game_changer bool,
            image_url text
        ) ON COMMIT DROP;
    """)
    execute_values(
        cur,
        "INSERT INTO _meta(name, produced_mana, edhrec_rank, game_changer, image_url) VALUES %s",
        rows, page_size=1000,
    )
    cur.execute("""
        UPDATE mtg_cards_v2 c
        SET produced_mana = s.produced_mana,
            edhrec_rank   = s.edhrec_rank,
            game_changer  = s.game_changer,
            image_url     = s.image_url
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
        ("image_url 保有",      "SELECT count(*) FROM mtg_cards_v2 WHERE image_url IS NOT NULL"),
    ]:
        cur.execute(sql)
        print(f"  {label}: {cur.fetchone()[0]}")
    cur.execute("SELECT card_name, produced_mana, edhrec_rank, game_changer "
                "FROM mtg_cards_v2 WHERE card_name='Llanowar Elves'")
    print("  Llanowar Elves:", cur.fetchone())

    conn.close()


if __name__ == "__main__":
    main()
