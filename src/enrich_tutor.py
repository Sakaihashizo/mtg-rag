#!/usr/bin/env python3
"""tutor / dig 列の導出 — 「サーチ」と「濾過」を分けて持つ（2026-07-31 本人裁定）。

経緯: ablation（腕の切除実験）で「土地をサーチするカード」が埋め込みなしだと
1.000→0.000 になり、辞書では届かないことが判明（GT の grade2 12 枚のうち 3 枚が
`search` の字面を持たない: Cartographer's Survey・Herd Migration・Slimefoot's Survey）。
本人の設計案「サーチ列と分けて、何枚見て何枚取るか・残りの行き先・拾えるカード
タイプを持つ濾過列を作る」を実装する。

## 二つの列（本人裁定・境界は「位置を知っているか」）

- **tutor jsonb** = ライブラリ**全域**に条件でアクセスする（位置不問・通常シャッフル）
- **dig jsonb**   = **上から N 枚**という位置に縛られる（占術・諜報・切削して拾う型を含む）

一枚が両方を持つことがある（実例 Slimefoot's Survey: サーチ 2 枚戦場へ ＋ 上から X 枚
見て 1 枚残し底へ）。**だから列を分ける**——removal 列で踏んだ「効果と対象が結びつかず
複数効果の札だけ壊れる」（PHASE2 §14）を設計時点で回収する。

## 効果オブジェクトの形（配列＝一つの効果に属する情報を一つの束に）

tutor: {"pick": ["land"|"creature"|"basic_land"|"any"|...],  # 拾えるカードタイプ
        "count": N | null,          # 取る枚数（"up to X"・任意枚数は null＝不定）
        "dest": "battlefield"|"hand"|"graveyard"|"top"|"exile",
        "site": "spell"|"activated"|"triggered"}   # 在り処（本業か付随かの将来の軸・
                                                   #  Fable の死角 (b) を先に受ける）
dig:   {"look": N | null,           # 見る枚数
        "take": N | null,           # 取る枚数（占術/諜報は 0）
        "pick": [...],              # 拾えるカードタイプ（制限なしは ["any"]）
        "dest": "hand"|"battlefield"|"graveyard",   # 取ったカードの行き先
        "rest": "graveyard"|"bottom"|"top"|"random"|null,  # 残りの行き先
        "via_mill": true/false,     # 切削してから拾う型（Cache Grab＝蓄え放題型。
                                    #  墓地に「落ちてから」拾う＝落魄/昂揚が誘発する
                                    #  機能差が実在するので旗を立てる）
        "site": ...}

**「蓄え放題型」は専用の型を作らない**（本人案の要点）——`rest="graveyard"` という値で
表す。同じ器で占術（take=0・rest=top/bottom）も諜報（take=0・rest=graveyard）も
Impulse（look4/take1/dest=hand/rest=bottom）も Dig Through Time（look7/take2/
rest=graveyard）も表現できる＝族が増えるたびに列を増やす未来を防ぐ。

不在は NULL（番兵禁止）。導出は冪等（値が変わる行だけ UPDATE）。

## 門は別便（列は豊かに・門は貧しく）

「土地をサーチするカード」の門は **tutor.pick ∋ land ∪ dig.pick ∋ land の和集合**
（本人裁定）。採点は目的ベース・列は機構ベースなので、門を片方に縛ると
Cartographer's Survey（濾過型だが grade 2）が落ちて採点と正面衝突する。
この便では列だけ作り、門と直行路は測ってから別便で入れる（列→並べ方の順）。

前提の明示（design-premise・崩れたら本人に経路ごと問い直す）:
1. 面選定は castable_oracle（enrich_removal と同一規則・2026-07-15 共通規約）
2. 注釈括弧は strip_reminder で除去（「(あなたのライブラリーを切り直す)」等）
3. 判定単位は文。複数の効果文があれば配列に複数要素が入る
4. 対戦相手のライブラリを見る/サーチする効果は**数えない**（自分の掘削でない）
"""
import json
import re
import sys

import psycopg2
from psycopg2.extras import execute_batch, Json

sys.path.insert(0, '/mnt/mtg_rag/src')
from db_config import get_db_config
from enrich_removal import strip_reminder, castable_oracle

# ── 語彙 ────────────────────────────────────────────────────────────
_NUM = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11,
        'twelve': 12, 'thirteen': 13, 'twenty': 20}

# 拾えるカードタイプの検出（長い語を先に＝basic land が land に食われないように）
_PICK_PATTERNS = [
    ('basic_land',   r'\bbasic land\b'),
    ('land',         r'\bland\b'),
    ('creature',     r'\bcreature\b'),
    ('artifact',     r'\bartifact\b'),
    ('enchantment',  r'\benchantment\b'),
    ('instant',      r'\binstant\b'),
    ('sorcery',      r'\bsorcery\b'),
    ('planeswalker', r'\bplaneswalker\b'),
    ('permanent',    r'\bpermanent\b'),
]


