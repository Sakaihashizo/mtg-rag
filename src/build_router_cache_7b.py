#!/usr/bin/env python3
"""ローカル 7B（Ollama/qwen2.5）でルーターキャッシュを作る（$0 版ビルダー）。

build_router_cache.py の Gemini 版と同じキャッシュ形式（entries の鍵は
eval_framework の router_entry が読むもの）を、mtg_rag_agent.rewrite_query_ollama
（FABLE_PROMPT 調教版・temp0/seed42・検証層 _parse_router_json は Gemini と共通）で
生成する。用途: EDH 便など新クエリ族の初期キャッシュを課金ゼロで用意する
（2026-07-23 本人指示「とりあえず7Bでやってみてよ」）。eval=Gemini の正準運用は
不変——7B キャッシュは別ファイルに書き、正準キャッシュを汚さない。

使い方:
  python build_router_cache_7b.py --queries eval_queries_edh.json \
      --out eval_router_cache_edh_7b.json
Ollama は Windows ホスト側・常時起動でない＝走行前に生死確認する（内蔵済み）。
"""
import argparse
import datetime
import json
import sys

import requests

sys.path.insert(0, '/mnt/mtg_rag/src')
from mtg_rag_agent import rewrite_query_ollama


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--queries', default='eval_queries_edh.json')
    ap.add_argument('--out', default='eval_router_cache_edh_7b.json')
    args = ap.parse_args()

    from ollama_router_test import OLLAMA_URL, MODEL
    base = OLLAMA_URL.rsplit('/api/', 1)[0]
    try:
        requests.get(base + '/api/tags', timeout=5)
    except Exception as e:
        sys.exit(f"Ollama が起きてない（{base}）: {e} — Windows 側で起動してから再実行")

    with open(args.queries) as f:
        queries = json.load(f)

    entries = {}
    for i, q in enumerate(queries, 1):
        query = q['query']
        print(f"[{i}/{len(queries)}] {query}")
        (sq, hyde, ja_hyde, tb, rm, cm,
         type_filter, fmt, filters) = rewrite_query_ollama(query, raise_on_error=True)
        entries[query] = {
            'search_query': sq, 'hyde_text': hyde, 'ja_hyde_text': ja_hyde,
            'tournament_boost': tb, 'removal_mode': rm, 'counter_mode': cm,
            'type_filter': type_filter, 'format': fmt, 'filters': filters,
        }
        print(f"    sq={sq!r} type={type_filter} fmt={fmt} "
              f"tb={tb} rm={rm} cm={cm} filters={filters}")

    out = {
        'meta': {
            'backend': f'ollama/{MODEL}', 'temperature': 0, 'seed': 42,
            'generated': datetime.date.today().isoformat(),
            'queries_file': args.queries,
            'note': '7B 調教版 FABLE_PROMPT・検証層は Gemini と共通（_parse_router_json）',
        },
        'entries': entries,
    }
    with open(args.out, 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n書き出し: {args.out}（{len(entries)} 件・$0）")


if __name__ == '__main__':
    main()
