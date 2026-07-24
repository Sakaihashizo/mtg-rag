"""
scrape_wisdom_guild_full.py — スコープC: 日本語名あり4190枚へバックフィル

使い方:
  # 対象件数確認
  python scrape_wisdom_guild_full.py --status

  # サンプル10件をにぃにに見せる
  python scrape_wisdom_guild_full.py --sample

  # 全件スクレイプ（中断可・再開可）
  python scrape_wisdom_guild_full.py --run

  # reembed 用 id リストを表示
  python scrape_wisdom_guild_full.py --updated_ids

対象:
  japanese_oracle_text IS NULL/空
  AND japanese_name IS NOT NULL AND japanese_name != ''
  AND set_code NOT IN ('unf','ust','me2','me4','unk')

礼儀:
  ~2.5s/req / HTMLキャッシュ（wg_cache/ に保存、再アクセス不要）
  再開可能（DB の japanese_oracle_text が既に埋まっていればスキップ）
  取得不可はエラーログに記録してスキップ
"""

import re
import html as html_module
import time
import os
import argparse
import urllib.request
import urllib.parse
import psycopg2

from db_config import DB_CONFIG

BASE_URL    = "http://whisper.wisdom-guild.net/card/{name}/"
SLEEP_SEC   = 2.5
SAMPLE_SIZE = 10
CACHE_DIR   = "/mnt/mtg_rag/wg_cache"

# アン・セット / オンライン専用 を除外
EXCLUDE_SETS = ('unf', 'ust', 'me2', 'me4', 'unk')

JAPANESE_RE   = re.compile(r'[ぁ-んァ-ン一-龯]')
FURIGANA_RE   = re.compile(r'（[ぁ-んァ-ヶー]+）')
TAG_RE        = re.compile(r'<[^>]+>')
WHITESPACE_RE = re.compile(r'\s+')

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


def cache_path(card_name: str) -> str:
    safe = urllib.parse.quote(card_name, safe='')
    return os.path.join(CACHE_DIR, safe + ".html")


def fetch_html(card_name: str) -> tuple[str | None, str | None]:
    """HTML を取得（キャッシュ優先）。戻り値: (html_body, error_msg)"""
    path = cache_path(card_name)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return f.read(), None

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

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(body)
    return body, None


def parse_oracle(body: str) -> tuple[str | None, str | None]:
    """HTML から日本語オラクルを抽出。戻り値: (ja_text, error_msg)
    注意: 両面カードのページでは dc/lc セル＝表面のみが返る。両面は
    parse_face_oracles を使うこと（2026-07-15・表//表 複製バグの根本原因）。"""
    m = TEXT_CELL_RE.search(body)
    if not m:
        return None, "text cell not found"
    ja_text = clean_html(m.group(1))
    if not ja_text:
        return None, "empty after clean"
    if not is_japanese(ja_text):
        return None, f"not japanese: {ja_text[:60]}"
    return ja_text, None


# 両面カードのテキスト欄（2026-07-15 判明）: whisper はどちらの面名で引いても
# 同一のカードページを返し、表面テキスト＝ th.dc/td.lc・裏面テキスト＝
# th.ddc/td.dlc に載る（単面カードは dc のみ）。従来の parse_oracle（dc 限定）を
# 面別ページに使うと常に表面が返り、面別に取得して連結すると「表 // 表」になる
# ＝日本語複製バグ 53 枚（本人発見・ロクの伝説起点）の根本原因。
TEXT_CELL_BACK_RE = re.compile(
    r'<th[^>]*class="ddc"[^>]*>\s*テキスト\s*</th>\s*<td[^>]*class="dlc"[^>]*>(.*?)</td>',
    re.DOTALL | re.IGNORECASE)


def parse_face_oracles(body: str) -> list[str]:
    """カードページから面別の日本語テキストを [表, 裏] で返す（単面は [表]）。
    取れない面は含めない＝呼び手が面数と突き合わせて完全時のみ使う（推測で埋めない）。"""
    faces = []
    for regex in (TEXT_CELL_RE, TEXT_CELL_BACK_RE):
        m = regex.search(body)
        if m:
            t = clean_html(m.group(1))
            if t and is_japanese(t):
                faces.append(t)
    return faces


def get_target_cards(conn) -> list[tuple[int, str, str | None]]:
    """対象カード一覧: (id, card_name, current_japanese_name)"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, card_name, japanese_name
            FROM mtg_cards_v2
            WHERE (japanese_oracle_text IS NULL OR japanese_oracle_text = '')
              AND japanese_name IS NOT NULL AND japanese_name != ''
              AND set_code NOT IN %s
            ORDER BY id
        """, (EXCLUDE_SETS,))
        return cur.fetchall()


