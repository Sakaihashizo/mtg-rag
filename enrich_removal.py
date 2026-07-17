#!/usr/bin/env python
"""enrich_removal.py — oracle から除去メカ・対象型を導出して mtg_cards_v2 に列として持つ。

設計（2026-07-06・本人×Fable）:
  target_types text[]  … 正規化した対象型＋クエリ頻出 qualifier トークン
                          (creature/permanent/artifact/enchantment/land/planeswalker/
                           player/spell/any + creature_spell/noncreature_spell)
                          → 絞り込み・GIN・配列統計(most_common_elems)用。%creature% の
                          先頭ワイルドカード LIKE を避け、noncreature 偽陽性を殺す。
  target jsonb         … フル句＋qualifier（例 "nonblack creature"）→ R2 条件付き判定と長い尾。
  removal_types text[] … メカ種別 destroy/exile/damage/minus/sacrifice/bounce → is_removal 相当・絞り込み。
  removal jsonb        … 詳細 {type,object,amount,stat,permanent,targeted} → 順位づけ・恒久性。
不在は NULL（番兵禁止・data_handling 規約）。導出列なので reembed 不要。
冪等: 再実行で全件上書き。新セット取り込み後はこれを再実行して populate（索引はデータに自動追随）。
"""
import re, sys
import psycopg2
from psycopg2.extras import Json, execute_batch
from db_config import get_db_config

TT = ["creature", "permanent", "artifact", "enchantment", "land",
      "planeswalker", "player", "spell"]


def strip_reminder(t):
    return re.sub(r'\([^)]*\)', '', t or '')


def castable_oracle(oracle_text, card_faces_json):
    """役割列の導出に使うテキスト＝「手札から唱えられる面」だけの oracle。
    規則は face_cmcs/face_types と同一（mana_cost 非空の面のみ・全面空なら表面
    フォールバック・2026-07-13 本人の言語化を流用）。前提の明示（design-premise）:
    従来は全文（裏面込み）をパースしており、変身カードの唱えられない裏面にしか無い
    destroy/exile が役割タグに混ざって機構ゲートを通していた（2026-07-15 本人指摘・
    Elesh Norn の destroy=裏面英雄譚 III 章のみ、で実証）。単面カードは従来どおり全文
    ＝導出結果も不変。全面 castable（split/adventure/MDFC）は ' // ' 連結＝oracle_text
    と同形＝これも不変。"""
    faces = card_faces_json or []
    if not isinstance(faces, list) or len(faces) < 2:
        return oracle_text
    castable = [f for f in faces if (f.get('mana_cost') or '').strip()]
    if not castable:
        castable = faces[:1]
    return ' // '.join((f.get('oracle_text') or '') for f in castable)


def find_types(p):
    return [t for t in TT if re.search(r'\b' + t + r's?\b', p)]


# 対象句のトークン: アポストロフィ・数字込み（2026-07-15 修理⑤: 紅蓮破の
# "if it's blue" が旧クラス [a-z\-] のアポストロフィで句切れて青が消えていた）
TOK = r"[a-z0-9'\-]+"


