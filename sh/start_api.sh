#!/usr/bin/env bash
# start_api.sh — MTG RAG の API サーバを一発起動（2026-07-17）
# 使い方: sh/start_api.sh    止めるのは Ctrl+C
# やること: ①DB(compose) が寝てたら起こす ②5435 の応答を待つ ③uvicorn 起動
set -euo pipefail
cd "$(dirname "$0")/.."   # どこから叩いてもリポジトリ直下へ

# ① DB コンテナ（起きていれば no-op・冪等）
docker compose up -d

# ② DB の TCP 応答待ち（deploy/mtg-rag-api.service の ExecStartPre と同じ流儀）
for i in $(seq 1 30); do
  (echo > /dev/tcp/127.0.0.1/5435) 2>/dev/null && break
  [ "$i" = 30 ] && { echo "DB(5435) が応答しません"; exit 1; }
  sleep 1
done

# ③ 先客チェック（既に 8000 で動いてたら二重起動しない）
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000; then
  echo "localhost:8000 は既に応答しています（起動済みでは？）"
  exit 1
fi

echo "API サーバ起動 → http://localhost:8000 （Ctrl+C で停止）"
exec /mnt/new_hdd/my_rag_env/bin/python -m uvicorn api_server:app \
     --host 127.0.0.1 --port 8000
