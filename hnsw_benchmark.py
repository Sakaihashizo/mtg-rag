"""
hnsw_benchmark.py — HNSW パラメータのベンチマーク
====================================================
ef_search と m の値を変えて精度と速度を比較する。

使い方:
  python hnsw_benchmark.py                    # ef_search のみ検証
  python hnsw_benchmark.py --rebuild_m        # m=32 でインデックス再構築して比較
  python hnsw_benchmark.py --output hnsw_result
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field

import psycopg2

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_hybrid_search_v2 import MTGHybridSearcherV2, DB_CONFIG_PRIMARY

# ─── 評価クエリ（hybrid_benchmark.py と同じ known_good）──────

QUERIES = [
    {
        "query": "counter target spell",
        "known_good": ["counterspell", "force of will", "force of negation",
                       "spell pierce", "mana drain", "spell snare"],
        "label": "counter",
    },
    {
        "query": "destroy target creature",
        "known_good": ["lightning bolt", "fatal push", "swords to plowshares",
                       "path to exile", "terminate", "prismatic ending"],
        "label": "removal",
    },
    {
        "query": "draw two cards",
        "known_good": ["brainstorm", "ponder", "preordain",
                       "divination", "opt"],
        "label": "draw",
    },
    {
        "query": "flying creature",
        "known_good": ["murktide regent", "subtlety", "vendilion clique",
                       "restoration angel"],
        "label": "flying",
    },
    {
        "query": "add mana ramp",
        "known_good": ["llanowar elves", "birds of paradise", "sol ring",
                       "arcane signet", "cultivate"],
        "label": "ramp",
    },
]

TOP_K = 10
EF_SEARCH_VALUES = [10, 20, 40, 100, 200, 500]


# ─── ベクトル検索のみで recall を測定 ────────────────────────

def vector_search_only(conn, cfg, query_vec: list[float],
                       top_k: int, ef_search: int) -> list[str]:
    """ef_search を指定してベクトル検索を実行し、カード名リストを返す"""
    vec_str = "[" + ",".join(f"{v:.8f}" for v in query_vec) + "]"
    table   = cfg["embeddings_table"]
    cards_t = cfg["cards_table"]

    with conn.cursor() as cur:
        cur.execute(f"SET hnsw.ef_search = {ef_search};")
        cur.execute(f"""
            SELECT c.card_name
            FROM {table} e
            JOIN {cards_t} c ON e.card_id = c.id
            ORDER BY e.embedding <=> '{vec_str}'::vector
            LIMIT {top_k};
        """)
        return [row[0].lower() for row in cur.fetchall()]


def calc_recall(results: list[str], known_good: list[str]) -> float:
    """known_good のうち results に含まれる割合"""
    if not known_good:
        return 0.0
    hits = sum(1 for kg in known_good if any(kg in r for r in results))
    return hits / len(known_good)


# ─── ef_search ベンチマーク ───────────────────────────────────

def run_ef_search_benchmark(searcher, repeat: int = 3) -> list[dict]:
    cfg  = searcher.cfg
    conn = searcher.conn
    results = []

    for ef in EF_SEARCH_VALUES:
        recalls = []
        times   = []

        for query_info in QUERIES:
            query_vec = searcher._embed(query_info["query"])
            known_good = query_info["known_good"]

            # repeat 回実行して平均を取る
            for _ in range(repeat):
                t0 = time.perf_counter()
                card_names = vector_search_only(conn, cfg, query_vec,
                                                TOP_K, ef)
                elapsed = (time.perf_counter() - t0) * 1000
                times.append(elapsed)
                recalls.append(calc_recall(card_names, known_good))

        avg_recall = sum(recalls) / len(recalls)
        avg_ms     = sum(times) / len(times)
        p95_ms     = sorted(times)[int(len(times) * 0.95)]

        results.append({
            "ef_search":  ef,
            "avg_recall": round(avg_recall, 3),
            "avg_ms":     round(avg_ms, 1),
            "p95_ms":     round(p95_ms, 1),
        })
        print(f"  ef_search={ef:4d}: recall={avg_recall:.1%}  "
              f"avg={avg_ms:.1f}ms  p95={p95_ms:.1f}ms")

    return results


# ─── インデックスサイズ確認 ───────────────────────────────────

def check_index_size(conn, table: str) -> str:
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT pg_size_pretty(pg_relation_size(
                (SELECT indexrelid FROM pg_index
                 JOIN pg_class ON pg_class.oid = pg_index.indrelid
                 WHERE pg_class.relname = '{table}'
                 LIMIT 1)
            ));
        """)
        # HNSW インデックスのサイズを取得
        cur.execute(f"""
            SELECT indexname, pg_size_pretty(pg_relation_size(indexname::regclass))
            FROM pg_indexes
            WHERE tablename = '{table}'
              AND indexname LIKE '%hnsw%';
        """)
        rows = cur.fetchall()
        return ", ".join(f"{r[0]}: {r[1]}" for r in rows)


# ─── メイン ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model",  default="SMALL_V2",
                        choices=["SMALL_V2", "BASE_V2"])
    parser.add_argument("--repeat", type=int, default=3,
                        help="各クエリの繰り返し回数（平均を取るため）")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    print(f"HNSW ef_search ベンチマーク")
    print(f"モデル: {args.model}  top_k: {TOP_K}  repeat: {args.repeat}")

    searcher = MTGHybridSearcherV2(model_key=args.model)
    conn     = searcher.conn

    # インデックス情報を表示
    idx_size = check_index_size(conn, searcher.cfg["embeddings_table"])
    print(f"インデックス: {idx_size}")

    with conn.cursor() as cur:
        cur.execute("SHOW hnsw.ef_search;")
        current_ef = cur.fetchone()[0]
    print(f"現在の ef_search: {current_ef}")
    print(f"\nef_search の影響を検証中...")
    print(f"{'ef_search':>10} {'recall':>8} {'avg(ms)':>10} {'p95(ms)':>10}")
    print("-" * 45)

    ef_results = run_ef_search_benchmark(searcher, repeat=args.repeat)

    print("\n" + "=" * 50)
    print("サマリー:")
    print(f"{'ef_search':>10} {'recall':>8} {'avg(ms)':>10} {'p95(ms)':>10}")
    print("-" * 45)
    for r in ef_results:
        print(f"{r['ef_search']:>10} {r['avg_recall']:>8.1%} "
              f"{r['avg_ms']:>10.1f} {r['p95_ms']:>10.1f}")

    baseline = ef_results[0]
    print(f"\n基準（ef_search={baseline['ef_search']}）との比較:")
    for r in ef_results[1:]:
        recall_diff = r['avg_recall'] - baseline['avg_recall']
        speed_diff  = r['avg_ms'] - baseline['avg_ms']
        print(f"  ef_search={r['ef_search']:4d}: "
              f"recall {recall_diff:+.1%}  速度 {speed_diff:+.1f}ms")

    if args.output:
        out = {
            "model":      args.model,
            "top_k":      TOP_K,
            "ef_results": ef_results,
        }
        path = args.output if args.output.endswith(".json") else args.output + ".json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n結果を {path} に保存しました")

    searcher.close()


if __name__ == "__main__":
    main()
