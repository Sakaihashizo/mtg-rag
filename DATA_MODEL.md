# データモデル（DATA_MODEL.md）

MTG RAG System の PostgreSQL スキーマの詳細。README の「データモデル」節の補足。
本書のスキーマ・件数は実 DB（PostgreSQL 18 + pgvector）から確認したもの（2026-07-14 時点）。

検索対象コア（リーガル・embedding 済み）は **31,635 件**（SMALL / BASE 埋め込みと一致）。Marvel Super Heroes 653 件は先行投入時は英語のみで検索から隔離していたが、2026-07-13 に日本語（カード名・オラクル）を WHISPER（Wisdom Guild が運営する日本語カード情報データベース）から補填し embedding を付与して検索対象へ昇格済み。Vintage 非リーガル（Un- セット〔銀枠ジョークセット〕・Alchemy・リバランス版）2,779 件は `*_nonlegal` テーブルへ退避し検索対象外。**日本語データの純度（2026-07-17）**: 公式和訳が存在しないカード 831 件に非公式翻訳が混入していたことを検出し（「日本語名なし・日本語テキストあり」の組を指紋として特定）、日本語オラクル列から除去・再 embedding 済み。公式テキストのみを日本語列に保持する方針（非公式訳は採用しない）。テキスト持ちカードの日本語カバレッジは 96.5%。

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
| `mtg_cards_v2` | 31,635 | カード本体（全件検索対象・Marvel 653 は 2026-07-13 に日本語補填＋embedding 付与） |
| `mtg_embeddings_small_v2` | 31,635 | multilingual-e5-small の埋め込み（384 次元・**現行唯一の embedding**） |
| `deck_list` | 17,270 | デッキ（MTGTop8 構築 6 フォーマット 12,348 ＋ Duel Commander 959 ＋ Moxfield 多人数 Commander 1,232 ＋ precon 2,731・source 列で系統分離・夜間バッチで毎日成長） |
| `deck_cards` | 694,547 | デッキ収録カード明細（card_id 解決 99.96%・未解決 303 行は次元カード等の正当な NULL） |
| `card_format_strength` | 6,257 | カード×**構築**フォーマット別 play-rate 事前集計（導出テーブル・構築 6 フォーマット） |
| `edh_card_strength` | 15,377 | カード×EDH 系（Duel Commander / 多人数 Commander）play-rate 事前集計（**構築と物理分離**・2026-07-14 分離・07-22 多人数追加） |
| `format_deck_counts` | 8 | play-rate の分母表（フォーマット別総デッキ数・recompute が更新） |
| `card_cooccurrence` | 2,835,082 | カード共起・**構築系**（precon／構築 6 フォーマットを source 列で分離。embed_text の "Often used with" は本線 4 フォーマット分のみが出所） |
| `edh_card_cooccurrence` | 513,411 | カード共起・**EDH 系**（100 枚シングルトン由来＝共起の意味論が構築と別物なため物理分離・2026-07-22） |
| `eval_runs` | 109 | 評価実行ログ（内部用） |

退役・アーカイブ（現行の検索経路からは参照されない）:

| テーブル | 行数 | 位置づけ |
|---|---:|---|
| `mtg_embeddings_base_v2` | —（DROP 済み） | **2026-07-20 退役**。SMALL との同条件一対比較で全敗し pg_dump 退避の上 DROP（復元手順は `import_cards.py` の注記に記載） |
| `*_nonlegal`（5 テーブル） | 計 約17,700 | Vintage 非リーガルの退避アーカイブ（各本体テーブルと同一スキーマ・将来の Alchemy 対応時に復元可能） |

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
| keywords | text[] | Scryfall 由来のキーワード能力（カード単位＝裏面の能力も含む） |
| front_keywords | text[] | **表面（front face）のキーワード能力のみ**の導出列。両面カードの判定を表面基準に揃える（裏面にのみ持つキーワードは「条件付き獲得」扱い）。**構造化オンリー直行路とキーワード生得フィルタで使用中** |
| legalities | jsonb | フォーマット別リーガリティ |
| tournament_score | integer | 大会データ由来の使用頻度スコア（旧集計。ランキングの boost は `card_format_strength` へ配線替え済み） |
| produced_mana | text[] | 生み出すマナの色（マナ生成判定に使用） |
| edhrec_rank | integer | EDHREC 人気度（取り込みのみ・検索未使用） |
| game_changer | boolean | 公式 Commander ブラケットの高影響フラグ（取り込みのみ） |
| face_cmcs | integer[] | 各面の「実際に撃てるマナ総量」集合（分割/X 呪文対応） |
| face_types | text[] | 手札から直接唱えられる各面の type_line 集合（face_cmcs と同一の mana_cost 非空面規則・型否定ゲートの判定列・2026-07-13） |
| has_x | boolean | X 呪文識別（列保持のみ・自動フィルタには未使用） |
| is_mana_boost | boolean | マナ加速（マナクリーチャー/ランプ）の構造化判定。oracle 解析「出すマナ − 払うマナ − 土地補正 > 0」で導出し、マナフィルターを排除。**検索フィルタで使用中** |
| target_types | text[] | oracle の「target 〜」句から導出した正規化対象型（creature / player / any / artifact / permanent / spell / planeswalker / land / enchantment ＋ creature_spell 等の修飾トークン。条件付き打ち消しは `spell_conditional` トークンで識別＝516 枚）。**カウンター判定（spell を対象に取るか・条件付きか）と強度腕の役割ゲートで使用中** |
| target | jsonb | 対象のフル句と修飾語（例 "nonblack creature"）。条件付き除去（R2 型）の判定・分析用 |
| removal_types | text[] | 除去のメカ種別（damage / destroy / exile / minus / sacrifice / bounce / tuck）。**強度腕の役割ゲートと、機構明示クエリ（「追放する除去」等）の機構ゲート（候補生成の WHERE）で使用中**（役割ゲートは恒久除去のみ通す・bounce は除外） |
| draw_count | integer | **命令文の「Draw N card(s)」だけ**を数えた最大枚数（誘発の条件文・置換文に現れる draw は数えない・採点規約 R14 の検索側の写し・2026-07-23）。**枚数指定ドロークエリの候補生成 WHERE（全腕）で使用中** |
| draw_x | boolean | 可変枚数ドロー（Draw X / that many / for each / equal to）の識別。枚数ゲートでは `draw_count >= N OR draw_x` で通過（「X を N 以上で選べば引ける」） |
| removal | jsonb | 除去の詳細（type / object / amount / stat / permanent / targeted）。順位づけ・恒久性判定用 |

索引:
- `mtg_cards_v2_pkey` — btree(id)
- `mtg_cards_v2_card_name_key` — UNIQUE btree(card_name)
- `mtg_cards_v2_oracle_text_gin_idx` — **GIN** `to_tsvector('english', COALESCE(oracle_text,''))`（英語 FTS 用）
- `mtg_cards_v2_target_types_gin` — **GIN** (target_types)
- `mtg_cards_v2_removal_types_gin` — **GIN** (removal_types)
- `mtg_cards_v2_front_keywords_gin` — **GIN** (front_keywords)

メモ: `power` / `toughness` / `loyalty` は text（`*` / `X` 等の非数値を含むため）。数値フィルタは整数行のみ CAST して比較する。

### 導出列（enrich スクリプトによる事前計算）

`is_mana_boost`・`target_types`・`target`・`removal_types`・`removal`・`front_keywords`・`floor_cmc`・`draw_count`・`draw_x` は、oracle テキスト等から事前計算する**導出列**。共通の運用:

- **不在は NULL**（番兵値は使わない）。例外は `front_keywords` のみ——「能力なし」を空配列で表現し、不在の NULL と区別する
- 再実行は値が変わる行だけ更新する冪等設計（何度走っても結果が同じ・物理チャーンを避ける）
- `embed_text` に含めないため再ベクトル化は不要。新セット取り込み後はスクリプト再実行で追随する
- 導出の入力は「手札から唱えられる面」に限定（2026-07-15。変身カードの裏面にしか無い除去語が役割タグに混入する事故を排除）

| 列 | 導出スクリプト | 判定の要点 |
|---|---|---|
| `target_types` / `target` / `removal_types` / `removal` | `enrich_removal.py` | 対象句の正規化と除去メカ種別。対象リストの列挙・割り振りダメージ構文・追加コストの条件化・墓地/ライブラリ操作の領域ガード・キッカー等のモード分解（`extra_cost`）まで対応（2026-07-15〜17 の精密化） |
| `floor_cmc` | `enrich_removal.py` | コスト軽減の床値＝版図/親和/探査/想起/ピッチのベストケース実効コスト |
| `front_keywords` | `enrich_front_keywords.py` | 表面（front face）のキーワード能力のみを保持 |
| `is_mana_boost` | enrich 系（2026-06-24 導入） | net-mana 定義「出すマナ − 払うマナ − 土地補正 > 0」でマナ加速とマナフィルターを区別 |
| `draw_count` / `draw_x` | `enrich_draw.py` | 命令文の「Draw N card(s)」だけを数える（誘発の条件文・置換文は除外＝採点規約 R14 の検索側の写し・2026-07-23） |

