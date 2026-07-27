#!/usr/bin/env python3
"""is_mana_boost 列の再導出 — 「本業か付随か」線の実装（2026-07-26 本人裁定）。

経緯: 列は 2026-06-24 に新設されたが導出スクリプトがリポジトリに残っておらず、
2,419 行の値だけが「生きた化石」になっていた。7/26 のマナ直行路の実測棄却
（id=113・偽陽性を play-rate 順が増幅）を受けて、列の再定義とコード化を行う。
このファイルが以後の正本（採点規約の対応節: docs/me/grading_conventions.md）。

## 定義（二段の門）

**第一の門＝文脈（2026-07-26 本人裁定・R14「行為ベース」のマナ版の写し）**:
マナ産出文のうち「本業」だけを数える。
- 数える: 起動型能力（コスト: Add）・呪文の効果文（儀式）・常在/付与型・
  **自身の ETB 誘発**（Dockside/Prosperous Innkeeper＝R14 が draw で
  「ETB 内は数える」と引いた線の写し）・
  **マナ倍化**（tap for mana → add an additional 型。裁定①=2: ドローの倍化=1 と
  非対称だが、マナはリソースそのものなので倍化した瞬間に加速が成立する）
- 数えない（おまけ＝採点は 1 でも列は False 側・draw 前例と同じ写像）:
  - 死亡時・被破壊時などの誘発報酬（Greedy Freebooter・Shambling Ghast）
  - 唱えるたび/攻撃時/ターン起点等の繰り返し誘発（裁定②=1: Birgi・Neheb）
  - 他者の死亡等・盤面依存の報酬（裁定③=1: Pitiless Plunderer・Revel in Riches）

**第二の門＝量（2026-06-24 本人定義・不変）**:
本業のマナ文について net-mana = 出すマナ − 払うマナ −（土地なら 1）> 0 のみ TRUE。
- 払うマナ: 起動型はコロン左のマナシンボルのみ（タップ/生け贄/ライフ=0）。
  呪文（インスタント/ソーサリー）は唱えるコスト自体（使い切りだから）。
  パーマネントの設置コストは数えない（設置後に繰り返し使う）。
- 可変（X・任意の量）は出すマナ≈∞ ＝ ほぼ無条件で正。
- 宝物等のマナトークン生成は遅延産出として出すマナに数える（6/24 裁定の維持）。

値: TRUE=マナ加速の本業 / FALSE=マナ産出はあるが加速でない（フィルター・おまけのみ）/
NULL=マナ産出文なし（不在は NULL・番兵禁止）。

前提の明示（design-premise・崩れたら本人に経路ごと問い直してもらう）:
1. 面選定は castable_oracle（手札から唱えられる面のみ・2026-07-15 共通規約）。
   これにより Tamiyo（裏面 PW の条件マナ）は自動で落ちる。
2. 注釈括弧は原則除去するが、括弧除去後にマナ文が無く、括弧内に「{T}: Add」型の
   能力があるカード（基本土地等＝能力が注釈内表記）はそれを能力として評価する
   （6/24「括弧全消しで基本土地が壊れた」教訓の実装形）。
3. 判定単位は文。複数のマナ文があれば「本業で net>0 の文が一つでもあれば TRUE」。
"""
import json
import re

import psycopg2
from psycopg2.extras import execute_batch

from db_config import get_db_config
from enrich_removal import strip_reminder, castable_oracle

NUM = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4,
       'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}

# マナ産出の検出（Add {…}{…} / Add one mana / adds an additional {G} /
# create … Treasure token）。連続シンボルは全部拾う（Dark Ritual {B}{B}{B}）
ADD_RE = re.compile(r"\badds?\s+(?:an\s+additional\s+|additional\s+)?"
                    r"((?:\{[^}]+\})+|one|two|three|four|five|six|seven|"
                    r"eight|nine|ten|x\b|an amount|that much|any amount|"
                    r"\d+\s+mana)", re.I)
TREASURE_RE = re.compile(r"\bcreates?\b[^.]*?\b(a|an|one|two|three|four|five|"
                         r"six|seven|eight|nine|ten|x|that many)\b[^.]*?"
                         r"\b(?:Treasure|Gold|Powerstone)\b", re.I)
# 誘発文の頭。能力語プレフィックス（「Opus — Whenever …」「Landfall — Whenever …」）
# は剥がしてから判定する（_is_trigger）。章立て「I — Create …」は剥がしても
# 誘発語が現れない＝本業扱いのまま（draw 版 R14「鏡割りの寓話 II 章=2」の線と同じ）
TRIGGER_RE = re.compile(r"^\s*(when|whenever|at the beginning)\b", re.I)
ABILITY_WORD_RE = re.compile(r"^[^—.]{1,30}—\s*")


def _is_trigger(sent: str) -> bool:
    return bool(TRIGGER_RE.match(ABILITY_WORD_RE.sub('', sent)))
# 自身の ETB（数える側の例外）
ETB_SELF_RE = re.compile(r"\bthis (creature|artifact|permanent|enchantment|land)"
                         r" enters\b|(?<!an )(?<!a )\benters the battlefield\b",
                         re.I)
