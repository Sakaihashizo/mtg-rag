"""
scrape_mtgtop8.py — MTGTop8 大会デッキスクレイパー
====================================================
礼儀正しいスクレイピング:
  - リクエスト間隔: 2秒
  - User-Agent を明示
  - 中断・再開対応（取得済みイベントはスキップ）

取得フロー:
  1. /format?f={FORMAT}&meta={META} → イベントID一覧
  2. /event?e={EVENT_ID}            → デッキID一覧
  3. /dec?d={DECK_ID}               → カードリスト（.dec形式）

テーブル構成:
  deck_list  （既存・source='mtgtop8' で追加）
  deck_cards （既存・同上）

使い方:
  # モダン 2024年分を取得
  python scrape_mtgtop8.py --format MO --meta 276 --year 2024

  # スタンダード 2024年分
  python scrape_mtgtop8.py --format ST --meta 276 --year 2024

  # 取得状況確認
  python scrape_mtgtop8.py --status

フォーマットコード:
  ST=Standard, PI=Pioneer, MO=Modern, LE=Legacy, VI=Vintage
  PAU=Pauper, EDH=Duel Commander

メタコード（モダン）:
  276=2024, 315=2025, 246=2023, 236=2022
"""

import argparse
import re
import time
import psycopg2
import psycopg2.extras
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

DB_CONFIG = {
    "dbname": "rag_dev",
    "user": "devuser",
    "password": "***REMOVED***",
    "host": "localhost",
    "port": 5435,
}

BASE_URL       = "https://www.mtgtop8.com"
REQUEST_INTERVAL = 2.0  # 秒（礼儀正しいスクレイピング）
SOURCE         = "mtgtop8"

HEADERS = {
    "User-Agent": "MTG-RAG-Research-Bot/1.0 (educational project; contact via GitHub)",
    "Accept-Language": "en-US,en;q=0.9",
}

FORMAT_NAMES = {
    "ST": "Standard", "PI": "Pioneer", "MO": "Modern",
    "LE": "Legacy",   "VI": "Vintage", "PAU": "Pauper",
    "EDH": "Duel Commander",
}


# ─── HTTP ヘルパー ────────────────────────────────────────────

def fetch(url: str, retries: int = 3) -> str | None:
    """礼儀正しい HTTP GET（インターバル付き・リトライあり）"""
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            time.sleep(REQUEST_INTERVAL)
            if resp.status_code == 200:
                return resp.text
            print(f"  HTTP {resp.status_code}: {url}")
            return None
        except requests.RequestException as e:
            print(f"  通信エラー（{attempt+1}/{retries}）: {e}")
            time.sleep(REQUEST_INTERVAL * 2)
    return None


# ─── スクレイピング ───────────────────────────────────────────

def get_event_ids(format_code: str, meta: int) -> list[int]:
    """イベント一覧ページからイベントIDを取得する"""
    url  = f"{BASE_URL}/format?f={format_code}&meta={meta}&a="
    html = fetch(url)
    if not html:
        return []

    # href=event?e=XXXX の数字を抽出
    event_ids = re.findall(r'href=["\']?event\?e=(\d+)', html)
    unique_ids = list(dict.fromkeys(int(e) for e in event_ids))
    return unique_ids


def get_deck_ids(event_id: int) -> list[tuple[int, str, str]]:
    """
    イベントページからデッキIDを取得する。
    戻り値: [(deck_id, deck_name, player_name), ...]
    """
    url  = f"{BASE_URL}/event?e={event_id}"
    html = fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    # イベント名を取得
    title_div = soup.find("div", class_="event_title")
    event_name = title_div.get_text(strip=True) if title_div else f"Event {event_id}"

    # デッキIDとデッキ名を取得
    # パターン: href=?e=1&d=101680&f=VI または href=event?e=1&d=101680&f=VI
    deck_links = re.findall(
        r'href=["\']?\?e=\d+&d=(\d+)&f=[A-Z]+["\']?[^>]*>([^<]+)<',
        html
    )

    # プレイヤー名を取得
    player_links = re.findall(
        r'href=["\']?search\?player=([^"\'&>]+)["\']?[^>]*class=["\']?player["\']?',
        html
    )

    results = []
    for i, (deck_id, deck_name) in enumerate(deck_links):
        player = player_links[i].replace("+", " ") if i < len(player_links) else ""
        results.append((int(deck_id), deck_name.strip(), player.strip()))

    return results


