"""
add_face_types.py — 型の否定ゲート用の導出列（2026-07-13）: 「唱えられる面の型」を
mtg_cards_v2 に追加。

足す列（embed_text に入れない構造化メタ＝reembed 不要）:
  - face_types text[] : 手札から直接唱えられる各面の type_line の集合。
                        通常カード = [type_line]（1要素・後方互換）。
                        多面カード = mana_cost 非空の面の type_line（face_cmcs と同一規則）。
                        全面 mana_cost 空（変身土地等）= 表面の type_line
                        （表面基準フォールバック・採点規約 R8補足b と同じ思想）。

背景（2026-07-13 本人の言語化）: 「非クリーチャーカード」の正否は面の型ではなく
「その面を手札から直接唱えられるか」で決まる。Valki//Tibalt（modal_dfc）は
Tibalt 面を直接唱えられる＝コスト7の非クリーチャーとして適格。
鏡割りの寓話（transform）の裏面クリーチャーは直接唱えられない＝表面 Saga が本質。
face_cmcs が cmc 側で既に実装していた「mana_cost 非空の面＝唱えられる面」の規則を
型側にそのまま写す（cmc と型で判定基準がズレないことがこのスクリプトの本体）。

冪等（ADD COLUMN IF NOT EXISTS / UPDATE 上書き）。card_faces_json + type_line は
既に mtg_cards_v2 にあるので外部ソース不要。

使い方:
    /mnt/new_hdd/my_rag_env/bin/python add_face_types.py
"""
import json
import psycopg2
from psycopg2.extras import execute_values
from db_config import get_db_config

ALTER = """
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS face_types text[];
"""


def _faces(card_faces_json):
    """jsonb 列を Python list に正規化（psycopg2 が str を返す場合に備える）。"""
    if card_faces_json is None:
        return None
    if isinstance(card_faces_json, str):
        try:
            return json.loads(card_faces_json)
        except Exception:
            return None
    return card_faces_json


def compute_types(type_line, card_faces_json):
    """1 カードの face_types（唱えられる面の type_line 集合）を返す。"""
    faces = _faces(card_faces_json)
    if faces and len(faces) >= 2:
        ft = [(f.get("type_line") or "").strip() for f in faces
              if (f.get("mana_cost") or "").strip()]
        ft = [t for t in ft if t]
        if not ft:                       # 全面 mana_cost 空（変身土地等）→ 表面基準
            first = (faces[0].get("type_line") or "").strip()
            ft = [first] if first else [(type_line or "").strip()]
    else:
        ft = [(type_line or "").strip()]
    return ft


def main():
    conn = psycopg2.connect(**get_db_config())
    cur = conn.cursor()
    cur.execute(ALTER)
    conn.commit()
    print("カラム追加（IF NOT EXISTS）完了")

    cur.execute("SELECT id, type_line, card_faces_json FROM mtg_cards_v2")
    rows = cur.fetchall()
    print(f"対象: {len(rows)} 件を計算中…")

    out = [(cid, compute_types(tl, cfj)) for cid, tl, cfj in rows]

    cur.execute("""
        CREATE TEMP TABLE _ft(id int, face_types text[]) ON COMMIT DROP;
    """)
    execute_values(
        cur,
        "INSERT INTO _ft(id, face_types) VALUES %s",
        out, page_size=2000,
    )
    cur.execute("""
        UPDATE mtg_cards_v2 c
        SET face_types = s.face_types
        FROM _ft s WHERE c.id = s.id;
    """)
    print(f"mtg_cards_v2 を更新: {cur.rowcount} 行")
    conn.commit()

    # 検証
    cur.execute("SELECT count(*) FROM mtg_cards_v2 WHERE face_types IS NULL"
                " OR face_types = '{}'")
    print(f"  NULL/空: {cur.fetchone()[0]}（0 が正）")
    for name in ("Valki, God of Lies // Tibalt, Cosmic Impostor",   # modal_dfc → 2面
                 "Fable of the Mirror-Breaker // Reflection of Kiki-Jiki",  # transform → 表面のみ
                 "Atraxa, Grand Unifier",                            # 単面
                 "Agadeem's Awakening // Agadeem, the Undergrowth",  # MDFC 裏が土地 → 呪文面のみ
                 "Westvale Abbey // Ormendahl, Profane Prince",      # 変身土地 → 表面基準フォールバック
                 "Consign // Oblivion"):                             # split → 2面
        cur.execute("SELECT card_name, layout, face_types "
                    "FROM mtg_cards_v2 WHERE card_name=%s", (name,))
        print("  ", cur.fetchone())

    conn.close()


if __name__ == "__main__":
    main()
