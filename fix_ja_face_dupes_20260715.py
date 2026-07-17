"""fix_ja_face_dupes_20260715.py — 両面カードの日本語テキスト複製（表//表）を修理

対象: japanese_oracle_text の前半と後半が同一の 53 枚（2026-07-15 本人報告:
ロクの伝説 // アバター・ロク 起点・検出式は下の SQL）。
原資: wg_cache の面別ページ（whisper は面ごとにページがあり、先頭の
テキスト欄が当該面のテキスト＝検証済み）。キャッシュに無い面のみ丁寧に fetch。

規律:
  - 全面が取れて・全部日本語で・前後半が同一でなくなる場合のみ UPDATE
    （推測で埋めない・取れない面は skip 一覧に出す）。
  - --run の前にサーバ stop（正順: stop→書き込み→start・2026-07-13 教訓）。
  - UPDATE 後は rebuild_embed_text.py --update_text/--reembed を
    --card_ids_file（本スクリプトが出力）で部分実行。

使い方:
  python fix_ja_face_dupes_20260715.py           # dry-run（修理案の一覧）
  python fix_ja_face_dupes_20260715.py --run     # DB UPDATE + ids ファイル出力
"""
import argparse
import sys
import time

import psycopg2

sys.path.insert(0, '/mnt/mtg_rag')
from db_config import DB_CONFIG
from scrape_wisdom_guild_full import fetch_html, parse_oracle, SLEEP_SEC
import os

IDS_OUT = '/mnt/mtg_rag/fix_ja_face_dupes_ids.txt'

FIND_SQL = """
    SELECT id, card_name, japanese_oracle_text, oracle_text
    FROM mtg_cards_v2
    WHERE japanese_oracle_text LIKE '%% // %%'
      AND split_part(japanese_oracle_text, ' // ', 1)
          = split_part(japanese_oracle_text, ' // ', 2)
      AND length(split_part(japanese_oracle_text, ' // ', 1)) > 0
    ORDER BY card_name
"""


def repair_one(card_name: str):
    """(new_ja_text|None, reason) — 全面成功時のみテキストを返す。
    whisper は面名どちらでも同一ページ＝表面 dc・裏面 ddc セル（2026-07-15 判明）。
    表面名のページ 1 枚から parse_face_oracles で両面を取る。"""
    from scrape_wisdom_guild_full import cache_path, parse_face_oracles
    faces = card_name.split(' // ')
    front = faces[0]
    cached = os.path.exists(cache_path(front))
    body, err = fetch_html(front)
    if not cached:
        time.sleep(SLEEP_SEC)
    if err or not body:
        return None, f'fetch NG ({front}): {err}'
    parts = parse_face_oracles(body)
    if len(parts) != len(faces):
        return None, f'面欠け: {len(parts)}/{len(faces)} 面しか取れず'
    new_text = ' // '.join(parts)
    if len(set(parts)) == 1:
        return None, '両面同文（whisper 側も同一）'
    return new_text, 'ok'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true')
    args = ap.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(FIND_SQL)
        targets = cur.fetchall()
    print(f'対象: {len(targets)} 枚')

    fixes, skips = [], []
    for cid, name, old, en_text in targets:
        # 英語側が単面表記（出来事のバニラ表面等＝oracle_text に ' // ' なし）なら、
        # 正しい日本語も単面＝複製を剥がすだけ（スクレイプ不要・英語の形式に揃える）
        if ' // ' not in (en_text or ''):
            new_text, reason = old.split(' // ')[0], 'dedupe（英語は単面表記）'
        else:
            new_text, reason = repair_one(name)
        if new_text is None:
            skips.append((cid, name, reason))
            print(f'SKIP {name}: {reason}')
            continue
        fixes.append((cid, name, new_text))
        print(f'OK   {name}')
        print(f'     旧: {old[:70]}')
        print(f'     新: {new_text[:70]}')

    print(f'\n修理可 {len(fixes)} / skip {len(skips)}')

    if not args.run:
        print('dry-run（--run で UPDATE）')
        conn.close()
        return

    with conn.cursor() as cur:
        for cid, _, new_text in fixes:
            cur.execute(
                'UPDATE mtg_cards_v2 SET japanese_oracle_text = %s WHERE id = %s',
                (new_text, cid))
    conn.commit()
    with open(IDS_OUT, 'w') as f:
        f.write('\n'.join(str(cid) for cid, _, _ in fixes))
    print(f'UPDATE {len(fixes)} 行・ids → {IDS_OUT}')
    conn.close()


if __name__ == '__main__':
    main()
