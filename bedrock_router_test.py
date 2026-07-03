"""
bedrock_router_test.py — Bedrock を LLM ルーターとして試験し、Gemini キャッシュと突き合わせる
================================================================================
目的: ルーター役を Gemini 2.5 Flash-Lite（無料枠・日次クォータあり）から
Bedrock の小型モデル（Nova Micro 想定・従量課金・クォータ実質なし・serverless）へ
置き換えられるかの品質・コスト・レイテンシ実測。

公平性の担保:
  - プロンプトは本番と同一（mtg_rag_agent.REWRITE_PROMPT をそのまま使用）
  - JSON 検証も rewrite_query と同一ロジック（型検証・ホワイトリスト・範囲チェック）
  - 比較対象は eval_router_cache.json（Gemini の30クエリ分・prompt_sha 一致を確認）
  ※ Gemini 出力は「参照」であって正解ではない。不一致は良し悪しを目視判断する。

認証: .env（gitignore 済み）の AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
AWS_DEFAULT_REGION を読む（キーは表示しない）。

使い方:
  PYTHONPATH=/home/claude/pylibs python bedrock_router_test.py --check   # 接続確認のみ（1クエリ）
  PYTHONPATH=/home/claude/pylibs python bedrock_router_test.py           # 30クエリ比較
  PYTHONPATH=/home/claude/pylibs python bedrock_router_test.py --model us.amazon.nova-lite-v1:0
"""

import argparse
import json
import os
import re
import sys
import time
import hashlib
from datetime import datetime

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_rag_agent import REWRITE_PROMPT, detect_format, FORMAT_KEYWORDS

CACHE_PATH = "/mnt/mtg_rag/eval_router_cache.json"
ENV_PATH = "/mnt/mtg_rag/.env"
OUT_DIR = "/mnt/mtg_rag/docs/me"

# 1M トークンあたり USD（2026-02〜04 調査・us-east-1）
PRICING = {
    "us.amazon.nova-micro-v1:0": (0.035, 0.14),
    "us.amazon.nova-lite-v1:0":  (0.06, 0.24),
    "us.amazon.nova-pro-v1:0":   (0.80, 3.20),
}

JA_RE = re.compile(r'[ぁ-んァ-ン一-龥]')


def load_env():
    """`.env` から AWS_* を環境変数へ（値は表示しない）。"""
    if not os.path.exists(ENV_PATH):
        return []
    loaded = []
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line.startswith("AWS_") and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            loaded.append(k.strip())
    return loaded


def parse_and_validate(raw_text: str, query: str):
    """rewrite_query と同一の検証ロジック（Gemini 版と揃える）。"""
    raw_text = raw_text.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw_text)
    entry = {
        "search_query": parsed.get("search_query", query),
        "hyde_text": parsed.get("hyde_text", ""),
        "ja_hyde_text": parsed.get("ja_hyde_text", ""),
        "tournament_boost": bool(parsed.get("tournament_boost", False)),
        "removal_mode": bool(parsed.get("removal_mode", False)),
        "counter_mode": bool(parsed.get("counter_mode", False)),
        "type_filter": parsed.get("type_filter", None),
    }
    fmt_raw = parsed.get("format")
    fmt = (fmt_raw.lower()
           if isinstance(fmt_raw, str) and fmt_raw.lower() in set(FORMAT_KEYWORDS.values())
           else None)
    if fmt is None:
        fmt = detect_format(query)
    entry["format"] = fmt

    def _vint(key):
        try:
            n = int(parsed.get(key))
        except (ValueError, TypeError):
            return None
        return n if 0 <= n <= 99 else None
    filters = {}
    for k in ("cmc_min", "cmc_max", "power_min", "power_max",
              "toughness_min", "toughness_max"):
        v = _vint(k)
        if v is not None:
            filters[k] = v
    if bool(parsed.get("mana_producer", False)):
        filters["mana_producer"] = True
    entry["filters"] = filters
    return entry


