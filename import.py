# import_base.py （BASEモデル専用・バッチ処理版）
import ijson
import json
from sentence_transformers import SentenceTransformer
import psycopg2
from tqdm import tqdm

# ==================== 設定 ====================
MODEL_NAME = 'intfloat/multilingual-e5-base'   # 768次元
JSON_FILE = '/mnt/new_hdd/all_cards.json'

DB_CONFIG = {
    'dbname': 'rag_dev',
    'user': 'devuser',
    'password': '***REMOVED***',
    'host': 'localhost',
    'port': 5435
}

BATCH_SIZE = 64          # ファンがうるさければ32や16に下げてね
TABLE_CARDS = "mtg_cards_base"
TABLE_EMBED = "mtg_embeddings_base"
# =============================================

print("【BASEモデル 768次元】ロード中...")
model = SentenceTransformer(MODEL_NAME, cache_folder="/mnt/new_hdd/hf_cache")

print("DBに接続中...")
conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

print("MTGカード取り込み開始...（BASEモデル・バッチ処理）")
print("※1000枚ごとに進捗を表示します")

count = 0
batch_texts = []
batch_card_ids = []

with open(JSON_FILE, 'r', encoding='utf-8') as f:
    cards = ijson.items(f, 'item')
    
    for card_data in tqdm(cards, desc="BASE処理中", mininterval=20):
        card_name = card_data.get('name')
        if not card_name:
            continue
        
        oracle_text = card_data.get('oracle_text', '')
        text_for_embedding = f"{card_name} - {card_data.get('type_line', '')} - {oracle_text}"
        
        # raw_json保存
        try:
            raw_json_str = json.dumps(card_data, ensure_ascii=False, default=str)
        except:
            raw_json_str = json.dumps(card_data, ensure_ascii=False, default=lambda o: str(o))
        
        cur.execute(f"""
            INSERT INTO {TABLE_CARDS} (card_name, raw_json, oracle_text)
            VALUES (%s, %s::jsonb, %s)
            ON CONFLICT (card_name) DO NOTHING
            RETURNING id;
        """, (card_name, raw_json_str, oracle_text))
        
        result = cur.fetchone()
        if result:
            card_id = result[0]
            batch_texts.append(text_for_embedding)
            batch_card_ids.append(card_id)
        
        count += 1
        
        # バッチ処理
        if len(batch_texts) >= BATCH_SIZE:
            embeddings = model.encode(batch_texts, batch_size=BATCH_SIZE)
            for card_id, emb in zip(batch_card_ids, embeddings):
                cur.execute(f"""
                    INSERT INTO {TABLE_EMBED} (card_id, embedding)
                    VALUES (%s, %s::vector);
                """, (card_id, emb.tolist()))
            
            conn.commit()
            batch_texts.clear()
            batch_card_ids.clear()
        
        if count % 5000 == 0:
            print(f"→ 現在 {count:,} 枚処理完了")

# 残りのバッチを処理
if batch_texts:
    embeddings = model.encode(batch_texts, batch_size=BATCH_SIZE)
    for card_id, emb in zip(batch_card_ids, embeddings):
        cur.execute(f"""
            INSERT INTO {TABLE_EMBED} (card_id, embedding)
            VALUES (%s, %s::vector);
        """, (card_id, emb.tolist()))
    conn.commit()

cur.close()
conn.close()

print(f"\n🎉 BASEモデル取り込み完了！ 処理したカード数: {count:,} 枚")