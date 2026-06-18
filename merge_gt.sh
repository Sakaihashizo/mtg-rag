#!/bin/bash

# eval_groundtruth_v2.csv と eval_pool_20260612_1712_routed.csv をマージ
# キー: query + card_name
# routed_pool の human_grade で既存行を更新、新規行は追加

gt_file="eval_groundtruth_v2.csv"
pool_file="eval_pool_20260612_1712_routed.csv"
tmp_file="${gt_file}.tmp"

# GT をコピー
cp "$gt_file" "$tmp_file"

# pool の各行で human_grade が埋まってるもので、GT を更新 or 追加
awk -F',' '
NR == 1 { next }  # header skip
NF > 10 && $10 != "" && $10 !~ /^[[:space:]]*$/ {
    # human_grade が入ってる（$10 は human_grade カラム）
    print "query=" $1 " card=" $5 " grade=" $10
}
' "$pool_file" | while read line; do
    query=$(echo "$line" | grep -oP 'query=\K[^ ]*')
    card=$(echo "$line" | grep -oP 'card=\K[^ ]*')
    grade=$(echo "$line" | grep -oP 'grade=\K[^ ]*')
    
    # GT で該当行を見つけて更新（クエリと card_name で）
    # CSV なので危ないが、試しに
    
    echo "Found: query=$query card=$card grade=$grade"
done

echo "マージ未実装。代わりに VM で実行を検討"
