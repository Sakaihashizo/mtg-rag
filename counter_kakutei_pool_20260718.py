"""counter_kakutei_pool_20260718.py — 「確定カウンター呪文」採点ワークシート生成

カウンター便（2026-07-17 深夜裁定=案B）: 偽ペルソナ「条件付きカウンター呪文」を退役し、
俗語アンカーのある「確定カウンター呪文」へ置換する。その GT を作るための本人採点用
ワークシート。機械提案は spell_conditional の類型分割（2026-07-18・soft/scope）に基づく:

  提案 2 = counter 役割（target_types に spell）∧ soft でない
           （範囲制限 scope は確定性を削らない＝R2' と同精神・本人の線）
  提案 1 = soft（unless ... pays＝支払いで逃げられる）
  提案なし = spell トークンなし（役割外・罠候補＝Stifle 型等・意味判断は本人）

機械の既知の死角: 状態依存（"if you control ..." 型）は未捕捉＝確定側に紛れる。
ワークシートの目視で拾う（note に注意書き）。

候補プール（union・dedup）:
  a) 退役クエリ「条件付きカウンター呪文」の GT 採点済み 41 枚（判断の引き継ぎ用・
     旧 grade を note に参考表示。クエリが違うのでプリフィルはしない）
  b) 直行 B top15（counter 役割 ∧ Vintage リーガル・本線 4F 採用率順）
  c) 層別サンプル: 確定/scope のみ/soft の各層から採用率上位 5 枚
出力: counter_kakutei_pool_20260718.csv（human_grade は空欄＝本人記入）
"""
import csv
import sys

sys.path.insert(0, '/mnt/mtg_rag')

import psycopg2
from db_config import DB_CONFIG

OUT = '/mnt/mtg_rag/counter_kakutei_pool_20260718.csv'
GT = '/mnt/mtg_rag/eval_groundtruth_v2.csv'
MAINLINE = ['Standard', 'Pioneer', 'Modern', 'Legacy']
COUNTER_QUERIES = {'純粋に強いカウンター呪文', 'モダンの最強カウンター呪文',
                   'レガシーのカウンター呪文', '2マナ以下のカウンター呪文',
                   '条件付きカウンター呪文', 'counter target spell'}

BASE_SQL = """
    SELECT c.card_name, c.japanese_name, c.type_line,
           c.japanese_oracle_text, c.oracle_text, c.target_types,
           COALESCE(s.pr, 0) AS pr
    FROM mtg_cards_v2 c
    LEFT JOIN (SELECT card_id, SUM(play_decks) AS pr
               FROM card_format_strength
               WHERE format_name = ANY(%s) GROUP BY card_id) s ON s.card_id = c.id
    WHERE (c.legalities->>'vintage') IN ('legal','restricted')
"""


def machine_suggest(tt):
    tt = tt or []
    if 'spell' not in tt:
        return ''
    return '1' if 'spell_conditional_soft' in tt else '2'


def main():
    conn = psycopg2.connect(**DB_CONFIG)

    # 参考 grade（カウンター族 6 クエリの既存採点）
    ref = {}
    with open(GT, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if r['query'] in COUNTER_QUERIES and r['human_grade'].strip() in ('0', '1', '2'):
                ref.setdefault(r['card_name'], {})[r['query']] = r['human_grade'].strip()

    def fetch(extra_where, order, limit, params=()):
        sql = BASE_SQL + extra_where + f" ORDER BY {order} LIMIT {limit}"
        with conn.cursor() as cur:
            cur.execute(sql, (MAINLINE, *params))
            return cur.fetchall()

    picked, rows = [], {}

    def add(rs):
        for r in rs:
            if r[0] not in rows:
                rows[r[0]] = r
                picked.append(r[0])

    # a) 退役クエリの採点済みカード（引き継ぎの軸）
    cond_cards = [c for c, g in ref.items() if '条件付きカウンター呪文' in g]
    if cond_cards:
        with conn.cursor() as cur:
            cur.execute(BASE_SQL + " AND c.card_name = ANY(%s)",
                        (MAINLINE, cond_cards))
            add(cur.fetchall())
    # b) 直行 B top15（counter 役割）
    add(fetch(" AND c.target_types @> ARRAY['spell']", "pr DESC, c.card_name", 15))
    # c) 層別サンプル各 5
    add(fetch(" AND c.target_types @> ARRAY['spell']"
              " AND NOT c.target_types && ARRAY['spell_conditional']",
              "pr DESC, c.card_name", 5))                                   # 無条件（確定ど真ん中）
    add(fetch(" AND c.target_types @> ARRAY['spell','spell_conditional_scope']"
              " AND NOT c.target_types && ARRAY['spell_conditional_soft']",
              "pr DESC, c.card_name", 5))                                   # scope のみ（確定・範囲つき）
    add(fetch(" AND c.target_types @> ARRAY['spell','spell_conditional_soft']",
              "pr DESC, c.card_name", 5))                                   # soft（不確定）

    out_rows, blanks = [], 0
    for i, name in enumerate(picked, 1):
        _, jn, tl, jo, ot, tt, pr = rows[name]
        s = machine_suggest(tt)
        note = (f'機械提案:{s}（確定線=soft でなければ 2・状態依存は死角＝目視）'
                if s else '機械提案:なし（spell 対象なし＝役割外/罠候補・要判断）')
        refs = ref.get(name, {})
        if refs:
            note += ' ／参考: ' + '・'.join(f'{q[:6]}={g}' for q, g in refs.items())
        blanks += 1
        out_rows.append(['確定カウンター呪文', '', 'counter_kakutei', i, name,
                         jn or '', tl or '', jo or '', ot or '', '', note])

    with open(OUT, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['query', 'format', 'category', 'system_rank', 'card_name',
                    'japanese_name', 'type_line', 'japanese_oracle_text',
                    'oracle_text', 'human_grade', 'note'])
        w.writerows(out_rows)
    print(f'{OUT}: {len(out_rows)} 行（全行 要記入・機械提案は note）')
    conn.close()


if __name__ == '__main__':
    main()
