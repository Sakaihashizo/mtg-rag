"""modern_removal_quality_audit_20260715.py — 層3（実績）を切った機能品質だけの全数監査

問い（2026-07-15 本人）: play-rate を参照せず、モダンの全除去を機能品質だけで
精査・並べ替えたら何が見えるか（チェックボックス OFF の世界の全景）。

品質成分（全部 列から導出・カード内在・フォーマット非依存）:
  clean     : 恒久（permanent≠false）× 対象を取る destroy/exile エントリを持つ
  uncond    : target に qualifier IS NULL の creature/permanent エントリ＝無条件
  no_cap    : 除去手段が「固定値ダメージのみ」ではない（X・destroy/exile 系あり）
  targeted  : 対象を取る（単体除去性・wrath は 0）
  breadth   : 役割内万能性の幅（permanent 全域=4・単クラス=1）
並び: 純度 pts（clean+uncond+no_cap+targeted・0〜4）→ 幅 → cmc → 名前。
※cmc は同率の中の表示順のためだけ（R3: コストは機能に数えない）。

出力: docs/me/modern_removal_quality_audit_20260715.csv（全数・Excel 精査用）
      ＋ stdout に分布と最上位層。
"""
import csv
import sys

sys.path.insert(0, '/mnt/mtg_rag')

import psycopg2
from db_config import DB_CONFIG

OUT = '/mnt/mtg_rag/docs/me/modern_removal_quality_audit_20260715.csv'

SQL = """
SELECT
  c.card_name, c.japanese_name, c.cmc, c.type_line,
  c.removal_types,
  -- clean: 恒久・対象取り destroy/exile
  EXISTS (SELECT 1 FROM jsonb_array_elements(c.removal) e
          WHERE (e->>'type') IN ('destroy','exile')
            AND COALESCE((e->>'permanent')::boolean, true)
            AND COALESCE((e->>'targeted')::boolean, false)) AS clean,
  -- uncond: 無条件の creature/permanent 対象
  EXISTS (SELECT 1 FROM jsonb_array_elements(c.target) t
          WHERE t->>'qualifier' IS NULL
            AND t->>'type' IN ('creature','permanent')) AS uncond,
  -- no_cap: 固定値ダメージだけのカードではない
  EXISTS (SELECT 1 FROM jsonb_array_elements(c.removal) e
          WHERE (e->>'type') IN ('destroy','exile','tuck','sacrifice')
             OR ((e->>'type') = 'damage' AND (e->>'amount') = 'X')) AS no_cap,
  -- targeted: 対象を取るエントリあり
  EXISTS (SELECT 1 FROM jsonb_array_elements(c.removal) e
          WHERE COALESCE((e->>'targeted')::boolean, false)) AS targeted,
  -- breadth: 役割内万能性
  (SELECT COALESCE(MAX(CASE
     WHEN (e->>'type') IN ('destroy','exile','tuck','sacrifice')
          AND COALESCE((e->>'permanent')::boolean, true)
     THEN CASE WHEN e->>'object' = 'permanent' THEN 4 ELSE 1 END
     WHEN (e->>'type') IN ('damage','minus') THEN 1
     ELSE 0 END), 0)
   FROM jsonb_array_elements(c.removal) e) AS breadth,
  -- 精査用: 条件句の生テキスト
  (SELECT string_agg(DISTINCT t->>'qualifier', ' / ')
   FROM jsonb_array_elements(c.target) t
   WHERE t->>'qualifier' IS NOT NULL) AS qualifiers
FROM mtg_cards_v2 c
WHERE (c.legalities->>'modern') IN ('legal','restricted')
  AND (c.legalities->>'vintage') IN ('legal','restricted')
  AND EXISTS (
    SELECT 1 FROM jsonb_array_elements(c.removal) e
    WHERE (e->>'type') = 'sacrifice'
       OR ((e->>'type') IN ('destroy','exile','tuck')
           AND COALESCE((e->>'permanent')::boolean, true)
           AND ((e->>'object') IN ('creature','permanent')
                OR ((e->>'object') IS NULL
                    AND c.target_types && ARRAY['creature','any','permanent']::text[])))
       OR ((e->>'type') IN ('damage','minus')
           AND c.target_types && ARRAY['creature','any','permanent']::text[]))
"""


def main():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(SQL)
        rows = cur.fetchall()
    conn.close()

    scored = []
    for (name, ja, cmc, tl, rts, clean, uncond, no_cap, targeted,
         breadth, quals) in rows:
        pts = int(clean) + int(uncond) + int(no_cap) + int(targeted)
        scored.append((pts, breadth, float(cmc or 0), name, ja, tl, rts,
                       clean, uncond, no_cap, targeted, quals))
    scored.sort(key=lambda r: (-r[0], -r[1], r[2], r[3]))

    with open(OUT, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['purity_pts', 'breadth', 'cmc', 'card_name', 'japanese_name',
                    'type_line', 'removal_types', 'clean', 'uncond', 'no_cap',
                    'targeted', 'qualifiers'])
        for r in scored:
            w.writerow(r)

    from collections import Counter
    dist = Counter((r[0], r[1]) for r in scored)
    print(f'モダンの除去 全 {len(scored)} 枚 → {OUT}')
    print('\n純度 pts × 幅 の分布:')
    for (pts, br), n in sorted(dist.items(), key=lambda x: (-x[0][0], -x[0][1])):
        print(f'  pts={pts} 幅={br}: {n:4d} 枚')
    print('\n最上位層（純度 4）全員:')
    for r in scored:
        if r[0] < 4:
            break
        print(f'  幅{r[1]} cmc{r[2]:.0f} {r[3]}' + (f'（{r[4]}）' if r[4] else ''))


if __name__ == '__main__':
    main()
