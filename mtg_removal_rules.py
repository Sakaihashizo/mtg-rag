"""
mtg_removal_rules.py — 除去クエリのペナルティルール管理
=========================================================
除去系クエリ（単体除去・全体除去・追放除去等）の検索結果から
「除去ではないカード」を自動的にスコアダウンするルール集。

ルールの追加方法:
  PENALTY_RULES に辞書を追加するだけ。
  各ルールは oracle_text（英語）または japanese_oracle_text（日本語）に対して
  パターンマッチを行い、ヒットした場合に rrf_score を乗算する。

  {
      "label":       "ルールの説明",
      "lang":        "en" または "ja",
      "must_contain": ["必ず含むキーワード（AND条件）"],
      "must_not_contain": ["含んではいけないキーワード（NOT条件）"],（省略可）
      "penalty":     0.5,  # スコアに掛ける係数（1.0未満でペナルティ）
  }
"""

from typing import Optional

# ─── ペナルティルール定義 ─────────────────────────────────────

PENALTY_RULES = [

    # ルール1: 墓地のカードを追放するカード
    # 例: 歩く彫像の攪乱者 "exile target card from an opponent's graveyard"
    {
        "label":        "墓地追放（除去ではない）",
        "lang":         "en",
        "must_contain": ["target card"],
        "penalty":      0.3,
    },

    # ルール2: 自分のクリーチャーを追放・破壊するカード（明滅・自己強化）
    # 例: 魅力的な王子 "あなたがオーナーであるクリーチャー１体を対象とし、それを追放する"
    {
        "label":        "自分のクリーチャーを対象にする追放（除去ではない）",
        "lang":         "ja",
        "must_contain": ["あなたがオーナーである"],
        "penalty":      0.3,
    },

    # ルール3: 自分がコントロールするクリーチャーを追放
    # 例: 儚い存在 "あなたがコントロールしているクリーチャー１体を対象とし、それを追放する"
    {
        "label":        "自分のクリーチャーを対象にする追放（明滅等）",
        "lang":         "ja",
        "must_contain": ["あなたがコントロール", "追放する"],
        "penalty":      0.3,
    },

    # ルール4: 手札・ライブラリーのカードを追放
    # 例: 各種ハンデス追放効果
    {
        "label":        "手札・ライブラリーからの追放（除去ではない）",
        "lang":         "en",
        "must_contain": ["exile"],
        "must_or_contain": ["from hand", "from their hand",
                            "from your library", "from their library",
                            "from the top of"],
        "penalty":      0.4,
    },

    # ルール5: コストとして生け贄にするカード（自分のパーマネント）
    # 例: 用心棒ラクドス "sacrifice another creature"
    {
        "label":        "自分のパーマネントを生け贄にするコスト",
        "lang":         "en",
        "must_contain": ["sacrifice"],
        "must_or_contain": ["as an additional cost",
                            "sacrifice a ", "sacrifice another",
                            "sacrifice this"],
        "must_not_contain": ["target player sacrifices",
                             "target opponent sacrifices",
                             "each player sacrifices",
                             "each opponent sacrifices"],
        "penalty":      0.4,
    },

    # ルール: 自分・プレイヤーへのダメージ（除去ではない）
    {
        "label":        "自分・プレイヤーへのダメージ（除去ではない）",
        "lang":         "en",
        "must_contain": ["deals", "damage"],
        "must_or_contain": ["damage to you", 
                            "damage to target player",
                            "damage to each player",
                            "damage to each opponent",
                            "damage to its controller"],
        "must_not_contain": ["any target", 
                            "target creature",
                            "target planeswalker"],
        "penalty":      0.1,
    },
]


# ─── ペナルティ適用関数 ───────────────────────────────────────