def cmd_status(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_target_cards(conn)

    # キャッシュ済み件数
    cached = sum(1 for _, name, _ in cards if os.path.exists(cache_path(name)))

    # フリガナ混入件数
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM mtg_cards_v2
            WHERE japanese_name ~ '（'
              AND set_code NOT IN %s
        """, (EXCLUDE_SETS,))
        furigana_count = cur.fetchone()[0]

    conn.close()

    print(f"スコープC 対象: {len(cards)} 件（日本語名あり・japanese_oracle_text 空）")
    print(f"キャッシュ済み: {cached} 件（再スクレイプ不要）")
    print(f"未キャッシュ:   {len(cards) - cached} 件")
    print(f"フリガナ混入（除外セット外）: {furigana_count} 件")
    print(f"\n予想所要時間: 未キャッシュ {len(cards)-cached} 件 × 2.5s = {(len(cards)-cached)*2.5/60:.0f} 分")


def cmd_sample(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_target_cards(conn)
    conn.close()

    print(f"対象カード総数: {len(cards)} 件")
    print(f"サンプル {SAMPLE_SIZE} 件をスクレイプします...\n")

    ok = 0
    for card_id, card_name, ja_name in cards[:SAMPLE_SIZE]:
        body, fetch_err = fetch_html(card_name)
        if fetch_err:
            print(f"[NG]  {card_name}: {fetch_err}")
            print()
            if not os.path.exists(cache_path(card_name)):
                time.sleep(SLEEP_SEC)
            continue

        ja_text, parse_err = parse_oracle(body)
        cached = os.path.exists(cache_path(card_name))

        if ja_text:
            print(f"[OK]  {card_name}  ({'キャッシュ' if cached else 'スクレイプ'})")
            print(f"      名前: {ja_name}")
            print(f"      取得: {ja_text[:100]}")
            ok += 1
        else:
            print(f"[NG]  {card_name}: {parse_err}")

        print()
        if not cached:
            time.sleep(SLEEP_SEC)

    print(f"サンプル結果: {ok}/{SAMPLE_SIZE} 件取得成功")
    print("\n問題なければ --run で全件 UPDATE してください。")


def cmd_run(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_target_cards(conn)
    total = len(cards)
    print(f"対象カード: {total} 件（日本語名あり・japanese_oracle_text 空）")
    print(f"キャッシュディレクトリ: {CACHE_DIR}")

    updated = 0
    skipped_parse = 0
    skipped_fetch = 0
    updated_ids: list[int] = []

    with conn.cursor() as cur:
        for i, (card_id, card_name, ja_name) in enumerate(cards, 1):
            cached = os.path.exists(cache_path(card_name))

            body, fetch_err = fetch_html(card_name)
            if fetch_err:
                skipped_fetch += 1
                print(f"[{i}/{total}] FETCH_NG  {card_name}: {fetch_err}")
                time.sleep(SLEEP_SEC)
                continue

            ja_text, parse_err = parse_oracle(body)
            # 両面カードは dc（表面）＋ddc（裏面）を「表 // 裏」で格納
            # （2026-07-15・従来は表面のみ＝裏面欠落。全面取れたときだけ採用）
            if ' // ' in card_name:
                face_texts = parse_face_oracles(body)
                if len(face_texts) == len(card_name.split(' // ')):
                    ja_text = ' // '.join(face_texts)
            if not ja_text:
                # テキストセルが空 = バニラカード等でテキストが存在しない
                # NULL（未取得）と区別するため全角スペースを挿入
                skipped_parse += 1
                cur.execute(
                    "UPDATE mtg_cards_v2 SET japanese_oracle_text = %s WHERE id = %s",
                    ('　', card_id),
                )
                updated_ids.append(card_id)
                print(f"[{i}/{total}] NO_TEXT  {card_name}: {parse_err} → 全角スペース挿入")
                if not cached:
                    time.sleep(SLEEP_SEC)
                continue

            # フリガナ除去
            ja_name_clean = FURIGANA_RE.sub('', ja_name).strip() if ja_name else ja_name

            cur.execute("""
                UPDATE mtg_cards_v2
                SET japanese_oracle_text = %s, japanese_name = %s
                WHERE id = %s
            """, (ja_text, ja_name_clean, card_id))
            updated_ids.append(card_id)
            updated += 1

            cached_label = 'C' if cached else ' '
            print(f"[{i}/{total}]{cached_label} OK  {card_name}: {ja_text[:60]}")

            if updated % 100 == 0:
                conn.commit()
                print(f"  → {updated} 件コミット済み")

            if not cached:
                time.sleep(SLEEP_SEC)

    conn.commit()
    conn.close()

    # 更新 ID を reembed 用ファイルに書き出し
    ids_file = "/mnt/mtg_rag/scope_c_updated_ids.txt"
    with open(ids_file, 'w') as f:
        for cid in updated_ids:
            f.write(f"{cid}\n")

    print(f"\n完了: 更新={updated} / fetch失敗={skipped_fetch} / parse失敗={skipped_parse} / 対象={total}")
    print(f"更新 ID リスト: {ids_file}  ({len(updated_ids)} 件)")
    print(f"\n次のステップ:")
    print(f"  embed_text 再構築: python rebuild_embed_text.py --update_text --card_ids_file {ids_file}")
    print(f"  reembed:          python rebuild_embed_text.py --reembed --model SMALL_V2 --card_ids_file {ids_file}")


def cmd_updated_ids(args):
    ids_file = "/mnt/mtg_rag/scope_c_updated_ids.txt"
    if not os.path.exists(ids_file):
        print(f"ファイルが見つかりません: {ids_file}")
        return
    with open(ids_file) as f:
        ids = [l.strip() for l in f if l.strip()]
    print(f"更新済み ID: {len(ids)} 件")
    print(ids_file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="スコープC: 日本語名あり4190枚への日本語オラクルバックフィル"
    )
    parser.add_argument("--status",      action="store_true", help="対象件数・キャッシュ状況確認")
    parser.add_argument("--sample",      action="store_true", help="サンプル10件表示")
    parser.add_argument("--run",         action="store_true", help="全件スクレイプ＆UPDATE")
    parser.add_argument("--updated_ids", action="store_true", help="更新済み ID ファイルを表示")
    args = parser.parse_args()

    if args.status:
        cmd_status(args)
    elif args.sample:
        cmd_sample(args)
    elif args.run:
        cmd_run(args)
    elif args.updated_ids:
        cmd_updated_ids(args)
    else:
        parser.print_help()
