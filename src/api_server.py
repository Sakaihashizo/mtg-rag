"""
api_server.py — MTG RAG 検索 API（ローカルサーバ・2026-07-11 新設）
=====================================================================
CLI（mtg_rag_agent.py）の検索フローに HTTP の皮を被せる。AWS デプロイ
（C案 = API Gateway + Lambda コンテナ + Aurora Data API）の前段として、
まず VM 内で同じ中身を HTTP で叩ける形にする。

設計の前提（design-premise-ledger 流に明示）:
  - 検索の芯は mtg_rag_agent.run_search() を共用する。検索フローをここに
    重複実装しない（2026-07-09: 経路差が機構ゲート不発の故障源になった教訓）。
  - /search は use_rewrite=false なら外部 LLM を一切呼ばない＝配管検証が無料。
  - /ask（回答生成つき）は Gemini クォータを消費する。呼ぶのは明示 GO の下で。
  - searcher はプロセスに1つ・Lock で直列化。これは psycopg2 単一接続の保護で、
    「デモ流量では十分・製品流量では接続層ごと見直す」前提（Lambda は
    1コンテナ1リクエストなので本番では自然に解消される）。

起動（VM・リポジトリ直下で）:
  /mnt/new_hdd/my_rag_env/bin/python -m uvicorn api_server:app \
      --host 127.0.0.1 --port 8000
確認:
  curl -s localhost:8000/health
  curl -s localhost:8000/search -H 'Content-Type: application/json' \
      -d '{"query": "速攻を持つクリーチャー", "use_rewrite": false}'
"""
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import requests                       # /health?deep=true のルーター疎通確認用

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db import make_db
from mtg_hybrid_search_v2 import MTGHybridSearcherV2
from mtg_rag_agent import run_search, build_context, ask_gemini

# searcher は自前の psycopg2 接続を持つ（検索14箇所の db.py 移行は別作業）。
# query_log/health は db.py 経由＝当面 DB 接続は2本（searcher + db）。
# どちらも _lock 内でのみ触る＝直列化は共通。
_state: dict = {"searcher": None, "db": None}
_lock = threading.Lock()

# クエリログ（2026-07-12・語彙学習 v1 の観測基盤）:
# 「どんなクエリが・どの経路で・ルーターが何を立てたか」を記録する。
# 用途=route:router のクエリから辞書化候補を掘る（自動追加はしない・
# 候補を本人がレビューして辞書へ昇格させる human-in-the-loop が確定構想）。
_LOG_DDL = """
CREATE TABLE IF NOT EXISTS query_log (
    id             bigserial PRIMARY KEY,
    ts             timestamptz NOT NULL DEFAULT now(),
    endpoint       text NOT NULL,
    query          text NOT NULL,
    format         text,
    route          text,
    router_backend text,
    search_query   text,
    flags          jsonb,
    top_cards      jsonb,
    latency_ms     integer
);
"""


def _log_query(endpoint: str, req, result: dict, latency_ms: int) -> None:
    """検索1件をログに書く。ログは主業務でない＝失敗してもリクエストは落とさない
    （db.py はエラーを握らず raise する設計＝握るのはこの呼び出し側の責務）。"""
    try:
        _state["db"].execute(
            "INSERT INTO query_log (endpoint, query, format, route,"
            " router_backend, search_query, flags, top_cards, latency_ms)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)",
            (endpoint, req.query, req.format,
             result.get("route"), result.get("router_backend"),
             result.get("search_query"),
             json.dumps(result.get("flags"), ensure_ascii=False),
             json.dumps([c["card_name"] for c in result.get("cards", [])],
                        ensure_ascii=False),
             latency_ms))
    except Exception as e:
        print(f"  [query_log] 書き込み失敗（握って続行）: {e}")


def _ensure_state() -> None:
    """e5 モデルのロードと DB 接続（起動に数秒〜十数秒）。

    2026-07-28 Lambda 化で lazy 化: Lambda の INIT フェーズには 10 秒制限があり、
    モデルロードが収まらず INIT が破棄→リクエスト内で全初期化やり直し＝毎回
    コールド 20 秒級になっていた（CloudWatch の INIT_REPORT Status: timeout が証拠）。
    初期化をリクエスト内へ移せば 10 秒制限を受けない（Lambda timeout 60s まで可）
    ＝初回だけ遅く、2 回目以降のコンテナ再利用で速くなる。
    ローカル uvicorn は従来どおり lifespan が呼ぶ＝挙動不変。"""
    if _state["searcher"] is None:
        _state["searcher"] = MTGHybridSearcherV2(
            model_key=os.environ.get("RAG_MODEL", "SMALL_V2"))
        _state["db"] = make_db()
        _state["db"].execute(_LOG_DDL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_state()
    yield
    _state["db"].close()
    _state["searcher"].close()


app = FastAPI(title="MTG RAG API", version="0.1.0", lifespan=lifespan)

# CORS（2026-07-28）: CloudFront 配信のフロントは別オリジン。API GW の
# cors_configuration は preflight(OPTIONS) を素通しして FastAPI が 405 を返し、
# ブラウザが NetworkError になった（実測）。アプリ側で OPTIONS に 2xx+ヘッダを
# 返すのが確実。デモは全公開前提＝"*"（B2B 化時にドメインで絞る）
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    format: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)
    use_rewrite: bool = True