def parse_dec(dec_text: str) -> tuple[str, str, list[tuple[str, int, str]]]:
    """
    .dec テキストをパースしてカードリストを返す。
    戻り値: (deck_name, format_name, [(card_name, count, board), ...])
    """
    deck_name   = ""
    format_name = ""
    cards       = []

    for line in dec_text.splitlines():
        line = line.strip()
        if not line:
            continue

        # コメント行からメタ情報を抽出
        if line.startswith("//"):
            if "NAME" in line:
                deck_name = line.split(":", 1)[-1].strip()
            elif "FORMAT" in line:
                format_name = line.split(":", 1)[-1].strip()
            continue

        # サイドボード
        if line.upper().startswith("SB:"):
            line  = line[3:].strip()
            board = "side"
        else:
            board = "main"

        # "4 [MR] Counterspell" または "4 Counterspell"
        m = re.match(r'^(\d+)\s+(?:\[[^\]]+\]\s+)?(.+)$', line)
        if m:
            count     = int(m.group(1))
            card_name = m.group(2).strip()
            # 分割カード "Fire // Ice" → "Fire // Ice" のままで OK
            cards.append((card_name, count, board))

    return deck_name, format_name, cards


def get_deck_cards(deck_id: int) -> list[tuple[str, int, str]]:
    """デッキIDから .dec を取得してカードリストを返す"""
    url      = f"{BASE_URL}/dec?d={deck_id}"
    dec_text = fetch(url)
    if not dec_text:
        return []
    _, _, cards = parse_dec(dec_text)
    return cards


# ─── DB 操作 ──────────────────────────────────────────────────

def get_scraped_event_ids(conn) -> set[int]:
    """既に取り込み済みのイベントIDを取得（再開用）"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT tournament_event_id FROM deck_list
            WHERE source = %s AND tournament_event_id IS NOT NULL
        """, (SOURCE,))
        return {row[0] for row in cur.fetchall()}


def ensure_columns(conn):
    """deck_list に大会データ用カラムを追加する"""
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE deck_list
            ADD COLUMN IF NOT EXISTS tournament_name  TEXT,
            ADD COLUMN IF NOT EXISTS tournament_date  DATE,
            ADD COLUMN IF NOT EXISTS placement        INTEGER,
            ADD COLUMN IF NOT EXISTS player_name      TEXT,
            ADD COLUMN IF NOT EXISTS format_name      TEXT,
            ADD COLUMN IF NOT EXISTS source_url       TEXT,
            ADD COLUMN IF NOT EXISTS tournament_event_id INTEGER;
        """)
    conn.commit()
    print("カラム追加完了")


def save_deck(conn, event_id: int, event_name: str,
              deck_id: int, deck_name: str, player_name: str,
              format_code: str, cards: list[tuple[str, int, str]],
              archetype: str = "") -> bool:
    """デッキを DB に保存する。重複の場合は False を返す"""
    unique_name = f"mtgtop8_{event_id}_{deck_id}"
    source_url  = f"{BASE_URL}/event?e={event_id}&d={deck_id}&f={format_code}"

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO deck_list
                (deck_name, set_code, source, tournament_name,
                 player_name, format_name, source_url, tournament_event_id,
                 archetype)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (deck_name) DO NOTHING
            RETURNING id;
        """, (unique_name, format_code, SOURCE, event_name,
              player_name, FORMAT_NAMES.get(format_code, format_code),
              source_url, event_id, archetype or None))
        result = cur.fetchone()

    if result is None:
        return False

    deck_db_id = result[0]

    if cards:
        psycopg2.extras.execute_values(
            conn.cursor(),
            """
            INSERT INTO deck_cards (deck_id, card_name, count, board)
            VALUES %s
            """,
            [(deck_db_id, name, count, board) for name, count, board in cards],
        )

    conn.commit()
    return True


