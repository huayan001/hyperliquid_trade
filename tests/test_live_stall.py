"""成功扫描标记与 40 分钟停滞判定。正常 15 分钟间隔不能被当成卡住。"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from hl_bot.config import BotConfig
from hl_bot.runner import BotRunner, ScanReport, note_scan_complete

ROOT = Path(__file__).resolve().parents[1]
STALL = ROOT / "scripts" / "live_scan_stall.sh"
WATCHDOG = ROOT / "scripts" / "live_watchdog.sh"
ENSURE = ROOT / "scripts" / "ensure_live.sh"


def _stall_rc(child_start: int, marker: Path, stall_s: int = 2400) -> int:
    env = os.environ.copy()
    env["HL_SCAN_OK_FILE"] = str(marker)
    env["HL_SCAN_STALL_S"] = str(stall_s)
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; if scan_is_stalled "$2"; then exit 0; else exit 1; fi',
            "bash",
            str(STALL),
            str(child_start),
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.stderr == "", completed.stderr
    return completed.returncode


def test_shell_scripts_parse() -> None:
    for path in (STALL, WATCHDOG, ENSURE):
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_normal_15min_gap_is_not_a_stall(tmp_path: Path) -> None:
    now = int(time.time())
    marker = tmp_path / "live_scan.ok"
    marker.write_text("ok\n", encoding="utf-8")
    os.utime(marker, (now - 900, now - 900))
    # 进程已跑了两小时，最近一次成功扫描在 15 分钟前（正常轮询间隔）。
    assert _stall_rc(now - 7200, marker) == 1


def test_missing_successful_scan_for_40min_is_a_stall(tmp_path: Path) -> None:
    now = int(time.time())
    marker = tmp_path / "live_scan.ok"
    marker.write_text("ok\n", encoding="utf-8")
    os.utime(marker, (now - 3000, now - 3000))
    assert _stall_rc(now - 7200, marker) == 0


def test_fresh_child_ignores_previous_process_marker(tmp_path: Path) -> None:
    now = int(time.time())
    marker = tmp_path / "live_scan.ok"
    marker.write_text("old\n", encoding="utf-8")
    os.utime(marker, (now - 5000, now - 5000))
    # 刚拉起 30 秒：上一轮进程留下的旧标记不能立刻再杀一次。
    assert _stall_rc(now - 30, marker) == 1
    # 这次启动后 50 分钟仍没有新的成功扫描。
    assert _stall_rc(now - 3000, marker) == 0


def test_child_without_marker_stalls_only_after_threshold(tmp_path: Path) -> None:
    now = int(time.time())
    missing = tmp_path / "absent.ok"
    assert _stall_rc(now - 60, missing) == 1
    assert _stall_rc(now - 3000, missing) == 0


def test_current_process_is_not_stalled() -> None:
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; start=$(pid_start_epoch $$); test -n "$start"; if scan_is_stalled "$start"; then exit 2; fi',
            "bash",
            str(STALL),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_live_scan_marks_only_after_success(tmp_path, monkeypatch, caplog) -> None:
    marker = tmp_path / "logs" / "live_scan.ok"
    monkeypatch.setenv("HL_SCAN_OK_FILE", str(marker))
    monkeypatch.setattr("hl_bot.runner.time.sleep", lambda *_a, **_k: None)
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.state_path = str(tmp_path / "paper.json")
    cfg.poll_seconds = 15
    runner = BotRunner(cfg)
    calls = {"n": 0}

    def scan_once(*, persist_paper: bool = False):
        calls["n"] += 1
        if calls["n"] == 1:
            return ScanReport(network="mainnet", dry_run=False, equity=1.0, generated_at="t", symbols=[])
        raise KeyboardInterrupt

    runner.scan_once = scan_once  # type: ignore[method-assign]
    with caplog.at_level("INFO", logger="hl_bot.runner"):
        with pytest.raises(KeyboardInterrupt):
            runner.run_forever()
    assert calls["n"] == 2
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip()
    assert any(record.getMessage() == "扫描完成" for record in caplog.records)


def test_dry_run_success_does_not_touch_live_marker(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "live_scan.ok"
    monkeypatch.setenv("HL_SCAN_OK_FILE", str(marker))
    monkeypatch.setattr("hl_bot.runner.time.sleep", lambda *_a, **_k: None)
    cfg = BotConfig()
    cfg.state_path = str(tmp_path / "paper.json")
    cfg.poll_seconds = 15
    runner = BotRunner(cfg)
    calls = {"n": 0}

    def scan_once(*, persist_paper: bool = False):
        calls["n"] += 1
        if calls["n"] == 1:
            return ScanReport(network="mainnet", dry_run=True, equity=1.0, generated_at="t", symbols=[])
        raise KeyboardInterrupt

    runner.scan_once = scan_once  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        runner.run_forever()
    assert calls["n"] == 2
    assert not marker.exists()


def test_note_scan_complete_is_atomic(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "live_scan.ok"
    monkeypatch.setenv("HL_SCAN_OK_FILE", str(marker))
    note_scan_complete()
    assert marker.is_file()
    assert not marker.with_name(marker.name + ".tmp").exists()
