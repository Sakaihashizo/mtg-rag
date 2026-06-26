"""
restore_missing_50.py — 欠落 50 件を Scryfall collection エンドポイントから取得して core DB に復帰

使い方:
  # 取得内容を確認（DB 書込なし）
  python restore_missing_50.py --sample

  # DB INSERT 実行
  python restore_missing_50.py --run

  # 検証のみ
  python restore_missing_50.py --verify
"""

import json
import time
import argparse
import urllib.request
import urllib.error
import psycopg2
import psycopg2.extras

from db_config import DB_CONFIG

SCRYFALL_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
SCRYFALL_NAMED_URL = "https://api.scryfall.com/cards/named"
USER_AGENT = "mtg-rag-restore/1.0 (educational; non-commercial)"
IDS_OUT = "/mnt/mtg_rag/restore_50_ids.txt"

# 監査ドキュメント確定 50 件（oracle_cards_existing_audit_20260619.md）
MISSING_NAMES = [
    "Adorned Pouncer",
    "Agate Instigator",
    "Angel of Sanctions",
    "Arco-Flagellant",
    "Assembly-Worker",
    "Aven Initiate",
    "Aven Wind Guide",
    "Blink",
    "Champion of Wits",
    "Coruscation Mage",
    "Cunning",
    "Darkstar Augur",
    "Dinosaur Egg",
    "Dragon Egg",
    "Earth Rumble",
    "Earthshaker Khenra",
    "Fast // Furious",
    "Glyph Keeper",
    "Goblin Wizard",
    "Honored Hydra",
    "Inferno",
    "Intrepid Rabbit",
    "Kobolds of Kher Keep",
    "Labyrinth Guardian",
    "Oketra's Attendant",
    "Ornithopter",
    "Pawpatch Recruit",
    "Phyrexian Hydra",
    "Pick Your Poison",
    "Proven Combatant",
    "Red Herring",
    "Resilient Khenra",
    "Sacred Cat",
    "Scarecrow",
    "Shapeshifter",
    "Sicarian Infiltrator",
    "Space Marine Devastator",
    "Spark Elemental",
    "Starscape Cleric",
    "Sunscourge Champion",
    "Tarmogoyf",
    "Temmet, Vizier of Naktamun",
    "Tender Wildguide",
    "Timeless Dragon",
    "Trueheart Duelist",
    "Ultramarines Honour Guard",
    "Unwavering Initiate",
    "Vanguard Suppressor",
    "Warren Warleader",
    "Zephyrim",
]

# token 系・非カード layout
INVALID_LAYOUTS = frozenset({
    "token", "emblem", "double_faced_token", "art_series",
    "vanguard", "planar", "scheme",
})


