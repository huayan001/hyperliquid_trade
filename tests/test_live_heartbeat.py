"""心跳过期重启。不导入交易代码，也不连接 Hyperliquid。

健康时 watchdog 约每 30 秒写一次心跳。有效阈值默认 180 秒，
并且不会低于 90 秒或 3 个心跳周期，避免一次抖动就重启。
扫描停滞闸（默认 40 分钟）仍然单独成立。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEARTBEAT = ROOT / "scripts" / "live_heartbeat.sh"
STALL = ROOT / "scripts" / "live_scan_stall.sh"
WATCHDOG = ROOT / "scripts" / "live_watchdog.sh"
ENSURE = ROOT / "scripts" / "ensure_live.sh"

_KEEPALIVE_ENV = (
    "HL_HB_STALE_S",
    "HL_WATCHDOG_HEARTBEAT_S",
    "HL_SCAN_STALL_S",
    "HL_SCAN_OK_FILE",
    "HL_PRIVATE_KEY",
)


def _bash(
    body: str,
    args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in _KEEPALIVE_ENV:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", body, "bash", str(HEARTBEAT), *(args or [])],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _stale_rc(hb_mtime: int, now: int, proc_start: int | None = None, **env: str) -> int:
    if proc_start is None:
        body = 'source "$1"; if heartbeat_is_stale "$2" "$3"; then exit 0; else exit 1; fi'
        args = [str(hb_mtime), str(now)]
    else:
        body = 'source "$1"; if heartbeat_is_stale "$2" "$3" "$4"; then exit 0; else exit 1; fi'
        args = [str(hb_mtime), str(now), str(proc_start)]
    completed = _bash(body, args, extra_env=env or None)
    assert completed.stderr == "", completed.stderr
    return completed.returncode


def _action(wd: int, bot: int, hb_mtime: int, now: int, wd_start: str, scan: int, **env: str) -> str:
    completed = _bash(
        'source "$1"; live_keepalive_action "$2" "$3" "$4" "$5" "$6" "$7"',
        [str(wd), str(bot), str(hb_mtime), str(now), wd_start, str(scan)],
        extra_env=env or None,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    return completed.stdout.strip()


def _tick(last_tick: int, now: int, child_start: int, **env: str) -> str:
    completed = _bash(
        'source "$1"; watchdog_tick_action "$2" "$3" "$4"',
        [str(last_tick), str(now), str(child_start)],
        extra_env=env or None,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    return completed.stdout.strip()


def test_keepalive_scripts_parse() -> None:
    for path in (HEARTBEAT, STALL, WATCHDOG, ENSURE):
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_functions_survive_nounset() -> None:
    completed = _bash(
        """
        set -uo pipefail
        source "$1"
        hydrate_keepalive_env /no/such.env
        test "$(hb_stale_seconds)" = 180
        heartbeat_is_stale 1 2
        live_keepalive_action 0 0 0 10 "" 0
        watchdog_tick_action 10 20 20
        """
    )
    assert completed.returncode == 0, completed.stderr


def test_default_threshold_is_three_minutes() -> None:
    completed = _bash('source "$1"; hb_stale_seconds')
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "180"


def test_normal_heartbeat_interval_is_not_stale() -> None:
    now = 1_700_000_000
    assert _stale_rc(now - 30, now) == 1
    assert _stale_rc(now - 40, now) == 1
    assert _stale_rc(now - 179, now) == 1


def test_heartbeat_at_threshold_is_stale() -> None:
    now = 1_700_000_000
    assert _stale_rc(now - 180, now) == 0
    assert _stale_rc(now - 1000, now) == 0


def test_clock_step_backward_is_not_stale() -> None:
    now = 1_700_000_000
    assert _stale_rc(now + 30, now) == 1


def test_two_minute_override_still_ignores_brief_gaps() -> None:
    now = 1_700_000_000
    env = {"HL_HB_STALE_S": "120"}
    assert _stale_rc(now - 119, now, **env) == 1
    assert _stale_rc(now - 120, now, **env) == 0


def test_threshold_floor_blocks_restart_storm() -> None:
    now = 1_700_000_000
    env = {"HL_HB_STALE_S": "10", "HL_WATCHDOG_HEARTBEAT_S": "30"}
    completed = _bash('source "$1"; hb_stale_seconds', extra_env=env)
    assert completed.stdout.strip() == "90"
    assert _stale_rc(now - 89, now, **env) == 1
    assert _stale_rc(now - 90, now, **env) == 0


def test_long_heartbeat_interval_raises_threshold() -> None:
    now = 1_700_000_000
    env = {"HL_HB_STALE_S": "180", "HL_WATCHDOG_HEARTBEAT_S": "100"}
    completed = _bash('source "$1"; hb_stale_seconds', extra_env=env)
    assert completed.stdout.strip() == "300"
    assert _stale_rc(now - 299, now, **env) == 1
    assert _stale_rc(now - 300, now, **env) == 0


def test_invalid_threshold_falls_back_to_default() -> None:
    now = 1_700_000_000
    env = {"HL_HB_STALE_S": "abc"}
    assert _stale_rc(now - 179, now, **env) == 1
    assert _stale_rc(now - 180, now, **env) == 0


def test_young_watchdog_ignores_previous_heartbeat_file() -> None:
    now = 1_700_000_000
    # 旧文件已经 1000 秒没动，但这个 watchdog 才活了 20 秒。
    assert _stale_rc(now - 1000, now, now - 20) == 1
    assert _action(1, 1, now - 1000, now, str(now - 20), 0) == "ok"


def test_unknown_process_start_does_not_use_heartbeat_gate() -> None:
    now = 1_700_000_000
    assert _stale_rc(now - 1000, now, proc_start=0) == 1
    assert _action(1, 1, now - 1000, now, "", 0) == "ok"


def test_old_process_with_stale_heartbeat_restarts_before_scan_stall() -> None:
    now = 1_700_000_000
    # 扫描标记还新鲜（scan flag 0），但心跳已经 1000 秒。这正是要提前重启的情况。
    assert _action(1, 1, now - 1000, now, str(now - 7200), 0) == "hb_stale"
    assert _action(1, 1, 0, now, str(now - 7200), 0) == "hb_stale"


def test_fresh_heartbeat_still_honors_scan_stall() -> None:
    now = 1_700_000_000
    assert _action(1, 1, now - 20, now, str(now - 7200), 1) == "scan_stalled"


def test_both_gates_report_heartbeat_first_but_scan_gate_remains() -> None:
    now = 1_700_000_000
    assert _action(1, 1, now - 1000, now, str(now - 7200), 1) == "hb_stale"
    assert _action(1, 1, now - 20, now, str(now - 7200), 0) == "ok"


def test_missing_process_is_not_the_heartbeat_gate() -> None:
    now = 1_700_000_000
    assert _action(0, 1, now - 1000, now, str(now - 7200), 0) == "missing"
    assert _action(1, 0, now - 1000, now, str(now - 7200), 1) == "missing"


def test_watchdog_tick_restarts_on_long_gap_even_if_scan_is_fresh(tmp_path: Path) -> None:
    now = int(time.time())
    marker = tmp_path / "live_scan.ok"
    marker.write_text("ok\n", encoding="utf-8")
    os.utime(marker, (now, now))
    env = {"HL_SCAN_OK_FILE": str(marker), "HL_SCAN_STALL_S": "2400"}
    assert _tick(now - 30, now, now - 7200, **env) == "ok"
    assert _tick(now - 600, now, now - 7200, **env) == "hb_gap"


def test_watchdog_tick_keeps_scan_stall_when_heartbeat_gap_is_normal(tmp_path: Path) -> None:
    now = int(time.time())
    marker = tmp_path / "live_scan.ok"
    marker.write_text("old\n", encoding="utf-8")
    os.utime(marker, (now - 3000, now - 3000))
    env = {"HL_SCAN_OK_FILE": str(marker), "HL_SCAN_STALL_S": "2400"}
    assert _tick(now - 15, now, now - 7200, **env) == "scan"
    # 刚拉起的子进程不把上一轮的扫描标记当成卡住。
    assert _tick(now - 15, now, now - 30, **env) == "ok"


def test_dotenv_supplies_threshold_without_loading_secrets(tmp_path: Path) -> None:
    envfile = tmp_path / ".env"
    envfile.write_text(
        "\n".join(
            [
                "# HL_HB_STALE_S=999",
                'export HL_HB_STALE_S="150" # two and a half minutes',
                "HL_SCAN_STALL_S=1800",
                "HL_PRIVATE_KEY=not-a-real-key",
                "",
            ]
        ),
        encoding="utf-8",
    )
    completed = _bash(
        """
        source "$1"
        hydrate_keepalive_env "$2"
        printf 'hb=%s\\nscan=%s\\nbeat=%s\\nkey=%s\\n' \
          "$HL_HB_STALE_S" "$HL_SCAN_STALL_S" "$HL_WATCHDOG_HEARTBEAT_S" "${HL_PRIVATE_KEY-unset}"
        """,
        [str(envfile)],
    )
    assert completed.returncode == 0, completed.stderr
    assert "not-a-real-key" not in completed.stdout
    assert completed.stdout.splitlines() == [
        "hb=150",
        "scan=1800",
        "beat=30",
        "key=unset",
    ]


def test_process_environment_overrides_dotenv(tmp_path: Path) -> None:
    envfile = tmp_path / ".env"
    envfile.write_text("HL_HB_STALE_S=120\nHL_PRIVATE_KEY=not-a-real-key\n", encoding="utf-8")
    completed = _bash(
        """
        source "$1"
        hydrate_keepalive_env "$2"
        printf '%s\\n' "$HL_HB_STALE_S"
        """,
        [str(envfile)],
        extra_env={"HL_HB_STALE_S": "180"},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "180"
    assert "not-a-real-key" not in completed.stdout + completed.stderr


def test_ensure_dry_run_is_before_signals_and_both_gates_are_wired() -> None:
    text = ENSURE.read_text(encoding="utf-8")
    assert "live_keepalive_action" in text
    assert "hb_mtime=0" in text
    assert "heartbeat stale" in text
    assert "scan stalled" in text
    guard = text.index('if [ "${HL_ENSURE_DRY_RUN:-0}" = 1 ]')
    assert guard < text.index("kill -TERM")
    assert guard < text.index('kill "$wd_pid"')

    watchdog = WATCHDOG.read_text(encoding="utf-8")
    assert "watchdog_tick_action" in watchdog
    assert "hb_gap" in watchdog
    assert "scan stalled" in watchdog
    assert "hung child killed after scan stall" in watchdog
    assert watchdog.index("hb_gap") < watchdog.index("exited code=")
