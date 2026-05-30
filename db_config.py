"""
db_config.py — DB 接続設定の一元管理
====================================
パスワード等の機密情報は **このファイルには書かない**。
リポジトリ直下の .env（.gitignore 済み）または環境変数から読み込む。
.env の書き方は .env.example を参照。

使い方:
    # 通常（フラグファイルで Primary/Standby を自動切り替え）
    from db_config import get_db_config
    conn = psycopg2.connect(**get_db_config())

    # 単純なスクリプト（Primary 固定でよい場合）
    from db_config import DB_CONFIG
    conn = psycopg2.connect(**DB_CONFIG)

    # Standby を明示的に使う場合（例: ベンチマーク）
    from db_config import DB_CONFIG_STANDBY
"""

import os


# ─── .env の読み込み（依存ライブラリなしの簡易パーサ）─────────

def _load_dotenv(path: str = None) -> None:
    """リポジトリ直下の .env を読み、未設定の環境変数だけを補う。

    既存の環境変数は上書きしない（OS の環境変数を優先）。
    .env が無ければ何もしない（その場合は環境変数のみで動く）。
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


# ─── 接続設定 ─────────────────────────────────────────────────

_COMMON = {
    "host":     os.environ.get("DB_HOST", "localhost"),
    "dbname":   os.environ.get("DB_NAME", "rag_dev"),
    "user":     os.environ.get("DB_USER", "devuser"),
    "password": os.environ.get("DB_PASSWORD", ""),
}

DB_CONFIG_PRIMARY = {**_COMMON, "port": int(os.environ.get("DB_PORT", "5435"))}
DB_CONFIG_STANDBY = {**_COMMON, "port": int(os.environ.get("DB_PORT_STANDBY", "5436"))}

# reembed・共起集計等の重い更新処理中に作成するフラグファイル
FLAG_FILE = os.environ.get("DB_FLAG_FILE", "/mnt/mtg_rag/.primary_updating")


def get_db_config() -> dict:
    """フラグファイルが存在する場合は Standby を使用する。

    reembed や共起集計等の更新処理中に自動的に Standby へ切り替わる。
    """
    if os.path.exists(FLAG_FILE):
        print(f"  [INFO] {FLAG_FILE} を検出 → Standby ({DB_CONFIG_STANDBY['port']}) を使用")
        return DB_CONFIG_STANDBY
    return DB_CONFIG_PRIMARY


# 後方互換: 既存スクリプトは DB_CONFIG を import している
DB_CONFIG = DB_CONFIG_PRIMARY
