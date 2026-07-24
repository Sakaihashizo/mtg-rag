"""
scrape_moxfield.py — Moxfield 公開デッキスクレイパー（多人数 EDH 向け）
====================================================================
Moxfield は private API（無文書・無保証・予告なく変更されうる）。
2026-07-21 に Rob 'Kerakis' Upchurch（Director of Community）から
専用 User-Agent を条件付きで発行された（非営利・1req/sec厳守・UA秘密保持）。

このスクリプトの根拠は Moxfield 公式ドキュメントではなく、
コミュニティによる非公式リバースエンジニアリング（spoved/moxfield.cr 等）を
出発点にした live 検分（--smoke-test）。fmt の実際の文字列や応答形は
smoke-test で確認してから本走に進むこと。

礼儀正しいスクレイピング:
  - リクエスト間隔: 2秒（Rob 指定の 1req/sec の自主的に半分）
  - User-Agent は ~/.moxfield_ua から読む（chmod 600・リポジトリ外・
    ログ/print/例外メッセージに値を絶対に出さない）
  - 中断・再開対応（既存 source_url はスキップ）

取得フロー:
  1. GET /v2/decks/search?fmt=...&commanderCardId=...&pageNumber=...
     → デッキメタデータ一覧（publicId・format・likeCount 等。カードリストは無し）
  2. GET /v2/decks/all/{publicId}
     → デッキ全体（mainboard/commanders/sideboard の Hash(name -> Entry)）

テーブル構成:
  deck_list  （既存・source='moxfield_edh' で追加）
  deck_cards （既存・同上、board は main/side/commander）

design note（実測で確定した点・2026-07-21 追加検分）:
  - fmt='commander' で確認済み（Duel Commander とは別）。sortType='views' も実測で
    降順ソート確認済み。
  - **bracket 情報は /v2/decks/search の DeckDatum にだけ載る（/v2/decks/all の
    フル取得には bracket/isLegal フィールドが存在しない＝生JSON実測で確認済み）**。
    そのため候補選定は search 段階で bracket を確定させ、フル取得後の save_deck に
    引数で渡す（フル取得結果からは復元できない）。
  - **bracket は 1〜5 の5段階が実在**（WotC公式は1〜4=Exhibition/Core/Upgraded/
    Optimized。5 は Moxfield 独自の cEDH 拡張とみられる＝実測で bracket=5 かつ
    hubNames=["Competitive"] のデッキを複数確認）。grading_conventions.md R13補足a
    の「ブラケット文言」は公式1〜4 前提の設計＝5 を含めるかは公式の外なので**本人
    裁定待ち**（既定は本人指示に従い5段階とも均等に取得・後で SQL 側で bracket<=4
    に絞ることもできる設計＝実データは残す）。
  - bracket は検索クエリのパラメータとしては機能しない（bracket=1 を渡しても
    無視されて全件返る＝実測確認済み）。**取得側でのフィルタは不可能・結果を
    client 側でバケット分けするしかない**。
  - 「完成されたデッキ」の判定は search 結果の `isLegal` フィールド（true のみ採用）
    を使う。デッキが Commander の singleton legal 建築（100枚・禁止カード無し等）
    を満たすかの構造化フラグ＝R9 の「crisp な属性は厳格に」の一族。
  - tournament_name/tournament_date/placement は Moxfield に対応概念が無い
    ため NULL のまま（不在は NULL・番兵にしない）。player_name はデッキ
    投稿者の Moxfield ユーザー名（実データなので NULL にしない）。
  - archetype 列（MTGTop8 の大会公式アーキタイプ名）に相当する概念が無い
    ので NULL のまま。hub_names は捨てず raw JSON ログにだけ残す（列は作らない）。
  - **bracket は deck_list に新規列として保存**（ensure_columns で
    ADD COLUMN IF NOT EXISTS・他 source は NULL のまま・既存踏襲の idiom）。

使い方:
  # 生死確認・実データ形を見るだけ（DB に書かない・数コールのみ）
  python scrape_moxfield.py --smoke-test

  # 取得状況確認
  python scrape_moxfield.py --status

  # 本走（本人 GO 後）: bracket 1〜5 それぞれ MostView 上位から100件ずつ
  python scrape_moxfield.py --sample-by-bracket --per-bracket 100 --brackets 1,2,3,4,5
"""

import argparse
import os
import sys
import time

import psycopg2
import psycopg2.extras
import requests

from db_config import DB_CONFIG

