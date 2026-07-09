"""
bedrock_router_test.py — Bedrock を LLM ルーターとして試験し、Gemini キャッシュと突き合わせる
================================================================================
目的: ルーター役を Gemini 2.5 Flash-Lite（無料枠・日次クォータあり）から
Bedrock の小型モデル（Nova Micro 想定・従量課金・クォータ実質なし・serverless）へ
置き換えられるかの品質・コスト・レイテンシ実測。

公平性の担保:
  - プロンプトは本番と同一（mtg_rag_agent.REWRITE_PROMPT をそのまま使用）
  - JSON 検証も rewrite_query と同一ロジック（型検証・ホワイトリスト・範囲チェック）
  - 比較対象は eval_router_cache.json（Gemini の30クエリ分・prompt_sha 一致を確認）
  ※ Gemini 出力は「参照」であって正解ではない。不一致は良し悪しを目視判断する。

認証: .env（gitignore 済み）の AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
AWS_DEFAULT_REGION を読む（キーは表示しない）。

使い方:
  PYTHONPATH=/home/claude/pylibs python bedrock_router_test.py --check   # 接続確認のみ（1クエリ）
  PYTHONPATH=/home/claude/pylibs python bedrock_router_test.py           # 30クエリ比較
  PYTHONPATH=/home/claude/pylibs python bedrock_router_test.py --model us.amazon.nova-lite-v1:0
"""

import argparse
import json
import os
import re
import sys
import time
import hashlib
from datetime import datetime

sys.path.insert(0, '/mnt/mtg_rag')
from mtg_rag_agent import REWRITE_PROMPT, detect_format, FORMAT_KEYWORDS

CACHE_PATH = "/mnt/mtg_rag/eval_router_cache.json"
ENV_PATH = "/mnt/mtg_rag/.env"
OUT_DIR = "/mnt/mtg_rag/docs/me"

