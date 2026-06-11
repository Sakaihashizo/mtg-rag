"""
eval_framework.py — MTG RAG 評価フレームワーク v2
==================================================
設計方針:
  - ラベル付けは CSV をExcel等で編集する（コンソール作業しない）
  - DBに残すのは eval_runs（実行ログ）のみ
  - eval_queries / eval_pool / eval_groundtruth はCSVで管理

ファイル構成:
  eval_queries.json       クエリセット定義
  eval_pool_{date}.csv    候補カード一覧（human_rankは空欄で出力）
  eval_groundtruth.csv    編集済みGT（human_rankを手入力したもの）

使い方:
  # 候補CSV出力（Excelで human_rank を埋める）
  python eval_framework.py --pool

  # 編集済みCSVを読んで指標計算 → eval_runs に保存
  python eval_framework.py --run --gt eval_groundtruth.csv --note "baseline"

  # ルーター/エージェント経路で評価（要: build_router_cache.py で生成したキャッシュ）
  python eval_framework.py --run --router-cache eval_router_cache.json --note "router baseline"

  # ルーター経路の候補プール出力（GT 未ラベルの新カードを洗い出してラベル拡張する用）
  python eval_framework.py --pool --router-cache eval_router_cache.json

  # 実行結果一覧
  python eval_framework.py --show
"""

import argparse
import csv
import json
import math
import sys
import time
from datetime import datetime

import psycopg2

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_hybrid_search_v2 import MTGHybridSearcherV2

from db_config import DB_CONFIG

QUERIES_JSON   = "eval_queries.json"
TOP_K          = 10


# ─── eval_runs テーブル作成 ───────────────────────────────────

SETUP_SQL = """
CREATE TABLE IF NOT EXISTS eval_runs (
    id           SERIAL PRIMARY KEY,
    run_date     TIMESTAMP DEFAULT NOW(),
    model_key    TEXT NOT NULL,
    config_json  JSONB,
    query_count  INTEGER,
    gt_count     INTEGER,
    recall_5     FLOAT,
    recall_10    FLOAT,
    precision_5  FLOAT,
    precision_10 FLOAT,
    mrr          FLOAT,
    ndcg_10      FLOAT,
    note         TEXT
);
"""

def setup(conn):
    with conn.cursor() as cur:
        cur.execute(SETUP_SQL)
    conn.commit()
    print("eval_runs テーブル作成完了")


# ─── Vintage リーガルチェック ─────────────────────────────────