API_SCHEME = "https"
API_HOST = "api2.moxfield.com"
BASE_URL = f"{API_SCHEME}://{API_HOST}"
DECK_SEARCH_PATH = "/v2/decks/search"
DECK_ALL_PATH = "/v2/decks/all"
CARD_SEARCH_PATH = "/v2/cards/search"

REQUEST_INTERVAL = 2.0  # 秒（Rob 指定 1req/sec の自主的に半分）
UA_FILE = os.path.expanduser("~/.moxfield_ua")
SOURCE = "moxfield_edh"

BOARD_MAP = {
    "mainboard": "main",
    "sideboard": "side",
    "commanders": "commander",
}

# Moxfield の生 fmt 文字列 → card_format_strength/edh_card_strength の format_name
# （既存の Standard/Pioneer/.../Duel Commander と同じ表記規則に揃える）。
FORMAT_NAMES = {
    "commander": "Commander",
}

_last_request_at = 0.0


def load_user_agent() -> str:
    """~/.moxfield_ua から UA を読む。値は絶対にログ・例外メッセージに出さない。"""
    if not os.path.exists(UA_FILE):
        raise SystemExit(f"UA ファイルが無い: {UA_FILE}（値は表示しません）")
    with open(UA_FILE, "r") as f:
        ua = f.read().strip()
    if not ua:
        raise SystemExit(f"UA ファイルが空: {UA_FILE}")
    return ua


def _headers() -> dict:
    return {
        "User-Agent": load_user_agent(),
        "Accept": "application/json",
    }


def _throttled_get(path: str, params: dict | None = None, retries: int = 2):
    """礼儀正しい GET（間隔厳守・リトライあり）。UA は例外メッセージに出さない。"""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)

    url = f"{BASE_URL}{path}"
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=_headers(), timeout=15)
            _last_request_at = time.monotonic()
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (401, 403):
                raise SystemExit(
                    f"認証エラー status={resp.status_code} path={path}"
                    "（UA が拒否された可能性・値はここに表示しません）"
                )
            last_exc = RuntimeError(f"status={resp.status_code} path={path}")
        except requests.RequestException as e:
            last_exc = e
        time.sleep(REQUEST_INTERVAL)
    raise last_exc


def search_decks(fmt: str | None = None, commander_card_id: str | None = None,
                  page: int = 1, size: int = 100,
                  sort_type: str = "updated", sort_direction: str = "Descending") -> dict:
    params = {
        "pageNumber": page,
        "pageSize": size,
        "sortType": sort_type,
        "sortDirection": sort_direction,
    }
    if fmt:
        params["fmt"] = fmt
    if commander_card_id:
        params["commanderCardId"] = commander_card_id
    return _throttled_get(DECK_SEARCH_PATH, params)


def get_deck(public_id: str) -> dict:
    return _throttled_get(f"{DECK_ALL_PATH}/{public_id}")


def search_cards(query: str) -> dict:
    return _throttled_get(CARD_SEARCH_PATH, {"q": query})


# ─── DB 操作 ──────────────────────────────────────────────────

def ensure_columns(conn):
    """deck_list に bracket 列を追加する（他 source は NULL のまま・既存踏襲の idiom）"""
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE deck_list ADD COLUMN IF NOT EXISTS bracket INTEGER;"
        )
    conn.commit()


def get_scraped_public_ids(conn, source: str = SOURCE) -> set[str]:
    """既に取り込み済みの publicId を取得（deck_name= f"moxfield_{public_id}" から復元）"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT deck_name FROM deck_list WHERE source = %s",
            (source,),
        )
        prefix = "moxfield_"
        return {
            row[0][len(prefix):] for row in cur.fetchall()
            if row[0].startswith(prefix)
        }


def _extract_cards(deck_json: dict) -> list[tuple[str, int, str]]:
    """mainboard/sideboard/commanders の Hash(name -> Entry) をフラット化"""
    cards = []
    for board_key, board_label in BOARD_MAP.items():
        section = deck_json.get(board_key) or {}
        for card_name, entry in section.items():
            qty = entry.get("quantity", 1)
            cards.append((card_name, qty, board_label))
    return cards


def save_deck(conn, deck_json: dict, bracket: int | None = None, source: str = SOURCE) -> bool:
    """デッキを DB に保存する。重複の場合は False を返す

    bracket は search 段階でしか取れない（/v2/decks/all のフル取得には無い・
    実測確認済み）ため呼び出し側から渡す。
    """
    public_id = deck_json["publicId"]
    unique_name = f"moxfield_{public_id}"
    source_url = deck_json.get("publicUrl") or f"https://moxfield.com/decks/{public_id}"
    deck_name = deck_json.get("name") or unique_name
    raw_format = deck_json.get("format")
    format_name = FORMAT_NAMES.get(raw_format, raw_format)
    author = (deck_json.get("createdByUser") or {}).get("userName")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO deck_list
                (deck_name, set_code, source, tournament_name, tournament_date,
                 player_name, format_name, source_url, tournament_event_id,
                 archetype, bracket)
            VALUES (%s, %s, %s, NULL, NULL, %s, %s, %s, NULL, NULL, %s)
            ON CONFLICT (deck_name) DO NOTHING
            RETURNING id;
            """,
            (unique_name, format_name, source, author, format_name, source_url, bracket),
        )
        result = cur.fetchone()

    if result is None:
        return False

    deck_db_id = result[0]
    cards = _extract_cards(deck_json)
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
    _ = deck_name  # (deck_name は unique_name に折り込み済み・生名は source_url 経由で辿れる)
    return True


