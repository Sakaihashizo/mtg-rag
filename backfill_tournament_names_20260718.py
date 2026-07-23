#!/usr/bin/env python3
"""
backfill_tournament_names_20260718.py — deck_list の大会名/大会日/順位バックフィル
================================================================================
調査元: docs/ai/baton_tournament_names_20260718.md（2026-07-18・調査のみバトン）

背景:
    scrape_mtgtop8.py の get_deck_ids() は MTGTop8 のイベントページから実大会名
    （div.event_title）を正しく取得していたが、戻り値に含めず捨てていた。呼び出し元の
    scrape() はさらに "MTGTop8 Event {event_id} ({year} {format_code})" という合成
    プレースホルダで event_name を上書きしていたため、deck_list.tournament_name には
    実大会名が一度も保存されていなかった（tournament_date・placement は列はあるが
    そもそも書き込みロジック自体が無かった）。

    一方 deck_list.tournament_event_id（MTGTop8内部のイベントID）と source_url は
    既存デッキ全件（precon除く）に100%保存されているため、distinct イベント単位で
    イベントページを再取得すれば、デッキ単位の再取得なしに実大会名・大会日・順位を
    復元できる（調査時点で対象 855 イベント・9,275デッキ）。

このスクリプトがやること:
    1. deck_list から「まだ実名化されていない」distinct (source, tournament_event_id)
       を抽出する。
    2. 各イベントについて https://www.mtgtop8.com/event?e={ID} を1回だけ GET する
       （礼儀間隔 2〜3秒・User-Agent明示・タイムアウト・失敗は1回だけリトライして
       ダメならスキップしログに記録）。
    3. 取得した実大会名・大会日を deck_list.tournament_name / tournament_date に、
       デッキ別の順位を deck_list.placement に UPDATE する。

書き込み対象: deck_list テーブルの tournament_name / tournament_date / placement
             の3列のみ。他列・他テーブルへの書き込みコードは一切含まない。

再開設計（中断→再実行に対応）:
    「まだ実名化されていない」の判定は DB に問い合わせるだけで求まる
    （tournament_date が NULL、または tournament_name が旧・合成プレースホルダの
    正規表現 `^MTGTop8 Event \\d+ \\(\\d{4} [A-Z]+\\)$` に一致するイベントを対象とする）。
    一度 UPDATE されたイベントは次回実行時に自然に対象から外れるため、進捗を記録する
    専用の状態ファイルは持たない。

依存: このプロジェクト既存のもの（psycopg2 / requests / beautifulsoup4）のみ。
      新規 pip インストールは行わない。db_config.py の DB_CONFIG をそのまま使う
      （bin/dbq は読み取り専用ロールのため、書き込みはこのスクリプト内の psycopg2
      接続で行う）。

使い方（/mnt/mtg_rag で実行する想定・未実行）:
    python backfill_tournament_names_20260718.py --dry-run   # まず取得のみで試す
    python backfill_tournament_names_20260718.py --limit 5   # 先頭5件だけ本走で試す
    python backfill_tournament_names_20260718.py             # 全対象を本走
"""

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime

import psycopg2
import requests
from bs4 import BeautifulSoup

from db_config import DB_CONFIG

BASE_URL          = "https://www.mtgtop8.com"
REQUEST_INTERVAL  = 2.5    # 秒（礼儀正しいスクレイピング・2〜3秒の中間値）
REQUEST_TIMEOUT   = 15     # 秒
MAX_RETRIES       = 1      # 失敗イベントは1回だけリトライしてスキップ（指示どおり）
PROGRESS_EVERY    = 50     # 何イベントごとに進捗を stdout にも出すか

HEADERS = {
    "User-Agent": "MTG-RAG-Research-Bot/1.0 (educational project; contact via GitHub)",
    "Accept-Language": "en-US,en;q=0.9",
}

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_PATH  = os.path.join(REPO_ROOT, "docs", "me", "backfill_tournament_names_20260718.log")

# scrape_mtgtop8.py が書き込んでいた合成プレースホルダのパターン
# 例: "MTGTop8 Event 63156 (2024 MO)"
PLACEHOLDER_RE = re.compile(r'^MTGTop8 Event \d+ \(\d{4} [A-Z]+\)$')

# イベントページ内の日付表記: "465 players - 30/12/24"
DATE_RE = re.compile(r'\d+\s+players?\s*-\s*(\d{2})/(\d{2})/(\d{2})')

# デッキ行の順位マーカー: "<div ... class=S14>1</div> ... <a href=?e=E&d=D&f=FMT>"
# 順位は "1" 単独のほか、タイブレークグループ表記 "3-4" "5-8" もありうる
# （先頭の整数のみを placement に格納する＝タイの上位側で近似）。
RANK_DECK_RE = re.compile(
    r'align=center class=S14>([\w\-]+)</div>\s*<div[^>]*><a href=\?e=\d+&d=(\d+)&f=[A-Z]+>'
)


def setup_logging(dry_run: bool) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),  # 追記（デフォルトmode='a'）
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.info("=== backfill_tournament_names 起動（dry_run=%s） ===", dry_run)


