"""removal_regrade_pool_20260717.py — R2' 新基準の再採点ワークシート生成

本人指示（2026-07-17 未明）: TournamentBoost を排した除去系クエリの採点プールを
全フォーマット・深さ 30 で生成。単体除去はパイオニア/レガシー/ヴィンテージ/パウパー
を新設（既存: モダン/スタンダード＋機構 3 クエリ）。最強系（boost）は対象外。

候補の取り方（クエリごとに union・dedup）:
  a) 中立 top-30: 役割 WHERE（＋機構門/フォーマット門）→ 純度 DESC, cmc ASC, 名前
     （play-rate を見ない＝boost 排除・新基準の 2/1/罠 のスペクトルを公平に敷く）
  b) 直行 B top-10（採用率順）＝定番の取り込み保証
  c) 直行 合成 top-10（幅×上限×テンポ）
  d) 本番 top-10（router キャッシュがある既存 5 クエリのみ・新設 4 は本番経路なし）
機械提案（R2' は列で機械化可能）を note に併記——**human_grade は本人が記入**。
  提案 2 = 対象を取る ∧（destroy/exile/tuck/damage）∧ 恒久
  提案 1 = sacrifice（エディクト）/ minus（R6 間接）/ 対象を取らない（全体）
  0（罠）は意味判断＝機械提案しない（空欄提案）。
"""
import csv
import sys

sys.path.insert(0, '/mnt/mtg_rag')

import psycopg2
from db_config import DB_CONFIG
from role_quality import breadth, purity, cap_penalty, modes, tempo_gain
import removal_direct_proto_20260715 as proto

OUT = '/mnt/mtg_rag/removal_regrade_pool_20260717.csv'
MAINLINE = ['Standard', 'Pioneer', 'Modern', 'Legacy']

# (クエリ, GTのformat値, legalities キー, 機構, per-format pr, テンポ母集団)
QUERIES = [
    ('クリーチャーを破壊する除去', '', 'vintage', ['destroy'], MAINLINE, 'Modern'),
    ('destroy target creature',   '', 'vintage', ['destroy'], MAINLINE, 'Modern'),
    ('クリーチャーを追放する除去', '', 'vintage', ['exile'],   MAINLINE, 'Modern'),
    ('モダンの単体除去',       'modern',   'modern',   None, ['Modern'],   'Modern'),
    ('スタンダードの単体除去', 'standard', 'standard', None, ['Standard'], 'Standard'),
    ('パイオニアの単体除去',   'pioneer',  'pioneer',  None, ['Pioneer'],  'Modern'),
    ('レガシーの単体除去',     'legacy',   'legacy',   None, ['Legacy'],   'Legacy'),
    ('ヴィンテージの単体除去', 'vintage',  'vintage',  None, ['Vintage'],  'Legacy'),
    ('パウパーの単体除去',     'pauper',   'pauper',   None, ['Pauper'],   'Modern'),
]

PRODUCTION_QUERIES = {'クリーチャーを破壊する除去', 'destroy target creature',
                      'クリーチャーを追放する除去', 'モダンの単体除去',
                      'スタンダードの単体除去'}


def machine_suggest(rem, td):
    """R2'（＋補足a 2026-07-17）の機械提案。0（罠）は意味判断なので提案しない。
    minus は targeted なら 2（税血/Dismember 族）・非 targeted は 1（全体系）。
    damage の targeted は割り振り構文（激情）も捕捉済み（enrich 側）。"""
    for e in (rem or []):
        # minus の permanent は修整の持続時間＝死の恒久性でないため見ない
        # （税血=until end of turn でも死は恒久・is_creature_removal と同じ注記）
        if e.get('type') == 'minus' and e.get('targeted'):
            return '2'
        if (e.get('type') in ('destroy', 'exile', 'tuck', 'damage')
                and e.get('permanent') is not False
                and e.get('targeted')):
            return '2'
    for e in (rem or []):
        if e.get('type') == 'sacrifice':
            return '1'
        if (e.get('type') in ('destroy', 'exile', 'damage', 'minus')
                and not e.get('targeted')):
            return '1'
    return ''