# マナ倍化（裁定①）。3 つの oracle テンプレを拾う:
#   旧: 「Whenever ~ is tapped for mana, … adds …」（Mana Flare・Wild Growth）
#   新: 「Whenever a land's ability causes you to add … , add …」（Caged Sun 現行文）
#   共通: 「adds? (an) additional …」（additional の語があれば倍化の証拠）
DOUBLER_RE = re.compile(
    r"tap[^.]*?\bfor mana\b[^.]*?\badds?\b"
    r"|causes? you to add[^.]*?\badd"
    r"|\badds?\s+(?:one|two|three|\{[^}]+\})?\s*additional\b"
    r"|\badds?\s+an\s+additional\b", re.I)
# 打ち消し呪文の検出（2026-07-26 本人裁定「マナ吸収は1にしてしまおう」）:
# 打ち消しを含む呪文の Add 文は付随（本業はカウンター・マナはおまけ）＝
# 本人 7/23 の診断「打ち消せたら一回きりのおまけマナ」（PHASE2 §11 実例3枚目）の
# 列への写し。Mana Drain・Mana Sculpt・Plasm Capture・Spell Swindle 型が対象
COUNTER_SPELL_RE = re.compile(r"\bcounter (target|that|all|it\b)", re.I)
# 起動型（コロンの左＝コスト）
ACTIVATED_RE = re.compile(r"^[^\"]*?:")
# マナシンボル（generic は数値ぶん・hybrid/phyrexian は 1）
SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def _mana_amount(match_text: str) -> int:
    """Add の後ろのテキスト → 出すマナ量（可変は 99）"""
    t = match_text.lower().strip()
    if t.startswith('{'):
        n = 0
        for sym in SYMBOL_RE.findall(match_text):
            s = sym.strip().lower()
            if s.isdigit():
                n += int(s)
            elif s in ('x', 'y', 'z'):
                return 99
            elif s == 't':          # 稀な誤マッチ保険
                continue
            else:
                n += 1
        return n
    if t in ('x', 'an amount', 'that much', 'any amount'):
        return 99
    m = re.match(r"(\d+)", t)
    if m:
        return int(m.group(1))
    return NUM.get(t.split()[0], 1)


def _cost_mana(cost_text: str) -> int:
    """起動コスト（コロン左）のマナシンボル数。タップ/生け贄/ライフは 0。"""
    n = 0
    for sym in SYMBOL_RE.findall(cost_text):
        s = sym.strip().lower()
        if s.isdigit():
            n += int(s)
        elif s in ('t', 'q'):
            continue
        elif s in ('x', 'y', 'z'):
            n += 0        # X 起動は「払う側の可変」＝保守的に 0（Vault 型は net 正のまま）
        else:
            n += 1        # 色/hybrid/phyrexian は 1
    return n


def _spell_cost(mana_cost: str) -> int:
    """呪文の唱えるコスト（儀式の払う側）"""
    return _cost_mana(mana_cost or '')