def _num(tok):
    """'three' / '3' / 'X' → 3 / 3 / None（不定は None＝番兵禁止）"""
    if tok is None:
        return None
    tok = tok.strip().lower()
    if tok.isdigit():
        return int(tok)
    return _NUM.get(tok)


def _picks(phrase: str) -> list:
    """対象句から拾えるカードタイプを抽出。何も特定できなければ ['any']。"""
    found = []
    seen = phrase.lower()
    for name, pat in _PICK_PATTERNS:
        if re.search(pat, seen):
            found.append(name)
            seen = re.sub(pat, ' ', seen)   # 長い語を先に潰す（basic land → land 防止）
    return found or ['any']


def _site(sentence: str, tline: str) -> str:
    """効果の在り処: 起動型（コロンの左にコスト）／誘発型（when/whenever/at）／呪文本体。
    R14/R15 の「本業か付随か」の軸がサーチにも来る予兆への備え（列に持つだけで門は読まない）。"""
    s = sentence.strip().lower()
    if re.match(r'^[^:.]{1,60}:', s):
        return 'activated'
    if re.match(r'^\s*(when|whenever|at the beginning)\b', s):
        return 'triggered'
    if 'instant' in (tline or '').lower() or 'sorcery' in (tline or '').lower():
        return 'spell'
    return 'spell'


def _sentences(text: str) -> list:
    """効果文へ分割（箇条書きの・も文の切れ目として扱う）。"""
    text = re.sub(r'\s+', ' ', text or '')
    return [s.strip() for s in re.split(r'(?<=[.;])\s+|\s+•\s*', text) if s.strip()]


# ── サーチ（tutor）─────────────────────────────────────────────────
_SEARCH_RE = re.compile(
    r'search (?:your|their) library for '
    r'(?:up to (\w+)|(\w+))?\s*([^,.]*)', re.I)
_DEST_RE = [
    ('battlefield', r'onto the battlefield'),
    ('hand',        r'into your hand|into their hand'),
    ('graveyard',   r'into your graveyard'),
    ('exile',       r'exile (?:it|them|that card)'),
    ('top',         r'on top of your library'),
]


def parse_tutor(text: str, tline: str):
    """ライブラリ全域サーチの効果配列。無ければ None。"""
    out = []
    for sent in _sentences(text):
        low = sent.lower()
        if 'search' not in low:
            continue
        if re.search(r"search (?:target )?(?:opponent|player)", low):
            continue      # 相手のライブラリを見る効果は自分の掘削でない（前提 4）
        m = _SEARCH_RE.search(low)
        if not m:
            continue
        cnt = _num(m.group(1) or m.group(2))
        phrase = m.group(3) or ''
        dest = 'hand'
        for name, pat in _DEST_RE:
            if re.search(pat, low):
                dest = name
                break
        out.append({"pick": _picks(phrase), "count": cnt,
                    "dest": dest, "site": _site(sent, tline)})
    return out or None


# ── 濾過（dig）─────────────────────────────────────────────────────
# 掘削の起点（アンカー）。**文で切らない**——「上から N 枚見る」と「うち M 枚を取る」は
# 別の文に分かれるのが定型（Impulse・Dig Through Time で実測）。アンカーから次の
# アンカーまで（または文末まで）を一つの効果の射程として読む。
_ANCHOR_RE = re.compile(r'look at the top (\w+) cards?|mill (\w+) cards?'
                        r'|\bscry (\w+)|\bsurveil (\w+)', re.I)
_TAKE_RE = re.compile(
    r'put (?:up to )?(\w+)?\s*(?:of them|of those cards|'
    r'(?:[a-z\- ]{0,30}?cards?) from among (?:them|the cards milled this way|those cards))'
    r'[^.]{0,40}?(?:into|onto|on)\s+(?:the )?(your hand|your graveyard|the battlefield|top of your library)',
    re.I)
_REST_RE = [
    ('graveyard', r'rest into your graveyard'),
    ('bottom',    r'rest on the bottom'),
    ('random',    r'in a random order'),
    ('top',       r'put them back in any order|rest on top of your library'),
]


