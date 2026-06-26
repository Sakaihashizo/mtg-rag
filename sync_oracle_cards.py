"""
sync_oracle_cards.py — oracle_cards 起点の English カード同期（go-forward 取り込み）
================================================================================
背景:
  従来の import_cards.py は all_cards（全印刷）を全件走査して
  `INSERT ... ON CONFLICT(card_name) DO NOTHING` していたため、
  同名の token / front card / playtest が先に入ると本物カードが弾かれた
  （2026-06-19 監査 docs/me/oracle_cards_existing_audit_20260619.md で 50 件実証）。

方針（監査 rec#2 + 2026-06-19 本人決定）:
  - 英語カード本体は **oracle_cards**（名前単位で 1 オブジェクト）を正準ソースにする。
  - 名前ごとに「本物カード（Vintage 合法・非 token レイアウト）」を解決して採用する。
  - **新規 insert は完全投入**（card_faces_json / set_code / edhrec_rank 等も含む）。
  - **既存 update は検索/正確性に効く列だけ**（UPDATE_COLS）。
    edhrec_rank（時間変動）/ collector_number・set_code・set_name（代表print ノイズ）/
    card_faces_json（揮発フィールド込みの churn 源）は既存行では更新しない。
  - 日本語・embed_text・face_cmcs・has_x・tournament_score は同期対象外＝既存値を温存。
  - 比較は**正規化**（NULL/''/[] を等価・配列はソート・oracle は空白圧縮）して
    「表現差」を実差分と誤検知しない（不在は NULL の規約を壊さない）。
  - 変更があった行のうち **embed に効く列が変わった分だけ** を reembed 対象に出力。
  - 日本語は別パイプライン（whisper / lang:ja）で後追い。本ツールは触らない。

責務分離（本ツールはカードデータ同期のみ。embedding は既存ツール）:
  本ツール → mtg_cards_v2 の英語列を同期 + 変更 id を出力
  その後   → add_face_cmcs.py（face_cmcs/has_x）
            rebuild_embed_text.py --update_text --card_ids_file <ids>
            rebuild_embed_text.py --reembed --model SMALL_V2/BASE_V2 --card_ids_file <ids>

使い方:
  python sync_oracle_cards.py --bulk /path/oracle_cards.json            # dry-run（既定）
  python sync_oracle_cards.py --bulk ... --apply --ids-out /path/ids.txt
  python sync_oracle_cards.py --bulk ... --exclude-sets msh,msc
"""

import argparse
import json
from decimal import Decimal

import ijson
import psycopg2
from psycopg2.extras import execute_values

from db_config import DB_CONFIG

# 本物カードでないレイアウト（import_cards.py と同一）。
EXCLUDE_LAYOUTS = {
    "art_series", "token", "emblem", "double_faced_token",
    "reversible_card", "planar", "scheme", "vanguard",
}
VINTAGE_OK = {"legal", "restricted"}

# 新規 insert で投入する列（本物カードを一通り埋める）。
INSERT_COLS = [
    "card_name", "type_line", "oracle_text", "mana_cost", "colors",
    "color_identity", "rarity", "layout", "set_code", "set_name",
    "collector_number", "cmc", "power", "toughness", "loyalty",
    "card_faces_json", "keywords", "legalities", "produced_mana",
    "edhrec_rank", "game_changer",
]
# 既存行で同期する列（検索/正確性に効くものだけ。代表print/時間変動/揮発jsonは除外）。
UPDATE_COLS = [
    "type_line", "oracle_text", "mana_cost", "colors", "color_identity",
    "rarity", "layout", "cmc", "power", "toughness", "loyalty",
    "keywords", "legalities", "produced_mana", "game_changer",
]
# embed_text に効く列（これが変わった行だけ reembed）。
EMBED_COLS = ["type_line", "oracle_text", "colors", "keywords", "rarity"]

# 比較正規化の型分類。
PROSE_COLS = {"type_line", "oracle_text"}                       # 空白圧縮 + NULL/'' 等価
TEXT_COLS = {"mana_cost", "rarity", "layout", "power", "toughness", "loyalty"}  # NULL/'' 等価
ARRAY_COLS = {"colors", "color_identity", "keywords", "produced_mana"}          # NULL/{} 等価 + ソート
# legalities(jsonb) / cmc(numeric) / game_changer(bool) は素の IS DISTINCT FROM。


