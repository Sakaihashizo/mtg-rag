"""
ollama_router_test.py — ローカル 7B（Ollama/qwen2.5）を LLM ルーターとして試験する
================================================================================
bedrock_router_test.py と同じ比較設計（Gemini キャッシュ30件が参照・同一検証）。
呼び先が Windows ホストの Ollama（VirtualBox NAT: 10.0.2.2:11434）になる。

2条件:
  --cond a : 本番 REWRITE_PROMPT をそのまま（Gemini とドロップイン差し替え可能かを測る）
  --cond b : Fable 調教版プロンプト＋ Ollama の format=json 制約デコード
             （7B に合わせて書き直したらどこまで行けるかを測る。
              前回の弱点＝MTG用語の誤訳（接死→"death"）を用語辞書で塞ぐ・
              全キー必須の完全な few-shot でスキーマ逸脱を抑える）

使い方:
  python ollama_router_test.py --cond b --smoke   # 1クエリだけ（動作確認）
  python ollama_router_test.py --cond a
  python ollama_router_test.py --cond b
"""

import argparse
import json
import sys
import time
import os
from datetime import datetime

import requests

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_rag_agent import REWRITE_PROMPT
from bedrock_router_test import parse_and_validate, JA_RE

OLLAMA_URL = "http://10.0.2.2:11434/api/chat"
MODEL = "qwen2.5:7b-instruct-q4_K_M"
CACHE_PATH = "/mnt/mtg_rag/eval_router_cache.json"
OUT_DIR = "/mnt/mtg_rag/docs/me"

