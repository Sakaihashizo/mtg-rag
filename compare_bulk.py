"""
compare_bulk.py — mtg_cards_v2 の Scryfall 由来フィールドを最新バルクと全カード照合し、
DB がどれだけ古い/ズレているかをフィールド別に概観する（非破壊・データ鮮度監査）。

比較対象（Scryfall 生フィールド）: type_line / oracle_text / mana_cost / cmc / colors /
keywords / power / toughness / loyalty / rarity / layout。
比較しない: japanese_*（別ソース）/ embed_text（生成）/ tournament_score・deck系・
produced_mana 等の enrich（別取り込み）。
両面カード（card_faces あり）は top-level が空/結合差で誤差分になるため、
type_line/oracle_text/mana_cost/power/toughness/loyalty の比較から除外してカウント別計上。

使い方:
    python compare_bulk.py            # フィールド別差分件数の概観
    python compare_bulk.py <field>    # そのフィールドの差分サンプル（最大20件）
"""
import sys
import json
from collections import Counter
import psycopg2
from db_config import get_db_config

BULK = "/mnt/new_hdd/oracle_cards.json"
SHOW_FIELD = sys.argv[1] if len(sys.argv) > 1 else None

SIMPLE = ["rarity", "layout"]                 # 全カード比較（単純文字列）
NUM = ["cmc"]                                  # numeric
ARR = ["colors", "keywords"]                   # 配列
FACE_SENSITIVE = ["type_line", "oracle_text", "mana_cost", "power", "toughness", "loyalty"]  # 単面のみ


def ns(x):
    return (x or "").strip() if isinstance(x, str) or x is None else str(x)


def na(x):
    return sorted(x) if x else []


def nn(x):
    return None if x is None else float(x)


def main():
    conn = psycopg2.connect(**get_db_config())
    cur = conn.cursor()

    bulk = {}
    with open(BULK, encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            try:
                c = json.loads(line)
            except Exception:
                continue
            n = c.get("name")
            if n and n not in bulk:
                bulk[n] = c
    print(f"バルク {len(bulk)} 件")

    cols = ["card_name", "type_line", "oracle_text", "mana_cost", "cmc", "colors",
            "keywords", "power", "toughness", "loyalty", "rarity", "layout"]
    cur.execute(f"SELECT {', '.join(cols)}, (card_faces_json IS NOT NULL) AS hf FROM mtg_cards_v2")
    diff = Counter()
    samples = {}
    n_db = n_nobulk = 0
    for row in cur.fetchall():
        n_db += 1
        rec = dict(zip(cols, row[:-1]))
        hf = row[-1]
        name = rec["card_name"]
        b = bulk.get(name)
        if not b:
            n_nobulk += 1
            continue
        checks = []
        for f in SIMPLE:
            checks.append((f, ns(rec[f]) != ns(b.get(f))))
        for f in NUM:
            checks.append((f, nn(rec[f]) != nn(b.get(f))))
        for f in ARR:
            checks.append((f, na(rec[f]) != na(b.get(f))))
        if not hf:
            for f in FACE_SENSITIVE:
                checks.append((f, ns(rec[f]) != ns(b.get(f))))
        else:
            diff["(両面: テキスト系比較スキップ)"] += 1
        for f, changed in checks:
            if changed:
                diff[f] += 1
                if SHOW_FIELD == f and len(samples.setdefault(f, [])) < 20:
                    samples[f].append((name, rec[f], b.get(f)))

    print(f"DB {n_db} 件 / バルク未収載 {n_nobulk} 件")
    print("=== フィールド別 差分件数 ===")
    for k, v in diff.most_common():
        print(f"  {k}: {v}")

    if SHOW_FIELD and SHOW_FIELD in samples:
        print(f"\n=== {SHOW_FIELD} 差分サンプル (DB | バルク) ===")
        for name, db_v, b_v in samples[SHOW_FIELD]:
            print(f"  [{name}]\n    DB  : {db_v!r}\n    bulk: {b_v!r}")

    conn.close()


if __name__ == "__main__":
    main()
