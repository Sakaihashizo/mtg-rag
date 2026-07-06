# データモデル（DATA_MODEL.md）

MTG RAG System の PostgreSQL スキーマの詳細。README の「データモデル」節の補足。
本書のスキーマ・件数は実 DB（PostgreSQL 18 + pgvector）から確認したもの（2026-07-06 時点）。

検索対象コア（リーガル・embedding 済み）は **30,982 件**（SMALL / BASE 埋め込みと一致）。`mtg_cards_v2` はこれに加え Marvel Super Heroes 653 件（英語のみ・embedding 未付与＝検索対象外・FTS からも除外。大会データとの名寄せ用に先行投入）を含み計 31,635 件。Vintage 非リーガル（un 系・Alchemy・リバランス版）2,779 件は `*_nonlegal` テーブルへ退避し検索対象外。以下の件数はコア側。

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
| `mtg_cards_v2` | 31,635 | カード本体（コア 30,982 ＋ Marvel 653〔embedding 未付与・検索対象外〕） |
| `mtg_embeddings_small_v2` | 30,982 | multilingual-e5-small の埋め込み（384 次元） |
| `mtg_embeddings_base_v2` | 30,982 | multilingual-e5-base の埋め込み（768 次元） |
| `deck_list` | 9,721 | デッキ（MTGTop8 大会 6,990 ＋ 構築済み precon 2,731） |
| `deck_cards` | 320,563 | デッキ収録カード明細（大会 214,192 ＋ precon 106,371） |
| `card_format_strength` | 4,281 | カード×フォーマット別 play-rate 事前集計（導出テーブル） |
| `card_cooccurrence` | 2,810,359 | カード共起（embed_text の "Often used with" の出所） |
| `eval_runs` | 36 | 評価実行ログ（内部用） |
| `*_nonlegal`（5 テーブル） | 計 約17,700 | Vintage 非リーガルの退避アーカイブ（各本体テーブルと同一スキーマ） |

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
| is_mana_boost | boolean | マナ加速（マナクリーチャー/ランプ）の構造化判定。oracle 解析「出すマナ − 払うマナ − 土地補正 > 0」で導出し、マナフィルターを排除。**検索フィルタで使用中** |
| target_types | text[] | oracle の「target 〜」句から導出した正規化対象型（creature / player / any / artifact / permanent / spell / planeswalker / land / enchantment ＋ creature_spell 等の修飾トークン）。**カウンター判定（spell を対象に取るか）と強度腕の役割ゲートで使用中** |
| target | jsonb | 対象のフル句と修飾語（例 "nonblack creature"）。条件付き除去（R2 型）の判定・分析用 |
| removal_types | text[] | 除去のメカ種別（damage / destroy / exile / minus / sacrifice / bounce / tuck）。**強度腕の役割ゲートで使用中**（恒久除去のみ通す・bounce は除外） |
| removal | jsonb | 除去の詳細（type / object / amount / stat / permanent / targeted）。順位づけ・恒久性判定用 |

索引:
- `mtg_cards_v2_pkey` — btree(id)
- `mtg_cards_v2_card_name_key` — UNIQUE btree(card_name)
- `mtg_cards_v2_oracle_text_gin_idx` — **GIN** `to_tsvector('english', COALESCE(oracle_text,''))`（英語 FTS 用）
- `mtg_cards_v2_target_types_gin` — **GIN** (target_types)
- `mtg_cards_v2_removal_types_gin` — **GIN** (removal_types)

メモ: `power` / `toughness` / `loyalty` は text（`*` / `X` 等の非数値を含むため）。数値フィルタは整数行のみ CAST して比較する。

メモ（導出列）: `is_mana_boost`・`target_types`・`target`・`removal_types`・`removal` は oracle テキストからの**導出列**（enrich スクリプトで populate。除去 4 列は `enrich_removal.py`）。不在は NULL（番兵値は使わない）・再実行で全件上書きの冪等設計。`embed_text` に含めないため再ベクトル化は不要で、新セット取り込み後はスクリプト再実行で追随する。充填数（2026-07-06 実測・全 31,635 行中）: target_types 10,450 / target 9,713 / removal_types・removal 各 5,644 / is_mana_boost 2,419。

