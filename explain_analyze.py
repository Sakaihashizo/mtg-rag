"""
explain_analyze.py — MTG ハイブリッド検索のパフォーマンス分析
=============================================================
EXPLAIN ANALYZE を使って各検索系の実行計画を分析する。
インデックスあり/なしの比較も可能。

使い方:
  python explain_analyze.py                        # 全検索系を分析
  python explain_analyze.py --query "counter spell" # 特定クエリで分析
  python explain_analyze.py --no-index             # インデックス無効化して比較
  python explain_analyze.py --output explain_result # テキストファイルに保存
"""

import argparse
import datetime
import sys
import time
import psycopg2
import psycopg2.extras
from sentence_transformers import SentenceTransformer

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_hybrid_search_v2 import (
    get_db_config, MODEL_REGISTRY, REMOVAL_TSQUERY, expand_query
)

# ─── 設定 ─────────────────────────────────────────────────────

DEFAULT_QUERY = "counter target spell"
DEFAULT_MODEL = "SMALL_V2"


# ─── ベクトル検索の EXPLAIN ANALYZE ──────────────────────────

def explain_vector_search(conn, model, cfg, query: str,
                          top_k: int = 10, use_index: bool = True) -> str:
    vec = model.encode(f"query: {query}", normalize_embeddings=True)
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"

    if not use_index:
        conn.cursor().execute("SET enable_indexscan = OFF;")
        conn.cursor().execute("SET enable_bitmapscan = OFF;")

    sql = f"""
        EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
        SELECT
            c.card_name,
            1 - (e.embedding <=> '{vec_str}'::vector) AS similarity
        FROM {cfg['embeddings_table']} e
        JOIN {cfg['cards_table']} c ON e.card_id = c.id
        ORDER BY e.embedding <=> '{vec_str}'::vector
        LIMIT {top_k};
    """

    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    if not use_index:
        conn.cursor().execute("SET enable_indexscan = ON;")
        conn.cursor().execute("SET enable_bitmapscan = ON;")

    return "\n".join(row[0] for row in rows)


# ─── 英語 FTS の EXPLAIN ANALYZE ─────────────────────────────

def explain_en_fts(conn, cfg, query: str,
                   top_k: int = 10, removal_mode: bool = False) -> str:
    if removal_mode:
        tsquery = REMOVAL_TSQUERY
        sql = f"""
            EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
            SELECT c.card_name,
                ts_rank(
                    to_tsvector('english', COALESCE(c.oracle_text, '')),
                    to_tsquery('english', %s)
                ) AS score
            FROM {cfg['cards_table']} c
            WHERE to_tsvector('english', COALESCE(c.oracle_text, ''))
                  @@ to_tsquery('english', %s)
            ORDER BY score DESC
            LIMIT {top_k};
        """
        with conn.cursor() as cur:
            cur.execute(sql, (tsquery, tsquery))
            rows = cur.fetchall()
    else:
        primary = query.replace("'", "''")
        sql = f"""
            EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
            SELECT c.card_name,
                ts_rank(
                    to_tsvector('english', COALESCE(c.oracle_text, '')),
                    plainto_tsquery('english', '{primary}')
                ) AS score
            FROM {cfg['cards_table']} c
            WHERE to_tsvector('english', COALESCE(c.oracle_text, ''))
                  @@ plainto_tsquery('english', '{primary}')
            ORDER BY score DESC
            LIMIT {top_k};
        """
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()

    return "\n".join(row[0] for row in rows)


# ─── 日本語 LIKE の EXPLAIN ANALYZE ──────────────────────────

def explain_ja_fts(conn, cfg, keyword: str, top_k: int = 10) -> str:
    sql = f"""
        EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
        SELECT c.card_name, c.tournament_score
        FROM {cfg['cards_table']} c
        WHERE c.japanese_oracle_text IS NOT NULL
          AND c.japanese_oracle_text LIKE %s
        ORDER BY c.tournament_score DESC
        LIMIT {top_k};
    """
    with conn.cursor() as cur:
        cur.execute(sql, (f"%{keyword}%",))
        rows = cur.fetchall()

    return "\n".join(row[0] for row in rows)


