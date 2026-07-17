"""removal_direct.py — 除去ファミリーの SQL 直行路（本実装）

2026-07-17 明け方の採用ゲート裁定（本人）の実装。検証終了（卒業）した除去クエリは
LLM ルーター・意味検索を通さず SQL 一本で応答する（キーワード直行路と同じ思想）。
卒業レジストリ GRADUATED がこのモジュールの正本で、三役が全部ここを参照する:
  - 検索ルーティング（mtg_hybrid_search_v2.search / search_with_hyde・mtg_rag_agent）
  - eval のルーターキャッシュ免除（eval_framework.run_eval＝直行クエリは Gemini 不要）
  - 採点プール生成の除外（eval_framework.collect_pool＝卒業組は新規採点労務ゼロ）

設計の前提（design ledger 用に明示）:
- 直行路の資格は「クエリ単位の実測検証」で与える（2026-07-17 直行 B 家族平均 0.987・
  未採点ゼロの土俵）。パターン汎化（任意の「◯◯の単体除去」）は検証を持たないため
  発動させない＝レジストリ完全一致のみ。誤発動=有害（間違った答えの集合を確定的に
  返す）・取り逃し=無害（ルーター経由のハイブリッドに落ちる＝遅く・正しく）。
  この非対称性は test_removal_direct_gate.py が検証する。
- 並びレシピはファミリー統一（per-query 最適の寄せ集めは 1 クエリ過適合）:
    機能/フォーマット系 = B: per-format 採用率 DESC → id
    superlative（最強）= 合成: 採用率 × (1+0.25×幅) × 上限cap × (1+β×テンポ)・β=0.6
  レガシーだけは per-query では合成 1.000 > B 0.954 だがレシピ統一を優先
  （要再訪・WORKLOG 2026-07-17 明け方）。
- 「クリーチャーを破壊する除去」は本番残留（本番 0.951 > 直行 0.927・唯一の本番勝ち）
  ＝レジストリに載せない。採点プールにも残り続ける。
"""
from typing import Optional

from role_quality import (breadth, cap_penalty, kill_pred, modes, tempo_gain)

MAINLINE = ['Standard', 'Pioneer', 'Modern', 'Legacy']

# 役割 WHERE（is_creature_removal の SQL 写し・removal_direct_proto_20260715 で検証済みの形）
ROLE_SQL = """
  EXISTS (
    SELECT 1 FROM jsonb_array_elements(c.removal) e
    WHERE (e->>'type') = 'sacrifice'
       OR ((e->>'type') IN ('destroy','exile','tuck')
           AND COALESCE((e->>'permanent')::boolean, true)
           AND ((e->>'object') IN ('creature','permanent')
                OR ((e->>'object') IS NULL
                    AND c.target_types && ARRAY['creature','any','permanent']::text[])))
       OR ((e->>'type') IN ('damage','minus')
           AND c.target_types && ARRAY['creature','any','permanent']::text[])
  )
"""

# 卒業レジストリ＝「検証終了クエリ」の正準リスト（2026-07-17 本人裁定・9 本中 8＋最強）。
#   format:    卒業時の検証条件（None=フォーマット無指定で検証）
#   legal_key: legalities のキー（フォーマット門。無指定クエリは vintage=全大会カード）
#   mechs:     機構門（明示クエリのみ・R5）
#   recipe:    'B'（採用率のみ）| 'composite'（最強＝合成）
#   pr_formats: 採用率の分母フォーマット（フォーマット修飾=単独・無指定=本線 4F SUM）
#   tempo_pop: 合成のテンポ母集団フォーマット（composite のみ）
GRADUATED = {
    'クリーチャーを追放する除去': dict(
        format=None, legal_key='vintage', mechs=('exile',),
        recipe='B', pr_formats=MAINLINE),
    'destroy target creature': dict(
        format=None, legal_key='vintage', mechs=('destroy',),
        recipe='B', pr_formats=MAINLINE),
    'モダンの単体除去': dict(
        format='modern', legal_key='modern', mechs=None,
        recipe='B', pr_formats=['Modern']),
    'スタンダードの単体除去': dict(
        format='standard', legal_key='standard', mechs=None,
        recipe='B', pr_formats=['Standard']),
    'パイオニアの単体除去': dict(
        format='pioneer', legal_key='pioneer', mechs=None,
        recipe='B', pr_formats=['Pioneer']),
    'レガシーの単体除去': dict(
        format='legacy', legal_key='legacy', mechs=None,
        recipe='B', pr_formats=['Legacy']),
    'ヴィンテージの単体除去': dict(
        format='vintage', legal_key='vintage', mechs=None,
        recipe='B', pr_formats=['Vintage']),
    'パウパーの単体除去': dict(
        format='pauper', legal_key='pauper', mechs=None,
        recipe='B', pr_formats=['Pauper']),
    '最強の単体除去': dict(
        format=None, legal_key='vintage', mechs=None,
        recipe='composite', pr_formats=MAINLINE, tempo_pop='Modern'),
}

# 照合は前後空白と大小文字だけ吸収（英語クエリ用）。それ以上の正規化はしない
_NORM = {q.strip().lower(): spec for q, spec in GRADUATED.items()}


