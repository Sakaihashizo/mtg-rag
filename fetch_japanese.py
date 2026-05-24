"""
fetch_japanese.py — Scryfall API から日本語テキストを取得
==========================================================
mtg_cards_v2 に japanese_name / japanese_oracle_text カラムを追加し、
Scryfall API から日本語データを取得して格納する。

日本語版が存在しないカードは NULL のまま。

使い方:
  # カラム追加 + 全件取得
  python fetch_japanese.py

  # 未取得分だけ再実行（中断後の再開）
  python fetch_japanese.py --resume

Scryfall API レートリミット: 100ms/リクエスト
全件取得の目安: 約3〜5時間（カード数・通信環境による）
"""

import argparse
import time
import psycopg2
import requests
from tqdm import tqdm

DB_CONFIG = {
    "dbname": "rag_dev",
    "user": "devuser",
    "password": "***REMOVED***",
    "host": "localhost",
    "port": 5435,
}

# Scryfall API
SCRYFALL_NAMED_URL = "https://api.scryfall.com/cards/named"
SCRYFALL_SEARCH_URL = "https://api.scryfall.com/cards/search"
REQUEST_INTERVAL = 0.11   # 100ms + バッファ
BATCH_COMMIT = 200        # 何件ごとにコミットするか


# ─── カラム追加 ───────────────────────────────────────────────

def add_japanese_columns(conn):
    """mtg_cards_v2 に日本語カラムを追加する（既存なら何もしない）"""
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE mtg_cards_v2
            ADD COLUMN IF NOT EXISTS japanese_name TEXT,
            ADD COLUMN IF NOT EXISTS japanese_oracle_text TEXT;
        """)
    conn.commit()
    print("カラム追加完了: japanese_name, japanese_oracle_text")


# ─── Scryfall API 呼び出し ────────────────────────────────────

def fetch_japanese_by_name(card_name: str) -> tuple[str | None, str | None]:
    """
    カード名で Scryfall API を叩き、日本語名と日本語テキストを返す。
    日本語版がない場合は (None, None) を返す。
    """
    try:
        # まず正確な名前で検索
        resp = requests.get(
            SCRYFALL_NAMED_URL,
            params={"exact": card_name, "lang": "ja"},
            timeout=10,
        )
        time.sleep(REQUEST_INTERVAL)

        if resp.status_code == 200:
            data = resp.json()
            if data.get("lang") != "ja":
                return None, None
            ja_name   = data.get("printed_name") or data.get("name")
            ja_oracle = data.get("printed_text") or data.get("oracle_text")

            # 両面カードは card_faces から取得
            if not ja_oracle and "card_faces" in data:
                faces = data["card_faces"]
                texts = [f.get("printed_text") or f.get("oracle_text", "")
                         for f in faces if f.get("printed_text") or f.get("oracle_text")]
                ja_oracle = " // ".join(t for t in texts if t)

            return ja_name, ja_oracle

        elif resp.status_code == 404:
            # 日本語版なし
            return None, None
        else:
            # その他のエラー
            print(f"  API エラー {resp.status_code}: {card_name}")
            return None, None

    except requests.RequestException as e:
        print(f"  通信エラー: {card_name} — {e}")
        time.sleep(1.0)  # エラー時は少し待つ
        return None, None


# ─── メイン処理 ───────────────────────────────────────────────

def fetch_all(resume: bool = False):
    conn = psycopg2.connect(**DB_CONFIG)
    add_japanese_columns(conn)

    # 取得対象カードを取得
    with conn.cursor() as cur:
        if resume:
            # resume モード：japanese_name が NULL のカードだけ対象
            # （取得済み = japanese_name が入っている or 明示的に '' を入れた）
            cur.execute("""
                SELECT id, card_name FROM mtg_cards_v2
                WHERE japanese_name IS NULL
                ORDER BY id;
            """)
        else:
            cur.execute("""
                SELECT id, card_name FROM mtg_cards_v2
                ORDER BY id;
            """)
        cards = cur.fetchall()

    total = len(cards)
    print(f"取得対象: {total} 件")
    print(f"推定時間: {total * REQUEST_INTERVAL / 60:.0f} 分")

    found     = 0
    not_found = 0
    errors    = 0

    for i, (card_id, card_name) in enumerate(tqdm(cards, desc="日本語取得", mininterval=10)):
        ja_name, ja_oracle = fetch_japanese_by_name(card_name)

        if ja_name or ja_oracle:
            found += 1
        else:
            not_found += 1
            # 日本語版なし → 空文字を入れて「取得済み・なし」を記録
            # NULL のままだと resume 時に再取得してしまう
            ja_name   = ""
            ja_oracle = ""

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE mtg_cards_v2
                SET japanese_name = %s, japanese_oracle_text = %s
                WHERE id = %s
            """, (ja_name or None, ja_oracle or None, card_id))

        # 定期コミット
        if (i + 1) % BATCH_COMMIT == 0:
            conn.commit()
            print(f"  {i+1}/{total} 件完了 "
                  f"(日本語あり: {found} / なし: {not_found} / エラー: {errors})")

    conn.commit()
    conn.close()

    print(f"\n完了!")
    print(f"  日本語テキストあり: {found} 件")
    print(f"  日本語版なし:       {not_found} 件")
    print(f"  エラー:             {errors} 件")


# ─── 取得状況確認 ─────────────────────────────────────────────

def check_status():
    """取得状況をサマリーで表示する"""
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM mtg_cards_v2")
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM mtg_cards_v2
            WHERE japanese_name IS NOT NULL AND japanese_name != ''
        """)
        found = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM mtg_cards_v2
            WHERE japanese_oracle_text IS NULL
        """)
        pending = cur.fetchone()[0]

    conn.close()
    print(f"総カード数:         {total}")
    print(f"日本語テキストあり: {found}")
    print(f"未取得:             {pending}")

    # サンプル表示
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT card_name, japanese_name, japanese_oracle_text
            FROM mtg_cards_v2
            WHERE japanese_name IS NOT NULL AND japanese_name != ''
            LIMIT 5
        """)
        rows = cur.fetchall()
    conn.close()
    print("\nサンプル（日本語あり）:")
    for row in rows:
        print(f"  {row[0]} → {row[1]}")
        print(f"    {(row[2] or '')[:60]}...")


# ─── エントリーポイント ───────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--resume", action="store_true",
        help="未取得分だけ再実行する（中断後の再開用）",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="取得状況を確認する",
    )
    args = parser.parse_args()

    if args.status:
        check_status()
    else:
        fetch_all(resume=args.resume)
