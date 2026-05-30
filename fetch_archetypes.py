"""
fetch_archetypes.py — MTGTop8 からアーキタイプ名を取得して DB に格納
=====================================================================
既存の deck_list テーブルの source_url からイベントIDとデッキIDを取得し、
MTGTop8 のイベントページからアーキタイプ名を取得して
deck_archetypes テーブルに格納する。

.dec の再ダウンロードは不要。イベントページのみ取得。

使い方:
  python fetch_archetypes.py            # 全件取得
  python fetch_archetypes.py --status   # 取得状況確認
  python fetch_archetypes.py --dry_run  # 取得せず確認のみ
"""

import argparse
import re
import time
import psycopg2
import requests
from tqdm import tqdm

from db_config import DB_CONFIG

BASE_URL         = "https://www.mtgtop8.com"
REQUEST_INTERVAL = 2.0
HEADERS = {
    "User-Agent": "MTG-RAG-Research-Bot/1.0 (educational project)",
    "Accept-Language": "en-US,en;q=0.9",
}


# ─── テーブル作成 ─────────────────────────────────────────────

def create_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE deck_list
            ADD COLUMN IF NOT EXISTS archetype TEXT;
        """)
    conn.commit()
    print("deck_list.archetype カラム確認完了")


# ─── イベントページからアーキタイプ取得 ──────────────────────

def fetch_archetypes_from_event(event_id: int) -> dict[int, str]:
    """
    イベントページから {deck_id: archetype} の辞書を返す。
    """
    url  = f"{BASE_URL}/event?e={event_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        time.sleep(REQUEST_INTERVAL)
        if resp.status_code != 200:
            return {}
    except requests.RequestException:
        time.sleep(REQUEST_INTERVAL)
        return {}

    html = resp.text

    # href=?e=XXXX&d=YYYY&f=ZZ>アーキタイプ名< のパターン
    matches = re.findall(
        r'href=\?e=\d+&d=(\d+)&f=[A-Z]+>([^<]+)<',
        html
    )

    result = {}
    for deck_id_str, archetype in matches:
        # &rarr; 等の不要なエントリを除外
        if archetype.startswith('&') or not archetype.strip():
            continue
        deck_id = int(deck_id_str)
        archetype = archetype.strip()
        # 同じ deck_id が複数ある場合は最初のものを使用
        if deck_id not in result:
            result[deck_id] = archetype

    return result


# ─── メイン処理 ───────────────────────────────────────────────

def run(dry_run: bool = False):
    conn = psycopg2.connect(**DB_CONFIG)
    create_table(conn)

    # 未取得の deck_list を取得
    with conn.cursor() as cur:
        cur.execute("""
            SELECT deck_name, tournament_event_id, source_url
            FROM deck_list
            WHERE source = 'mtgtop8'
              AND archetype IS NULL
              AND tournament_event_id IS NOT NULL
            ORDER BY tournament_event_id
        """)
        rows = cur.fetchall()

    print(f"未取得デッキ数: {len(rows)}")

    if dry_run:
        print("dry_run モード: 実際には取得しません")
        for row in rows[:5]:
            print(f"  {row[0]} / event={row[1]}")
        conn.close()
        return

    # イベントID ごとにまとめる
    event_to_decks: dict[int, list[tuple[str, str]]] = {}
    for deck_name, event_id, source_url in rows:
        # source_url から deck_id を抽出
        m = re.search(r'd=(\d+)', source_url or "")
        if not m:
            continue
        deck_id = int(m.group(1))
        if event_id not in event_to_decks:
            event_to_decks[event_id] = []
        event_to_decks[event_id].append((deck_name, deck_id))

    print(f"取得対象イベント数: {len(event_to_decks)}")

    inserted  = 0
    not_found = 0

    for event_id, deck_list in tqdm(event_to_decks.items(),
                                     desc="イベント取得", mininterval=5):
        archetypes = fetch_archetypes_from_event(event_id)

        with conn.cursor() as cur:
            for deck_name, deck_id in deck_list:
                archetype = archetypes.get(deck_id)
                if not archetype:
                    not_found += 1
                    continue

                cur.execute("""
                    UPDATE deck_list
                    SET archetype = %s
                    WHERE deck_name = %s;
                """, (archetype, deck_name))
                inserted += 1

        conn.commit()

    conn.close()
    print(f"\n完了: {inserted} 件登録 / {not_found} 件未取得")


# ─── 状況確認 ─────────────────────────────────────────────────

def check_status():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM deck_list WHERE source='mtgtop8'")
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM deck_list 
            WHERE source='mtgtop8' AND archetype IS NOT NULL
        """)
        fetched = cur.fetchone()[0]

        # アーキタイプ別件数TOP10
        cur.execute("""
            SELECT archetype, COUNT(*) as cnt
            FROM deck_list
            WHERE source = 'mtgtop8'
              AND archetype IS NOT NULL
            GROUP BY archetype
            ORDER BY cnt DESC
            LIMIT 10
        """)
        top_archetypes = cur.fetchall()

        # 稲妻が入っているデッキのアーキタイプ
        cur.execute("""
            SELECT d.archetype, COUNT(*) as cnt
            FROM deck_cards dc
            JOIN deck_list d ON dc.deck_id = d.id
            WHERE dc.card_name = 'Lightning Bolt'
              AND d.source = 'mtgtop8'
              AND d.archetype IS NOT NULL
              AND dc.board = 'main'
            GROUP BY d.archetype
            ORDER BY cnt DESC
            LIMIT 10
        """)
        bolt_archetypes = cur.fetchall()

    conn.close()

    print(f"mtgtop8 デッキ総数: {total}")
    print(f"アーキタイプ取得済み: {fetched} ({fetched/total*100:.1f}%)")

    print(f"\nアーキタイプ別件数 TOP10:")
    for arch, cnt in top_archetypes:
        print(f"  {cnt:4d}件  {arch}")

    if bolt_archetypes:
        print(f"\nLightning Bolt が入っているデッキ TOP10:")
        for arch, cnt in bolt_archetypes:
            print(f"  {cnt:4d}件  {arch}")


# ─── エントリーポイント ───────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--status",  action="store_true")
    parser.add_argument("--dry_run", action="store_true")
    args = parser.parse_args()

    if args.status:
        check_status()
    else:
        run(dry_run=args.dry_run)