充填数（全 31,635 行中）: `target_types` 10,503 / `target` 9,784 / `removal_types`・`removal` 各 5,343 / `floor_cmc` 345 / `is_mana_boost` 2,419 / `front_keywords` 31,323（以上 2026-07-17 実測）・`draw_count` 2,888 / `draw_x` 430（2026-07-23 実測）。

---

## mtg_embeddings_small_v2（現行唯一の embedding）

検索対象コア 31,635 件と 1 対 1。構造を分離することで、構造化列の更新時に再ベクトル化を避ける。

| 列 | 型 | 備考 |
|---|---|---|
| id | integer | PK |
| card_id | integer | FK → `mtg_cards_v2(id)` ON DELETE CASCADE / UNIQUE |
| embedding | `vector(384)` | multilingual-e5-small・pgvector |

索引:
- btree(id)（PK）
- UNIQUE btree(card_id)
- **HNSW** `(embedding vector_cosine_ops)` WITH `(m=16, ef_construction=64)`

**mtg_embeddings_base_v2（768 次元）は 2026-07-20 に退役**: SMALL との同条件一対比較（run id=93）でベクトルが所有する全層で敗北し、「768 次元でも記述層は救えない・救うのは構造化列」を確認して DROP した。pg_dump アーカイブから復元可能（手順は `import_cards.py` の退役注記に記載）・e5-base のローカル再計算でも数時間・$0 で再構築できる。

---

## deck_list

PK = `id`、UNIQUE = `deck_name`。

列: `id` (integer, PK) / `deck_name` (text, NOT NULL, UNIQUE) / `set_code` (text) / `source` (text, NOT NULL) / `created_at` (timestamp) / `tournament_name` (text) / `tournament_date` (date) / `placement` (integer) / `player_name` (text) / `format_name` (text) / `source_url` (text) / `tournament_event_id` (integer) / `archetype` (text) / `bracket` (integer, nullable・**Moxfield 由来のみ**・公式ブラケット 1〜4 ＋ Moxfield 独自の 5=cEDH 拡張をそのまま保持・2026-07-22)。

`source` 値: `mtgtop8`（構築 4F） / `mtgtop8_pauper` / `mtgtop8_vintage` / `mtgtop8_edh`（Duel Commander） / `moxfield_edh`（多人数 Commander・ブラケット別取得） / `mtgjson_precon`。

索引: btree(id)（PK） / UNIQUE btree(deck_name)。

---

## deck_cards

デッキ収録カードの明細。PK = `id`。`deck_id` は必須 FK、`card_id` は後段の名前解決で埋める任意 FK。

列: `id` (integer, PK) / `deck_id` (integer, NOT NULL, FK → `deck_list(id)` ON DELETE CASCADE) / `card_id` (integer, nullable, FK → `mtg_cards_v2(id)`) / `card_name` (text, NOT NULL) / `count` (integer, NOT NULL) / `board` (text, NOT NULL; 値 = main / side / commander)。

索引: btree(id)（PK） / btree(card_id) / btree(card_name) / btree(deck_id)。

### 紐付けの実態（正直な記載）

| 指標 | 実測値（2026-07-24） |
|---|---|
| `card_id`（FK）が埋まっている行 | **99.96%**（694,244 / 694,547） |
| ├ board=main | 99.97%（581,247 / 581,446） |
| ├ board=side | 99.94%（110,262 / 110,323） |
| └ board=commander | 98.45%（2,735 / 2,778） |
| 未解決 303 行 | Planechase 次元カード等、検索対象コアの対象外（正当な未解決を NULL で表現）。board=commander の未解決 43 行は Moxfield 由来の Unfinity アトラクション（`[]` 付き生名・銀枠＝コア外）で同種 |

`card_id` は取り込み時ではなく後段の名前解決ステップで埋める設計。当初はスクレイプ由来の名前ゆれ（`[]` 接頭辞・分割カードの旧区切り ` / `・両面カードの表面名が DB の `A // B` 形式と不一致）で 51.8% に留まっていたが、正規化マッチで **99.96%** へ解決した（生の `card_name` 完全一致率 89.7% より解決率が高いのは正規化を挟むため）。検索・共起は `card_name` 基準で動くため `card_id` は検索の読み取りパス外だが、**フォーマット別 play-rate 集計（`card_format_strength`）は `card_id` 基準で行う**ため、この解決率は大会データ由来のランキング信号の品質に直結する。

---

## card_format_strength

カード×フォーマット別の play-rate 事前集計（大会デッキ由来のランキング信号）。複合 PK = (`card_id`, `format_name`)。`deck_cards`（card_id 基準・土地除外）から `recompute_card_format_strength.py` で再計算する導出テーブル。