def parse(oracle):
    t = strip_reminder(oracle).replace('\n', ' ')
    tl = t.lower()
    target_types, target_detail = set(), []
    # リスト列挙（A, B, or C）を丸ごと取る（2026-07-15 修理④: 失せろの
    # 「クリーチャー、エンチャント、PW」の 2 項目以降が消えていた）
    for m in re.finditer(
            rf"target ({TOK}(?:[, ]+(?:or )?{TOK}){{0,10}})", tl):
        phrase = m.group(1).strip().rstrip(',')
        words = [w.strip(',') for w in phrase.split()]
        if 'spell' in words:
            target_types.add('spell')
            i = words.index('spell')
            q = words[i - 1] if i > 0 else None
            if q == 'creature':
                target_types.add('creature_spell')
            elif q == 'noncreature':
                target_types.add('noncreature_spell')
            target_detail.append({"phrase": ' '.join(words[:i + 1]), "type": "spell",
                                  "qualifier": (q if q not in (None, 'target') else None)})
            # 条件付きカウンター判定（R12 の4類型のうち機械化できる3つ・2026-07-07）:
            #  (a) 対象制限 = spell の前後に修飾語。前置=「noncreature spell」型／
            #      後置=「spell that targets ...」「spell with mana value ...」型
            #  (b) MV 制限 = "spell with mana value" 型（後置に含まれる）
            #  (c) ソフト  = "unless ... pays" 型（Mana Leak / Force Void 系）
            # 状態依存（"if you control..."）は言い回しが多様なので v1 では拾わない（過小
            # 検出側に倒れる＝条件付きが plain 扱いになるだけ・偽陽性は出ない）。
            # ピッチ等の代替コストは「唱えること」への条件＝打ち消しは無条件（R12・FoW）
            # なので、ここでは counter 句の修飾と unless だけを見る。
            if (q not in (None, 'target')
                    or i + 1 < len(words)
                    or re.search(r'unless[^.]{0,60}pays?', tl)):
                target_types.add('spell_conditional')
        else:
            # or/カンマの型別名列挙は broadening＝qualifier に混ぜない（修理④）。
            # 各セグメントの主型を 1 語ずつ除いた残りだけが qualifier
            # （nonartifact / if it's blue / an opponent controls / 複合型の余り）。
            segs = [s.strip() for s in
                    re.split(r',\s*(?:or\s+)?|\s+or\s+', phrase) if s.strip()]
            seg_prims, leftovers = [], []
            for seg in segs:
                sts = find_types(seg)
                if not sts:
                    leftovers.append(seg)
                    continue
                prim = sts[0]
                seg_prims.append(prim)
                sw = seg.split()
                k = next((i for i, w in enumerate(sw)
                          if re.match(r'^' + prim + r's?$', w)), None)
                if k is not None:
                    sw = sw[:k] + sw[k + 1:]
                if sw:
                    leftovers.append(' '.join(sw))
            if seg_prims:
                target_types.update(seg_prims)
                qual = ' '.join(leftovers).strip()
                detail = {"phrase": phrase, "type": seg_prims[0],
                          "qualifier": qual or None}
                if len(seg_prims) > 1:
                    detail["alts"] = seg_prims[1:]
                target_detail.append(detail)
    if 'any target' in tl:
        target_types.add('any')

    removal = []

    def obj(v):
        """(第一クラス, 全クラス list|None, 対象句の生テキスト)。リスト列挙対応（修理④）"""
        m = re.search(v + rf' (?:another )?(?:target |all |each |up to \w+ )?'
                          rf'({TOK}(?:[, ]+(?:or )?{TOK}){{0,6}})', tl)
        phrase = m.group(1) if m else ''
        ts = find_types(phrase)
        return (ts[0] if ts else None), (ts if len(ts) > 1 else None), phrase

    # 領域ガード（2026-07-17・錨=アガサの魂の大釜）: 「card from a graveyard」等の
    # 墓地/ライブラリ/手札のカード操作は盤面除去でない（bounce/tuck の既存ガードと同族）。
    ZONE_RE = re.compile(r'\b(cards?|graveyards?|library|hand)\b')

    def _entry(typ, first, alls, **kw):
        e = {"type": typ, "object": first, **kw}
        if alls:
            e["objects"] = alls   # 複数クラス時のみ（幅=異なり数の材料・修理⑦）
        return e

    def _is_targeted(verb):
        """「up to one (other) target creature」も対象を取る（2026-07-17・Solitude 錨）"""
        return bool(re.search(
            verb + r' (?:another )?(?:up to \w+ )?(?:other )?target', tl))

    m = re.search(r'destroy (?:another )?(target|all|each|up to)', tl)
    if m:
        o1, oa, oph = obj("destroy")
        if not ZONE_RE.search(oph):
            removal.append(_entry("destroy", o1, oa,
                                  targeted=_is_targeted("destroy"), permanent=True))
    m = re.search(r'exile (?:another )?(target|all|each|up to)', tl)
    if m:
        # ブリンク（追放して戦場に戻す＝除去でない・R1で0）は permanent:false。
        # 「until end of turn / until the next」型に加えて「(then) return it/that card/
        # those cards/them to the battlefield」型を検知する。アンカー型の
        # 「When ~ leaves the battlefield, return the exiled card ...」は代名詞でなく
        # "the exiled card" なのでここに掛からない＝恒久寄りのまま（R1 で 1〜2）。
        blink = re.search(r'return (it|that card|those cards|them) to the battlefield', tl)
        o1, oa, oph = obj("exile")
        if not ZONE_RE.search(oph):
            removal.append(_entry("exile", o1, oa,
                                  targeted=_is_targeted("exile"),
                                  permanent=not (blink or re.search(
                                      r'exile[^.]*until (end of turn|the next)', tl))))
    m = re.search(r'deals? (\d+|x) damage', tl)
    if m:
        # targeted 判定拡張（2026-07-17・錨=激情）: 「divided as you choose among
        # ... target creatures」の割り振り構文も対象を取る。従来の 2 パターンに加え
        # 「damage と target が同文内で近接」を捕捉（全体火力 each/all は含まれない）
        dmg_targeted = bool(
            'any target' in tl or 'to target' in tl
            or re.search(r'deals? (?:\d+|x) damage[^.]{0,60}\btargets?\b', tl))
        removal.append({"type": "damage",
                        "amount": (m.group(1).upper() if m.group(1) == 'x' else int(m.group(1))),
                        "targeted": dmg_targeted})
    m = re.search(r'[-−]\s?(\d+|x)/[-−]\s?(\d+|x)', tl)
    if m and (m.group(2) == 'x' or (m.group(2).isdigit() and int(m.group(2)) > 0)):
        # targeted フラグ追加（2026-07-17・錨=税血の収穫者）: R2'補足a の
        # 「対象を取る minus は機構不問クエリで 2」の機械化材料
        removal.append({"type": "minus", "stat": "toughness",
                        "amount": (m.group(2).upper() if m.group(2) == 'x' else int(m.group(2))),
                        "targeted": bool(re.search(
                            r'target creature[^.]{0,40}gets? [-−]', tl)),
                        "permanent": 'until end of turn' not in tl})
    if re.search(r'(player|opponent)s?\s+sacrifices?', tl):
        removal.append({"type": "sacrifice", "targeted": 'target player' in tl})
    # bounce=盤面のパーマネントを手札に戻す。所有格 "owner's hand" があるので hand は別途
    # in-check。「target ... card from graveyard/exile ...」＝墓地/追放からの回収はバウンスで
    # ないので、対象句（return target と to の間）に card/graveyard/exile があれば除外。
    mb = re.search(r'return target ([a-z\- ]*?) to (?:its owner|their|your)', tl)
    if mb and 'hand' in tl and not re.search(r'\b(card|graveyard|exile)\b', mb.group(1)):
        removal.append({"type": "bounce", "targeted": True, "permanent": False})
    # tuck=対象をライブラリの上/下へ送る or シャッフルして戻す（本人分類: バウンスより硬い＝除去）。
    # 墓地からの戻し（target ... card ... graveyard）は tuck でないので除外。
    mt = re.search(r'(?:put|shuffle) target ([a-z\- ]*?) '
                   r'(?:on (?:the )?(?:top|bottom) of|into) [^.]*?library', tl)
    if mt and not re.search(r'\b(card|graveyard|exile)\b', mt.group(1)):
        span = tl[mt.start():mt.end()]
        where = 'bottom' if 'bottom' in span else ('top' if 'top' in span else 'shuffle')
        removal.append({"type": "tuck", "targeted": True, "where": where,
                        "permanent": where != 'top'})

    # モード分解（2026-07-15 修理②・錨=Burst Lightning）: キッカーで効果が伸びる
    # ダメージは「そのマナ込みの別モード」として追加エントリ化。
    # extra_cost = キッカーの点数（実効コスト = cmc + extra_cost）。
    mk = re.search(r'kicker\s*(?:—|-)?\s*((?:\{[^}]+\})+)', tl)
    if mk and 'was kicked' in tl:
        md = re.search(r'kicked[^.]{0,80}?deals? (\d+) damage', tl)
        if md:
            removal.append({"type": "damage", "amount": int(md.group(1)),
                            "targeted": ('any target' in tl or 'to target' in tl),
                            "extra_cost": _mana_value(mk.group(1))})

    # 追加コストの条件化（2026-07-15 修理⑥・錨=Bone Splinters）: R2 の
    # 「下振れ・対称コスト付き＝条件」の写し。無条件を名乗れなくする印。
    if removal and re.search(r'as an additional cost to cast this (?:spell|card)', tl):
        for e in removal:
            e['add_cost'] = True

    r_types = sorted({r["type"] for r in removal})
    return (sorted(target_types) or None, target_detail or None,
            r_types or None, removal or None)


