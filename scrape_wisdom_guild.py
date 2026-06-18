"""
scrape_wisdom_guild.py — Wisdom Guild whisper から EOE系の日本語オラクルを補填

使い方:
  # サンプル10件を表示してにぃにに目視確認してもらう
  python scrape_wisdom_guild.py --sample

  # 全件スクレイプ → DB UPDATE
  python scrape_wisdom_guild.py --run

  # 取得状況を確認
  python scrape_wisdom_guild.py --status

対象: set_code IN ('eoe','eoc','yeoe') で japanese_oracle_text が空 or 英語混入 (302件)
ソース: http://whisper.wisdom-guild.net/card/<英語名URLエンコード>/
礼儀: ~1.2秒/req、User-Agent 明示
"""

import re
import html as html_module
import time
import argparse
import urllib.request
import urllib.parse
import psycopg2

from db_config import DB_CONFIG

BASE_URL    = "http://whisper.wisdom-guild.net/card/{name}/"
SLEEP_SEC   = 1.2
SAMPLE_SIZE = 10
TARGET_SETS = ('eoe', 'eoc', 'yeoe')

JAPANESE_RE  = re.compile(r'[ぁ-んァ-ン一-龯]')
TAG_RE       = re.compile(r'<[^>]+>')
WHITESPACE_RE = re.compile(r'\s+')

# <th class="dc">テキスト</th> の直後の <td class="lc">…</td> を取るパターン
TEXT_CELL_RE = re.compile(
    r'<th[^>]*class="dc"[^>]*>\s*テキスト\s*</th>\s*<td[^>]*class="lc"[^>]*>(.*?)</td>',
    re.DOTALL | re.IGNORECASE,
)


def is_japanese(text: str) -> bool:
    return bool(JAPANESE_RE.search(text))


def clean_html(raw: str) -> str:
    text = TAG_RE.sub('', raw)
    text = html_module.unescape(text)
    text = WHITESPACE_RE.sub(' ', text).strip()
    return text


def fetch_oracle(card_name: str) -> tuple[str | None, str | None]:
    """
    Wisdom Guild whisper から card_name の日本語オラクルテキストを取得。
    戻り値: (ja_text, error_msg)。取得不可なら (None, reason)。
    """
    encoded = urllib.parse.quote(card_name)
    url = BASE_URL.format(name=encoded)
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'MTG-RAG-Project/1.0 (educational; non-commercial)'},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        return None, f"fetch error: {e}"

    m = TEXT_CELL_RE.search(body)
    if not m:
        return None, "text cell not found"

    ja_text = clean_html(m.group(1))
    if not ja_text:
        return None, "empty after clean"
    if not is_japanese(ja_text):
        return None, f"not japanese: {ja_text[:60]}"

    return ja_text, None


def get_target_cards(conn) -> list[tuple[int, str, str | None]]:
    """
    対象カード一覧を取得: (id, card_name, current_ja_oracle)
    空 or 英語混入のカードのみ。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, card_name, japanese_oracle_text
            FROM mtg_cards_v2
            WHERE set_code IN %s
              AND (
                japanese_oracle_text IS NULL
                OR japanese_oracle_text = ''
                OR japanese_oracle_text !~ '[ぁ-んァ-ヶ亜-熙]'
              )
            ORDER BY id
        """, (TARGET_SETS,))
        return cur.fetchall()


def cmd_sample(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_target_cards(conn)
    conn.close()

    print(f"対象カード総数: {len(cards)} 件（空+英語混入）")
    print(f"サンプル {SAMPLE_SIZE} 件をスクレイプします...\n")

    sample = cards[:SAMPLE_SIZE]
    ok = 0
    for card_id, card_name, current_ja in sample:
        ja_text, err = fetch_oracle(card_name)
        current_preview = (current_ja or "（空）")[:60]
        if ja_text:
            print(f"[OK]  {card_name}")
            print(f"      現在: {current_preview}")
            print(f"      取得: {ja_text[:80]}")
            ok += 1
        else:
            print(f"[NG]  {card_name}  → {err}")
            print(f"      現在: {current_preview}")
        print()
        time.sleep(SLEEP_SEC)

    print(f"サンプル結果: {ok}/{len(sample)} 件取得成功")
    print("\n目視確認後、問題なければ --run で全件 UPDATE してください。")


def cmd_run(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_target_cards(conn)
    total = len(cards)
    print(f"対象カード: {total} 件")

    updated = 0
    skipped = 0
    skip_log: list[tuple[str, str]] = []

    with conn.cursor() as cur:
        for i, (card_id, card_name, _) in enumerate(cards, 1):
            ja_text, err = fetch_oracle(card_name)
            if ja_text:
                cur.execute(
                    "UPDATE mtg_cards_v2 SET japanese_oracle_text = %s WHERE id = %s",
                    (ja_text, card_id),
                )
                updated += 1
                print(f"[{i}/{total}] OK  {card_name}: {ja_text[:60]}")
            else:
                skipped += 1
                skip_log.append((card_name, err))
                print(f"[{i}/{total}] NG  {card_name}: {err}")

            if updated % 50 == 0 and updated > 0:
                conn.commit()
                print(f"  → {updated} 件コミット済み")

            time.sleep(SLEEP_SEC)

    conn.commit()
    conn.close()

    print(f"\n完了: 更新={updated} / スキップ={skipped} / 対象={total}")
    if skip_log:
        print("\nスキップ一覧:")
        for name, reason in skip_log:
            print(f"  {name}: {reason}")


def cmd_status(args):
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                count(*) FILTER (WHERE japanese_oracle_text IS NULL OR japanese_oracle_text = ''),
                count(*) FILTER (WHERE japanese_oracle_text ~ '[ぁ-んァ-ヶ亜-熙]'),
                count(*) FILTER (WHERE japanese_oracle_text <> ''
                                   AND japanese_oracle_text !~ '[ぁ-んァ-ヶ亜-熙]')
            FROM mtg_cards_v2
            WHERE set_code IN %s
        """, (TARGET_SETS,))
        empty, ja_ok, en_mix = cur.fetchone()

        cur.execute("""
            SELECT card_name, japanese_oracle_text
            FROM mtg_cards_v2
            WHERE set_code IN %s AND japanese_oracle_text ~ '[ぁ-んァ-ヶ亜-熙]'
            LIMIT 5
        """, (TARGET_SETS,))
        samples = cur.fetchall()
    conn.close()

    print(f"EOE系 japanese_oracle_text 状況:")
    print(f"  空:       {empty} 件")
    print(f"  日本語OK: {ja_ok} 件")
    print(f"  英語混入: {en_mix} 件")
    print("\nサンプル（日本語ありカード）:")
    for name, text in samples:
        print(f"  {name}: {(text or '')[:80]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Wisdom Guild から EOE系の日本語オラクルを補填"
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("--sample", help="サンプル10件を表示")
    sub.add_parser("--run",    help="全件 UPDATE")
    sub.add_parser("--status", help="取得状況を確認")

    # --xxx 形式でも動くように
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--run",    action="store_true")
    parser.add_argument("--status", action="store_true")

    args = parser.parse_args()

    if args.sample:
        cmd_sample(args)
    elif args.run:
        cmd_run(args)
    elif args.status:
        cmd_status(args)
    else:
        parser.print_help()
