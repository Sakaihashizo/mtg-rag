"""
add_face_names.py — 両面系カードの japanese_name を補填

対象: layout IN ('transform','adventure','split','modal_dfc','flip')
      かつ japanese_name IS NULL / ''

手順:
  1. oracle_cards.json から card_faces[].printed_name を取得（is_japanese 検証）
  2. バルクで取れない場合は whisper フォールバック（wg_cache/ 再利用）
  3. フリガナ除去・末尾 ' // ' 除去
  4. サンプル表示（--sample）→ 本人確認後に mass-UPDATE（--run）

使い方:
  python add_face_names.py --sample     # サンプル10件表示
  python add_face_names.py --run        # 全件 UPDATE
  python add_face_names.py --status     # 状況確認
"""

import re
import json
import html as html_module
import time
import os
import argparse
import urllib.request
import urllib.parse
import psycopg2

from db_config import DB_CONFIG

BULK_FILE  = "/mnt/new_hdd/oracle_cards.json"
CACHE_DIR  = "/mnt/mtg_rag/wg_cache"
BASE_URL   = "http://whisper.wisdom-guild.net/card/{name}/"
SLEEP_SEC  = 2.5
SAMPLE_SIZE = 10

# layout ホワイトリストは使わない（prepare 等の新メカニズム漏れを防ぐため）
# 代わりに card_faces_json 構造で選定する

JAPANESE_RE   = re.compile(r'[ぁ-んァ-ン一-龯]')
FURIGANA_RE   = re.compile(r'（[ぁ-んァ-ヶー]+）')
TAG_RE        = re.compile(r'<[^>]+>')
WHITESPACE_RE = re.compile(r'\s+')

# whisper の両面カード名セル（mc=表面 / dmc=裏面）
# <td class="mc"><b>日本語名/英語名</b> の形式
CARD_NAME_CELL_RE = re.compile(
    r'<td[^>]*class="[dm]*mc"[^>]*>\s*<b>([^<]+)</b>',
    re.DOTALL | re.IGNORECASE,
)


def is_japanese(text: str) -> bool:
    return bool(JAPANESE_RE.search(text))


def clean_furigana(text: str) -> str:
    text = FURIGANA_RE.sub('', text)
    return text.strip()


def clean_html(raw: str) -> str:
    text = TAG_RE.sub('', raw)
    text = html_module.unescape(text)
    return WHITESPACE_RE.sub(' ', text).strip()


def build_ja_name_from_faces(faces: list[dict]) -> str | None:
    """card_faces から日本語面名を構築。'面1 // 面2' 形式。"""
    parts = []
    for face in faces:
        name = (face.get('printed_name') or '').strip()
        name = clean_furigana(name)
        if name and is_japanese(name):
            parts.append(name)
    if not parts:
        return None
    return ' // '.join(parts)


def cache_path(card_name: str) -> str:
    safe = urllib.parse.quote(card_name, safe='')
    return os.path.join(CACHE_DIR, safe + ".html")


def fetch_html(card_name: str) -> tuple[str | None, str | None]:
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


def _extract_ja_parts_from_html(body: str) -> list[str]:
    """HTML から日本語面名リストを抽出。"""
    matches = CARD_NAME_CELL_RE.findall(body)
    parts = []
    for raw in matches:
        raw_clean = html_module.unescape(raw).strip()
        ja_part = raw_clean.split('/')[0].strip()
        ja_part = clean_furigana(ja_part)
        if ja_part and is_japanese(ja_part):
            parts.append(ja_part)
    return parts


