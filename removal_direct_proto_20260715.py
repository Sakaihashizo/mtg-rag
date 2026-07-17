"""removal_direct_proto_20260715.py — 除去クエリの SQL 直行路プロトタイプ（$0）

主張の検証（2026-07-15 本人）: 「target/removal/removal_types 列の登場で除去は
crisp になった＝直行路（WHERE＋決定的 ORDER）に引っ越せるのでは」。
除去 6 クエリについて SQL 一本のランキングを現行 GT で採点し、
現行検索（eval id=78 系）との天井比較を出す。eval_runs には書かない。

集合（WHERE）: is_creature_removal（mtg_hybrid_search_v2）の SQL 写し
  ＋ Vintage リーガル ＋ フォーマット門 ＋ 機構門（明示クエリのみ）。
並び（ORDER）2 案:
  A: clean 階層 → play-rate → id （clean = 恒久・対象取り・destroy/exile の
     ど真ん中エントリ持ち＝R1/R2/R5 の「無条件 2」の近似）
  B: play-rate → id （キーワード直行路と同型・メタ実勢のみ）
最強の単体除去は R11 の定義どおり play-rate 主導（B 相当）に clean を従とする。
破壊系は機構門 2 変種: destroy のみ（現行）／ destroy∪damage（R5「致死ダメージ
による破壊を含む」整合＝D 案の試走）。
"""
import csv
import sys

sys.path.insert(0, '/mnt/mtg_rag')

import psycopg2
from db_config import DB_CONFIG
from eval_framework import compute_metrics
from mtg_hybrid_search_v2 import format_filter_sql

GT_PATH = '/mnt/mtg_rag/eval_groundtruth_v2.csv'
MAINLINE = ['Standard', 'Pioneer', 'Modern', 'Legacy']

ROLE_SQL = """
  EXISTS (
    SELECT 1 FROM jsonb_array_elements(c.removal) e
    WHERE (e->>'type') = 'sacrifice'
       OR ((e->>'type') IN ('destroy','exile','tuck')
           AND COALESCE((e->>'permanent')::boolean, true)
           AND ((e->>'object') IN ('creature','permanent')
                OR ((e->>'object') IS NULL
                    AND c.target_types && ARRAY['creature','any','permanent']::text[])))
       OR ((e->>'type') IN ('damage','minus')
           AND c.target_types && ARRAY['creature','any','permanent']::text[])
  )
"""

CLEAN_TIER_SQL = """
  CASE WHEN EXISTS (
    SELECT 1 FROM jsonb_array_elements(c.removal) e
    WHERE (e->>'type') IN ('destroy','exile')
      AND COALESCE((e->>'permanent')::boolean, true)
      AND COALESCE((e->>'targeted')::boolean, false)
      AND ((e->>'object') IN ('creature','permanent')
           OR ((e->>'object') IS NULL
               AND c.target_types && ARRAY['creature','any','permanent']::text[]))
  ) THEN 0 ELSE 1 END
"""

# (クエリ, GT の format, フォーマット門, 機構, superlative)
QUERIES = [
    ('クリーチャーを破壊する除去', None, ['destroy'], False),
    ('destroy target creature', None, ['destroy'], False),
    ('クリーチャーを追放する除去', None, ['exile'], False),
    ('モダンの単体除去', 'modern', None, False),
    ('スタンダードの単体除去', 'standard', None, False),
    ('最強の単体除去', None, None, True),
]


def load_gt():
    gt_by_query = {}
    with open(GT_PATH, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            hg = row['human_grade'].strip()
            if hg in ('0', '1', '2'):
                gt_by_query.setdefault(row['query'], {})[row['card_name']] = int(hg)
    return gt_by_query


def run_sql(conn, fmt, mechs, order, damage_widen=False):
    fmt_sql = format_filter_sql(fmt)
    mech_sql = ''
    if mechs:
        ms = ", ".join(f"'{m}'" for m in mechs)
        mech_sql = f" AND c.removal_types && ARRAY[{ms}]::text[]"
        if damage_widen:
            # R5: 致死ダメージによる破壊は破壊の本線＝damage も門を通す
            mech_sql = (f" AND (c.removal_types && ARRAY[{ms}]::text[]"
                        f" OR (c.removal_types && ARRAY['damage']::text[]"
                        f"     AND c.target_types && ARRAY['creature','any']::text[]))")
    pr_formats = [fmt.capitalize()] if fmt else MAINLINE
    sql = f"""
        SELECT c.card_name
        FROM mtg_cards_v2 c
        LEFT JOIN (SELECT card_id, SUM(play_decks) AS pr
                   FROM card_format_strength
                   WHERE format_name = ANY(%s)
                   GROUP BY card_id) s ON s.card_id = c.id
        WHERE (c.legalities->>'vintage') IN ('legal','restricted')
          {fmt_sql}
          AND {ROLE_SQL}
          {mech_sql}
        ORDER BY {order}
        LIMIT 10
    """
    with conn.cursor() as cur:
        cur.execute(sql, (pr_formats,))
        return [r[0] for r in cur.fetchall()]


def score(names, gt):
    sr = [(n, i + 1) for i, n in enumerate(names)]
    m = compute_metrics(sr, gt)
    mj = compute_metrics([(n, r) for n, r in sr if n in gt], gt)
    unl = sum(1 for n in names if n not in gt) / max(len(names), 1)
    return m['ndcg_10'], mj['ndcg_10'], unl


def main():
    gt_by_query = load_gt()
    conn = psycopg2.connect(**DB_CONFIG)

    ORDER_A = f"{CLEAN_TIER_SQL} ASC, COALESCE(s.pr,0) DESC, c.id"
    ORDER_B = "COALESCE(s.pr,0) DESC, c.id"
    ORDER_SUP = f"COALESCE(s.pr,0) DESC, {CLEAN_TIER_SQL} ASC, c.id"

    for q, fmt, mechs, sup in QUERIES:
        gt = gt_by_query[q]
        variants = []
        if sup:
            variants.append(('R11型(play-rate→clean)', ORDER_SUP, False))
        else:
            variants.append(('A(clean→play-rate)', ORDER_A, False))
            variants.append(('B(play-rateのみ)', ORDER_B, False))
            if mechs == ['destroy']:
                variants.append(('A+damage門(R5整合)', ORDER_A, True))
        print(f'\n#### {q} (fmt={fmt} mech={mechs})')
        for label, order, widen in variants:
            names = run_sql(conn, fmt, mechs, order, damage_widen=widen)
            ndcg, ndcg_j, unl = score(names, gt)
            print(f'  {label:22s} NDCG={ndcg:.3f} judged={ndcg_j:.3f} 未採点={unl:.0%}')
            print(f'    top10: ' + ' / '.join(
                f'{n}{ {2: "②", 1: "①", 0: "⓪"}.get(gt.get(n), "－") }'
                for n in names))
    conn.close()


if __name__ == '__main__':
    main()
