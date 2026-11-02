# 扫描停滞判定。由 live_watchdog.sh / ensure_live.sh source，不要直接当服务跑。
# 实盘进程只有在一轮扫描成功返回后才更新 HL_SCAN_OK_FILE（默认 $ROOT/logs/live_scan.ok）。
# 子进程已经跑了、并且「这次启动之后」超过 HL_SCAN_STALL_S（默认 2400 秒 = 40 分钟）
# 仍没有成功扫描，就当成卡住。正常 15 分钟一轮不会触发。
# 新鲜心跳不能代替这个判定：心跳是 watchdog 写的，扫描线程卡死时心跳仍会刷新。
# 心跳本身停更（循环没在写）由 live_heartbeat.sh 另判，默认大约 3 分钟。

scan_stall_seconds() {
  printf '%s\n' "${HL_SCAN_STALL_S:-2400}"
}

scan_ok_file() {
  if [ -n "${HL_SCAN_OK_FILE:-}" ]; then
    printf '%s\n' "$HL_SCAN_OK_FILE"
    return
  fi
  printf '%s\n' "${ROOT:-.}/logs/live_scan.ok"
}

# 本次子进程生命周期内、最近一次成功扫描的 epoch。
# 标记比子进程启动还早（上一轮进程留下的）不算，改用子进程启动时间，避免刚拉起就被当成卡住。
scan_progress_epoch() {
  local child_start="$1"
  local marker_path marker
  marker_path=$(scan_ok_file)
  marker=0
  if [ -f "$marker_path" ]; then
    marker=$(stat -c %Y "$marker_path" 2>/dev/null || echo 0)
  fi
  case "$marker" in
    ''|*[!0-9]*) marker=0 ;;
  esac
  if [ "$marker" -ge "$child_start" ]; then
    printf '%s\n' "$marker"
  else
    printf '%s\n' "$child_start"
  fi
}

scan_stall_age() {
  local child_start="$1"
  local now progress
  now=$(date +%s)
  progress=$(scan_progress_epoch "$child_start")
  printf '%s\n' $(( now - progress ))
}

# 0 = 卡住，1 = 还在正常间隔内（或子进程刚启动）。
scan_is_stalled() {
  local child_start="$1"
  local age limit
  age=$(scan_stall_age "$child_start")
  limit=$(scan_stall_seconds)
  [ "$age" -ge "$limit" ]
}

# ps 的 etimes 是进程已运行秒数。失败时返回非 0，调用方不要把空串拿去比。
pid_start_epoch() {
  local pid="$1"
  local etimes now
  etimes=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d '[:space:]')
  [ -n "$etimes" ] || return 1
  case "$etimes" in
    ''|*[!0-9]*) return 1 ;;
  esac
  now=$(date +%s)
  printf '%s\n' $(( now - etimes ))
}

# 卡在 SSL 读上的解释器经常不响应 SIGTERM。先 TERM，几秒后仍在就 KILL。
kill_hung_pid() {
  local pid="$1"
  [ -n "${pid:-}" ] || return 0
  kill -TERM "$pid" 2>/dev/null || true
  sleep 5
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
}