def is_vintage_legal(conn, card_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT legalities->>'vintage'
            FROM mtg_cards_v2 WHERE card_name = %s
        """, (card_name,))
        row = cur.fetchone()
    if not row or not row[0]:
        return False
    return row[0] in ("legal", "restricted")


def search_legal(searcher, conn, query, fmt, top_k: int, router_entry: dict = None):
    """ハイブリッド検索結果から Vintage リーガルなカードを上位 top_k 件返す。

    pool 収集（ラベル付け候補）と run 評価で **同じ集合・同じ順序** を使うための
    共通処理。これを介さないと「pool ではリーガル除外したのに run では生の検索結果で
    評価する」という不整合（ラベルと評価対象のズレ）が起きる。
    フィルタ後に top_k 件を確保できるよう内部では多めに取得する。

    router_entry が与えられた場合はルーター/エージェント経路を再現する:
    キャッシュ済みのルーター出力（search_query / hyde_text / フラグ / filters）で
    mtg_rag_agent.search_cards と同じ呼び方をする。評価中に LLM は呼ばない
    （毎回 Gemini を呼ぶと出力が揺れて A/B 比較にならないため、キャッシュで固定する）。
    なお format はキャッシュの抽出値ではなく GT 側の値（引数 fmt）を使う。
    GT ラベルはその format 前提で付けられており、両経路を同条件で比較するため。

    返り値: (legal_results, skipped_count)
    """
    fetch_k = top_k * 2
    if router_entry is None:
        results = searcher.search(query, top_k=fetch_k, format=fmt)
    else:
        e = router_entry
        kwargs = dict(
            top_k=fetch_k, format=fmt,
            tournament_boost_override=bool(e.get("tournament_boost")),
            removal_mode_override=bool(e.get("removal_mode")),
            counter_mode_override=bool(e.get("counter_mode")),
            type_filter_override=e.get("type_filter"),
            **(e.get("filters") or {}),
        )
        sq   = e.get("search_query") or query
        hyde = e.get("hyde_text") or ""
        if hyde:
            results = searcher.search_with_hyde(query=sq, hyde_text=hyde, **kwargs)
        else:
            results = searcher.search(sq, **kwargs)
    legal = []
    skipped = 0
    for r in results:
        if len(legal) >= top_k:
            break
        if not is_vintage_legal(conn, r.card_name):
            skipped += 1
            continue
        legal.append(r)
    return legal, skipped


def load_router_cache(path: str) -> dict:
    """build_router_cache.py が出力したルーター出力キャッシュを読み込む。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("meta", {})
    print(f"ルーターキャッシュ: {path}")
    print(f"  生成: {meta.get('created_at')} / Gemini: {meta.get('gemini_model')}"
          f" / prompt_sha: {meta.get('prompt_sha')} / {len(data.get('entries', {}))} クエリ")
    return data


# ─── 候補CSV出力 ──────────────────────────────────────────────

def collect_pool(conn, model_key: str = "SMALL_V2", top_k: int = TOP_K,
                 queries_json: str = QUERIES_JSON, router_cache: dict = None):
    """
    全クエリに対してハイブリッド検索を実行し、
    候補カード一覧をCSVに出力する。human_rank は空欄。
    Vintage でリーガルでないカードを除外した後に top_k 件になるよう多めに取得する。
    router_cache 指定時はルーター経路で収集する（GT 未ラベルの新カードを洗い出す用）。
    """
    with open(queries_json, "r", encoding="utf-8") as f:
        queries = json.load(f)

    entries = (router_cache or {}).get("entries", {})
    if router_cache is not None:
        missing = [q["query"] for q in queries if q["query"] not in entries]
        if missing:
            print(f"エラー: ルーターキャッシュに無いクエリが {len(missing)} 件: {missing}")
            print("build_router_cache.py を再実行してください（経路の混在は不可）。")
            return

    date_str  = datetime.now().strftime("%Y%m%d_%H%M")
    suffix    = "_routed" if router_cache is not None else ""
    out_path  = f"eval_pool_{date_str}{suffix}.csv"

    # フィルタ後に top_k 件確保できるよう多めに取得（最大2倍）
    fetch_k = top_k * 2

    route_label = "ルーター経路（キャッシュ）" if router_cache is not None else "searcher 直呼び"
    print(f"候補プール収集: {len(queries)} クエリ × top_{top_k}（内部取得: {fetch_k}件）")
    print(f"モデル: {model_key}  経路: {route_label}  Vintage非リーガル除外後に{top_k}件に絞る")
    searcher  = MTGHybridSearcherV2(model_key=model_key)

    rows = []
    for q in queries:
        query  = q["query"]
        fmt    = q.get("format")
        cat    = q["category"]
        entry  = entries[query] if router_cache is not None else None
        legal, skipped = search_legal(searcher, conn, query, fmt, top_k,
                                      router_entry=entry)

        for system_rank, r in enumerate(legal, start=1):
            # 日本語テキスト全文をDBから取得
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT japanese_oracle_text, oracle_text
                    FROM mtg_cards_v2 WHERE card_name = %s
                """, (r.card_name,))
                db_row = cur.fetchone()
            ja_text = db_row[0] if db_row and db_row[0] else ""
            en_text = db_row[1] if db_row and db_row[1] else ""

            rows.append({
                "query":       query,
                "format":      fmt or "",
                "category":    cat,
                "system_rank": system_rank,
                "card_name":   r.card_name,
                "japanese_name": r.japanese_name or "",
                "type_line":   r.type_line or "",
                "japanese_oracle_text": ja_text,
                "oracle_text": en_text,
                "human_rank":  "",   # ← Excelで記入
                "note":        "",   # ← 任意
            })

        skip_str = f"  (Vintage非リーガル除外: {skipped}件)" if skipped else ""
        print(f"  「{query}」→ {len(legal)} 件{skip_str}")

    searcher.close()

    fieldnames = [
        "query", "format", "category",
        "system_rank", "card_name", "japanese_name", "type_line",
        "japanese_oracle_text", "oracle_text",
        "human_rank", "note",
    ]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nCSV出力完了: {out_path}  ({len(rows)} 件)")
    print("Excel で human_rank 列を記入してください。")
    print("  0 = 的外れ / 1〜10 = 良い順の相対順位（候補集合内）")
    return out_path


# ─── 指標計算 ─────────────────────────────────────────────────

def dcg(grades: list, k: int) -> float:
    result = 0.0
    for i, g in enumerate(grades[:k]):
        result += (2 ** g - 1) / math.log2(i + 2)
    return result

def ndcg(grades_in_order: list, ideal_grades: list, k: int) -> float:
    # ideal DCG は「GT 全体の grade」を理想順に並べて計算する。
    # システムが取得できたカードだけで ideal を作ると、最良カードの取りこぼしが
    # NDCG に反映されず過大評価になるため、ideal_grades を別引数で受け取る。
    ideal_dcg = dcg(sorted(ideal_grades, reverse=True), k)
    if ideal_dcg == 0:
        return 0.0
    return dcg(grades_in_order, k) / ideal_dcg

def compute_metrics(system_results: list, gt: dict) -> dict:
    """
    system_results: [(card_name, system_rank), ...] system_rank順
    gt: {card_name: human_rank}  human_rank=0は無関連
    """
    relevant = {name for name, hr in gt.items() if hr > 0}
    n_relevant = len(relevant)

    # recall@k / precision@k
    metrics = {}
    for k in [5, 10]:
        top_k_names = [r[0] for r in system_results[:k]]
        hits = sum(1 for name in top_k_names if name in relevant)
        metrics[f"recall_{k}"]    = hits / n_relevant if n_relevant > 0 else 0.0
        metrics[f"precision_{k}"] = hits / k

    # MRR
    mrr = 0.0
    for i, (name, _) in enumerate(system_results):
        if name in relevant:
            mrr = 1.0 / (i + 1)
            break
    metrics["mrr"] = mrr

    # NDCG@10: human_rank を relevance grade に変換して使用。
    # human_rank は 1 が最良（小さいほど良い順位）なので、max から引いて反転し
    # 「grade が大きいほど良い」形にする（rank1 -> 最大 grade、0/未記入 -> 0）。
    max_hr = max((hr for hr in gt.values() if hr > 0), default=1)
    grades_in_order = []
    for name, _ in system_results[:10]:
        hr = gt.get(name, 0)
        grade = (max_hr - hr + 1) if hr > 0 else 0
        grades_in_order.append(grade)

    # ideal は GT 全体の grade（システムが取得できなかった良カードも含む）を基準にする
    ideal_grades = [(max_hr - hr + 1) for hr in gt.values() if hr > 0]
    metrics["ndcg_10"] = ndcg(grades_in_order, ideal_grades, 10)

    return metrics


# ─── 評価実行 ─────────────────────────────────────────────────

def run_eval(conn, gt_path: str, model_key: str, note: str = "",
             router_cache: dict = None, allow_partial: bool = False):
    """
    編集済みGT CSVを読んで指標を計算し、eval_runs に保存する。
    router_cache 指定時はルーター/エージェント経路で検索する（キャッシュ利用・決定的）。
    経路は config_json に記録される。searcher 直呼びの数値と混ぜて比較しないこと。
    allow_partial=True のときだけ、キャッシュ未取得のクエリを除外して部分評価できる
    （クォータ等でキャッシュが未完成な場合の速報用。除外リストは config_json に記録。
    部分評価の数値は n が違うため、全クエリの run と直接比較しないこと）。
    """
    # GT CSV を読み込む
    gt_by_query: dict[str, dict] = {}   # query → {card_name: human_rank}
    fmt_by_query: dict[str, str] = {}

    with open(gt_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            query = row["query"]
            hr_str = row["human_rank"].strip()
            if not hr_str:
                continue   # human_rank 未記入はスキップ
            try:
                hr = int(hr_str)
            except ValueError:
                continue
            if query not in gt_by_query:
                gt_by_query[query] = {}
                fmt_by_query[query] = row.get("format", "") or None
            gt_by_query[query][row["card_name"]] = hr

    if not gt_by_query:
        print("GT に human_rank が記入されていません。")
        return

    entries = (router_cache or {}).get("entries", {})
    partial_missing: list = []
    if router_cache is not None:
        missing = [q for q in gt_by_query if q not in entries]
        if missing and not allow_partial:
            print(f"エラー: ルーターキャッシュに無いクエリが {len(missing)} 件: {missing}")
            print("build_router_cache.py を再実行するか、--partial で部分評価してください"
                  "（経路の混在は不可）。")
            return
        if missing:
            partial_missing = missing
            for q in missing:
                gt_by_query.pop(q)
            print(f"部分評価モード: キャッシュ未取得の {len(missing)} クエリを除外"
                  f" → n={len(gt_by_query)}")
            for q in missing:
                print(f"  除外: {q}")

    route_label = "ルーター経路（キャッシュ）" if router_cache is not None else "searcher 直呼び"
    print(f"評価実行: {len(gt_by_query)} クエリ / モデル: {model_key} / 経路: {route_label}")
    searcher = MTGHybridSearcherV2(model_key=model_key)

    all_metrics = []
    for query, gt in gt_by_query.items():
        fmt = fmt_by_query.get(query)
        entry = entries[query] if router_cache is not None else None
        # pool 収集と同じ Vintage リーガルフィルタを通す（ラベルと評価対象を揃える）
        legal, _ = search_legal(searcher, conn, query, fmt, TOP_K,
                                router_entry=entry)
        system_results = [(r.card_name, i + 1) for i, r in enumerate(legal)]
        m = compute_metrics(system_results, gt)
        # GT に存在しないカード（ラベル付けプール外）の混入率。grade 0 扱いになるため、
        # これが高いクエリは「悪い」のではなく「未採点」の可能性がある（ラベル拡張の目印）。
        top10 = [name for name, _ in system_results[:10]]
        m["unlabeled_10"] = (sum(1 for name in top10 if name not in gt)
                             / max(len(top10), 1))
        m["query"] = query
        all_metrics.append(m)
        unl = f"  未ラベル={m['unlabeled_10']:.0%}" if m["unlabeled_10"] > 0 else ""
        print(
            f"  「{query[:28]}」"
            f"  R@5={m['recall_5']:.2f} P@5={m['precision_5']:.2f}"
            f"  MRR={m['mrr']:.2f} NDCG={m['ndcg_10']:.2f}{unl}"
        )

    searcher.close()

    n = len(all_metrics)
    avg = {k: sum(m[k] for m in all_metrics) / n
           for k in ["recall_5","recall_10","precision_5","precision_10","mrr","ndcg_10"]}
    gt_count = sum(len(gt) for gt in gt_by_query.values())

    avg_unlabeled = sum(m["unlabeled_10"] for m in all_metrics) / n

    config = {
        "model_key": model_key,
        "top_k": TOP_K,
        "gt_path": gt_path,
        "run_date": datetime.now().isoformat(),
        # 検索経路。searcher 直呼びとルーター経由の数値は条件が違うので比較しない
        "route": "router" if router_cache is not None else "searcher",
        "avg_unlabeled_10": avg_unlabeled,
        "per_query": all_metrics,
    }
    if router_cache is not None:
        config["router_meta"] = router_cache.get("meta", {})
    if partial_missing:
        config["partial_missing"] = partial_missing

    setup(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO eval_runs
                (model_key, config_json, query_count, gt_count,
                 recall_5, recall_10, precision_5, precision_10,
                 mrr, ndcg_10, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            model_key,
            json.dumps(config, ensure_ascii=False),
            n, gt_count,
            avg["recall_5"], avg["recall_10"],
            avg["precision_5"], avg["precision_10"],
            avg["mrr"], avg["ndcg_10"],
            note or None,
        ))
        run_id = cur.fetchone()[0]
    conn.commit()

    print("\n" + "=" * 60)
    print(f"  実行ID: {run_id}  モデル: {model_key}  クエリ数: {n}")
    print(f"  recall@5:     {avg['recall_5']:.3f}")
    print(f"  recall@10:    {avg['recall_10']:.3f}")
    print(f"  precision@5:  {avg['precision_5']:.3f}")
    print(f"  precision@10: {avg['precision_10']:.3f}")
    print(f"  MRR:          {avg['mrr']:.3f}")
    print(f"  NDCG@10:      {avg['ndcg_10']:.3f}")
    print(f"  経路: {'router' if router_cache is not None else 'searcher'}"
          f"  平均未ラベル混入率(top10): {avg_unlabeled:.1%}")
    print("=" * 60)
    print(f"eval_runs に保存しました（id={run_id}）")
    if router_cache is not None and avg_unlabeled > 0.2:
        print("注意: 未ラベル混入率が高めです。ルーター経路の結果に GT 未採点カードが")
        print("多く含まれており、指標が実態より低く出ている可能性があります。")
        print("--pool --router-cache で候補を出力し、ラベル拡張を検討してください。")


# ─── 結果表示 ─────────────────────────────────────────────────

def show_runs(conn):
    setup(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, run_date, model_key, query_count, gt_count,
                   recall_5, recall_10, precision_5, precision_10,
                   mrr, ndcg_10, note
            FROM eval_runs ORDER BY run_date DESC LIMIT 10
        """)
        rows = cur.fetchall()

    if not rows:
        print("eval_runs にデータがありません。")
        return

    print(f"\n{'ID':>4} {'日時':<18} {'モデル':<10} {'Q':>4} {'R@5':>6} {'R@10':>6} {'P@5':>6} {'MRR':>6} {'NDCG':>6}  備考")
    print("-" * 80)
    for row in rows:
        rid, rd, model, qc, gc, r5, r10, p5, p10, mrr, ndcg, note = row
        ds = rd.strftime("%Y-%m-%d %H:%M") if rd else "-"
        print(f"{rid:>4} {ds:<18} {model:<10} {qc:>4}"
              f" {r5:>6.3f} {r10:>6.3f} {p5:>6.3f} {mrr:>6.3f} {ndcg:>6.3f}  {note or ''}")