def _probe_router(backend: str) -> dict:
    """ルーターの実疎通を確かめる（2026-07-26 新設）。

    由来: 2026-07-26 に ollama がホスト側で落ちていたのを丸一日気づけなかった。
    原因は health が `router_backend: ollama` という**設定値**を返すだけで、
    そこへ実際に届くかを見ていなかったこと＝「設定を健全性と誤認する」構造。

    正直さの規律: **課金する経路は叩かない**。Bedrock/Gemini を疎通確認のために
    呼ぶと 1 回ごとに金がかかるので、鍵の有無だけ見て `unchecked` と正直に返す
    （「確かめていない」を「正常」と偽らない）。無料の ollama だけ実際に叩く。
    """
    if backend == "ollama":
        try:
            from ollama_router_test import OLLAMA_URL
            base = OLLAMA_URL.rsplit("/api/", 1)[0]
            r = requests.get(f"{base}/api/tags", timeout=3)
            r.raise_for_status()
            names = [m.get("name") for m in r.json().get("models", [])]
            return {"reachable": True, "models": len(names)}
        except Exception as e:
            return {"reachable": False, "error": type(e).__name__}
    if backend == "nova":
        has_key = bool(os.environ.get("AWS_ACCESS_KEY_ID"))
        return {"reachable": "unchecked", "credentials": has_key,
                "note": "課金経路のため疎通確認はしない（鍵の有無のみ）"}
    if backend == "gemini":
        return {"reachable": "unchecked",
                "credentials": bool(os.environ.get("GOOGLE_API_KEY")),
                "note": "無料枠を消費するため疎通確認はしない（鍵の有無のみ）"}
    return {"reachable": "unknown", "note": f"未知のバックエンド: {backend}"}


@app.get("/health")
def health(deep: bool = False):
    """既定は軽量（DB の生存のみ）。`?deep=true` でルーターの実疎通まで見る。

    軽い方を既定にしているのは、監視や systemd から頻繁に叩かれる前提のため。
    デモや作業の前には deep で叩く＝今回のような「静かな故障」を一目で捕まえる。
    """
    with _lock:
        _ensure_state()
        _state["db"].query("SELECT 1")
    backend = os.environ.get("ROUTER_BACKEND", "gemini").lower()
    body = {"status": "ok",
            "router_backend": backend,
            "db_backend": os.environ.get("DB_BACKEND", "psycopg2").lower()}
    if deep:
        body["router"] = _probe_router(backend)
        if body["router"].get("reachable") is False:
            body["status"] = "degraded"   # 検索は動くがルーターを通らない状態
    return body


def _attach_images(result: dict) -> dict:
    """検索結果に image_url（Scryfall CDN への直リンク）を後付けする（2026-07-26）。

    設計判断: 検索エンジン本体（searcher の SELECT 群）には触らず、応答を組む
    この層で card_name の IN 句 1 発で引く＝検索経路が不変なので eval に影響しない
    （破壊半径を応答整形に閉じ込める）。画像バイトは自前で持たない——URL 文字列
    だけ返し、表示はブラウザ→Scryfall の直リンク（帯域・保管コストゼロ）。"""
    cards = result.get("cards") or []
    names = [c.get("card_name") for c in cards if c.get("card_name")]
    if not names:
        return result
    try:
        rows = _state["db"].query(
            "SELECT card_name, image_url, image_url_ja FROM mtg_cards_v2"
            " WHERE card_name = ANY(%s)", (names,))
        urls = {r[0]: (r[1], r[2]) for r in rows}
        for c in cards:
            en, ja = urls.get(c.get("card_name"), (None, None))
            c["image_url"] = en
            # 日本語印刷の画像（存在しないカードは None＝フロントが英語画像へ
            # フォールバック）。値の充填は all_cards バルクの集約便（set_codes と同便）
            c["image_url_ja"] = ja
    except Exception:
        # 画像は飾り＝取得に失敗しても検索結果は返す（欠けたら image_url 無しのまま）
        pass
    return result