def _mana_value(sym: str) -> int:
    """'{4}{R}{R}' → 6。数値は加算・色/混成/C は 1 と数える"""
    total = 0
    for s in re.findall(r'\{([^}]+)\}', sym):
        total += int(s) if s.isdigit() else 1
    return total


def _colored_mv(mana_cost: str) -> int:
    """色拘束ぶんの点数（コスト軽減で割り込めない床）。X/Y は数えない"""
    return sum(1 for s in re.findall(r'\{([^}]+)\}', mana_cost or '')
               if not s.isdigit() and s.upper() not in ('X', 'Y'))


def floor_cost(oracle: str, mana_cost: str, cmc) -> float | None:
    """実効コストの床値（2026-07-15 修理②・錨=力線の束縛=1）。
    コスト軽減の仕組みはカードに書いてある＝ベストケースは内在。
    軽減なしのカードは None（不在は NULL・番兵禁止）。"""
    if not mana_cost:
        return None
    tl = strip_reminder(oracle or '').replace('\n', ' ').lower()
    red = re.search(r'costs \{(\d+)\} less to cast for each', tl)
    if red:
        if 'basic land type' in tl:   # 版図: 最大 5 タイプ
            return max(_colored_mv(mana_cost),
                       float(cmc or 0) - int(red.group(1)) * 5)
        return float(_colored_mv(mana_cost))   # 親和系: 無制限軽減→色拘束が床
    if re.search(r'\bdelve\b|affinity for', tl):
        return float(_colored_mv(mana_cost))
    # 想起（2026-07-16 追補・錨=Solitude②が合成順位から落ちた件）:
    # マナの想起コストはその点数が床・ピッチ想起（手札から追放）はマナ 0 が床。
    # カードを失うコストはマナ軸の外＝層3（採用率）が織り込む（層設計文書の分業）。
    me = re.search(r'evoke\s*(?:—|-)?\s*((?:\{[^}]+\})+)', tl)
    if me:
        return float(_mana_value(me.group(1)))
    if re.search(r'evoke\s*(?:—|-)?\s*(?:exile|sacrifice|discard|pay)', tl):
        return 0.0
    # ピッチ呪文（FoW 型: 代替コストで唱える）: 代替文中のマナ記号の合計が床（無ければ 0）
    mp = re.search(r'you may [^.]{0,100}? rather than pay (?:this spell\'s|its) mana cost',
                   tl)
    if mp:
        return float(_mana_value(mp.group(0)))
    return None


