#!/usr/bin/env python
"""test_router_validation.py — ルーター出力の検証層（_parse_router_json まわり）の安全試験。

第1弾（2026-07-12）: _adjust_exclusive_bounds
  「Nより大きい/N超え」（排他）を包含 min/max スキーマへ ±1 補正する決定的レイヤ。
  本人の実地テスト「パワーが９より小さく7より大きいクリーチャー」でパワー7が
  混入した穴（7B が排他→包含の算術を片方だけ失敗・query_log id=10）への対応。
純 Python・LLM 不要・決定的。
"""
import sys

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_rag_agent import _adjust_exclusive_bounds

# (クエリ, ルーターが出した filters, 期待する補正後)
CASES = [
    # ─ 本人発見の実ケース（query_log id=10・全角９と半角7の混在そのまま） ─
    ("パワーが９より小さく7より大きいクリーチャー",
     {"power_min": 7, "power_max": 8}, {"power_min": 8, "power_max": 8}),
    # ─ 冪等: ルーターが既に正しく変換済みなら二重補正しない ─
    ("パワーが9より小さく7より大きいクリーチャー",
     {"power_min": 8, "power_max": 8}, {"power_min": 8, "power_max": 8}),
    # ─ 包含表現（以上/以下）は不変 ─
    ("2マナ以下のカウンター呪文", {"cmc_max": 2}, {"cmc_max": 2}),
    ("パワー7以上のクリーチャー", {"power_min": 7}, {"power_min": 7}),
    # ─ 「超え」「未満」 ─
    ("マナ総量5を超えるカード",   {"cmc_min": 5}, {"cmc_min": 6}),
    ("マナ総量5超えのカード",     {"cmc_min": 5}, {"cmc_min": 6}),
    ("タフネス3未満のクリーチャー", {"toughness_max": 3}, {"toughness_max": 2}),
    # ─ 全角数字 ─
    ("パワー７より大きいクリーチャー", {"power_min": 7}, {"power_min": 8}),
    # ─ 値が一致しないスロットは触らない ─
    ("パワー7より大きいクリーチャー", {"cmc_max": 3, "power_min": 7},
     {"cmc_max": 3, "power_min": 8}),
    # ─ filters 空・排他表現なし ─
    ("パワーが7より大きい", {}, {}),
    ("速攻を持つクリーチャー", {"mana_producer": True}, {"mana_producer": True}),
]


def main() -> int:
    failures = []
    for query, given, want in CASES:
        got = _adjust_exclusive_bounds(query, dict(given))
        if got != want:
            failures.append(f"{query!r}: got {got}, want {want}")
    print(f"CASES {len(CASES)} 本")
    if failures:
        print(f"\nFAIL {len(failures)} 件:")
        for x in failures:
            print("  " + x)
        return 1
    print("ALL PASS（排他境界の補正・冪等性・包含の不変）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