def removal_direct_gate(query: str, fmt: Optional[str] = None) -> Optional[dict]:
    """卒業クエリなら仕様 dict を返す（発動）。それ以外は None（不発）。
    fmt が明示され、卒業時の検証条件（spec['format']）と食い違うときは発動しない
    （未検証の組合せ＝ハイブリッド経路に落とす安全側。例: format='modern' 付きの
    「destroy target creature」はフォーマット無指定でしか検証していない）。"""
    spec = _NORM.get((query or '').strip().lower())
    if spec is None:
        return None
    if fmt is not None and fmt.lower() != (spec['format'] or ''):
        return None
    return spec


def is_graduated(query: str, fmt: Optional[str] = None) -> bool:
    """採点プール生成の除外判定（卒業＝検証終了・新規採点労務ゼロ化）。"""
    return removal_direct_gate(query, fmt) is not None


_SELECT_COLS = """c.card_name, c.type_line, c.oracle_text,
               c.japanese_name, c.japanese_oracle_text, c.mana_cost, c.rarity"""

_PR_JOIN = """
    LEFT JOIN (SELECT card_id, SUM(play_decks) AS pr
               FROM card_format_strength
               WHERE format_name = ANY(%s)
               GROUP BY card_id) s ON s.card_id = c.id
"""

_POP_CACHE: dict = {}


def _mech_sql(spec) -> str:
    if not spec['mechs']:
        return ''
    ms = ", ".join(f"'{m}'" for m in spec['mechs'])
    return f" AND c.removal_types && ARRAY[{ms}]::text[]"


def _fetch_b(db, spec, top_k, cards_table):
    sql = f"""
        SELECT {_SELECT_COLS}
        FROM {cards_table} c
        {_PR_JOIN}
        WHERE (c.legalities->>'vintage') IN ('legal','restricted')
          AND (c.legalities->>%s) IN ('legal','restricted')
          AND {ROLE_SQL}{_mech_sql(spec)}
        ORDER BY COALESCE(s.pr,0) DESC, c.id
        LIMIT %s
    """
    return db.query_dicts(sql, (spec['pr_formats'], spec['legal_key'], top_k))


def _population(db, fmt_name, cards_table):
    """テンポ母集団: そのフォーマットの実戦クリーチャー分布 [(mv, toughness, weight)]。
    (mv, toughness) で集約済み（tempo_gain は重みに線形＝結果は非集約と同一）。"""
    key = (fmt_name, cards_table)
    if key not in _POP_CACHE:
        rows = db.query_dicts(f"""
            SELECT c.cmc AS mv, c.toughness AS t, SUM(s.play_decks) AS w
            FROM card_format_strength s
            JOIN {cards_table} c ON c.id = s.card_id
            WHERE s.format_name = %s
              AND c.type_line ILIKE '%%Creature%%'
              AND c.toughness ~ '^[0-9]+$'
            GROUP BY c.cmc, c.toughness
        """, (fmt_name,))
        _POP_CACHE[key] = [(float(r['mv'] or 0), int(r['t']), int(r['w']))
                           for r in rows]
    return _POP_CACHE[key]


def _fetch_composite(db, spec, top_k, cards_table):
    """合成順（superlative）: 採用率 × (1+0.25×幅) × 上限cap × (1+0.6×テンポ)。
    採用率 0 は合成値 0＝top10 に届かないため pr>0 に絞ってから Python で採点する
    （層2 の幅/テンポは SQL に書けない・removal_regrade_pool_20260717 の comp と同式）。"""
    sql = f"""
        SELECT {_SELECT_COLS},
               c.removal, c.target, c.target_types, c.cmc, c.floor_cmc,
               COALESCE(s.pr,0) AS pr
        FROM {cards_table} c
        {_PR_JOIN}
        WHERE (c.legalities->>'vintage') IN ('legal','restricted')
          AND (c.legalities->>%s) IN ('legal','restricted')
          AND {ROLE_SQL}{_mech_sql(spec)}
          AND COALESCE(s.pr,0) > 0
    """
    rows = db.query_dicts(sql, (spec['pr_formats'], spec['legal_key']))
    pop = _population(db, spec['tempo_pop'], cards_table)

    def comp(r):
        b = breadth(r['removal'], r['target'], r['target_types'])
        t = max((tempo_gain(cost, kill_pred(e, r['target']), pop)
                 for cost, e in modes(r['removal'], r['cmc'], r['floor_cmc'])),
                default=0.0)
        return float(r['pr']) * (1 + 0.25 * b) * cap_penalty(r['removal']) \
            * (1 + 0.6 * t)

    rows.sort(key=lambda r: (-comp(r), r['card_name']))
    return rows[:top_k]


def fetch_direct(db, spec, top_k, cards_table='mtg_cards_v2'):
    """直行路の実行。戻り値は表示列 dict のリスト（呼び出し側が CardResult に包む）。"""
    if spec['recipe'] == 'composite':
        return _fetch_composite(db, spec, top_k, cards_table)
    return _fetch_b(db, spec, top_k, cards_table)
