#!/usr/bin/env bash
# nightly_cron_driver.sh — cron 起点の夜間便ドライバ（2026-07-22 設計・Fable）
# ============================================================================
# 仕様（本人 GO 2026-07-22 22:15 JST）:
#   - cron が毎日 03:00 JST に起動。VM が点いていない夜は cron ごと沈黙＝自然に不発
#     （3時に電源が入っている場合のみ始動、の指示をそのまま cron の性質で実現）。
#   - 1 パス = MTGTop8 レーンと Moxfield レーンを並行起動し、両方の完了を待つ。
#   - パスが終わってもまだ 9 時台なら、休憩（既定 10 分）を挟んで新パスを起動し続ける。
#   - 10:00 になったら新規起動をやめる（走行中のパスは殺さず自然完了に任せる。
#     各ジョブには nightly_scrape.sh 側の timeout 6h が掛かっている）。
#   - スクレイパーの取得済みスキップにより、続行パスは差分だけ拾う
#     （新規ゼロのパスは MTGTop8=フォーマットあたり 1 リクエスト・
#       Moxfield=一覧ページのみで個別デッキ取得ゼロ）。
#
# ジョブ列の変更: 下の JOBS_TOP8 / JOBS_MOX の既定値を編集する
#   （meta コードは年替わりで変わる。2026: ST=341 PI=340 MO=339 LE=338 VI=337 PAU=342・
#     EDH=343 は本人裁定によりスキップ）。
#
# テスト用の環境変数（通常運用では未設定でよい）:
#   NIGHTLY_END_HOUR   この時になったら新規起動をやめる（既定 10）
#   NIGHTLY_INTERVAL   パス間の休憩秒（既定 600）
#   NIGHTLY_MAX_PASSES 0=無制限（時刻でのみ終了）・正数でパス数上限
#   NIGHTLY_LOGDIR     レポート/ログの出力先（既定 docs/me/nightly/<日付>）
#   NIGHTLY_JOBS_TOP8 / NIGHTLY_JOBS_MOX  ジョブ列の差し替え（空文字でレーン無効化）
# ============================================================================
set -u

REPO=/mnt/mtg_rag
NIGHT=$(date +%Y%m%d)
LOGDIR="${NIGHTLY_LOGDIR:-$REPO/docs/me/nightly/$NIGHT}"
END_HOUR="${NIGHTLY_END_HOUR:-10}"
INTERVAL="${NIGHTLY_INTERVAL:-600}"
MAX_PASSES="${NIGHTLY_MAX_PASSES:-0}"
JOBS_TOP8="${NIGHTLY_JOBS_TOP8-mtgtop8:ST:341 mtgtop8:PI:340 mtgtop8:MO:339 mtgtop8:LE:338 mtgtop8:VI:337 mtgtop8:PAU:342}"
# per-bracket 上限（2026-07-26 本人 GO「上限を増やしておこう」で 300→1000）:
# 300 は Moxfield の在庫切れでなく自分で決めたバケット上限だった（bracket 2〜5 が
# 揃って 300 で頭打ち＝満杯で早期打ち切り）。天井は Moxfield 側の totalResults=10000
# キャップの方（bracket 1 の 31 件はそちらの本物の枯れ）。
JOBS_MOX="${NIGHTLY_JOBS_MOX-moxfield:2,3,4,5:1000}"

mkdir -p "$LOGDIR"
DLOG=$LOGDIR/driver.log

exec 8>/tmp/nightly_cron_driver.lock
flock -n 8 || { echo "[$(date '+%F %T')] driver 二重起動を検出・中止" >> "$DLOG"; exit 1; }

dlog() { echo "[$(date '+%F %T')] $*" >> "$DLOG"; }

dlog "=== 夜間便ドライバ開始（${END_HOUR}:00 以降は新規起動なし・休憩 ${INTERVAL}s）==="
dlog "TOP8 ジョブ列: ${JOBS_TOP8:-（無効）}"
dlog "MOX  ジョブ列: ${JOBS_MOX:-（無効）}"

pass=0
while :; do
  h=$((10#$(date +%H)))
  if [ "$h" -ge "$END_HOUR" ]; then
    dlog "時刻 ${h}時 >= ${END_HOUR}時 → 新規起動を終了"
    break
  fi

  pass=$((pass+1))
  dlog "--- パス $pass 起動 ---"
  pids=() names=()
  if [ -n "$JOBS_TOP8" ]; then
    NIGHTLY_LANE=top8 NIGHTLY_LOGDIR="$LOGDIR" "$REPO/sh/nightly_scrape.sh" $JOBS_TOP8 &
    pids+=($!); names+=(top8)
  fi
  if [ -n "$JOBS_MOX" ]; then
    NIGHTLY_LANE=mox NIGHTLY_LOGDIR="$LOGDIR" "$REPO/sh/nightly_scrape.sh" $JOBS_MOX &
    pids+=($!); names+=(mox)
  fi
  if [ "${#pids[@]}" -eq 0 ]; then
    dlog "有効なレーンなし・終了"
    break
  fi
  i=0
  for pid in "${pids[@]}"; do
    wait "$pid"
    dlog "--- パス $pass レーン ${names[$i]} 完了 rc=$?"
    i=$((i+1))
  done

  if [ "$MAX_PASSES" -gt 0 ] && [ "$pass" -ge "$MAX_PASSES" ]; then
    dlog "MAX_PASSES=$MAX_PASSES 到達（テスト用上限）・終了"
    break
  fi
  h=$((10#$(date +%H)))
  if [ "$h" -ge "$END_HOUR" ]; then
    dlog "時刻 ${h}時 >= ${END_HOUR}時 → 休憩せず終了"
    break
  fi
  dlog "休憩 ${INTERVAL}s → 次パスへ"
  sleep "$INTERVAL"
done

dlog "=== ドライバ終了（総パス $pass）==="
exit 0
