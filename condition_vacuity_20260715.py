"""condition_vacuity_20260715.py — R7' の試作: 「条件の空虚度」をフォーマット実勢で測る（$0）

問い（2026-07-15 本人）: R7 のハードキャップは card-intrinsic だが、条件の重さは
フォーマット相対では？（虹色の終焉の上限はモダンではほぼ噛まない＝実質無条件）

物差し: 捕捉率 = そのフォーマットで実際に採用されているクリーチャー
（card_format_strength に行がある＝main 採用・採用デッキ数で重み付け）のうち、
その除去の条件を満たして討てる比率。
  - 捕捉率 ≈ 1 → 条件は実質空（vacuous）＝ superlative 採点で無条件扱いの候補
  - 捕捉率が低い → 条件が実戦で噛む＝条件付き（R7 どおり 1）

正直な限界（読み方）:
  - タフネス条件は数値タフネスのみ（X/* は分母から除外・件数表示）
  - 呪禁/護法/プロテクションは見ない（対象に取れるかは別軸）
  - delirium/revolt 等「使い手側の条件」は base/enabled の両端を表示
    （定番デッキでは enabled 側が実態＝Unholy Heat in Murktide）
"""
import sys

sys.path.insert(0, '/mnt/mtg_rag')

import psycopg2
from db_config import DB_CONFIG

# (カード, フォーマット, 条件ラベル, WHERE 追加句)
# 条件句は「討てる側」= TRUE で捕捉
CASES = [
    ('Lightning Bolt',    'Modern',   'タフネス≤3',            "t <= 3"),
    ('Unholy Heat',       'Modern',   'base タフネス≤2',       "t <= 2"),
    ('Unholy Heat',       'Modern',   '昂揚 タフネス≤6',        "t <= 6"),
    ('Prismatic Ending',  'Modern',   'X=2色 MV≤2',           "cmc <= 2"),
    ('Prismatic Ending',  'Modern',   'X=3色 MV≤3',           "cmc <= 3"),
    ('Prismatic Ending',  'Modern',   'X=4色 MV≤4',           "cmc <= 4"),
    ('Fatal Push',        'Modern',   'base MV≤2',            "cmc <= 2"),
    ('Fatal Push',        'Modern',   '紛争 MV≤4',             "cmc <= 4"),
    ('Fatal Push',        'Pioneer',  'base MV≤2',            "cmc <= 2"),
    ('Fatal Push',        'Pioneer',  '紛争 MV≤4',             "cmc <= 4"),
    ('Abrupt Decay',      'Modern',   'MV≤3',                 "cmc <= 3"),
    ('Go for the Throat', 'Modern',   '非アーティファクト',      "type_line NOT ILIKE '%%Artifact%%'"),
    ('Go for the Throat', 'Standard', '非アーティファクト',      "type_line NOT ILIKE '%%Artifact%%'"),
    ('Orcish Bowmasters', 'Modern',   'ping タフネス≤1',        "t <= 1"),
    ('Burst Lightning',   'Standard', 'base タフネス≤2',        "t <= 2"),
    ('Burst Lightning',   'Standard', 'キッカー タフネス≤4',     "t <= 4"),
    ('Torch the Tower',   'Standard', 'base タフネス≤2',        "t <= 2"),
    ('Torch the Tower',   'Standard', '協約 タフネス≤3',        "t <= 3"),
    ('Lightning Bolt',    'Legacy',   'タフネス≤3',            "t <= 3"),
    ('Swords to Plowshares', 'Legacy', '無条件（基準）',         "TRUE"),
]

POP_SQL = """
    SELECT c.cmc, c.toughness, c.type_line, s.play_decks
    FROM card_format_strength s
    JOIN mtg_cards_v2 c ON c.id = s.card_id
    WHERE s.format_name = %s
      AND c.type_line ILIKE '%%Creature%%'
"""


def main():
    conn = psycopg2.connect(**DB_CONFIG)
    pops = {}
    with conn.cursor() as cur:
        for fmt in {c[1] for c in CASES}:
            cur.execute(POP_SQL, (fmt,))
            rows = cur.fetchall()
            pop = []
            skipped = 0
            for cmc, tough, tl, w in rows:
                if tough is None or not str(tough).isdigit():
                    skipped += 1
                    continue
                pop.append((float(cmc or 0), int(tough), tl, int(w)))
            pops[fmt] = (pop, skipped)
    conn.close()

    print('捕捉率 = 採用デッキ数で重み付けした「討てるクリーチャー」比率')
    for fmt in sorted(pops):
        pop, skipped = pops[fmt]
        total_w = sum(w for _, _, _, w in pop)
        print(f'\n== {fmt}: 採用クリーチャー {len(pop)} 種（重み計 {total_w:,}・'
              f'非数値タフネス除外 {skipped} 種） ==')
        for name, f, label, cond in CASES:
            if f != fmt:
                continue
            def match(cmc, t, tl):
                if cond == 'TRUE':
                    return True
                if cond.startswith('t <='):
                    return t <= int(cond.split('<=')[1])
                if cond.startswith('cmc <='):
                    return cmc <= int(cond.split('<=')[1])
                if cond.startswith('type_line'):
                    return 'artifact' not in tl.lower()
                raise ValueError(cond)
            hit = sum(w for cmc, t, tl, w in pop if match(cmc, t, tl))
            rate = hit / total_w if total_w else 0
            bar = '#' * int(rate * 30)
            print(f'  {name:20s} {label:14s} {rate:6.1%}  {bar}')


if __name__ == '__main__':
    main()