# ── 調教版プロンプト（2026-07-09・Fable。設計文書: docs/me/nova_router_tuning_v1_20260709.md）
# 7B v3（FABLE_PROMPT・ollama_router_test.py）を Nova 固有の故障（search_query 圧縮・
# ja_hyde 用語捏造・boost 過剰発火）向けに移植。プレースホルダは <<QUERY>>。
NOVA_PROMPT = """あなたはMagic: The Gathering検索クエリの解析器。次のJSONだけを出力する（説明文・マークダウン禁止・キーは全部必ず含める）:
{"search_query": "", "hyde_text": "", "ja_hyde_text": "", "tournament_boost": false, "removal_mode": false, "counter_mode": false, "type_filter": null, "format": null, "mana_producer": false, "cmc_min": null, "cmc_max": null, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

規則:
1. search_query = 入力クエリから指示語（「〜を3枚選んで」「教えて」「おすすめは」等）だけを取り除いたもの。それ以外は削らない・言い換えない・要約しない・翻訳しない。機構語（追放する・破壊する・打ち消す）、機能語（除去・ドロー・フィルタリング・手札補充・マナ加速）、属性修飾（青い・クリーチャーを・インスタントで）は必ずそのまま残す（例:「クリーチャーを追放する除去」→そのまま。「除去」に縮めるのは失格）。英語のクエリは英語のまま。例外は2つだけ: フォーマット語は format フィールドへ移して search_query から除く。数値表現（Nマナ等）は cmc/power/toughness フィールドへ移してよい。
2. hyde_text = クエリに理想的な架空カードの英語ルールテキスト1〜3文。下の用語辞書の英語を必ず使う。実在カードの丸写し禁止。
3. クエリが抽象的（「強い」「環境」「コンボ」「相性」等）なときこそ、クエリの言い換えは禁止。具体的なゲーム機構でカードテキストを創作する（例: マナ・コストを支払わず唱えられる代替コスト、低コストで過剰な効果、除去＋ドローの複合、カード・アドバンテージ）。形容詞だけの文（"A powerful card."）は失格。
4. キーワード能力（飛行・接死・トランプル等）を問うクエリの hyde_text は、キーワード名を書くだけでよい（"Creature with trample."）。キーワードの意味・ルールの説明文を自作しない。クエリに無い能力（"can't be blocked" 等）を付け足さない。
5. ja_hyde_text = hyde_text と同じ内容の日本語カードテキスト。日本語のみ（日本語以外の言語を混ぜない）。用語は下の辞書の日本語表記だけを使う。辞書に無い訳語を発明しない（「死触」「踏み潰し」等は存在しない用語＝失格）。
6. tournament_boost = 次の語がクエリに明示されているときだけ true: 最強・強い・強力・純粋に強い・環境・メタ・tier・採用率・定番・必須・勝てる・優勝・競技。機能語だけのクエリ（除去・ドロー・マナ加速・打ち消し・キーワード能力・「クリーチャーを破壊/追放する」等）では絶対に true にしない（機能を探すことと強さを問うことは別）。
7. removal_mode = 除去（破壊・追放・対処）を探すクエリなら true。counter_mode = 打ち消し呪文を探すクエリなら true。
8. type_filter は「クエリが探している主役」で判定する。クリーチャーが主役（「飛行を持つクリーチャー」「環境で強いクリーチャー」のように"…クリーチャー"を探している）→"Creature"。「クリーチャーを破壊する」「クリーチャーを追放する」のように目的語に出るだけ（探しているのは除去）→null（「クリーチャー」という字面に反応しない）。カウンター呪文（打ち消し）を探す→"Instant"。"Instant" にしてよいのは、クエリが「インスタント」「瞬速」を明示するか、打ち消し呪文を探すときだけ＝「手札補充」「ドロー」等の機能語からカードの型を推測しない（負例:「手札補充できる青いカード」→ type_filter は null。「青い」はカードの色であって型指定ではない。手札補充はエンチャントにもソーサリーにもある。正例:「インスタントでドローできるカード」→ "Instant"。「インスタント」という型の語が明示されているから）。明示があれば ソーサリー→"Sorcery"・エンチャント→"Enchantment"・アーティファクト→"Artifact"。除去を探すクエリは null（除去はインスタントとソーサリーの両方にある）。指定なし→null。
9. format: スタンダード→"standard" ／ パイオニア→"pioneer" ／ モダン→"modern" ／ レガシー→"legacy" ／ ヴィンテージ→"vintage" ／ パウパー→"pauper" ／ 指定なし→null。フォーマット語は search_query から除く。
10. 数値: 「Nマナ」→cmc_min=N かつ cmc_max=N ／「Nマナ以下」→cmc_max=N ／「Nマナ以上」→cmc_min=N。パワー・タフネスも同様。クエリに数字が書かれていなければ、cmc/power/toughness は必ず全部 null（下の例の数値を流用しない）。「強い」「重い」等の曖昧語は数値ではない。「コンボ」「シナジー」「アグロ」等の戦略語からコストを推測するのも禁止（例:「コンボに使えるカード」→ 全部 null）。「ドロー」「フィルタリング」「手札補充」等の能力語からも数値を発明しない。規則3の機構の具体化は hyde_text / ja_hyde_text の中だけで行い、数値フィールドには反映しない。
11. mana_producer = クエリに「マナ」の語が明示されているときだけ true（マナクリーチャー・マナ加速・マナを生む/出す/伸ばす・マナファクト等）。**「マナ」の語が無いクエリでは必ず false**（打ち消し・除去・ドロー・カウンター等のクエリで true にしない。負例:「条件付きカウンター呪文」→ mana_producer は false。hyde_text に「支払わないかぎり打ち消す」等のマナ支払い条件を書いたとしても、それはカウンターの条件であってマナ加速ではない）。マナ加速とは自分のマナを増やすこと（土地・マナ・アーティファクト・マナクリーチャー）。墓地からの踏み倒しや代替コストはマナ加速ではない。

用語辞書（日本語表記=英語。ja_hyde_text は左の日本語表記・hyde_text は右の英語を必ず使う）:
飛行=flying ／ 接死=deathtouch ／ トランプル=trample ／ 速攻=haste ／ 破壊不能=indestructible ／ 警戒=vigilance ／ 絆魂=lifelink ／ 先制攻撃=first strike ／ 到達=reach ／ 呪禁=hexproof ／ 打ち消す=counter target spell ／ 追放する=exile ／ 破壊する=destroy ／ カードを引く=draw a card ／ 単体除去=destroy target creature ／ マナ加速=add mana

例1 入力「トランプルを持つクリーチャー」
{"search_query": "トランプルを持つクリーチャー", "hyde_text": "Creature with trample.", "ja_hyde_text": "トランプルを持つクリーチャー。", "tournament_boost": false, "removal_mode": false, "counter_mode": false, "type_filter": "Creature", "format": null, "mana_producer": false, "cmc_min": null, "cmc_max": null, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

例2 入力「2マナ以下のカウンター呪文」
{"search_query": "カウンター呪文", "hyde_text": "Counter target spell.", "ja_hyde_text": "呪文1つを対象とし、それを打ち消す。", "tournament_boost": false, "removal_mode": false, "counter_mode": true, "type_filter": "Instant", "format": null, "mana_producer": false, "cmc_min": null, "cmc_max": 2, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

例3 入力「クリーチャーを追放する除去」（機構語を search_query に保持する見本・type_filter は null）
{"search_query": "クリーチャーを追放する除去", "hyde_text": "Exile target creature.", "ja_hyde_text": "クリーチャー1体を対象とし、それを追放する。", "tournament_boost": false, "removal_mode": true, "counter_mode": false, "type_filter": null, "format": null, "mana_producer": false, "cmc_min": null, "cmc_max": null, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

例4 入力「モダンの最強の単体除去を教えて」
{"search_query": "最強の単体除去", "hyde_text": "Destroy target creature.", "ja_hyde_text": "クリーチャー1体を対象とし、それを破壊する。", "tournament_boost": true, "removal_mode": true, "counter_mode": false, "type_filter": null, "format": "modern", "mana_producer": false, "cmc_min": null, "cmc_max": null, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

例5 入力「1マナのマナクリーチャー」（数値は filters へ移す・それ以外は言い換えない）
{"search_query": "マナクリーチャー", "hyde_text": "Creature. {T}: Add one mana of any color.", "ja_hyde_text": "クリーチャー。{T}：好きな色のマナ1点を加える。", "tournament_boost": false, "removal_mode": false, "counter_mode": false, "type_filter": "Creature", "format": null, "mana_producer": true, "cmc_min": 1, "cmc_max": 1, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

例6 入力「destroy target creature」（英語クエリは英語のまま）
{"search_query": "destroy target creature", "hyde_text": "Destroy target creature.", "ja_hyde_text": "クリーチャー1体を対象とし、それを破壊する。", "tournament_boost": false, "removal_mode": true, "counter_mode": false, "type_filter": null, "format": null, "mana_producer": false, "cmc_min": null, "cmc_max": null, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

例7 入力「純粋に強いカード」（抽象クエリ→機構で具体化する。言い換え禁止の見本）
{"search_query": "純粋に強いカード", "hyde_text": "You may cast this spell without paying its mana cost by exiling a card from your hand. Draw two cards, then destroy target creature.", "ja_hyde_text": "あなたは、手札からカード1枚を追放することで、この呪文をマナ・コストを支払うことなく唱えてもよい。カードを2枚引き、その後クリーチャー1体を対象とし、それを破壊する。", "tournament_boost": true, "removal_mode": false, "counter_mode": false, "type_filter": null, "format": null, "mana_producer": false, "cmc_min": null, "cmc_max": null, "power_min": null, "power_max": null, "toughness_min": null, "toughness_max": null}

クエリ: <<QUERY>>
JSON:"""

