"""
add_face_cmcs.py — 構造化フィルタ v2（T4）: 「撃てるコスト」を表す列を mtg_cards_v2 に追加。

足す列（embed_text に入れない構造化メタ＝reembed 不要）:
  - face_cmcs int[]  : 各面の実コストから計算した「撃てる cmc の集合」。
                       通常カード = [cmc]（1要素・既存挙動と後方互換）。
                       split/多面 = 各面 mana_cost から算出した集合（例 {1}{U}//{4}{B} → [2,5]）。
                       唱えない面（土地面・変身先など mana_cost 空）は除外。
  - has_x boolean    : mana_cost に {X} を含むか（top-level または各面）。X呪文の識別用。
                       フィルタで自動除外はしない（grade を学習信号に reranker/対話で扱う方針）。

背景: mana_value(ルール上の数値) ≠ castable cost(実際に撃てるコスト)。単一 cmc では
split（合計 cmc）や X呪文（X=0 で cmc 計算）を「撃てる現実」で捉えられない。
card_faces_json の各面に Scryfall は cmc を持たない（mana_cost のみ）ため、mana_cost を
自前パースして CMC を計算する（parse_cmc）。

冪等（ADD COLUMN IF NOT EXISTS / UPDATE 上書き）。card_faces_json + mana_cost は既に
mtg_cards_v2 にあるので外部ソース不要。

使い方:
    /mnt/new_hdd/my_rag_env/bin/python add_face_cmcs.py
"""
import re
import json
import psycopg2
from psycopg2.extras import execute_values
from db_config import get_db_config

TOKEN_RE = re.compile(r'\{([^}]+)\}')

ALTER = """
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS face_cmcs int[];
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS has_x boolean;
"""


def token_value(tok: str) -> int:
    """マナ記号1個の CMC 寄与（Scryfall の mana_value 規則に準拠）。"""
    if tok.isdigit():
        return int(tok)            # {n} → n
    if tok in ("X", "Y", "Z"):
        return 0                   # 変数マナは CMC 計算で 0
    if "/" in tok:
        nums = [int(p) for p in tok.split("/") if p.isdigit()]
        if nums:
            return max(nums)       # ハイブリッド数字 {2/W} → 2
        return 1                   # {W/U}（色ハイブリッド）/ {W/P}（ファイレクシア）→ 1
    return 1                       # 単色・無色・氷雪など単一記号 → 1


def parse_cmc(mana_cost: str | None) -> int:
    """mana_cost 文字列（例 '{1}{U}'）から CMC を計算。空/None は 0。"""
    if not mana_cost:
        return 0
    return sum(token_value(t) for t in TOKEN_RE.findall(mana_cost))


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


def compute(mana_cost, cmc, card_faces_json):
    """1 カードの (face_cmcs, has_x) を返す。"""
    faces = _faces(card_faces_json)

    # has_x: top-level または各面の mana_cost に {X}
    has_x = "{X}" in (mana_cost or "")
    if faces:
        has_x = has_x or any("{X}" in (f.get("mana_cost") or "") for f in faces)

    # face_cmcs
    if faces and len(faces) >= 2:
        fc = [parse_cmc(f.get("mana_cost")) for f in faces
              if (f.get("mana_cost") or "").strip()]
        if not fc:                       # 全面マナコスト空（稀）→ top-level cmc にフォールバック
            fc = [int(round(cmc or 0))]
    else:
        fc = [int(round(cmc or 0))]      # 単面: 既存挙動と後方互換

    return fc, has_x


def main():
    conn = psycopg2.connect(**get_db_config())
    cur = conn.cursor()
    cur.execute(ALTER)
    conn.commit()
    print("カラム追加（IF NOT EXISTS）完了")

    cur.execute("SELECT id, mana_cost, cmc, card_faces_json FROM mtg_cards_v2")
    rows = cur.fetchall()
    print(f"対象: {len(rows)} 件を計算中…")

    out = []
    for cid, mana_cost, cmc, cfj in rows:
        fc, has_x = compute(mana_cost, cmc, cfj)
        out.append((cid, fc, has_x))

    cur.execute("""
        CREATE TEMP TABLE _fc(id int, face_cmcs int[], has_x bool) ON COMMIT DROP;
    """)
    execute_values(
        cur,
        "INSERT INTO _fc(id, face_cmcs, has_x) VALUES %s",
        out, page_size=2000,
    )
    cur.execute("""
        UPDATE mtg_cards_v2 c
        SET face_cmcs = s.face_cmcs, has_x = s.has_x
        FROM _fc s WHERE c.id = s.id;
    """)
    print(f"mtg_cards_v2 を更新: {cur.rowcount} 行")
    conn.commit()

    # 検証
    cur.execute("SELECT count(*) FROM mtg_cards_v2 WHERE face_cmcs IS NOT NULL")
    print(f"  face_cmcs 保有: {cur.fetchone()[0]}")
    cur.execute("SELECT count(*) FROM mtg_cards_v2 WHERE has_x IS TRUE")
    print(f"  has_x=true: {cur.fetchone()[0]}")
    for name in ("Lightning Bolt", "Consign // Oblivion",
                 "Rimrock Knight // Boulder Rush", "Fireball"):
        cur.execute("SELECT card_name, mana_cost, cmc, face_cmcs, has_x "
                    "FROM mtg_cards_v2 WHERE card_name=%s", (name,))
        print("  ", cur.fetchone())

    conn.close()


if __name__ == "__main__":
    main()