DDL = """
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS target_types  text[];
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS target        jsonb;
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS removal_types text[];
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS removal       jsonb;
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS floor_cmc     numeric;
"""
IDX = """
CREATE INDEX IF NOT EXISTS mtg_cards_v2_target_types_gin  ON mtg_cards_v2 USING gin (target_types);
CREATE INDEX IF NOT EXISTS mtg_cards_v2_removal_types_gin ON mtg_cards_v2 USING gin (removal_types);
"""


def main():
    cfg = get_db_config()
    conn = psycopg2.connect(**cfg)
    cur = conn.cursor()
    for stmt in DDL.strip().split(';'):
        if stmt.strip():
            cur.execute(stmt)
    conn.commit()

    # 面対応（2026-07-15）: 導出入力を「唱えられる面」に限定。単面は結果不変なので、
    # 書き込みは値が実際に変わる行だけ（無駄な全行 UPDATE の物理チャーン回避）。
    cur.execute("""SELECT id, oracle_text, card_faces_json, mana_cost, cmc,
                          target_types, target, removal_types, removal, floor_cmc
                   FROM mtg_cards_v2""")
    rows = cur.fetchall()
    updates = []
    for cid, ot, cfj, mc, cmc, cur_tt, cur_td, cur_rt, cur_rem, cur_fc in rows:
        tt, td, rt, rem = parse(castable_oracle(ot, cfj))
        fc = floor_cost(ot, mc, cmc)
        new = (tt or None, td or None, rt or None, rem or None, fc)
        old = (cur_tt or None, cur_td or None, cur_rt or None, cur_rem or None,
               float(cur_fc) if cur_fc is not None else None)
        if new == old:
            continue
        updates.append((tt, Json(td) if td else None, rt,
                        Json(rem) if rem else None, fc, cid))
    print(f"値が変わる行: {len(updates)} 件（他はスキップ）")
    execute_batch(cur,
                  "UPDATE mtg_cards_v2 SET target_types=%s, target=%s, removal_types=%s, removal=%s, floor_cmc=%s WHERE id=%s",
                  updates, page_size=1000)
    conn.commit()

    for stmt in IDX.strip().split(';'):
        if stmt.strip():
            cur.execute(stmt)
    conn.commit()
    cur.execute("ANALYZE mtg_cards_v2")
    conn.commit()

    cur.execute("SELECT count(*) FROM mtg_cards_v2 WHERE removal_types IS NOT NULL")
    n_removal = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM mtg_cards_v2 WHERE target_types IS NOT NULL")
    n_target = cur.fetchone()[0]
    print(f"populate 完了: 全{len(rows)}件 / removal あり {n_removal} / target あり {n_target}")
    conn.close()


if __name__ == "__main__":
    main()