列: `card_id` (integer, NOT NULL, FK → `mtg_cards_v2(id)` ON DELETE CASCADE) / `format_name` (text, NOT NULL) / `play_decks` (integer, NOT NULL)。

現在 6,257 行（3,951 カード × 構築 6 フォーマット: Legacy / Modern / Pioneer / Standard / Vintage / Pauper）。**EDH 系（Duel Commander / 多人数 Commander）は `edh_card_strength` に物理分離**（2026-07-14。少母数フォーマットの率が横断フォールバックの MAX を支配する汚染を避けるため）。率の分母は `format_deck_counts` を参照。「最強」系クエリの GT 機械採点に加え、**検索側でも使用中**（2026-07-05 接続済み）: (1) tournament_boost の加算元（旧 `tournament_score` 列からの配線替え）、(2) play-rate 上位を候補生成に加える「強度腕」、(3) 構造化オンリー直行路・検証終了直行路（除去・確定カウンター）の並び順（play_decks 降順）。大会データ由来のランキング信号の品質はこの表の集計品質に直結する。

索引: btree(card_id, format_name)（PK） / btree(format_name, play_decks DESC)。

---

## edh_card_strength

EDH 系の play-rate 事前集計。スキーマは `card_format_strength` と同一（card_id / format_name / play_decks）で、`format_name` = `Duel Commander`（MTGTop8 大会 1v1・959 デッキ）と `Commander`（Moxfield 多人数・1,232 デッキ）の 2 値。**構築テーブルと分ける理由**: EDH は母数が小さく、混ぜると「どこかの環境で一線級なら強い」の横断 MAX を少数デッキの率が支配してしまう（2026-07-14 実測 id=65〜70）。検索側は format='commander'/'duel' のとき本表を参照（2026-07-22 に多人数 Commander の実データへ切替済み・それ以前は Duel の近似）。

## format_deck_counts

play-rate の分母表（`format_name` / `total_decks`・現在 8 フォーマット）。`recompute_card_format_strength.py` が strength 再計算と同時に更新する。率＝play_decks / total_decks の分母を一元管理し、フォーマット追加時の既存値の破壊を防ぐ。

---

## card_cooccurrence

カード共起。複合 PK = (`card_name_a`, `card_name_b`, `source`)。**FK は持たず `card_name`（text）で参照**するため、ER 上は非識別関係。

列: `card_name_a` (text, NOT NULL) / `card_name_b` (text, NOT NULL) / `co_count` (integer, NOT NULL) / `source` (text, NOT NULL)。

`source` 値は構築系（`mtgjson_precon` / `mtgtop8` / `mtgtop8_vintage` / `mtgtop8_pauper` 等）。`embed_text` の「Often used with …」生成に使用（本線 4 フォーマット分のみが出所）。**EDH 系の共起は `edh_card_cooccurrence` に物理分離**（2026-07-22・下記）。

索引: UNIQUE btree(card_name_a, card_name_b, source)（PK） / btree(card_name_a, source) / btree(card_name_b, source)。

---

## edh_card_cooccurrence

EDH 系のカード共起（513,411 行・スキーマは `card_cooccurrence` と同一）。**分離の理由（設計判断）**: 共起の意味論が構築と EDH で別物——構築は同一アーキタイプの複製デッキで co_count が水増しされやすいのに対し、EDH は 100 枚シングルトンで各デッキがほぼ独立に構築されるため、高い co_count がより強いシナジー信号になる。混ぜると信号の較正が壊れる。検索への配線は未実装（統率者シナジー適合の改善候補として温存）。

---

## eval_runs（内部評価ログ）

評価ハーネスの実行結果。README の ER 図には載せない。

列: `id` (PK) / `run_date` (timestamp) / `model_key` (text) / `config_json` (jsonb) / `query_count` (integer) / `gt_count` (integer) / `recall_5` / `recall_10` / `precision_5` / `precision_10` / `mrr` / `ndcg_10`（いずれも double precision） / `note` (text)。

---

## 非リーガルアーカイブ（`*_nonlegal`）

Vintage 非リーガル（テストカード・Un- セット〔銀枠ジョークセット〕・Alchemy・A- リバランス版）を本体から退避したもの。各本体テーブルと同一スキーマ。検索対象コアをクリーンに保つための分離。

`mtg_cards_v2_nonlegal`（2,779） / `mtg_embeddings_small_v2_nonlegal`（2,779） / `mtg_embeddings_base_v2_nonlegal`（2,779） / `deck_cards_nonlegal`（401）。
