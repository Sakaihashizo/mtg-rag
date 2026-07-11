#!/usr/bin/env python
"""test_db_driver.py — db.py（ドライバ切替層）の安全試験（2026-07-12）。

2部構成:
  A) Data API 書式変換の純ユニット（boto3 不要・決定的）——%s→:pN 変換・型タグ・
     行デコード。DataApiDriver 本体は実物の Aurora が無いため未検証（デプロイ日）だが、
     変換ロジックだけは今日から固定できる。
  B) PsycopgDriver の実接続試験（VM の PostgreSQL）——query / query_dicts / execute /
     エラー後に接続が生きている（rollback 防御）の4面。
"""
import sys

sys.path.insert(0, '/mnt/mtg_rag')
from db import (convert_params_for_data_api, decode_data_api_row,
                PsycopgDriver)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


# ─── A) 純ユニット ───────────────────────────────────────────

# %s → :p0, :p1（順序どおり・1個ずつ）
sql, params = convert_params_for_data_api(
    "SELECT * FROM t WHERE a = %s AND b = %s", ("x", 5))
check("convert.sql", sql, "SELECT * FROM t WHERE a = :p0 AND b = :p1")
check("convert.params", params, [
    {"name": "p0", "value": {"stringValue": "x"}},
    {"name": "p1", "value": {"longValue": 5}},
])

# パラメータ無しは素通し（LIKE '%%...%%' 等を壊さない）
sql, params = convert_params_for_data_api(
    "SELECT 1 WHERE x LIKE '%%Creature%%'", None)
check("convert.noparams.sql", sql, "SELECT 1 WHERE x LIKE '%%Creature%%'")
check("convert.noparams.params", params, [])

# 型タグ: bool は int の子クラス＝先に判定されること・None・float・list
_, params = convert_params_for_data_api(
    "%s %s %s %s %s", (True, 3, 2.5, None, ["a", "b"]))
check("tag.bool",  params[0]["value"], {"booleanValue": True})
check("tag.int",   params[1]["value"], {"longValue": 3})
check("tag.float", params[2]["value"], {"doubleValue": 2.5})
check("tag.null",  params[3]["value"], {"isNull": True})
check("tag.list",  params[4]["value"],
      {"arrayValue": {"stringValues": ["a", "b"]}})

# 行デコード: 型タグ → 素の値・isNull → None
row = decode_data_api_row([
    {"stringValue": "Sol Ring"}, {"longValue": 1},
    {"isNull": True}, {"doubleValue": 0.5}, {"booleanValue": False},
])
check("decode.row", row, ("Sol Ring", 1, None, 0.5, False))

# ─── B) PsycopgDriver 実接続（VM の PostgreSQL） ─────────────

db = PsycopgDriver()

check("pg.query", db.query("SELECT 1, 'two'"), [(1, "two")])
check("pg.query_dicts",
      db.query_dicts("SELECT 1 AS a, 'two' AS b"), [{"a": 1, "b": "two"}])
check("pg.params", db.query("SELECT %s::int + 1", (41,)), [(42,)])

# execute: 一時テーブルで書き込み+commit の面を検証（実テーブルを汚さない）
db.execute("CREATE TEMP TABLE _drv_test (x int)")
db.execute("INSERT INTO _drv_test VALUES (%s)", (7,))
check("pg.execute", db.query("SELECT x FROM _drv_test"), [(7,)])

# エラー後も接続が生きている（rollback 防御＝psycopg2 の失敗トランザクション残留対策）
try:
    db.query("SELECT * FROM _no_such_table_xyz")
    failures.append("pg.error: 期待した例外が出ていない")
except Exception:
    pass
check("pg.alive_after_error", db.query("SELECT 1"), [(1,)])

db.close()

# ─── 結果 ────────────────────────────────────────────────────

if failures:
    print(f"FAIL {len(failures)} 件:")
    for x in failures:
        print("  " + x)
    sys.exit(1)
print("ALL PASS（変換ユニット + psycopg2 実接続4面）")
