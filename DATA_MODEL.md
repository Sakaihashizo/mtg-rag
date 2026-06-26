# データモデル（DATA_MODEL.md）

MTG RAG System の PostgreSQL スキーマの詳細。README の「データモデル」節の補足。
本書のスキーマ・件数は実 DB（PostgreSQL 18 + pgvector）から確認したもの（2026-06-25 時点）。

検索対象コア（リーガル・embedding 済み）は **30,982 件**（`mtg_cards_v2` / SMALL / BASE の三表一致）。vintage 非リーガル（un 系・Alchemy・リバランス版）約 2,700 件は `*_nonlegal` テーブルへ退避し検索対象外。以下の件数はコア側。

---

## 設計方針（要約）

- **1 対 1 属性は列に昇格し、別テーブルに分割しない**。汎用 key-value（EAV）は複合フィルタで self-JOIN が増えるため採らない。
- **embedding は別テーブル**（`mtg_embeddings_*_v2`）。構造化列は `embed_text` に含めないため、列の追加・更新で reembed が不要。
- **デッキとカードの多対多関係は中間テーブルで正規化**（`deck_list`—`deck_cards`—`mtg_cards_v2`）。ただし FK `card_id` の backfill は部分的（後述）。
- **生データは JSONB/バルク、ホットパスで使う属性だけ列に昇格**（`legalities`・`card_faces_json` は JSONB 保持）。
- **リーガル/非リーガルを分離**（`*_nonlegal` に退避）。

---

## テーブル一覧

| テーブル | 行数 | 役割 |
|---|---:|---|
| `mtg_cards_v2` | 30,982 | カード本体（コア） |
| `mtg_embeddings_small_v2` | 30,982 | multilingual-e5-small の埋め込み（384 次元） |
| `mtg_embeddings_base_v2` | 30,982 | multilingual-e5-base の埋め込み（768 次元） |
| `deck_list` | ~8,656 | 大会・プリコン等のデッキ |
| `deck_cards` | 288,054 | デッキ収録カード（明細） |
| `card_cooccurrence` | 2,789,113 | カード共起（embed_text の "Often used with" の出所） |
| `eval_runs` | 9 | 評価実行ログ（内部用） |
| `*_nonlegal`（4 テーブル） | 計 ~8,738 | vintage 非リーガルの退避アーカイブ（本体と同一スキーマ） |

---

## mtg_cards_v2（コア）

PK = `id`、UNIQUE = `card_name`。

| 列 | 型 | 備考 |
|---|---|---|
| id | integer | PK |
| card_name | text | UNIQUE |
| type_line | text | |
| oracle_text | text | |
| mana_cost | text | |
| colors | text[] | |
| rarity | text | |
| layout | text | |
| embed_text | text | 埋め込み対象テキスト（日英混合） |
| japanese_name | text | |
| japanese_oracle_text | text | |
| power | text | `*` / `X` 等の非数値を保持するため text |
| toughness | text | 同上 |
| loyalty | text | 同上 |
| cmc | numeric | マナ総量（ルール値） |
| color_identity | text[] | |
| set_code | text | |
| set_name | text | |
| collector_number | text | |
| card_faces_json | jsonb | 両面・分割カードの面情報 |
| keywords | text[] | |
| legalities | jsonb | フォーマット別リーガリティ |
| tournament_score | integer | 大会データ由来の使用頻度スコア |
| produced_mana | text[] | 生み出すマナの色（マナ生成判定に使用） |
| edhrec_rank | integer | EDHREC 人気度（取り込みのみ・検索未使用） |
| game_changer | boolean | 公式 Commander ブラケットの高影響フラグ（取り込みのみ） |
| face_cmcs | integer[] | 各面の「実際に撃てるマナ総量」集合（分割/X 呪文対応） |
| has_x | boolean | X 呪文識別（列保持のみ・自動フィルタには未使用） |

索引:
- `mtg_cards_v2_pkey` — btree(id)
- `mtg_cards_v2_card_name_key` — UNIQUE btree(card_name)
- `mtg_cards_v2_oracle_text_gin_idx` — **GIN** `to_tsvector('english', COALESCE(oracle_text,''))`（英語 FTS 用）

メモ: `power` / `toughness` / `loyalty` は text（`*` / `X` 等の非数値を含むため）。数値フィルタは整数行のみ CAST して比較する。