# ─── 実行時間抽出 ────────────────────────────────────────────

def extract_execution_time(explain_output: str) -> str:
    for line in explain_output.splitlines():
        if "Execution Time" in line or "Planning Time" in line:
            return line.strip()
    return "時間情報なし"


# ─── メイン ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query",    default=DEFAULT_QUERY)
    parser.add_argument("--model",    default=DEFAULT_MODEL,
                        choices=list(MODEL_REGISTRY.keys()))
    parser.add_argument("--top_k",   type=int, default=10)
    parser.add_argument("--no-index", action="store_true",
                        help="インデックスを無効化して比較")
    parser.add_argument("--output",   default=None,
                        help="結果をテキストファイルに保存")
    args = parser.parse_args()

    cfg   = MODEL_REGISTRY[args.model]
    conn  = psycopg2.connect(**get_db_config())
    model = SentenceTransformer(
        cfg["model_name"], cache_folder="/mnt/new_hdd/hf_cache"
    )

    ts      = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines   = []
    divider = "=" * 70

    def log(text: str = ""):
        print(text)
        lines.append(text)

    log(divider)
    log(f"MTG ハイブリッド検索 EXPLAIN ANALYZE")
    log(f"日時: {ts}")
    log(f"モデル: {args.model} ({cfg['model_name']})")
    log(f"クエリ: {args.query}")
    log(f"top_k: {args.top_k}")
    log(divider)

    # ── ベクトル検索（インデックスあり）
    log("\n【1】ベクトル検索 (HNSW インデックスあり)")
    log("-" * 70)
    t0 = time.perf_counter()
    result = explain_vector_search(conn, model, cfg, args.query,
                                   args.top_k, use_index=True)
    elapsed = (time.perf_counter() - t0) * 1000
    log(result)
    log(f"\n→ 実際の実行時間（Python計測）: {elapsed:.1f}ms")

    # ── ベクトル検索（インデックスなし）
    if args.no_index:
        log("\n【2】ベクトル検索 (インデックスなし / シーケンシャルスキャン)")
        log("-" * 70)
        t0 = time.perf_counter()
        result = explain_vector_search(conn, model, cfg, args.query,
                                       args.top_k, use_index=False)
        elapsed = (time.perf_counter() - t0) * 1000
        log(result)
        log(f"\n→ 実際の実行時間（Python計測）: {elapsed:.1f}ms")

    # ── 英語 FTS
    log("\n【3】英語 FTS")
    log("-" * 70)
    t0 = time.perf_counter()
    result = explain_en_fts(conn, cfg, args.query, args.top_k)
    elapsed = (time.perf_counter() - t0) * 1000
    log(result)
    log(f"\n→ 実際の実行時間（Python計測）: {elapsed:.1f}ms")

    # ── 英語 FTS（除去モード）
    log("\n【4】英語 FTS（removal_mode / REMOVAL_TSQUERY）")
    log("-" * 70)
    t0 = time.perf_counter()
    result = explain_en_fts(conn, cfg, args.query, args.top_k,
                            removal_mode=True)
    elapsed = (time.perf_counter() - t0) * 1000
    log(result)
    log(f"\n→ 実際の実行時間（Python計測）: {elapsed:.1f}ms")

    # ── 日本語 LIKE 検索
    log("\n【5】日本語 LIKE 検索")
    log("-" * 70)
    ja_keyword = "打ち消す"
    log(f"キーワード: {ja_keyword}")
    t0 = time.perf_counter()
    result = explain_ja_fts(conn, cfg, ja_keyword, args.top_k)
    elapsed = (time.perf_counter() - t0) * 1000
    log(result)
    log(f"\n→ 実際の実行時間（Python計測）: {elapsed:.1f}ms")

    log(divider)

    # ── ファイル出力
    if args.output:
        out_path = args.output
        if not out_path.endswith(".txt"):
            out_path += ".txt"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n結果を保存: {out_path}")

    conn.close()


if __name__ == "__main__":
    main()
