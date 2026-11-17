"""菲阿里纯规则、默认关闭的入口，以及它不碰趋势主循环。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hl_bot.cli import main
from hl_bot.models import StrategyName
from hl_bot.phil_ali.gate import PhilAliGate, assert_crypto_symbol, live_orders_allowed, load_gate, parse_symbols
from hl_bot.phil_ali.rules import H_MS, simulate_symbol
from hl_bot.phil_ali.runner import run_cycle
from hl_bot.phil_ali.session import Book, on_new_bars

ROOT = Path(__file__).resolve().parents[1]


def _ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


def _hours(day_ts: int, overrides: dict[int, tuple[float, float, float, float]]) -> list[dict]:
    bars = []
    for hour in range(24):
        open_, high, low, close = overrides.get(hour, (100.0, 105.0, 95.0, 100.0))
        bars.append({"t": day_ts + hour * H_MS, "o": open_, "h": high, "l": low, "c": close})
    return bars


def _sample():
    day0 = _ms(2024, 1, 1)
    day1 = _ms(2024, 1, 2)
    daily = [
        {"t": day0, "o": 100.0, "h": 110.0, "l": 90.0, "c": 105.0},
        {"t": day1, "o": 100.0, "h": 113.0, "l": 95.0, "c": 112.0},
    ]
    hourly = _hours(
        day1,
        {
            3: (108.0, 112.0, 107.0, 111.0),
            23: (111.0, 113.0, 111.0, 112.0),
        },
    )
    return day0, day1, daily, hourly


def _gate(tmp_path: Path, **overrides) -> PhilAliGate:
    values = dict(
        enabled=True,
        mode="paper",
        arm_live=False,
        symbols=("BTC",),
        risk_frac=0.01,
        paper_equity=10_000.0,
        fee_rt=0.0005,
        poll_seconds=300,
        network="mainnet",
        leverage=10,
        state_dir=str(tmp_path),
        cli_live=False,
        dry_run=True,
        enable_live=False,
    )
    values.update(overrides)
    return PhilAliGate(**values)


def _quiet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HL_PHIL_ALI_ENABLED", "0")
    monkeypatch.setenv("HL_PHIL_ALI_MODE", "off")
    monkeypatch.setenv("HL_PHIL_ALI_ARM_LIVE", "0")
    monkeypatch.setenv("HL_PHIL_ALI_SYMBOLS", "BTC,ETH,SOL")
    monkeypatch.setenv("HL_DRY_RUN", "true")
    monkeypatch.setenv("HL_ENABLE_LIVE", "0")


def test_default_gate_ignores_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_env(monkeypatch)
    monkeypatch.setenv("HL_PHIL_ALI_MODE", "live")
    monkeypatch.setenv("HL_PHIL_ALI_ARM_LIVE", "1")
    monkeypatch.setenv("HL_ENABLE_LIVE", "1")
    monkeypatch.setenv("HL_DRY_RUN", "false")
    gate = load_gate(cli_live=True)
    assert gate.enabled is False
    assert gate.mode == "off"
    assert gate.active is False
    assert live_orders_allowed(gate) is False


def test_live_orders_need_every_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_env(monkeypatch)
    monkeypatch.setenv("HL_PHIL_ALI_ENABLED", "1")
    monkeypatch.setenv("HL_PHIL_ALI_MODE", "live")
    monkeypatch.setenv("HL_PHIL_ALI_ARM_LIVE", "1")
    monkeypatch.setenv("HL_ENABLE_LIVE", "1")
    monkeypatch.setenv("HL_DRY_RUN", "false")
    assert live_orders_allowed(load_gate(cli_live=False)) is False
    assert live_orders_allowed(load_gate(cli_live=True)) is True
    monkeypatch.setenv("HL_PHIL_ALI_ARM_LIVE", "0")
    assert live_orders_allowed(load_gate(cli_live=True)) is False


def test_gold_symbols_rejected() -> None:
    for name in ("XAU", "XAUUSD", "PAXG", "PAXGUSDT", "GOLD", "xaut"):
        with pytest.raises(ValueError):
            assert_crypto_symbol(name)
    with pytest.raises(ValueError):
        parse_symbols("BTC,PAXG")
    assert parse_symbols("eth,SOL") == ("ETH", "SOL")
    with pytest.raises(ValueError):
        simulate_symbol("XAU", [], [])


def test_long_break_eod_pnl_and_no_bias_filter() -> None:
    _day0, day1, daily, hourly = _sample()
    trades, _curve = simulate_symbol("BTC", daily, hourly)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.side == "long"
    assert trade.entry == pytest.approx(110.0)
    assert trade.exit == pytest.approx(112.0)
    assert trade.r == pytest.approx(20.0)
    assert trade.qty == pytest.approx(5.0)
    assert trade.pnl_net == pytest.approx(9.7225)
    assert trade.yo == pytest.approx(100.0)
    assert trade.yc == pytest.approx(105.0)
    # 当天开盘低于昨收，纯规则仍然做多。
    assert hourly[0]["o"] < trade.yc


def test_same_bar_both_sides_skips_the_day() -> None:
    day0 = _ms(2024, 1, 1)
    day1 = _ms(2024, 1, 2)
    daily = [
        {"t": day0, "o": 100.0, "h": 110.0, "l": 90.0, "c": 105.0},
        {"t": day1, "o": 100.0, "h": 120.0, "l": 80.0, "c": 100.0},
    ]
    hourly = _hours(day1, {1: (100.0, 111.0, 89.0, 100.0)})
    trades, _curve = simulate_symbol("ETH", daily, hourly)
    assert trades == []


def test_gap_open_fills_at_open_and_only_first_break() -> None:
    day0 = _ms(2024, 1, 1)
    day1 = _ms(2024, 1, 2)
    daily = [
        {"t": day0, "o": 100.0, "h": 110.0, "l": 90.0, "c": 105.0},
        {"t": day1, "o": 115.0, "h": 116.0, "l": 80.0, "c": 112.0},
    ]
    hourly = _hours(
        day1,
        {
            2: (115.0, 116.0, 114.0, 115.0),
            10: (100.0, 101.0, 80.0, 90.0),
            23: (112.0, 112.0, 112.0, 112.0),
        },
    )
    trades, _curve = simulate_symbol("SOL", daily, hourly)
    assert len(trades) == 1
    assert trades[0].side == "long"
    assert trades[0].entry == pytest.approx(115.0)
    assert trades[0].r == pytest.approx(25.0)
    assert trades[0].qty == pytest.approx(4.0)


def test_replay_matches_simulator() -> None:
    _day0, day1, daily, hourly = _sample()
    trades, _curve = simulate_symbol("BTC", daily, hourly)
    book = Book(equity=10_000.0, risk_frac=0.01, fee_rt=0.0005)
    book.last_bar_ts["BTC"] = -1
    on_new_bars(book, "BTC", daily, hourly, hourly[-1]["t"] + H_MS, fill=True)
    assert len(book.trades) == 1
    got = book.trades[0]
    assert got["side"] == trades[0].side
    assert got["entry"] == pytest.approx(trades[0].entry)
    assert got["exit"] == pytest.approx(trades[0].exit)
    assert got["qty"] == pytest.approx(trades[0].qty)
    assert got["pnl_net"] == pytest.approx(trades[0].pnl_net)
    assert book.equity == pytest.approx(trades[0].equity_after)


def test_prime_does_not_chase_an_earlier_break() -> None:
    _day0, day1, daily, hourly = _sample()
    book = Book(equity=10_000.0)
    # 小时 5 已收盘，小时 3 的突破已经过去。
    on_new_bars(book, "BTC", daily, hourly, day1 + 6 * H_MS, fill=True)
    assert book.positions == {}
    assert book.consumed["BTC"] == "2024-01-02"
    on_new_bars(book, "BTC", daily, hourly, day1 + 24 * H_MS, fill=True)
    assert book.positions == {}
    assert book.trades == []


def test_new_bar_enters_then_utc_close_flattens() -> None:
    _day0, day1, daily, hourly = _sample()
    book = Book(equity=10_000.0, risk_frac=0.01, fee_rt=0.0005)
    on_new_bars(book, "BTC", daily, hourly, day1 + 3 * H_MS, fill=True)
    assert book.positions == {}
    events = on_new_bars(book, "BTC", daily, hourly, day1 + 4 * H_MS, fill=True)
    assert [event.action for event in events] == ["enter"]
    assert book.positions["BTC"]["side"] == "long"
    events = on_new_bars(book, "BTC", daily, hourly, day1 + 24 * H_MS, fill=True)
    assert any(event.action == "exit" for event in events)
    assert book.positions == {}
    assert book.trades[0]["pnl_net"] == pytest.approx(9.7225)


def test_paper_and_unarmed_live_do_not_submit(tmp_path: Path) -> None:
    _day0, day1, daily, hourly = _sample()
    markets = {"BTC": (daily, hourly)}
    calls: list = []

    def _submit(intent):
        calls.append(intent)
        return {"status": "ok"}

    paper = Book(equity=10_000.0, risk_frac=0.01, fee_rt=0.0005)
    paper.last_bar_ts["BTC"] = day1 + 2 * H_MS
    run_cycle(paper, markets, day1 + 4 * H_MS, _gate(tmp_path, mode="paper"), submit=_submit)
    assert calls == []
    assert "BTC" in paper.positions

    live = Book(equity=10_000.0, risk_frac=0.01, fee_rt=0.0005)
    live.last_bar_ts["BTC"] = day1 + 2 * H_MS
    events = run_cycle(
        live,
        markets,
        day1 + 4 * H_MS,
        _gate(tmp_path / "live", mode="live", arm_live=False, cli_live=True, dry_run=False, enable_live=True),
        submit=_submit,
    )
    assert calls == []
    assert live.positions == {}
    assert events[0].action == "enter"
    assert events[0].submitted is False


def test_armed_live_submits_without_a_stop(tmp_path: Path) -> None:
    _day0, day1, daily, hourly = _sample()
    markets = {"BTC": (daily, hourly)}
    calls: list = []

    def _submit(intent):
        calls.append(intent)
        return {"status": "ok"}

    book = Book(equity=10_000.0, risk_frac=0.01, fee_rt=0.0005)
    book.last_bar_ts["BTC"] = day1 + 2 * H_MS
    gate = _gate(
        tmp_path,
        mode="live",
        arm_live=True,
        cli_live=True,
        dry_run=False,
        enable_live=True,
    )
    assert live_orders_allowed(gate) is True
    run_cycle(book, markets, day1 + 4 * H_MS, gate, submit=_submit)
    assert len(calls) == 1
    intent = calls[0]
    assert intent.stop_price is None
    assert intent.strategy is StrategyName.PHIL_ALI
    assert intent.reduce_only is False
    assert intent.symbol == "BTC"
    assert "BTC" in book.positions

    run_cycle(book, markets, day1 + 24 * H_MS, gate, submit=_submit)
    assert len(calls) == 2
    assert calls[1].reduce_only is True
    assert calls[1].stop_price is None
    assert book.positions == {}


def test_disabled_command_does_not_start_the_trend_bot(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _quiet_env(monkeypatch)

    def _boom(*_args, **_kwargs):
        raise AssertionError("趋势 BotRunner 不应被构造")

    monkeypatch.setattr("hl_bot.cli.BotRunner", _boom)
    monkeypatch.setattr("hl_bot.phil_ali.cli._run_once", _boom)
    assert main(["phil-ali", "--live"]) == 0
    assert "未启用" in capsys.readouterr().out


def test_trend_loop_and_watchdog_do_not_know_phil_ali() -> None:
    runner = (ROOT / "src/hl_bot/runner.py").read_text(encoding="utf-8")
    assert "phil_ali" not in runner
    assert "phil-ali" not in runner
    for name in ("scripts/ensure_live.sh", "scripts/live_watchdog.sh", "scripts/live_heartbeat.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "phil_ali" not in text
        assert "phil-ali" not in text
    fetch = (ROOT / "backtest/phil_ali/fetch.py").read_text(encoding="utf-8")
    assert "PAXGUSDT" not in fetch
    assert 'SYMBOLS = ("BTC", "ETH", "SOL")' in fetch
