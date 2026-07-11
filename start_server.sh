#!/bin/bash
# start_server.sh — MTG RAG API サーバ起動（VM 内・冪等・2026-07-12）
# =====================================================================
# ★2026-07-12 以降の正道は systemd（deploy/mtg-rag-api.service・自動起動+死活監視）:
#     systemctl --user restart mtg-rag-api   ← 再起動はこっち
#   このスクリプトは systemd の無い環境・臨時用の予備。systemd 管理中に叩くと
#   ポート衝突で二重起動は失敗する（実害はないが紛らわしい）。
# 使い方（VM 内）:      ./start_server.sh
# 使い方（Windows から）: ssh -i ~/.ssh/mtg_vm_ed25519 -p 9999 claude@127.0.0.1 \
#                          '/mnt/mtg_rag/start_server.sh'
# ルーター切替:          ROUTER_BACKEND=gemini ./start_server.sh
#                        （既定は ollama = ローカル7B・$0。gemini はクォータ消費）
#
# 前提: PostgreSQL(docker) は compose の restart: unless-stopped で VM 起動時に
#       自動復活する。このスクリプトが面倒を見るのは uvicorn だけ（居なければ
#       docker compose up -d も打つが、通常は不要）。
# 停止: pgrep -u $USER -f "[u]vicorn api_server" | xargs -r kill
set -u
cd "$(dirname "$0")"

PY=/mnt/new_hdd/my_rag_env/bin/python
BACKEND="${ROUTER_BACKEND:-ollama}"

# 旧プロセスを畳む（[u] は自分自身のコマンドラインにマッチさせない常套句＝
# 2026-07-11 に pkill 自殺を2連発した教訓）
pgrep -u "$USER" -f "[u]vicorn api_server" | xargs -r kill
sleep 1

# DB コンテナの生存確認（通常は restart ポリシーで復活済みのはず）
if ! docker ps --format '{{.Names}}' | grep -q '^pg18-primary$'; then
    echo "[start_server] pg18-primary 不在 → docker compose up -d"
    docker compose up -d
    sleep 5
fi

echo "[start_server] uvicorn 起動（ROUTER_BACKEND=${BACKEND}）"
ROUTER_BACKEND="$BACKEND" nohup "$PY" -m uvicorn api_server:app \
    --host 127.0.0.1 --port 8000 > /tmp/api_server.log 2>&1 &

# health 待ち（e5 モデルのロードで数十秒かかることがある）
for i in $(seq 1 20); do
    r=$(curl -s -m 2 localhost:8000/health 2>/dev/null)
    if [ -n "$r" ]; then
        echo "[start_server] OK: $r"
        echo "[start_server] Windows からは http://localhost:8000 （要ポートフォワード）"
        exit 0
    fi
    sleep 3
done

echo "[start_server] TIMEOUT — 直近ログ:"
tail -10 /tmp/api_server.log
exit 1
