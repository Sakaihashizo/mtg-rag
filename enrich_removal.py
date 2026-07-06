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


def find_types(p):
    return [t for t in TT if re.search(r'\b' + t + r's?\b', p)]


def parse(oracle):
    t = strip_reminder(oracle).replace('\n', ' ')
    tl = t.lower()
    target_types, target_detail = set(), []
    for m in re.finditer(r'target ([a-z\-]+(?: [a-z\-]+){0,4})', tl):
        phrase = m.group(1).strip()
        words = phrase.split()
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
        else:
            ts = find_types(phrase)
            if ts:
                base = ts[0]
                target_types.update(ts)
                idx = next((k for k, w in enumerate(words)
                            if re.match(r'^' + base + r's?$', w)), len(words) - 1)
                # 後置修飾（you control / an opponent controls 等）も qualifier に保持する。
                # 型の単語で切り詰めると「creature you control」の you control（自陣対象＝
                # 除去でない判定の材料）が消えるため。
                qual = ' '.join(words[:idx] + words[idx + 1:]).strip()
                target_detail.append({"phrase": phrase, "type": base,
                                      "qualifier": qual or None})
    if 'any target' in tl:
        target_types.add('any')

    removal = []

    def obj(v):
        m = re.search(v + r' (?:another )?(?:target |all |each |up to \w+ )?((?:[a-z\-]+ ?){1,3})', tl)
        ts = find_types(m.group(1)) if m else []
        return ts[0] if ts else None

    m = re.search(r'destroy (?:another )?(target|all|each|up to)', tl)
    if m:
        removal.append({"type": "destroy", "targeted": m.group(1) == "target",
                        "object": obj("destroy"), "permanent": True})
    m = re.search(r'exile (?:another )?(target|all|each|up to)', tl)
    if m:
        # ブリンク（追放して戦場に戻す＝除去でない・R1で0）は permanent:false。
        # 「until end of turn / until the next」型に加えて「(then) return it/that card/
        # those cards/them to the battlefield」型を検知する。アンカー型の
        # 「When ~ leaves the battlefield, return the exiled card ...」は代名詞でなく
        # "the exiled card" なのでここに掛からない＝恒久寄りのまま（R1 で 1〜2）。
        blink = re.search(r'return (it|that card|those cards|them) to the battlefield', tl)
        removal.append({"type": "exile", "targeted": m.group(1) == "target", "object": obj("exile"),
                        "permanent": not (blink or re.search(
                            r'exile[^.]*until (end of turn|the next)', tl))})
    m = re.search(r'deals? (\d+|x) damage', tl)
    if m:
        removal.append({"type": "damage",
                        "amount": (m.group(1).upper() if m.group(1) == 'x' else int(m.group(1))),
                        "targeted": ('any target' in tl or 'to target' in tl)})
    m = re.search(r'[-−]\s?(\d+|x)/[-−]\s?(\d+|x)', tl)
    if m and (m.group(2) == 'x' or (m.group(2).isdigit() and int(m.group(2)) > 0)):
        removal.append({"type": "minus", "stat": "toughness",
                        "amount": (m.group(2).upper() if m.group(2) == 'x' else int(m.group(2))),
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

    r_types = sorted({r["type"] for r in removal})
    return (sorted(target_types) or None, target_detail or None,
            r_types or None, removal or None)


DDL = """
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS target_types  text[];
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS target        jsonb;
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS removal_types text[];
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS removal       jsonb;
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

    cur.execute("SELECT id, oracle_text FROM mtg_cards_v2")
    rows = cur.fetchall()
    updates = []
    for cid, ot in rows:
        tt, td, rt, rem = parse(ot)
        updates.append((tt, Json(td) if td else None, rt, Json(rem) if rem else None, cid))
    execute_batch(cur,
                  "UPDATE mtg_cards_v2 SET target_types=%s, target=%s, removal_types=%s, removal=%s WHERE id=%s",
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
