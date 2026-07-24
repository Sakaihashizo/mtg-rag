"""
MTG RAG System — モデル精度比較スクリプト
=========================================
SMALL (384d) / BASE (768d) の2モデルを
複数クエリで公平に比較し、結果をターミナルと JSON に出力します。

使い方:
  source /mnt/new_hdd/my_rag_env/bin/activate
  python benchmark_models.py [--top_k 10] [--output results.json]
"""

import argparse
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import psycopg2
from sentence_transformers import SentenceTransformer

# ─── 接続設定 ────────────────────────────────────────────────
# 読み取り専用のため、reembed 中はフラグファイルで Standby へ自動切替する。
from db_config import get_db_config

# ─── モデル定義 ───────────────────────────────────────────────
@dataclass
class ModelConfig:
    key: str
    model_name: str
    dim: int
    cards_table: str
    embeddings_table: str
    instruct: bool = False
    instruct_prefix: str = ""

MODELS = [
    ModelConfig(
        key="SMALL",
        model_name="intfloat/multilingual-e5-small",
        dim=384,
        cards_table="mtg_cards_v2",
        embeddings_table="mtg_embeddings_small_v2",
    ),
    ModelConfig(
        key="BASE",
        model_name="intfloat/multilingual-e5-base",
        dim=768,
        cards_table="mtg_cards_v2",
        embeddings_table="mtg_embeddings_base_v2",
    ),
]

# ─── 評価クエリ ───────────────────────────────────────────────
# (query_text, expected_keywords_in_oracle_text, label)
QUERIES = [
    # カウンター系
    ("純粋に強いカウンター呪文",
     ["counter target spell"],
     "counter_spell_pure"),
    ("カウンター呪文が強いカード",
     ["counter target spell", "counter target noncreature"],
     "counter_synergy"),
    ("Counterspell",
     ["counter target spell"],
     "counterspell_en"),
    ("counter target spell",
     ["counter target spell"],
     "counter_en"),
    # ドロー系
    ("カードを2枚引く",
     ["draw two cards"],
     "draw_2"),
    ("手札補充できる青いカード",
     ["draw two cards", "draw three cards", "draw a card"],
     "draw_blue"),
    # 除去系（"add mana" や無関係ワードを除外するため厳密に）
    ("最強の単体除去",
     ["destroy target creature", "exile target creature",
      "damage to any target", "damage to target creature"],
     "removal"),
    ("クリーチャーを破壊する",
     ["destroy target creature"],
     "destroy_creature"),
    # ランプ系（"mana abilities" 等を除外するため厳密に）
    ("マナ加速できるカード",
     ["{t}: add", "add one mana of any color",
      "add {g}", "add {c}{c}", "search your library for a land"],
     "ramp"),
    # コンボ向け
    ("無限コンボに使えるカード",
     ["whenever", "untap"],
     "combo"),
]

# ─── MTG カード評価指標 ───────────────────────────────────────
# known_good はクエリに対して明らかに正解とされるカード名（小文字）
KNOWN_GOOD: dict[str, list[str]] = {
    "counter_spell_pure": [
        "counterspell", "mana drain", "force of will",
        "force of negation", "cryptic command", "spell pierce",
        "archmage's charm", "negate", "spell snare",
    ],
    "counter_synergy": [
        "snapcaster mage", "mystical tutor",
        "baral, chief of compliance", "mana leak",
        "remand", "cryptic command",
    ],
    "counterspell_en": ["counterspell"],
    "counter_en":      ["counterspell", "force of will", "mana drain"],
    "draw_2": [
        "divination", "concentrate", "brainstorm",
        "preordain", "ponder", "opt",
    ],
    "draw_blue": [
        "brainstorm", "ponder", "preordain",
        "divination", "jace's ingenuity",
    ],
    "removal": [
        "lightning bolt", "fatal push", "swords to plowshares",
        "path to exile", "terminate", "murder",
        "prismatic ending", "solitude",
    ],
    "destroy_creature": [
        "fatal push", "murder", "terminate",
        "doom blade", "go for the throat",
    ],
    "ramp": [
        "llanowar elves", "birds of paradise", "sol ring",
        "arcane signet", "cultivate", "kodama's reach",
        "farseek", "rampant growth", "elvish mystic",
    ],
    "combo": [
        "splinter twin", "exarch", "pestermite",
        "kiki-jiki, mirror breaker",
    ],
}

