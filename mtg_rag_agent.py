"""
mtg_rag_agent.py — MTG RAG エージェント（Gemini API版）
=========================================================
ユーザーの質問 → RAG検索 → Gemini API で回答 の流れを実装。

使い方:
  # 対話モード（英語入力のみ）
  export GOOGLE_API_KEY='your-api-key'
  python mtg_rag_agent.py

  # ファイルから質問を読み込む（日本語対応）
  python mtg_rag_agent.py --question_file questions.txt

  # 1件だけ質問
  python mtg_rag_agent.py --question "best counterspell in modern"

question.txt の書き方:
  # で始まる行はコメント（スキップ）
  空行はスキップ
  フォーマット指定: [modern] 最強の単体除去
"""

import argparse
import re
import sys
import os
import time
import random
import datetime
import textwrap
import requests

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_hybrid_search_v2 import MTGHybridSearcherV2, extract_keywords

# ─── 設定 ─────────────────────────────────────────────────────
GEMINI_MODEL   = "gemini-2.5-flash-lite"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
MAX_TOKENS     = 1024
RETRY_COUNT    = 7
RETRY_WAIT_BASE = 1   # 秒（初回待機時間）
RETRY_WAIT_MAX  = 60  # 秒（最大待機時間）

SYSTEM_PROMPT = """あなたはMagic: The Gatheringの専門家アシスタントです。
ユーザーの質問に対して、提供されたカード情報を精査した上で回答してください。

回答のガイドライン:
- 提供されたカードリストをまず精査し、質問に本当に関係するカードだけを選ぶ
- 関係ないと判断したカードは無視してよい（無理にこじつけない）
- 関係するカードが1枚もない場合は「提供されたカード情報では適切に回答できません」と正直に答える
- 選んだカードについてはカード名（日本語名と英語名）・マナコスト・効果を説明する
- 「採用デッキ」の情報がある場合は、そのカードがどのようなデッキで使われるかを説明する
- なぜそのカードが質問に適しているか理由も添える
- カードの効果をただ言い換えるだけでなく、なぜ強いか・どう使うかを説明する
- リストにないカードについては言及しない
- 日本語で回答する"""

FORMAT_KEYWORDS = {
    "スタンダード": "standard", "standard": "standard",
    "パイオニア":   "pioneer",  "pioneer":  "pioneer",
    "モダン":       "modern",   "modern":   "modern",
    "レガシー":     "legacy",   "legacy":   "legacy",
    "ヴィンテージ": "vintage",  "vintage":  "vintage",
    "パウパー":     "pauper",   "pauper":   "pauper",
}


def structured_direct_gate(query: str) -> bool:
    """LLM ルーターを呼ばずに構造化オンリー直行路へ行けるクエリかの入口判定（2026-07-07）。
    辞書（extract_keywords）で完結するキーワード系クエリは、Gemini を呼んでも
    HyDE が直行路で捨てられるだけ＝レイテンシとクォータの無駄なのでスキップする。
    ガードは2枚:
      (1) kw_only ＝ キーワード能力のみ・他の意味語なし・boost/除去/カウンター/付与の意図なし
          （extract_keywords 側の極性ガード込み）
      (2) クエリに数字なし ＝ 数値抽出（cmc 等）は現状ルーターの仕事なので、
          数字があるときはルーターに任せる（迷ったら高い方＝正確な方に倒す）
    format 語（「モダンの〜」）は search_cards 側の決定的フォールバックが拾うため妨げない。"""
    _, _, _, tb, rm, cm, _, kw_only = extract_keywords(query)
    return (kw_only and not (tb or rm or cm)
            and not re.search(r'[0-9０-９一二三四五六七八九十]', query))


def detect_format(text: str) -> str | None:
    """クエリ文字列からフォーマット名を素朴に検出する（決定的・LLM 不要）。
    LLM 抽出が null だったときのフォールバック。"""
    low = text.lower()
    for kw, f in FORMAT_KEYWORDS.items():
        if kw.lower() in low:
            return f
    return None


# ─── 質問ファイル読み込み ─────────────────────────────────────

