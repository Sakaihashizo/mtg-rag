import pandas as pd

# 既存の gt を読み込み
gt = pd.read_csv('eval_groundtruth_v2.csv')

# routed pool を読み込み
pool = pd.read_csv('eval_pool_20260612_1712_routed.csv')

# キー設定
gt['_key'] = gt['query'] + '###' + gt['card_name']
pool['_key'] = pool['query'] + '###' + pool['card_name']

# pool で human_grade が入ってるもの抽出
pool_with_grade = pool[pool['human_grade'].notna()].copy()

print(f"既存 GT: {len(gt)} 行")
print(f"routed pool 採점済み: {len(pool_with_grade)} 行")

# マージ：キーでマッチして human_grade を更新
for idx, row in pool_with_grade.iterrows():
    key = row['_key']
    matching = gt[gt['_key'] == key]

    if len(matching) > 0:
        # 既存行の human_grade を更新
        gt.loc[gt['_key'] == key, 'human_grade'] = row['human_grade']
        print(f"UPDATE: {row['query']} / {row['card_name']} → {row['human_grade']}")
    else:
        # 新規行を追加
        new_row = row.drop('_key').to_dict()
        gt = pd.concat([gt, pd.DataFrame([new_row])], ignore_index=True)
        print(f"INSERT: {row['query']} / {row['card_name']} → {row['human_grade']}")

# _key カラム削除
gt = gt.drop('_key', axis=1)

# eval_groundtruth_v2.csv に上書き保存
gt.to_csv('eval_groundtruth_v2.csv', index=False)

# 統計
human_grade_counts = gt['human_grade'].value_counts().sort_index()
print(f"\nマージ完了。eval_groundtruth_v2.csv に上書き保存")
print(f"human_grade 分布:")
print(human_grade_counts)
print(f"合計: {len(gt)} 行")