# ── 条件B: Fable 調教版 ─────────────────────────────────────────
# 7B 向けの設計原則: (1) スキーマを先頭に・全キー必須 (2) 規則は短く番号で
# (3) MTG 用語辞書で HyDE の誤訳を塞ぐ (4) few-shot は全キー入りの完全形
# (5) 日本語フィールドへの英語・中国語混入を明示的に禁止（qwen 対策）
# プレースホルダは <<QUERY>>（.format の brace エスケープ地獄を回避）。
FABLE_PROMPT = """あなたはMagic: The Gathering検索クエリの解析器。次のJSONだけを出力する（説明文・マークダウン禁止・キーは全部必ず含める）:
{"search_query": "", "hyde_text": "", "ja_hyde_text": "", "tournament_boost": false, "removal_mode": false, "counter_mode": false, "type_filter": null, "format": null, "mana_producer": false, "cmc_min": null, "cmc_max": null, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

規則:
1. search_query = クエリの核心の日本語キーワードのみ。「〜を3枚選んで」「教えて」等の指示語は捨てる。
2. hyde_text = クエリに理想的な架空カードの英語ルールテキスト1〜3文。下の用語辞書の英語を必ず使う。実在カードの丸写し禁止。
3. **クエリが抽象的（「強い」「環境」「コンボ」「相性」等）なときこそ、クエリの言い換えは禁止。** 具体的なゲーム機構でカードテキストを創作する（例: マナ・コストを支払わず唱えられる代替コスト、低コストで過剰な効果、除去＋ドローの複合、カード・アドバンテージ）。形容詞だけの文（"A powerful card."）は失格。
4. ja_hyde_text = hyde_text と同じ内容の日本語カードテキスト。日本語のみ（英語・中国語を混ぜない）。
5. tournament_boost = 「最強」「強い」「環境」「メタ」「tier」「純粋に強い」等の強さ・実績の語があれば true。
6. removal_mode = 除去（破壊・追放・対処）を探すクエリなら true。counter_mode = 打ち消し呪文を探すクエリなら true。
7. type_filter: クリーチャーを探す→"Creature" ／ カウンター呪文を探す→"Instant" ／ 明示があれば ソーサリー→"Sorcery"・エンチャント→"Enchantment"・アーティファクト→"Artifact" ／ **除去を探すクエリは null**（除去はインスタントとソーサリーの両方にある）／ 指定なし→null。「クリーチャーを破壊/追放する」はクリーチャーを探すのではなく除去を探すクエリ→null。
8. format: スタンダード→"standard" ／ パイオニア→"pioneer" ／ モダン→"modern" ／ レガシー→"legacy" ／ ヴィンテージ→"vintage" ／ パウパー→"pauper" ／ 指定なし→null。フォーマット語は search_query から除く。
9. 数値: 「Nマナ」→cmc_min=N かつ cmc_max=N ／「Nマナ以下」→cmc_max=N ／「Nマナ以上」→cmc_min=N。パワー・タフネスも同様。**クエリに数字が書かれていなければ、cmc/power/toughness は必ず全部 null（下の例の数値を流用しない）。**「強い」「重い」等の曖昧語は数値ではない。
10. mana_producer = マナクリーチャー・マナ加速・マナを生む/出す/伸ばす系のクエリなら true。

用語辞書（日本語→英語。hyde_text ではこの英語を使うこと）:
飛行=flying ／ 接死=deathtouch ／ トランプル=trample ／ 速攻=haste ／ 破壊不能=indestructible ／ 警戒=vigilance ／ 絆魂=lifelink ／ 先制攻撃=first strike ／ 到達=reach ／ 呪禁=hexproof ／ 打ち消す=counter target spell ／ 追放する=exile ／ 破壊する=destroy ／ カードを引く=draw a card ／ 単体除去=destroy target creature ／ マナ加速=add mana

例1 入力「トランプルを持つクリーチャー」
{"search_query": "トランプルを持つクリーチャー", "hyde_text": "Creature with trample.", "ja_hyde_text": "トランプルを持つクリーチャー。", "tournament_boost": false, "removal_mode": false, "counter_mode": false, "type_filter": "Creature", "format": null, "mana_producer": false, "cmc_min": null, "cmc_max": null, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

例2 入力「2マナ以下のカウンター呪文」
{"search_query": "カウンター呪文", "hyde_text": "Counter target spell.", "ja_hyde_text": "呪文1つを対象とし、それを打ち消す。", "tournament_boost": false, "removal_mode": false, "counter_mode": true, "type_filter": "Instant", "format": null, "mana_producer": false, "cmc_min": null, "cmc_max": 2, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

例3 入力「モダンの最強の単体除去を教えて」
{"search_query": "最強の単体除去", "hyde_text": "Destroy target creature.", "ja_hyde_text": "クリーチャー1体を対象とし、それを破壊する。", "tournament_boost": true, "removal_mode": true, "counter_mode": false, "type_filter": null, "format": "modern", "mana_producer": false, "cmc_min": null, "cmc_max": null, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

例4 入力「1マナのマナクリーチャー」
{"search_query": "マナを生むクリーチャー", "hyde_text": "Creature. {T}: Add one mana of any color.", "ja_hyde_text": "クリーチャー。{T}：好きな色のマナ1点を加える。", "tournament_boost": false, "removal_mode": false, "counter_mode": false, "type_filter": "Creature", "format": null, "mana_producer": true, "cmc_min": 1, "cmc_max": 1, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

例5 入力「純粋に強いカード」（抽象クエリ→機構で具体化する。言い換え禁止の見本）
{"search_query": "純粋に強いカード", "hyde_text": "You may cast this spell without paying its mana cost by exiling a card from your hand. Draw two cards, then destroy target creature.", "ja_hyde_text": "あなたは、手札からカード1枚を追放することで、この呪文をマナ・コストを支払うことなく唱えてもよい。カードを2枚引き、その後クリーチャー1体を対象とし、それを破壊する。", "tournament_boost": true, "removal_mode": false, "counter_mode": false, "type_filter": null, "format": null, "mana_producer": false, "cmc_min": null, "cmc_max": null, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

クエリ: <<QUERY>>
JSON:"""


def build_prompt(cond: str, query: str) -> str:
    if cond == "a":
        return REWRITE_PROMPT.format(query=query)
    return FABLE_PROMPT.replace("<<QUERY>>", query)


