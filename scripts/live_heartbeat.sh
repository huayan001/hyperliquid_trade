# 心跳过期判定。由 live_watchdog.sh / ensure_live.sh source，不要直接当服务跑。
#
# watchdog 在子进程还活着时，大约每 HL_WATCHDOG_HEARTBEAT_S（默认 30 秒）
# 原子写一次 logs/live_watchdog.heartbeat。健康时 hb_age 应接近这个周期。
#
# HL_HB_STALE_S 默认 180 秒（3 分钟）。有效阈值还会再抬高到：
#   max(配置值, 3 × 心跳周期, 90 秒)
# 因此一次 sleep 变慢、cron 晚几秒，都不会重启。要连续多个周期没写上心跳才算。
# 想更早一点可以设 120（2 分钟）；设得比 90 秒还小会被抬回 90 秒，避免误杀。
#
# 这和 live_scan.ok 的约 40 分钟扫描停滞是两道闸，互不替代：
# - 心跳停更：watchdog 循环没在跑（整机卡过之后 PID 还在，或 sleep 很晚才返回）
# - 扫描停滞：心跳仍新鲜，但 python 久未成功扫完（例如卡在 clearinghouseState）
#
# 刚拉起的 watchdog 还没覆写上一份心跳文件时，用进程启动时间兜住：
# 进程自己还没活过有效阈值，就不按心跳重启。读不到启动时间同样不按心跳杀。

if ! declare -F scan_is_stalled >/dev/null 2>&1; then
  # shellcheck source=live_scan_stall.sh
  source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/live_scan_stall.sh"
fi

# 只读 KEY=VALUE，不 source .env（里面有私钥）。注释行、非数字都忽略。
read_dotenv_value() {
  local key="$1"
  local file="$2"
  local line val
  [ -n "${file:-}" ] && [ -f "$file" ] || return 0
  line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" 2>/dev/null | tail -n 1 || true)
  [ -n "$line" ] || return 0
  val=${line#*=}
  val=${val%%$'\r'}
  val=$(printf '%s' "$val" | sed -e 's/[[:space:]]*#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\\(.*\\)'$/\\1/")
  printf '%s' "$val"
}

# 环境变量优先；未设置时再从 .env 取三个保活数字。最后补默认值。
hydrate_keepalive_env() {
  local envfile="${1:-}"
  local key val
  for key in HL_HB_STALE_S HL_SCAN_STALL_S HL_WATCHDOG_HEARTBEAT_S; do
    if [ -n "${!key:-}" ]; then
      continue
    fi
    val=$(read_dotenv_value "$key" "$envfile")
    case "$val" in
      ''|*[!0-9]*) continue ;;
    esac
    export "${key}=${val}"
  done
  export HL_HB_STALE_S="${HL_HB_STALE_S:-180}"
  export HL_SCAN_STALL_S="${HL_SCAN_STALL_S:-2400}"
  export HL_WATCHDOG_HEARTBEAT_S="${HL_WATCHDOG_HEARTBEAT_S:-30}"
}

# 实际用来比较的秒数，不是原始配置。日志请打这个值。
hb_stale_seconds() {
  local raw="${HL_HB_STALE_S:-180}"
  local interval="${HL_WATCHDOG_HEARTBEAT_S:-30}"
  local floor
  case "$raw" in
    ''|*[!0-9]*) raw=180 ;;
  esac
  case "$interval" in
    ''|*[!0-9]*) interval=30 ;;
  esac
  if [ "$interval" -lt 1 ]; then
    interval=30
  fi
  floor=$(( interval * 3 ))
  if [ "$floor" -lt 90 ]; then
    floor=90
  fi
  if [ "$raw" -lt "$floor" ]; then
    raw=$floor
  fi
  printf '%s\n' "$raw"
}

# 0 = 心跳过期，1 = 还新鲜、或数据不够（宁可不杀）。
# $1 心跳文件 mtime（文件不存在时调用方传 0）
# $2 now
# $3 可选：watchdog 启动时刻。传入时，进程太年轻或读不到启动时间都返回 1。
#    省略 $3 表示调用方在量「这一拍实际隔了多久」（watchdog 自己的 sleep）。
heartbeat_is_stale() {
  local hb_mtime="$1"
  local now="$2"
  local limit age proc_start
  limit=$(hb_stale_seconds)
  case "$hb_mtime" in
    ''|*[!0-9]*) return 1 ;;
  esac
  case "$now" in
    ''|*[!0-9]*) return 1 ;;
  esac
  age=$(( now - hb_mtime ))
  if [ "$age" -lt 0 ] || [ "$age" -lt "$limit" ]; then
    return 1
  fi
  if [ "$#" -ge 3 ]; then
    proc_start="$3"
    case "$proc_start" in
      ''|*[!0-9]*) return 1 ;;
    esac
    if [ "$proc_start" -le 0 ]; then
      return 1
    fi
    if [ $(( now - proc_start )) -lt "$limit" ]; then
      return 1
    fi
  fi
  return 0
}

# ensure_live 的判定。只打印一个词：ok | hb_stale | scan_stalled | missing
# $1 wd_alive  $2 bot_alive  $3 hb_mtime  $4 now  $5 wd_start  $6 scan_stalled(1/0)
# 进程不齐走 missing（原有重启）。两道闸都在、且进程都在时，心跳过期优先记原因，
# 但两条都会让调用方进入同一条干净重启。
live_keepalive_action() {
  local wd_alive="$1"
  local bot_alive="$2"
  local hb_mtime="$3"
  local now="$4"
  local wd_start="$5"
  local scan_stalled="${6:-0}"
  if [ "$wd_alive" != 1 ] || [ "$bot_alive" != 1 ]; then
    printf '%s\n' missing
    return 0
  fi
  if heartbeat_is_stale "$hb_mtime" "$now" "$wd_start"; then
    printf '%s\n' hb_stale
    return 0
  fi
  if [ "$scan_stalled" = 1 ]; then
    printf '%s\n' scan_stalled
    return 0
  fi
  printf '%s\n' ok
  return 0
}

# watchdog 每一拍：hb_gap | scan | ok
# $1 上一拍时刻  $2 now  $3 子进程启动时刻
# 心跳间隔用两参数形式，不套用「进程年轻」宽限：这一拍本该约 30 秒就返回。
watchdog_tick_action() {
  local last_tick="$1"
  local now="$2"
  local child_start="$3"
  if heartbeat_is_stale "$last_tick" "$now"; then
    printf '%s\n' hb_gap
    return 0
  fi
  if scan_is_stalled "$child_start"; then
    printf '%s\n' scan
    return 0
  fi
  printf '%s\n' ok
  return 0
}
