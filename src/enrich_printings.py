#!/usr/bin/env python3
"""enrich_printings.py — 全印刷バルク（all_cards）から印刷由来の列を集約する（2026-07-27）。

充填する列（どちらも 2026-07-27 のセット検索設計・WORKLOG 参照）:
  - set_codes    text[] : そのカードが**一度でも収録された全セット**のコード集合。
      oracle_cards 由来の set_code（代表印刷のみ）は再録で上書きされるため、
      「灯争大戦のプレインズウォーカー」で看板 PW が全滅する偽陰性が実測で出た
      （Nicol Bolas→rvr 等）。全印刷の集合なら取りこぼさない。
      デジタル専用印刷（Arena/MTGO）は除外＝紙のセット名辞書と噛み合わせるため。
  - image_url_ja text   : 日本語印刷のカード画像 URL（lang="ja" の印刷のうち
      released_at 最新のもの・両面カードは表面）。表記切替「日」モードで使う。
      日本語印刷が無いカードは NULL（フロントが英語画像へフォールバック）。

設計メモ:
  - 入力は /mnt/new_hdd/all_cards_scryfall.json（2.4GB・全印刷・全言語・1 行 1 カード）。
    ストリームで舐める＝メモリに全部載せない。
  - 行数は 31,635 のまま＝**過去の重複排除（オラクル単位 1 行）は崩さない**。
    印刷履歴が属性として増えるだけ（本人裁定 2026-07-27「set_codes を配列で持てばいい」）。
  - 既存の text[] 列（front_keywords 等）と同型＝非正規化が正（2026-06-24 裁定）。
  - 冪等: 値が変わる行だけ UPDATE。バルクを新版に差し替えて再実行すれば追いつく。

使い方:
    python enrich_printings.py [all_cards のパス]
"""
import json
import sys

import psycopg2
from psycopg2.extras import execute_batch

from db_config import get_db_config

BULK = sys.argv[1] if len(sys.argv) > 1 else "/mnt/new_hdd/all_cards_scryfall.json"

ALTER = """
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS set_codes    text[];
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS image_url_ja text;
"""


def _face_image(c: dict):
    """image_uris.normal（両面カードは表面）。enrich_scryfall_meta と同じ規則。"""
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

    # DB 側の対象カード名（コア 31,635 のみ集約＝バルクの 9 万種全部は持たない）
    cur.execute("SELECT card_name FROM mtg_cards_v2")
    targets = {r[0] for r in cur.fetchall()}
    print(f"対象カード名: {len(targets)}")

    sets: dict[str, set] = {}          # name -> set codes
    ja_img: dict[str, tuple] = {}      # name -> (released_at, url)
    n_lines = 0
    with open(BULK, encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            n_lines += 1
            try:
                c = json.loads(line)
            except Exception:
                continue
            name = c.get("name")
            if name not in targets:
                continue
            # set_codes: デジタル専用印刷は除外（紙のセット名辞書と噛み合わせる）
            if not c.get("digital"):
                sc = c.get("set")
                if sc:
                    sets.setdefault(name, set()).add(sc)
            # image_url_ja: 日本語印刷のうち released_at 最新を採る
            if c.get("lang") == "ja":
                url = _face_image(c)
                if url:
                    rel = c.get("released_at") or ""
                    cur_best = ja_img.get(name)
                    if cur_best is None or rel > cur_best[0]:
                        ja_img[name] = (rel, url)
            if n_lines % 500000 == 0:
                print(f"  …{n_lines:,} 行走査")

    print(f"走査 {n_lines:,} 行 / set_codes 対象 {len(sets):,} 枚 / 日本語画像 {len(ja_img):,} 枚")

    # 差分だけ UPDATE（冪等）
    cur.execute("SELECT id, card_name, set_codes, image_url_ja FROM mtg_cards_v2")
    updates = []
    for cid, name, old_sets, old_ja in cur.fetchall():
        new_sets = sorted(sets.get(name, set())) or None
        new_ja = ja_img.get(name, (None, None))[1]
        if (old_sets or None) != new_sets or (old_ja or None) != (new_ja or None):
            updates.append((new_sets, new_ja, cid))
    print(f"差分: {len(updates):,} 行")
    execute_batch(cur,
                  "UPDATE mtg_cards_v2 SET set_codes=%s, image_url_ja=%s WHERE id=%s",
                  updates, page_size=1000)
    conn.commit()

    # GIN 索引（set_codes && ARRAY[...] を索引で解く・front_keywords と同じ流儀)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_set_codes"
                " ON mtg_cards_v2 USING gin(set_codes)")
    conn.commit()

    for label, sql in [
        ("set_codes 保有",    "SELECT count(*) FROM mtg_cards_v2 WHERE set_codes IS NOT NULL"),
        ("image_url_ja 保有", "SELECT count(*) FROM mtg_cards_v2 WHERE image_url_ja IS NOT NULL"),
    ]:
        cur.execute(sql)
        print(f"  {label}: {cur.fetchone()[0]:,}")
    cur.execute("SELECT card_name, set_codes FROM mtg_cards_v2"
                " WHERE card_name='Nicol Bolas, Dragon-God'")
    print("  検分（灯争大戦の看板）:", cur.fetchone())
    conn.close()


if __name__ == "__main__":
    main()