def call_ollama(cond: str, query: str, timeout=180):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": build_prompt(cond, query)}],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 512},
    }
    if cond == "b":
        body["format"] = "json"   # 制約デコード＝調教の一部（条件Bのみ）
    t0 = time.time()
    r = requests.post(OLLAMA_URL, json=body, timeout=timeout)
    ms = (time.time() - t0) * 1000
    r.raise_for_status()
    d = r.json()
    text = d["message"]["content"]
    pe = d.get("prompt_eval_count", 0)
    ev = d.get("eval_count", 0)
    return text, pe, ev, ms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cond", choices=["a", "b"], required=True)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()

    cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    entries = cache["entries"]
    queries = list(entries.keys())
    if args.smoke:
        queries = queries[:1]

    label = "A(本番プロンプト)" if args.cond == "a" else "B(Fable調教版+json制約)"
    print(f"条件{label} / model={MODEL} / {len(queries)} クエリ")

    results = {}
    lats = []
    for i, q in enumerate(queries, 1):
        try:
            text, pe, ev, ms = call_ollama(args.cond, q)
            entry = parse_and_validate(text, q)
            err = None
        except Exception as e:
            entry, err = None, f"{type(e).__name__}: {str(e)[:120]}"
            pe = ev = 0
            ms = 0.0
        if ms:
            lats.append(ms)
        results[q] = {"llm": entry, "error": err,
                      "tokens": [pe, ev], "latency_ms": round(ms)}
        mark = "OK" if err is None else f"失敗: {err[:80]}"
        print(f"  [{i}/{len(queries)}] {q[:24]} → {mark} ({ms/1000:.1f}s)")

    if args.smoke:
        q = queries[0]
        if results[q]["llm"]:
            print("\n--- smoke 出力 ---")
            print(json.dumps(results[q]["llm"], ensure_ascii=False, indent=2))
        return

    # ── Gemini キャッシュとの突き合わせ（bedrock 版と同一指標）──
    STRUCT = ["tournament_boost", "removal_mode", "counter_mode",
              "type_filter", "format", "filters"]
    agree = {k: 0 for k in STRUCT}
    sq_same = hyde_ok = ja_ok = n_ok = 0
    diffs = []
    for q in queries:
        r = results[q]
        if r["error"]:
            continue
        n_ok += 1
        b, g = r["llm"], entries[q]
        row_diff = []
        for k in STRUCT:
            if b[k] == g.get(k) or (k == "filters" and b[k] == (g.get(k) or {})):
                agree[k] += 1
            else:
                row_diff.append(f"{k}: gemini={g.get(k)!r} 7B={b[k]!r}")
        if b["search_query"] == g.get("search_query"):
            sq_same += 1
        if b["hyde_text"].strip():
            hyde_ok += 1
        if b["ja_hyde_text"].strip() and JA_RE.search(b["ja_hyde_text"]):
            ja_ok += 1
        if row_diff:
            diffs.append((q, row_diff))

    lat_avg = sum(lats) / len(lats) if lats else 0
    lat_p95 = sorted(lats)[int(len(lats) * 0.95) - 1] if lats else 0
    print("\n" + "=" * 60)
    print(f"  条件{label} / 成功 {n_ok}/{len(queries)}")
    for k in STRUCT:
        print(f"  {k:>18}: {agree[k]}/{n_ok} 一致")
    print(f"  {'search_query':>18}: {sq_same}/{n_ok} 完全一致")
    print(f"  {'hyde_en 非空':>18}: {hyde_ok}/{n_ok}")
    print(f"  {'ja_hyde 日本語':>18}: {ja_ok}/{n_ok}")
    print(f"  レイテンシ: 平均 {lat_avg/1000:.1f}s / p95 {lat_p95/1000:.1f}s")
    print("=" * 60)

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out = {"meta": {"model": MODEL, "cond": args.cond, "ts": ts,
                    "lat_avg_ms": round(lat_avg), "lat_p95_ms": round(lat_p95)},
           "agreement": {**agree, "n_ok": n_ok, "search_query_exact": sq_same,
                         "hyde_en_nonempty": hyde_ok, "ja_hyde_japanese": ja_ok},
           "results": results}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = f"{OUT_DIR}/ollama_router_test_{ts}_cond{args.cond}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"生出力を保存: {path}")
    if diffs:
        print(f"\n不一致 {len(diffs)} クエリ:")
        for q, d in diffs:
            print(f"  [{q}]")
            for line in d:
                print(f"    {line}")


if __name__ == "__main__":
    main()