def scrape_ja_name(card_name: str) -> tuple[str | None, str | None, bool]:
    """whisper からカード名を取得。戻り値: (ja_name, error, from_cache)

    まず card_name 全体でページを取得（transform/split 等は1ページに両面掲載）。
    prepare カードは表面のみ掲載のため、面名が足りない場合は各面名を個別にフェッチして補完。
    """
    path = cache_path(card_name)
    cached = os.path.exists(path)
    body, err = fetch_html(card_name)
    if err:
        return None, err, cached

    face_names = [f.strip() for f in card_name.split('//')]
    expected_faces = len(face_names)

    parts = _extract_ja_parts_from_html(body) if body else []

    # 面数が足りない場合（prepare 等）は各面を個別にフェッチして補完
    if len(parts) < expected_faces:
        補完 = []
        for i, face_en in enumerate(face_names):
            if i < len(parts):
                補完.append(parts[i])
                continue
            # この面だけ個別フェッチ
            face_cached = os.path.exists(cache_path(face_en))
            face_body, face_err = fetch_html(face_en)
            if not face_err and face_body:
                face_parts = _extract_ja_parts_from_html(face_body)
                if face_parts:
                    補完.append(face_parts[0])
                    if not face_cached:
                        time.sleep(SLEEP_SEC)
                    continue
            if not face_cached:
                time.sleep(SLEEP_SEC)
        parts = 補完

    if not parts:
        return None, "name cell not found", cached

    ja_parts = [p for p in parts if p]
    if not ja_parts:
        return None, f"no japanese found in page", cached

    return ' // '.join(ja_parts), None, cached


def load_bulk_names(target_names: set[str]) -> dict[str, str]:
    """
    oracle_cards.json から両面系カードの日本語面名を収集。
    戻り値: {card_name: ja_name}
    """
    result: dict[str, str] = {}
    print(f"バルクスキャン中: {BULK_FILE}")
    with open(BULK_FILE, encoding='utf-8') as f:
        cards = json.load(f)
    for card in cards:
        name = (card.get('name') or '').strip()
        if name not in target_names:
            continue
        faces = card.get('card_faces') or []
        if not faces:
            continue
        ja_name = build_ja_name_from_faces(faces)
        if ja_name:
            result[name] = ja_name
    print(f"バルクで取得: {len(result)} 件")
    return result


def get_target_cards(conn) -> list[tuple[int, str]]:
    """対象カード: (id, card_name)
    card_faces_json 構造で選定（layout ホワイトリスト不使用）。
    - japanese_name が空: 未補填
    - japanese_name に ' // ' が無い: 補填済みだが後半面名が欠落
    両方を対象にして全2面カードを「A // B」形式に揃える。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, card_name
            FROM mtg_cards_v2
            WHERE card_faces_json IS NOT NULL
              AND jsonb_array_length(card_faces_json) >= 2
              AND (
                japanese_name IS NULL
                OR japanese_name = ''
                OR japanese_name NOT LIKE '% // %'
              )
            ORDER BY id
        """)
        return cur.fetchall()