---

## mtg_embeddings_small_v2 / mtg_embeddings_base_v2

検索対象コア 30,982 件と 1 対 1（Marvel 653 件は embedding 未付与のため対象外）。構造を分離することで、構造化列の更新時に再ベクトル化を避ける。

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
| `card_id`（FK）が埋まっている行 | **99.96%**（320,444 / 320,563） |
| ├ board=main | 99.95%（258,008 / 258,127） |
| ├ board=side | 100%（62,094 / 62,094） |
| └ board=commander | 100%（342 / 342） |
| 未解決 119 行 | Planechase 次元カード等、カード DB の対象外（正当な未解決を NULL で表現） |

`card_id` は取り込み時ではなく後段の名前解決ステップで埋める設計。当初はスクレイプ由来の名前ゆれ（`[]` 接頭辞・分割カードの旧区切り ` / `・両面カードの表面名が DB の `A // B` 形式と不一致）で 51.8% に留まっていたが、正規化マッチで **99.96%** へ解決した（生の `card_name` 完全一致率 89.7% より解決率が高いのは正規化を挟むため）。検索・共起は `card_name` 基準で動くため `card_id` は検索の読み取りパス外だが、**フォーマット別 play-rate 集計（`card_format_strength`）は `card_id` 基準で行う**ため、この解決率は大会データ由来のランキング信号の品質に直結する。

---

## card_format_strength

カード×フォーマット別の play-rate 事前集計（大会デッキ由来のランキング信号）。複合 PK = (`card_id`, `format_name`)。`deck_cards`（card_id 基準・土地除外）から `recompute_card_format_strength.py` で再計算する導出テーブル。

列: `card_id` (integer, NOT NULL, FK → `mtg_cards_v2(id)` ON DELETE CASCADE) / `format_name` (text, NOT NULL) / `play_decks` (integer, NOT NULL)。

現在 4,281 行（2,930 カード × 4 フォーマット: Legacy / Modern / Pioneer / Standard）。「最強」系クエリの GT 機械採点で使用中。検索ランキング（tournament_boost）への接続は未実装（現行 boost は旧 `tournament_score` 列を参照しており、この表への配線替えが次の改善レバー）。

索引: btree(card_id, format_name)（PK） / btree(format_name, play_decks DESC)。

---

## card_cooccurrence

カード共起。複合 PK = (`card_name_a`, `card_name_b`, `source`)。**FK は持たず `card_name`（text）で参照**するため、ER 上は非識別関係。

列: `card_name_a` (text, NOT NULL) / `card_name_b` (text, NOT NULL) / `co_count` (integer, NOT NULL) / `source` (text, NOT NULL)。

`source` 値: `mtgjson_precon`（2,710,127 行） / `mtgtop8`（100,232 行）。`embed_text` の「Often used with …」生成に使用。

索引: UNIQUE btree(card_name_a, card_name_b, source)（PK） / btree(card_name_a, source) / btree(card_name_b, source)。

---

## eval_runs（内部評価ログ）

評価ハーネスの実行結果。README の ER 図には載せない。

列: `id` (PK) / `run_date` (timestamp) / `model_key` (text) / `config_json` (jsonb) / `query_count` (integer) / `gt_count` (integer) / `recall_5` / `recall_10` / `precision_5` / `precision_10` / `mrr` / `ndcg_10`（いずれも double precision） / `note` (text)。

---

## 非リーガルアーカイブ（`*_nonlegal`）

Vintage 非リーガル（テスト/un 系・Alchemy・A- リバランス版）を本体から退避したもの。各本体テーブルと同一スキーマ。検索対象コアをクリーンに保つための分離。

`mtg_cards_v2_nonlegal`（2,779） / `mtg_embeddings_small_v2_nonlegal`（2,779） / `mtg_embeddings_base_v2_nonlegal`（2,779） / `deck_cards_nonlegal`（401）。
