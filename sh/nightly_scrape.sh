#!/usr/bin/env bash
# nightly_scrape.sh — 夜間スクレイプの一本道ランナー（2026-07-22 設計・Fable）
# ============================================================================
# 設計思想: 実行の継続性を AI の覚醒に依存させない。
#   7/21〜22 の実測: run_in_background の子は外的要因で消えると後処理が浮く。
#   さらにサブエージェントの heartbeat 監視は「外から再開させないと動かない」
#   ため、深夜無人だと正常完了ですら後処理に進めない（7/22 朝に実証）。
#   → 判断が要らない並び（スクレイプ→後処理→検証）は 1 本のシェルに縫い込み、
#     OS のプロセス実行だけで完遂させる。通知は最後に 1 回で足りる。
#
# 芯となる規則:
#   - 後処理は成否に関わらず必ず実行（; であって && でない）。
#     fix_deck_links は card_id NULL のみ充填・recompute は全再構築＝どちらも
#     冪等なので、部分取得で死んだ夜でも安全に走れる（7/21 異常終了の教訓）。
#   - ランナー自体の二重起動は flock で防止。
#   - recompute は並行ランナー間で共有ロックにより直列化
#     （card_format_strength / edh_card_strength の TRUNCATE 衝突回避）。
#   - 各ジョブは timeout で包む（ハングしても後処理に進む）。
#
# 使い方:
#   sh/nightly_scrape.sh <job> [<job> ...]
#   job の形式:
#     mtgtop8:<FORMAT>:<meta>     例 mtgtop8:MO:339
#     moxfield:<brackets>:<per>   例 moxfield:2,3,4,5:300
#   例（並行 2 便・レーンごとに別プロセスで起動する）:
#     nohup sh/nightly_scrape.sh mtgtop8:ST:341 mtgtop8:PI:340 &
#     nohup sh/nightly_scrape.sh moxfield:2,3,4,5:300 &
#
# 2026 年の meta コード（実測 2026-07-22）:
#   ST=341 PI=340 MO=339 LE=338 VI=337 PAU=342 （EDH=343 はスキップ方針）
#   スクレイパーは取得済みイベントを自動スキップ＝同じコマンドを毎晩打っても
#   新規イベントだけ取りに行く（差分運用がコマンド不変で成立する）。
# ============================================================================
set -u

REPO=/mnt/mtg_rag
PY=/mnt/new_hdd/my_rag_env/bin/python
STAMP=$(date +%Y%m%d_%H%M)
LANE="${NIGHTLY_LANE:-lane_$$}"
LOGDIR="${NIGHTLY_LOGDIR:-$REPO/docs/me}"
REPORT=$LOGDIR/nightly_report_${STAMP}_${LANE}.md
LOCK=/tmp/nightly_scrape_${LANE}.lock
RECOMPUTE_LOCK=/tmp/recompute_strength.lock

cd "$REPO" || exit 1
mkdir -p "$LOGDIR"

exec 9>"$LOCK"
flock -n 9 || { echo "同一レーン二重起動を検出・中止 ($LANE)"; exit 1; }

log() { echo "[$(date '+%F %T')] $*" >> "$REPORT"; }

log "# 夜間便レポート $STAMP（レーン: $LANE）"
log "ジョブ列: $*"

overall=0
for job in "$@"; do
  IFS=: read -r kind a b <<<"$job"
  joblog=$LOGDIR/scrape_${STAMP}_${kind}_$(echo "$a" | tr ',' '-').log
  log "--- ジョブ開始: $job（詳細ログ: $(basename "$joblog")）"
  case $kind in
    mtgtop8)
      timeout 6h "$PY" scrape_mtgtop8.py --format "$a" --meta "$b" --year 2026 \
        > "$joblog" 2>&1
      rc=$? ;;
    moxfield)
      timeout 6h "$PY" scrape_moxfield.py --sample-by-bracket \
        --brackets "$a" --per-bracket "$b" --max-pages 300 \
        > "$joblog" 2>&1
      rc=$? ;;
    *)
      log "不明なジョブ種: $kind（スキップ）"
      rc=2 ;;
  esac
  log "--- ジョブ終了: $job rc=$rc（124=timeout）"
  if [ -f "$joblog" ]; then
    log "    末尾: $(tr '\r' '\n' < "$joblog" | tail -2 | tr '\n' ' / ')"
  fi
  [ "$rc" -ne 0 ] && overall=1
done

log "=== 後処理（成否に関わらず必ず実行） ==="
"$PY" fix_deck_links.py > /tmp/nightly_fix_${LANE}.out 2>&1
log "fix_deck_links rc=$? / $(tail -2 /tmp/nightly_fix_${LANE}.out | tr '\n' ' ')"

flock "$RECOMPUTE_LOCK" "$PY" recompute_card_format_strength.py \
  > /tmp/nightly_recompute_${LANE}.out 2>&1
log "recompute rc=$?（直列化ロック経由）"
log "$(head -12 /tmp/nightly_recompute_${LANE}.out | tr '\n' ' | ')"

log "=== 完了 overall=$overall（0=全ジョブ正常）==="
exit "$overall"
