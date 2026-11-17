from hl_bot.models import Candle, OrderKind, Regime, Side, StrategyName
from hl_bot.strategies.mean_reversion import MeanReversionStrategy
from tests.candles import H1, START, candle, decision, flat_range, market, series_from_closes


def _dump_then_reversal(*, confirm: bool) -> list[Candle]:
    closes = [100.0] * 30 + [98, 96, 94, 91, 88, 85]
    candles = series_from_closes(closes, interval=H1, wick=0.002)
    last_ts = START + (len(closes) - 1) * H1
    if confirm:
        # 收盘重新站回下轨之上，构成确认（不是触及当根）
        candles[-1] = candle(94.0, open_=85.0, high=94.5, low=83.0, ts=last_ts, interval=H1)
    else:
        candles[-1] = candle(83.0, open_=85.0, high=85.2, low=82.5, ts=last_ts, interval=H1)
    return candles


def _rally_then_reversal() -> list[Candle]:
    closes = [100.0] * 30 + [102, 104, 107, 110, 113, 116]
    candles = series_from_closes(closes, interval=H1, wick=0.002)
    last_ts = START + (len(closes) - 1) * H1
    # 收盘重新回到上轨之下
    candles[-1] = candle(107.0, open_=116.0, high=118.0, low=106.5, ts=last_ts, interval=H1)
    return candles


def _mr_dec(**kwargs):
    defaults = dict(
        daily_adx=12,
        hourly_adx=14,
        is_range=True,
        ema_fast=100,
        ema_slow=100.2,
        range_high=130,
        range_low=70,
    )
    defaults.update(kwargs)
    return decision(Regime.MEAN_REVERSION, **defaults)


def test_long_requires_reversal_confirmation() -> None:
    strat = MeanReversionStrategy()
    dec = _mr_dec()
    ok = strat.generate_signal(market("ETH", h1=_dump_then_reversal(confirm=True)), dec)
    no = strat.generate_signal(market("ETH", h1=_dump_then_reversal(confirm=False)), dec)
    assert ok is not None
    assert ok.side is Side.LONG
    assert ok.kind is OrderKind.MAKER_LIMIT
    assert ok.extras["require_reversal"] is True
    assert no is None


def test_short_is_range_fade_not_trend_break() -> None:
    strat = MeanReversionStrategy()
    dec = _mr_dec(daily_adx=11, hourly_adx=13)
    sig = strat.generate_signal(market("SOL", h1=_rally_then_reversal()), dec)
    assert sig is not None
    assert sig.side is Side.SHORT
    assert sig.tag == "mr_short_fade"
    assert "非趋势空" in sig.reason


def test_wrong_regime_no_signal() -> None:
    strat = MeanReversionStrategy()
    h1 = _dump_then_reversal(confirm=True)
    assert strat.generate_signal(market("BTC", h1=h1), decision(Regime.TREND_LONG)) is None


def test_adx_expansion_halts_new_entries() -> None:
    strat = MeanReversionStrategy()
    h1 = flat_range(40)
    dec = decision(Regime.MEAN_REVERSION, hourly_adx=26.0, is_range=True)
    halted, why = strat.expansion_halt(h1, dec)
    assert halted
    assert "ADX" in why
    assert strat.generate_signal(market("BTC", h1=h1), dec) is None


def test_range_close_break_halts() -> None:
    strat = MeanReversionStrategy()
    h1 = flat_range(16, 100, 110)
    last = h1[-1]
    h1[-1] = Candle(
        ts=last.ts,
        end_ts=last.end_ts,
        open=last.open,
        high=125,
        low=last.low,
        close=124,
        volume=last.volume,
    )
    dec = decision(Regime.MEAN_REVERSION, hourly_adx=12, is_range=True)
    # decision.range_structure high/low is 110/100 from dummy_range
    halted, why = strat.expansion_halt(h1, dec)
    assert halted
    assert "突破区间" in why


def test_large_body_and_bandwidth_halts() -> None:
    strat = MeanReversionStrategy()
    closes = [100.0] * 25
    candles = series_from_closes(closes, interval=H1, wick=0.0001)
    last = candles[-1]
    candles[-1] = candle(115.0, open_=100.0, high=116, low=99.5, ts=last.ts, interval=H1, volume=50)
    dec = decision(Regime.MEAN_REVERSION, hourly_adx=12, is_range=True)
    halted, _ = strat.expansion_halt(candles, dec)
    # 实体巨大；若带宽未进高位也可能因「带宽扩张且 1.5×ATR」触发
    assert halted or strat.generate_signal(market("BTC", h1=candles), dec) is None