# ─── エントリーポイント ───────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MTG RAG 評価フレームワーク v2")
    parser.add_argument("--pool",   action="store_true",
                        help="候補CSV出力（Excelで human_rank を記入する）")
    parser.add_argument("--run",    action="store_true",
                        help="編集済みGT CSVを読んで指標計算・eval_runsに保存")
    parser.add_argument("--show",   action="store_true",
                        help="実行結果一覧")
    parser.add_argument("--model",  default="SMALL_V2",
                        choices=["SMALL_V2", "BASE_V2"])
    parser.add_argument("--gt",     default="eval_groundtruth.csv",
                        help="編集済みGT CSVのパス（--run 時に使用）")
    parser.add_argument("--queries_json", default=QUERIES_JSON)
    parser.add_argument("--top_k",  type=int, default=TOP_K)
    parser.add_argument("--note",   default="", help="実行結果のメモ")
    parser.add_argument("--router-cache", default=None, dest="router_cache",
                        help="build_router_cache.py が出力したキャッシュJSON。"
                             "指定するとルーター/エージェント経路で検索する")
    parser.add_argument("--partial", action="store_true",
                        help="キャッシュ未取得のクエリを除外して部分評価する"
                             "（キャッシュ未完成時の速報用）")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)

    router_cache = load_router_cache(args.router_cache) if args.router_cache else None

    if args.pool:
        collect_pool(conn, model_key=args.model, top_k=args.top_k,
                     queries_json=args.queries_json, router_cache=router_cache)
    elif args.run:
        run_eval(conn, gt_path=args.gt, model_key=args.model, note=args.note,
                 router_cache=router_cache, allow_partial=args.partial)
    elif args.show:
        show_runs(conn)
    else:
        parser.print_help()

    conn.close()


if __name__ == "__main__":
    main()
