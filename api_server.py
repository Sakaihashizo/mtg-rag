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

from fastapi import FastAPI, HTTPException
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
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (endpoint, req.query, req.format,
             result.get("route"), result.get("router_backend"),
             result.get("search_query"),
             json.dumps(result.get("flags"), ensure_ascii=False),
             json.dumps([c["card_name"] for c in result.get("cards", [])],
                        ensure_ascii=False),
             latency_ms))
    except Exception as e:
        print(f"  [query_log] 書き込み失敗（握って続行）: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # e5 モデルのロードと DB 接続。起動に数十秒かかる（コールドスタートの実測点）
    _state["searcher"] = MTGHybridSearcherV2(
        model_key=os.environ.get("RAG_MODEL", "SMALL_V2"))
    _state["db"] = make_db()
    _state["db"].execute(_LOG_DDL)
    yield
    _state["db"].close()
    _state["searcher"].close()


app = FastAPI(title="MTG RAG API", version="0.1.0", lifespan=lifespan)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    format: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)
    use_rewrite: bool = True


@app.get("/health")
def health():
    with _lock:
        _state["db"].query("SELECT 1")
    return {"status": "ok",
            "router_backend": os.environ.get("ROUTER_BACKEND", "gemini").lower(),
            "db_backend": os.environ.get("DB_BACKEND", "psycopg2").lower()}


@app.post("/search")
def search(req: SearchRequest):
    """検索のみ（回答生成なし）。use_rewrite=false または直行路クエリなら LLM ゼロで動く。

    api_key の要否判定は run_search に委ねる（直行路 gate は run_search 内で
    発動するため、ここで事前に弾くと「キー無しでも通るはずの直行路」まで死ぬ）。"""
    api_key = os.environ.get("GOOGLE_API_KEY")
    try:
        t0 = time.perf_counter()
        with _lock:
            result = run_search(_state["searcher"], req.query, fmt=req.format,
                                top_k=req.top_k, api_key=api_key,
                                use_rewrite=req.use_rewrite)
            _log_query("/search", req, result,
                       int((time.perf_counter() - t0) * 1000))
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/ask")
def ask(req: SearchRequest):
    """検索 + 回答生成。Gemini を最大2リクエスト消費（ルーター+回答）＝
    無料枠クォータ（日次約100）を食う。呼ぶのは明示 GO の下で。"""
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="GOOGLE_API_KEY が未設定")
    t0 = time.perf_counter()
    with _lock:
        result = run_search(_state["searcher"], req.query, fmt=req.format,
                            top_k=req.top_k, api_key=api_key,
                            use_rewrite=req.use_rewrite)
        if not result["cards"]:
            _log_query("/ask", req, result,
                       int((time.perf_counter() - t0) * 1000))
            return {**result, "answer": None}
        context = build_context(result["cards"])
        answer = ask_gemini(req.query, context, api_key)
        _log_query("/ask", req, result,
                   int((time.perf_counter() - t0) * 1000))
    return {**result, "answer": answer}
