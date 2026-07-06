#!/usr/bin/env python
"""enrich_front_keywords.py — 表面（front face）の生得キーワードだけの導出列を作る。

前提と目的（2026-07-06・本人×Fable）:
  Scryfall の keywords 配列はカード単位＝両面カードでは裏面の能力も混ざる
  （例: 秘密を掘り下げる者の Flying は裏面・変身後のもの）。採点規約は
  「両面は表面の本質で判定」（R8補足b・デルバー裁定）なので、検索の
  キーワード・ハードフィルタも表面基準の列で引く必要がある。

  front_keywords text[] … 単面カード＝keywords のコピー。
                          両面カード＝keywords のうち表面 oracle_text に
                          「キーワード能力行」として載っているものだけ。
判定: 表面テキスト（注釈除去後）の各行を ',' 区切りし、全トークンが
  カード keywords のいずれかに一致（コスト付き "Ward {2}" は前方一致）する行を
  キーワード能力行とみなす。「gains flying」等の付与文はトークンが不一致で弾ける。
不在は NULL 相当（keywords が空/NULL ならそのまま）。冪等: 再実行で全件上書き。
新セット取り込み後は enrich_removal.py と一緒に再実行。"""
import json
import re

import psycopg2
from psycopg2.extras import execute_batch

from db_config import get_db_config


def strip_reminder(t):
    return re.sub(r'\([^)]*\)', '', t or '')


def front_keywords(keywords, faces_json):
    if not keywords:
        return keywords
    faces = faces_json if isinstance(faces_json, list) else None
    if not faces or len(faces) < 2:
        return keywords
    front_text = strip_reminder(faces[0].get('oracle_text') or '')
    kws_lower = {k.lower(): k for k in keywords}
    found = set()
    for line in front_text.split('\n'):
        tokens = [tok.strip() for tok in line.split(',') if tok.strip()]
        if not tokens:
            continue
        matched = []
        for tok in tokens:
            tl = tok.lower()
            hit = next((orig for low, orig in kws_lower.items()
                        if tl == low or tl.startswith(low + ' ')), None)
            if hit is None:
                matched = None
                break
            matched.append(hit)
        if matched:
            found.update(matched)
    return sorted(found) or None


def main():
    cfg = get_db_config()
    conn = psycopg2.connect(**cfg)
    cur = conn.cursor()
    cur.execute("ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS front_keywords text[]")
    cur.execute("CREATE INDEX IF NOT EXISTS mtg_cards_v2_front_keywords_gin "
                "ON mtg_cards_v2 USING gin (front_keywords)")
    cur.execute("SELECT id, keywords, card_faces_json FROM mtg_cards_v2")
    updates = []
    n_multi, n_diff = 0, 0
    for cid, kws, faces in cur.fetchall():
        fk = front_keywords(kws, faces)
        if isinstance(faces, list) and len(faces) >= 2 and kws:
            n_multi += 1
            if sorted(fk or []) != sorted(kws or []):
                n_diff += 1
        updates.append((fk, cid))
    execute_batch(cur, "UPDATE mtg_cards_v2 SET front_keywords = %s WHERE id = %s",
                  updates, page_size=1000)
    conn.commit()
    cur.execute("SELECT count(front_keywords) FROM mtg_cards_v2")
    print(f"populate 完了: 全{len(updates)}件 / front_keywords あり {cur.fetchone()[0]} "
          f"/ 両面でkeywords持ち {n_multi} / うち表面と差分あり {n_diff}")
    conn.close()


if __name__ == '__main__':
    main()