def cmd_status(args):
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT layout, count(*)
            FROM mtg_cards_v2
            WHERE card_faces_json IS NOT NULL
              AND jsonb_array_length(card_faces_json) >= 2
              AND (
                japanese_name IS NULL
                OR japanese_name = ''
                OR japanese_name NOT LIKE '% // %'
              )
            GROUP BY layout ORDER BY count(*) DESC
        """)
        rows = cur.fetchall()
        cur.execute("SELECT count(*) FROM mtg_cards_v2 WHERE japanese_name LIKE '%損耗%'")
        wear = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM mtg_cards_v2 WHERE japanese_name LIKE '%悪魔の教示者%'")
        demonic = cur.fetchone()[0]
    conn.close()
    print("両面系 japanese_name 欠落（card_faces_json ベース）:")
    for layout, cnt in rows:
        print(f"  {layout}: {cnt} 件")
    print(f"損耗ヒット: {wear} 件")
    print(f"悪魔の教示者ヒット: {demonic} 件")


def cmd_sample(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_target_cards(conn)
    conn.close()

    target_names = {name for _, name in cards}
    bulk_map = load_bulk_names(target_names)

    # prepare + adventure 両面化の代表例をサンプルに含める
    priority = [
        'Emeritus of Woe // Demonic Tutor',   # prepare: 表面新・裏面有名再録
        'Brazen Borrower // Petty Theft',      # adventure: 補填済み単一名 → 両面化
        'Curious Pair // Treats to Share',     # adventure: 同上
        'Crescendo Conductor // Boltwave',     # prepare: 裏面有名再録
        'Scathing Shadelock // Venomous Words', # prepare
    ]
    sample_names = [n for n in priority if any(name == n for _, name in cards)]
    remaining = [name for _, name in cards if name not in sample_names]
    sample = [(i, n) for i, n in cards
              if n in sample_names or n in remaining[:SAMPLE_SIZE - len(sample_names)]][:SAMPLE_SIZE]

    print(f"対象総数: {len(cards)} 件\nサンプル {len(sample)} 件:\n")
    for card_id, card_name in sample:
        bulk = bulk_map.get(card_name)
        if bulk:
            print(f"[バルク] {card_name}")
            print(f"         → {bulk}")
        else:
            ja_name, err, cached = scrape_ja_name(card_name)
            label = 'キャッシュ' if cached else 'whisper'
            if ja_name:
                print(f"[{label}] {card_name}")
                print(f"         → {ja_name}")
            else:
                print(f"[NG]     {card_name}: {err}")
            if not cached:
                time.sleep(SLEEP_SEC)
        print()

    print("問題なければ --run で全件 UPDATE してください。")


def cmd_run(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_target_cards(conn)
    total = len(cards)
    print(f"対象: {total} 件")

    target_names = {name for _, name in cards}
    bulk_map = load_bulk_names(target_names)

    updated = 0
    bulk_count = 0
    whisper_count = 0
    skip_count = 0
    updated_ids: list[int] = []

    with conn.cursor() as cur:
        for i, (card_id, card_name) in enumerate(cards, 1):
            ja_name = bulk_map.get(card_name)
            source = 'バルク'

            if not ja_name:
                cached = os.path.exists(cache_path(card_name))
                ja_name, err, _ = scrape_ja_name(card_name)
                source = 'whisper'
                if not ja_name:
                    skip_count += 1
                    print(f"[{i}/{total}] NG  {card_name}: {err}")
                    if not cached:
                        time.sleep(SLEEP_SEC)
                    continue
                if not cached:
                    time.sleep(SLEEP_SEC)

            cur.execute(
                "UPDATE mtg_cards_v2 SET japanese_name = %s WHERE id = %s",
                (ja_name, card_id),
            )
            updated_ids.append(card_id)
            updated += 1
            if source == 'バルク':
                bulk_count += 1
            else:
                whisper_count += 1
            print(f"[{i}/{total}] {source}  {card_name} → {ja_name}")

            if updated % 100 == 0:
                conn.commit()
                print(f"  → {updated} 件コミット済み")

    conn.commit()
    conn.close()

    # 全角スペース番兵の掃除
    print("\n全角スペース番兵を NULL に掃除中...")
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM mtg_cards_v2 WHERE japanese_oracle_text = '　'"
        )
        sentinel_ids = [r[0] for r in cur.fetchall()]
        cur.execute(
            "UPDATE mtg_cards_v2 SET japanese_oracle_text = NULL WHERE japanese_oracle_text = '　'"
        )
    conn.commit()
    conn.close()
    print(f"番兵掃除: {len(sentinel_ids)} 件 → NULL")

    # reembed 対象 id リスト保存
    all_ids = list(set(updated_ids + sentinel_ids))
    ids_file = "/mnt/mtg_rag/t3_reembed_ids.txt"
    with open(ids_file, 'w') as f:
        for cid in sorted(all_ids):
            f.write(f"{cid}\n")

    print(f"\n完了: 更新={updated}（バルク={bulk_count} / whisper={whisper_count}）/ スキップ={skip_count} / 番兵掃除={len(sentinel_ids)}")
    print(f"reembed 対象 ID: {ids_file}（{len(all_ids)} 件）")
    print(f"\n次のステップ:")
    print(f"  embed_text 再構築: python rebuild_embed_text.py --update_text")
    print(f"  reembed SMALL_V2:  python rebuild_embed_text.py --reembed --model SMALL_V2 --card_ids_file {ids_file}")
    print(f"  reembed BASE_V2:   python rebuild_embed_text.py --reembed --model BASE_V2  --card_ids_file {ids_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="両面系カードの japanese_name 補填")
    parser.add_argument("--sample", action="store_true", help="サンプル表示")
    parser.add_argument("--run",    action="store_true", help="全件 UPDATE")
    parser.add_argument("--status", action="store_true", help="状況確認")
    args = parser.parse_args()

    if args.status:
        cmd_status(args)
    elif args.sample:
        cmd_sample(args)
    elif args.run:
        cmd_run(args)
    else:
        parser.print_help()
