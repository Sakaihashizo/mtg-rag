"""AWS Lambda（コンテナイメージ）の入口 — 既存の FastAPI アプリをそのまま動かす。

設計の方針（2026-07-26）:
ローカルは uvicorn が常駐して待ち受ける形、Lambda は「呼ばれるたびに関数が動く」形で、
待ち受けの作法が違う。その差だけを Mangum という薄い変換層で吸収し、
**`src/api_server.py` は一行も変えない**。db.py が「接続の張り方だけ」を
psycopg2 / Data API で切り替えているのと同じ考え方（中身は共通・境界だけ二重化）。

コールドスタートの扱い:
  api_server の lifespan（起動時にモデルを読み DB へ繋ぐ処理）は Mangum が
  最初の呼び出しの前に一度だけ実行する。温まったコンテナは AWS が使い回すため、
  二回目以降はモデル読み込みが走らない。

環境変数（本番で渡す想定）:
  MODEL_CACHE_DIR=/opt/models   イメージに焼いた埋め込みモデルの置き場所
  DB_BACKEND=dataapi            Aurora へは Data API 経由（ローカル検証では psycopg2）
  ROUTER_BACKEND=nova           本番のクエリルーター
"""
import os
import sys

# src/ を import 対象に加える（コンテナ内では /var/task/src に置く）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
sys.path.insert(0, "/var/task/src")

from mangum import Mangum          # noqa: E402
from api_server import app         # noqa: E402

# lifespan="off"（2026-07-28 変更）: INIT フェーズの 10 秒制限にモデルロードが
# 収まらず「INIT 破棄→リクエスト内で再初期化」の二重コールドが起きていた。
# 初期化は api_server._ensure_state() が初回リクエスト内で行う（制限なし・
# コンテナ再利用で 2 回目以降は素通り）
handler = Mangum(app, lifespan="off")
