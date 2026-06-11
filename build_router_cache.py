"""
build_router_cache.py — 評価用のルーター出力キャッシュを生成する（要 GOOGLE_API_KEY）
====================================================================================
評価をルーター/エージェント経路に通すとき、評価のたびに Gemini を呼ぶと出力が
揺れて A/B 比較の前提条件が固定できない（レート制限・コストも乗る）。
そこで eval_queries.json の全クエリについて rewrite_query の出力
（search_query / hyde_text / フラグ / filters）を一度だけ取得して
eval_router_cache.json に保存し、評価はこのキャッシュを読んで決定的に回す。

待機時間の制限と途中再開:
  - リトライ待機は 8秒 → 16秒 の2回まで（1クエリの待機は合計24秒が上限）
  - 3クエリ連続で失敗したら中断する（キー無効・強いレート制限に延々付き合わない）
  - 1クエリ取得するごとに保存するので、中断しても進捗は残る。再実行すると
    取得済みクエリをスキップして続きから取得する（再開モード）

REWRITE_PROMPT を変更したらこのキャッシュは陳腐化する（meta.prompt_sha で検知し、
再実行時に全件取り直しになる）。

使い方:
    export GOOGLE_API_KEY='your-api-key'
    python build_router_cache.py [eval_queries.json のパス]
"""
import sys
import os
import json
import time
import hashlib
from datetime import datetime

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_rag_agent import rewrite_query, detect_format, REWRITE_PROMPT, GEMINI_MODEL

QUERIES_JSON     = sys.argv[1] if len(sys.argv) > 1 else "eval_queries.json"
OUT_PATH         = "eval_router_cache.json"
SLEEP_SEC        = 8        # クエリ間の待機（無料枠レート制限よけ・約7リクエスト/分）
RETRY_WAITS      = (8, 16)  # リトライ前の待機。これ以上は粘らない
MAX_CONSEC_FAIL  = 3        # 連続失敗がこの数に達したら中断


def save_cache(entries: dict, prompt_sha: str):
    data = {
        "meta": {
            "created_at":   datetime.now().isoformat(timespec="seconds"),
            "gemini_model": GEMINI_MODEL,
            "prompt_sha":   prompt_sha,
            "queries_json": QUERIES_JSON,
            "n_queries":    len(entries),
        },
        "entries": entries,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("エラー: GOOGLE_API_KEY 未設定。export してから実行してください。")
        sys.exit(1)

    with open(QUERIES_JSON, encoding="utf-8") as f:
        queries = json.load(f)
    prompt_sha = hashlib.sha256(REWRITE_PROMPT.encode("utf-8")).hexdigest()[:12]

    # 既存キャッシュがあれば再開（プロンプトが同一の場合のみ。suspect は取り直す）
    entries = {}
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            old = json.load(f)
        if old.get("meta", {}).get("prompt_sha") == prompt_sha:
            entries = {q: e for q, e in old.get("entries", {}).items()
                       if not e.get("suspect")}
            if entries:
                print(f"再開モード: 既存キャッシュから {len(entries)} 件を再利用")
        else:
            print("REWRITE_PROMPT が変更されているため全件取り直します")

    todo = [q for q in queries if q["query"] not in entries]
    print(f"{len(queries)} クエリ中 {len(todo)} 件を取得します"
          f"（{SLEEP_SEC}秒間隔・正常時 約{max(len(todo) * SLEEP_SEC // 60, 1)}分）")

    suspects = []
    consec_fail = 0
    fetched = 0
    for i, q in enumerate(queries, 1):
        query = q["query"]
        if query in entries:
            print(f"  [{i}/{len(queries)}] 「{query[:24]}」→ キャッシュ済み・スキップ")
            continue
        if fetched > 0:
            time.sleep(SLEEP_SEC)

        # raise_on_error=True で実際のエラー内容（429 等）を見えるようにする。
        # 正常時は hyde_text が必ず生成されるため、空＝失敗として扱う。
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                sq, hyde, tb, rm, cm, tf, rfmt, filters = rewrite_query(
                    query, api_key, raise_on_error=True)
            except Exception as err:
                detail = f"{type(err).__name__}: {err}"
                resp = getattr(err, "response", None)
                if resp is not None:
                    detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    # 日次クォータ枯渇はリトライしても無駄なので即中断
                    if resp.status_code == 429 and "PerDay" in resp.text:
                        save_cache(entries, prompt_sha)
                        print(f"  [{i}] {detail}")
                        print("\n日次クォータ枯渇のため中断します。進捗は保存済み。"
                              "クォータ回復後に再実行すると続きから取得します。")
                        sys.exit(3)
                print(f"  [{i}] エラー: {detail}")
                sq, hyde, tb, rm, cm, tf, rfmt, filters = (
                    query, "", False, False, False, None,
                    detect_format(query), {})
            if hyde:
                break
            if attempt < len(RETRY_WAITS):
                wait = RETRY_WAITS[attempt]
                print(f"  [{i}] {wait}秒後にリトライ "
                      f"({attempt + 1}/{len(RETRY_WAITS)})...")
                time.sleep(wait)
        fetched += 1

        entry = {
            "search_query":     sq,
            "hyde_text":        hyde,
            "tournament_boost": tb,
            "removal_mode":     rm,
            "counter_mode":     cm,
            "type_filter":      tf,
            "format":           rfmt,
            "filters":          filters,
        }
        if not hyde:
            entry["suspect"] = True
            suspects.append(query)
            consec_fail += 1
        else:
            consec_fail = 0
        entries[query] = entry
        save_cache(entries, prompt_sha)   # 1件ごとに保存（中断しても進捗が残る）

        flag_str = " ".join(k for k, v in
                            (("tb", tb), ("removal", rm), ("counter", cm)) if v)
        mark = " ※失敗" if not hyde else ""
        print(f"  [{i}/{len(queries)}] 「{query[:24]}」→ filters={filters or '{}'} "
              f"type={tf} format={rfmt} flags={flag_str or 'なし'}{mark}")

        if consec_fail >= MAX_CONSEC_FAIL:
            print(f"\n{MAX_CONSEC_FAIL}クエリ連続で失敗したため中断します。"
                  "キー無効か強いレート制限の可能性が高いです。")
            print(f"進捗は {OUT_PATH} に保存済み。原因解消後に再実行すると"
                  "続きから取得します。")
            sys.exit(2)

    print(f"\n保存しました: {OUT_PATH}（{len(entries)} 件）")
    if suspects:
        print(f"注意: 素通しのまま保存したクエリが {len(suspects)} 件あります:")
        for s in suspects:
            print(f"  - {s}")
        print("再実行するとこの分だけ取り直します（成功分はスキップされます）。")
    else:
        print("全クエリ正常。eval_framework.py --run --router-cache "
              f"{OUT_PATH} で評価できます。")


if __name__ == "__main__":
    main()
