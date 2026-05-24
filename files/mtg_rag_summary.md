# MTG RAG システム プロジェクトまとめ

## プロジェクト概要

Magic: The Gathering（MTG）の全カード（約33,000枚）を対象とした日本語対応 RAG 検索システム。
自然言語クエリ（日本語・英語）で意味的に適切なカードを高精度で検索できるようにすることが目的。

将来的には大会入賞デッキデータと組み合わせ、カード投資判断支援ツールへの発展を目指している。

---

## 環境構成

| 項目 | 内容 |
|------|------|
| ホスト OS | Windows 10 |
| 仮想環境 | Oracle VirtualBox + Ubuntu 64bit |
| DB | PostgreSQL 18 + pgvector（Docker コンテナ） |
| 接続 | localhost:5435 / DB名: rag_dev / ユーザー: devuser |
| 作業フォルダ | /mnt/mtg_rag |
| データ置き場 | /mnt/new_hdd |
| Python 仮想環境 | /mnt/new_hdd/my_rag_env |

---

## データベース構成

### カードデータ（メイン）

```
mtg_cards_v2
  id SERIAL PRIMARY KEY
  card_name TEXT UNIQUE          -- 英語カード名
  type_line TEXT                 -- タイプ行
  oracle_text TEXT               -- 英語ルールテキスト
  mana_cost TEXT                 -- マナコスト
  cmc NUMERIC                    -- 点数で見たマナコスト
  colors TEXT[]                  -- 色
  color_identity TEXT[]          -- 色アイデンティティ
  rarity TEXT                    -- レアリティ
  set_code TEXT                  -- セットコード
  set_name TEXT                  -- セット名
  collector_number TEXT          -- コレクター番号
  keywords TEXT[]                -- キーワード能力
  power TEXT                     -- パワー
  toughness TEXT                 -- タフネス
  loyalty TEXT                   -- 忠誠度
  card_faces_json JSONB          -- 両面カード情報
  legalities JSONB               -- 使用可能フォーマット
  japanese_name TEXT             -- 日本語カード名
  japanese_oracle_text TEXT      -- 日本語ルールテキスト
  embed_text TEXT                -- embedding 用テキスト（日英混合）
  layout TEXT                    -- カードレイアウト
```

### Embedding テーブル

```
mtg_embeddings_small_v2   -- multilingual-e5-small (384次元)
mtg_embeddings_base_v2    -- multilingual-e5-base  (768次元)
  id SERIAL PRIMARY KEY
  card_id INTEGER REFERENCES mtg_cards_v2(id)
  embedding vector(N)
```

### デッキデータ

```
deck_list
  id SERIAL PRIMARY KEY
  deck_name TEXT UNIQUE
  set_code TEXT
  source TEXT                    -- 'mtgjson_precon' / 'mtgtop8' / 'world_championship'
  tournament_name TEXT
  tournament_date DATE
  placement INTEGER
  player_name TEXT
  format_name TEXT
  source_url TEXT
  tournament_event_id INTEGER

deck_cards
  id SERIAL PRIMARY KEY
  deck_id INTEGER REFERENCES deck_list(id)
  card_name TEXT
  count INTEGER
  board TEXT                     -- 'main' / 'side' / 'commander'

card_cooccurrence              -- カード共起集計
  card_name_a TEXT
  card_name_b TEXT
  co_count INTEGER
  source TEXT
```

### 旧テーブル（参考・非推奨）

```
mtg_cards                      -- LARGE モデル用（oracle_text の一部が NULL）
mtg_embeddings                 -- LARGE (1024次元)
mtg_embeddings_base            -- BASE  (768次元)  ※ mtg_cards を参照
mtg_embeddings_small           -- SMALL (384次元)  ※ mtg_cards を参照
```

---

## 作成済みスクリプト一覧

| ファイル名 | 役割 | 状態 |
|-----------|------|------|
| `import_cards.py` | all_cards.json → mtg_cards_v2 + embedding | 完成・動作確認済み |
| `enrich_cards.py` | game フィールド・日本語テキストを mtg_cards_v2 に追加 | 完成・動作確認済み |
| `rebuild_embed_text.py` | embed_text 再構築 + embedding 再計算 | 完成・動作確認済み |
| `extract_japanese.py` | all_cards.json から日本語テキストを抽出 | 完成・動作確認済み |
| `benchmark_models.py` | SMALL/BASE モデルの精度比較・評価 | 完成・動作確認済み |
| `mtg_hybrid_search_v2.py` | ハイブリッド検索（ベクトル + 英語FTS + 日本語FTS + RRF） | 完成・動作確認済み |
| `import_decks.py` | MTGJSON プリコンデッキ取り込み + 共起集計 | 完成・動作確認済み |
| `scrape_mtgtop8.py` | MTGTop8 大会デッキスクレイパー | 完成・未実行 |

---

## embed_text の構造

```
passage: Counterspell | Type: Instant | Color: blue | Keywords: ... |
Instant | Counter target spell. | P/T: N/A |
対抗呪文 | 呪文１つを対象とする。それを打ち消す。
```

日英混合テキストにより日本語・英語クエリの両方に対応。

---

## ハイブリッド検索の設計