def scryfall_collection(names: list[str]) -> tuple[list[dict], list[dict]]:
    """Scryfall collection エンドポイントで取得。
    戻り値: (found_cards, not_found)
    429 時は 1s 待って再試行（最大 3 回）。
    """
    identifiers = [{"name": n} for n in names]
    body = json.dumps({"identifiers": identifiers}).encode("utf-8")

    for attempt in range(3):
        req = urllib.request.Request(
            SCRYFALL_COLLECTION_URL,
            data=body,
            headers={
                "User-Agent":   USER_AGENT,
                "Accept":       "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("data", []), data.get("not_found", [])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt
                print(f"  429 Too Many Requests → {wait}s 待機 (attempt {attempt+1})")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Scryfall API: 3 回リトライ後も 429")


def validate_card(card: dict) -> tuple[bool, str]:
    """本物カードか検証。(ok, reason)"""
    layout = card.get("layout", "")
    if layout in INVALID_LAYOUTS:
        return False, f"layout={layout}"
    vintage = card.get("legalities", {}).get("vintage", "")
    if vintage == "not_legal":
        return False, f"vintage=not_legal"
    return True, ""


def map_card(card: dict) -> dict:
    """Scryfall カードオブジェクト → mtg_cards_v2 カラムマッピング"""
    faces = card.get("card_faces")

    # type_line: 多面は ' // ' 結合
    if faces:
        type_line = " // ".join(f.get("type_line", "") for f in faces if f.get("type_line"))
    else:
        type_line = card.get("type_line") or ""

    # oracle_text: 多面は ' // ' 結合
    if faces:
        oracle_text = " // ".join(
            f.get("oracle_text", "") for f in faces if f.get("oracle_text") is not None
        )
    else:
        oracle_text = card.get("oracle_text") or ""

    # mana_cost: 多面は通常 top-level にない（split など）
    mana_cost = card.get("mana_cost")

    return {
        "card_name":         card["name"],
        "type_line":         type_line or None,
        "oracle_text":       oracle_text or None,
        "mana_cost":         mana_cost or None,
        "colors":            card.get("colors") or [],
        "color_identity":    card.get("color_identity") or [],
        "rarity":            card.get("rarity"),
        "layout":            card.get("layout"),
        "set_code":          card.get("set"),
        "set_name":          card.get("set_name"),
        "collector_number":  card.get("collector_number"),
        "cmc":               card.get("cmc"),
        "power":             card.get("power"),
        "toughness":         card.get("toughness"),
        "loyalty":           card.get("loyalty"),
        "card_faces_json":   json.dumps(faces) if faces else None,
        "keywords":          card.get("keywords") or [],
        "legalities":        json.dumps(card.get("legalities") or {}),
        "produced_mana":     card.get("produced_mana") or [],
        "edhrec_rank":       card.get("edhrec_rank"),
        "game_changer":      card.get("game_changer") or False,
        # 日本語・トーナメント・embedding は別パスで
        "japanese_name":     None,
        "japanese_oracle_text": None,
        "tournament_score":  None,
        "embed_text":        None,
        "face_cmcs":         None,
        "has_x":             None,
    }


INSERT_SQL = """
INSERT INTO mtg_cards_v2 (
    card_name, type_line, oracle_text, mana_cost,
    colors, color_identity, rarity, layout,
    set_code, set_name, collector_number, cmc,
    power, toughness, loyalty,
    card_faces_json, keywords, legalities, produced_mana,
    edhrec_rank, game_changer,
    japanese_name, japanese_oracle_text, tournament_score,
    embed_text, face_cmcs, has_x
) VALUES (
    %(card_name)s, %(type_line)s, %(oracle_text)s, %(mana_cost)s,
    %(colors)s, %(color_identity)s, %(rarity)s, %(layout)s,
    %(set_code)s, %(set_name)s, %(collector_number)s, %(cmc)s,
    %(power)s, %(toughness)s, %(loyalty)s,
    %(card_faces_json)s, %(keywords)s, %(legalities)s, %(produced_mana)s,
    %(edhrec_rank)s, %(game_changer)s,
    %(japanese_name)s, %(japanese_oracle_text)s, %(tournament_score)s,
    %(embed_text)s, %(face_cmcs)s, %(has_x)s
) RETURNING id
"""


def scryfall_named_exact(name: str) -> dict | None:
    """/cards/named?exact=<name> で 1 件取得（not_found フォールバック用）"""
    import urllib.parse
    url = SCRYFALL_NAMED_URL + "?exact=" + urllib.parse.quote(name)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_and_validate() -> tuple[list[dict], list[str], list[str]]:
    """Scryfall から取得して検証。
    戻り値: (valid_rows, skipped_names, not_found_names)
    """
    print(f"Scryfall collection API へ POST ... ({len(MISSING_NAMES)} 件)")
    found, not_found = scryfall_collection(MISSING_NAMES)
    print(f"  found={len(found)}, not_found={len(not_found)}")

    valid_rows = []
    skipped = []
    not_found_names = [nf.get("name") or str(nf) for nf in not_found]

    # not_found カードを /cards/named?exact= でフォールバック取得
    still_not_found = []
    for nf_name in not_found_names:
        print(f"  フォールバック取得: {nf_name}")
        time.sleep(0.1)
        card = scryfall_named_exact(nf_name)
        if card:
            print(f"    → 取得成功: {card['name']} ({card.get('set')})")
            found.append(card)
        else:
            still_not_found.append(nf_name)
            print(f"    → not_found")
    not_found_names = still_not_found

    for card in found:
        ok, reason = validate_card(card)
        if not ok:
            skipped.append(f"{card['name']}: {reason}")
            print(f"  [SKIP] {card['name']}: {reason}")
            continue
        valid_rows.append(map_card(card))

    return valid_rows, skipped, not_found_names


def cmd_sample(args):
    rows, skipped, not_found_names = fetch_and_validate()

    print(f"\n=== 取得結果 ===")
    print(f"  有効: {len(rows)} 件")
    print(f"  スキップ: {len(skipped)} 件")
    print(f"  not_found: {len(not_found_names)} 件")

    if not_found_names:
        print(f"\n  not_found: {not_found_names}")
    if skipped:
        print(f"\n  skip 理由: {skipped}")

    print(f"\n=== サンプル ===")
    for r in rows[:5]:
        print(f"  {r['card_name']} | {r['layout']} | {r['set_code']} | oracle: {(r['oracle_text'] or '')[:60]}")

    print(f"\n問題なければ --run で INSERT します。")


def cmd_run(args):
    rows, skipped, not_found_names = fetch_and_validate()

    if not_found_names:
        print(f"\n[WARNING] not_found: {not_found_names}")
    if skipped:
        print(f"\n[SKIP] {skipped}")

    print(f"\n INSERT 対象: {len(rows)} 件")

    conn = psycopg2.connect(**DB_CONFIG)
    new_ids = []

    with conn.cursor() as cur:
        # id sequence 安全化
        cur.execute("SELECT setval('mtg_cards_v2_id_seq', (SELECT max(id) FROM mtg_cards_v2));")
        seq_val = cur.fetchone()[0]
        print(f"  sequence → {seq_val}")

        for row in rows:
            # conflict ガード: 同名が既存なら STOP
            cur.execute(
                "SELECT id FROM mtg_cards_v2 WHERE card_name = %s",
                (row["card_name"],),
            )
            existing = cur.fetchone()
            if existing:
                conn.rollback()
                conn.close()
                raise RuntimeError(
                    f"card_name 重複: {row['card_name']} (id={existing[0]}) "
                    f"— 監査済みのはずなので調査が必要"
                )

            cur.execute(INSERT_SQL, row)
            new_id = cur.fetchone()[0]
            new_ids.append(new_id)
            print(f"  [OK] {row['card_name']} → id={new_id} ({row['set_code']})")

    conn.commit()
    conn.close()

    with open(IDS_OUT, "w") as f:
        for cid in sorted(new_ids):
            f.write(f"{cid}\n")

    print(f"\n完了: {len(new_ids)} 件 INSERT")
    print(f"not_found: {len(not_found_names)} 件 / skip: {len(skipped)} 件")
    print(f"\nnew_ids ファイル: {IDS_OUT} ({len(new_ids)} 件)")
    print(f"\n次のステップ:")
    print(f"  add_face_cmcs.py (冪等)")
    print(f"  rebuild_embed_text.py --update_text --card_ids_file {IDS_OUT}")
    print(f"  rebuild_embed_text.py --reembed --model SMALL_V2 --card_ids_file {IDS_OUT}")
    print(f"  rebuild_embed_text.py --reembed --model BASE_V2  --card_ids_file {IDS_OUT}")


def cmd_verify(args):
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        # 件数確認
        cur.execute("SELECT COUNT(*) FROM mtg_cards_v2")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM mtg_embeddings_small_v2")
        small = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM mtg_embeddings_base_v2")
        base = cur.fetchone()[0]

        # 50 件の存在確認
        missing = []
        for name in MISSING_NAMES:
            cur.execute(
                "SELECT id, layout, oracle_text FROM mtg_cards_v2 WHERE card_name = %s",
                (name,),
            )
            row = cur.fetchone()
            if row is None:
                missing.append(name)
            else:
                cid, layout, oracle = row
                if not oracle:
                    print(f"  [WARN] {name} (id={cid}): oracle_text 空")
                elif layout in INVALID_LAYOUTS:
                    print(f"  [WARN] {name} (id={cid}): layout={layout}")

    conn.close()

    print(f"mtg_cards_v2:            {total} 件（目標: 30,982）")
    print(f"mtg_embeddings_small_v2: {small} 件（目標: 30,982）")
    print(f"mtg_embeddings_base_v2:  {base} 件（目標: 30,982）")
    print(f"50件存在確認: {len(MISSING_NAMES) - len(missing)} / {len(MISSING_NAMES)}")
    if missing:
        print(f"  まだ欠落: {missing}")
    else:
        print(f"  全50件存在 ✓")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="欠落 50 件を Scryfall から取得して core DB に復帰")
    parser.add_argument("--sample", action="store_true", help="DRY RUN: 取得・検証のみ")
    parser.add_argument("--run",    action="store_true", help="DB INSERT 実行")
    parser.add_argument("--verify", action="store_true", help="完了条件を確認")
    args = parser.parse_args()

    if args.sample:
        cmd_sample(args)
    elif args.run:
        cmd_run(args)
    elif args.verify:
        cmd_verify(args)
    else:
        parser.print_help()