def call_bedrock(client, model_id: str, query: str, max_tokens: int = 512):
    t0 = time.time()
    resp = client.converse(
        modelId=model_id,
        messages=[{"role": "user",
                   "content": [{"text": REWRITE_PROMPT.format(query=query)}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.1},
    )
    ms = (time.time() - t0) * 1000
    text = resp["output"]["message"]["content"][0]["text"]
    usage = resp.get("usage", {})
    return text, usage.get("inputTokens", 0), usage.get("outputTokens", 0), ms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="us.amazon.nova-micro-v1:0")
    ap.add_argument("--check", action="store_true", help="接続確認のみ（1クエリ）")
    args = ap.parse_args()

    loaded = load_env()
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        print("エラー: AWS 認証が見つからない。.env に AWS_ACCESS_KEY_ID /"
              " AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION を追記してください。")
        sys.exit(1)
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    print(f".env から読み込み: {loaded} / region={region} / model={args.model}")

    import boto3
    client = boto3.client("bedrock-runtime", region_name=region)

    cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    entries = cache["entries"]
    prompt_sha = hashlib.sha256(REWRITE_PROMPT.encode("utf-8")).hexdigest()[:12]
    sha_ok = cache["meta"].get("prompt_sha") == prompt_sha
    print(f"Gemini キャッシュ: {len(entries)} 件 / prompt_sha 一致: {sha_ok}")
    if not sha_ok:
        print("警告: プロンプトがキャッシュ生成時と異なる＝比較の前提が崩れてる。中断。")
        sys.exit(1)

    queries = list(entries.keys())
    if args.check:
        queries = queries[:1]

    results = {}
    tot_in = tot_out = 0
    lats = []
    for i, q in enumerate(queries, 1):
        try:
            text, tin, tout, ms = call_bedrock(client, args.model, q)
            entry = parse_and_validate(text, q)
            err = None
        except Exception as e:
            entry, err = None, f"{type(e).__name__}: {e}"
            tin = tout = 0
            ms = 0.0
        tot_in += tin
        tot_out += tout
        if ms:
            lats.append(ms)
        results[q] = {"bedrock": entry, "error": err,
                      "tokens": [tin, tout], "latency_ms": round(ms)}
        mark = "OK" if err is None else f"失敗: {err[:90]}"
        print(f"  [{i}/{len(queries)}] {q[:24]} → {mark} ({ms:.0f}ms)")
        time.sleep(0.2)

    if args.check:
        print("\n--check 完了。本走は --check を外して実行。")
        return

    # ── Gemini キャッシュとの突き合わせ ──
    STRUCT = ["tournament_boost", "removal_mode", "counter_mode",
              "type_filter", "format", "filters"]
    agree = {k: 0 for k in STRUCT}
    sq_same = hyde_ok = ja_ok = n_ok = 0
    diffs = []
    for q in queries:
        r = results[q]
        if r["error"]:
            continue
        n_ok += 1
        b, g = r["bedrock"], entries[q]
        row_diff = []
        for k in STRUCT:
            if b[k] == g.get(k) or (k == "filters" and b[k] == (g.get(k) or {})):
                agree[k] += 1
            else:
                row_diff.append(f"{k}: gemini={g.get(k)!r} bedrock={b[k]!r}")
        if b["search_query"] == g.get("search_query"):
            sq_same += 1
        else:
            row_diff.append(f"search_query: gemini={g.get('search_query')!r} "
                            f"bedrock={b['search_query']!r}")
        if b["hyde_text"].strip():
            hyde_ok += 1
        if b["ja_hyde_text"].strip() and JA_RE.search(b["ja_hyde_text"]):
            ja_ok += 1
        if row_diff:
            diffs.append((q, row_diff))

    pin, pout = PRICING.get(args.model, (None, None))
    cost = (tot_in / 1e6 * pin + tot_out / 1e6 * pout) if pin else None
    lat_avg = sum(lats) / len(lats) if lats else 0
    lat_p95 = sorted(lats)[int(len(lats) * 0.95) - 1] if lats else 0

    print("\n" + "=" * 60)
    print(f"  モデル: {args.model} / 成功 {n_ok}/{len(queries)}")
    for k in STRUCT:
        print(f"  {k:>18}: {agree[k]}/{n_ok} 一致")
    print(f"  {'search_query':>18}: {sq_same}/{n_ok} 完全一致（不一致=言い換え・要目視）")
    print(f"  {'hyde_en 非空':>18}: {hyde_ok}/{n_ok}")
    print(f"  {'ja_hyde 日本語':>18}: {ja_ok}/{n_ok}")
    print(f"  レイテンシ: 平均 {lat_avg:.0f}ms / p95 {lat_p95:.0f}ms")
    print(f"  トークン: in {tot_in:,} / out {tot_out:,}")
    if cost is not None:
        print(f"  実コスト: ${cost:.6f}（30クエリ）→ 1クエリ ${cost/max(n_ok,1):.6f}")
    print("=" * 60)

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out = {"meta": {"model": args.model, "region": region, "ts": ts,
                    "prompt_sha": prompt_sha,
                    "tokens_in": tot_in, "tokens_out": tot_out,
                    "cost_usd": cost, "lat_avg_ms": round(lat_avg),
                    "lat_p95_ms": round(lat_p95)},
           "agreement": {**agree, "n_ok": n_ok, "search_query_exact": sq_same,
                         "hyde_en_nonempty": hyde_ok, "ja_hyde_japanese": ja_ok},
           "results": results}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = f"{OUT_DIR}/bedrock_router_test_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n生出力を保存: {path}")
    if diffs:
        print(f"\n不一致 {len(diffs)} クエリ（詳細は JSON）:")
        for q, d in diffs:
            print(f"  [{q}]")
            for line in d:
                print(f"    {line}")


if __name__ == "__main__":
    main()