# ─── メイン処理 ───────────────────────────────────────────────

def scrape(format_code: str, meta: int, year: int):
    conn = psycopg2.connect(**DB_CONFIG)
    ensure_columns(conn)

    # 取得済みイベントをスキップ
    scraped = get_scraped_event_ids(conn)

    print(f"フォーマット: {FORMAT_NAMES.get(format_code, format_code)} "
          f"(meta={meta}, year={year})")
    print(f"イベントID取得中...")

    event_ids = get_event_ids(format_code, meta)
    print(f"イベント数: {len(event_ids)}  取得済み: {len(scraped)}")

    new_events  = [e for e in event_ids if e not in scraped]
    print(f"新規取得対象: {len(new_events)} イベント")

    total_decks  = 0
    total_cards  = 0

    for event_id in tqdm(new_events, desc="イベント処理"):
        deck_infos = get_deck_ids(event_id)
        if not deck_infos:
            continue

        event_name = f"MTGTop8 Event {event_id} ({year} {format_code})"

        for deck_id, archetype, player_name in deck_infos:
            cards = get_deck_cards(deck_id)
            if not cards:
                continue

            saved = save_deck(
                conn, event_id, event_name,
                deck_id, archetype, player_name,
                format_code, cards,
                archetype=archetype,
            )
            if saved:
                total_decks += 1
                total_cards += len(cards)

    conn.close()
    print(f"\n完了: {total_decks} デッキ / {total_cards} カード行を取り込みました")


# ─── 状況確認 ─────────────────────────────────────────────────

def check_status():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source, COUNT(*) FROM deck_list GROUP BY source ORDER BY COUNT(*) DESC
        """)
        by_source = cur.fetchall()

        cur.execute("""
            SELECT format_name, COUNT(*) FROM deck_list
            WHERE source = 'mtgtop8'
            GROUP BY format_name ORDER BY COUNT(*) DESC
        """)
        by_format = cur.fetchall()

        # Counterspell の共起（大会データのみ）
        cur.execute("""
            SELECT
                CASE WHEN card_name_a = 'Counterspell'
                     THEN card_name_b ELSE card_name_a END AS partner,
                co_count
            FROM card_cooccurrence
            WHERE (card_name_a = 'Counterspell' OR card_name_b = 'Counterspell')
              AND source = 'mtgtop8'
            ORDER BY co_count DESC
            LIMIT 10
        """)
        cooc = cur.fetchall()

    conn.close()

    print("=== ソース別デッキ数 ===")
    for source, count in by_source:
        print(f"  {source}: {count}")

    print("\n=== フォーマット別（mtgtop8）===")
    for fmt, count in by_format:
        print(f"  {fmt}: {count}")

    if cooc:
        print("\n=== Counterspell 共起 TOP10（大会データ）===")
        for partner, count in cooc:
            print(f"  {count:3d}回  {partner}")
    else:
        print("\n大会データの共起集計は --cooccur で実行してください")


# ─── エントリーポイント ───────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", default="MO",
                        choices=["ST","PI","MO","LE","VI","PAU","EDH"],
                        help="フォーマットコード")
    parser.add_argument("--meta",   type=int, default=276,
                        help="メタコード（年別ID）")
    parser.add_argument("--year",   type=int, default=2024,
                        help="年（ログ表示用）")
    parser.add_argument("--status", action="store_true",
                        help="取得状況確認")
    args = parser.parse_args()

    if args.status:
        check_status()
    else:
        scrape(args.format, args.meta, args.year)
