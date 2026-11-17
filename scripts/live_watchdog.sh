#!/usr/bin/env bash
# 实盘 watchdog：拉起 `python -m hl_bot run --live`，子进程退出后指数退避重启。
# 子进程还在时，每 HL_WATCHDOG_HEARTBEAT_S 秒（默认 30）写一次心跳。
# 若这一拍实际隔了 >= 有效 HL_HB_STALE_S（默认 180 秒，且至少 3 个心跳周期），
# 说明本循环或整机卡住过：立刻杀掉子进程并拉起，不等 40 分钟扫描停滞。
# 心跳仍然新鲜、但 logs/live_scan.ok 超过约 40 分钟没更新，也杀掉子进程。
# 两道闸互不替代。扫描卡在 HTTP 读上时，心跳仍会按周期刷新，所以扫描闸要留着。
# 时间戳显式使用 Asia/Shanghai（+08:00），不依赖系统时区。
set -uo pipefail
ROOT="/workspace/hyperliquid_trade"
cd "$ROOT" || exit 1
mkdir -p logs state
# shellcheck source=live_scan_stall.sh
source "$ROOT/scripts/live_scan_stall.sh" || exit 1
# shellcheck source=live_heartbeat.sh
source "$ROOT/scripts/live_heartbeat.sh" || exit 1
HEARTBEAT_FILE="$ROOT/logs/live_watchdog.heartbeat"
export HL_SCAN_OK_FILE="${HL_SCAN_OK_FILE:-$ROOT/logs/live_scan.ok}"

ts() { TZ=Asia/Shanghai date -Iseconds "$@"; }

echo "$(ts) watchdog start pid=$$" >> logs/watchdog_live.out
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "$(ts) recreating venv" >> logs/watchdog_live.out
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -e ".[dev]" >> logs/watchdog_live.out 2>&1
fi
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a
hydrate_keepalive_env "$ROOT/.env"
HEARTBEAT_S="${HL_WATCHDOG_HEARTBEAT_S}"
export HL_DRY_RUN=false
export HL_ENABLE_LIVE=1
INTERVAL="${1:-900}"
backoff=60
child=""

on_term() {
  local sig="$1"
  echo "$(ts) watchdog pid=$$ got SIG$sig; stopping child=${child:-none}" >> logs/live_run.log
  if [ -n "$child" ] && kill -0 "$child" 2>/dev/null; then
    kill -TERM "$child" 2>/dev/null
    wait "$child" 2>/dev/null
  fi
  echo "$(ts) watchdog pid=$$ exit (SIG$sig)" >> logs/live_run.log
  exit 0
}
trap 'on_term TERM' TERM
trap 'on_term INT' INT
trap 'on_term HUP' HUP

heartbeat() {
  echo "$(ts) wd=$$ child=${child:-none} state=$1" > "$HEARTBEAT_FILE.tmp" && mv -f "$HEARTBEAT_FILE.tmp" "$HEARTBEAT_FILE"
}

while true; do
  echo "$(ts) starting LIVE loop interval=${INTERVAL}s" >> logs/live_run.log
  started=$(date +%s)
  python -m hl_bot run --network mainnet --live --interval "$INTERVAL" >> logs/live_run.log 2>&1 &
  child=$!
  echo "$(ts) child pid=$child started" >> logs/live_run.log
  heartbeat running
  last_tick=$(date +%s)
  reason=""
  while kill -0 "$child" 2>/dev/null; do
    # 后台 sleep + wait，保证 TERM 能即时打断
    sleep "$HEARTBEAT_S" &
    wait $! 2>/dev/null
    now=$(date +%s)
    tick=$(watchdog_tick_action "$last_tick" "$now" "$started")
    case "$tick" in
      hb_gap)
        gap=$(( now - last_tick ))
        echo "$(ts) heartbeat gap ${gap}s >= $(hb_stale_seconds)s (normal interval ${HEARTBEAT_S}s; host or watchdog loop was stuck); killing hung child=$child" >> logs/live_run.log
        reason=hb_gap
        kill_hung_pid "$child"
        break
        ;;
      scan)
        age=$(scan_stall_age "$started")
        echo "$(ts) scan stalled: no successful complete scan for ${age}s (threshold $(scan_stall_seconds)s, marker=$(scan_ok_file)); killing hung child=$child" >> logs/live_run.log
        reason=scan
        kill_hung_pid "$child"
        break
        ;;
      *)
        last_tick=$now
        heartbeat running
        ;;
    esac
  done
  wait "$child"
  code=$?
  ran=$(( $(date +%s) - started ))
  child=""
  if [ "$reason" = "scan" ]; then
    heartbeat "stalled"
    echo "$(ts) hung child killed after scan stall (wait status=$code, ran ${ran}s); relaunching now" >> logs/live_run.log
    backoff=60
    continue
  fi
  if [ "$reason" = "hb_gap" ]; then
    heartbeat "hb_gap"
    echo "$(ts) hung child killed after heartbeat gap (threshold $(hb_stale_seconds)s, wait status=$code, ran ${ran}s); relaunching now" >> logs/live_run.log
    backoff=60
    continue
  fi
  if [ "$code" -gt 128 ]; then
    signum=$(( code - 128 ))
    signame=$(kill -l "$signum" 2>/dev/null || echo "?")
    why="killed by signal $signum (SIG$signame)"
  else
    why="exit code $code"
  fi
  heartbeat "exited:$code"
  echo "$(ts) exited code=$code ($why) after ${ran}s; sleep ${backoff}s" >> logs/live_run.log
  # 跑满 1 小时视为健康，退避重置
  if [ "$ran" -ge 3600 ]; then backoff=60; fi
  sleep "$backoff" &
  wait $!
  if [ "$backoff" -lt 600 ]; then backoff=$((backoff*2)); else backoff=600; fi
done
