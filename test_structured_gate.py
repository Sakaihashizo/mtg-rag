#!/usr/bin/env python
"""test_structured_gate.py — 構造化オンリー直行路の入口ゲート安全試験。

structured_direct_gate()（＝LLM ルーターをスキップして SQL 直行路へ行く判定）が
「発動すべきでないクエリで誤発動しない」ことを検証する。純 Python・LLM 不要・決定的。

ゲートの失敗の向き:
  誤発動（false fire）= 意味・数値・極性を落として間違った答えの集合を返す ＝ 有害
  取り逃し（no fire） = ルーター経由のハイブリッドに落ちるだけ ＝ 無害（遅く・正しく）
よってこの試験は誤発動ゼロを必須とし、取り逃しは記録のみ（辞書の recall 境界の文書化）。
"""
import sys

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_rag_agent import structured_direct_gate

# (クエリ, ゲートが発動してよいか)
CASES = [
    # ─ 発動すべき（辞書で完結するキーワード系）─
    ("飛行を持つクリーチャー", True),
    ("接死を持つクリーチャー", True),
    ("トランプルを持つクリーチャー", True),
    ("速攻を持つクリーチャー", True),
    ("破壊不能を持つクリーチャー", True),
    ("威迫を持つクリーチャー", True),
    ("到達を持つクリーチャー", True),
    ("警戒を持つクリーチャー", True),
    ("絆魂を持つクリーチャー", True),
    ("先制攻撃を持つクリーチャー", True),
    ("飛行持ち", True),
    ("モダンの飛行を持つクリーチャー", True),  # format は search_cards 側で決定的に拾う
    # ─ 極性ひっかけ: 答えは「持ってない側」や「付与する側」＝誤発動したら事故 ─
    ("破壊不能を除去できるカード", False),
    ("飛行を付与するカード", False),
    ("接死を与える装備品", False),
    ("破壊不能を得るクリーチャー", False),
    ("飛行を持たせるオーラ", False),
    ("トランプルを授けるエンチャント", False),
    ("飛行クリーチャーを破壊する除去", False),
    ("速攻クリーチャーを打ち消すカウンター", False),
    # ─ 数値入り: cmc 抽出はルーターの仕事＝スキップしたら制約が落ちる ─
    ("1マナの飛行クリーチャー", False),
    ("3マナ以下の速攻クリーチャー", False),
    ("パワー5以上のトランプル持ち", False),
    ("二マナの接死持ち", False),
    # ─ boost・複合意味: 直行路では強さ判定・意味検索が要る ─
    ("最強の飛行クリーチャー", False),
    ("環境で強い接死持ち", False),
    ("飛行を持つドローできるカード", False),
    ("接死持ちでカードを引くクリーチャー", False),
    ("速攻でマナ加速できるカード", False),
    # ─ 言い換え: 辞書は沈黙してルーターに譲るのが正しい（誤発動でなく取り逃し側）─
    ("空を飛ぶクリーチャー", False),
    ("飛んでいる生物", False),
    ("ブロックされにくいクリーチャー", False),
    ("除去されにくいタフなクリーチャー", False),
    ("flying creature", False),
]


def main():
    false_fire, missed, ok = [], [], 0
    for query, should_fire in CASES:
        fired = structured_direct_gate(query)
        if fired == should_fire:
            ok += 1
            print(f"  OK   {'発動' if fired else '不発'}  {query}")
        elif fired and not should_fire:
            false_fire.append(query)
            print(f"  ** 誤発動（有害・要修正） {query}")
        else:
            missed.append(query)
            print(f"  -- 取り逃し（無害・ルーター行き） {query}")
    print(f"\n合計 {len(CASES)} 件: 期待どおり {ok} / 誤発動 {len(false_fire)} / 取り逃し {len(missed)}")
    if false_fire:
        print("誤発動あり＝直行路が間違った集合を返す。ガード（extract_keywords の極性語・"
              "structured_direct_gate の数字判定）を修正すること。")
        sys.exit(1)
    print("誤発動ゼロ: ゲートは安全（取り逃しはハイブリッド経路が受ける）。")


if __name__ == '__main__':
    main()
