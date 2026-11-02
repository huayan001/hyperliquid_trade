"""净空或 BTC 日线明确空头时，禁止新标的做多。已有多仓的管理和做空不拦。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from hl_bot.btc_regime import BTC_DAILY_BARS, shadow_log_path
from hl_bot.config import BotConfig, load_config
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
from hl_bot.portfolio import net_exposure_blocks_long, signed_open_notional
from hl_bot.regime import daily_clear_trend_side, decide_from_metrics
from hl_bot.runner import BotRunner
from tests.candles import DAY, decision, downtrend, dummy_range, market, uptrend

DAY1 = 1_758_000_000_000


def _freeze(monkeypatch, now_ms: int) -> None:
    def keys(now=None):
        dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
        iso = dt.isocalendar()
        return dt.strftime("%Y-%m-%d"), f"{iso.year}-W{iso.week:02d}", now_ms

    monkeypatch.setattr("hl_bot.runner._utc_keys", keys)


def _pos(
    symbol: str,
    side: Side,
    notional: float,
    *,
    entry: float = 100.0,
    stop: float | None = None,
) -> Position:
    size = notional / entry
    if stop is None:
        stop = entry * (0.9 if side is Side.LONG else 1.01)
    return Position(
        symbol=symbol,
        strategy=StrategyName.TREND,
        side=side,
        size=size,
        entry_price=entry,
        stop_price=stop,
        leverage=5,
        opened_ts=1,
        tag="donchian_close_breakout",
        extras={"_opened_size": size},
    )


def _signal(symbol: str, side: Side, *, strategy: StrategyName = StrategyName.TREND) -> Signal:
    stop = 96.0 if side is Side.LONG else 104.0
    return Signal(
        symbol=symbol,
        strategy=strategy,
        side=side,
        kind=OrderKind.MARKET,
        entry_price=100.0,
        stop_price=stop,
        reason="open",
        tag="trend_confirmed_starter" if strategy is StrategyName.TREND else "mr",
        extras={"starter": True, "starter_frac": 0.35, "size_frac": 0.35}
        if strategy is StrategyName.TREND
        else {},
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
    symbols: tuple[str, ...] = ("HYPE",),
    now_ms: int = DAY1,
    adx_value: float = 30.0,
    candles: str = "down",
    bearish_mode: str = "off",
    adx_mode: str = "off",
    block_net_short: bool = True,
    regime: Regime = Regime.TREND_LONG,
):
    _freeze(monkeypatch, now_ms)
    monkeypatch.setattr(
        "hl_bot.btc_regime.adx",
        lambda series, period=14: [float(adx_value)] * len(series),
    )
    cfg = BotConfig()
    cfg.dry_run = True
    cfg.symbols = symbols
    cfg.paper_equity = 10_000.0
    cfg.state_path = str(tmp_path / "paper_state.json")
    cfg.trend.btc_regime_mode = adx_mode
    cfg.trend.btc_bearish_long_mode = bearish_mode
    cfg.trend.block_new_longs_when_net_short = block_net_short
    cfg.trend.stop_cooldown_bars_4h = 0
    runner = BotRunner(cfg)
    runner.client.fetch_asset_contexts = lambda: _contexts(symbols)  # type: ignore[method-assign]
    runner.client.fetch_mids = lambda: {symbol: 100.0 for symbol in symbols}  # type: ignore[method-assign]
    runner.client.load_market = lambda symbol, ctx=None, mid=None: market(symbol, mid=100.0, funding=0.0)  # type: ignore[method-assign]
    fetches: list[tuple[str, str, int]] = []
    series = downtrend(120, interval=DAY) if candles == "down" else uptrend(120, interval=DAY)

    def fetch_candles(symbol, interval, count=120):
        fetches.append((symbol, interval, count))
        return series

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
        lambda *a, **k: decision(regime, daily_adx=30),
    )
    return runner, fetches, executed


def _arm(runner: BotRunner, side: Side, *, strategy: StrategyName = StrategyName.TREND) -> None:
    def generate(market, *a, **k):
        return _signal(market.symbol, side, strategy=strategy)

    if strategy is StrategyName.TREND:
        runner.trend.generate_signal = generate  # type: ignore[method-assign]
    else:
        runner.mr.generate_signal = generate  # type: ignore[method-assign]


def _opens(executed: list[OrderIntent]) -> list[OrderIntent]:
    return [item for item in executed if item.action is IntentAction.OPEN]


def _read_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_signed_notional_and_reason_unit() -> None:
    flat: list[Position] = []
    assert signed_open_notional(flat) == 0.0
    assert net_exposure_blocks_long(flat) is None

    only_long = [_pos("ETH", Side.LONG, 800)]
    assert signed_open_notional(only_long) == pytest.approx(800)
    assert net_exposure_blocks_long(only_long) is None

    shorts = [_pos("ETH", Side.SHORT, 800), _pos("SOL", Side.SHORT, 400)]
    assert signed_open_notional(shorts) == pytest.approx(-1200)
    reason = net_exposure_blocks_long(shorts)
    assert reason is not None
    assert "净空敞口" in reason
    assert "ETH空" in reason and "SOL空" in reason

    net_long = [_pos("ETH", Side.LONG, 1000), _pos("SOL", Side.SHORT, 100)]
    assert signed_open_notional(net_long) == pytest.approx(900)
    assert net_exposure_blocks_long(net_long) is None

    flat_book = [_pos("ETH", Side.LONG, 500), _pos("SOL", Side.SHORT, 500)]
    assert abs(signed_open_notional(flat_book)) < 1e-9
    assert net_exposure_blocks_long(flat_book) is None
    assert net_exposure_blocks_long(shorts, enabled=False) is None


def test_daily_clear_side_matches_regime_and_ignores_hourly_conflict() -> None:
    assert daily_clear_trend_side(120, 100, 28, clear_trend_adx=22.5) is Side.LONG
    assert daily_clear_trend_side(90, 110, 30, clear_trend_adx=22.5) is Side.SHORT
    assert daily_clear_trend_side(90, 110, 22.5, clear_trend_adx=22.5) is Side.SHORT
    assert daily_clear_trend_side(90, 110, 22.49, clear_trend_adx=22.5) is None
    assert daily_clear_trend_side(100, 100, 40, clear_trend_adx=22.5) is None

    routed = decide_from_metrics(
        ema_fast=90,
        ema_slow=110,
        daily_adx=30,
        hourly_adx=27,
        rng=dummy_range(False),
    )
    assert routed.regime is Regime.TREND_SHORT
    conflict = decide_from_metrics(
        ema_fast=90,
        ema_slow=110,
        daily_adx=30,
        hourly_adx=12,
        rng=dummy_range(True),
    )
    assert conflict.regime is Regime.WATCH
    assert conflict.conflict
    # 组合闸只用日线明确空头，1h 冲突不会把「大盘偏空」洗掉。
    assert daily_clear_trend_side(90, 110, 30, clear_trend_adx=22.5) is Side.SHORT


def test_config_defaults_and_switches(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("HL_RISK_PCT", raising=False)
    cfg = BotConfig()
    assert cfg.trend.block_new_longs_when_net_short is True
    assert cfg.trend.btc_bearish_long_mode == "enforce"
    loaded = load_config("config.toml")
    assert loaded.trend.block_new_longs_when_net_short is True
    assert loaded.trend.btc_bearish_long_mode == "enforce"
    assert loaded.trend.btc_regime_mode == "shadow"

    off = tmp_path / "off.toml"
    off.write_text(
        '[trend]\nblock_new_longs_when_net_short = false\nbtc_bearish_long_mode = " OFF "\n',
        encoding="utf-8",
    )
    disabled = load_config(off)
    assert disabled.trend.block_new_longs_when_net_short is False
    assert disabled.trend.btc_bearish_long_mode == "off"

    missing = tmp_path / "missing.toml"
    missing.write_text("[trend]\nrisk_pct = 0.02\n", encoding="utf-8")
    defaults = load_config(missing)
    assert defaults.trend.block_new_longs_when_net_short is True
    assert defaults.trend.btc_bearish_long_mode == "enforce"

    with pytest.raises(ValueError, match="btc_bearish_long_mode"):
        bad = tmp_path / "bad.toml"
        bad.write_text('[trend]\nbtc_bearish_long_mode = "sometimes"\n', encoding="utf-8")
        load_config(bad)


def test_open_shorts_block_new_long_even_if_btc_adx_is_shadow(tmp_path, monkeypatch) -> None:
    """2026-10-31：ETH/SOL 已是趋势空，HYPE 多 starter 不该开。ADX 闸仍是 shadow。"""
    runner, fetches, executed = _runner(
        tmp_path, monkeypatch, symbols=("HYPE",), adx_mode="shadow", bearish_mode="off", adx_value=10.0
    )
    runner.account.positions.extend([_pos("ETH", Side.SHORT, 800), _pos("SOL", Side.SHORT, 400)])
    _arm(runner, Side.LONG)
    report = runner.scan_once()
    assert _opens(executed) == []
    assert report.symbols[0].signal is not None
    assert report.symbols[0].signal.tag == "trend_confirmed_starter"
    assert report.symbols[0].intent is None
    note = " ".join(report.symbols[0].notes)
    assert "组合方向拒绝" in note
    assert "净空敞口" in note
    assert "ETH空" in note and "SOL空" in note
    assert fetches == []
    assert not any("BTC regime" in item for item in report.symbols[0].notes)


def test_flat_or_only_longs_still_allow_new_long(tmp_path, monkeypatch) -> None:
    runner, fetches, executed = _runner(tmp_path, monkeypatch, symbols=("HYPE",))
    _arm(runner, Side.LONG)
    flat = runner.scan_once()
    assert len(_opens(executed)) == 1
    assert flat.symbols[0].intent is not None
    assert flat.symbols[0].intent.action is IntentAction.OPEN
    assert fetches == []

    executed.clear()
    runner.account.positions.append(_pos("ETH", Side.LONG, 900))
    only_longs = runner.scan_once()
    assert len(_opens(executed)) == 1
    assert only_longs.symbols[0].intent is not None
    assert not any("组合方向拒绝" in note for note in only_longs.symbols[0].notes)


def test_net_long_and_equal_notional_do_not_block(tmp_path, monkeypatch) -> None:
    runner, _fetches, executed = _runner(tmp_path, monkeypatch, symbols=("HYPE",))
    runner.account.positions.extend([_pos("ETH", Side.LONG, 1000), _pos("SOL", Side.SHORT, 100)])
    _arm(runner, Side.LONG)
    report = runner.scan_once()
    assert len(_opens(executed)) == 1
    assert report.symbols[0].intent is not None

    executed.clear()
    runner.account.positions.clear()
    runner.account.positions.extend([_pos("ETH", Side.LONG, 500), _pos("SOL", Side.SHORT, 500)])
    tied = runner.scan_once()
    assert len(_opens(executed)) == 1
    assert tied.symbols[0].intent is not None


def test_switch_off_allows_long_against_shorts(tmp_path, monkeypatch) -> None:
    runner, _fetches, executed = _runner(tmp_path, monkeypatch, block_net_short=False)
    runner.account.positions.extend([_pos("ETH", Side.SHORT, 800), _pos("SOL", Side.SHORT, 400)])
    _arm(runner, Side.LONG)
    report = runner.scan_once()
    assert len(_opens(executed)) == 1
    assert report.symbols[0].intent is not None
    assert not any("净空敞口" in note for note in report.symbols[0].notes)


def test_net_short_does_not_block_new_short_or_existing_long_management(tmp_path, monkeypatch) -> None:
    runner, fetches, executed = _runner(tmp_path, monkeypatch, symbols=("HYPE",))
    runner.account.positions.extend([_pos("ETH", Side.SHORT, 800), _pos("SOL", Side.SHORT, 200)])
    _arm(runner, Side.SHORT)
    report = runner.scan_once()
    assert len(_opens(executed)) == 1
    assert report.symbols[0].intent is not None
    assert report.symbols[0].intent.side is Side.SHORT
    assert fetches == []

    executed.clear()
    runner.account.positions.clear()
    runner.cfg.symbols = ("ETH",)
    runner.client.fetch_asset_contexts = lambda: _contexts(("ETH",))  # type: ignore[method-assign]
    runner.client.fetch_mids = lambda: {"ETH": 100.0}  # type: ignore[method-assign]
    runner.account.positions.extend(
        [_pos("ETH", Side.LONG, 100, stop=90.0), _pos("SOL", Side.SHORT, 5_000, stop=100.2)]
    )
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
    runner.trend.generate_exits = lambda *a, **k: [close]  # type: ignore[method-assign]
    closed = runner.scan_once()
    assert [item.action for item in executed] == [IntentAction.CLOSE]
    assert not any("组合方向拒绝" in note for note in closed.symbols[0].notes)

    executed.clear()
    runner.account.positions.clear()
    runner.account.positions.extend(
        [_pos("ETH", Side.LONG, 100, stop=90.0), _pos("SOL", Side.SHORT, 5_000, stop=100.2)]
    )
    runner.trend.generate_exits = lambda *a, **k: []  # type: ignore[method-assign]
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
    added = runner.scan_once()
    assert [item.action for item in executed] == [IntentAction.ADD]
    assert not any("组合方向拒绝" in note or "禁止新开多" in note for note in added.symbols[0].notes)
    assert fetches == []


def test_mean_reversion_long_blocked_only_when_net_short(tmp_path, monkeypatch) -> None:
    runner, _fetches, executed = _runner(
        tmp_path, monkeypatch, regime=Regime.MEAN_REVERSION, symbols=("HYPE",)
    )
    _arm(runner, Side.LONG, strategy=StrategyName.MEAN_REVERSION)
    allowed = runner.scan_once()
    assert len(_opens(executed)) == 1
    assert allowed.symbols[0].intent.strategy is StrategyName.MEAN_REVERSION

    executed.clear()
    runner.account.positions.append(_pos("ETH", Side.SHORT, 300))
    blocked = runner.scan_once()
    assert _opens(executed) == []
    assert blocked.symbols[0].intent is None
    assert any("净空敞口" in note for note in blocked.symbols[0].notes)


def test_btc_bearish_blocks_new_long_and_logs(tmp_path, monkeypatch) -> None:
    runner, fetches, executed = _runner(
        tmp_path,
        monkeypatch,
        candles="down",
        adx_value=30.0,
        bearish_mode="enforce",
        adx_mode="shadow",
    )
    _arm(runner, Side.LONG)
    report = runner.scan_once()
    assert _opens(executed) == []
    assert report.symbols[0].signal is not None
    assert report.symbols[0].intent is None
    assert any("BTC日线偏空，禁止新开多" in note for note in report.symbols[0].notes)
    assert fetches == [("BTC", "1d", BTC_DAILY_BARS)]
    rows = [row for row in _read_jsonl(shadow_log_path(runner.cfg.state_path)) if row.get("gate") == "btc_bearish_long"]
    assert len(rows) == 1
    assert rows[0]["blocked"] is True
    assert rows[0]["symbol"] == "HYPE"
    assert rows[0]["side"] == "long"
    assert rows[0]["ema_fast"] < rows[0]["ema_slow"]
    assert not any("仅记录不拦截" in note for note in report.symbols[0].notes)


def test_btc_not_clearly_bearish_allows_long(tmp_path, monkeypatch) -> None:
    bull, fetches, executed = _runner(
        tmp_path, monkeypatch, candles="up", adx_value=30.0, bearish_mode="enforce"
    )
    _arm(bull, Side.LONG)
    up = bull.scan_once()
    assert len(_opens(executed)) == 1
    assert up.symbols[0].intent is not None
    assert not any("禁止新开多" in note for note in up.symbols[0].notes)
    assert fetches == [("BTC", "1d", BTC_DAILY_BARS)]

    weak, weak_fetches, weak_executed = _runner(
        tmp_path, monkeypatch, candles="down", adx_value=22.49, bearish_mode="enforce"
    )
    _arm(weak, Side.LONG)
    soft = weak.scan_once()
    assert len(_opens(weak_executed)) == 1
    assert soft.symbols[0].intent is not None
    assert weak_fetches == [("BTC", "1d", BTC_DAILY_BARS)]

    edge, _edge_fetches, edge_executed = _runner(
        tmp_path, monkeypatch, candles="down", adx_value=22.5, bearish_mode="enforce"
    )
    _arm(edge, Side.LONG)
    blocked = edge.scan_once()
    assert _opens(edge_executed) == []
    assert any("禁止新开多" in note for note in blocked.symbols[0].notes)


def test_btc_bearish_shadow_and_off_and_fail_open(tmp_path, monkeypatch) -> None:
    shadow, _fetches, executed = _runner(
        tmp_path, monkeypatch, candles="down", adx_value=30.0, bearish_mode="shadow"
    )
    _arm(shadow, Side.LONG)
    report = shadow.scan_once()
    assert len(_opens(executed)) == 1
    assert any("仅记录不拦截" in note for note in report.symbols[0].notes)
    rows = _read_jsonl(shadow_log_path(shadow.cfg.state_path))
    assert rows[-1]["gate"] == "btc_bearish_long"
    assert rows[-1]["would_block"] is True
    assert "blocked" not in rows[-1]

    off, fetches, off_executed = _runner(
        tmp_path, monkeypatch, candles="down", adx_value=30.0, bearish_mode="off"
    )
    _arm(off, Side.LONG)
    opened = off.scan_once()
    assert len(_opens(off_executed)) == 1
    assert opened.symbols[0].intent is not None
    assert fetches == []
    assert not any("BTC日线偏空" in note for note in opened.symbols[0].notes)

    broken, broken_fetches, broken_executed = _runner(
        tmp_path, monkeypatch, candles="down", adx_value=40.0, bearish_mode="enforce"
    )
    _arm(broken, Side.LONG)

    def boom(symbol, interval, count=120):
        broken_fetches.append((symbol, interval, count))
        raise RuntimeError("candle down")

    broken.client.fetch_candles = boom  # type: ignore[method-assign]
    failed = broken.scan_once()
    assert len(_opens(broken_executed)) == 1
    assert failed.symbols[0].intent is not None
    assert broken_fetches == [("BTC", "1d", BTC_DAILY_BARS)]


def test_btc_bearish_does_not_block_shorts_or_existing_long_adds(tmp_path, monkeypatch) -> None:
    runner, fetches, executed = _runner(
        tmp_path, monkeypatch, symbols=("HYPE",), candles="down", adx_value=35.0, bearish_mode="enforce"
    )
    _arm(runner, Side.SHORT)
    short = runner.scan_once()
    assert len(_opens(executed)) == 1
    assert short.symbols[0].intent.side is Side.SHORT
    assert not any("禁止新开多" in note for note in short.symbols[0].notes)
    assert fetches == []

    executed.clear()
    runner.cfg.symbols = ("ETH",)
    runner.client.fetch_asset_contexts = lambda: _contexts(("ETH",))  # type: ignore[method-assign]
    runner.client.fetch_mids = lambda: {"ETH": 100.0}  # type: ignore[method-assign]
    runner.account.positions.append(_pos("ETH", Side.LONG, 100, stop=90.0))
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
    added = runner.scan_once()
    assert [item.action for item in executed] == [IntentAction.ADD]
    assert not any("禁止新开多" in note for note in added.symbols[0].notes)
    assert fetches == []


def test_btc_bearish_blocks_mean_reversion_long(tmp_path, monkeypatch) -> None:
    runner, fetches, executed = _runner(
        tmp_path,
        monkeypatch,
        regime=Regime.MEAN_REVERSION,
        candles="down",
        adx_value=28.0,
        bearish_mode="enforce",
    )
    _arm(runner, Side.LONG, strategy=StrategyName.MEAN_REVERSION)
    report = runner.scan_once()
    assert _opens(executed) == []
    assert report.symbols[0].signal.strategy is StrategyName.MEAN_REVERSION
    assert any("禁止新开多" in note for note in report.symbols[0].notes)
    assert fetches == [("BTC", "1d", BTC_DAILY_BARS)]
