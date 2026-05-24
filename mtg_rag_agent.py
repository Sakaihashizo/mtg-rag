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
import sys
import os
import time
import random
import datetime
import textwrap
import requests

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_hybrid_search_v2 import MTGHybridSearcherV2

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
- なぜそのカードが質問に適しているか理由も添える
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


# ─── RAG 検索 ─────────────────────────────────────────────────

def search_cards(searcher, query, top_k, fmt):
    # クエリからフォーマット自動検出（引数 fmt が None の場合）
    if fmt is None:
        for kw, f in FORMAT_KEYWORDS.items():
            if kw in query:
                fmt = f
                break

    results = searcher.search(query, top_k=top_k, format=fmt)
    cards = []
    for r in results:
        cards.append({
            "card_name":            r.card_name,
            "japanese_name":        r.japanese_name,
            "type_line":            r.type_line,
            "mana_cost":            r.mana_cost,
            "oracle_text":          r.oracle_text,
            "japanese_oracle_text": r.japanese_oracle_text,
            "rarity":               r.rarity,
            "rrf_score":            r.rrf_score,
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
            if resp.status_code not in (429, 503):
                return f"エラー: {e}"
        except Exception as e:
            if attempt < RETRY_COUNT - 1:
                wait = min(
                    RETRY_WAIT_BASE * (2 ** attempt) + random.uniform(0, 1),
                    RETRY_WAIT_MAX
                )
                print(f"  通信エラー。{wait:.1f}秒後にリトライ ({attempt+1}/{RETRY_COUNT})...")
                time.sleep(wait)
                continue
            return f"エラー: {e}"

    return "エラー: リトライ上限に達しました。"


# ─── 1件処理 ─────────────────────────────────────────────────

def process_question(searcher, question, fmt, top_k, api_key,
                     output_file=None):
    fmt_label = f"[{fmt}]" if fmt else ""
    print(f"\n質問: {fmt_label} {question}")
    print("  検索中...", end="", flush=True)

    cards, detected_fmt = search_cards(searcher, question, top_k, fmt)
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
        if not output_file and args.question_file:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"rag_answers_{ts}.txt"

        if output_file:
            # ヘッダーを書く
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(f"MTG RAG エージェント 回答ログ\n")
                f.write(f"生成日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"モデル: {args.model} / Gemini: {GEMINI_MODEL}\n")
                f.write(f"質問ファイル: {args.question_file}\n")
            print(f"回答を {output_file} に保存します")

        for i, (q, fmt) in enumerate(questions):
                process_question(searcher, q, fmt or args.format,
                                 args.top_k, api_key, output_file=output_file)
                if i < len(questions) - 1:
                    print("  8秒待機中...")
                    time.sleep(8)

        if output_file:
            print(f"\n完了。回答を {output_file} に保存しました。")

        # 1件だけ質問
        elif args.question:
            output_file = args.output
            print("=" * 60)
            process_question(searcher, args.question, args.format,
                             args.top_k, api_key, output_file=output_file)
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
                                 args.top_k, api_key)

    finally:
        searcher.close()


if __name__ == "__main__":
    main()
