"""
mtg_counter_rules.py — カウンター呪文クエリのペナルティルール管理
=================================================================
カウンター呪文系クエリ（純粋に強いカウンター呪文・モダンの最強カウンター等）の
検索結果から「カウンター呪文ではないカード」を自動的にスコアダウンするルール集。

ルールの追加方法:
  COUNTER_PENALTY_RULES に辞書を追加するだけ。
  各ルールは oracle_text（英語）または japanese_oracle_text（日本語）に対して
  パターンマッチを行い、ヒットした場合に rrf_score を乗算する。
"""

import re
from typing import Optional

# ─── ペナルティルール定義 ─────────────────────────────────────

COUNTER_PENALTY_RULES = [

    # ルール1: 護法（Ward）キーワードを持つカード
    # 「護法」というテキストがあればそのカードは護法持ち
    # 護法の打ち消しはカウンター呪文ではない
    {
        "label":        "護法キーワード（カウンター呪文ではない）",
        "lang":         "ja",
        "must_contain": ["護法"],
        "penalty":      0.1,
    },
]


# ─── ペナルティ適用関数 ───────────────────────────────────────

def apply_counter_penalties(scores: dict, counter_mode: bool) -> dict:
    """
    counter_mode=True の場合のみペナルティを適用する。
    またパーマネント（Instant/Sorcery 以外）で打ち消し効果を持つカードも
    スコアダウンする。
    """
    if not counter_mode:
        return scores

    for name, data in scores.items():
        oracle_en  = (data["row"].get("oracle_text") or "").lower()
        oracle_ja  = (data["row"].get("japanese_oracle_text") or "").lower()
        type_line  = (data["row"].get("type_line") or "").lower()

        # ルールベースのペナルティ
        for rule in COUNTER_PENALTY_RULES:
            lang    = rule["lang"]
            text    = oracle_en if lang == "en" else oracle_ja
            penalty = rule["penalty"]

            must = rule.get("must_contain", [])
            if not all(kw.lower() in text for kw in must):
                continue

            must_or = rule.get("must_or_contain", [])
            if must_or and not any(kw.lower() in text for kw in must_or):
                continue

            must_not = rule.get("must_not_contain", [])
            if any(kw.lower() in text for kw in must_not):
                continue

            data["rrf"] *= penalty

        # パーマネントの打ち消し効果はペナルティなし
        # 相殺・コジレック・吸収するウェルク等はカウンター呪文として扱う

    return scores


# ─── デバッグ用 ──────────────────────────────────────────────

def check_card(card_name: str, oracle_en: str, oracle_ja: str,
               type_line: str) -> list[str]:
    applied = []

    for rule in COUNTER_PENALTY_RULES:
        lang = rule["lang"]
        text = oracle_en.lower() if lang == "en" else oracle_ja.lower()

        must = rule.get("must_contain", [])
        if not all(kw.lower() in text for kw in must):
            continue
        must_or = rule.get("must_or_contain", [])
        if must_or and not any(kw.lower() in text for kw in must_or):
            continue
        must_not = rule.get("must_not_contain", [])
        if any(kw.lower() in text for kw in must_not):
            continue
        applied.append(f"[{rule['label']}] penalty={rule['penalty']}")

    # パーマネントの打ち消し効果はペナルティなし

    return applied


if __name__ == "__main__":
    test_cases = [
        {
            "name": "Hall of Storm Giants（ストームジャイアントの聖堂）",
            "en":   "{T}: Add {C}. Ward {4}. Counter that spell or ability.",
            "ja":   "護法{4}（このクリーチャーが対戦相手がコントロールしている呪文や能力の対象になるたび、そのプレイヤーが{4}を支払わないかぎり、その呪文や能力を打ち消す。）",
            "type": "Land",
        },
        {
            "name": "Counterspell（対抗呪文）",
            "en":   "Counter target spell.",
            "ja":   "呪文１つを対象とする。それを打ち消す。",
            "type": "Instant",
        },
        {
            "name": "No More Lies（喝破）",
            "en":   "Counter target spell unless its controller pays {3}.",
            "ja":   "呪文１つを対象とする。それのコントローラーが{3}を支払わないかぎり、それを打ち消す。",
            "type": "Instant",
        },
        {
            "name": "Draining Whelk（吸収するウェルク）",
            "en":   "Flash. Flying. When this creature enters, counter target spell.",
            "ja":   "瞬速。飛行。吸収するウェルクが場に出たとき、呪文１つを対象とし、それを打ち消す。",
            "type": "Creature",
        },
        {
            "name": "Counterbalance（相殺）",
            "en":   "Whenever an opponent casts a spell, you may reveal the top card of your library. If you do, counter that spell if it has the same mana value.",
            "ja":   "対戦相手が呪文を唱えるたび、あなたはあなたのライブラリーの一番上のカードを公開してもよい。そうしたなら、その呪文のマナ総量が等しい場合、その呪文を打ち消す。",
            "type": "Enchantment",
        },
    ]

    print("=== カウンター呪文ペナルティルール動作確認 ===\n")
    for tc in test_cases:
        rules = check_card(tc["name"], tc["en"], tc["ja"], tc["type"])
        if rules:
            print(f"  ✗ {tc['name']}")
            for r in rules:
                print(f"      → {r}")
        else:
            print(f"  ✓ {tc['name']}（ペナルティなし）")