---

## mtg_embeddings_small_v2 / mtg_embeddings_base_v2

カード本体と 1 対 1（件数一致 30,982 / 30,982）。構造を分離することで、構造化列の更新時に再ベクトル化を避ける。

| 列 | 型 | 備考 |
|---|---|---|
| id | integer | PK |
| card_id | integer | FK → `mtg_cards_v2(id)` ON DELETE CASCADE / UNIQUE |
| embedding | `vector(384)`（small） / `vector(768)`（base） | pgvector |

索引（両テーブル共通の形）:
- btree(id)（PK）
- UNIQUE btree(card_id)
- **HNSW** `(embedding vector_cosine_ops)` WITH `(m=16, ef_construction=64)`

---

## deck_list

PK = `id`、UNIQUE = `deck_name`。

列: `id` (integer, PK) / `deck_name` (text, NOT NULL, UNIQUE) / `set_code` (text) / `source` (text, NOT NULL) / `created_at` (timestamp) / `tournament_name` (text) / `tournament_date` (date) / `placement` (integer) / `player_name` (text) / `format_name` (text) / `source_url` (text) / `tournament_event_id` (integer) / `archetype` (text)。

索引: btree(id)（PK） / UNIQUE btree(deck_name)。

---

## deck_cards

デッキ収録カードの明細。PK = `id`。`deck_id` は必須 FK、`card_id` は後段の名前解決で埋める任意 FK。

列: `id` (integer, PK) / `deck_id` (integer, NOT NULL, FK → `deck_list(id)` ON DELETE CASCADE) / `card_id` (integer, nullable, FK → `mtg_cards_v2(id)`) / `card_name` (text, NOT NULL) / `count` (integer, NOT NULL) / `board` (text, NOT NULL; 値 = main / side / commander)。

索引: btree(id)（PK） / btree(card_id) / btree(card_name) / btree(deck_id)。

### 紐付けの実態（正直な記載）

| 指標 | 実測値 |
|---|---|
| `card_id`（FK）が埋まっている行 | **51.8%**（149,303 / 288,054） |
| ├ board=main | 57.7%（135,550 / 235,096） |
| ├ board=side | 25.5%（13,413 / 52,616） |
| └ board=commander | 99.4%（340 / 342） |
| `card_name` がカタログに存在（名前一致） | **97.3%**（280,399 / 288,054） |

`card_id` は import 時ではなく後段の名前解決ステップで埋める設計のため、後発のデッキ取り込み後に解決を回さないと未充足のまま残る（現状 51.8%）。ただし **検索・共起ともに `card_name` 基準で動くため `card_id` は read path 外**であり、検索品質には影響しない。「正規化済み＝ほぼ全リンク」ではなく、FK backfill は道半ば、というのが正確な状態。

---

## card_cooccurrence

カード共起。複合 PK = (`card_name_a`, `card_name_b`, `source`)。**FK は持たず `card_name`（text）で参照**するため、ER 上は非識別関係。

列: `card_name_a` (text, NOT NULL) / `card_name_b` (text, NOT NULL) / `co_count` (integer, NOT NULL) / `source` (text, NOT NULL)。

`source` 値: `mtgjson_precon`（2,710,127 行） / `mtgtop8`（78,986 行）。`embed_text` の「Often used with …」生成に使用。

索引: UNIQUE btree(card_name_a, card_name_b, source)（PK） / btree(card_name_a, source) / btree(card_name_b, source)。

---

## eval_runs（内部評価ログ）

評価ハーネスの実行結果。README ER には載せない。

列: `id` (PK) / `run_date` (timestamp) / `model_key` (text) / `config_json` (jsonb) / `query_count` (integer) / `gt_count` (integer) / `recall_5` / `recall_10` / `precision_5` / `precision_10` / `mrr` / `ndcg_10`（いずれも double precision） / `note` (text)。

---

## 非リーガルアーカイブ（`*_nonlegal`）

vintage 非リーガル（テスト/un 系・Alchemy・A- リバランス版）を本体から退避したもの。各本体と同一スキーマ。検索対象コアをクリーンに保つための分離。

`mtg_cards_v2_nonlegal`（2,779） / `mtg_embeddings_small_v2_nonlegal`（2,779） / `mtg_embeddings_base_v2_nonlegal`（2,779） / `deck_cards_nonlegal`（401）。
