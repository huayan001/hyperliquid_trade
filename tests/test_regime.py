from hl_bot.models import Regime
from hl_bot.regime import decide_from_metrics, detect_range_structure, route_regime
from tests.candles import dummy_range, flat_range, uptrend


def test_range_structure_oscillation() -> None:
    hourly = flat_range(16, 100, 110)
    info = detect_range_structure(hourly, lookback_hours=12)
    assert info.is_range
    assert info.touched_high and info.touched_low
    assert not info.new_extreme


def test_range_structure_breakout_is_not_range() -> None:
    hourly = flat_range(12, 100, 110)
    last = hourly[-1]
    hourly[-1] = type(last)(
        ts=last.ts,
        end_ts=last.end_ts,
        open=last.open,
        high=130,
        low=last.low,
        close=128,
        volume=last.volume,
    )
    info = detect_range_structure(hourly, lookback_hours=12)
    assert not info.is_range
    assert info.new_extreme


def test_clear_daily_trend_routes_to_trend_long() -> None:
    d = decide_from_metrics(
        ema_fast=120,
        ema_slow=100,
        daily_adx=28,
        hourly_adx=26,
        rng=dummy_range(False),
    )
    assert d.regime is Regime.TREND_LONG
    assert not d.conflict


def test_clear_daily_bear_routes_to_trend_short() -> None:
    d = decide_from_metrics(
        ema_fast=90,
        ema_slow=110,
        daily_adx=30,
        hourly_adx=27,
        rng=dummy_range(False),
    )
    assert d.regime is Regime.TREND_SHORT


def test_hourly_range_routes_to_mean_reversion() -> None:
    d = decide_from_metrics(
        ema_fast=101,
        ema_slow=100,
        daily_adx=12,
        hourly_adx=14,
        rng=dummy_range(True),
    )
    assert d.regime is Regime.MEAN_REVERSION


def test_conflict_daily_trend_and_hourly_range_is_watch() -> None:
    d = decide_from_metrics(
        ema_fast=120,
        ema_slow=100,
        daily_adx=26,
        hourly_adx=15,
        rng=dummy_range(True),
    )
    assert d.regime is Regime.WATCH
    assert d.conflict


def test_middle_adx_band_is_watch() -> None:
    d = decide_from_metrics(
        ema_fast=120,
        ema_slow=100,
        daily_adx=21.0,
        hourly_adx=22.0,
        rng=dummy_range(False),
    )
    assert d.regime is Regime.WATCH
    assert "中间地带" in d.reason


def test_route_regime_on_synthetic_uptrend() -> None:
    daily = uptrend(80, 100, 2.0)
    # 1h 也走强，避免被识别成区间
    hourly = uptrend(80, 100, 0.8, interval=3_600_000)
    decision = route_regime(daily, hourly)
    assert decision.regime is Regime.TREND_LONG
    assert decision.daily_adx is not None and decision.daily_adx > 22.5