@app.post("/search")
def search(req: SearchRequest):
    """検索のみ（回答生成なし）。use_rewrite=false または直行路クエリなら LLM ゼロで動く。

    api_key の要否判定は run_search に委ねる（直行路 gate は run_search 内で
    発動するため、ここで事前に弾くと「キー無しでも通るはずの直行路」まで死ぬ）。"""
    api_key = os.environ.get("GOOGLE_API_KEY")
    try:
        t0 = time.perf_counter()
        with _lock:
            _ensure_state()
            result = run_search(_state["searcher"], req.query, fmt=req.format,
                                top_k=req.top_k, api_key=api_key,
                                use_rewrite=req.use_rewrite)
            result = _attach_images(result)
            _log_query("/search", req, result,
                       int((time.perf_counter() - t0) * 1000))
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/ask")
def ask(req: SearchRequest):
    """検索 + 回答生成。Gemini を最大2リクエスト消費（ルーター+回答）＝
    無料枠クォータ（日次約100）を食う。呼ぶのは明示 GO の下で。

    例外＝直行路（structured_direct）: 答えが一意に決まる検索は説明不要なので
    回答生成をスキップし、検索結果＋定型文を返す（LLM 消費ゼロ・api_key 不要。
    ROADMAP 原則「説明が要るときだけ LLM」の実装・2026-07-13 本人裁定）。
    api_key の事前チェックはしない＝直行路をキー無しで通すため。回答生成が
    実際に要る経路に限り、生成の直前でキーを検査する。"""
    api_key = os.environ.get("GOOGLE_API_KEY")
    t0 = time.perf_counter()
    try:
        with _lock:
            _ensure_state()
            result = run_search(_state["searcher"], req.query, fmt=req.format,
                                top_k=req.top_k, api_key=api_key,
                                use_rewrite=req.use_rewrite)
            result = _attach_images(result)
            if not result["cards"]:
                _log_query("/ask", req, result,
                           int((time.perf_counter() - t0) * 1000))
                return {**result, "answer": None}
            if result["route"] == "structured_direct":
                answer = (f"条件が一意に決まる検索のため、該当カード"
                          f" {len(result['cards'])} 件をそのまま返します"
                          "（回答生成 LLM 不使用）。")
                _log_query("/ask", req, result,
                           int((time.perf_counter() - t0) * 1000))
                return {**result, "answer": answer}
            if result["route"] == "removal_direct":
                # 卒業クエリ（検証終了・2026-07-17）も LLM 消費ゼロで応答まで完結
                answer = (f"検証終了済みの定型クエリのため、決定的ランキング"
                          f" {len(result['cards'])} 件をそのまま返します"
                          "（回答生成 LLM 不使用）。")
                _log_query("/ask", req, result,
                           int((time.perf_counter() - t0) * 1000))
                return {**result, "answer": answer}
            if result["route"] == "counter_direct":
                # 確定カウンターの卒業クエリ（2026-07-19）も同様＝7/20 に漏れを発見して追補
                answer = (f"検証終了済みの定型クエリのため、決定的ランキング"
                          f" {len(result['cards'])} 件をそのまま返します"
                          "（回答生成 LLM 不使用）。")
                _log_query("/ask", req, result,
                           int((time.perf_counter() - t0) * 1000))
                return {**result, "answer": answer}
            if result.get("scope_note"):
                # 守備範囲外（相性/コンボ/ヴォーソス等）＝LLM に作文させず正直な定型文
                # で答える（「自信満々に間違える」対策・2026-07-20）
                _log_query("/ask", req, result,
                           int((time.perf_counter() - t0) * 1000))
                return {**result, "answer": result["scope_note"]}
            if not api_key:
                raise HTTPException(status_code=400,
                                    detail="GOOGLE_API_KEY が未設定（回答生成に必要）")
            context = build_context(result["cards"])
            answer = ask_gemini(req.query, context, api_key)
            _log_query("/ask", req, result,
                       int((time.perf_counter() - t0) * 1000))
        return {**result, "answer": answer}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# 静的フロント（static/index.html）。ルート定義の後に mount する＝
# /search /ask /health は API が先勝ちし、それ以外を静的配信が受ける。
# 本番（C案）では S3+CloudFront がこの役割を担う＝この mount はローカル開発用。
# 2026-07-26: ディレクトリが在るときだけ mount する。Lambda コンテナには
# static/ を載せない（本番の配信は S3+CloudFront）ため、無条件 mount だと
# 起動時に例外で落ちる＝7/24 の static パス取り違えによるクラッシュループと同族。
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
