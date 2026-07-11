"""
bakeoff_explain.py — 解説レイヤーのブラインド試飲会（2026-07-10）
====================================================================
目的: 最終回答（解説）生成を担う LLM を、ベンダーの推薦（ポジショントーク）でなく
**目隠し採点**で選ぶ。全候補に「同じ検索結果・同じ SYSTEM_PROMPT・同じ生成条件」を
配り、出力をモデル名を伏せてシャッフルし、採点者（本人）は名前を知らずに採点する。

公平性の担保:
  - 検索は searcher 直（決定的・ルーター LLM 不使用）で1回だけ実行し、全候補で共有
  - プロンプトは本番 mtg_rag_agent の SYSTEM_PROMPT + build_context をそのまま使用
  - 生成条件は本番と同じ temperature 0.7 / MAX_TOKENS
  - ラベル（回答A/B/C…）は**クエリごとに**ランダムに割り当て（文体でモデルを跨ぎ
    追跡して目隠しが破れるのを防ぐ）。対応表は answer_key.json に封印し、採点後に開ける

使い方:
  python bakeoff_explain.py --list        # 口座で見える推論プロファイルを棚卸し（要 List 権限）
  python bakeoff_explain.py --dry         # 呼び出し計画とコスト概算のみ（API 呼ばない）
  python bakeoff_explain.py               # 本走 → docs/me/bakeoff_<ts>/ に採点シートと封印キー
  python bakeoff_explain.py --models nova-lite,claude-haiku-4.5   # 受験生を絞る

採点の手順（本人）:
  1. grading_sheet.md を開き、クエリごとに 回答A/B/C… を読んで採点（5点満点目安:
     正確さ=ルール捏造なし / 用語の正しさ / 説明の質「なぜ強いか・どう使うか」/ 日本語の艶）
  2. 全部終わってから answer_key.json を開けて正体を知る
  3. 結果とコストは docs/me/cost_ledger.md へ（実測は出たその日に）
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime

# 日本語率の自動判定（「日本語で回答する」指示の遵守チェック＝crisp な欠格条件。
# 2026-07-11: Gemma が英語で回答したのを審査員が発見する羽目になった反省から）
JA_RE = re.compile(r'[ぁ-んァ-ン一-龥]')

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_rag_agent import SYSTEM_PROMPT, build_context, search_cards, MAX_TOKENS
from mtg_hybrid_search_v2 import MTGHybridSearcherV2

ENV_PATH = "/mnt/mtg_rag/.env"
OUT_ROOT = "/mnt/mtg_rag/docs/me"

# ── 受験生名簿 ───────────────────────────────────────────────
# kind: "bedrock" は inference profile ID / "gemini" は REST 直（現職・基準線）
# Claude 系の ID は推定を含む（--list で実在確認してから本走推奨）
CANDIDATES = {
    "nova-lite":        {"kind": "bedrock", "id": "us.amazon.nova-lite-v1:0"},
    "nova-pro":         {"kind": "bedrock", "id": "us.amazon.nova-pro-v1:0"},
    "claude-haiku-4.5": {"kind": "bedrock", "id": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
    "claude-sonnet-4.5": {"kind": "bedrock", "id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"},
    "gemini-flash-lite": {"kind": "gemini", "id": "gemini-2.5-flash-lite"},
    # 第二ラウンド（2026-07-11）: 格安の刺客。Gemma 4 世代（在庫棚卸しで判明・
    # 旧知識の Gemma 3 は棚から消えてた）。API 無料枠で呼べる。
    # wrap="ja_enforce" = Gemma 調教 v1（素の SYSTEM_PROMPT では英語で回答した→
    # Nova に調教機会を与えて Gemma に与えないのは不公平、との本人指摘 2026-07-11）
    "gemma-4-31b":       {"kind": "gemini", "id": "gemma-4-31b-it", "wrap": "ja_enforce"},
}

# Gemma 調教 v1: 言語指示を最前列と最後尾に（指示追従が弱いモデルは端の指示に従いやすい。
# 反証最小＝まず言語だけ直す。長さ・様式はこの版では触らない）
JA_ENFORCE_PREFIX = ("【最重要指示】回答は必ずすべて日本語で書くこと。英語での回答は禁止。\n"
                     "カード名は「日本語名（英語名）」の形式でよいが、説明文は日本語のみ。\n\n")
JA_ENFORCE_SUFFIX = "\n\n【再確認】回答は必ず日本語で書くこと。"

# USD per 1M tokens (in, out)。Nova はリポジトリ調査値・他は概算（凍結前に要確認）
PRICING = {
    "nova-lite":         (0.06, 0.24),
    "nova-pro":          (0.80, 3.20),
    "claude-haiku-4.5":  (1.00, 5.00),    # 概算・要確認
    "claude-sonnet-4.5": (3.00, 15.00),   # 概算・要確認
    "gemini-flash-lite": (0.10, 0.40),    # 概算・無料枠内なら $0
    "gemma-4-31b":       (0.0, 0.0),      # API 無料枠（2026-07-11 疎通確認済み）
}

# ── 試験クエリ（検索 kwargs 込み。ルーターを通さないぶん、本番でルーターが
#    立てるはずのフラグは既知の正解を手で与える＝全候補共通なので公平） ──
QUERIES = [
    {"query": "ゴルガリカラーの単体除去", "kwargs": {}},
    {"query": "ブラケット2で使えるラクドスカラーのマナ加速",
     "kwargs": {"filters": {"mana_producer": True}}},
    {"query": "コンボに使えるカード", "kwargs": {}},
    {"query": "最強の単体除去", "kwargs": {"tournament_boost": True, "removal_mode": True}},
    {"query": "条件付きカウンター呪文", "kwargs": {"counter_mode": True}},
    # 統率者クエリ改訂版（2026-07-11 本人設計）: EDH 意図を明示し、候補は固有色（赤緑）で
    # 揃えた手組みプール＋引っ掛け1枚。「発現する浅瀬」は青入り＝怒りの座の固有色違反だが
    # エレメンタルシナジー的には魅力満点＝解説レイヤーが固有色ルールを適用できるかを突く罠。
    # 統率者本体を先頭に置くのは、固有色の根拠（マナコスト）を文脈内で照合可能にするため
    {"query": "怒りの座、オムナスの統率者デッキと相性がいいカード",
     "kwargs": {},
     "card_names": [
         "Omnath, Locus of Rage",
         "Scapeshift", "Lotus Cobra", "Oracle of Mul Daya",
         "Rampaging Baloths", "Moraug, Fury of Akoum",
         "Tireless Provisioner", "Ancient Greenwarden", "Valakut Exploration",
         "Risen Reef",
     ]},
]


def fetch_cards_by_name(searcher, names):
    """手組みプール用: 指定カードを DB から検索経路と同じ形で取得（指定順を保持）。"""
    from mtg_rag_agent import get_archetypes
    with searcher.conn.cursor() as cur:
        cur.execute(
            "SELECT card_name, japanese_name, type_line, mana_cost, power, toughness,"
            " oracle_text, japanese_oracle_text, rarity"
            " FROM mtg_cards_v2 WHERE card_name = ANY(%s)", (names,))
        rows = {r[0]: r for r in cur.fetchall()}
    cards = []
    for n in names:
        r = rows.get(n)
        if not r:
            print(f"  [警告] 手組みプールに無いカード: {n}")
            continue
        cards.append({
            "card_name": r[0], "japanese_name": r[1], "type_line": r[2],
            "mana_cost": r[3], "power": r[4], "toughness": r[5],
            "oracle_text": r[6], "japanese_oracle_text": r[7], "rarity": r[8],
            "rrf_score": 0.0,
            "archetypes": get_archetypes(r[0], searcher.conn),
        })
    return cards


def load_env():
    if not os.path.exists(ENV_PATH):
        return
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_user_message(question: str, context: str) -> str:
    # mtg_rag_agent.ask_gemini と同一の問題用紙
    return (f"{SYSTEM_PROMPT}\n\n"
            f"以下のカード情報を参考に、質問に答えてください。\n\n"
            f"{context}\n\n【質問】\n{question}")


def call_bedrock(client, model_id: str, message: str, retries: int = 3):
    """一時故障（スロットリング等）はバックオフ付きで再試行。
    ResourceNotFound（用途フォーム未提出等）は恒久＝再試行しない。"""
    for attempt in range(retries):
        t0 = time.time()
        try:
            resp = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": message}]}],
                inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0.7},
            )
            ms = (time.time() - t0) * 1000
            text = resp["output"]["message"]["content"][0]["text"]
            u = resp.get("usage", {})
            return text, u.get("inputTokens", 0), u.get("outputTokens", 0), ms
        except Exception as e:
            name = type(e).__name__
            transient = any(k in str(e) or k in name for k in
                            ("Throttling", "ServiceUnavailable", "ModelNotReady",
                             "Timeout", "InternalServer"))
            if transient and attempt < retries - 1:
                time.sleep(4 * (2 ** attempt) + random.uniform(0, 1))
                continue
            raise


def call_gemini(model_id: str, message: str, api_key: str, retries: int = 3):
    """429/503 は本番 agent と同じく指数バックオフで再試行。"""
    import requests
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model_id}:generateContent")
    for attempt in range(retries):
        t0 = time.time()
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={"contents": [{"parts": [{"text": message}]}],
                  "generationConfig": {"maxOutputTokens": MAX_TOKENS, "temperature": 0.7}},
            timeout=60)
        ms = (time.time() - t0) * 1000
        if resp.status_code in (429, 503) and attempt < retries - 1:
            time.sleep(8 * (2 ** attempt) + random.uniform(0, 1))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:120]}")
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        um = data.get("usageMetadata", {})
        return text, um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0), ms


def list_profiles(region: str):
    import boto3
    bc = boto3.client("bedrock", region_name=region)
    print("口座で見える推論プロファイル（要 bedrock:ListInferenceProfiles）:")
    paginator = bc.get_paginator("list_inference_profiles") \
        if bc.can_paginate("list_inference_profiles") else None
    profiles = []
    if paginator:
        for page in paginator.paginate():
            profiles.extend(page.get("inferenceProfileSummaries", []))
    else:
        profiles = bc.list_inference_profiles().get("inferenceProfileSummaries", [])
    for p in profiles:
        print(f"  {p['inferenceProfileId']}")
    named = {c["id"] for c in CANDIDATES.values() if c["kind"] == "bedrock"}
    found = {p["inferenceProfileId"] for p in profiles}
    for mid in sorted(named):
        mark = "OK" if mid in found else "★不一致（名簿の ID を直すこと）"
        print(f"  名簿照合: {mid} → {mark}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="推論プロファイルの棚卸しのみ")
    ap.add_argument("--dry", action="store_true", help="計画とコスト概算のみ（API 呼ばない）")
    ap.add_argument("--models", default=None, help="カンマ区切りで受験生を絞る")
    ap.add_argument("--top_k", type=int, default=10)
    ap.add_argument("--go", action="store_true",
                    help="課金モデルを含む走行の明示承認。これが無い限り課金走行は"
                         "スクリプト自身が拒否する（2026-07-11 無許可走行事故の再発防止）")
    args = ap.parse_args()

    load_env()
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    if args.list:
        list_profiles(region)
        return

    models = dict(CANDIDATES)
    if args.models:
        wanted = [m.strip() for m in args.models.split(",")]
        unknown = [m for m in wanted if m not in models]
        if unknown:
            print(f"エラー: 名簿に無い受験生 {unknown} / 名簿={list(models)}")
            sys.exit(1)
        models = {k: v for k, v in models.items() if k in wanted}

    gem_key = os.environ.get("GOOGLE_API_KEY", "")
    if not gem_key:
        skipped = [k for k, v in models.items() if v["kind"] == "gemini"]
        if skipped:
            print(f"注意: GOOGLE_API_KEY が .env に無い → {skipped} は欠席扱い")
        models = {k: v for k, v in models.items() if v["kind"] != "gemini"}

    print(f"受験生: {list(models)} / クエリ {len(QUERIES)} 本 / "
          f"呼び出し {len(models) * len(QUERIES)} 回")

    # ── 金庫の鍵（2026-07-11）: 課金モデルを含む走行は --go 必須 ──
    paid = [m for m in models if PRICING.get(m, (1, 1)) != (0.0, 0.0)]
    if paid and not args.dry and not args.go:
        est_in, est_out = 3000, 600
        est = sum((est_in / 1e6 * PRICING[m][0] + est_out / 1e6 * PRICING[m][1])
                  * len(QUERIES) for m in paid)
        print(f"\n【課金停止】課金モデル {paid} を含む走行には --go が必要。")
        print(f"  実行した場合の概算: ${est:.4f}")
        print(f"  承認するなら同じコマンドに --go を付けて再実行。")
        sys.exit(2)

    if args.dry:
        est_in, est_out = 3000, 600
        total = sum((est_in / 1e6 * PRICING[m][0] + est_out / 1e6 * PRICING[m][1])
                    * len(QUERIES) for m in models)
        print(f"コスト概算（1回答 in{est_in}/out{est_out}想定）: ${total:.4f}")
        return

    import boto3
    client = boto3.client("bedrock-runtime", region_name=region)
    searcher = MTGHybridSearcherV2("SMALL_V2")

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out_dir = f"{OUT_ROOT}/bakeoff_{ts}"
    os.makedirs(out_dir, exist_ok=True)

    sheet = ["# 解説レイヤー ブラインド試飲会 採点シート",
             f"\n実施: {ts} / 受験生 {len(models)} 名（名前は封印）/ "
             f"採点後に answer_key.json を開けること\n",
             "採点方法（軽量版・2026-07-10 本人フィードバックで多軸5段階から変更）: "
             "各クエリで一番良い回答に ◎ を1つ。捏造・用語誤りを見つけたら ✗。"
             "余力があれば一番ダメな回答に ▲。それだけ。\n"]
    key = {"ts": ts, "queries": {}}
    costs = {m: {"in": 0, "out": 0, "ms": []} for m in models}

    for qi, spec in enumerate(QUERIES, 1):
        q = spec["query"]
        print(f"\n[{qi}/{len(QUERIES)}] {q}")
        if spec.get("card_names"):
            cards = fetch_cards_by_name(searcher, spec["card_names"])
        else:
            cards, fmt = search_cards(searcher, q, args.top_k, None, **spec["kwargs"])
        context = build_context(cards)
        message = build_user_message(q, context)

        order = list(models.keys())
        random.shuffle(order)   # クエリごとに割り当てを変える＝文体追跡による目隠し破り対策
        key["queries"][q] = {}

        sheet.append(f"\n---\n\n## Q{qi}. {q}\n")
        sheet.append("検索で渡したカード（全回答共通の材料）: "
                     + "、".join(c["card_name"] for c in cards[:args.top_k]) + "\n")

        for li, name in enumerate(order):
            label = chr(ord("A") + li)
            spec_m = models[name]
            msg = message
            if spec_m.get("wrap") == "ja_enforce":
                msg = JA_ENFORCE_PREFIX + message + JA_ENFORCE_SUFFIX
            try:
                if spec_m["kind"] == "bedrock":
                    text, tin, tout, ms = call_bedrock(client, spec_m["id"], msg)
                else:
                    text, tin, tout, ms = call_gemini(spec_m["id"], msg, gem_key)
                costs[name]["in"] += tin
                costs[name]["out"] += tout
                costs[name]["ms"].append(ms)
                print(f"  回答{label} ← 生成 OK ({ms:.0f}ms)")
            except Exception as e:
                text = f"（生成失敗: {type(e).__name__}: {str(e)[:120]}）"
                print(f"  回答{label} ← 失敗: {str(e)[:80]}")
            key["queries"][q][f"回答{label}"] = name
            ja_ratio = len(JA_RE.findall(text)) / max(len(text), 1)
            lang_flag = ""
            if not text.startswith("（生成失敗") and ja_ratio < 0.25:
                lang_flag = (f"⚠ **自動判定: 日本語率 {ja_ratio:.0%}＝"
                             f"「日本語で回答する」指示違反 → 読まなくてよい（欠格）**\n\n")
                print(f"    ⚠ 回答{label} は日本語率 {ja_ratio:.0%}＝欠格として印字")
            sheet.append(f"### 回答{label}\n\n{lang_flag}{text}\n\n"
                         f"判定: 　（◎=一番良い ／ ▲=一番ダメ〔任意〕 ／ "
                         f"✗=捏造・用語誤り〔複数可〕 ／ 空欄=普通）\n")
            time.sleep(1.0)

    with open(f"{out_dir}/grading_sheet.md", "w", encoding="utf-8") as f:
        f.write("\n".join(sheet))
    with open(f"{out_dir}/answer_key.json", "w", encoding="utf-8") as f:
        json.dump(key, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    cost_rows = {}
    for name, c in costs.items():
        pin, pout = PRICING[name]
        usd = c["in"] / 1e6 * pin + c["out"] / 1e6 * pout
        lat = sum(c["ms"]) / len(c["ms"]) if c["ms"] else 0
        cost_rows[name] = {"tokens_in": c["in"], "tokens_out": c["out"],
                           "usd": round(usd, 6), "lat_avg_ms": round(lat)}
        print(f"  {name:20s} in={c['in']:6,d} out={c['out']:5,d} "
              f"${usd:.4f} / 平均{lat:.0f}ms")
    with open(f"{out_dir}/costs.json", "w", encoding="utf-8") as f:
        json.dump(cost_rows, f, ensure_ascii=False, indent=2)
    print(f"\n採点シート: {out_dir}/grading_sheet.md")
    print(f"封印キー:   {out_dir}/answer_key.json（採点が終わるまで開けない）")
    print(f"コスト:     {out_dir}/costs.json（cost_ledger へ転記）")
    searcher.close()


if __name__ == "__main__":
    main()
