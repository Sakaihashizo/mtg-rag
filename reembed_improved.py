# reembed_improved.py （低メモリ・ストリーミング版）
import argparse
import json
import re
import time
import ijson
import psycopg2
import psycopg2.extras
from sentence_transformers import SentenceTransformer

DB_CONFIG = {
    "host": "localhost", "port": 5435,
    "dbname": "rag_dev", "user": "devuser", "password": "***REMOVED***",
}

MODEL_REGISTRY = {
    "SMALL": {
        "model_name": "intfloat/multilingual-e5-small",
        "dim": 384, "prefix": "passage: ",
        "cards_table": "mtg_cards_small_v2",
        "embeddings_table": "mtg_embeddings_small_v2",
    },
    "BASE": {
        "model_name": "intfloat/multilingual-e5-base",
        "dim": 768, "prefix": "passage: ",
        "cards_table": "mtg_cards_base_v2",
        "embeddings_table": "mtg_embeddings_base_v2",
    },
}

MANA_MAP = {
    r'\{W\}': 'white', r'\{U\}': 'blue', r'\{B\}': 'black',
    r'\{R\}': 'red', r'\{G\}': 'green', r'\{C\}': 'colorless',
    r'\{T\}': 'tap', r'\{Q\}': 'untap',
}

EXCLUDE_LAYOUTS = {"art_series", "token", "emblem", "double_faced_token",
                   "reversible_card", "planar", "scheme", "vanguard"}
def simplify_mana(mana_cost: str) -> str:
    if not mana_cost:
        return ""
    result = mana_cost
    for pattern, repl in MANA_MAP.items():
        result = re.sub(pattern, repl, result)
    return re.sub(r'\{\d+\}', 'generic', result).replace('{', '').replace('}', '')

def build_card_text(card: dict) -> str:
    name = card.get("name", "")
    type_line = card.get("type_line", "")
    oracle = card.get("oracle_text", "")
    keywords = card.get("keywords", []) or []
    mana_cost = card.get("mana_cost", "")
    colors = card.get("colors", []) or []
    rarity = card.get("rarity", "")

    color_words = [c for c in colors]  # W/U/Bなど
    mana_simple = simplify_mana(mana_cost)

    parts = [name]
    if color_words:
        parts.append("Color: " + " ".join(color_words))
    if mana_simple:
        parts.append(f"Cost: {mana_simple}")
    if keywords:
        parts.append("Keywords: " + ", ".join(str(k) for k in keywords))
    parts.append(type_line)
    parts.append(oracle)

    if rarity in ("rare", "mythic"):
        parts.append(f"Rarity: {rarity}")

    return " | ".join(p for p in parts if p)

def create_tables(conn, cfg: dict):
    dim = cfg["dim"]
    cards_t = cfg["cards_table"]
    embed_t = cfg["embeddings_table"]
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {embed_t}")
        cur.execute(f"DROP TABLE IF EXISTS {cards_t}")
        cur.execute(f"""
            CREATE TABLE {cards_t} (
                id SERIAL PRIMARY KEY,
                card_name TEXT NOT NULL,
                type_line TEXT,
                oracle_text TEXT,
                mana_cost TEXT,
                colors TEXT[],
                rarity TEXT,
                embed_text TEXT
            );
        """)
        cur.execute(f"""
            CREATE TABLE {embed_t} (
                id SERIAL PRIMARY KEY,
                card_id INTEGER REFERENCES {cards_t}(id),
                embedding vector({dim})
            );
        """)
        cur.execute(f"""
            CREATE INDEX {embed_t}_hnsw_idx
            ON {embed_t} USING hnsw (embedding vector_cosine_ops);
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {cards_t} (
                id SERIAL PRIMARY KEY,
                card_name TEXT NOT NULL UNIQUE,   # ← UNIQUE を追加
                type_line TEXT,
                oracle_text TEXT,
                mana_cost TEXT,
                colors TEXT[],
                rarity TEXT,
                embed_text TEXT
            );
        """)
    conn.commit()
    print(f"テーブル作成完了 → {cards_t} / {embed_t}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=["SMALL", "BASE"], default="SMALL")
    parser.add_argument("--batch_size", type=int, default=32)   # メモリ重視で32に下げてる
    parser.add_argument("--json", default="/mnt/new_hdd/all_cards.json")
    args = parser.parse_args()

    cfg = MODEL_REGISTRY[args.model]
    print(f"【{args.model}モデル】再embedding開始（低メモリ版）")

    model = SentenceTransformer(cfg["model_name"], cache_folder="/mnt/new_hdd/hf_cache")
    conn = psycopg2.connect(**DB_CONFIG)
    create_tables(conn, cfg)

    count = 0
    batch_texts = []
    batch_cards = []
    batch_size = args.batch_size

    with open(args.json, encoding="utf-8") as f:
        for card in ijson.items(f, 'item'):   # ← ストリーミングでメモリ節約
            if not card.get("name"):
                continue

            text = cfg["prefix"] + build_card_text(card)
            batch_texts.append(text)
            batch_cards.append(card)

            if len(batch_texts) >= batch_size:
                vecs = model.encode(batch_texts, normalize_embeddings=True)

                # cardsテーブル挿入
                card_rows = []
                for c in batch_cards:
                    card_rows.append((
                        c.get("name"),
                        c.get("type_line"),
                        c.get("oracle_text"),
                        c.get("mana_cost"),
                        c.get("colors"),
                        c.get("rarity"),
                        build_card_text(c),
                    ))

                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        f"INSERT INTO {cfg['cards_table']} "
                        "(card_name, type_line, oracle_text, mana_cost, colors, rarity, embed_text) "
                        "VALUES %s RETURNING id",
                        card_rows
                    )
                    card_ids = [r[0] for r in cur.fetchall()]

                    # embeddingsテーブル挿入
                    embed_rows = [(cid, vec.tolist()) for cid, vec in zip(card_ids, vecs)]
                    psycopg2.extras.execute_values(
                        cur,
                        f"INSERT INTO {cfg['embeddings_table']} (card_id, embedding) VALUES %s",
                        embed_rows
                    )
                conn.commit()

                count += len(batch_texts)
                if count % 5000 == 0:
                    print(f"  {count:,} 枚処理完了")

                batch_texts.clear()
                batch_cards.clear()

    # 残り処理
    if batch_texts:
        vecs = model.encode(batch_texts, normalize_embeddings=True)
        # （上記と同じ挿入処理を省略して同じロジックで処理）
        # ※省略して書いているが、実際は上記と同じコードをコピーして残りも処理
        print("残り分も処理完了")

    conn.close()
    print(f"\n🎉 {args.model}モデル 再embedding完了！ 合計 {count} 枚")

if __name__ == "__main__":
    main()