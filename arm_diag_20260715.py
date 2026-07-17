"""arm_diag_20260715.py — 腕別 TOP10 診断（除去・打ち消し 8 クエリ）

eval（router キャッシュ経路・id=63/72 と同一条件）で各腕が「何を連れてきたか」を
腕ごとに top-10 で見せる。全部 $0（ローカル embedding＋キャッシュ＋SELECT のみ）。

腕の内訳（search()/search_with_hyde の実呼び出しを写経・深さも eval 同値=120）:
  vec      : expand_query(search_query) の embedding 近傍
  en_fts   : 英語キーワード FTS（removal_mode 時は REMOVAL_TSQUERY）
  ja_fts   : 日本語キーワード FTS
  strength : play-rate 上位候補腕（tournament_boost クエリのみ・役割フィルタ付き）
  hyde_en  : 英語 HyDE 文の embedding 近傍
  hyde_ja  : 日本語 HyDE 文の embedding 近傍
注意: 腕の生列は Vintage リーガル除外前（eval はマージ後に除外する）。
grade 注記は eval_groundtruth_v2.csv（2026-07-15 の裁定 E 反映後）。

実行: /mnt/new_hdd/my_rag_env/bin/python arm_diag_20260715.py
出力: docs/me/arm_diagnostics_20260715.md ＋ stdout に圧縮表示
"""
import csv
import sys

sys.path.insert(0, '/mnt/mtg_rag')

import psycopg2
from db_config import DB_CONFIG
from eval_framework import search_legal, load_router_cache
from mtg_hybrid_search_v2 import (
    MTGHybridSearcherV2, extract_keywords, expand_query,
    format_filter_sql, type_filter_sql, attr_filter_sql,
    keyword_filter_sql, removal_mech_filter_sql,
    detect_pt_relation, pt_relation_sql, detect_tribal, tribal_filter_sql,
    detect_name_search, name_contains_sql, detect_neg_type, neg_type_filter_sql,
)

GT_PATH = '/mnt/mtg_rag/eval_groundtruth_v2.csv'
CACHE_PATH = '/mnt/mtg_rag/eval_router_cache.json'
OUT_PATH = '/mnt/mtg_rag/docs/me/arm_diagnostics_20260715.md'

TARGETS = [
    'モダンの単体除去',
    'スタンダードの単体除去',
    'クリーチャーを追放する除去',
    'クリーチャーを破壊する除去',
    'destroy target creature',
    '最強の単体除去',
    '純粋に強いカウンター呪文',
    'モダンの最強カウンター呪文',
]

# eval 経路の実効深さ: search_legal(top_k=10) → search_with_hyde(20) → search(40)
ARM_TOP_K = 40

GRADE_MARK = {2: '②', 1: '①', 0: '⓪'}


