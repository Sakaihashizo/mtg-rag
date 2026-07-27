"""
db.py — DB 接続の連絡係（ドライバ切替層・2026-07-12 新設）
============================================================
検索本体は「SQL を渡して行をもらう」ことだけ知っていればよく、通信手段
（ローカル=psycopg2 の TCP / 本番=Aurora Data API の HTTPS）はここに閉じ込める。
切替は環境変数 DB_BACKEND（ROUTER_BACKEND と同じパターン）:

  DB_BACKEND=psycopg2   既定。ローカル VM の PostgreSQL（電話＝接続を張って話す）
  DB_BACKEND=data_api   AWS 本番。Aurora Serverless v2 へ HTTPS+IAM で1リクエスト
                        ずつ送る（手紙）。★未検証＝実物の Aurora がまだ無い。
                        デプロイ日に検証してからこの注記を外すこと。
                        必要な環境変数: AURORA_CLUSTER_ARN / AURORA_SECRET_ARN

設計の前提（design-premise-ledger 流に明示）:
  - 主業務は読み取り。書き込みは query_log / eval_runs 程度＝「1文ごと自動 commit の
    execute()」で足りる。細粒度トランザクション制御が要る仕事が生まれたら、
    この前提ごと問い直す（BEGIN/COMMIT の面を足すかは その時の判断）。
  - Data API のレスポンス上限 1 MiB（SELECT に embedding 列を含めない実装ルール・
    architecture_serverless.md）はドライバでは守れない＝呼び出し側の責務。
  - psycopg2 モードのエラー時は rollback してから raise（失敗トランザクションが
    接続に残ると以後の全クエリが死ぬ psycopg2 の性質への防御）。握るかどうかは
    呼び出し側が決める（ドライバは握らない）。
"""
import os
from typing import Any, Optional


# ─── Data API の書式変換（純関数＝単体テスト可能・boto3 不要） ──────────────

def convert_params_for_data_api(sql: str, params) -> tuple[str, list[dict]]:
    """psycopg2 流の位置プレースホルダ（%s）を Data API の名前付き（:p0, :p1 ...）へ
    変換し、パラメータを型タグ付き dict のリストにする。呼び出し側は psycopg2 と
    同じ書式のまま使える。
    注意: パラメータ無しの SQL はそのまま通す（既存 SQL の LIKE '%%...%%' 等は
    psycopg2 でも素通しされており、挙動を変えない）。"""
    if not params:
        return sql, []
    out_params: list[dict] = []
    for i, v in enumerate(params):
        if isinstance(v, (list, tuple)):
            # Data API は配列パラメータ非対応（2026-07-28 実測:
            # ValidationException: Array parameters are not supported）
            # ＝予告どおり SQL 側へ ARRAY[...] リテラルとして展開する。
            # 値はエスケープ（' → ''）するので注入は成立しない
            sql = sql.replace("%s", _sql_array_literal(v), 1)
            continue
        name = f"p{i}"
        sql = sql.replace("%s", f":{name}", 1)
        out_params.append({"name": name, "value": _type_tag(v)})
    return sql, out_params


def _sql_array_literal(v) -> str:
    """list/tuple → PostgreSQL の ARRAY[...] リテラル（ANY(%s) 用・エスケープ付き）。
    空配列は型が決まらないので text[] にキャストする（用途上ほぼ来ないが安全側）。"""
    if not v:
        return "ARRAY[]::text[]"
    parts = []
    for x in v:
        if x is None:
            parts.append("NULL")
        elif isinstance(x, bool):
            parts.append("TRUE" if x else "FALSE")
        elif isinstance(x, (int, float)):
            parts.append(str(x))
        else:
            parts.append("'" + str(x).replace("'", "''") + "'")
    return "ARRAY[" + ", ".join(parts) + "]"


def _type_tag(v: Any) -> dict:
    """Python 値 → Data API の型タグ付き value。
    ★list（PostgreSQL 配列・ANY(%s) 用）の Data API 対応は未検証＝
    デプロイ検証の必須項目（通らなければ SQL 側を = ANY(ARRAY[...]) 組み立てに変える）。"""
    if v is None:
        return {"isNull": True}
    if isinstance(v, bool):          # bool は int より先に判定（bool は int の子クラス）
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"longValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"stringValues": [str(x) for x in v]}}
    return {"stringValue": str(v)}


def decode_data_api_row(record: list[dict]) -> tuple:
    """Data API の1行（[{"stringValue": ...}, {"longValue": ...}, ...]）→ タプル。"""
    out = []
    for field in record:
        if field.get("isNull"):
            out.append(None)
        else:
            # 型タグは1フィールドに1つ（stringValue/longValue/doubleValue/...）
            out.append(next(iter(field.values())))
    return tuple(out)


# ─── ドライバ2実装 ────────────────────────────────────────────────────────

