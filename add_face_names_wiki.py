"""
add_face_names_wiki.py — whisper で取れなかった29件を all_cards_scryfall.json から補填

soc 6件は本人確認済み正解を直接 UPDATE。
sos 21件・slx 2件は all_cards_scryfall.json（全言語版）の card_faces[].printed_name から取得。

使い方:
  python add_face_names_wiki.py --sample
  python add_face_names_wiki.py --run
  python add_face_names_wiki.py --status
"""

import re
import json
import argparse
import psycopg2

from db_config import DB_CONFIG

ALL_CARDS_FILE = "/mnt/new_hdd/all_cards_scryfall.json"

FURIGANA_RE = re.compile(r'（[ぁ-んァ-ヶー]+）')
JAPANESE_RE = re.compile(r'[ぁ-んァ-ン一-龯]')

# soc 6件: 本人 MTG Wiki 確認済み確定正解
SOC_CONFIRMED = {
    'Defacing Duskmage // Vandal\'s Edit':      '落書きの薄暮魔道士 // 蛮人による編集',
    'Eccentric Pestfinder // Turn Stones':       '変わり者の害獣探し // 石の裏返し',
    'Inspired Skypainter // Maestro\'s Gift':    '見事なる天描師 // 巨匠の贈り物',
    'Lorehold Archivist // Restore Relic':       'ロアホールドの文書管理人 // 秘宝の修復',
    'Naktamun Lorespinner // Wheel of Fortune':  'ナクタムンの伝承紡ぎ // 運命の輪',
    'Striding Shotcaller // Run the Play':       '一足飛びの司令塔 // 作戦通り',
}

TARGET_SETS = ('sos', 'soc', 'slx')


def is_japanese(text: str) -> bool:
    return bool(JAPANESE_RE.search(text))


def clean_furigana(text: str) -> str:
    return FURIGANA_RE.sub('', text).strip()


def get_target_cards(conn) -> list[tuple[int, str, str]]:
    """対象カード: (id, card_name, set_code)"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, card_name, set_code
            FROM mtg_cards_v2
            WHERE card_faces_json IS NOT NULL
              AND jsonb_array_length(card_faces_json) >= 2
              AND (
                japanese_name IS NULL
                OR japanese_name = ''
                OR japanese_name NOT LIKE '%% // %%'
              )
              AND set_code IN %s
            ORDER BY set_code, id
        """, (TARGET_SETS,))
        return cur.fetchall()


def load_scryfall_ja_names(target_en_names: set[str]) -> dict[str, str]:
    """
    all_cards_scryfall.json（1行1カード形式）から対象カードの日本語名を収集。
    grep で sos/soc/slx の ja 行だけ抽出してメモリを節約。
    戻り値: {英語カード名: 日本語名（'A // B' 形式）}
    """
    import subprocess
    result: dict[str, str] = {}
    print(f"grep 抽出中: sos/soc/slx × lang=ja ...")

    proc = subprocess.run(
        ['bash', '-c',
         f'grep \'"lang":"ja"\' {ALL_CARDS_FILE} | grep -E \'"set":"(sos|soc|slx)"\''],
        capture_output=True, text=True, timeout=120,
    )
    lines = proc.stdout.splitlines()
    print(f"  抽出行数: {len(lines)}")

    for line in lines:
        try:
            card = json.loads(line)
        except json.JSONDecodeError:
            continue
        en_name = (card.get('name') or '').strip()
        if en_name not in target_en_names:
            continue
        if en_name in result:
            continue

        faces = card.get('card_faces') or []
        if faces:
            parts = []
            for face in faces:
                pn = clean_furigana((face.get('printed_name') or '').strip())
                if pn and is_japanese(pn):
                    parts.append(pn)
            if parts:
                result[en_name] = ' // '.join(parts)
        else:
            pn = clean_furigana((card.get('printed_name') or '').strip())
            if pn and is_japanese(pn):
                result[en_name] = pn

    print(f"Scryfall から取得: {len(result)} 件")
    return result


def cmd_status(args):
    conn = psycopg2.connect(**DB_CONFIG)
    rows = get_target_cards(conn)
    conn.close()
    from collections import Counter
    cnt = Counter(r[2] for r in rows)
    print("残り対象（whisper 未取得）:")
    for s, c in sorted(cnt.items()):
        print(f"  {s}: {c} 件")
    print(f"合計: {len(rows)} 件")


def cmd_sample(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_target_cards(conn)
    conn.close()

    target_names = {name for _, name, _ in cards}
    scryfall_map = load_scryfall_ja_names(target_names)

    print(f"\n対象: {len(cards)} 件\n")
    for card_id, card_name, set_code in cards:
        # soc 確定正解
        if card_name in SOC_CONFIRMED:
            ja = SOC_CONFIRMED[card_name]
            print(f"[確定] ({set_code}) {card_name}")
            print(f"       → {ja}")
        elif card_name in scryfall_map:
            print(f"[Scryfall] ({set_code}) {card_name}")
            print(f"           → {scryfall_map[card_name]}")
        else:
            print(f"[NG] ({set_code}) {card_name}: 取得不可")
        print()

    print("問題なければ --run で UPDATE してください。")


def cmd_run(args):
    conn = psycopg2.connect(**DB_CONFIG)
    cards = get_target_cards(conn)
    total = len(cards)
    print(f"対象: {total} 件")

    target_names = {name for _, name, _ in cards}
    scryfall_map = load_scryfall_ja_names(target_names)

    updated = 0
    skipped = 0
    updated_ids: list[int] = []

    with conn.cursor() as cur:
        for card_id, card_name, set_code in cards:
            if card_name in SOC_CONFIRMED:
                ja_name = SOC_CONFIRMED[card_name]
                source = '確定'
            elif card_name in scryfall_map:
                ja_name = scryfall_map[card_name]
                source = 'Scryfall'
            else:
                skipped += 1
                print(f"[NG] ({set_code}) {card_name}: 取得不可")
                continue

            cur.execute(
                "UPDATE mtg_cards_v2 SET japanese_name = %s WHERE id = %s",
                (ja_name, card_id),
            )
            updated_ids.append(card_id)
            updated += 1
            print(f"[{source}] ({set_code}) {card_name} → {ja_name}")

    conn.commit()
    conn.close()

    ids_file = "/mnt/mtg_rag/t3_wiki_reembed_ids.txt"
    with open(ids_file, 'w') as f:
        for cid in sorted(updated_ids):
            f.write(f"{cid}\n")

    print(f"\n完了: 更新={updated} / スキップ={skipped} / 合計={total}")
    print(f"reembed 対象 ID: {ids_file}（{len(updated_ids)} 件）")
    print(f"\n次のステップ:")
    print(f"  embed_text 再構築: python rebuild_embed_text.py --update_text")
    print(f"  reembed SMALL_V2:  python rebuild_embed_text.py --reembed --model SMALL_V2 --card_ids_file {ids_file}")
    print(f"  reembed BASE_V2:   python rebuild_embed_text.py --reembed --model BASE_V2  --card_ids_file {ids_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="whisper 未取得29件を Scryfall 全言語版から補填")
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--run",    action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        cmd_status(args)
    elif args.sample:
        cmd_sample(args)
    elif args.run:
        cmd_run(args)
    else:
        parser.print_help()