def norm_expr(side: str, c: str) -> str:
    """比較用に正規化した SQL 式（side は 'm' or 't'）。"""
    col = side + "." + c
    if c in PROSE_COLS:
        return ("NULLIF(btrim(regexp_replace(COALESCE(" + col + ",''),'\\s+',' ','g')),'')")
    if c in TEXT_COLS:
        return "NULLIF(btrim(COALESCE(" + col + ",'')),'')"
    if c in ARRAY_COLS:
        return ("(SELECT array_agg(e ORDER BY e) FROM unnest("
                "COALESCE(" + col + ", ARRAY[]::text[])) e)")
    return col


def diff_pred(c: str) -> str:
    return norm_expr("m", c) + " IS DISTINCT FROM " + norm_expr("t", c)


def join_faces(card: dict, key: str) -> str:
    top = (card.get(key) or "").strip()
    if top:
        return top
    faces = card.get("card_faces") or []
    vals = [(f.get(key) or "").strip() for f in faces if (f.get(key) or "").strip()]
    return " // ".join(vals)


def is_eligible(card: dict, exclude_sets: set) -> bool:
    if card.get("layout") in EXCLUDE_LAYOUTS:
        return False
    if (card.get("legalities") or {}).get("vintage") not in VINTAGE_OK:
        return False
    if card.get("set") in exclude_sets:
        return False
    if "Token" in (join_faces(card, "type_line") or ""):
        return False
    return True


def card_score(card: dict) -> tuple:
    has_oracle = 1 if join_faces(card, "oracle_text") else 0
    real_layout = 0 if card.get("layout") in ("art_series",) else 1
    return (has_oracle, real_layout)


def to_row(card: dict) -> dict:
    """Scryfall オブジェクト → mtg_cards_v2 列の dict（DB の空表現規約に合わせる）。"""
    faces = card.get("card_faces")
    cmc = card.get("cmc")
    if isinstance(cmc, Decimal):
        cmc = float(cmc)
    return {
        "card_name": card.get("name"),
        "type_line": join_faces(card, "type_line") or None,
        "oracle_text": join_faces(card, "oracle_text") or None,
        "mana_cost": card.get("mana_cost") or "",          # DB は空を '' で持つ
        "colors": card.get("colors") or [],
        "color_identity": card.get("color_identity") or [],
        "rarity": card.get("rarity") or None,
        "layout": card.get("layout") or None,
        "set_code": card.get("set") or None,
        "set_name": card.get("set_name") or None,
        "collector_number": card.get("collector_number") or None,
        "cmc": cmc,
        "power": card.get("power"),
        "toughness": card.get("toughness"),
        "loyalty": card.get("loyalty"),
        "card_faces_json": json.dumps(faces, ensure_ascii=False) if faces else None,
        "keywords": card.get("keywords") or [],
        "legalities": json.dumps(card.get("legalities") or {}, ensure_ascii=False),
        "produced_mana": card.get("produced_mana") or None,  # 不在は NULL（DB 規約）
        "edhrec_rank": card.get("edhrec_rank"),
        "game_changer": bool(card.get("game_changer", False)),
    }


def load_canonical(bulk_path: str, exclude_sets: set) -> dict:
    chosen = {}
    total = eligible = 0
    with open(bulk_path, "r", encoding="utf-8") as f:
        for card in ijson.items(f, "item"):
            total += 1
            name = card.get("name")
            if not name or not is_eligible(card, exclude_sets):
                continue
            eligible += 1
            sc = card_score(card)
            cur = chosen.get(name)
            if cur is None or sc > cur[0]:
                chosen[name] = (sc, card)
    rows = {name: to_row(c) for name, (_, c) in chosen.items()}
    print(f"  oracle_cards 走査: {total} obj / eligible {eligible} / "
          f"名前単位（重複解決後） {len(rows)}")
    return rows