def load_questions(filepath: str) -> list[tuple[str, str | None]]:
    """
    質問ファイルを読み込む。
    戻り値: [(question, format_or_None), ...]

    ファイル形式:
      # コメント行（スキップ）
      純粋に強いカウンター呪文
      [modern] 最強のカウンター呪文
      [standard] 単体除去のおすすめ
    """
    questions = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # [format] プレフィックスを解析
            fmt = None
            if line.startswith("["):
                end = line.find("]")
                if end > 0:
                    fmt_str = line[1:end].lower()
                    line    = line[end+1:].strip()
                    # フォーマット名を正規化
                    for kw, f in FORMAT_KEYWORDS.items():
                        if kw.lower() == fmt_str or f == fmt_str:
                            fmt = f
                            break
                    if not fmt:
                        fmt = fmt_str  # そのまま使う

            if line:
                questions.append((line, fmt))

    return questions


# ─── Query Rewriting ──────────────────────────────────────────

REWRITE_PROMPT = """あなたはMagic: The Gatheringの検索エキスパートです。
ユーザーのクエリを解析して以下のJSON形式のみで返してください。

{{
  "search_query": "検索に使う核心的な日本語キーワードのみ（枚数・選んで・理由・教えて等の指示文を除く）",
  "hyde_text": "クエリに対して理想的なMTGカードのテキストを英語で生成（50単語以内・カード名不要）",
  "ja_hyde_text": "同じ理想カードのテキストを日本語で生成（hyde_text の和訳に相当・80文字以内・カード名不要）",
  "tournament_boost": true or false,
  "removal_mode": true or false,
  "counter_mode": true or false,
  "type_filter": "Creature" or "Instant" or "Sorcery" or "Enchantment" or "Artifact" or null,
  "format": "standard" or "pioneer" or "modern" or "legacy" or "vintage" or "pauper" or null,
  "mana_producer": true or false,
  "cmc_min": 整数 or null,
  "cmc_max": 整数 or null,
  "power_min": 整数 or null,
  "power_max": 整数 or null,
  "toughness_min": 整数 or null,
  "toughness_max": 整数 or null
}}

search_query の抽出ルール:
- 「〜を3枚選んで」「それぞれ理由を教えて」「おすすめは？」等の指示文は除く
- 検索の核心となるキーワードだけを残す
- 日本語のまま出力する

hyde_text のルール:
- 実在するカードのテキストをそのまま使わない
- MTGのカードテキストの形式で書く（例: "When this enters, draw a card."）
- クエリに対して「理想的なカード」のテキストを想像して生成する
- 英語で出力する

ja_hyde_text のルール:
- hyde_text と同じ「理想的なカード」を日本語のカードテキスト形式で書く（例: "これが戦場に出たとき、カードを1枚引く。"）
- hyde_text の内容と一致させる（英語版の和訳に相当）
- 日本語で出力する（カード名は不要）

type_filter の判定ルール:
- 「クリーチャー」「生物」が含まれる → "Creature"
- 「インスタント」「瞬速呪文」が含まれる → "Instant"
- 「ソーサリー」が含まれる → "Sorcery"
- 「エンチャント」が含まれる → "Enchantment"
- 「アーティファクト」が含まれる → "Artifact"
- 特定のカードタイプが指定されていない → null

format の判定ルール（フォーマット指定の抽出。フォーマット語は search_query から除く）:
- 「スタンダード」→ "standard" ／「パイオニア」→ "pioneer" ／「モダン」→ "modern"
- 「レガシー」→ "legacy" ／「ヴィンテージ」→ "vintage" ／「パウパー」→ "pauper"
- フォーマット指定が無い → null

数値制約の判定ルール（マナ総量 cmc・パワー・タフネス。指定が無ければ全て null）:
- 「Nマナ」「マナ総量N」「Nマナの」→ cmc_min=N かつ cmc_max=N
- 「Nマナ以下」→ cmc_max=N ／「Nマナ以上」→ cmc_min=N
- 「パワーN以上」→ power_min=N ／「タフネスN以下」→ toughness_max=N（パワー/タフネスも同様）
- 「強い」「最強」「重い」等の曖昧表現は数値ではない → null（強さは tournament_boost で扱う）

mana_producer の判定ルール（マナを生み出すカードに絞るフラグ。Scryfall の produced_mana 構造化データで厳密に絞る）:
- 「マナクリーチャー」「マナエルフ」「マナを生む」「マナを出す」「マナ生成」「マナファクト」→ true
- 「マナ加速」「ランプ」など"マナを増やす"意図が明確 → true
- 上記に当てはまらない（マナと無関係）→ false
入力: 「カードを2枚引ける強いカードを3枚選んでそれぞれ理由を教えて」
出力: {{"search_query": "カードを2枚引く", "hyde_text": "Draw two cards. This spell costs {{1}} less if you control a creature.", "ja_hyde_text": "カードを2枚引く。あなたがクリーチャーをコントロールしているなら、この呪文を唱えるためのコストは{{1}}少なくなる。", "tournament_boost": true, "removal_mode": false, "counter_mode": false, "type_filter": null, "format": null}}

入力: 「環境で強いクリーチャーを3枚」
出力: {{"search_query": "環境で強いクリーチャー", "hyde_text": "2/2 creature with haste. When this creature enters, draw a card. Ward {{2}}.", "ja_hyde_text": "速攻を持つ2/2のクリーチャー。これが戦場に出たとき、カードを1枚引く。護法{{2}}。", "tournament_boost": true, "removal_mode": false, "counter_mode": false, "type_filter": "Creature", "format": null}}

入力: 「純粋に強いカウンター呪文を3枚選んでそれぞれ理由を教えて」
出力: {{"search_query": "純粋に強いカウンター呪文", "hyde_text": "Counter target spell. Draw a card.", "ja_hyde_text": "呪文1つを対象とし、それを打ち消す。カードを1枚引く。", "tournament_boost": true, "removal_mode": false, "counter_mode": true, "type_filter": "Instant", "format": null}}

入力: 「モダンの最強単体除去を教えて」
出力: {{"search_query": "最強の単体除去", "hyde_text": "Destroy target creature. This spell costs {{1}} less if the creature entered this turn.", "ja_hyde_text": "クリーチャー1体を対象とし、それを破壊する。そのクリーチャーがこのターンに戦場に出ていたなら、この呪文を唱えるためのコストは{{1}}少なくなる。", "tournament_boost": true, "removal_mode": true, "counter_mode": false, "type_filter": null, "format": "modern"}}

入力: 「1マナのマナクリーチャー」
出力: {{"search_query": "マナを生むクリーチャー", "hyde_text": "Creature. {{T}}: Add one mana of any color.", "ja_hyde_text": "クリーチャー。{{T}}：好きな色のマナ1点を加える。", "tournament_boost": false, "removal_mode": false, "counter_mode": false, "type_filter": "Creature", "format": null, "mana_producer": true, "cmc_min": 1, "cmc_max": 1}}

入力: 「2マナ以下のカウンター呪文」
出力: {{"search_query": "カウンター呪文", "hyde_text": "Counter target spell.", "ja_hyde_text": "呪文1つを対象とし、それを打ち消す。", "tournament_boost": false, "removal_mode": false, "counter_mode": true, "type_filter": "Instant", "format": null, "cmc_max": 2}}

ユーザーのクエリ: {query}

JSONのみ出力。余分なテキスト・マークダウン不要。"""


