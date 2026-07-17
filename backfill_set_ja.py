"""
backfill_set_ja.py — 新セットの日本語（カード名＋オラクル）を whisper から backfill（2026-07-13）

scrape_wisdom_guild_full.py（スコープC）は「日本語名が既にあるカード」を対象にする設計のため、
日本語名すら無い新セット（例: Marvel msh/msc）には効かない。本スクリプトは英語カード名で
whisper を引き、<title>日本語名/英語名：カードデータ</title> から日本語名を、テキスト行から
日本語オラクルを取り、両方を埋める。

使い方:
  python backfill_set_ja.py --sets msh,msc --status    # 対象件数
  python backfill_set_ja.py --sets msh,msc --sample    # 5件だけ試して表示（DB 更新なし）
  python backfill_set_ja.py --sets msh,msc --run       # 本走（再開可・バッチ commit）

設計上の前提（design-premise 流に明示）:
  - whisper は英語名 URL で引ける（2026-07-13 に msh/msc サンプル 8/8 で実証）。
  - 日本語名の正準ソースは <title>（フリガナ混入なし・英語名の一致で誤ページ検知）。
  - 両面カード（英語名 'A // B'）は面ごとに whisper ページが分かれる想定＝面別に取得し
    「A // B」「表 // 裏」で連結（T3 の規約）。取れない面はスキップ記録（推測で埋めない）。
  - oracle_text が空（バニラ/トークン級）は日本語名のみ埋める（訳すものが無い＝
    japanese_oracle_text は NULL のまま正常・data-handling 規約）。
  - 既に値がある列は上書きしない（再開可能・冪等）。
  - バッチごとに commit（idle in transaction を作らない＝2026-07-13 ロック事件の教訓）。

礼儀: scrape_wisdom_guild_full の fetch_html を流用（~2.5s/req・HTML キャッシュ・UA 明示）。
"""
import argparse
import html as html_module
import re
import sys
import time

import psycopg2

sys.path.insert(0, '/mnt/mtg_rag')
from db_config import DB_CONFIG
from scrape_wisdom_guild_full import (fetch_html, parse_oracle,
                                      parse_face_oracles, is_japanese,
                                      SLEEP_SEC)

TITLE_RE = re.compile(r'<title>(.+?)/(.+?)：カードデータ')
COMMIT_EVERY = 25


def parse_ja_name(body: str, expect_en: str):
    """<title> から日本語名を取る。英語名が一致しないページ（リダイレクト等）は棄却。
    title 内はアポストロフィ等が HTML エンティティ（&#039;）のまま＝unescape してから
    比較・格納する（2026-07-13 初走で 49 枚を誤棄却したバグの修正）。"""
    m = TITLE_RE.search(body)
    if not m:
        return None, "title not matched"
    ja = html_module.unescape(m.group(1)).strip()
    en = html_module.unescape(m.group(2)).strip()
    if en.lower() != expect_en.lower():
        return None, f"title en mismatch: {en!r}"
    if not is_japanese(ja):
        return None, f"title not japanese: {ja!r}"
    return ja, None


def fetch_card(card_name: str, need_sleep: bool):
    """1面ぶんの (ja_name, ja_text, errs) を取る。"""
    body, err = fetch_html(card_name)
    if need_sleep:
        time.sleep(SLEEP_SEC)
    if body is None:
        return None, None, [f"fetch: {err}"]
    errs = []
    ja_name, nerr = parse_ja_name(body, card_name)
    if nerr:
        errs.append(f"name: {nerr}")
    ja_text, terr = parse_oracle(body)
    if terr:
        errs.append(f"text: {terr}")
    return ja_name, ja_text, errs


