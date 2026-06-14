"""
import_cards.py — 修正版 v3
============================
修正点:
  1. embed_only モード追加
     → cards テーブルに既にデータがある場合、embedding だけを追加する
     → ベクトル計算を必要な分だけ実行（重複カードはスキップ）
  2. SMALL_V2 取り込み済みの mtg_cards_v2 に対して
     BASE_V2 の embedding だけ追加する用途に対応

使い方:
  # 新規取り込み（cards + embeddings を両方作る）
  python import_cards.py --model SMALL_V2

  # embedding だけ追加（cards は既存のものを使う）
  python import_cards.py --model BASE_V2 --embed_only

  # 既存テーブルの oracle_text だけ修正（再embeddingなし）
  python import_cards.py --repair --model SMALL_V2
"""

import argparse
import json
import re
import ijson
import psycopg2
from tqdm import tqdm
from sentence_transformers import SentenceTransformer

EXCLUDE_LAYOUTS = {
    "art_series", "token", "emblem", "double_faced_token",
    "reversible_card", "planar", "scheme", "vanguard",
}

MANA_MAP = {
    r'\{W\}': 'white', r'\{U\}': 'blue', r'\{B\}': 'black',
    r'\{R\}': 'red',   r'\{G\}': 'green', r'\{C\}': 'colorless',
    r'\{T\}': 'tap',   r'\{Q\}': 'untap',
}
COLOR_NAMES = {
    'W': 'white', 'U': 'blue', 'B': 'black', 'R': 'red', 'G': 'green',
}

MODEL_CONFIGS = {
    "SMALL_V2": {
        "model_name": "intfloat/multilingual-e5-small",
        "prefix": "passage: ",
        "table_cards": "mtg_cards_v2",
        "table_embed": "mtg_embeddings_small_v2",
    },
    "BASE_V2": {
        "model_name": "intfloat/multilingual-e5-base",
        "prefix": "passage: ",
        "table_cards": "mtg_cards_v2",
        "table_embed": "mtg_embeddings_base_v2",
    },
}

from db_config import DB_CONFIG

JSON_FILE  = "/mnt/new_hdd/all_cards.json"
HF_CACHE   = "/mnt/new_hdd/hf_cache"
BATCH_SIZE = 64


# ─── テキスト抽出 ─────────────────────────────────────────────

def extract_oracle_text(card: dict) -> str:
    top = card.get("oracle_text", "").strip()
    if top:
        return top
    faces = card.get("card_faces") or card.get("cardFaces") or []
    texts = [f.get("oracle_text", "").strip() for f in faces
             if f.get("oracle_text", "").strip()]
    return " // ".join(texts) if texts else ""


def extract_type_line(card: dict) -> str:
    top = card.get("type_line", "").strip()
    if top:
        return top
    faces = card.get("card_faces") or card.get("cardFaces") or []
    types = [f.get("type_line", "").strip() for f in faces
             if f.get("type_line", "").strip()]
    return " // ".join(types) if types else ""


def clean_oracle_text(text: str) -> str:
    for pattern, replacement in MANA_MAP.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r'\{\d+\}', 'N', text)
    return text.replace('\n', ' ').strip()


def build_embed_text(card: dict, prefix: str) -> str:
    name      = card.get("name", "")
    type_line = extract_type_line(card)
    oracle    = clean_oracle_text(extract_oracle_text(card))
    colors    = card.get("colors") or card.get("color_identity") or []
    keywords  = card.get("keywords") or []
    rarity    = card.get("rarity", "") or ""

    color_words = [COLOR_NAMES.get(c, c) for c in colors if c in COLOR_NAMES]

    IMPORTANT_TYPES = ["Instant", "Sorcery", "Creature", "Enchantment",
                       "Artifact", "Planeswalker", "Land", "Legendary"]
    main_types = [t for t in IMPORTANT_TYPES if t in type_line]

    parts = [name]
    if main_types:
        parts.append("Type: " + " ".join(main_types))
    if color_words:
        parts.append("Color: " + " ".join(color_words))
    if keywords:
        kw = keywords if isinstance(keywords, list) else [keywords]
        parts.append("Keywords: " + ", ".join(str(k) for k in kw))
    if type_line:
        parts.append(type_line)
    if oracle:
        parts.append(oracle)
    if rarity in ("rare", "mythic"):
        parts.append(f"Rarity: {rarity}")

    return prefix + " | ".join(p for p in parts if p)


# ─── テーブル作成 ─────────────────────────────────────────────