def rewrite_query(query: str, api_key: str, raise_on_error: bool = False):
    """
    Gemini を使ってクエリを解析し検索用クエリ・HyDEテキスト・フラグを返す。
    戻り値: (search_query, hyde_text, ja_hyde_text, tournament_boost, removal_mode,
             counter_mode, type_filter, router_format, filters)
    失敗時は元のクエリと空文字・False フラグを返す（router_format だけは
    detect_format による決定的検出が生きる）。raise_on_error=True なら失敗を
    握りつぶさず例外を投げる（build_router_cache 等、原因を見たい呼び出し用）。
    """
    import json as _json
    headers = {
        "Content-Type":   "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {
        "contents": [
            {"parts": [{"text": REWRITE_PROMPT.format(query=query)}]}
        ],
        "generationConfig": {
            "maxOutputTokens": 256,
            # 構造化抽出は創造性ゼロでいい仕事＝貪欲デコード。0.1 の残留サンプリングは
            # ハードフィルタ行きフィールド（cmc/type等）を稀に揺らすリスクでしかない
            # （2026-07-07 安定性試験 hammer_router.py と同時に 0.1→0 へ）。
            # 回答生成側の 0.7 は別（あちらは創造性が仕事）。
            "temperature": 0.0,
        }
    }
    try:
        resp = requests.post(GEMINI_API_URL, headers=headers,
                             json=payload, timeout=15)
        resp.raise_for_status()
        data     = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw_text = raw_text.replace("```json", "").replace("```", "").strip()
        parsed   = _json.loads(raw_text)
        search_query     = parsed.get("search_query", query)
        hyde_text        = parsed.get("hyde_text", "")
        ja_hyde_text     = parsed.get("ja_hyde_text", "")
        tournament_boost = bool(parsed.get("tournament_boost", False))
        removal_mode     = bool(parsed.get("removal_mode", False))
        counter_mode     = bool(parsed.get("counter_mode", False))
        type_filter      = parsed.get("type_filter", None)

        # format（フォーマット指定）。既知フォーマット名以外は捨てる
        # （cmc 等と同じく LLM 出力を信用せず検証する）
        fmt_raw = parsed.get("format")
        router_format = (
            fmt_raw.lower()
            if isinstance(fmt_raw, str)
               and fmt_raw.lower() in set(FORMAT_KEYWORDS.values())
            else None
        )
        # LLM が null でも原文にフォーマット語があれば決定的に拾う
        if router_format is None:
            router_format = detect_format(query)

        # 数値制約（cmc/power/toughness）を検証して filters dict に詰める。
        # LLM 出力は信用せず int 化＋範囲チェック（門番側でも再検証される＝二重防御）。
        def _vint(key):
            try:
                n = int(parsed.get(key))
            except (ValueError, TypeError):
                return None
            return n if 0 <= n <= 99 else None
        filters = {}
        # 数値幻覚ガード（2026-07-07・構造的裁可）: クエリ本文に数字が無いのに LLM が
        # 数値制約を出したら全部捨てる。プロンプト規則（「強い」「コンボ」等から数値を
        # 発明しない）は貪欲経路の揺れで破られうると実測済み（hammer_router: コンボで
        # cmc0-2・フィルタリングで power4・環境で強いで 4/4 と、規則を足すたび別クエリへ
        # 引っ越すもぐら叩き）→ LLM に頼まず数字の有無で機械判定。ハードフィルタ行きの
        # フィールドは「LLM が提案・決定的コードが裁可」で守る。
        if re.search(r'[0-9０-９一二三四五六七八九十]', query):
            for k in ("cmc_min", "cmc_max", "power_min", "power_max",
                      "toughness_min", "toughness_max"):
                v = _vint(k)
                if v is not None:
                    filters[k] = v
        # mana_producer は bool フラグ。True のときだけ filters に載せて
        # **filters 経由で門番（searcher）の mana_producer 引数へ渡す。
        if bool(parsed.get("mana_producer", False)):
            filters["mana_producer"] = True
        return (search_query, hyde_text, ja_hyde_text, tournament_boost,
                removal_mode, counter_mode, type_filter, router_format, filters)
    except Exception:
        if raise_on_error:
            raise
        return query, "", "", False, False, False, None, detect_format(query), {}