def process_card(card_name: str):
    """単面/両面を吸収して (ja_name, ja_text, errs) を返す。両面は 'A // B' 連結。
    テキストの取り方（2026-07-15 修正）: whisper は面名どちらでも同一ページを返し、
    表面＝dc セル・裏面＝ddc セルに分かれて載る。従来の「面別ページを parse_oracle」
    は常に表面が返り「表 // 表」を作っていた（複製バグ 53 枚の根本原因）。
    名前は従来どおり面別ページの title から取る。"""
    if ' // ' in card_name:
        faces = card_name.split(' // ')
        names, errs = [], []
        face_texts: list[str] = []
        for i, f in enumerate(faces):
            body, err = fetch_html(f)
            time.sleep(SLEEP_SEC)
            if body is None:
                names.append(None)
                errs.append(f"fetch: {err}")
                continue
            n, nerr = parse_ja_name(body, f)
            if nerr:
                errs.append(f"name: {nerr}")
            names.append(n)
            if i == 0:
                face_texts = parse_face_oracles(body)
        ja_name = ' // '.join(n for n in names if n) if all(names) else None
        ja_text = (' // '.join(face_texts)
                   if face_texts and len(face_texts) == len(faces) else None)
        if ja_text is None:
            errs.append("text: face cells incomplete")
        return ja_name, ja_text, errs
    return fetch_card(card_name, need_sleep=True)


def get_targets(conn, sets):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, card_name,
                   (japanese_name IS NULL OR japanese_name = '')        AS need_name,
                   (japanese_oracle_text IS NULL OR japanese_oracle_text = '')
                       AND oracle_text IS NOT NULL AND oracle_text <> '' AS need_text
            FROM mtg_cards_v2
            WHERE set_code = ANY(%s)
              AND ((japanese_name IS NULL OR japanese_name = '')
                   OR ((japanese_oracle_text IS NULL OR japanese_oracle_text = '')
                       AND oracle_text IS NOT NULL AND oracle_text <> ''))
            ORDER BY id
        """, (sets,))
        return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", required=True, help="対象 set_code（カンマ区切り・例 msh,msc）")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--sample", action="store_true", help="5件試すだけ（DB 更新なし）")
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    sets = [s.strip() for s in args.sets.split(",") if s.strip()]

    conn = psycopg2.connect(**DB_CONFIG)
    targets = get_targets(conn, sets)
    print(f"対象: {len(targets)} 件（sets={sets}）")

    if args.status:
        conn.close()
        return

    if args.sample:
        for cid, name, need_n, need_t in targets[:5]:
            ja_name, ja_text, errs = process_card(name)
            print(f"[{cid}] {name}\n  名前: {ja_name}\n  本文: {(ja_text or '')[:60]}"
                  + (f"\n  警告: {errs}" if errs else ""))
        conn.close()
        return

    if not args.run:
        print("実行するには --run（お試しは --sample）")
        conn.close()
        return

    updated_ids, skipped = [], []
    done = 0
    for cid, name, need_name, need_text in targets:
        ja_name, ja_text, errs = process_card(name)
        with conn.cursor() as cur:
            touched = False
            if need_name and ja_name:
                cur.execute("UPDATE mtg_cards_v2 SET japanese_name=%s"
                            " WHERE id=%s AND (japanese_name IS NULL OR japanese_name='')",
                            (ja_name, cid))
                touched = True
            if need_text and ja_text:
                cur.execute("UPDATE mtg_cards_v2 SET japanese_oracle_text=%s"
                            " WHERE id=%s AND (japanese_oracle_text IS NULL"
                            "                  OR japanese_oracle_text='')",
                            (ja_text, cid))
                touched = True
        if touched:
            updated_ids.append(cid)
        if errs:
            skipped.append((cid, name, errs))
            print(f"  [警告] {name}: {errs}")
        done += 1
        if done % COMMIT_EVERY == 0:
            conn.commit()   # バッチ commit＝長時間トランザクションを作らない
            print(f"  … {done}/{len(targets)} commit")
    conn.commit()

    print(f"\n完了: 更新 {len(updated_ids)} 件 / 警告あり {len(skipped)} 件")
    if skipped:
        print("警告一覧（要目視・推測で埋めていない）:")
        for cid, name, errs in skipped:
            print(f"  [{cid}] {name}: {errs}")
    # reembed 用 id リスト（rebuild_embed_text 連携用）
    print("\nupdated_ids:", ",".join(map(str, updated_ids)))
    conn.close()


if __name__ == "__main__":
    main()