def create_v2_tables(conn, cfg: dict):
    cards_t = cfg["table_cards"]
    embed_t = cfg["table_embed"]
    dim_map = {
        "intfloat/multilingual-e5-small": 384,
        "intfloat/multilingual-e5-base":  768,
        "intfloat/multilingual-e5-large-instruct": 1024,
    }
    dim = dim_map[cfg["model_name"]]
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {cards_t} (
                id          SERIAL PRIMARY KEY,
                card_name   TEXT NOT NULL UNIQUE,
                type_line   TEXT,
                oracle_text TEXT,
                mana_cost   TEXT,
                colors      TEXT[],
                rarity      TEXT,
                layout      TEXT,
                embed_text  TEXT
            );
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {embed_t} (
                id        SERIAL PRIMARY KEY,
                card_id   INTEGER REFERENCES {cards_t}(id) ON DELETE CASCADE,
                embedding vector({dim}),
                UNIQUE (card_id)
            );
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {embed_t}_hnsw_idx
            ON {embed_t} USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """)
    conn.commit()
    print(f"テーブル準備完了: {cards_t}, {embed_t} (dim={dim})")


# ─── embedding のみモード ─────────────────────────────────────

def embed_only(cfg: dict):
    """
    mtg_cards_v2 に既にあるカードデータを使い、
    embedding だけを別テーブルに追加する。

    SMALL_V2 取り込み済みの状態で BASE_V2 を追加する場合に使う。
    ベクトル計算はユニークカード分のみ実行されるため高速。
    """
    table_cards = cfg["table_cards"]
    table_embed = cfg["table_embed"]
    prefix      = cfg["prefix"]

    conn = psycopg2.connect(**DB_CONFIG)
    create_v2_tables(conn, cfg)

    # embedding 済みの card_id を除いて取得
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT c.id, c.embed_text
            FROM {table_cards} c
            LEFT JOIN {table_embed} e ON e.card_id = c.id
            WHERE e.card_id IS NULL
            ORDER BY c.id;
        """)
        rows = cur.fetchall()

    total = len(rows)
    print(f"embedding 対象: {total} 件")
    if total == 0:
        print("追加対象なし。既に全件 embedding 済みです。")
        conn.close()
        return

    # embed_text は SMALL_V2 取り込み時のもの（prefix が異なる）
    # BASE_V2 用に prefix を付け直す
    print(f"モデルロード中: {cfg['model_name']}")
    model = SentenceTransformer(cfg["model_name"], cache_folder=HF_CACHE)

    done = 0
    for i in tqdm(range(0, total, BATCH_SIZE), desc=table_embed):
        batch = rows[i:i + BATCH_SIZE]
        ids   = [r[0] for r in batch]

        # embed_text から passage: プレフィックスを除去して付け直す
        # （SMALL と BASE で prefix が同じ "passage: " なので実質そのまま）
        raw_texts = [r[1] or "" for r in batch]
        # prefix を付け直す（異なるモデルへの対応）
        texts = []
        for t in raw_texts:
            # 既存の passage: / query: プレフィックスを除去して付け直す
            for p in ("passage: ", "query: ", "Instruct:"):
                if t.startswith(p):
                    t = t[len(p):]
                    break
            texts.append(prefix + t.strip())

        embeddings = model.encode(
            texts, batch_size=BATCH_SIZE,
            normalize_embeddings=True, convert_to_tensor=False,
        )
        with conn.cursor() as cur:
            for card_id, emb in zip(ids, embeddings):
                cur.execute(f"""
                    INSERT INTO {table_embed} (card_id, embedding)
                    VALUES (%s, %s::vector)
                    ON CONFLICT (card_id) DO NOTHING;
                """, (card_id, emb.tolist()))
        conn.commit()
        done += len(batch)

        if done % 5000 == 0:
            print(f"  → {done}/{total} 件完了")

    conn.close()
    print(f"\n完了: {done} 件の embedding を {table_embed} に格納しました")


# ─── 新規取り込みモード ───────────────────────────────────────