def get_archetypes(card_name: str, conn) -> list[str]:
    """カードが使われているアーキタイプ TOP5 を取得する"""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.archetype, COUNT(*) as cnt
                FROM deck_cards dc
                JOIN deck_list d ON dc.deck_id = d.id
                WHERE dc.card_name = %s
                  AND d.source = 'mtgtop8'
                  AND d.archetype IS NOT NULL
                  AND dc.board = 'main'
                GROUP BY d.archetype
                ORDER BY cnt DESC
                LIMIT 5
            """, (card_name,))
            rows = cur.fetchall()
            return [row[0] for row in rows]
    except Exception:
        return []


def search_cards(searcher, query, top_k, fmt,
                 tournament_boost=False, removal_mode=False,
                 counter_mode=False, type_filter_override=None,
                 hyde_text="", ja_hyde_text="", filters=None, router_format=None):
    filters = filters or {}
    # フォーマット決定の優先順位: 明示引数 > ルーター抽出 > クエリ内キーワード検出。
    # キーワード検出はフォールバック（rewrite はフォーマット語を search_query から
    # 削るため、書き換え後のクエリでは検出できないことがある）。
    if fmt is None:
        fmt = router_format
    if fmt is None:
        for kw, f in FORMAT_KEYWORDS.items():
            if kw in query:
                fmt = f
                break

    # HyDE が有効な場合はハイブリッド検索クエリと HyDE 検索を統合
    if hyde_text:
        results = searcher.search_with_hyde(
            query=query,
            hyde_text=hyde_text,
            ja_hyde_text=ja_hyde_text,
            top_k=top_k,
            format=fmt,
            tournament_boost_override=tournament_boost,
            removal_mode_override=removal_mode,
            counter_mode_override=counter_mode,
            type_filter_override=type_filter_override,
            **filters,
        )
    else:
        results = searcher.search(
            query, top_k=top_k, format=fmt,
            tournament_boost_override=tournament_boost,
            removal_mode_override=removal_mode,
            counter_mode_override=counter_mode,
            type_filter_override=type_filter_override,
            **filters,
        )
    cards = []
    for r in results:
        # アーキタイプ情報を取得
        archetypes = get_archetypes(r.card_name, searcher.conn)
        cards.append({
            "card_name":            r.card_name,
            "japanese_name":        r.japanese_name,
            "type_line":            r.type_line,
            "mana_cost":            r.mana_cost,
            "oracle_text":          r.oracle_text,
            "japanese_oracle_text": r.japanese_oracle_text,
            "rarity":               r.rarity,
            "rrf_score":            r.rrf_score,
            "archetypes":           archetypes,
        })
    return cards, fmt


# ─── コンテキスト構築 ─────────────────────────────────────────

def build_context(cards):
    lines = ["【検索で見つかったカード一覧】\n"]
    for i, c in enumerate(cards, 1):
        ja = f"（{c['japanese_name']}）" if c.get("japanese_name") else ""
        lines.append(f"{i}. {c['card_name']}{ja}")
        lines.append(f"   タイプ: {c['type_line']}  コスト: {c['mana_cost'] or 'なし'}")
        if c.get("japanese_oracle_text"):
            lines.append(f"   効果: {c['japanese_oracle_text']}")
        elif c.get("oracle_text"):
            lines.append(f"   効果: {c['oracle_text']}")
        if c.get("archetypes"):
            lines.append(f"   採用デッキ: {', '.join(c['archetypes'])}")
        lines.append("")
    return "\n".join(lines)


# ─── Gemini API 呼び出し ──────────────────────────────────────

def ask_gemini(question, context, api_key):
    """リトライ付き Gemini API 呼び出し"""
    headers = {
        "Content-Type":  "application/json",
        "x-goog-api-key": api_key,
    }

    user_message = f"""{SYSTEM_PROMPT}

