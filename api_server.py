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
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from mtg_hybrid_search_v2 import MTGHybridSearcherV2
from mtg_rag_agent import run_search, build_context, ask_gemini

_state: dict = {"searcher": None}
_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # e5 モデルのロードと DB 接続。起動に数十秒かかる（コールドスタートの実測点）
    _state["searcher"] = MTGHybridSearcherV2(
        model_key=os.environ.get("RAG_MODEL", "SMALL_V2"))
    yield
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
        with _state["searcher"].conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    return {"status": "ok",
            "router_backend": os.environ.get("ROUTER_BACKEND", "gemini").lower()}


@app.post("/search")
def search(req: SearchRequest):
    """検索のみ（回答生成なし）。use_rewrite=false または直行路クエリなら LLM ゼロで動く。

    api_key の要否判定は run_search に委ねる（直行路 gate は run_search 内で
    発動するため、ここで事前に弾くと「キー無しでも通るはずの直行路」まで死ぬ）。"""
    api_key = os.environ.get("GOOGLE_API_KEY")
    try:
        with _lock:
            return run_search(_state["searcher"], req.query, fmt=req.format,
                              top_k=req.top_k, api_key=api_key,
                              use_rewrite=req.use_rewrite)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/ask")
def ask(req: SearchRequest):
    """検索 + 回答生成。Gemini を最大2リクエスト消費（ルーター+回答）＝
    無料枠クォータ（日次約100）を食う。呼ぶのは明示 GO の下で。"""
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="GOOGLE_API_KEY が未設定")
    with _lock:
        result = run_search(_state["searcher"], req.query, fmt=req.format,
                            top_k=req.top_k, api_key=api_key,
                            use_rewrite=req.use_rewrite)
        if not result["cards"]:
            return {**result, "answer": None}
        context = build_context(result["cards"])
        answer = ask_gemini(req.query, context, api_key)
    return {**result, "answer": answer}
