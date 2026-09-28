#!/usr/bin/env bash
# 保活入口：检查 live watchdog + 实盘 bot 是否在跑，缺失则重启；同时做重启(reboot)检测与心跳检查。
# 所有时间戳显式使用 Asia/Shanghai（+08:00），不依赖系统时区（系统时区可能被改成别的）。
ROOT="/workspace/hyperliquid_trade"
cd "$ROOT" || exit 0
mkdir -p logs
PIDFILE="$ROOT/logs/live_watchdog.pid"
LOG="$ROOT/logs/ensure_live.log"
BTIME_FILE="$ROOT/logs/last_btime"
HEARTBEAT_FILE="$ROOT/logs/live_watchdog.heartbeat"
# btime 由「当前时间 - uptime」推算，NTP 校时会有 ±1s 抖动；超过该阈值才算重启
BTIME_TOLERANCE_S=60

ts() { TZ=Asia/Shanghai date -Iseconds "$@"; }

is_alive() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# ---- reboot 检测 ----
btime=$(awk '/^btime/ {print $2}' /proc/stat 2>/dev/null)
if [ -n "$btime" ]; then
  prev_btime=$(cat "$BTIME_FILE" 2>/dev/null || true)
  if [ -z "$prev_btime" ]; then
    echo "$(ts) btime baseline: box booted at $(ts -d "@$btime") (btime=$btime)" >> "$LOG"
    echo "$btime" > "$BTIME_FILE"
  else
    diff=$(( btime - prev_btime ))
    [ "$diff" -lt 0 ] && diff=$(( -diff ))
    if [ "$diff" -gt "$BTIME_TOLERANCE_S" ]; then
      echo "$(ts) box rebooted at $(ts -d "@$btime") (btime $prev_btime -> $btime; previous boot $(ts -d "@$prev_btime"))" >> "$LOG"
      echo "$btime" > "$BTIME_FILE"
    fi
  fi
fi

# ---- 心跳 ----
hb_info="hb=none"
if [ -f "$HEARTBEAT_FILE" ]; then
  hb_mtime=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || echo 0)
  hb_age=$(( $(date +%s) - hb_mtime ))
  hb_info="hb_age=${hb_age}s"
fi

bot_alive=0
for pid in $(ps -eo pid=,args= | awk '/[p]ython/ && /-m hl_bot/ && /--live/ {print $1}'); do
  bot_alive=1
done

wd_pid=""
if [ -f "$PIDFILE" ]; then
  wd_pid=$(cat "$PIDFILE" 2>/dev/null || true)
fi

if is_alive "$wd_pid" && [ "$bot_alive" = 1 ]; then
  echo "$(ts) live already running wd=$wd_pid $hb_info" >> "$LOG"
  exit 0
fi

echo "$(ts) live missing (wd_alive=$(is_alive "$wd_pid" && echo 1 || echo 0) bot=$bot_alive $hb_info) — restarting" >> "$LOG"

if is_alive "$wd_pid"; then
  kill "$wd_pid" 2>/dev/null || true
fi
for pid in $(ps -eo pid=,args= | awk '/[p]ython/ && /-m hl_bot/ && /--live/ {print $1}'); do
  kill "$pid" 2>/dev/null || true
done
sleep 2

nohup bash "$ROOT/scripts/live_watchdog.sh" 900 >> "$ROOT/logs/watchdog_live.out" 2>&1 &
echo $! > "$PIDFILE"
disown 2>/dev/null || true
echo "$(ts) started watchdog pid=$(cat "$PIDFILE")" >> "$LOG"