def parse_dig(text: str, tline: str):
    """上から N 枚の位置に縛られる掘削の効果配列。無ければ None。

    射程はアンカー（look/mill/scry/surveil）から次のアンカーまで。文をまたぐ定型
    （「Look at the top four cards of your library. Put one of them into your hand
    and the rest on the bottom ...」）を一つの効果として拾うため。"""
    out = []
    anchors = list(_ANCHOR_RE.finditer(text))
    for i, m in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        scope = text[m.start():end]
        low = scope.lower()
        if re.search(r"(?:target )?(?:opponent|player)'?s?\s+library", low):
            continue      # 相手のライブラリ（前提 4）

        look_s, mill_s, scry_s, surv_s = m.groups()
        site = _site(text[:m.end()].split('.')[-1] or scope, tline)

        if scry_s is not None:
            out.append({"look": _num(scry_s), "take": 0, "pick": ["any"], "dest": None,
                        "rest": "top", "via_mill": False, "site": site})
            continue
        if surv_s is not None:
            out.append({"look": _num(surv_s), "take": 0, "pick": ["any"], "dest": None,
                        "rest": "graveyard", "via_mill": False, "site": site})
            continue

        via_mill = mill_s is not None
        look = _num(look_s if look_s is not None else mill_s)
        take, dest = None, None
        mt = _TAKE_RE.search(low)
        if mt:
            take = _num(mt.group(1)) if mt.group(1) else 1
            dest = {'your hand': 'hand', 'your graveyard': 'graveyard',
                    'the battlefield': 'battlefield',
                    'top of your library': 'top'}[mt.group(2)]
        rest = None
        for name, pat in _REST_RE:
            if re.search(pat, low):
                rest = name
                break
        if via_mill and take is None:
            continue      # 純粋な切削（拾わない）は掘削でなく墓地肥やし＝列に入れない
        if take is None and rest is None:
            continue      # 「見る」だけで取りも並べ替えもしない＝掘削でない
        if take is None:
            take = 0      # 並べ替えのみ（思案型）＝取る枚数 0
        out.append({"look": look, "take": take, "pick": _picks(mt.group(0) if mt else ''),
                    "dest": dest, "rest": rest, "via_mill": via_mill, "site": site})
    return out or None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='UPDATE せず分布と差分だけ出す')
    ap.add_argument('--show', metavar='NAME', help='1 枚の判定を詳しく見る')
    args = ap.parse_args()

    conn = psycopg2.connect(**get_db_config())
    cur = conn.cursor()
    cur.execute("ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS tutor jsonb")
    cur.execute("ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS dig   jsonb")
    conn.commit()

    if args.show:
        cur.execute("""SELECT card_name, oracle_text, card_faces_json, type_line
                       FROM mtg_cards_v2 WHERE card_name = %s""", (args.show,))
        for name, oracle, faces, tline in cur.fetchall():
            if isinstance(faces, str):
                faces = json.loads(faces)
            txt = strip_reminder(castable_oracle(oracle, faces))
            print(f"{name}\n  oracle: {txt[:200]}")
            print(f"  tutor: {json.dumps(parse_tutor(txt, tline), ensure_ascii=False)}")
            print(f"  dig  : {json.dumps(parse_dig(txt, tline), ensure_ascii=False)}")
        return

    cur.execute("""SELECT id, card_name, oracle_text, card_faces_json, type_line,
                          tutor, dig FROM mtg_cards_v2""")
    rows = cur.fetchall()
    changes, n_t, n_d = [], 0, 0
    for cid, name, oracle, faces, tline, old_t, old_d in rows:
        if isinstance(faces, str):
            faces = json.loads(faces)
        txt = strip_reminder(castable_oracle(oracle, faces))
        new_t = parse_tutor(txt, tline)
        new_d = parse_dig(txt, tline)
        n_t += 1 if new_t else 0
        n_d += 1 if new_d else 0
        if new_t != old_t or new_d != old_d:
            changes.append((cid, name, new_t, new_d))

    print(f"tutor 非 NULL: {n_t} 枚 / dig 非 NULL: {n_d} 枚 / 差分 {len(changes)} 行")
    if args.dry_run:
        for _, name, t, d in changes[:25]:
            print(f"  {name:34s} tutor={json.dumps(t, ensure_ascii=False)[:60]} "
                  f"dig={json.dumps(d, ensure_ascii=False)[:60]}")
        print("--dry-run: UPDATE していません")
        return

    execute_batch(cur, "UPDATE mtg_cards_v2 SET tutor=%s, dig=%s WHERE id=%s",
                  [(Json(t) if t else None, Json(d) if d else None, cid)
                   for cid, _, t, d in changes], page_size=500)
    conn.commit()
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_tutor ON mtg_cards_v2 USING gin (tutor)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_dig   ON mtg_cards_v2 USING gin (dig)")
    conn.commit()
    print(f"UPDATE 完了: {len(changes)} 行（値が変わる行だけ・冪等）＋ GIN 索引")


if __name__ == '__main__':
    main()