def import_cards(cfg: dict):
    """cards + embeddings を両方新規作成する（SMALL_V2 用）"""
    table_cards = cfg["table_cards"]
    table_embed = cfg["table_embed"]
    prefix      = cfg["prefix"]

    print(f"モデルロード中: {cfg['model_name']}")
    model = SentenceTransformer(cfg["model_name"], cache_folder=HF_CACHE)

    conn = psycopg2.connect(**DB_CONFIG)
    create_v2_tables(conn, cfg)

    count          = 0
    skipped_layout = 0
    skipped_dup    = 0
    batch_texts    = []
    batch_ids      = []

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        for card in tqdm(ijson.items(f, "item"), desc=table_cards, mininterval=20):
            card_name = card.get("name", "").strip()
            if not card_name:
                continue

            layout = card.get("layout", "")
            if layout in EXCLUDE_LAYOUTS:
                skipped_layout += 1
                continue

            oracle_text = extract_oracle_text(card)
            type_line   = extract_type_line(card)
            mana_cost   = card.get("mana_cost", "") or ""
            colors      = card.get("colors") or []
            rarity      = card.get("rarity", "") or ""
            embed_text  = build_embed_text(card, prefix)

            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {table_cards}
                        (card_name, type_line, oracle_text, mana_cost,
                         colors, rarity, layout, embed_text)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (card_name) DO NOTHING
                    RETURNING id;
                """, (card_name, type_line, oracle_text, mana_cost,
                      colors, rarity, layout, embed_text))
                result = cur.fetchone()

            if result is None:
                # 重複カード名 → ベクトル計算不要
                skipped_dup += 1
                count += 1
                continue

            batch_texts.append(embed_text)
            batch_ids.append(result[0])
            count += 1

            if len(batch_texts) >= BATCH_SIZE:
                _flush_batch(conn, model, batch_texts, batch_ids, table_embed)
                conn.commit()
                batch_texts.clear()
                batch_ids.clear()

            if count % 5000 == 0:
                print(f"  → {count:,} 件処理 "
                      f"(layout除外: {skipped_layout} / 重複: {skipped_dup})")

    if batch_texts:
        _flush_batch(conn, model, batch_texts, batch_ids, table_embed)
        conn.commit()

    conn.close()
    print(f"\n完了: {count:,} 件処理")
    print(f"  layout 除外: {skipped_layout} 件")
    print(f"  重複スキップ: {skipped_dup} 件")
    print(f"  実際に格納: {count - skipped_layout - skipped_dup} 件")


def _flush_batch(conn, model, texts, ids, table_embed):
    embeddings = model.encode(
        texts, batch_size=BATCH_SIZE,
        normalize_embeddings=True, convert_to_tensor=False,
    )
    with conn.cursor() as cur:
        for card_id, emb in zip(ids, embeddings):
            cur.execute(f"""
                INSERT INTO {table_embed} (card_id, embedding)
                VALUES (%s, %s::vector)
                ON CONFLICT (card_id) DO NOTHING;
            """, (card_id, emb.tolist()))


# ─── repair モード ────────────────────────────────────────────

def repair_oracle_text(cfg: dict):
    """既存テーブルの oracle_text が空のレコードだけ修正（再embeddingなし）"""
    table_cards = cfg["table_cards"]
    print(f"[repair] {table_cards} の空 oracle_text を修正します...")
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT id, card_name FROM {table_cards}
            WHERE oracle_text IS NULL OR oracle_text = ''
        """)
        empty_rows = {row[1]: row[0] for row in cur.fetchall()}
    print(f"  対象: {len(empty_rows)} 件")
    if not empty_rows:
        print("  修正対象なし。終了します。")
        conn.close()
        return
    updated = 0
    with open(JSON_FILE, "r", encoding="utf-8") as f:
        for card in tqdm(ijson.items(f, "item"), desc="repair", mininterval=10):
            name = card.get("name", "")
            if name not in empty_rows:
                continue
            oracle_text = extract_oracle_text(card)
            type_line   = extract_type_line(card)
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE {table_cards}
                    SET oracle_text = %s, type_line = %s
                    WHERE id = %s
                """, (oracle_text, type_line, empty_rows[name]))
            updated += 1
            if updated % 500 == 0:
                conn.commit()
                print(f"  {updated}/{len(empty_rows)} 更新済み")
    conn.commit()
    conn.close()
    print(f"[repair] 完了: {updated} 件を更新しました")


# ─── エントリーポイント ───────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        choices=["SMALL_V2", "BASE_V2"],
        default="SMALL_V2",
    )
    parser.add_argument(
        "--repair", action="store_true",
        help="oracle_text が空のレコードだけ修正（再embeddingなし）",
    )
    parser.add_argument(
        "--embed_only", action="store_true",
        help="cards テーブルは既存のものを使い embedding だけ追加する",
    )
    args = parser.parse_args()
    cfg = MODEL_CONFIGS[args.model]

    if args.repair:
        repair_oracle_text(cfg)
    elif args.embed_only:
        embed_only(cfg)
    else:
        import_cards(cfg)