# 1M トークンあたり USD（2026-02〜04 調査・us-east-1）
PRICING = {
    "us.amazon.nova-micro-v1:0": (0.035, 0.14),
    "us.amazon.nova-lite-v1:0":  (0.06, 0.24),
    "us.amazon.nova-pro-v1:0":   (0.80, 3.20),
}

JA_RE = re.compile(r'[ぁ-んァ-ン一-龥]')


def load_env():
    """`.env` から AWS_* を環境変数へ（値は表示しない）。"""
    if not os.path.exists(ENV_PATH):
        return []
    loaded = []
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if line.startswith("AWS_") and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            loaded.append(k.strip())
    return loaded


def parse_and_validate(raw_text: str, query: str):
    """rewrite_query と同一の検証ロジック（Gemini 版と揃える）。"""
    raw_text = raw_text.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw_text)
    entry = {
        "search_query": parsed.get("search_query", query),
        "hyde_text": parsed.get("hyde_text", ""),
        "ja_hyde_text": parsed.get("ja_hyde_text", ""),
        "tournament_boost": bool(parsed.get("tournament_boost", False)),
        "removal_mode": bool(parsed.get("removal_mode", False)),
        "counter_mode": bool(parsed.get("counter_mode", False)),
        "type_filter": parsed.get("type_filter", None),
    }
    fmt_raw = parsed.get("format")
    fmt = (fmt_raw.lower()
           if isinstance(fmt_raw, str) and fmt_raw.lower() in set(FORMAT_KEYWORDS.values())
           else None)
    if fmt is None:
        fmt = detect_format(query)
    entry["format"] = fmt

    def _vint(key):
        try:
            n = int(parsed.get(key))
        except (ValueError, TypeError):
            return None
        return n if 0 <= n <= 99 else None
    filters = {}
    for k in ("cmc_min", "cmc_max", "power_min", "power_max",
              "toughness_min", "toughness_max"):
        v = _vint(k)
        if v is not None:
            filters[k] = v
    if bool(parsed.get("mana_producer", False)):
        filters["mana_producer"] = True
    entry["filters"] = filters
    return entry


