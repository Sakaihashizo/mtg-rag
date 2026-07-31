#!/usr/bin/env python
"""
import_rules.py — MTG 総合ルール（Comprehensive Rules）の搬入（2026-07-31 新設）
================================================================================
目的: Agentic RAG の参照資料として、総合ルールの条文と用語集を DB に構造化して置く。
本人の発案「用語集とかルール文をエージェントが参照してよりよい答えを導きやすいように」。

なぜ S3 のファイルでなく DB か:
  - 検索装置一式（FTS・pgvector・Data API）が既に Postgres に建っている
  - 条番号（例 702.15a）が引用アンカーとして検証可能＝語り係 LLM が条番号つきで
    語れば、幻覚ガードが「その条文は実在するか・本文と合うか」を機械照合できる
    （LLM の出力は信用せず検証層で捨てる、の掟と同じ線）

設計の前提（design-premise-ledger 流に明示）:
  - 原文は英語正本（オラクル本文も英語・条番号は日英共通）。text_ja は不在なら
    NULL（番兵禁止）＝日本語訳の公式 txt が見つかったら後から埋める
  - 主用途は「安いモデルに正確な資料を持たせる」（7/30 段位表: 7B/Nova 級は
    知識が薄い）。Opus 級を賢くする道具ではない
  - 冪等: 同じファイル・同じ版なら 2 走目は差分ゼロ（TRUNCATE→INSERT）
  - 原文の置き場は data/rules/（WotC 配布物＝リポジトリに含めない・.gitignore 済み。
    売り物に混ぜない棚は Moxfield データと同じ扱い）

原文の構造（2026-06-19 版で実測）:
  - 冒頭〜"Credits"（1 回目）= 目次。本文はその直後の "1. Game Concepts" から
  - 条文は 1 行 1 条（"100.1. text..." / "100.1a text..."）。"Example: " 行は
    直前の条文の続きとして連結する
  - "Glossary"（2 回目）〜 "Credits"（2 回目）= 用語集。空行区切りで
    1 ブロック = 見出し語 1 行 + 定義行

使い方:
  /mnt/new_hdd/my_rag_env/bin/python src/import_rules.py \
      --file data/rules/MagicCompRules_20260619.txt --version 2026-06-19
"""
import argparse
import re
import sys

import psycopg2

sys.path.insert(0, '/mnt/mtg_rag/src')
from db_config import DB_CONFIG

DDL = """
CREATE TABLE IF NOT EXISTS mtg_rules (
    id             serial PRIMARY KEY,
    rule_number    text NOT NULL,       -- 条番号（'702.15a'）。用語集行は見出し語
    section        integer,             -- 章番号（702.15a → 7）。用語集行は NULL
    is_glossary    boolean NOT NULL DEFAULT false,
    text_en        text NOT NULL,
    text_ja        text,                -- 日本語訳。公式訳の txt 未入手＝当面 NULL
    source_version date NOT NULL,       -- 原文の版（例 2026-06-19）
    UNIQUE (is_glossary, rule_number)
);
CREATE INDEX IF NOT EXISTS mtg_rules_fts_en
    ON mtg_rules USING gin (to_tsvector('english', text_en));
"""

# 条文行:  "100.1. These Magic rules..." / "100.1a In a two-player game..."
RULE_RE = re.compile(r'^(\d{3}\.\d+[a-z]?)\.?\s+(\S.*)$')
# 見出し行: "1. Game Concepts" / "100. General"
HEAD_RE = re.compile(r'^(\d{1,3})\.\s+(\S.*)$')


def parse(path: str):
    text = open(path, encoding='utf-8-sig').read()   # BOM を剥がす
    lines = [ln.rstrip('\r') for ln in text.split('\n')]

    credits_idx = [i for i, ln in enumerate(lines) if ln.strip() == 'Credits']
    glossary_idx = [i for i, ln in enumerate(lines) if ln.strip() == 'Glossary']
    if len(credits_idx) < 2 or len(glossary_idx) < 2:
        raise SystemExit('原文の構造が想定と違う（Credits/Glossary の出現回数）。'
                         '版が変わって構造が動いた可能性＝目視で確認してから直す')
    body = lines[credits_idx[0] + 1: glossary_idx[1]]
    glossary = lines[glossary_idx[1] + 1: credits_idx[1]]

    rules = []          # (rule_number, section, text)
    for ln in body:
        s = ln.strip()
        if not s:
            continue
        m = RULE_RE.match(s)
        if m:
            num, txt = m.groups()
            rules.append([num, int(num[0]), txt])
            continue
        m = HEAD_RE.match(s)
        if m:
            num, txt = m.groups()
            rules.append([num, int(num[0]), txt])
            continue
        # "Example: ..." 等は直前の条文の続き
        if rules:
            rules[-1][2] += '\n' + s

    terms = []          # (term, definition)
    block: list[str] = []
    for ln in glossary + ['']:
        s = ln.strip()
        if s:
            block.append(s)
            continue
        if block:
            terms.append((block[0], '\n'.join(block[1:])))
            block = []
    # 定義が空の見出しは捨てない（相互参照だけの語もある）が、全体が空なら異常
    return rules, terms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True)
    ap.add_argument('--version', required=True, help='原文の版（YYYY-MM-DD）')
    args = ap.parse_args()

    rules, terms = parse(args.file)
    print(f'条文 {len(rules)} 件・用語 {len(terms)} 件を読んだ')
    if len(rules) < 3000 or len(terms) < 500:
        raise SystemExit('件数が経験値（条文 3,000+・用語 500+）を割った＝'
                         'パースの壊れを疑う。搬入せず終了')

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute(DDL)
    cur.execute('TRUNCATE mtg_rules RESTART IDENTITY')
    cur.executemany(
        'INSERT INTO mtg_rules (rule_number, section, is_glossary, text_en,'
        ' source_version) VALUES (%s, %s, false, %s, %s)',
        [(n, sec, t, args.version) for n, sec, t in rules])
    cur.executemany(
        'INSERT INTO mtg_rules (rule_number, section, is_glossary, text_en,'
        ' source_version) VALUES (%s, NULL, true, %s, %s)',
        [(term, d, args.version) for term, d in terms])
    conn.commit()
    cur.execute('SELECT is_glossary, count(*) FROM mtg_rules GROUP BY 1 ORDER BY 1')
    for row in cur.fetchall():
        print(('用語集' if row[0] else '条文'), row[1], '行')
    conn.close()


if __name__ == '__main__':
    main()
