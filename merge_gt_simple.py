#!/usr/bin/env python3
import csv
from collections import defaultdict

gt_file = 'eval_groundtruth_v2.csv'
pool_file = 'eval_pool_20260612_1712_routed.csv'

# GT を読み込み（キー → 行インデックス）
gt_rows = []
gt_keys = {}  # key -> index in gt_rows
with open(gt_file, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for idx, row in enumerate(reader):
        gt_rows.append(row)
        key = (row['query'], row['card_name'])
        gt_keys[key] = idx

print(f"既存 GT: {len(gt_rows)} 行")

# pool で human_grade が埋まってるもの抽出＆マージ
update_count = 0
insert_count = 0

with open(pool_file, 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        grade = row['human_grade'].strip() if row['human_grade'] else ''
        if grade and grade != '':
            key = (row['query'], row['card_name'])
            if key in gt_keys:
                # 既存行を更新
                gt_rows[gt_keys[key]]['human_grade'] = grade
                update_count += 1
            else:
                # 新規行を追加
                gt_rows.append(row)
                insert_count += 1

print(f"UPDATE: {update_count} 行")
print(f"INSERT: {insert_count} 行")

# CSV に保存
fieldnames = list(gt_rows[0].keys()) if gt_rows else []
with open(gt_file, 'w', encoding='utf-8', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(gt_rows)

print(f"\nマージ完了。{gt_file} に上書き保存")
print(f"合計: {len(gt_rows)} 行")

# 統計
grade_count = defaultdict(int)
for row in gt_rows:
    g = row['human_grade'].strip() if row['human_grade'] else 'None'
    grade_count[g] += 1

print(f"\nhuman_grade 分布:")
for g in sorted(grade_count.keys()):
    print(f"  {g}: {grade_count[g]}")
