#!/usr/bin/env python3
"""compare_runs.py — eval_runs の 2 つの run のラインナップ（top-10 の顔ぶれと順位）を比較する。

背景（2026-07-26・本人要望）: eval_runs には指標しか残っておらず、
「NDCG は動かないが顔ぶれ／並びは変わった」を検出できなかった。
eval_framework.py が per_query に top10_lineup を保存するようになったので、
その差分を読む道具をここに置く。**top10_lineup を持つ run 同士でしか比較できない**
（2026-07-26 より前の run には入っていない）。

使い方:
    python src/compare_runs.py 111 112
    python src/compare_runs.py 111 112 --out docs/me/rerank_ab_20260726.md
    python src/compare_runs.py 111 112 --only-changed     # 変化したクエリだけ

読み方:
    grade は GT の人手採点（2=ど真ん中 / 1=関連 / 0=的外れ / -=未採点）。
    移動列の ↑n / ↓n は A から B への順位変化。
"""
import argparse
import json
import sys

import psycopg2

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from db_config import DB_CONFIG


def load_run(conn, run_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT id, run_date, model_key, config_json, note "
                    "FROM eval_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
    if not row:
        sys.exit(f"run id={run_id} が見つかりません。")
    _id, run_date, model_key, config, note = row
    if isinstance(config, str):
        config = json.loads(config)
    per_q = {m["query"]: m for m in config.get("per_query", [])}
    if not any("top10_lineup" in m for m in per_q.values()):
        sys.exit(f"run id={run_id} に top10_lineup がありません"
                 "（2026-07-26 の eval_framework 改修より前の run です）。")
    return {"id": _id, "date": run_date, "model": model_key,
            "config": config, "per_query": per_q, "note": note}


def _fmt_grade(g) -> str:
    return "-" if g is None else str(g)


def _label(entry: dict) -> str:
    return f"{entry['card']} [{_fmt_grade(entry.get('grade'))}]"


def compare_query(qa: dict, qb: dict) -> dict:
    """1 クエリ分の差分を組み立てる。"""
    la = qa.get("top10_lineup") or []
    lb = qb.get("top10_lineup") or []
    rank_a = {e["card"]: e["rank"] for e in la}
    rank_b = {e["card"]: e["rank"] for e in lb}
    set_a, set_b = set(rank_a), set(rank_b)

    rows = []
    for i in range(max(len(la), len(lb))):
        ea = la[i] if i < len(la) else None
        eb = lb[i] if i < len(lb) else None
        move = ""
        if eb:
            card = eb["card"]
            if card not in set_a:
                move = "NEW"
            else:
                d = rank_a[card] - eb["rank"]      # 正なら上昇
                move = "=" if d == 0 else (f"↑{d}" if d > 0 else f"↓{-d}")
        rows.append({
            "rank": i + 1,
            "a": _label(ea) if ea else "",
            "b": _label(eb) if eb else "",
            "move": move,
        })

    return {
        "rows": rows,
        "entered": sorted(set_b - set_a),          # B にだけ居る＝新規流入
        "dropped": sorted(set_a - set_b),          # A にだけ居た＝脱落
        "order_changed": [e["card"] for e in la] != [e["card"] for e in lb],
        "ndcg_a": qa.get("ndcg_10"), "ndcg_b": qb.get("ndcg_10"),
        "p5_a": qa.get("precision_5"), "p5_b": qb.get("precision_5"),
        "reranked_b": qb.get("reranked"),
    }


def render(run_a: dict, run_b: dict, only_changed: bool) -> str:
    ca, cb = run_a["config"], run_b["config"]
    out = []
    add = out.append

    add(f"# ラインナップ比較: id={run_a['id']} → id={run_b['id']}\n")
    for tag, r, c in (("A", run_a, ca), ("B", run_b, cb)):
        add(f"- **{tag} = id={r['id']}**（{r['date']}）"
            f" rerank={c.get('rerank')} scope={c.get('rerank_scope')}"
            f" 経路={c.get('route')} model={r['model']}")
        if r["note"]:
            add(f"  - note: {r['note']}")
    add("")
    add(f"- 看板（正準・床除外）: **{ca.get('avg_ndcg_10_canonical'):.4f}"
        f" → {cb.get('avg_ndcg_10_canonical'):.4f}"
        f"（{cb.get('avg_ndcg_10_canonical') - ca.get('avg_ndcg_10_canonical'):+.4f}）**")
    add("")

    queries = [q for q in run_a["per_query"] if q in run_b["per_query"]]
    diffs = {q: compare_query(run_a["per_query"][q], run_b["per_query"][q])
             for q in queries}

    n_order = sum(1 for d in diffs.values() if d["order_changed"])
    n_member = sum(1 for d in diffs.values() if d["entered"] or d["dropped"])
    add(f"- 比較クエリ数 **{len(queries)}** / 並びが変わった **{n_order}** / "
        f"顔ぶれ（集合）が変わった **{n_member}**\n")

    # NDCG の変化が大きい順
    ordered = sorted(queries,
                     key=lambda q: -abs((diffs[q]["ndcg_b"] or 0)
                                        - (diffs[q]["ndcg_a"] or 0)))
    for q in ordered:
        d = diffs[q]
        changed = d["order_changed"] or d["entered"] or d["dropped"]
        if only_changed and not changed:
            continue
        dn = (d["ndcg_b"] or 0) - (d["ndcg_a"] or 0)
        flag = " ※rerank 適用" if d["reranked_b"] else " ※rerank 非適用"
        add(f"## 「{q}」  NDCG {d['ndcg_a']:.3f} → {d['ndcg_b']:.3f} "
            f"({dn:+.3f})  p@5 {d['p5_a']:.2f} → {d['p5_b']:.2f}{flag}")
        if not changed:
            add("\n並びも顔ぶれも変化なし。\n")
            continue
        if d["entered"]:
            add(f"\n- **流入**: {', '.join(d['entered'])}")
        if d["dropped"]:
            add(f"- **脱落**: {', '.join(d['dropped'])}")
        add("")
        add("| # | A | B | 移動 |")
        add("|---:|---|---|---|")
        for r in d["rows"]:
            add(f"| {r['rank']} | {r['a']} | {r['b']} | {r['move']} |")
        add("")

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_a", type=int, help="比較元の run id")
    ap.add_argument("run_b", type=int, help="比較先の run id")
    ap.add_argument("--out", default=None, help="Markdown の保存先")
    ap.add_argument("--only-changed", action="store_true",
                    help="並び／顔ぶれが変わったクエリだけ出す")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        a = load_run(conn, args.run_a)
        b = load_run(conn, args.run_b)
    finally:
        conn.close()

    md = render(a, b, args.only_changed)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print(f"書き出しました: {args.out}")
    print(md)


if __name__ == "__main__":
    main()
