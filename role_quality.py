"""role_quality.py — 層2（機能品質）の計算部品（2026-07-15 修理便）

設計文書: docs/ai/search_quality_layers_20260715.md
入力は enrich_removal.py が populate した構造化列（removal/target jsonb・
target_types・floor_cmc）。フォーマット実勢は使わない（層2＝カード内在）。
例外＝enablement 辞書（(メカニズム, フォーマット) の対・人間管理・本人裁定待ちの草案）。
"""
REMOVABLE_CLASSES = {'creature', 'artifact', 'enchantment', 'planeswalker', 'land'}
COLOR_WORDS = ('white', 'blue', 'black', 'red', 'green')

# enablement 辞書 v1（草案・本人 ratification 待ち・2026-07-15）
# 'free' = デッキ構造が無料で満たす／'work' = 構築を曲げるか追加の仕事が要る
# キッカー等のマナ払い条件は辞書対象外（extra_cost モードとして評価する）
ENABLEMENT = {
    ('revolt', 'Modern'): 'free', ('revolt', 'Legacy'): 'free',
    ('revolt', 'Vintage'): 'free', ('revolt', 'Pauper'): 'free',
    ('revolt', 'Standard'): 'work', ('revolt', 'Pioneer'): 'work',
    ('delirium', 'Modern'): 'free', ('delirium', 'Legacy'): 'free',
    ('delirium', 'Vintage'): 'free',
    ('delirium', 'Standard'): 'work', ('delirium', 'Pioneer'): 'work',
}


def _entry_classes(e, target_types):
    """1 エントリが恒久的に討てるクラス集合。permanent 全域は REMOVABLE_CLASSES"""
    typ = e.get('type')
    tt = set(target_types or [])
    if typ in ('destroy', 'exile', 'tuck', 'sacrifice'):
        if e.get('permanent') is False:
            return set()
        objs = e.get('objects') or ([e.get('object')] if e.get('object') else [])
        s = set()
        for o in objs:
            if o == 'permanent':
                return set(REMOVABLE_CLASSES)
            if o in REMOVABLE_CLASSES:
                s.add(o)
        if not s and tt & {'creature', 'any'}:
            s.add('creature')
        return s
    if typ in ('damage', 'minus'):
        s = set()
        if tt & {'creature', 'any'}:
            s.add('creature')
        if 'planeswalker' in tt or 'any' in tt:
            s.add('planeswalker')
        return s
    return set()


def _color_restricted(target_detail, cls_type):
    for t in (target_detail or []):
        if t.get('type') == cls_type or cls_type in (t.get('alts') or []):
            q = t.get('qualifier') or ''
            if any(c in q for c in COLOR_WORDS):
                return True
    return False


def breadth(removal, target_detail, target_types):
    """役割内万能性の幅（1〜4）。修理⑦: 討てるクラスの異なり数＋色限定の割引。
    虹色の終焉=4（nonland/MV 条件は幅を削らない）・紅蓮破=2（青限定で半減）・
    削剥=2（creature+artifact）・失せろ=3・Murder=1"""
    classes, perm_hit = set(), False
    for e in (removal or []):
        cs = _entry_classes(e, target_types)
        if cs == REMOVABLE_CLASSES:
            perm_hit = True
        classes |= cs
    b = 4 if perm_hit else min(len(classes), 4)
    if perm_hit and _color_restricted(target_detail, 'permanent'):
        b = max(1, b // 2)
    return max(b, 1) if (removal or []) else 0


def purity(removal, target_detail):
    """(clean, uncond, no_cap, targeted) — 監査・機能クエリの並び用。
    修理⑥: add_cost（Bone Splinters）は uncond を名乗れない"""
    rem = removal or []
    clean = any(e.get('type') in ('destroy', 'exile')
                and e.get('permanent') is not False
                and e.get('targeted') for e in rem)
    has_add_cost = any(e.get('add_cost') for e in rem)
    uncond = (not has_add_cost) and any(
        t.get('qualifier') is None and t.get('type') in ('creature', 'permanent')
        for t in (target_detail or []))
    no_cap = any(e.get('type') in ('destroy', 'exile', 'tuck', 'sacrifice')
                 or (e.get('type') == 'damage' and e.get('amount') == 'X')
                 for e in rem)
    targeted = any(e.get('targeted') for e in rem)
    return clean, uncond, no_cap, targeted


def cap_penalty(removal):
    """上限ペナルティ: 除去手段が固定値ダメージのみ → 0.85"""
    rem = removal or []
    if any(e.get('type') in ('destroy', 'exile', 'tuck', 'sacrifice')
           or (e.get('type') == 'damage' and e.get('amount') == 'X')
           for e in rem):
        return 1.0
    return 0.85 if rem else 1.0


def modes(removal, cmc, floor_cmc):
    """(実効コスト, エントリ) の列挙（修理②）。
    キッカー等 extra_cost 持ちは cmc+extra のモード・床値があれば床値を使う"""
    base = float(floor_cmc) if floor_cmc is not None else float(cmc or 0)
    out = []
    for e in (removal or []):
        cost = base + float(e.get('extra_cost') or 0)
        out.append((cost, e))
    return out


def tempo_gain(mode_cost, kills, population):
    """期待マナ利得 = Σ 重み×max(0, 脅威MV−実効コスト) [討てる脅威] / Σ 重み。
    population: [(mv, toughness, weight)]・kills: (mv, toughness)->bool"""
    total_w = sum(w for _, _, w in population) or 1
    gain = sum(w * max(0.0, mv - mode_cost)
               for mv, t, w in population if kills(mv, t))
    return gain / total_w