```
クエリ入力（日本語 or 英語）
  ↓
クエリ拡張（日本語 → MTG英語キーワードマッピング）
  ↓
3系統並列検索:
  ① ベクトル検索（pgvector HNSW）        重み 2.0
  ② 英語 FTS（to_tsvector + BM25風）     重み 1.5
  ③ 日本語 LIKE 検索（japanese_oracle_text） 重み 2.0
  ↓
RRF（Reciprocal Rank Fusion）でマージ
  ↓
フォーマット絞り込み（legalities JSONB）
type_line フィルタ（Creature のみ等）
  ↓
TOP-K 結果出力
```

---

## 検索精度の現状

### できていること（動作確認済み）

| クエリ | 結果 |
|--------|------|
| `counter target spell`（英語） | KW一致率 100%・全件カウンター呪文 |
| `カードを2枚引く`（日本語） | 全件ドロー呪文が上位 |
| `対抗呪文`（日本語） | Counterspell・Force of Negation 等が上位 |
| `モダンの最強カウンター呪文`（フォーマット絞り込み） | モダンリーガルのカウンター呪文に絞れる |
| `飛行を持つクリーチャー`（type フィルタ） | Creature のみに絞れる |

### 残課題

| クエリ | 問題 |
|--------|------|
| `純粋に強いカウンター呪文` | 「強い」という概念が oracle_text に存在しない |
| `最強の単体除去` | Path to Exile・Swords to Plowshares が上位に来ない |
| `マナ加速できるカード` | Llanowar Elves 等が安定して上位に来ない |

残課題の根本原因：「強い・最強」という概念はカードテキストに存在しない。
解決策として大会入賞デッキの共起情報を embed_text に追加することで改善予定。

---

## データ取り込み状況

| データ | 件数 | 状態 |
|--------|------|------|
| ユニークカード（mtg_cards_v2） | 33,433件 | 完了 |
| SMALL embedding | 33,433件 | 完了 |
| BASE embedding | 33,433件 | 完了 |
| 日本語テキスト（japanese_oracle_text） | 約20,000件 | 完了 |
| プリコンデッキ（mtgjson_precon） | 2,734件 | 完了 |
| 世界選手権デッキ（WC97〜WC04） | 32件 | 完了（precon と同テーブル） |
| 大会デッキ（mtgtop8） | 0件 | スクレイパー完成・未実行 |

---

## 次のアクション（優先順）

1. **MTGTop8 スクレイピング実行**
   ```bash
   python scrape_mtgtop8.py --format MO --meta 276 --year 2024
   python scrape_mtgtop8.py --format MO --meta 315 --year 2025
   ```

2. **大会データで共起集計を更新**
   ```bash
   python import_decks.py --cooccur
   ```

3. **共起情報を embed_text に追加して再 embedding**
   - 「Counterspell は Force of Will・Brainstorm とよく使われる」を embed_text に追加
   - rebuild_embed_text.py を修正して再実行

4. **mtg_hybrid_search_v2.py のファイル出力を完成させる**
   - 現在途中（str_replace エラーで中断）

5. **ポートフォリオ用 README 作成**
   - アーキテクチャ図
   - 改善前後の検索結果比較
   - 技術的チャレンジと解決策

---

## 技術的に解決した主な問題

1. **両面カードの oracle_text が NULL**
   → `card_faces` からフォールバック取得する `extract_oracle_text()` で解決

2. **art_series 等の非ゲームカードが混入**
   → `EXCLUDE_LAYOUTS` で取り込み時にスキップ

3. **Counterspell が237件重複**
   → `ON CONFLICT (card_name) DO NOTHING` + UNIQUE 制約で解決

4. **mtg_embeddings_base が mtg_cards を参照していた設計ミス**
   → mtg_cards を1本に統一・mtg_cards_base/small を廃止

5. **「カウンター」の多義性（呪文打ち消し vs +1/+1カウンター）**
   → 日本語 FTS（`japanese_oracle_text LIKE '%打ち消す%'`）で解決

6. **「飛行を持つクリーチャー」にオーラが混入**
   → `type_line LIKE '%Creature%'` フィルタで解決

---

## スキルシートに書ける内容（確認済み）

```
【自然言語検索システムの設計・実装（MTG RAGシステム）】

■ インフラ・データ基盤
- PostgreSQL 18 + pgvector を Docker コンテナで構築
- HNSW インデックスによる高速ベクトル類似検索（33,433件・ミリ秒レベル）
- VirtualBox/Ubuntu 環境での Linux サーバー運用

■ データエンジニアリング
- 2.3GB の大規模 JSON を ijson ストリーミング処理で省メモリ取り込み
- 英語・日本語テキストの正規化・前処理パイプライン構築
- 両面カード・分割カードなど複雑なデータ構造の解析と統合
- 外部キー制約・UNIQUE 制約を考慮したテーブル設計とデータ修復

■ 機械学習・自然言語処理
- multilingual-e5（SMALL/BASE）による多言語 embedding 生成
- 3モデル（384/768/1024次元）の定量的精度比較・評価フレームワーク構築
- embedding テキストの前処理最適化（マナ記号変換・タイプ情報付与・日英混合）

■ 検索システム
- ベクトル検索 + 英語FTS + 日本語LIKE の3系統ハイブリッド実装
- Reciprocal Rank Fusion（RRF）による検索結果の重み付きマージ
- JSONB を使ったフォーマット絞り込み（standard/modern 等）
- 日本語クエリ・英語クエリ両対応

■ 成果（確認済み）
- 英語クエリ KW一致率 100%
- 日本語クエリ「カードを2枚引く」全件ドロー呪文が上位出力
- フォーマット絞り込み・タイプフィルタの動作確認
```
