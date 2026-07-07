#!/usr/bin/env python
"""hammer_router.py — ルーター出力の安定性試験（同一クエリ N 連打）。

目的（2026-07-07・本人依頼）: 同じクエリを N 回叩いて「納得できる JSON を N 回連続で
吐くか」を測る。特に構造化フィールド（ハードフィルタ・フラグ行き）は毎回一致が必須。
temperature の比較（0.1=現行 vs 0=貪欲）で「設定で買える安定性」を先に実測し、
残る揺れ・誤りだけをプロンプト調教の対象にする。

使い方（VM から。Ollama は Windows ホスト 10.0.2.2:11434）:
  python hammer_router.py --query "破壊不能を持つクリーチャー" --n 50 --temps 0.1 0
出力: コンソール要約＋ docs/me/hammer_router_<ts>.json に生データ保存。
"""
import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime

import requests

sys.path.insert(0, '/mnt/mtg_rag')
from ollama_router_test import FABLE_PROMPT, MODEL, OLLAMA_URL

STRUCTURED_KEYS = [
    "type_filter", "format",
    "cmc_min", "cmc_max", "power_min", "power_max",
    "toughness_min", "toughness_max", "mana_producer",
    "tournament_boost", "removal_mode", "counter_mode",
]
TEXT_KEYS = ["search_query", "hyde_text", "ja_hyde_text"]


def call_once(query: str, temp: float, seed: int, timeout=180):
    body = {
        "model": MODEL,
        "messages": [{"role": "user",
                      "content": FABLE_PROMPT.replace("<<QUERY>>", query)}],
        "stream": False,
        "format": "json",
        "options": {"temperature": temp, "seed": seed, "num_predict": 512},
    }
    t0 = time.perf_counter()
    r = requests.post(OLLAMA_URL, json=body, timeout=timeout)
    r.raise_for_status()
    ms = (time.perf_counter() - t0) * 1000
    content = r.json()["message"]["content"]
    content = re.sub(r"^```(?:json)?|```$", "", content.strip()).strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None, content, ms
    return parsed, content, ms


def canon_structured(parsed: dict) -> str:
    return json.dumps({k: parsed.get(k) for k in STRUCTURED_KEYS},
                      sort_keys=True, ensure_ascii=False)


def hammer(query: str, n: int, temp: float, seed: int, seed_vary: bool):
    runs = []
    for i in range(n):
        s = seed + i if seed_vary else seed
        parsed, raw, ms = call_once(query, temp, s)
        runs.append({"i": i, "seed": s, "ok": parsed is not None,
                     "ms": round(ms), "parsed": parsed, "raw": raw})
        print(".", end="", flush=True)
    print()
    ok = [r for r in runs if r["ok"]]
    st = Counter(canon_structured(r["parsed"]) for r in ok)
    tx = Counter(json.dumps({k: r["parsed"].get(k) for k in TEXT_KEYS},
                            sort_keys=True, ensure_ascii=False) for r in ok)
    return runs, st, tx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--temps", type=float, nargs="+", default=[0.1, 0.0])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--seed-vary", action="store_true",
                    help="毎回 seed を変える（本番の『毎回別サンプリング』を模す。"
                         "温度0.1の実運用揺れはこちらが実態に近い）")
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_out = {"meta": {"model": MODEL, "query": args.query, "n": args.n,
                        "seed": args.seed, "seed_vary": args.seed_vary, "ts": ts},
               "conditions": {}}

    for temp in args.temps:
        label = f"temp={temp}" + ("/seed可変" if args.seed_vary else "/seed固定")
        print(f"\n=== {args.query} × {args.n}回 [{label}]")
        runs, st, tx = hammer(args.query, args.n, temp, args.seed, args.seed_vary)
        n_ok = sum(1 for r in runs if r["ok"])
        avg = sum(r["ms"] for r in runs) / len(runs)
        print(f"  JSON parse 成功: {n_ok}/{len(runs)}  平均 {avg:.0f}ms")
        print(f"  構造化フィールドのバリアント数: {len(st)} "
              f"{'← 完全一致' if len(st) == 1 else '← 揺れあり!'}")
        for variant, cnt in st.most_common():
            print(f"    [{cnt}回] {variant[:160]}")
        print(f"  テキスト欄(search/hyde/ja_hyde)のバリアント数: {len(tx)}")
        all_out["conditions"][str(temp)] = {
            "runs": runs, "n_ok": n_ok,
            "structured_variants": {k: v for k, v in st.items()},
            "text_variant_count": len(tx)}

    out_path = f"docs/me/hammer_router_{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_out, f, ensure_ascii=False, indent=1)
    print(f"\n生データ保存: {out_path}")


if __name__ == "__main__":
    main()