# ─── smoke test（DB に書かない・数コールのみ） ──────────────────

def smoke_test(fmt_candidates: list[str]):
    print(f"UA ファイル: {UA_FILE}（値は表示しません）")
    print(f"リクエスト間隔: {REQUEST_INTERVAL}秒\n")

    for fmt in fmt_candidates:
        print(f"--- fmt='{fmt}' で /v2/decks/search を1件試行 ---")
        try:
            resp = search_decks(fmt=fmt, page=1, size=3)
        except SystemExit as e:
            print(f"  認証/致命的エラー: {e}")
            return
        except Exception as e:
            print(f"  失敗: {e}")
            continue
        data = resp.get("data", [])
        print(f"  totalResults={resp.get('totalResults')} 件数(このページ)={len(data)}")
        for d in data:
            print(f"    id={d.get('id')} name={d.get('name')!r} format={d.get('format')!r} "
                  f"publicId={d.get('publicId')} likes={d.get('likeCount')} views={d.get('viewCount')}")
        if data:
            sample_id = data[0]["publicId"]
            print(f"  → 1件フル取得を試す（publicId={sample_id}）")
            try:
                deck = get_deck(sample_id)
            except Exception as e:
                print(f"    フル取得失敗: {e}")
                continue
            commanders = list((deck.get("commanders") or {}).keys())
            mainboard_count = deck.get("mainboardCount")
            print(f"    name={deck.get('name')!r} format={deck.get('format')!r} "
                  f"mainboardCount={mainboard_count} commanders={commanders} "
                  f"hubs={[h.get('name') for h in deck.get('hubs', [])]}")
        print()


def status():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM deck_list WHERE source = %s", (SOURCE,))
        n_decks = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM deck_cards dc JOIN deck_list dl ON dc.deck_id = dl.id "
            "WHERE dl.source = %s",
            (SOURCE,),
        )
        n_cards = cur.fetchone()[0]
    conn.close()
    print(f"source={SOURCE}: デッキ {n_decks} / deck_cards 行 {n_cards}")


# ─── bracket サンプリング ─────────────────────────────────────

def sample_by_bracket(fmt: str, brackets: list[int], per_bracket: int,
                       sort_type: str = "views", max_pages: int = 300) -> dict[int, list[dict]]:
    """MostView 順に search をページングし、bracket ごとに isLegal=True の
    デッキを per_bracket 件集める（bracket は search パラメータでは絞れない
    ＝実測確認済み・client 側でバケット分けするしかない）。

    戻り値: {bracket: [DeckDatum, ...]}（各バケット最大 per_bracket 件）
    """
    buckets: dict[int, list[dict]] = {b: [] for b in brackets}
    target_set = set(brackets)
    page = 1
    scanned = 0

    while page <= max_pages:
        if all(len(buckets[b]) >= per_bracket for b in brackets):
            break
        resp = search_decks(fmt=fmt, page=page, size=100, sort_type=sort_type)
        data = resp.get("data", [])
        if not data:
            print(f"  page={page}: データなし・打ち切り")
            break
        for d in data:
            scanned += 1
            if not d.get("isLegal"):
                continue
            b = d.get("bracket")
            if b in target_set and len(buckets[b]) < per_bracket:
                buckets[b].append(d)
        done = {b: len(buckets[b]) for b in brackets}
        print(f"  page={page} 走査累計={scanned} 充足状況={done}")
        page += 1
        if page > resp.get("totalPages", page):
            print("  最終ページ到達")
            break

    return buckets


# ─── メイン処理 ───────────────────────────────────────────────

