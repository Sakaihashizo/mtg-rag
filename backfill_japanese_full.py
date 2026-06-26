"""
backfill_japanese_full.py — 日本語オラクル/名前の包括 backfill（2026-06-21）
==============================================================================
背景:
  従来 me2/me4 を「online専用＝埋め不可」と誤って除外し、また scope-C の
  オラクル取得が T3 の両面名補填より前に走ったため、(a) me2/me4 の単面再録、
  (b) 両面カード、(c) 名前も空の旧セット が日本語オラクル空で取り残された。
  whisper(Wisdom Guild) は旧セット(Legends 等・日本語未発売)も公式 Oracle 訳を
  持つことを確認済み（孤島の聖域/Cyclonus/Adventurers' Guildhouse 等で実証）。

対象:
  japanese_oracle_text 空 AND oracle_text あり AND set_code NOT IN(EXCLUDE_SETS)
  ※ me2/me4 を含む。un系(unf/ust/unk)・slx(Universes Within=JPなし)のみ除外。

挙動:
  - 単面: テキスト1行/名前1セルを取得。
  - 両面(card_faces>=2): 「A // B」で名前/オラクルを取得（主ページで面数不足なら
    面ごとに個別 fetch して補完。add_face_names と同方式）。
  - japanese_oracle_text は JP が取れたら充填。
  - japanese_name は「現在空」のときだけ充填（既存名は壊さない・JP名が取れた時のみ。
    日本語未発売で JP 名が無いカードは whisper も英語名なので NULL のまま=正当）。
  - whisper 未掲載 / JP無し は skip（英語専用カードは空のまま残す）。
  - 2.5s/req・wg_cache 再利用・再開可能（既に埋まっていれば対象から外れる）。

使い方:
  python backfill_japanese_full.py --status
  python backfill_japanese_full.py --sample   # 代表カードで動作確認
  python backfill_japanese_full.py --run       # 全件（長い→nohup 推奨）
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
CACHE_DIR   = "/mnt/mtg_rag/wg_cache"
IDS_FILE    = "/mnt/mtg_rag/backfill_ja_ids.txt"
# 真の英語専用 / un系 / 出所不明のみ除外。me2/me4 は対象に含める。
EXCLUDE_SETS = ('unf', 'ust', 'unk', 'slx')

JAPANESE_RE = re.compile(r'[ぁ-んァ-ン一-龯]')
FURIGANA_RE = re.compile(r'（[ぁ-んァ-ヶー]+）')
TAG_RE      = re.compile(r'<[^>]+>')
WS_RE       = re.compile(r'\s+')
TEXT_CELL_RE = re.compile(
    r'<th[^>]*class="dc"[^>]*>\s*テキスト\s*</th>\s*<td[^>]*class="lc"[^>]*>(.*?)</td>',
    re.DOTALL | re.IGNORECASE)
NAME_CELL_RE = re.compile(
    r'<td[^>]*class="[dm]*mc"[^>]*>\s*<b>([^<]+)</b>',
    re.DOTALL | re.IGNORECASE)


def is_japanese(t): return bool(JAPANESE_RE.search(t or ''))
def clean_html(raw): return WS_RE.sub(' ', html_module.unescape(TAG_RE.sub('', raw))).strip()
def clean_furigana(t): return FURIGANA_RE.sub('', t or '').strip()
def cache_path(name): return os.path.join(CACHE_DIR, urllib.parse.quote(name, safe='') + ".html")


def fetch_html(name):
    """(body, from_cache, err)"""
    p = cache_path(name)
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            return f.read(), True, None
    req = urllib.request.Request(
        BASE_URL.format(name=urllib.parse.quote(name)),
        headers={'User-Agent': 'MTG-RAG-Project/1.0 (educational; non-commercial)'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode('utf-8', errors='replace')
    except Exception as e:
        return None, False, f"fetch error: {e}"
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(body)
    return body, False, None


def extract_oracles(body):
    return [clean_html(m) for m in TEXT_CELL_RE.findall(body)]


def extract_names(body):
    return [clean_furigana(html_module.unescape(raw).split('/')[0].strip())
            for raw in NAME_CELL_RE.findall(body)]


def scrape_card(card_name, expected_faces):
    """(ja_oracle|None, ja_name|None, from_cache, err)"""
    body, cached, err = fetch_html(card_name)
    if err:
        return None, None, cached, err
    oracles = extract_oracles(body)
    names = extract_names(body)
    # 両面で主ページが面数不足なら面ごとに個別 fetch
    if expected_faces >= 2 and len(oracles) < expected_faces:
        faces = [f.strip() for f in card_name.split('//')]
        oracles, names = [], []
        for fe in faces:
            fb, fc, ferr = fetch_html(fe)
            if not ferr and fb:
                fo = extract_oracles(fb); fn = extract_names(fb)
                oracles.append(fo[0] if fo else '')
                names.append(fn[0] if fn else '')
            else:
                oracles.append(''); names.append('')
            if not fc:
                time.sleep(SLEEP_SEC)
    ja_oracle_parts = [o for o in oracles if o and is_japanese(o)]
    ja_name_parts   = [n for n in names if n and is_japanese(n)]
    ja_oracle = ' // '.join(ja_oracle_parts) if ja_oracle_parts else None
    ja_name   = ' // '.join(ja_name_parts) if ja_name_parts else None
    return ja_oracle, ja_name, cached, None


def get_targets(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, card_name, japanese_name,
                   CASE WHEN card_faces_json IS NOT NULL
                        THEN jsonb_array_length(card_faces_json) ELSE 1 END AS faces
            FROM mtg_cards_v2
            WHERE (japanese_oracle_text IS NULL OR btrim(japanese_oracle_text)='')
              AND oracle_text IS NOT NULL AND btrim(oracle_text)<>''
              AND set_code NOT IN %s
            ORDER BY id
        """, (EXCLUDE_SETS,))
        return cur.fetchall()