def main():
    conn = psycopg2.connect(**DB_CONFIG)
    import re
    from eval_framework import search_legal, load_router_cache
    from mtg_hybrid_search_v2 import MTGHybridSearcherV2

    # GT プリフィル
    gt_rows = {}
    with open('/mnt/mtg_rag/eval_groundtruth_v2.csv', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            gt_rows[(row['query'], row['card_name'])] = (
                row['human_grade'].strip(), row['note'])

    # テンポ母集団
    def population(fmt_name):
        with conn.cursor() as cur:
            cur.execute("""SELECT c.cmc, c.toughness, s.play_decks
                           FROM card_format_strength s
                           JOIN mtg_cards_v2 c ON c.id = s.card_id
                           WHERE s.format_name = %s
                             AND c.type_line ILIKE '%%Creature%%'""", (fmt_name,))
            return [(float(cmc or 0), int(t), int(w)) for cmc, t, w in cur.fetchall()
                    if t is not None and str(t).isdigit()]
    POPS = {f: population(f) for f in ('Modern', 'Standard', 'Legacy')}
    MV_RE = re.compile(r'mana value (\d+)')

    def kill_pred(e, td):
        typ = e.get('type')
        if typ in ('damage', 'minus'):
            a = e.get('amount')
            if a == 'X':
                return lambda mv, t: False
            return (lambda n: lambda mv, t: t <= n)(int(a or 0))
        if typ in ('destroy', 'exile', 'tuck', 'sacrifice'):
            if e.get('permanent') is False:
                return lambda mv, t: False
            for d in (td or []):
                m = MV_RE.search(d.get('qualifier') or '')
                if m:
                    return (lambda n: lambda mv, t: mv <= n)(int(m.group(1)))
            return lambda mv, t: True
        return lambda mv, t: False

    def fetch_pool(legal_key, mechs, pr_formats):
        mech_sql = ''
        if mechs:
            ms = ", ".join(f"'{m}'" for m in mechs)
            mech_sql = f" AND c.removal_types && ARRAY[{ms}]::text[]"
        sql = f"""
            SELECT c.card_name, c.japanese_name, c.type_line,
                   c.japanese_oracle_text, c.oracle_text,
                   c.removal, c.target, c.target_types, c.cmc, c.floor_cmc,
                   COALESCE(s.pr, 0) AS pr
            FROM mtg_cards_v2 c
            LEFT JOIN (SELECT card_id, SUM(play_decks) AS pr
                       FROM card_format_strength
                       WHERE format_name = ANY(%s) GROUP BY card_id) s
                   ON s.card_id = c.id
            WHERE (c.legalities->>%s) IN ('legal','restricted')
              AND (c.legalities->>'vintage') IN ('legal','restricted')
              AND {proto.ROLE_SQL} {mech_sql}
        """
        with conn.cursor() as cur:
            cur.execute(sql, (pr_formats, legal_key))
            return cur.fetchall()

    cache = load_router_cache('/mnt/mtg_rag/eval_router_cache.json')
    searcher = MTGHybridSearcherV2(model_key='SMALL_V2')

    out_rows = []
    total_blank = 0
    for q, fmt_gt, legal_key, mechs, prf, popf in QUERIES:
        pool = fetch_pool(legal_key, mechs, prf)
        info = {r[0]: r for r in pool}
        pop = POPS[popf]

        def tempo_of(r):
            _, _, _, _, _, rem, td, tt, cmc, fc, pr = r
            return max((tempo_gain(cost, kill_pred(e, td), pop)
                        for cost, e in modes(rem, cmc, fc)), default=0.0)

        picked = []
        # a) 中立 top-30（純度→cmc→名前・play-rate 不使用）
        def pts(r):
            cl, un, nc_, tg = purity(r[5], r[6])
            return int(cl) + int(un) + int(nc_) + int(tg)
        for r in sorted(pool, key=lambda r: (-pts(r), float(r[8] or 0), r[0]))[:30]:
            if r[0] not in picked:
                picked.append(r[0])
        # b) 直行 B top-10
        for r in sorted(pool, key=lambda r: (-r[10], r[0]))[:10]:
            if r[0] not in picked:
                picked.append(r[0])
        # c) 直行 合成 top-10
        def comp(r):
            b = breadth(r[5], r[6], r[7])
            return r[10] * (1 + 0.25 * b) * cap_penalty(r[5]) * (1 + 0.6 * tempo_of(r))
        for r in sorted(pool, key=lambda r: (-comp(r), r[0]))[:10]:
            if r[0] not in picked:
                picked.append(r[0])
        # d) 本番 top-10（キャッシュのある既存クエリのみ）
        if q in PRODUCTION_QUERIES:
            entry = cache['entries'].get(q)
            legal, _ = search_legal(searcher, conn, q, fmt_gt or None, 10,
                                    router_entry=entry)
            for res in legal:
                if res.card_name not in picked:
                    picked.append(res.card_name)

        blanks = 0
        for i, name in enumerate(picked, 1):
            r = info.get(name)
            if r is None:   # 本番経路のみで出た役割外カード＝罠候補・情報を DB から
                with conn.cursor() as cur:
                    cur.execute("""SELECT japanese_name, type_line,
                                          japanese_oracle_text, oracle_text, removal, target
                                   FROM mtg_cards_v2 WHERE card_name = %s LIMIT 1""",
                                (name,))
                    jn, tl, jo, ot, rem, td = cur.fetchone()
            else:
                _, jn, tl, jo, ot, rem, td = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
            g, old_note = gt_rows.get((q, name), ('', ''))
            if g:
                note = old_note   # 既採点はそのまま（R2' で動かすかは明日の本人判断）
            else:
                s = machine_suggest(rem, td)
                note = f'機械提案:{s}（R2'"'"'）' if s else '機械提案:なし（罠候補？要判断）'
                blanks += 1
            out_rows.append([q, fmt_gt, 'removal_regrade_r2p', i, name,
                             jn or '', tl or '', jo or '', ot or '', g, note])
        total_blank += blanks
        print(f'{q}: {len(picked)} 枚（要記入 {blanks}）')

    searcher.close()
    with open(OUT, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['query', 'format', 'category', 'system_rank', 'card_name',
                    'japanese_name', 'type_line', 'japanese_oracle_text',
                    'oracle_text', 'human_grade', 'note'])
        w.writerows(out_rows)
    print(f'\n{OUT}: {len(out_rows)} 行（要記入 合計 {total_blank}）')
    conn.close()


if __name__ == '__main__':
    main()
