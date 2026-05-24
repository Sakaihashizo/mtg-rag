"""
rebuild_embed_text.py — embed_text を日英混合で再構築して再 embedding
=====================================================================
enrich_cards.py で追加されたフィールドを活用し、
embed_text を以下の構造で再構築する:

  passage: カード名 | Type: タイプ | Color: 色 | Keywords: キーワード |
           タイプ行 | 英語テキスト | 日本語名 | 日本語テキスト |
           P/T or Loyalty | Rarity

日本語テキストを含めることで日本語クエリの精度が大幅に向上する。

使い方:
  # Step1: embed_text カラムを更新（embedding は変えない）
  python rebuild_embed_text.py --update_text

  # Step2: embedding を再計算（SMALL_V2）
  python rebuild_embed_text.py --reembed --model SMALL_V2

  # Step3: embedding を再計算（BASE_V2）
  python rebuild_embed_text.py --reembed --model BASE_V2

  # 確認
  python rebuild_embed_text.py --status
"""

import argparse
import re
import psycopg2
import psycopg2.extras
from tqdm import tqdm
from sentence_transformers import SentenceTransformer

DB_CONFIG = {
    "dbname": "rag_dev",
    "user": "devuser",
    "password": "***REMOVED***",
    "host": "localhost",
    "port": 5435,
}

HF_CACHE   = "/mnt/new_hdd/hf_cache"
BATCH_SIZE = 64

MODEL_CONFIGS = {
    "SMALL_V2": {
        "model_name": "intfloat/multilingual-e5-small",
        "prefix": "passage: ",
        "table_embed": "mtg_embeddings_small_v2",
    },
    "BASE_V2": {
        "model_name": "intfloat/multilingual-e5-base",
        "prefix": "passage: ",
        "table_embed": "mtg_embeddings_base_v2",
    },
}

MANA_MAP = {
    r'\{W\}': 'white', r'\{U\}': 'blue', r'\{B\}': 'black',
    r'\{R\}': 'red',   r'\{G\}': 'green', r'\{C\}': 'colorless',
    r'\{T\}': 'tap',   r'\{Q\}': 'untap',
}
COLOR_NAMES = {
    'W': 'white', 'U': 'blue', 'B': 'black', 'R': 'red', 'G': 'green',
}
IMPORTANT_TYPES = [
    "Legendary", "Instant", "Sorcery", "Creature", "Enchantment",
    "Artifact", "Planeswalker", "Land",
]


def clean_text(text: str) -> str:
    for pattern, replacement in MANA_MAP.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r'\{\d+\}', 'N', text)
    return text.replace('\n', ' ').strip()


def build_embed_text(row: dict, prefix: str, cooc_partners: list[str] = []) -> str:
    """
    日英混合の embed_text を構築する。

    英語テキスト → multilingual-e5 の英語理解
    日本語テキスト → 日本語クエリとの直接マッチング
    共起カード → 「強さ」「シナジー」の間接的な表現
    の3つを1つの embedding に詰め込む。
    """
    name      = row["card_name"] or ""
    type_line = row["type_line"] or ""
    oracle    = clean_text(row["oracle_text"] or "")
    colors    = row["colors"] or []
    keywords  = row["keywords"] or []
    rarity    = row["rarity"] or ""
    power     = row["power"]
    toughness = row["toughness"]
    loyalty   = row["loyalty"]
    ja_name   = row["japanese_name"] or ""
    ja_text   = clean_text(row["japanese_oracle_text"] or "")

    color_words = [COLOR_NAMES.get(c, c) for c in colors if c in COLOR_NAMES]
    main_types  = [t for t in IMPORTANT_TYPES if t in type_line]

    parts = [name]

    if main_types:
        parts.append("Type: " + " ".join(main_types))
    if color_words:
        parts.append("Color: " + " ".join(color_words))
    if keywords:
        kw = keywords if isinstance(keywords, list) else [keywords]
        parts.append("Keywords: " + ", ".join(str(k) for k in kw if k))
    if type_line:
        parts.append(type_line)
    if oracle:
        parts.append(oracle)

    # P/T または Loyalty
    if power is not None and toughness is not None:
        parts.append(f"P/T: {power}/{toughness}")
    elif loyalty is not None:
        parts.append(f"Loyalty: {loyalty}")

    if rarity in ("rare", "mythic"):
        parts.append(f"Rarity: {rarity}")

    # 日本語テキストを末尾に追加（英語と日本語の両方に対応）
    if ja_name:
        parts.append(ja_name)
    if ja_text:
        parts.append(ja_text)

    # 大会共起カード（上位5件）を追加
    # → 「このカードは大会で一緒に使われる強いカード」という文脈を embedding に注入
    if cooc_partners:
        parts.append("Often used with: " + ", ".join(cooc_partners[:5]))

    body = " | ".join(p for p in parts if p)
    return prefix + body


# ─── Step1: embed_text カラムを更新 ──────────────────────────