def run_bracket_batch(fmt: str, brackets: list[int], per_bracket: int, sort_type: str, max_pages: int):
    conn = psycopg2.connect(**DB_CONFIG)
    ensure_columns(conn)
    scraped = get_scraped_public_ids(conn)
    print(f"既存 {len(scraped)} 件はスキップ対象\n")

    print(f"--- bracket 候補収集（fmt={fmt} sort={sort_type} 目標={per_bracket}件/bracket） ---")
    buckets = sample_by_bracket(fmt, brackets, per_bracket, sort_type, max_pages)
    for b in brackets:
        print(f"  bracket={b}: 候補 {len(buckets[b])}/{per_bracket} 件")
    print()

    saved_total = 0
    skipped_total = 0
    for b in brackets:
        saved = 0
        for d in buckets[b]:
            public_id = d["publicId"]
            if public_id in scraped:
                skipped_total += 1
                continue
            deck = get_deck(public_id)
            if save_deck(conn, deck, bracket=b):
                saved += 1
        print(f"  bracket={b}: 新規保存 {saved} 件")
        saved_total += saved

    conn.close()
    print(f"\n完了: 新規保存 {saved_total} 件 / 既存スキップ {skipped_total} 件")


def scrape(fmt: str, commander_card_id: str | None, limit: int):
    conn = psycopg2.connect(**DB_CONFIG)
    ensure_columns(conn)
    scraped = get_scraped_public_ids(conn)
    print(f"既存 {len(scraped)} 件はスキップ対象")

    fetched = 0
    saved = 0
    page = 1
    while fetched < limit:
        resp = search_decks(fmt=fmt, commander_card_id=commander_card_id, page=page, size=100)
        data = resp.get("data", [])
        if not data:
            print("これ以上デッキなし")
            break
        for d in data:
            if fetched >= limit:
                break
            public_id = d["publicId"]
            fetched += 1
            if public_id in scraped:
                continue
            deck = get_deck(public_id)
            if save_deck(conn, deck, bracket=d.get("bracket")):
                saved += 1
        page += 1
        if page > resp.get("totalPages", page):
            print("最終ページ到達")
            break

    conn.close()
    print(f"完了: 走査 {fetched} 件 / 新規保存 {saved} 件")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-test", action="store_true",
                         help="DB に書かず数コールだけ試す（fmt の実値確認用）")
    parser.add_argument("--fmt-candidates", default="commander,Commander",
                         help="smoke-test で試す fmt 候補（カンマ区切り）")
    parser.add_argument("--status", action="store_true", help="取得状況を表示")
    parser.add_argument("--fmt", default="commander", help="本走で使う fmt 値")
    parser.add_argument("--commander", default=None,
                         help="統率者名で絞る場合（--commander-card-id を先に解決すること）")
    parser.add_argument("--commander-card-id", default=None,
                         help="Moxfield 内部の commanderCardId（--commander の名前解決結果）")
    parser.add_argument("--limit", type=int, default=100, help="本走の取得上限（デッキ数）")
    parser.add_argument("--sample-by-bracket", action="store_true",
                         help="bracket ごとに MostView 上位 N 件を均等収集するモード")
    parser.add_argument("--brackets", default="1,2,3,4,5",
                         help="収集する bracket（カンマ区切り・既定は実測で確認した1〜5全部）")
    parser.add_argument("--per-bracket", type=int, default=100, help="bracket 1つあたりの目標件数")
    parser.add_argument("--sort", default="views", help="sortType（既定 views=MostView）")
    parser.add_argument("--max-pages", type=int, default=300,
                         help="bracket 候補探索の安全上限（search ページ数）")
    args = parser.parse_args()

    if args.smoke_test:
        smoke_test(args.fmt_candidates.split(","))
        return
    if args.status:
        status()
        return
    if args.sample_by_bracket:
        brackets = [int(b) for b in args.brackets.split(",")]
        run_bracket_batch(args.fmt, brackets, args.per_bracket, args.sort, args.max_pages)
        return

    commander_card_id = args.commander_card_id
    if args.commander and not commander_card_id:
        print(f"'{args.commander}' をカード検索で解決中...")
        result = search_cards(args.commander)
        cards = result.get("data", [])
        if not cards:
            print("見つからず。--commander-card-id を直接指定して。")
            sys.exit(1)
        commander_card_id = cards[0].get("id")
        print(f"→ id={commander_card_id} name={cards[0].get('name')!r}")

    scrape(args.fmt, commander_card_id, args.limit)


if __name__ == "__main__":
    main()
