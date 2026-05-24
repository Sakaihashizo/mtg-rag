"""
hybrid_benchmark.py — ハイブリッド検索精度ベンチマーク
========================================================
mtg_hybrid_search_v2.py のハイブリッド検索と
benchmark_models.py の embedding 単体検索を比較する。

使い方:
  python hybrid_benchmark.py
  python hybrid_benchmark.py --output hybrid_bench_v1
  python hybrid_benchmark.py --model SMALL_V2
  python hybrid_benchmark.py --top_k 10
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import defaultdict

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_hybrid_search_v2 import MTGHybridSearcherV2

# ─── 評価クエリ定義 ───────────────────────────────────────────
# (query, format, expected_keywords, known_good_cards, label)
QUERIES = [
    # カウンター系
    (
        "純粋に強いカウンター呪文", None,
        ["counter target spell"],
        ["counterspell", "force of will", "force of negation",
         "spell pierce", "mana drain", "archmage's charm", "spell snare"],
        "counter_pure",
    ),
    (
        "モダンの最強カウンター呪文", "modern",
        ["counter target spell"],
        ["counterspell", "force of negation", "spell pierce",
         "spell snare", "archmage's charm"],
        "counter_modern",
    ),
    # 除去系
    (
        "最強の単体除去", None,
        ["destroy target creature", "exile target creature",
         "damage to any target", "damage to target creature"],
        ["lightning bolt", "fatal push", "swords to plowshares",
         "path to exile", "terminate", "prismatic ending", "solitude"],
        "removal_pure",
    ),
    (
        "スタンダードの単体除去", "standard",
        ["destroy target creature", "exile target creature",
         "damage to any target"],
        ["long goodbye", "shoot the sheriff", "get lost",
         "torch the tower", "sunfall"],
        "removal_standard",
    ),
    # ドロー系
    (
        "カードを2枚引く", None,
        ["draw two cards"],
        ["divination", "brainstorm", "preordain",
         "chart a course", "opt", "ponder"],
        "draw_2",
    ),
    # マナ加速
    (
        "パイオニアのマナ加速", "pioneer",
        ["{t}: add", "add one mana of any color", "add {g}"],
        ["llanowar elves", "birds of paradise", "elvish mystic",
         "sylvan caryatid", "rattleclaw mystic"],
        "ramp_pioneer",
    ),
    # 飛行クリーチャー
    (
        "飛行を持つクリーチャー", None,
        ["flying"],
        ["murktide regent", "subtlety", "vendilion clique",
         "snapcaster mage", "restoration angel"],
        "flying_creature",
    ),
]


# ─── データクラス ─────────────────────────────────────────────

@dataclass
class HybridResult:
    rank: int
    card_name: str
    japanese_name: str
    type_line: str
    oracle_text: str
    rrf_score: float
    vector_rank: Optional[int]
    en_text_rank: Optional[int]
    ja_text_rank: Optional[int]
    keyword_hit: bool = False
    known_good_hit: bool = False


@dataclass
class HybridQueryResult:
    query: str
    format: Optional[str]
    label: str
    model_key: str
    elapsed_ms: float
    results: list[HybridResult] = field(default_factory=list)
    keyword_hit_rate: float = 0.0
    known_good_hit_rate: float = 0.0
    avg_rrf_score: float = 0.0
    avg_rank_known_good: Optional[float] = None


# ─── 評価関数 ─────────────────────────────────────────────────

def evaluate(
    search_results,
    keywords: list[str],
    known_good: list[str],
    query: str,
    fmt: Optional[str],
    label: str,
    model_key: str,
    elapsed_ms: float,
) -> HybridQueryResult:

    results = []
    for i, r in enumerate(search_results):
        oracle = (r.oracle_text or "").lower()
        name   = (r.card_name or "").lower()
        kw_hit = any(kw.lower() in oracle for kw in keywords)
        kg_hit = any(g in name for g in known_good)
        results.append(HybridResult(
            rank=i + 1,
            card_name=r.card_name,
            japanese_name=r.japanese_name or "",
            type_line=r.type_line or "",
            oracle_text=(r.oracle_text or "")[:120],
            rrf_score=r.rrf_score,
            vector_rank=r.vector_rank,
            en_text_rank=r.en_text_rank,
            ja_text_rank=r.ja_text_rank,
            keyword_hit=kw_hit,
            known_good_hit=kg_hit,
        ))

    total    = len(results)
    kw_rate  = sum(r.keyword_hit for r in results) / total if total else 0
    kg_rate  = sum(r.known_good_hit for r in results) / total if total else 0
    avg_rrf  = sum(r.rrf_score for r in results) / total if total else 0
    kg_ranks = [r.rank for r in results if r.known_good_hit]
    avg_kg   = sum(kg_ranks) / len(kg_ranks) if kg_ranks else None

    return HybridQueryResult(
        query=query, format=fmt, label=label, model_key=model_key,
        elapsed_ms=round(elapsed_ms, 1),
        results=results,
        keyword_hit_rate=round(kw_rate, 3),
        known_good_hit_rate=round(kg_rate, 3),
        avg_rrf_score=round(avg_rrf, 4),
        avg_rank_known_good=round(avg_kg, 1) if avg_kg else None,
    )


# ─── 表示関数 ─────────────────────────────────────────────────

def print_result(qr: HybridQueryResult, top_n: int = 5):
    fmt_label = f" [{qr.format}]" if qr.format else ""
    sep = "─" * 70
    print(f"\n{sep}")
    print(f"  [{qr.model_key}] 「{qr.query}」{fmt_label}  ({qr.elapsed_ms}ms)")
    print(f"  KW一致率: {qr.keyword_hit_rate:.1%}  |  "
          f"KG率: {qr.known_good_hit_rate:.1%}  |  "
          f"avg RRF: {qr.avg_rrf_score:.4f}")
    if qr.avg_rank_known_good:
        print(f"  KGカード平均順位: {qr.avg_rank_known_good}")
    print(sep)
    for r in qr.results[:top_n]:
        kw = "✓" if r.keyword_hit else " "
        kg = "★" if r.known_good_hit else " "
        v  = f"v:{r.vector_rank}"   if r.vector_rank   else "    "
        e  = f"e:{r.en_text_rank}"  if r.en_text_rank  else "    "
        j  = f"j:{r.ja_text_rank}"  if r.ja_text_rank  else "    "
        ja = f"（{r.japanese_name}）" if r.japanese_name else ""
        print(f"  [{r.rank:2d}] {kw}{kg} {r.rrf_score:.4f} "
              f"{v} {e} {j}  {r.card_name}{ja}")
        if r.oracle_text:
            print(f"       {r.oracle_text[:80]}")


def print_summary(all_results: list[HybridQueryResult]):
    print("\n" + "═" * 70)
    print("  ■ ハイブリッド検索ベンチマーク サマリー")
    print("═" * 70)
    print(f"  {'クエリ':<30} {'KW率':>8} {'KG率':>8} {'KGランク':>10}")
    print("  " + "─" * 60)
    for qr in all_results:
        fmt = f"[{qr.format}]" if qr.format else ""
        kr  = f"{qr.avg_rank_known_good:.1f}" if qr.avg_rank_known_good else "N/A"
        label = f"{qr.query}{fmt}"[:30]
        print(f"  {label:<30} {qr.keyword_hit_rate:>8.1%} "
              f"{qr.known_good_hit_rate:>8.1%} {kr:>10}")

    total = len(all_results)
    avg_kw = sum(q.keyword_hit_rate for q in all_results) / total
    avg_kg = sum(q.known_good_hit_rate for q in all_results) / total
    print("  " + "─" * 60)
    print(f"  {'平均':<30} {avg_kw:>8.1%} {avg_kg:>8.1%}")
    print("═" * 70)


# ─── メイン ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model",  default="SMALL_V2",
                        choices=["SMALL_V2", "BASE_V2"])
    parser.add_argument("--top_k", type=int, default=10)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    print(f"MTG ハイブリッド検索ベンチマーク")
    print(f"モデル: {args.model}  top_k: {args.top_k}")
    print(f"クエリ数: {len(QUERIES)}")

    searcher    = MTGHybridSearcherV2(model_key=args.model)
    all_results = []

    for query, fmt, keywords, known_good, label in QUERIES:
        t0      = time.perf_counter()
        results = searcher.search(query, top_k=args.top_k, format=fmt)
        elapsed = (time.perf_counter() - t0) * 1000

        qr = evaluate(results, keywords, known_good,
                      query, fmt, label, args.model, elapsed)
        all_results.append(qr)
        print_result(qr, top_n=5)

    print_summary(all_results)

    if args.output:
        out_path = args.output
        if not out_path.endswith(".json"):
            out_path += ".json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in all_results],
                      f, ensure_ascii=False, indent=2)
        print(f"\n結果を {out_path} に保存しました")

    searcher.close()


if __name__ == "__main__":
    main()
