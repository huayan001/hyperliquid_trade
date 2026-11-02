#!/usr/bin/env bash
# 保活入口：检查 live watchdog + 实盘 bot 是否在跑。下面几种情况走同一条干净重启
# （TERM，几秒后仍在则 KILL，再拉起 watchdog）：
# 1. watchdog 或 bot 不在。
# 2. 两边都在，但心跳过期。watchdog 约每 30 秒写 logs/live_watchdog.heartbeat。
#    hb_age >= 有效 HL_HB_STALE_S（默认 180 秒，且至少 3 个心跳周期、不低于 90 秒）
#    就重启。用来抓住「PID 还在，但循环或整机卡住、心跳停了」。
#    刚拉起、还没活过该阈值的 watchdog，不因上一份旧心跳文件被误杀。
# 3. 两边都在、心跳也新鲜，但超过约 40 分钟没有成功扫完（logs/live_scan.ok）。
#    扫描卡在 HTTP 读上时心跳仍会刷新，所以这道闸留下，不能只靠心跳。
# HL_ENSURE_DRY_RUN=1 只写日志，不发信号。
# 阈值可放环境变量，或写在 .env 的 HL_HB_STALE_S / HL_SCAN_STALL_S（不 source 私钥）。
# 所有时间戳显式使用 Asia/Shanghai（+08:00），不依赖系统时区（系统时区可能被改成别的）。
ROOT="/workspace/hyperliquid_trade"
cd "$ROOT" || exit 0
mkdir -p logs
# shellcheck source=live_scan_stall.sh
source "$ROOT/scripts/live_scan_stall.sh" || exit 0
# shellcheck source=live_heartbeat.sh
source "$ROOT/scripts/live_heartbeat.sh" || exit 0
PIDFILE="$ROOT/logs/live_watchdog.pid"
LOG="$ROOT/logs/ensure_live.log"
BTIME_FILE="$ROOT/logs/last_btime"
HEARTBEAT_FILE="$ROOT/logs/live_watchdog.heartbeat"
export HL_SCAN_OK_FILE="${HL_SCAN_OK_FILE:-$ROOT/logs/live_scan.ok}"
hydrate_keepalive_env "$ROOT/.env"
# /proc/stat 的 btime 是内核记录的开机时刻。这里只记日志，不因此重启。
# 漂移超过 BTIME_TOLERANCE_S 会打出一行 box rebooted，有时是误报；本次不改这个判定。
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
# 文件不存在时 mtime 记 0。年轻进程的宽限在 heartbeat_is_stale 里，不会马上误杀。
hb_mtime=0
hb_info="hb=none"
if [ -f "$HEARTBEAT_FILE" ]; then
  hb_mtime=$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null || echo 0)
  case "$hb_mtime" in
    ''|*[!0-9]*) hb_mtime=0 ;;
  esac
fi
now=$(date +%s)
if [ -f "$HEARTBEAT_FILE" ] && [ "$hb_mtime" -gt 0 ]; then
  hb_age=$(( now - hb_mtime ))
  hb_info="hb_age=${hb_age}s"
fi

live_python_pids() {
  ps -eo pid=,args= | awk '/[p]ython/ && /-m hl_bot/ && /--live/ {print $1}'
}

live_python_stalled() {
  local pid start
  for pid in $(live_python_pids); do
    start=$(pid_start_epoch "$pid" || true)
    if [ -n "$start" ] && scan_is_stalled "$start"; then
      return 0
    fi
  done
  return 1
}

bot_alive=0
for pid in $(live_python_pids); do
  bot_alive=1
done

wd_pid=""
if [ -f "$PIDFILE" ]; then
  wd_pid=$(cat "$PIDFILE" 2>/dev/null || true)
fi

wd_alive=0
wd_start=""
if is_alive "$wd_pid"; then
  wd_alive=1
  wd_start=$(pid_start_epoch "$wd_pid" || true)
fi
scan_flag=0
if live_python_stalled; then
  scan_flag=1
fi
action=$(live_keepalive_action "$wd_alive" "$bot_alive" "$hb_mtime" "$now" "$wd_start" "$scan_flag")
case "$action" in
  ok)
    echo "$(ts) live already running wd=$wd_pid $hb_info" >> "$LOG"
    exit 0
    ;;
  hb_stale)
    echo "$(ts) live process alive but heartbeat stale (${hb_info} >= $(hb_stale_seconds)s effective; HL_HB_STALE_S=${HL_HB_STALE_S}, watchdog writes every ${HL_WATCHDOG_HEARTBEAT_S}s, scan-stall gate still ${HL_SCAN_STALL_S}s) — restarting" >> "$LOG"
    ;;
  scan_stalled)
    echo "$(ts) live process alive but scan stalled (no successful scan for >=${HL_SCAN_STALL_S}s, heartbeat is not enough) — restarting hung python" >> "$LOG"
    ;;
  missing)
    echo "$(ts) live missing (wd_alive=$wd_alive bot=$bot_alive $hb_info) — restarting" >> "$LOG"
    ;;
  *)
    echo "$(ts) live keepalive action unknown (${action:-empty} $hb_info) — not restarting" >> "$LOG"
    exit 0
    ;;
esac

if [ "${HL_ENSURE_DRY_RUN:-0}" = 1 ]; then
  echo "$(ts) dry-run: would restart action=$action wd=${wd_pid:-none} $hb_info (no signals sent)" >> "$LOG"
  exit 0
fi

if is_alive "$wd_pid"; then
  kill "$wd_pid" 2>/dev/null || true
fi
for pid in $(live_python_pids); do
  kill -TERM "$pid" 2>/dev/null || true
done
sleep 2
for pid in $(live_python_pids); do
  if kill -0 "$pid" 2>/dev/null; then
    echo "$(ts) python $pid ignored SIGTERM (likely stuck in HTTPS read); SIGKILL" >> "$LOG"
    kill -KILL "$pid" 2>/dev/null || true
  fi
done

nohup bash "$ROOT/scripts/live_watchdog.sh" 900 >> "$ROOT/logs/watchdog_live.out" 2>&1 &
echo $! > "$PIDFILE"
disown 2>/dev/null || true
echo "$(ts) started watchdog pid=$(cat "$PIDFILE")" >> "$LOG"