def load_gt():
    gt_by_query, fmt_by_query = {}, {}
    with open(GT_PATH, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            q = row['query']
            hg = row['human_grade'].strip()
            if not hg:
                continue
            try:
                hg = int(hg)
            except ValueError:
                continue
            if hg not in (0, 1, 2):
                continue
            if q not in gt_by_query:
                gt_by_query[q] = {}
                fmt_by_query[q] = row.get('format', '') or None
            gt_by_query[q][row['card_name']] = hg
    return gt_by_query, fmt_by_query


def mark(name, gt):
    g = gt.get(name)
    return GRADE_MARK.get(g, '－')


def arm_rows_for(searcher, entry, query, fmt):
    """search()/search_with_hyde() の腕呼び出しを写経して 6 腕の生列を返す"""
    sq = entry.get('search_query') or query
    hyde = entry.get('hyde_text') or ''
    ja_hyde = entry.get('ja_hyde_text') or ''

    (en_kws, ja_kws, type_filter, tb_d, rm_d, cm_d,
     kw_abilities, neg_kw, _kw_only) = extract_keywords(sq)
    tournament_boost = tb_d or bool(entry.get('tournament_boost'))
    removal_mode = rm_d or bool(entry.get('removal_mode'))
    counter_mode = cm_d or bool(entry.get('counter_mode'))
    tf_override = entry.get('type_filter')
    if tf_override:
        type_filter = tf_override

    fmt_sql = format_filter_sql(fmt)
    type_sql = type_filter_sql(type_filter)          # vec/FTS/strength 腕用
    type_sql_hyde = type_filter_sql(tf_override)     # HyDE 腕は override のみ（写経）
    attr_sql = attr_filter_sql()                     # 8 クエリとも filters={}
    attr_sql += keyword_filter_sql(kw_abilities, neg_kw)
    attr_sql += removal_mech_filter_sql(sq, removal_mode)
    attr_sql += pt_relation_sql(detect_pt_relation(sq))
    attr_sql += tribal_filter_sql(detect_tribal(sq))
    attr_sql += name_contains_sql(detect_name_search(sq))
    attr_sql += neg_type_filter_sql(detect_neg_type(sq))

    arms = {}
    vec = searcher._embed(expand_query(sq))
    arms['vec'] = searcher._vector_search(vec, ARM_TOP_K, fmt_sql, type_sql, attr_sql)
    arms['en_fts'] = searcher._en_text_search(en_kws, ARM_TOP_K, fmt_sql, type_sql,
                                              attr_sql, removal_mode=removal_mode)
    arms['ja_fts'] = searcher._ja_text_search(ja_kws, ARM_TOP_K, fmt_sql, type_sql,
                                              attr_sql)
    if tournament_boost:
        arms['strength'] = searcher._strength_candidates(
            ARM_TOP_K, fmt, fmt_sql, type_sql, attr_sql,
            removal_mode=removal_mode, counter_mode=counter_mode)
    if hyde:
        hyde_vec = searcher._embed(hyde)
        arms['hyde_en'] = searcher._vector_search(hyde_vec, ARM_TOP_K, fmt_sql,
                                                  type_sql_hyde, attr_sql)
    if ja_hyde:
        ja_vec = searcher._embed(ja_hyde)
        arms['hyde_ja'] = searcher._vector_search(ja_vec, ARM_TOP_K, fmt_sql,
                                                  type_sql_hyde, attr_sql)
    return arms


def main():
    gt_by_query, fmt_by_query = load_gt()
    cache = load_router_cache(CACHE_PATH)
    entries = cache['entries']

    conn = psycopg2.connect(**DB_CONFIG)
    searcher = MTGHybridSearcherV2(model_key='SMALL_V2')

    lines_md = [
        '# 腕別 TOP10 診断（除去・打ち消し 8 クエリ・2026-07-15）',
        '',
        '目的: 各腕が候補として「何を連れてきたか」を腕単位で見る（融合前の生列）。',
        '条件: eval と同一（router キャッシュ 2026-06-23・腕深さ 120 のうち上位 10 を表示）。',
        'grade は 2026-07-15 裁定 E 反映後の GT。記号: ②=grade2 ①=grade1 ⓪=grade0 －=GT 未収録。',
        '腕の生列は Vintage リーガル除外前（eval はマージ後に除外）。',
        '再実行: `/mnt/new_hdd/my_rag_env/bin/python arm_diag_20260715.py`',
        '',
    ]
    compact = []

    for q in TARGETS:
        gt = gt_by_query.get(q, {})
        fmt = fmt_by_query.get(q)
        entry = entries[q]
        arms = arm_rows_for(searcher, entry, q, fmt)
        final10, _ = search_legal(searcher, conn, q, fmt, 10, router_entry=entry)

        n2 = sum(1 for g in gt.values() if g == 2)
        n1 = sum(1 for g in gt.values() if g == 1)
        lines_md += [f'## {q}（format={fmt or "なし"}・GT: grade2={n2} grade1={n1}）', '']
        compact.append(f'#### {q}  (format={fmt or "なし"})')

        for arm_name, rows in arms.items():
            top = rows[:10]
            lines_md.append(f'### {arm_name}')
            for r in top:
                nm = r['card_name']
                lines_md.append(f'{int(r["rank"]):3d}. [{mark(nm, gt)}] {nm}')
            lines_md.append('')
            compact.append(f'  {arm_name:8s}: '
                           + ' / '.join(f'{r["card_name"]}{mark(r["card_name"], gt)}'
                                        for r in top))
        lines_md.append('### 最終 top-10（融合＋リーガル除外後＝eval が採点する列）')
        for i, r in enumerate(final10):
            lines_md.append(f'{i + 1:3d}. [{mark(r.card_name, gt)}] {r.card_name}')
        lines_md.append('')
        compact.append(f'  最終    : '
                       + ' / '.join(f'{r.card_name}{mark(r.card_name, gt)}'
                                    for r in final10))
        compact.append('')

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines_md))

    print('\n' + '=' * 70)
    print('\n'.join(compact))
    print(f'完全版: {OUT_PATH}')

    searcher.close()
    conn.close()


if __name__ == '__main__':
    main()