def update_embed_text():
    """
    mtg_cards_v2 の embed_text を日英混合 + 共起情報で再構築する。
    embedding は変えないので高速に完了する。
    """
    conn = psycopg2.connect(**DB_CONFIG)

    # 共起情報を一括取得（大会データ優先）
    print("共起情報を取得中...")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                card_name,
                array_agg(partner ORDER BY co_count DESC) AS partners
            FROM (
                SELECT card_name_a AS card_name, card_name_b AS partner, co_count
                FROM card_cooccurrence WHERE source = 'mtgtop8'
                UNION ALL
                SELECT card_name_b AS card_name, card_name_a AS partner, co_count
                FROM card_cooccurrence WHERE source = 'mtgtop8'
            ) t
            GROUP BY card_name
        """)
        cooc_map = {row[0]: list(row[1])[:5] for row in cur.fetchall()}
    print(f"共起情報あり: {len(cooc_map)} 件")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, card_name, type_line, oracle_text,
                   colors, keywords, rarity, power, toughness, loyalty,
                   japanese_name, japanese_oracle_text
            FROM mtg_cards_v2
            ORDER BY id
        """)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

    print(f"embed_text 更新対象: {len(rows)} 件")
    prefix  = "passage: "
    updated = 0

    with conn.cursor() as cur:
        for row_tuple in tqdm(rows, desc="embed_text 更新", mininterval=5):
            row      = dict(zip(cols, row_tuple))
            partners = cooc_map.get(row["card_name"], [])
            new_text = build_embed_text(row, prefix, cooc_partners=partners)
            cur.execute(
                "UPDATE mtg_cards_v2 SET embed_text = %s WHERE id = %s",
                (new_text, row["id"]),
            )
            updated += 1
            if updated % 1000 == 0:
                conn.commit()

    conn.commit()
    conn.close()
    print(f"完了: {updated} 件の embed_text を更新しました")

    # サンプル確認
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT card_name, embed_text
            FROM mtg_cards_v2
            WHERE card_name IN ('Counterspell', 'Lightning Bolt', 'Llanowar Elves')
        """)
        for name, text in cur.fetchall():
            print(f"\n  [{name}]")
            print(f"  {text[:150]}...")
    conn.close()


# ─── Step2: embedding 再計算 ─────────────────────────────────

def reembed(model_key: str):
    """
    mtg_embeddings テーブルを TRUNCATE して再計算する。
    embed_text は既に更新済みであること。
    """
    cfg = MODEL_CONFIGS[model_key]
    table_embed = cfg["table_embed"]
    prefix      = cfg["prefix"]

    print(f"モデルロード中: {cfg['model_name']}")
    model = SentenceTransformer(cfg["model_name"], cache_folder=HF_CACHE)

    conn = psycopg2.connect(**DB_CONFIG)

    # TRUNCATE して再計算
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {table_embed};")
    conn.commit()
    print(f"{table_embed} を TRUNCATE しました")

    # embed_text を全件取得
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, embed_text FROM mtg_cards_v2 ORDER BY id
        """)
        rows = cur.fetchall()

    total = len(rows)
    print(f"embedding 対象: {total} 件")

    done = 0
    for i in tqdm(range(0, total, BATCH_SIZE), desc=f"{model_key} embedding"):
        batch     = rows[i:i + BATCH_SIZE]
        ids       = [r[0] for r in batch]
        raw_texts = [r[1] or "" for r in batch]

        # prefix を付け直す（embed_text には既に "passage: " が入っているので除去して付け直す）
        texts = []
        for t in raw_texts:
            for p in ("passage: ", "query: "):
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
                    ON CONFLICT (card_id) DO UPDATE
                        SET embedding = EXCLUDED.embedding;
                """, (card_id, emb.tolist()))
        conn.commit()
        done += len(batch)

    conn.close()
    print(f"\n完了: {done} 件の embedding を {table_embed} に格納しました")


# ─── 状況確認 ─────────────────────────────────────────────────

def check_status():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM mtg_cards_v2")
        total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM mtg_embeddings_small_v2")
        small = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM mtg_embeddings_base_v2")
        base = cur.fetchone()[0]

        cur.execute("""
            SELECT card_name, embed_text
            FROM mtg_cards_v2
            WHERE card_name = 'Counterspell'
        """)
        row = cur.fetchone()

    conn.close()
    print(f"mtg_cards_v2:              {total} 件")
    print(f"mtg_embeddings_small_v2:   {small} 件")
    print(f"mtg_embeddings_base_v2:    {base} 件")
    if row:
        print(f"\nCounterspell の embed_text:")
        print(f"  {row[1][:200]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--update_text", action="store_true",
                        help="embed_text カラムを日英混合で再構築する")
    parser.add_argument("--reembed", action="store_true",
                        help="embedding を再計算する")
    parser.add_argument("--model", choices=["SMALL_V2", "BASE_V2"],
                        default="SMALL_V2")
    parser.add_argument("--status", action="store_true",
                        help="状況確認")
    args = parser.parse_args()

    if args.status:
        check_status()
    elif args.update_text:
        update_embed_text()
    elif args.reembed:
        reembed(args.model)
    else:
        parser.print_help()
