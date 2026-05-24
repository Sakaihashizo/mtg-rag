# import.py （バッチ処理で大幅高速化版）
import ijson
import json
from sentence_transformers import SentenceTransformer
import psycopg2
from tqdm import tqdm
import torch  # ← 追加

# ==================== 設定 ====================
MODEL_NAME = 'intfloat/multilingual-e5-large-instruct'
JSON_FILE = '/mnt/new_hdd/all_cards.json'

DB_CONFIG = {
    'dbname': 'rag_dev',
    'user': 'devuser',
    'password': '***REMOVED***',
    'host': 'localhost',
    'port': 5435
}

BATCH_SIZE = 64          # ← ここを調整（32〜128で試してみて）
# =============================================

print("モデルをロード中...")
model = SentenceTransformer(MODEL_NAME, cache_folder="/mnt/new_hdd/hf_cache")

print("DBに接続中...")
conn = psycopg2.connect(**DB_CONFIG)
cur = conn.cursor()

print("MTGカード取り込み開始...（バッチ処理で高速化）")

count = 0
batch_texts = []
batch_card_ids = []
batch_card_names = []   # 後でINSERT用

with open(JSON_FILE, 'r', encoding='utf-8') as f:
    cards = ijson.items(f, 'item')
    
    for card_data in tqdm(cards, desc="カード処理中"):
        card_name = card_data.get('name')
        if not card_name:
            continue
        
        oracle_text = card_data.get('oracle_text', '')
        text_for_embedding = f"{card_name} - {card_data.get('type_line', '')} - {oracle_text}"
        
        # JSON保存部分
        try:
            raw_json_str = json.dumps(card_data, ensure_ascii=False, default=str)
        except:
            raw_json_str = json.dumps(card_data, ensure_ascii=False, default=lambda o: str(o))
        
        cur.execute("""
            INSERT INTO mtg_cards (card_name, raw_json, oracle_text)
            VALUES (%s, %s::jsonb, %s)
            ON CONFLICT (card_name) DO NOTHING
            RETURNING id;
        """, (card_name, raw_json_str, oracle_text))
        
        result = cur.fetchone()
        if result:
            card_id = result[0]
            batch_texts.append(text_for_embedding)
            batch_card_ids.append(card_id)
            batch_card_names.append(card_name)  # 念のため
        
        count += 1
        
        # バッチサイズに達したら一括embedding
        if len(batch_texts) >= BATCH_SIZE:
            embeddings = model.encode(batch_texts, batch_size=BATCH_SIZE, convert_to_tensor=False)
            
            for card_id, emb in zip(batch_card_ids, embeddings):
                cur.execute("""
                    INSERT INTO mtg_embeddings (card_id, embedding)
                    VALUES (%s, %s::vector);
                """, (card_id, emb.tolist()))
            
            conn.commit()
            batch_texts.clear()
            batch_card_ids.clear()
            batch_card_names.clear()
        
        if count % 5000 == 0:
            print(f"→ 現在 {count:,} 枚処理完了")

# 残りのバッチを処理
if batch_texts:
    embeddings = model.encode(batch_texts, batch_size=BATCH_SIZE)
    for card_id, emb in zip(batch_card_ids, embeddings):
        cur.execute("""
            INSERT INTO mtg_embeddings (card_id, embedding)
            VALUES (%s, %s::vector);
        """, (card_id, emb.tolist()))
    conn.commit()

conn.commit()
cur.close()
conn.close()

print(f"\n🎉 取り込み完了！ 処理したカード数: {count:,} 枚")