def fetch(url: str) -> str | None:
    """礼儀正しい HTTP GET。scrape_mtgtop8.py の fetch() と同型（タイムアウト明示・
    失敗は1回だけリトライしてスキップ、間隔を詰めるリトライはしない）。"""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            time.sleep(REQUEST_INTERVAL)
            if resp.status_code == 200:
                return resp.text
            logging.warning("HTTP %s: %s", resp.status_code, url)
        except requests.RequestException as e:
            logging.warning(
                "通信エラー（試行 %d/%d）: %s — %s",
                attempt + 1, MAX_RETRIES + 1, url, e,
            )
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_INTERVAL * 2)
    return None


def parse_event_page(html: str, event_id: int) -> tuple[str, str | None, dict[int, int]]:
    """
    イベントページから (実大会名, 大会日ISO文字列またはNone, {deck_id: placement}) を返す。
    """
    soup = BeautifulSoup(html, "html.parser")

    title_div  = soup.find("div", class_="event_title")
    event_name = title_div.get_text(strip=True) if title_div else f"Event {event_id}"

    event_date = None
    date_m = DATE_RE.search(html)
    if date_m:
        dd, mm, yy = date_m.groups()
        candidate = f"20{yy}-{mm}-{dd}"
        try:
            datetime.strptime(candidate, "%Y-%m-%d")  # 妥当性チェック（不正日付を弾く）
            event_date = candidate
        except ValueError:
            logging.warning("日付の解析に失敗（不正な日付）: event_id=%s raw=%s", event_id, date_m.group(0))

    placements: dict[int, int] = {}
    for rank_str, deck_id_str in RANK_DECK_RE.findall(html):
        lead = re.match(r'^(\d+)', rank_str)
        if lead:
            placements[int(deck_id_str)] = int(lead.group(1))

    return event_name, event_date, placements


def get_pending_events(conn) -> list[tuple[str, int]]:
    """
    まだ実名化されていない distinct (source, tournament_event_id) を取得する。
    再開設計: 一度 UPDATE 済みのイベントは自然にこの WHERE から外れる。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT source, tournament_event_id
            FROM deck_list
            WHERE tournament_event_id IS NOT NULL
              AND (
                    tournament_date IS NULL
                 OR tournament_name IS NULL
                 OR tournament_name ~ %s
              )
            ORDER BY source, tournament_event_id
        """, (PLACEHOLDER_RE.pattern,))
        return [(row[0], row[1]) for row in cur.fetchall()]


def update_event(
    conn, source: str, event_id: int,
    event_name: str, event_date: str | None,
    placements: dict[int, int],
) -> tuple[int, int]:
    """
    deck_list の tournament_name / tournament_date / placement のみを更新する。
    書き込み対象はこの3列のみ・他テーブルへの書き込みは行わない。
    戻り値: (tournament_name/date を更新した行数, placement を更新した行数)
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE deck_list
            SET tournament_name = %s,
                tournament_date = %s
            WHERE source = %s AND tournament_event_id = %s
        """, (event_name, event_date, source, event_id))
        name_date_rows = cur.rowcount

        placement_rows = 0
        for deck_id, placement in placements.items():
            # deck_name は save_deck() が f"mtgtop8_{event_id}_{deck_id}" で採番して
            # おり UNIQUE 索引済みのため、これで対象デッキ1行に正確に当たる。
            deck_name = f"mtgtop8_{event_id}_{deck_id}"
            cur.execute("""
                UPDATE deck_list
                SET placement = %s
                WHERE deck_name = %s AND source = %s
            """, (placement, deck_name, source))
            placement_rows += cur.rowcount

    conn.commit()
    return name_date_rows, placement_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="取得・解析のみ行い DB は更新しない")
    parser.add_argument("--limit", type=int, default=None, help="先頭N件のイベントだけ処理する（試走用）")
    args = parser.parse_args()

    setup_logging(args.dry_run)

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        pending = get_pending_events(conn)
        if args.limit is not None:
            pending = pending[: args.limit]

        total = len(pending)
        logging.info("対象イベント数: %d", total)
        print(f"対象イベント数: {total}")

        ok_count = 0
        failed: list[tuple[str, int]] = []

        for i, (source, event_id) in enumerate(pending, start=1):
            url  = f"{BASE_URL}/event?e={event_id}"
            html = fetch(url)

            if not html:
                logging.warning("スキップ（取得失敗・リトライ後も失敗）: source=%s event_id=%s url=%s",
                                 source, event_id, url)
                failed.append((source, event_id))
                continue

            event_name, event_date, placements = parse_event_page(html, event_id)

            if args.dry_run:
                logging.info(
                    "[dry-run] source=%s event_id=%s name=%r date=%s placements=%d件",
                    source, event_id, event_name, event_date, len(placements),
                )
            else:
                name_date_rows, placement_rows = update_event(
                    conn, source, event_id, event_name, event_date, placements
                )
                logging.info(
                    "更新: source=%s event_id=%s name=%r date=%s "
                    "(tournament_name/date=%d行 placement=%d行)",
                    source, event_id, event_name, event_date,
                    name_date_rows, placement_rows,
                )

            ok_count += 1

            if i % PROGRESS_EVERY == 0 or i == total:
                msg = f"進捗: {i}/{total} 件処理（成功 {ok_count} / 失敗 {len(failed)}）"
                print(msg)
                logging.info(msg)

        summary = f"完了: 成功 {ok_count} / 失敗 {len(failed)} / 対象 {total}"
        print(summary)
        logging.info("=== %s ===", summary)
        if failed:
            logging.info("失敗イベント一覧（source, event_id）: %s", failed)
            print(f"失敗イベント: {failed}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