def apply_removal_penalties(scores: dict, removal_mode: bool) -> dict:
    """
    removal_mode=True の場合のみペナルティを適用する。
    scores: {card_name: {"row": {...}, "rrf": float, ...}}
    戻り値: ペナルティ適用後の scores
    """
    if not removal_mode:
        return scores

    for name, data in scores.items():
        oracle_en = (data["row"].get("oracle_text") or "").lower()
        oracle_ja = (data["row"].get("japanese_oracle_text") or "").lower()

        for rule in PENALTY_RULES:
            lang    = rule["lang"]
            text    = oracle_en if lang == "en" else oracle_ja
            penalty = rule["penalty"]

            # must_contain: 全て含む必要がある（AND）
            must = rule.get("must_contain", [])
            if not all(kw.lower() in text for kw in must):
                continue

            # must_or_contain: いずれか1つ含む（OR）- 省略可
            must_or = rule.get("must_or_contain", [])
            if must_or and not any(kw.lower() in text for kw in must_or):
                continue

            # must_not_contain: これらを含む場合はペナルティを与えない
            must_not = rule.get("must_not_contain", [])
            if any(kw.lower() in text for kw in must_not):
                continue

            # ペナルティ適用
            before = data["rrf"]
            data["rrf"] *= penalty

    return scores


# ─── デバッグ用：特定カードのルール適用確認 ─────────────────

def check_card(card_name: str, oracle_en: str, oracle_ja: str) -> list[str]:
    """
    指定カードにどのペナルティルールが適用されるか確認する（デバッグ用）
    """
    applied = []
    for rule in PENALTY_RULES:
        lang    = rule["lang"]
        text    = oracle_en.lower() if lang == "en" else oracle_ja.lower()

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

    return applied


if __name__ == "__main__":
    # 動作確認
    test_cases = [
        {
            "name": "Disruptor Wanderglyph（歩く彫像の攪乱者）",
            "en":   "Whenever this creature attacks, exile target card from an opponent's graveyard.",
            "ja":   "歩く彫像の攪乱者が攻撃するたび、対戦相手の墓地にあるカード１枚を対象とする。それを追放する。",
        },
        {
            "name": "Charming Prince（魅力的な王子）",
            "en":   "Exile another target creature you own. Return it to the battlefield under your control at the beginning of the next end step.",
            "ja":   "他の、あなたがオーナーであるクリーチャー１体を対象とし、それを追放する。次の終了ステップの開始時に、それをあなたのコントロール下で戦場に戻す。",
        },
        {
            "name": "Ephemerate（儚い存在）",
            "en":   "Exile target creature you control, then return it to the battlefield under its owner's control.",
            "ja":   "あなたがコントロールしているクリーチャー１体を対象とし、それを追放する。その後それをオーナーのコントロール下で戦場に戻す。",
        },
        {
            "name": "Fatal Push（致命的な一押し）",
            "en":   "Destroy target creature if it has mana value 2 or less.",
            "ja":   "クリーチャー１体を対象とする。それの点数で見たマナ・コストが２以下であるなら、それを破壊する。",
        },
        {
            "name": "Path to Exile（流刑への道）",
            "en":   "Exile target creature. Its controller may search their library for a basic land card.",
            "ja":   "クリーチャー１体を対象とする。それを追放する。それのコントローラーは、自分のライブラリーから基本土地・カード１枚を探してもよい。",
        },
        {
            "name": "Rakdos, the Muscle（用心棒ラクドス）",
            "en":   "Whenever you sacrifice another creature, exile cards equal to its power from the top of defending player's library.",
            "ja":   "あなたが他のクリーチャーを生け贄に捧げるたび、防御プレイヤーのライブラリーの一番上からそのパワーに等しい枚数のカードを追放する。",
        },
    ]

    print("=== ペナルティルール動作確認 ===\n")
    for tc in test_cases:
        rules = check_card(tc["name"], tc["en"], tc["ja"])
        if rules:
            print(f"  ✗ {tc['name']}")
            for r in rules:
                print(f"      → {r}")
        else:
            print(f"  ✓ {tc['name']}（ペナルティなし）")