# ─── データクラス ─────────────────────────────────────────────
@dataclass
class SearchResult:
    rank: int
    card_name: str
    type_line: str
    oracle_text: str
    similarity: float
    keyword_hit: bool = False
    known_good_hit: bool = False

@dataclass
class QueryResult:
    query: str
    label: str
    model_key: str
    elapsed_ms: float
    results: list[SearchResult] = field(default_factory=list)
    keyword_hit_rate: float = 0.0
    known_good_hit_rate: float = 0.0
    avg_similarity: float = 0.0
    avg_rank_known_good: Optional[float] = None

# ─── 検索関数 ─────────────────────────────────────────────────
def embed_query(model: SentenceTransformer, cfg: ModelConfig, query: str) -> list[float]:
    text = f"{cfg.instruct_prefix}{query}" if cfg.instruct else f"query: {query}"
    vec = model.encode(text, normalize_embeddings=True)
    return vec.tolist()

def search(conn, cfg: ModelConfig, query_vec: list[float], top_k: int) -> list[dict]:
    vec_str = "[" + ",".join(f"{v:.8f}" for v in query_vec) + "]"
    sql = f"""
        SELECT
            c.card_name,
            c.type_line,
            c.oracle_text,
            1 - (e.embedding <=> '{vec_str}'::vector) AS similarity
        FROM {cfg.embeddings_table} e
        JOIN {cfg.cards_table} c ON e.card_id = c.id
        ORDER BY e.embedding <=> '{vec_str}'::vector
        LIMIT {top_k};
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

# ─── 評価関数 ─────────────────────────────────────────────────
def evaluate_results(
    rows: list[dict],
    keywords: list[str],
    label: str,
    query: str,
    model_key: str,
    elapsed_ms: float,
) -> QueryResult:
    results = []
    good_names = [n.lower() for n in KNOWN_GOOD.get(label, [])]

    for i, row in enumerate(rows):
        oracle = (row["oracle_text"] or "").lower()
        name   = (row["card_name"] or "").lower()
        kw_hit = any(kw.lower() in oracle for kw in keywords)
        kg_hit = any(g in name for g in good_names)
        results.append(SearchResult(
            rank=i + 1,
            card_name=row["card_name"],
            type_line=row["type_line"] or "",
            oracle_text=(row["oracle_text"] or "")[:120],
            similarity=round(float(row["similarity"]), 4),
            keyword_hit=kw_hit,
            known_good_hit=kg_hit,
        ))

    total = len(results)
    kw_rate   = sum(r.keyword_hit for r in results) / total if total else 0
    kg_rate   = sum(r.known_good_hit for r in results) / total if total else 0
    avg_sim   = sum(r.similarity for r in results) / total if total else 0
    kg_ranks  = [r.rank for r in results if r.known_good_hit]
    avg_kg_rank = sum(kg_ranks) / len(kg_ranks) if kg_ranks else None

    return QueryResult(
        query=query, label=label, model_key=model_key,
        elapsed_ms=round(elapsed_ms, 1),
        results=results,
        keyword_hit_rate=round(kw_rate, 3),
        known_good_hit_rate=round(kg_rate, 3),
        avg_similarity=round(avg_sim, 4),
        avg_rank_known_good=round(avg_kg_rank, 1) if avg_kg_rank else None,
    )

# ─── 表示関数 ─────────────────────────────────────────────────
def print_result(qr: QueryResult, top_n: int = 5):
    sep = "─" * 70
    print(f"\n{sep}")
    print(f"  モデル: {qr.model_key}  |  クエリ: 「{qr.query}」  ({qr.elapsed_ms}ms)")
    print(f"  KW一致率: {qr.keyword_hit_rate:.1%}  |  "
          f"既知良カード率: {qr.known_good_hit_rate:.1%}  |  "
          f"平均類似度: {qr.avg_similarity:.4f}")
    if qr.avg_rank_known_good:
        print(f"  既知良カード平均順位: {qr.avg_rank_known_good}")
    print(sep)
    for r in qr.results[:top_n]:
        kw  = "✓" if r.keyword_hit else " "
        kg  = "★" if r.known_good_hit else " "
        print(f"  [{r.rank:2d}] {kw}{kg} {r.similarity:.4f}  {r.card_name:<30} {r.type_line[:25]}")
        if r.oracle_text:
            print(f"       {r.oracle_text[:80]}...")

def print_summary(all_results: list[QueryResult]):
    print("\n" + "═" * 70)
    print("  ■ 総合サマリー")
    print("═" * 70)
    print(f"  {'モデル':<8} {'KW一致率':>10} {'KG率':>8} {'平均類似度':>12} {'平均KGランク':>12}")
    print("  " + "─" * 60)

    from collections import defaultdict
    stats: dict[str, list] = defaultdict(list)
    for qr in all_results:
        stats[qr.model_key].append(qr)

    for key in ["SMALL", "BASE"]:
        qs = stats.get(key, [])
        if not qs:
            continue
        avg_kw  = sum(q.keyword_hit_rate for q in qs) / len(qs)
        avg_kg  = sum(q.known_good_hit_rate for q in qs) / len(qs)
        avg_sim = sum(q.avg_similarity for q in qs) / len(qs)
        kg_ranks = [q.avg_rank_known_good for q in qs if q.avg_rank_known_good]
        avg_kr  = sum(kg_ranks) / len(kg_ranks) if kg_ranks else None
        kr_str  = f"{avg_kr:.1f}" if avg_kr else "N/A"
        print(f"  {key:<8} {avg_kw:>10.1%} {avg_kg:>8.1%} {avg_sim:>12.4f} {kr_str:>12}")
    print("═" * 70)

# ─── メイン ───────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="MTG RAG モデル比較ベンチマーク")
    parser.add_argument("--top_k",  type=int, default=10)
    parser.add_argument("--output", type=str, default="benchmark_results.json")
    parser.add_argument("--models", nargs="+", choices=["SMALL","BASE"],
                        default=["SMALL","BASE"])
    args = parser.parse_args()

    target_models = [m for m in MODELS if m.key in args.models]

    print("MTG RAG ベンチマーク開始")
    print(f"対象モデル: {[m.key for m in target_models]}")
    print(f"クエリ数: {len(QUERIES)}  |  top_k: {args.top_k}")

    conn = psycopg2.connect(**get_db_config())
    all_results: list[QueryResult] = []

    for cfg in target_models:
        print(f"\n[{cfg.key}] モデルロード中: {cfg.model_name}")
        model = SentenceTransformer(cfg.model_name,
                                    cache_folder="/mnt/new_hdd/hf_cache")

        for query_text, keywords, label in QUERIES:
            print(f"  検索中: 「{query_text}」", end=" ", flush=True)
            vec = embed_query(model, cfg, query_text)
            t0 = time.perf_counter()
            rows = search(conn, cfg, vec, args.top_k)
            elapsed = (time.perf_counter() - t0) * 1000
            print(f"({elapsed:.0f}ms)")
            qr = evaluate_results(rows, keywords, label, query_text, cfg.key, elapsed)
            all_results.append(qr)
            print_result(qr, top_n=5)

        del model

    print_summary(all_results)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in all_results], f, ensure_ascii=False, indent=2)
    print(f"\n結果を {args.output} に保存しました")

    conn.close()

if __name__ == "__main__":
    main()
