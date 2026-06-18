"""
fix_ja_slots.py — 日本語スロットに英語が入っている 30件を補填

バケットA (24件): japanese_name が英語 → whisper から日本語名を取得して UPDATE or NULL
バケットB (5件):  japanese_name + japanese_oracle_text が両方英語 → 両方 UPDATE or NULL
バケットC (1件):  Tam の japanese_oracle_text が NULL → whisper から取得して UPDATE

whisper キャッシュは既存の wg_cache/ を共用。
取得できなかったフィールドは NULL（英語を日本語スロットに残さない）。

使い方:
  python fix_ja_slots.py --sample   # 各バケットの取得結果を確認
  python fix_ja_slots.py --run      # DB UPDATE + reembed 用 ID ファイル出力
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

BASE_URL  = "http://whisper.wisdom-guild.net/card/{name}/"
SLEEP_SEC = 2.5
CACHE_DIR = "/mnt/mtg_rag/wg_cache"
IDS_OUT   = "/mnt/mtg_rag/fix_ja_slots_ids.txt"

JAPANESE_RE  = re.compile(r'[ぁ-んァ-ン一-龯]')
FURIGANA_RE  = re.compile(r'（[ぁ-んァ-ヶー]+）')
TAG_RE       = re.compile(r'<[^>]+>')
WHITESPACE_RE = re.compile(r'\s+')

# mc/dmc セル (exact match): カード名 = 各フェイスの最初のセルのみ使用
MC_CELL_PAT  = r'<td\b[^>]*\bclass\s*=\s*"mc"[^>]*>(.*?)</td>'
DMC_CELL_PAT = r'<td\b[^>]*\bclass\s*=\s*"dmc"[^>]*>(.*?)</td>'
LATIN_RE = re.compile(r'[A-Za-z]')
# テキストセル（日本語 oracle）
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


def fetch_html(card_name: str) -> tuple[str | None, str | None, bool]:
    """キャッシュ優先で HTML を取得。戻り値: (body, err, was_cached)"""
    path = cache_path(card_name)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return f.read(), None, True

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
        return None, f"fetch error: {e}", False

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(body)
    return body, None, False


def _parse_name_cell(raw_html: str) -> tuple[str | None, str | None]:
    """mc/dmc セルから (jp_part, en_part) を返す。
    形式: 'JP名/EN名'。EN 部分に英字がない場合は (None, None)。
    """
    first = re.split(r'<(?:br|a)\b', raw_html, maxsplit=1)[0]
    text = html_module.unescape(TAG_RE.sub('', first)).strip()
    if '/' not in text:
        return None, None
    slash_idx = text.index('/')
    jp_part = FURIGANA_RE.sub('', text[:slash_idx].strip()).strip()
    en_part = text[slash_idx + 1:].strip()
    if jp_part and is_japanese(jp_part) and en_part and LATIN_RE.search(en_part):
        return jp_part, en_part
    return None, None


def parse_ja_name(body: str, card_name: str | None = None) -> str | None:
    """mc/dmc セルから日本語カード名を抽出。
    card_name 指定時: EN 部分が card_name と一致するセルを優先（fin 等の共有ページ対応）。
    - 1 面カード (card_name 指定): 一致するセルを 1 つ返す
    - 2 面カード (card_name なし): mc + dmc を結合して返す
    """
    mc_jp, mc_en, dmc_jp, dmc_en = None, None, None, None

    mc_m = re.compile(MC_CELL_PAT, re.DOTALL | re.IGNORECASE).search(body)
    if mc_m:
        mc_jp, mc_en = _parse_name_cell(mc_m.group(1))

    dmc_m = re.compile(DMC_CELL_PAT, re.DOTALL | re.IGNORECASE).search(body)
    if dmc_m:
        dmc_jp, dmc_en = _parse_name_cell(dmc_m.group(1))

    if card_name and (mc_jp or dmc_jp):
        # 単面カード: EN 部分が card_name に近いセルを選択
        cn_key = card_name.split(',')[0].strip().lower()
        if mc_en and cn_key in mc_en.lower():
            return mc_jp
        if dmc_en and cn_key in dmc_en.lower():
            return dmc_jp
        # どちらも一致しなければ mc 優先
        return mc_jp or dmc_jp

    # 2 面カード (card_name なし): mc + dmc を結合
    parts = [p for p in (mc_jp, dmc_jp) if p]
    return ' // '.join(parts) if parts else None


def parse_ja_oracle(body: str) -> str | None:
    """テキストセルから日本語オラクルテキストを抽出。"""
    m = TEXT_CELL_RE.search(body)
    if not m:
        return None
    text = clean_html(m.group(1))
    if not text or not is_japanese(text):
        return None
    return text


# ─── バケット定義 ──────────────────────────────────────────────

def get_bucket_a(conn) -> list[tuple[int, str, str]]:
    """japanese_name が英語、japanese_oracle_text は日本語 → name だけ修正"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, set_code, card_name
            FROM mtg_cards_v2
            WHERE japanese_name IS NOT NULL
              AND btrim(japanese_name) <> ''
              AND japanese_name !~ '[ぁ-んァ-ヶ一-龯]'
              AND oracle_text IS NOT NULL
              AND btrim(oracle_text) <> ''
              AND japanese_oracle_text ~ '[ぁ-んァ-ヶ一-龯]'
            ORDER BY set_code, card_name
        """)
        return cur.fetchall()


def get_bucket_b(conn) -> list[tuple[int, str, str]]:
    """japanese_oracle_text が英語 → name + oracle_text 両方修正"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, set_code, card_name
            FROM mtg_cards_v2
            WHERE japanese_oracle_text IS NOT NULL
              AND btrim(japanese_oracle_text) <> ''
              AND japanese_oracle_text !~ '[ぁ-んァ-ヶ一-龯]'
            ORDER BY set_code, card_name
        """)
        return cur.fetchall()


def get_bucket_c(conn) -> list[tuple[int, str, str]]:
    """Tam: japanese_name あり、japanese_oracle_text が NULL → oracle_text のみ"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, set_code, card_name
            FROM mtg_cards_v2
            WHERE card_name LIKE 'Tam, Observant Sequencer%%'
              AND (japanese_oracle_text IS NULL OR btrim(japanese_oracle_text) = '')
        """)
        return cur.fetchall()


# ─── 処理コア ─────────────────────────────────────────────────

def process_card(
    card_id: int, card_name: str, set_code: str,
    fix_name: bool, fix_oracle: bool,
    dry_run: bool, cur,
) -> tuple[str, str | None, str | None]:
    """
    1枚処理。戻り値: (status, ja_name_result, ja_oracle_result)
    status: 'OK' / 'MISS_NAME' / 'MISS_ORA' / 'MISS_BOTH' / 'FETCH_NG'
    """
    body, err, cached = fetch_html(card_name)
    if err:
        return f"FETCH_NG: {err}", None, None

    ja_name   = parse_ja_name(body, card_name=card_name) if fix_name else "skip"
    ja_oracle = parse_ja_oracle(body) if fix_oracle else "skip"

    if not dry_run:
        if fix_name:
            cur.execute(
                "UPDATE mtg_cards_v2 SET japanese_name = %s WHERE id = %s",
                (ja_name, card_id),
            )
        if fix_oracle:
            cur.execute(
                "UPDATE mtg_cards_v2 SET japanese_oracle_text = %s WHERE id = %s",
                (ja_oracle, card_id),
            )

    miss_name  = fix_name   and ja_name   is None
    miss_ora   = fix_oracle and ja_oracle is None
    if miss_name and miss_ora:
        status = "MISS_BOTH"
    elif miss_name:
        status = "MISS_NAME"
    elif miss_ora:
        status = "MISS_ORA"
    else:
        status = "OK"

    return status, ja_name if fix_name else None, ja_oracle if fix_oracle else None


def run(dry_run: bool):
    conn = psycopg2.connect(**DB_CONFIG)
    bucket_a = get_bucket_a(conn)
    bucket_b = get_bucket_b(conn)
    bucket_c = get_bucket_c(conn)

    total = len(bucket_a) + len(bucket_b) + len(bucket_c)
    print(f"対象: A={len(bucket_a)} B={len(bucket_b)} C={len(bucket_c)} 合計={total}")
    print(f"モード: {'DRY RUN（DB 更新なし）' if dry_run else '本番 UPDATE'}")
    print()

    updated_ids: list[int] = []
    counts = {"OK": 0, "MISS_NAME": 0, "MISS_ORA": 0, "MISS_BOTH": 0, "FETCH_NG": 0}

    with conn.cursor() as cur:

        # ── バケットA ──
        print("=== バケットA: japanese_name 修正 ===")
        for card_id, set_code, card_name in bucket_a:
            cached = os.path.exists(cache_path(card_name))
            status, ja_name, _ = process_card(
                card_id, card_name, set_code,
                fix_name=True, fix_oracle=False,
                dry_run=dry_run, cur=cur,
            )
            c_flag = "C" if cached else " "
            label  = f"[A {c_flag}]"

            if status == "OK":
                print(f"{label} ({set_code}) {card_name}")
                print(f"       → {ja_name}")
                counts["OK"] += 1
                updated_ids.append(card_id)
            else:
                print(f"{label} ({set_code}) {card_name}: {status} → NULL")
                if "FETCH" not in status:
                    updated_ids.append(card_id)
                counts[status] = counts.get(status, 0) + 1

            if not cached and status != "FETCH_NG":
                time.sleep(SLEEP_SEC)

        print()

        # ── バケットB ──
        print("=== バケットB: japanese_name + japanese_oracle_text 修正 ===")
        for card_id, set_code, card_name in bucket_b:
            cached = os.path.exists(cache_path(card_name))
            status, ja_name, ja_oracle = process_card(
                card_id, card_name, set_code,
                fix_name=True, fix_oracle=True,
                dry_run=dry_run, cur=cur,
            )
            c_flag = "C" if cached else " "
            label  = f"[B {c_flag}]"

            if status == "OK":
                print(f"{label} ({set_code}) {card_name}")
                print(f"       名前: {ja_name}")
                print(f"       本文: {ja_oracle[:80] if ja_oracle else '(なし)'}")
                counts["OK"] += 1
                updated_ids.append(card_id)
            else:
                print(f"{label} ({set_code}) {card_name}: {status}")
                if ja_name:
                    print(f"       名前OK: {ja_name}")
                if ja_oracle:
                    print(f"       本文OK: {ja_oracle[:80]}")
                updated_ids.append(card_id)
                counts[status] = counts.get(status, 0) + 1

            if not cached and status != "FETCH_NG":
                time.sleep(SLEEP_SEC)

        print()

        # ── バケットC ──
        print("=== バケットC: Tam japanese_oracle_text のみ ===")
        for card_id, set_code, card_name in bucket_c:
            cached = os.path.exists(cache_path(card_name))
            status, _, ja_oracle = process_card(
                card_id, card_name, set_code,
                fix_name=False, fix_oracle=True,
                dry_run=dry_run, cur=cur,
            )
            c_flag = "C" if cached else " "
            label  = f"[C {c_flag}]"

            if status == "OK":
                print(f"{label} ({set_code}) {card_name}")
                print(f"       本文: {ja_oracle[:120] if ja_oracle else '(なし)'}")
                counts["OK"] += 1
                updated_ids.append(card_id)
            else:
                print(f"{label} ({set_code}) {card_name}: {status}")
                counts[status] = counts.get(status, 0) + 1

            if not cached and status != "FETCH_NG":
                time.sleep(SLEEP_SEC)

        if not dry_run:
            conn.commit()

    conn.close()

    # 基本土地 (hob) の結果を明示
    hob_ids = [r[0] for r in bucket_a if r[1] == 'hob']
    print()
    print(f"=== 基本土地 (hob) ===")
    print(f"  対象 ID: {sorted(hob_ids)}")
    print(f"  件数: {len(hob_ids)}")

    print()
    print(f"=== 集計 ===")
    for k, v in counts.items():
        if v:
            print(f"  {k}: {v}")
    print(f"  更新対象 ID 数: {len(updated_ids)}")

    if not dry_run and updated_ids:
        with open(IDS_OUT, 'w') as f:
            for cid in sorted(updated_ids):
                f.write(f"{cid}\n")
        print(f"\n次のステップ:")
        print(f"  embed_text 再構築:")
        print(f"    python rebuild_embed_text.py --update_text --card_ids_file {IDS_OUT}")
        print(f"  reembed SMALL_V2:")
        print(f"    python rebuild_embed_text.py --reembed --model SMALL_V2 --card_ids_file {IDS_OUT}")
        print(f"  reembed BASE_V2:")
        print(f"    python rebuild_embed_text.py --reembed --model BASE_V2 --card_ids_file {IDS_OUT}")
    elif dry_run:
        print(f"\n--run で上記を実際に UPDATE します。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="日本語スロット英語混入 30件の補填")
    parser.add_argument("--sample", action="store_true", help="DRY RUN（DB 更新なし）")
    parser.add_argument("--run",    action="store_true", help="DB UPDATE 実行")
    args = parser.parse_args()

    if args.sample:
        run(dry_run=True)
    elif args.run:
        run(dry_run=False)
    else:
        parser.print_help()
