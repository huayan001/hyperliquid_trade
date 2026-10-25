"""BTC 日线 ADX 开关：off 不拉行情；shadow 只记日志；enforce 才跳过新趋势开仓。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from hl_bot.btc_regime import BTC_DAILY_BARS, BTC_REGIME_RETRY_MS, shadow_log_path
from hl_bot.config import BotConfig, load_config
from hl_bot.exchange.client import RateLimitError
from hl_bot.models import (
    IntentAction,
    OrderIntent,
    OrderKind,
    Position,
    Regime,
    Side,
    Signal,
    StrategyName,
)
from hl_bot.runner import BotRunner
from tests.candles import DAY, decision, market, uptrend

DAY1 = 1_758_000_000_000
DAY2 = DAY1 + 86_400_000
SHADOW_KEYS = {"utc", "symbol", "side", "btc_adx", "threshold", "would_block"}
ENFORCE_KEYS = {"utc", "symbol", "side", "btc_adx", "threshold", "blocked"}


def _freeze(monkeypatch, now_ms: int) -> None:
    def keys(now=None):
        dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
        iso = dt.isocalendar()
        return dt.strftime("%Y-%m-%d"), f"{iso.year}-W{iso.week:02d}", now_ms

    monkeypatch.setattr("hl_bot.runner._utc_keys", keys)


def _iso(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _trend_signal(symbol: str = "ETH", *, starter: bool = True) -> Signal:
    if starter:
        extras = {"starter": True, "starter_frac": 0.35, "size_frac": 0.35}
        tag = "trend_confirmed_starter"
        kind = OrderKind.MARKET
    else:
        extras = {"size_frac": 1.0}
        tag = "donchian_close_breakout"
        kind = OrderKind.MAKER_LIMIT
    return Signal(
        symbol=symbol,
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=kind,
        entry_price=100.0,
        stop_price=96.0,
        reason="trend open",
        tag=tag,
        extras=extras,
    )


def _contexts(symbols: tuple[str, ...]) -> dict:
    return {
        symbol: {
            "funding": 0.0,
            "mark_px": 100.0,
            "mid_px": 100.0,
            "max_leverage": 20,
            "sz_decimals": 4,
        }
        for symbol in symbols
    }


def _runner(
    tmp_path,
    monkeypatch,
    *,
    mode: str = "off",
    symbols: tuple[str, ...] = ("ETH",),
    now_ms: int = DAY1,
    adx_value: float = 30.0,
):
    _freeze(monkeypatch, now_ms)
    monkeypatch.setattr(
        "hl_bot.btc_regime.adx",
        lambda candles, period=14: [float(adx_value)] * len(candles),
    )
    cfg = BotConfig()
    cfg.dry_run = True
    cfg.symbols = symbols
    cfg.paper_equity = 10_000.0
    cfg.state_path = str(tmp_path / "paper_state.json")
    cfg.trend.btc_regime_mode = mode
    cfg.trend.btc_regime_adx_min = 23.35
    cfg.trend.btc_regime_adx_period = 14
    runner = BotRunner(cfg)
    runner.client.fetch_asset_contexts = lambda: _contexts(symbols)  # type: ignore[method-assign]
    runner.client.fetch_mids = lambda: {symbol: 100.0 for symbol in symbols}  # type: ignore[method-assign]
    runner.client.load_market = lambda symbol, ctx=None, mid=None: market(symbol, mid=100.0, funding=0.0)  # type: ignore[method-assign]
    fetches: list[tuple[str, str, int]] = []

    def fetch_candles(symbol, interval, count=120):
        fetches.append((symbol, interval, count))
        return uptrend(120, interval=DAY)

    runner.client.fetch_candles = fetch_candles  # type: ignore[method-assign]
    runner.trend.generate_exits = lambda *a, **k: []  # type: ignore[method-assign]
    runner.mr.generate_exits = lambda *a, **k: []  # type: ignore[method-assign]
    runner.mr.expansion_halt = lambda *a, **k: (False, "")  # type: ignore[method-assign]
    executed: list[OrderIntent] = []
    original = runner._execute

    def wrap(intent, now_ms, persist_paper=False):
        executed.append(intent)
        return original(intent, now_ms, persist_paper=persist_paper)

    runner._execute = wrap  # type: ignore[method-assign]
    monkeypatch.setattr(
        "hl_bot.runner.route_regime",
        lambda *a, **k: decision(Regime.TREND_LONG, daily_adx=30),
    )
    return runner, fetches, executed


def _arm_open(runner: BotRunner, *, starter: bool = True) -> None:
    def generate(market, decision, now_ms, existing=None):
        return _trend_signal(market.symbol, starter=starter)

    runner.trend.generate_signal = generate  # type: ignore[method-assign]


def _opens(executed: list[OrderIntent]) -> list[OrderIntent]:
    return [item for item in executed if item.action is IntentAction.OPEN]


def test_code_default_off_and_toml_shadow(monkeypatch) -> None:
    cfg = BotConfig()
    assert cfg.trend.btc_regime_mode == "off"
    assert cfg.trend.btc_regime_adx_min == 23.35
    assert cfg.trend.btc_regime_adx_period == 14

    monkeypatch.delenv("HL_RISK_PCT", raising=False)
    loaded = load_config("config.toml")
    assert loaded.trend.btc_regime_mode == "shadow"
    assert loaded.trend.btc_regime_adx_min == 23.35
    assert loaded.trend.btc_regime_adx_period == 14
    assert abs(loaded.trend.risk_pct - 0.01) < 1e-12


def test_btc_regime_config_validation(tmp_path) -> None:
    def load(body: str) -> None:
        path = tmp_path / "bad.toml"
        path.write_text(body, encoding="utf-8")
        load_config(path)

    with pytest.raises(ValueError, match="btc_regime_mode"):
        load('[trend]\nbtc_regime_mode = "sometimes"\n')
    with pytest.raises(ValueError, match="btc_regime_adx_min"):
        load("[trend]\nbtc_regime_adx_min = 0\n")
    with pytest.raises(ValueError, match="btc_regime_adx_min"):
        load("[trend]\nbtc_regime_adx_min = 101\n")
    with pytest.raises(ValueError, match="btc_regime_adx_period"):
        load("[trend]\nbtc_regime_adx_period = 1\n")
    with pytest.raises(ValueError, match="btc_regime_adx_period"):
        load("[trend]\nbtc_regime_adx_period = 0\n")

    ok = tmp_path / "ok.toml"
    ok.write_text(
        '[trend]\nbtc_regime_mode = " SHADOW "\nbtc_regime_adx_min = 23.35\nbtc_regime_adx_period = 14\n',
        encoding="utf-8",
    )
    cfg = load_config(ok)
    assert cfg.trend.btc_regime_mode == "shadow"
    assert cfg.trend.btc_regime_adx_period == 14

    missing = tmp_path / "missing.toml"
    missing.write_text("[trend]\nrisk_pct = 0.02\n", encoding="utf-8")
    defaults = load_config(missing)
    assert defaults.trend.btc_regime_mode == "off"
    assert defaults.trend.btc_regime_adx_min == 23.35
    assert defaults.trend.btc_regime_adx_period == 14


def test_off_does_not_fetch_and_still_opens(tmp_path, monkeypatch) -> None:
    runner, fetches, executed = _runner(tmp_path, monkeypatch, mode="off", adx_value=10.0)
    _arm_open(runner, starter=True)

    def boom(*_a, **_k):
        raise AssertionError("off 模式不能询问 BTC regime")

    runner.btc_regime.blocks_new_entry = boom  # type: ignore[method-assign]
    runner.client.fetch_candles = boom  # type: ignore[method-assign]
    report = runner.scan_once()
    assert len(_opens(executed)) == 1
    assert _opens(executed)[0].extras.get("starter") is True
    assert report.symbols[0].intent is not None
    assert report.symbols[0].intent.action is IntentAction.OPEN
    assert fetches == []
    assert not shadow_log_path(runner.cfg.state_path).exists()
    assert not any("BTC regime" in note for note in report.symbols[0].notes)


def test_shadow_never_blocks_and_records_both_groups(tmp_path, monkeypatch, caplog) -> None:
    runner, fetches, executed = _runner(tmp_path, monkeypatch, mode="shadow", adx_value=30.0)
    _arm_open(runner, starter=True)
    path = shadow_log_path(runner.cfg.state_path)
    with caplog.at_level(logging.INFO):
        runner.scan_once()
    assert len(_opens(executed)) == 1
    assert fetches == [("BTC", "1d", BTC_DAILY_BARS)]
    rows = _read_jsonl(path)
    assert len(rows) == 1
    assert set(rows[0]) == SHADOW_KEYS
    assert rows[0]["would_block"] is False
    assert rows[0]["symbol"] == "ETH"
    assert rows[0]["side"] == "long"
    assert rows[0]["utc"] == _iso(DAY1)
    assert abs(rows[0]["btc_adx"] - 30.0) < 1e-9
    assert abs(rows[0]["threshold"] - 23.35) < 1e-9
    assert "BTC 日线 ADX(14)=30.00" in caplog.text

    monkeypatch.setattr(
        "hl_bot.btc_regime.adx",
        lambda candles, period=14: [10.0] * len(candles),
    )
    _freeze(monkeypatch, DAY2)
    with caplog.at_level(logging.INFO):
        runner.scan_once()
    assert len(_opens(executed)) == 2
    assert fetches == [("BTC", "1d", BTC_DAILY_BARS), ("BTC", "1d", BTC_DAILY_BARS)]
    rows = _read_jsonl(path)
    assert [row["would_block"] for row in rows] == [False, True]
    assert rows[1]["utc"] == _iso(DAY2)
    assert abs(rows[1]["btc_adx"] - 10.0) < 1e-9
    assert "仅记录不拦截" in caplog.text
    assert caplog.text.count("BTC 日线 ADX(14)=") == 2


def test_shadow_same_utc_day_fetches_once_and_logs_adx_once(tmp_path, monkeypatch, caplog) -> None:
    runner, fetches, executed = _runner(
        tmp_path, monkeypatch, mode="shadow", symbols=("ETH", "SOL"), adx_value=18.0
    )
    _arm_open(runner, starter=False)
    with caplog.at_level(logging.INFO):
        report = runner.scan_once()
    assert [item.symbol for item in _opens(executed)] == ["ETH", "SOL"]
    assert fetches == [("BTC", "1d", BTC_DAILY_BARS)]
    rows = _read_jsonl(shadow_log_path(runner.cfg.state_path))
    assert [row["symbol"] for row in rows] == ["ETH", "SOL"]
    assert all(row["would_block"] is True for row in rows)
    assert all(set(row) == SHADOW_KEYS for row in rows)
    assert caplog.text.count("BTC 日线 ADX(14)=18.00") == 1
    assert all(item.intent is not None and item.intent.action is IntentAction.OPEN for item in report.symbols)

    runner.scan_once()
    assert fetches == [("BTC", "1d", BTC_DAILY_BARS)]
    assert len(_opens(executed)) == 4
    assert len(_read_jsonl(shadow_log_path(runner.cfg.state_path))) == 4


def test_enforce_blocks_below_allows_at_and_above(tmp_path, monkeypatch) -> None:
    runner, fetches, executed = _runner(tmp_path, monkeypatch, mode="enforce", adx_value=30.0)
    _arm_open(runner, starter=True)
    path = shadow_log_path(runner.cfg.state_path)
    runner.scan_once()
    assert len(_opens(executed)) == 1
    assert _read_jsonl(path) == []

    monkeypatch.setattr(
        "hl_bot.btc_regime.adx",
        lambda candles, period=14: [23.35] * len(candles),
    )
    _freeze(monkeypatch, DAY2)
    runner.scan_once()
    assert len(_opens(executed)) == 2
    assert _read_jsonl(path) == []

    low_day = DAY2 + 86_400_000
    monkeypatch.setattr(
        "hl_bot.btc_regime.adx",
        lambda candles, period=14: [23.349] * len(candles),
    )
    _freeze(monkeypatch, low_day)
    report = runner.scan_once()
    assert len(_opens(executed)) == 2
    assert report.symbols[0].signal is not None
    assert report.symbols[0].signal.tag == "trend_confirmed_starter"
    assert report.symbols[0].intent is None
    assert any("跳过新趋势开仓" in note for note in report.symbols[0].notes)
    rows = _read_jsonl(path)
    assert len(rows) == 1
    assert set(rows[0]) == ENFORCE_KEYS
    assert rows[0]["blocked"] is True
    assert rows[0]["symbol"] == "ETH"
    assert rows[0]["side"] == "long"
    assert rows[0]["utc"] == _iso(low_day)
    assert abs(rows[0]["btc_adx"] - 23.349) < 1e-9
    assert abs(rows[0]["threshold"] - 23.35) < 1e-9
    assert len(fetches) == 3


def test_fail_open_on_fetch_error_and_retries_later(tmp_path, monkeypatch, caplog) -> None:
    runner, fetches, executed = _runner(tmp_path, monkeypatch, mode="enforce", adx_value=5.0)
    _arm_open(runner)
    state = {"raise": True, "calls": 0}

    def fetch_candles(symbol, interval, count=120):
        state["calls"] += 1
        fetches.append((symbol, interval, count))
        if state["raise"]:
            raise RateLimitError("429")
        return uptrend(120, interval=DAY)

    runner.client.fetch_candles = fetch_candles  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        runner.scan_once()
    assert len(_opens(executed)) == 1
    assert state["calls"] == 1
    assert "fail-open" in caplog.text
    assert _read_jsonl(shadow_log_path(runner.cfg.state_path)) == []

    runner.scan_once()
    assert state["calls"] == 1
    assert len(_opens(executed)) == 2

    _freeze(monkeypatch, DAY1 + BTC_REGIME_RETRY_MS)
    runner.scan_once()
    assert state["calls"] == 2
    assert len(_opens(executed)) == 3

    state["raise"] = False
    _freeze(monkeypatch, DAY1 + BTC_REGIME_RETRY_MS + 86_400_000)
    runner.scan_once()
    assert state["calls"] == 3
    assert len(_opens(executed)) == 3
    rows = _read_jsonl(shadow_log_path(runner.cfg.state_path))
    assert len(rows) == 1
    assert rows[0]["blocked"] is True


def test_shadow_fail_open_does_not_block(tmp_path, monkeypatch, caplog) -> None:
    runner, _fetches, executed = _runner(tmp_path, monkeypatch, mode="shadow", adx_value=1.0)
    _arm_open(runner)

    def fetch_candles(symbol, interval, count=120):
        raise RuntimeError("candle down")

    runner.client.fetch_candles = fetch_candles  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        runner.scan_once()
    assert len(_opens(executed)) == 1
    assert "fail-open" in caplog.text
    assert not shadow_log_path(runner.cfg.state_path).exists()


def test_rejected_open_does_not_fetch_or_log(tmp_path, monkeypatch) -> None:
    runner, fetches, executed = _runner(tmp_path, monkeypatch, mode="shadow", adx_value=1.0)
    runner.cfg.trend.max_positions = 0
    _arm_open(runner)
    report = runner.scan_once()
    assert fetches == []
    assert _opens(executed) == []
    assert any("风控拒绝" in note for note in report.symbols[0].notes)
    assert not shadow_log_path(runner.cfg.state_path).exists()


def test_adds_exits_and_mean_reversion_unaffected(tmp_path, monkeypatch) -> None:
    runner, fetches, executed = _runner(tmp_path, monkeypatch, mode="enforce", adx_value=1.0)

    def forbid_new_open(*_a, **_k):
        raise AssertionError("已有仓或非趋势新开仓不应走 generate_signal")

    runner.trend.generate_signal = forbid_new_open  # type: ignore[method-assign]
    close = OrderIntent(
        action=IntentAction.CLOSE,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.SHORT,
        kind=OrderKind.MARKET,
        size=1.0,
        price=100.0,
        stop_price=None,
        leverage=5,
        isolated=True,
        reduce_only=True,
        reason="趋势失效离场",
    )
    runner.account.positions.append(
        Position(
            symbol="ETH",
            strategy=StrategyName.TREND,
            side=Side.LONG,
            size=1.0,
            entry_price=100.0,
            stop_price=90.0,
            leverage=5,
            opened_ts=1,
            tag="donchian_close_breakout",
            extras={"_opened_size": 1.0},
        )
    )
    runner.trend.generate_exits = lambda *a, **k: [close]  # type: ignore[method-assign]
    runner.scan_once()
    assert [item.action for item in executed] == [IntentAction.CLOSE]
    assert fetches == []

    executed.clear()
    runner.account.positions.clear()
    runner.account.positions.append(
        Position(
            symbol="ETH",
            strategy=StrategyName.TREND,
            side=Side.LONG,
            size=0.35,
            entry_price=100.0,
            stop_price=80.0,
            leverage=5,
            opened_ts=1,
            tag="trend_confirmed_starter",
            extras={
                "_opened_size": 0.35,
                "starter_pending_add": True,
                "starter_frac": 0.35,
                "intended_full_size": 1.0,
                "intended_risk_usd": 200.0,
            },
        )
    )
    add = Signal(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        entry_price=100.0,
        stop_price=80.0,
        reason="第二档补仓",
        tag="donchian_close_breakout",
        extras={
            "tier_add": True,
            "add_size": 0.65,
            "intended_full_size": 1.0,
            "starter_frac": 0.35,
            "original_size": 0.35,
        },
    )
    runner.trend.generate_exits = lambda *a, **k: []  # type: ignore[method-assign]
    runner.trend.generate_breakout_add = lambda *a, **k: add  # type: ignore[method-assign]
    runner.scan_once()
    assert [item.action for item in executed] == [IntentAction.ADD]
    assert fetches == []

    executed.clear()
    runner.account.positions.clear()
    runner.account.positions.append(
        Position(
            symbol="ETH",
            strategy=StrategyName.TREND,
            side=Side.LONG,
            size=1.0,
            entry_price=100.0,
            stop_price=90.0,
            leverage=5,
            opened_ts=1,
            tag="donchian_close_breakout",
            extras={"_opened_size": 1.0},
        )
    )
    pyramid = Signal(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MARKET,
        entry_price=110.0,
        stop_price=105.0,
        reason="金字塔",
        tag="pyramid",
        extras={"original_size": 1.0, "add_size": 0.4, "pyramid": True},
    )
    runner.trend.generate_pyramid = lambda *a, **k: pyramid  # type: ignore[method-assign]
    runner.scan_once()
    assert [item.action for item in executed] == [IntentAction.ADD]
    assert fetches == []

    executed.clear()
    runner.account.positions.clear()
    monkeypatch.setattr(
        "hl_bot.runner.route_regime",
        lambda *a, **k: decision(Regime.MEAN_REVERSION, daily_adx=12, hourly_adx=12, is_range=True),
    )
    mr = Signal(
        symbol="ETH",
        strategy=StrategyName.MEAN_REVERSION,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        entry_price=100.0,
        stop_price=98.0,
        reason="均值回归",
        tag="mr",
    )
    runner.mr.generate_signal = lambda *a, **k: mr  # type: ignore[method-assign]
    runner.scan_once()
    assert [item.action for item in executed] == [IntentAction.OPEN]
    assert executed[0].strategy is StrategyName.MEAN_REVERSION
    assert fetches == []


def test_full_breakout_open_is_gated_like_starter(tmp_path, monkeypatch) -> None:
    runner, fetches, executed = _runner(tmp_path, monkeypatch, mode="enforce", adx_value=10.0)
    _arm_open(runner, starter=False)
    report = runner.scan_once()
    assert _opens(executed) == []
    assert report.symbols[0].signal.tag == "donchian_close_breakout"
    assert report.symbols[0].intent is None
    assert fetches == [("BTC", "1d", BTC_DAILY_BARS)]
    assert _read_jsonl(shadow_log_path(runner.cfg.state_path))[0]["blocked"] is True
