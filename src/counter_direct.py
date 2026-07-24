"""counter_direct.py — 確定カウンター呪文の SQL 直行路（本実装）

2026-07-19 採用ゲート裁定（本人 GO）の実装。カウンター便（案B・2026-07-17 深夜）で
偽ペルソナ「条件付きカウンター呪文」を退役し「確定カウンター呪文」へ置換、その直行化を
プロトタイプ検証（counter_direct_proto_20260719）で採択した:
  直行T = NDCG@10 1.000 ＞ 本番相当 0.825 ＞ 直行B 0.790

removal_direct.py の姉妹モジュール。卒業レジストリ GRADUATED が正本で、三役
（検索ルーティング / eval のキャッシュ免除 / 採点プール除外）が全部ここを参照する。

設計の前提（design ledger 用に明示）:
- 資格は「crisp に定義できる ∧ クエリ単位の実測検証」で与える。カウンターは昨日の
  spell_conditional 類型分割（soft=支払い回避で不確定 / scope=範囲制限で確定を保つ）で
  crisp 化した＝役割 WHERE（target_types に spell）＋確定段ソートで SQL 一本に落ちる。
- 並びレシピ=T（確定段→採用率）: 除去の機能クエリは B（採用率のみ）だったが、
  カウンターは「確実に消せるか」が品質＝soft を採用率で上に出すと沈む（本人の Daze 洞察の
  数値化: B=0.790 は Daze/Pierce が採用率で上位に食い込むため）。役割が違えばレシピが違う。
- 純・能力打ち消し（Stifle 型）は target_types に spell を持たない＝役割 WHERE で自然に
  除外される（R12・0）。誤発動ゼロは test_counter_direct_gate.py が検証。
"""
from typing import Optional

MAINLINE = ['Standard', 'Pioneer', 'Modern', 'Legacy']

# 役割 WHERE: 呪文を対象に取る＝本物のカウンター（護法/Stifle は spell を持たず自然に除外）
ROLE_SQL = "c.target_types && ARRAY['spell']::text[]"

# 卒業レジストリ＝「検証終了クエリ」の正準リスト（2026-07-19 本人 GO・確定カウンター 1 本）。
#   format:    卒業時の検証条件（None=フォーマット無指定で検証）
#   legal_key: legalities のキー（フォーマット門・無指定は vintage）
#   pr_formats: 採用率の分母フォーマット
GRADUATED = {
    '確定カウンター呪文': dict(
        format=None, legal_key='vintage', pr_formats=MAINLINE),
}

_NORM = {q.strip().lower(): spec for q, spec in GRADUATED.items()}


def counter_direct_gate(query: str, fmt: Optional[str] = None) -> Optional[dict]:
    """卒業クエリなら仕様 dict を返す（発動）。それ以外は None（不発）。
    fmt が卒業時の検証条件と食い違うときは発動しない（未検証の組合せ＝安全側）。
    数値・色・条件の残余があるクエリはレジストリ完全一致から外れるため自然に不発。"""
    spec = _NORM.get((query or '').strip().lower())
    if spec is None:
        return None
    if fmt is not None and fmt.lower() != (spec['format'] or ''):
        return None
    return spec


def is_graduated(query: str, fmt: Optional[str] = None) -> bool:
    """採点プール生成の除外判定（卒業＝検証終了・新規採点労務ゼロ化）。"""
    return counter_direct_gate(query, fmt) is not None


_SELECT_COLS = """c.card_name, c.type_line, c.oracle_text,
               c.japanese_name, c.japanese_oracle_text, c.mana_cost, c.rarity"""


def fetch_direct(db, spec, top_k, cards_table='mtg_cards_v2'):
    """確定カウンター直行路（レシピ T = 確定段 → 採用率 → id）。
    確定段 = spell_conditional_soft を持たない（支払いで逃げられない）＝GRADE の主キー。
    soft は下段。除去の姉妹だが ORDER が「機能段→採用率」で除去 B と異なる。"""
    sql = f"""
        SELECT {_SELECT_COLS}
        FROM {cards_table} c
        LEFT JOIN (SELECT card_id, SUM(play_decks) AS pr
                   FROM card_format_strength
                   WHERE format_name = ANY(%s)
                   GROUP BY card_id) s ON s.card_id = c.id
        WHERE (c.legalities->>'vintage') IN ('legal','restricted')
          AND (c.legalities->>%s) IN ('legal','restricted')
          AND {ROLE_SQL}
        ORDER BY
          (c.target_types && ARRAY['spell_conditional_soft']::text[]) ASC,  -- 確定段が先
          COALESCE(s.pr,0) DESC,
          c.id
        LIMIT %s
    """
    return db.query_dicts(sql, (spec['pr_formats'], spec['legal_key'], top_k))