class PsycopgDriver:
    """ローカル（VM PostgreSQL）用。既存の db_config.get_db_config() で接続。"""

    def __init__(self):
        import psycopg2
        from db_config import get_db_config
        self.conn = psycopg2.connect(**get_db_config())
        # 読み取り主体のこのアプリでは autocommit=True にする（#29 恒久修正・
        # 2026-07-18）。psycopg2 は既定 autocommit=False＝SELECT 一発で暗黙 BEGIN が
        # 張られ、query() が commit/rollback しないため、持ち回し接続が
        # 「idle in transaction」で ACCESS SHARE ロックを抱え続け ALTER/TRUNCATE/
        # recompute と衝突していた。autocommit で暗黙トランザクションを開かせない
        # （1 文ごと確定・execute() は単文なので一括ロールバック能力は不要＝副作用なし）。
        self.conn.autocommit = True

    def query(self, sql: str, params=None) -> list[tuple]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except Exception:
            self.conn.rollback()
            raise

    def query_dicts(self, sql: str, params=None) -> list[dict]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception:
            self.conn.rollback()
            raise

    def execute(self, sql: str, params=None) -> None:
        """書き込み1文＋commit。失敗時は rollback して raise。"""
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self):
        self.conn.close()


class DataApiDriver:
    """AWS 本番（Aurora Serverless v2 + RDS Data API）用。
    ★全体が未検証（実物の Aurora がまだ無い）＝デプロイ日に必ず実測検証:
      配列パラメータ / vector 型の ::vector キャスト / 1 MiB 上限 / resume 時リトライ。"""

    def __init__(self):
        import boto3  # 遅延 import（psycopg2 モードでは boto3 不要）
        self.client = boto3.client("rds-data")
        self.resource_arn = os.environ["AURORA_CLUSTER_ARN"]
        self.secret_arn = os.environ["AURORA_SECRET_ARN"]
        # AURORA_DB_NAME を優先（2026-07-28: VM から Data API 評価を回すとき
        # DB_NAME はローカル psycopg2 側でも読まれ名前空間が衝突するため分離。
        # Lambda は DB_NAME だけでも動く後方互換）
        self.database = (os.environ.get("AURORA_DB_NAME")
                         or os.environ.get("DB_NAME", "rag_dev"))

    def _execute(self, sql: str, params=None, with_meta: bool = False) -> dict:
        sql2, params2 = convert_params_for_data_api(sql, params)
        return self.client.execute_statement(
            resourceArn=self.resource_arn,
            secretArn=self.secret_arn,
            database=self.database,
            sql=sql2,
            parameters=params2,
            includeResultMetadata=with_meta,
        )

    @staticmethod
    def _decode_rows(resp) -> list[tuple]:
        """行のデコード＋psycopg2 互換の型合わせ（2026-07-28・検収⑧の実測で追加）。
        Data API は psycopg2 が自動でやってくれる 2 つをやらない:
          - jsonb/json 列 → 文字列のまま返す（psycopg2 は dict/list に）
            実害: role_quality が removal jsonb の entry.get() で AttributeError
          - 配列列 → arrayValue の入れ子 dict で返す（psycopg2 は list に）
        列メタの typeName を見て両方を psycopg2 と同じ顔に直す。"""
        import json as _json
        meta = resp.get("columnMetadata", [])
        json_cols = {i for i, m in enumerate(meta)
                     if (m.get("typeName") or "").lower() in ("json", "jsonb")}
        rows = []
        for rec in resp.get("records", []):
            row = []
            for i, field in enumerate(rec):
                if field.get("isNull"):
                    row.append(None)
                    continue
                if "arrayValue" in field:
                    av = field["arrayValue"]
                    # stringValues/longValues/doubleValues/booleanValues のどれか
                    row.append(next(iter(av.values())) if av else [])
                    continue
                v = next(iter(field.values()))
                if i in json_cols and isinstance(v, str):
                    try:
                        v = _json.loads(v)
                    except Exception:
                        pass   # 壊れた json は文字列のまま（握って続行）
                row.append(v)
            rows.append(tuple(row))
        return rows

    def query(self, sql: str, params=None) -> list[tuple]:
        resp = self._execute(sql, params, with_meta=True)
        return self._decode_rows(resp)

    def query_dicts(self, sql: str, params=None) -> list[dict]:
        resp = self._execute(sql, params, with_meta=True)
        cols = [m["name"] for m in resp.get("columnMetadata", [])]
        return [dict(zip(cols, r)) for r in self._decode_rows(resp)]

    def execute(self, sql: str, params=None) -> None:
        self._execute(sql, params)   # Data API は1文ごと自動 commit

    def close(self):
        pass  # 接続という概念が無い（毎回 HTTPS）


# ─── ファクトリ ──────────────────────────────────────────────────────────

def make_db():
    """DB_BACKEND 環境変数でドライバを選んで生成する。
    プロセスで1つ使い回すか毎回作るかは呼び出し側の設計（api_server は lifespan で
    1つ・Lambda は1コンテナ1つ、が想定形）。"""
    backend = os.environ.get("DB_BACKEND", "psycopg2").lower()
    if backend == "data_api":
        return DataApiDriver()
    return PsycopgDriver()