def parse_mana_boost(oracle: str, mana_cost: str, type_line: str):
    """castable な oracle → True/False/None（docstring の二段の門）"""
    raw = oracle or ''
    text = strip_reminder(raw)
    # 引用符内＝他のオブジェクトに与える能力（トークンへの付与等）は自分の産出
    # でない（Fable of the Mirror-Breaker の Goblin トークン付与「Whenever this
    # token attacks, create a Treasure」を自分の能力と誤読した事故の対策。
    # 6/24 の宝物注釈バグと同じ「他所のテキストを自分と読む」故障クラス）
    text = re.sub(r'"[^"]*"', '', text)
    is_land = 'Land' in (type_line or '')
    is_spell = bool(re.search(r'\b(Instant|Sorcery)\b', type_line or ''))
    # 打ち消し呪文のマナは付随（本業はカウンター）＝マナ文があっても False 側
    # （R15 追記・本人裁定 2026-07-26。カウンター系クエリでの採点には影響しない＝
    # これは is_mana_boost の話であって counter 判定は target_types の管轄）
    if is_spell and COUNTER_SPELL_RE.search(text):
        if ADD_RE.search(text) or TREASURE_RE.search(text):
            return False
        return None

    # 前提2: 括弧除去後にマナ文が無いが、括弧内に起動型 Add がある（基本土地型）
    if not ADD_RE.search(text) and not TREASURE_RE.search(text):
        paren = ' '.join(re.findall(r'\(([^)]*)\)', raw))
        if re.search(r'\{T\}\s*:\s*Add', paren, re.I):
            text = paren        # 括弧内の能力を本文として評価
        else:
            return None         # マナ産出文なし

    # 文分割: まず [.\n;] で切り、文内のモード（choose one — • A • B）は
    # ヘッダの誘発文脈をモードにも伝播させる（Shambling Ghast「When dies,
    # choose one — … • Create a Treasure token」の Treasure がヘッダの誘発を
    # 失って本業扱いになる事故の対策）
    units = []                   # (sent, in_trigger)
    for raw_sent in re.split(r'[.\n;]', text):
        raw_sent = raw_sent.strip()
        if not raw_sent:
            continue
        if raw_sent.startswith('•') or re.match(r'(?i)if\b', raw_sent):
            # 誘発文脈の継承 2 形:
            # ・行頭モード（oracle が改行でモードを分ける形式＝Shambling Ghast の
            #   「choose one —」ヘッダの誘発をモードにも効かせる）
            # ・If 開始文（Smothering Tithe「If they don't, you create a Treasure」
            #   ＝直前の誘発文の帰結節。分割で文脈を失うと誘発報酬が本業化する）
            prev_trig = units[-1][1] if units else False
            units.append((raw_sent.lstrip('• ').strip(), prev_trig))
        elif '•' in raw_sent:
            head, *modes = raw_sent.split('•')
            trig = _is_trigger(head)
            units.append((head.strip(), trig))
            units.extend((m.strip(), trig) for m in modes if m.strip())
        else:
            units.append((raw_sent, _is_trigger(raw_sent)))

    found_any = False
    for sent, in_trigger in units:
        add_m = ADD_RE.search(sent)
        tre_m = TREASURE_RE.search(sent)
        if not add_m and not tre_m:
            continue
        found_any = True

        # ── 第一の門: 文脈 ──
        # 倍化（裁定①）は文脈より先に判定する: High Tide は「Until end of
        # turn, whenever …」で始まり誘発頭に見えないが、倍化文なら文脈不問で
        # 本業＆量の門も免除（増分がそのまま正）
        if DOUBLER_RE.search(sent):
            return True
        # 呪文（インスタント/ソーサリー）の本文に書かれた誘発は遅延誘発＝
        # 効果の一部（Mana Drain）なので門を免除する。おまけ問題（Greedy/
        # Birgi 型）はパーマネントの常設誘発の話
        if in_trigger and not is_spell:
            if not ETB_SELF_RE.search(sent):
                continue        # おまけ（死亡時/攻撃時/唱えるたび/他者依存）＝数えない

        # ── 第二の門: net-mana ──
        # 可変判定（draw 版の写し＋拡張）: 文内に for each / equal to があれば
        # 出すマナ≈∞。窓を「Add 直後」に絞ると or 継ぎ（Culling Ritual
        # 「Add {B} or {G} for each …」）と文頭型（Brass's Bounty「For each
        # land …, create a Treasure」）を見逃す＝文単位で判定する（文は
        # ピリオドで切れているので他文の for each を拾う誤結合は起きない）
        low_sent = sent.lower()
        is_variable = ('for each' in low_sent or 'equal to' in low_sent)
        out = 0
        if add_m:
            amt = _mana_amount(add_m.group(1))
            if is_variable:
                amt = 99
            out = max(out, amt)
        if tre_m:
            w = tre_m.group(1).lower()
            n = 99 if (w in ('x', 'that many') or is_variable) else NUM.get(w, 1)
            out = max(out, n)
        cost_m = ACTIVATED_RE.match(sent)
        if cost_m and ':' in sent:
            pay = _cost_mana(sent.split(':', 1)[0])
        elif is_spell:
            pay = _spell_cost(mana_cost)
        else:
            pay = 0             # 常在/誘発/付与型は起動コスト無し
        net = out - pay - (1 if is_land else 0)
        if net > 0:
            return True

    return False if found_any else None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='UPDATE せず差分リストだけ出す')
    args = ap.parse_args()

    conn = psycopg2.connect(**get_db_config())
    cur = conn.cursor()
    cur.execute("""SELECT id, card_name, oracle_text, mana_cost, type_line,
                          card_faces_json, is_mana_boost
                   FROM mtg_cards_v2""")
    rows = cur.fetchall()

    changes = []
    stats = {'t': 0, 'f': 0, 'null': 0}
    for cid, name, oracle, mc, tline, faces, old in rows:
        if isinstance(faces, str):
            faces = json.loads(faces)
        c_oracle = castable_oracle(oracle, faces)
        new = parse_mana_boost(c_oracle, mc, tline)
        stats['t' if new is True else 'f' if new is False else 'null'] += 1
        if new != old:
            changes.append((cid, name, old, new))

    print(f"新judgement分布: TRUE {stats['t']} / FALSE {stats['f']} / NULL {stats['null']}")
    print(f"差分: {len(changes)} 行")
    for _, name, old, new in sorted(changes, key=lambda c: (str(c[2]), c[1])):
        print(f"  {str(old):<5} → {str(new):<5}  {name}")

    if args.dry_run:
        print("\n--dry-run: UPDATE していません")
        return

    execute_batch(cur,
                  "UPDATE mtg_cards_v2 SET is_mana_boost=%s WHERE id=%s",
                  [(new, cid) for cid, _, _, new in changes], page_size=500)
    conn.commit()
    print(f"UPDATE 完了: {len(changes)} 行（値が変わる行だけ・冪等）")


if __name__ == '__main__':
    main()