def cmd_status(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_targets(conn)
    cached = sum(1 for _, n, _, _ in cards if os.path.exists(cache_path(n)))
    conn.close()
    print(f"対象: {len(cards)} 件 / キャッシュ済 {cached} / 未キャッシュ {len(cards)-cached}")
    print(f"予想: 未キャッシュ × 2.5s = 約 {(len(cards)-cached)*2.5/60:.0f} 分")


def cmd_sample(args):
    samples = [
        ("Island Sanctuary", 1), ("Cyclonus, the Saboteur // Cyclonus, Cybertronian Fighter", 2),
        ("Adventurers' Guildhouse", 1), ("Abbey Matron", 1),
        ("Great Hall of the Biblioplex", 1), ("Acid Rain", 1),
    ]
    for name, faces in samples:
        ja_o, ja_n, cached, err = scrape_card(name, faces)
        tag = 'C' if cached else ' '
        print(f"[{tag}] {name}")
        print(f"     name : {ja_n}")
        print(f"     oracle: {(ja_o or '(なし)')[:90]}")
        if not cached:
            time.sleep(SLEEP_SEC)
    print("\n問題なければ --run（nohup 推奨）。")


def cmd_run(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_targets(conn)
    total = len(cards)
    print(f"対象: {total} 件 開始", flush=True)
    filled_oracle = filled_name = skipped = 0
    ids = []
    with conn.cursor() as cur:
        for i, (cid, name, cur_janame, faces) in enumerate(cards, 1):
            cached = os.path.exists(cache_path(name))
            ja_o, ja_n, _, err = scrape_card(name, faces)
            if err:
                skipped += 1
                print(f"[{i}/{total}] FETCH_NG {name}: {err}", flush=True)
                continue
            sets, params = [], []
            if ja_o:
                sets.append("japanese_oracle_text=%s"); params.append(ja_o)
                filled_oracle += 1
            if ja_n and not (cur_janame and cur_janame.strip()):
                sets.append("japanese_name=%s"); params.append(ja_n)
                filled_name += 1
            if sets:
                params.append(cid)
                cur.execute(f"UPDATE mtg_cards_v2 SET {', '.join(sets)} WHERE id=%s", params)
                ids.append(cid)
                if i % 50 == 0:
                    print(f"[{i}/{total}] OK {name}: {(ja_o or '')[:45]}", flush=True)
            else:
                skipped += 1
            if i % 100 == 0:
                conn.commit()
                with open(IDS_FILE, 'w') as f:
                    f.write('\n'.join(map(str, ids)) + '\n')
                print(f"  → commit {len(ids)} 件 ({i}/{total})", flush=True)
            if not cached:
                time.sleep(SLEEP_SEC)
    conn.commit()
    conn.close()
    with open(IDS_FILE, 'w') as f:
        f.write('\n'.join(map(str, ids)) + ('\n' if ids else ''))
    print(f"\n完了: oracle充填={filled_oracle} / name充填={filled_name} / skip={skipped} / 対象={total}")
    print(f"reembed 対象 ID: {IDS_FILE}（{len(ids)} 件）", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if a.status: cmd_status(a)
    elif a.sample: cmd_sample(a)
    elif a.run: cmd_run(a)
    else: ap.print_help()