以下のカード情報を参考に、質問に答えてください。

{context}

【質問】
{question}"""

    payload = {
        "contents": [
            {"parts": [{"text": user_message}]}
        ],
        "generationConfig": {
            "maxOutputTokens": MAX_TOKENS,
            "temperature": 0.7,
        }
    }

    for attempt in range(RETRY_COUNT):
        try:
            resp = requests.post(
                GEMINI_API_URL, headers=headers, json=payload, timeout=30
            )

            if resp.status_code in (429, 503):
                # Exponential Backoff + ジッター
                wait = min(
                    RETRY_WAIT_BASE * (2 ** attempt) + random.uniform(0, 1),
                    RETRY_WAIT_MAX
                )
                status = "レートリミット" if resp.status_code == 429 else "サービス一時停止"
                print(f"  {status}。{wait:.1f}秒後にリトライ ({attempt+1}/{RETRY_COUNT})...")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]

        except requests.exceptions.HTTPError as e:
            if resp.status_code == 403:
                return "エラー: API キーが無効か権限がありません。"
            if resp.status_code == 400:
                return "エラー: リクエストが不正です。プロンプトを確認してください。"
            if resp.status_code not in (429, 503):
                # URL（APIキー含む可能性）をログに出さない
                return f"エラー: HTTP {resp.status_code}"
        except requests.exceptions.Timeout:
            if attempt < RETRY_COUNT - 1:
                wait = min(
                    RETRY_WAIT_BASE * (2 ** attempt) + random.uniform(0, 1),
                    RETRY_WAIT_MAX
                )
                print(f"  タイムアウト。{wait:.1f}秒後にリトライ ({attempt+1}/{RETRY_COUNT})...")
                time.sleep(wait)
                continue
            return "エラー: タイムアウトが続いています。"
        except Exception as e:
            if attempt < RETRY_COUNT - 1:
                wait = min(
                    RETRY_WAIT_BASE * (2 ** attempt) + random.uniform(0, 1),
                    RETRY_WAIT_MAX
                )
                print(f"  通信エラー。{wait:.1f}秒後にリトライ ({attempt+1}/{RETRY_COUNT})...")
                time.sleep(wait)
                continue
            return "エラー: 予期しないエラーが発生しました。"

    return "エラー: リトライ上限に達しました。"


# ─── 1件処理 ─────────────────────────────────────────────────

def process_question(searcher, question, fmt, top_k, api_key,
                     output_file=None, use_rewrite=True):
    fmt_label = f"[{fmt}]" if fmt else ""
    print(f"\n質問: {fmt_label} {question}")

    # 意図解析 → 検索クエリ抽出 + HyDE + フラグ判定
    search_query     = question
    hyde_text        = ""
    ja_hyde_text     = ""
    tournament_boost = False
    removal_mode     = False
    counter_mode     = False
    type_filter      = None
    router_format    = None
    filters          = {}

    # 辞書で完結するキーワード系クエリは LLM ルーターを呼ばない（直行路の入口版）
    if use_rewrite and structured_direct_gate(question):
        print("  構造化オンリー: LLM ルーターをスキップ（辞書で完結・SQL 直行路へ）")
        use_rewrite = False

    if use_rewrite:
        print("  意図解析中...", end="", flush=True)
        search_query, hyde_text, ja_hyde_text, tournament_boost, removal_mode, counter_mode, type_filter, router_format, filters = rewrite_query(
            question, api_key
        )
        flags = []
        if tournament_boost: flags.append("tournament_boost")
        if removal_mode:     flags.append("removal_mode")
        if counter_mode:     flags.append("counter_mode")
        if type_filter:      flags.append(f"type:{type_filter}")
        if router_format:    flags.append(f"format:{router_format}")
        if filters:          flags.append(str(filters))
        if hyde_text:        flags.append("HyDE")
        flag_str = f" [{', '.join(flags)}]" if flags else " [フラグなし]"
        if search_query != question:
            print(f" 検索クエリ: 「{search_query}」{flag_str}")
        else:
            print(flag_str)

    print("  検索中...", end="", flush=True)
    cards, detected_fmt = search_cards(
        searcher, search_query, top_k, fmt,
        tournament_boost=tournament_boost,
        removal_mode=removal_mode,
        counter_mode=counter_mode,
        type_filter_override=type_filter,
        hyde_text=hyde_text,
        ja_hyde_text=ja_hyde_text,
        filters=filters,
        router_format=router_format,
    )
    if not cards:
        print(" 関連するカードが見つかりませんでした。")
        return

    fmt_info = f"({detected_fmt})" if detected_fmt else ""
    print(f" {len(cards)}件取得{fmt_info}。Gemini に問い合わせ中...", end="", flush=True)

    context = build_context(cards)
    answer  = ask_gemini(question, context, api_key)
    print(" 完了")

    # ファイルに出力
    if output_file:
        # 30文字で折り返す（箇条書き記号・インデントを保持）
        wrapped_lines = []
        for line in answer.splitlines():
            # インデントを検出
            stripped = line.lstrip()
            indent = line[:len(line) - len(stripped)]
            if stripped:
                wrapped = textwrap.fill(
                    stripped,
                    width=30,
                    initial_indent=indent,
                    subsequent_indent=indent + "  ",
                )
                wrapped_lines.append(wrapped)
            else:
                wrapped_lines.append("")
        wrapped_answer = "\n".join(wrapped_lines)

        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'─' * 40}\n")
            f.write(f"質問: {fmt_label} {question}\n")
            f.write(f"{'─' * 40}\n")
            f.write(wrapped_answer)
            f.write(f"\n")
    else:
        print("─" * 60)
        print(answer)
        print("─" * 60)


# ─── メイン ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MTG RAG エージェント（Gemini版）")
    parser.add_argument("--model",         default="SMALL_V2",
                        choices=["SMALL_V2", "BASE_V2"])
    parser.add_argument("--top_k",         type=int, default=5)
    parser.add_argument("--format",        default=None)
    parser.add_argument("--question",      default=None,
                        help="1件だけ質問する")
    parser.add_argument("--question_file", default=None,
                        help="質問ファイルのパス（UTF-8）")
    parser.add_argument("--output", default=None,
                        help="回答の出力ファイルパス（省略時は標準出力）")
    parser.add_argument("--no-rewrite", action="store_true",
                        help="Query Rewriting を無効化する")
    args = parser.parse_args()

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("エラー: GOOGLE_API_KEY 環境変数が設定されていません。")
        print("  export GOOGLE_API_KEY='your-api-key'")
        sys.exit(1)

    print(f"MTG RAG エージェント起動（Gemini {GEMINI_MODEL}）")
    print(f"RAGモデル: {args.model}  top_k: {args.top_k}")

    searcher = MTGHybridSearcherV2(model_key=args.model)

    try:
        # ファイルから質問を読み込む
        if args.question_file:
            questions = load_questions(args.question_file)
            print(f"質問ファイル: {args.question_file}（{len(questions)}件）")
            print("=" * 60)
            # 出力ファイルパスを決定
            output_file = args.output
            if not output_file:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = f"rag_answers_{ts}.txt"
            if output_file:
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(f"MTG RAG エージェント 回答ログ\n")
                    f.write(f"生成日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"モデル: {args.model} / Gemini: {GEMINI_MODEL}\n")
                    f.write(f"質問ファイル: {args.question_file}\n")
                print(f"回答を {output_file} に保存します")
            for i, (q, fmt) in enumerate(questions):
                process_question(searcher, q, fmt or args.format,
                                 args.top_k, api_key, output_file=output_file,
                                 use_rewrite=not args.no_rewrite)
                if i < len(questions) - 1:
                    print("  15秒待機中...")
                    time.sleep(15)
            if output_file:
                print(f"\n完了。回答を {output_file} に保存しました。")

        # 1件だけ質問
        elif args.question:
            output_file = args.output
            print("=" * 60)
            process_question(searcher, args.question, args.format,
                             args.top_k, api_key, output_file=output_file,
                             use_rewrite=not args.no_rewrite)
            if output_file:
                print(f"\n回答を {output_file} に保存しました。")

        # 対話モード（英語推奨）
        else:
            print("対話モード（日本語入力が難しい場合は --question_file を使用）")
            print("終了: 'quit' または Ctrl+C")
            print("=" * 60)
            while True:
                try:
                    question = input("\n質問: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n終了します。")
                    break
                if not question:
                    continue
                if question.lower() in ("quit", "exit"):
                    print("終了します。")
                    break
                process_question(searcher, question, args.format,
                                 args.top_k, api_key,
                                 use_rewrite=not args.no_rewrite)

    finally:
        searcher.close()


if __name__ == "__main__":
    main()