def build_prompt(query: str, tuned: bool) -> str:
    return (NOVA_PROMPT.replace("<<QUERY>>", query) if tuned
            else REWRITE_PROMPT.format(query=query))


def call_bedrock(client, model_id: str, query: str, tuned: bool = False,
                 max_tokens: int = 512, temperature: float = 0.1):
    t0 = time.time()
    resp = client.converse(
        modelId=model_id,
        messages=[{"role": "user",
                   "content": [{"text": build_prompt(query, tuned)}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    ms = (time.time() - t0) * 1000
    text = resp["output"]["message"]["content"][0]["text"]
    usage = resp.get("usage", {})
    return text, usage.get("inputTokens", 0), usage.get("outputTokens", 0), ms


def canon_structured(entry: dict) -> str:
    """hammer 用: テキスト欄を除いた構造化フィールドだけの正規化文字列。"""
    keys = ["search_query", "tournament_boost", "removal_mode", "counter_mode",
           "type_filter", "format", "filters"]
    return json.dumps({k: entry.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)


def run_hammer(client, model_id: str, tuned: bool, n: int):
    """代表クエリを N 連打し、構造化フィールドのバリアント数を数える（temp0 決定性試験）。
    設計: docs/me/nova_router_tuning_v1_20260709.md §4-3（7B hammer_router.py と同じ狙い）。"""
    reps = ["コンボに使えるカード", "1マナのマナクリーチャー",
           "2マナ以下のカウンター呪文", "接死を持つクリーチャー"]
    print(f"hammer: {len(reps)} クエリ × {n} 連打 / temp=0 / model={model_id} / "
          f"tuned={tuned}")
    for q in reps:
        variants: dict[str, int] = {}
        errs = 0
        for _ in range(n):
            try:
                text, _, _, _ = call_bedrock(client, model_id, q, tuned=tuned,
                                             temperature=0.0)
                entry = parse_and_validate(text, q)
                key = canon_structured(entry)
                variants[key] = variants.get(key, 0) + 1
            except Exception:
                errs += 1
            time.sleep(0.15)
        top = max(variants.values()) if variants else 0
        print(f"  [{q}] variants={len(variants)} 最頻値={top}/{n} エラー={errs}")
        if len(variants) > 1:
            for k, c in sorted(variants.items(), key=lambda x: -x[1]):
                print(f"      {c:>3}回: {k[:140]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="us.amazon.nova-micro-v1:0")
    ap.add_argument("--check", action="store_true", help="接続確認のみ（1クエリ）")
    ap.add_argument("--tuned", action="store_true",
                    help="NOVA_PROMPT（調教版・2026-07-09）を使う。素の REWRITE_PROMPT との比較用")
    ap.add_argument("--hammer", type=int, default=0,
                    help="代表4クエリをN回連打しtemp0決定性を測る（30本比較はスキップ）")
    ap.add_argument("--only", default=None,
                    help="カンマ区切りのクエリ（キャッシュのキーと完全一致）だけ再走。"
                         "プロンプト変更の差分再測定用＝コスト抑えめの規律")
    ap.add_argument("--temp", type=float, default=0.1,
                    help="温度。本番想定は 0（貪欲＝決定的・7B の調教の型と同じ）")
    args = ap.parse_args()

    loaded = load_env()
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        print("エラー: AWS 認証が見つからない。.env に AWS_ACCESS_KEY_ID /"
              " AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION を追記してください。")
        sys.exit(1)
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    print(f".env から読み込み: {loaded} / region={region} / model={args.model} "
          f"/ tuned={args.tuned}")

    import boto3
    client = boto3.client("bedrock-runtime", region_name=region)

    if args.hammer:
        run_hammer(client, args.model, args.tuned, args.hammer)
        return

    cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    entries = cache["entries"]
    prompt_sha = hashlib.sha256(REWRITE_PROMPT.encode("utf-8")).hexdigest()[:12]
    sha_ok = cache["meta"].get("prompt_sha") == prompt_sha
    print(f"Gemini キャッシュ: {len(entries)} 件 / prompt_sha 一致: {sha_ok}")
    if not sha_ok and not args.tuned:
        print("警告: プロンプトがキャッシュ生成時と異なる＝比較の前提が崩れてる。中断。")
        sys.exit(1)
    if args.tuned:
        print("調教版（NOVA_PROMPT）＝Gemini キャッシュとは別プロンプト。"
              "sha 不一致は想定内・審判は出力の一致率（design doc §4-1 参照）。")

    queries = list(entries.keys())
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        missing = [w for w in wanted if w not in entries]
        if missing:
            print(f"エラー: キャッシュに無いクエリ: {missing}")
            sys.exit(1)
        queries = wanted
        print(f"差分再走: {len(queries)} 本のみ")
    if args.check:
        queries = queries[:1]

    results = {}
    tot_in = tot_out = 0
    lats = []
    for i, q in enumerate(queries, 1):
        try:
            text, tin, tout, ms = call_bedrock(client, args.model, q, tuned=args.tuned,
                                               temperature=args.temp)
            entry = parse_and_validate(text, q)
            err = None
        except Exception as e:
            entry, err = None, f"{type(e).__name__}: {e}"
            tin = tout = 0
            ms = 0.0
        tot_in += tin
        tot_out += tout
        if ms:
            lats.append(ms)
        results[q] = {"bedrock": entry, "error": err,
                      "tokens": [tin, tout], "latency_ms": round(ms)}
        mark = "OK" if err is None else f"失敗: {err[:90]}"
        print(f"  [{i}/{len(queries)}] {q[:24]} → {mark} ({ms:.0f}ms)")
        time.sleep(0.2)

    if args.check:
        print("\n--check 完了。本走は --check を外して実行。")
        return

    # ── Gemini キャッシュとの突き合わせ ──
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
        b, g = r["bedrock"], entries[q]
        row_diff = []
        for k in STRUCT:
            if b[k] == g.get(k) or (k == "filters" and b[k] == (g.get(k) or {})):
                agree[k] += 1
            else:
                row_diff.append(f"{k}: gemini={g.get(k)!r} bedrock={b[k]!r}")
        if b["search_query"] == g.get("search_query"):
            sq_same += 1
        else:
            row_diff.append(f"search_query: gemini={g.get('search_query')!r} "
                            f"bedrock={b['search_query']!r}")
        if b["hyde_text"].strip():
            hyde_ok += 1
        if b["ja_hyde_text"].strip() and JA_RE.search(b["ja_hyde_text"]):
            ja_ok += 1
        if row_diff:
            diffs.append((q, row_diff))

    pin, pout = PRICING.get(args.model, (None, None))
    cost = (tot_in / 1e6 * pin + tot_out / 1e6 * pout) if pin else None
    lat_avg = sum(lats) / len(lats) if lats else 0
    lat_p95 = sorted(lats)[int(len(lats) * 0.95) - 1] if lats else 0

    print("\n" + "=" * 60)
    print(f"  モデル: {args.model} / 成功 {n_ok}/{len(queries)}")
    for k in STRUCT:
        print(f"  {k:>18}: {agree[k]}/{n_ok} 一致")
    print(f"  {'search_query':>18}: {sq_same}/{n_ok} 完全一致（不一致=言い換え・要目視）")
    print(f"  {'hyde_en 非空':>18}: {hyde_ok}/{n_ok}")
    print(f"  {'ja_hyde 日本語':>18}: {ja_ok}/{n_ok}")
    print(f"  レイテンシ: 平均 {lat_avg:.0f}ms / p95 {lat_p95:.0f}ms")
    print(f"  トークン: in {tot_in:,} / out {tot_out:,}")
    if cost is not None:
        print(f"  実コスト: ${cost:.6f}（30クエリ）→ 1クエリ ${cost/max(n_ok,1):.6f}")
    print("=" * 60)

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out = {"meta": {"model": args.model, "region": region, "ts": ts, "tuned": args.tuned,
                    "prompt_sha": prompt_sha,
                    "tokens_in": tot_in, "tokens_out": tot_out,
                    "cost_usd": cost, "lat_avg_ms": round(lat_avg),
                    "lat_p95_ms": round(lat_p95)},
           "agreement": {**agree, "n_ok": n_ok, "search_query_exact": sq_same,
                         "hyde_en_nonempty": hyde_ok, "ja_hyde_japanese": ja_ok},
           "results": results}
    os.makedirs(OUT_DIR, exist_ok=True)
    suffix = ("_tuned" if args.tuned else "") + ("_partial" if args.only else "")
    path = f"{OUT_DIR}/bedrock_router_test_{ts}{suffix}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n生出力を保存: {path}")
    if diffs:
        print(f"\n不一致 {len(diffs)} クエリ（詳細は JSON）:")
        for q, d in diffs:
            print(f"  [{q}]")
            for line in d:
                print(f"    {line}")


if __name__ == "__main__":
    main()