def sync(bulk_path: str, exclude_sets: set, apply: bool, ids_out: str):
    rows = load_canonical(bulk_path, exclude_sets)

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _oc (LIKE mtg_cards_v2 INCLUDING DEFAULTS) ON COMMIT DROP;")
        ins_cols = ", ".join(INSERT_COLS)
        payload = [tuple(r[c] for c in INSERT_COLS) for r in rows.values()]
        tmpl = "(" + ", ".join(
            "%s::jsonb" if c in ("card_faces_json", "legalities") else "%s"
            for c in INSERT_COLS
        ) + ")"
        execute_values(cur, f"INSERT INTO _oc ({ins_cols}) VALUES %s",
                       payload, template=tmpl, page_size=1000)

        any_pred = " OR ".join(diff_pred(c) for c in UPDATE_COLS)
        embed_pred = " OR ".join(diff_pred(c) for c in EMBED_COLS)

        cur.execute("SELECT count(*) FROM _oc")
        n_incoming = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM _oc t LEFT JOIN mtg_cards_v2 m "
                    "ON m.card_name=t.card_name WHERE m.card_name IS NULL")
        n_insert = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM _oc t JOIN mtg_cards_v2 m "
                    f"ON m.card_name=t.card_name WHERE {any_pred}")
        n_update = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM _oc t JOIN mtg_cards_v2 m "
                    f"ON m.card_name=t.card_name WHERE {embed_pred}")
        n_embed_change = cur.fetchone()[0]

        print("\n=== 同期サマリ（正規化比較・UPDATE 範囲は検索/正確性列）===")
        print(f"  受信(eligible)     : {n_incoming}")
        print(f"  新規 insert        : {n_insert}")
        print(f"  既存 update(real)  : {n_update}")
        print(f"  うち embed 変更    : {n_embed_change}  (= reembed 対象 + 新規)")

        print("\n  列別 差分行数（既存行・正規化後）:")
        for c in UPDATE_COLS:
            cur.execute(f"SELECT count(*) FROM _oc t JOIN mtg_cards_v2 m "
                        f"ON m.card_name=t.card_name WHERE {diff_pred(c)}")
            star = "  <embed>" if c in EMBED_COLS else ""
            print(f"    {c:16s}: {cur.fetchone()[0]}{star}")

        cur.execute("SELECT t.card_name, t.set_code, t.layout FROM _oc t "
                    "LEFT JOIN mtg_cards_v2 m ON m.card_name=t.card_name "
                    "WHERE m.card_name IS NULL ORDER BY t.card_name LIMIT 15")
        sample_new = cur.fetchall()
        if sample_new:
            print("\n  新規 sample (最大15):")
            for nm, sc, lay in sample_new:
                print(f"    + {nm}  [{sc}/{lay}]")

        if not apply:
            print("\n[dry-run] 書き込みませんでした。--apply で適用。")
            conn.rollback()
            conn.close()
            return

        cur.execute(f"SELECT m.id FROM _oc t JOIN mtg_cards_v2 m "
                    f"ON m.card_name=t.card_name WHERE {embed_pred}")
        embed_changed_ids = [r[0] for r in cur.fetchall()]

        set_clause = ", ".join(f"{c} = t.{c}" for c in UPDATE_COLS)
        cur.execute(f"UPDATE mtg_cards_v2 m SET {set_clause} FROM _oc t "
                    f"WHERE m.card_name = t.card_name AND ({any_pred})")
        updated = cur.rowcount

        cur.execute(f"""
            INSERT INTO mtg_cards_v2 ({ins_cols})
            SELECT {ins_cols} FROM _oc t
            WHERE NOT EXISTS (SELECT 1 FROM mtg_cards_v2 m WHERE m.card_name=t.card_name)
            RETURNING id
        """)
        inserted_ids = [r[0] for r in cur.fetchall()]
        conn.commit()
        print(f"\n[apply] insert={len(inserted_ids)} / update={updated}")

        out_ids = sorted(set(embed_changed_ids) | set(inserted_ids))
        if ids_out:
            with open(ids_out, "w", encoding="utf-8") as f:
                f.write("\n".join(str(i) for i in out_ids) + ("\n" if out_ids else ""))
            print(f"[apply] embed 変更 id {len(out_ids)} 件 → {ids_out}")
            print("  次工程: add_face_cmcs.py → "
                  "rebuild_embed_text.py --update_text/--reembed --card_ids_file 上記")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", required=True, help="oracle_cards.json のパス")
    ap.add_argument("--apply", action="store_true", help="実際に書き込む（既定はdry-run）")
    ap.add_argument("--ids-out", default=None, help="embed 変更 id の出力先")
    ap.add_argument("--exclude-sets", default="", help="除外 set code カンマ区切り")
    args = ap.parse_args()
    excl = {s.strip() for s in args.exclude_sets.split(",") if s.strip()}
    sync(args.bulk, excl, args.apply, args.ids_out)
