#!/usr/bin/env bash
# 实盘 watchdog：拉起 `python -m hl_bot run --live`，子进程退出后指数退避重启。
# 每 HEARTBEAT_S 秒写心跳文件；子进程退出时明确记录退出码 / 信号。
# 时间戳显式使用 Asia/Shanghai（+08:00），不依赖系统时区。
set -uo pipefail
ROOT="/workspace/hyperliquid_trade"
cd "$ROOT" || exit 1
mkdir -p logs state
HEARTBEAT_FILE="$ROOT/logs/live_watchdog.heartbeat"
HEARTBEAT_S="${HL_WATCHDOG_HEARTBEAT_S:-30}"

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
  while kill -0 "$child" 2>/dev/null; do
    # 后台 sleep + wait，保证 TERM 能即时打断
    sleep "$HEARTBEAT_S" &
    wait $! 2>/dev/null
    heartbeat running
  done
  wait "$child"
  code=$?
  ran=$(( $(date +%s) - started ))
  if [ "$code" -gt 128 ]; then
    signum=$(( code - 128 ))
    signame=$(kill -l "$signum" 2>/dev/null || echo "?")
    why="killed by signal $signum (SIG$signame)"
  else
    why="exit code $code"
  fi
  child=""
  heartbeat "exited:$code"
  echo "$(ts) exited code=$code ($why) after ${ran}s; sleep ${backoff}s" >> logs/live_run.log
  # 跑满 1 小时视为健康，退避重置
  if [ "$ran" -ge 3600 ]; then backoff=60; fi
  sleep "$backoff" &
  wait $!
  if [ "$backoff" -lt 600 ]; then backoff=$((backoff*2)); else backoff=600; fi
done
