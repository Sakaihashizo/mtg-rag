#!/usr/bin/env python3
"""Draw 枚数列（draw_count / draw_x）の導出 — R14「ドロー族＝行為ベース」の検索側の写し。

規約の正本: docs/me/grading_conventions.md R14（2026-07-23 制定・本人裁定）。
審査材料: docs/me/draw_anchor_candidates_20260718.md。

列の意味（機械的事実の列であって grade ではない）:
- draw_count int  : そのカードが一回の命令で引かせる最大枚数 N。
                    「命令形の Draw N card(s)」だけを数える。無ければ NULL（番兵 0 禁止）。
- draw_x     bool : 可変枚数ドロー（Draw X / that many / equal to / for each）を
                    持つなら TRUE。無ければ NULL。

前提の明示（design-premise・崩れたら本人に経路ごと問い直してもらう）:
1. 行為ベース＝命令形のみ（R14）。置換文（... would draw ...）の中の draw は数えない
   （Dredge・倍化・概念泥棒は列 NULL。GT の 0/1 は人間採点側の仕事）。誘発の条件節
   （Whenever you draw ...,）は先頭コンマまで落としてから残りを数える（Sheoldred が
   浮かない・除去の「注釈テキスト除去」の教訓の写し）。注釈（括弧）は先に除去。
2. 主語の向き: draw の直前文脈に opponent がいる命令（each/target opponent draws）は
   相手に引かせる行為＝自分のドローでないから数えない（P2 precision 優先）。
   each player draws / target player draws は自分も引ける・自分を選べる＝数える
   （ホイール・Blue Sun's Zenith 型）。
3. 可変（X 等）は draw_x に分離し、枚数ゲートでは「OR draw_x」で満たす扱い
   （R14 モード裁定「選択2枚でも2枚引けることはひける」の同族＝X=N を選べば引ける）。
4. 面選定は castable_oracle（face_cmcs / removal と同一規則＝手札から唱えられる面）。
5. 複数の draw 命令は合算せず max（ETB 1枚＋死亡時 1枚 は「2枚引く」ではない）。
"""
import re

import psycopg2
from psycopg2.extras import execute_batch

from db_config import get_db_config
from enrich_removal import strip_reminder, castable_oracle

NUM = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
       'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11,
       'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15, 'twenty': 20}

# 命令形/自分主語の draw。additional は Sylvan Library（draw two additional cards）用、
# up to は Truce 系（draw up to two ＝選べば引ける＝R14 モード裁定の同族）
DRAW_RE = re.compile(
    r"\bdraws?\s+(?:up to\s+)?(a|an|one|two|three|four|five|six|seven|eight|"
    r"nine|ten|eleven|twelve|thirteen|fourteen|fifteen|twenty|x|that many|\d+)"
    r"\s+(?:additional\s+)?cards?\b", re.I)
# 「draw cards equal to ...」型の可変
DRAW_EQUAL_RE = re.compile(r"\bdraws?\s+cards?\s+equal to\b", re.I)
# 上付き指数の可変（Mathemagics「draws 2ˣ cards」）
DRAW_SUPER_RE = re.compile(r"\bdraws?\s+\d*[ˣ⁰¹²³⁴⁵⁶⁷⁸⁹]+\s*cards?", re.I)
# 誘発の条件節（先頭コンマまで）: Whenever you draw a card, → 落とす
TRIGGER_PREFIX_RE = re.compile(
    r"^\s*(?:whenever|when|as long as)\b[^,]*,", re.I)


def parse_draw(oracle: str):
    """castable な oracle テキスト → (draw_count or None, draw_x or None)"""
    text = strip_reminder(oracle or '')
    best = None
    has_x = False
    # 文・モード・能力の区切りで分割（• はモード弾・— は Spree/能力語の区切り）
    for raw_sent in re.split(r'[.\n•;]|—', text):
        sent = raw_sent.strip()
        if not sent:
            continue
        low = sent.lower()
        # 置換文は丸ごと数えない（前提1: Dredge/倍化/概念泥棒/Jace 勝利置換）
        if 'would draw' in low:
            continue
        # 誘発の条件節を落とす（条件の中の draw は行為でない）
        low = TRIGGER_PREFIX_RE.sub('', low)
        for m in DRAW_RE.finditer(low):
            # 主語の向き（前提2）: draw の直前が opponent（each/target/an opponent
            # draws）のときだけ相手のドロー。窓を広くすると「相手が引き、次に
            # あなたが引く」文（Cut a Deal）の後半まで誤って殺す＝末尾アンカー必須
            before = low[max(0, m.start() - 40):m.start()]
            if re.search(r"opponents?\s+(?:may\s+)?$", before):
                continue
            word = m.group(1)
            # 可変判定: X / that many / 直後の for each（前提3）
            after = low[m.end():m.end() + 30]
            if word in ('x', 'that many') or after.lstrip().startswith('for each'):
                has_x = True
                continue
            n = NUM.get(word) or (int(word) if word.isdigit() else None)
            if n is not None and (best is None or n > best):
                best = n
        if DRAW_EQUAL_RE.search(low) or DRAW_SUPER_RE.search(low):
            has_x = True
    return best, (True if has_x else None)


DDL = """
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS draw_count integer;
ALTER TABLE mtg_cards_v2 ADD COLUMN IF NOT EXISTS draw_x     boolean;
"""
IDX = """
CREATE INDEX IF NOT EXISTS mtg_cards_v2_draw_count_idx
    ON mtg_cards_v2 (draw_count) WHERE draw_count IS NOT NULL;
"""


def main():
    cfg = get_db_config()
    conn = psycopg2.connect(**cfg)
    cur = conn.cursor()
    for stmt in DDL.strip().split(';'):
        if stmt.strip():
            cur.execute(stmt)
    conn.commit()

    # 書き込みは値が変わる行だけ（物理チャーン回避・enrich_removal と同じ流儀）
    cur.execute("""SELECT id, oracle_text, card_faces_json, draw_count, draw_x
                   FROM mtg_cards_v2""")
    rows = cur.fetchall()
    updates = []
    for cid, ot, cfj, cur_dc, cur_dx in rows:
        dc, dx = parse_draw(castable_oracle(ot, cfj))
        if (dc, dx) == (cur_dc, cur_dx):
            continue
        updates.append((dc, dx, cid))
    print(f"値が変わる行: {len(updates)} 件（他はスキップ）")
    execute_batch(cur,
                  "UPDATE mtg_cards_v2 SET draw_count=%s, draw_x=%s WHERE id=%s",
                  updates, page_size=1000)
    conn.commit()

    for stmt in IDX.strip().split(';'):
        if stmt.strip():
            cur.execute(stmt)
    conn.commit()
    cur.execute("ANALYZE mtg_cards_v2")
    conn.commit()

    cur.execute("SELECT count(*) FROM mtg_cards_v2 WHERE draw_count IS NOT NULL")
    n_dc = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM mtg_cards_v2 WHERE draw_x")
    n_dx = cur.fetchone()[0]
    cur.execute("""SELECT draw_count, count(*) FROM mtg_cards_v2
                   WHERE draw_count IS NOT NULL GROUP BY 1 ORDER BY 1""")
    dist = cur.fetchall()
    print(f"populate 完了: 全{len(rows)}件 / draw_count あり {n_dc} / draw_x {n_dx}")
    print("分布:", ', '.join(f"{k}枚={v}" for k, v in dist))
    conn.close()


if __name__ == "__main__":
    main()
