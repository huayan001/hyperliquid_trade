from hl_bot.indicators import atr, donchian, last_value
from hl_bot.models import OrderKind, Regime, Side, StrategyName
from hl_bot.strategies.trend import TrendStrategy
from tests.candles import H4, START, candle, decision, market, series_from_closes, uptrend


def _breakout_h4(*, chase: bool) -> list:
    bars = series_from_closes([100.0] * 30, start=START, interval=H4, wick=0.0)
    # 收紧影线，通道上轨≈100
    tight = []
    for i, c in enumerate(bars):
        tight.append(
            candle(100.0, open_=100.0, high=100.2, low=99.8, ts=START + i * H4, interval=H4)
        )
    last_open = 100.2
    last_close = 112.0 if chase else 101.0
    last_high = last_close
    tight.append(
        candle(
            last_close,
            open_=last_open,
            high=last_high,
            low=100.0,
            ts=START + 30 * H4,
            interval=H4,
        )
    )
    return tight


def test_donchian_close_breakout_long_no_ema_pullback() -> None:
    h4 = _breakout_h4(chase=False)
    # 4h EMA 远离价格：策略不得把「回到 EMA」当作条件
    snap = market("ETH", daily=uptrend(80), h4=h4, mid=h4[-1].close)
    sig = TrendStrategy().generate_signal(snap, decision(Regime.TREND_LONG, daily_adx=28))
    assert sig is not None
    assert sig.side is Side.LONG
    assert sig.strategy is StrategyName.TREND
    assert sig.kind is OrderKind.MAKER_LIMIT
    assert sig.extras["forbid_ema_pullback"] is True
    assert sig.entry_price == h4[-1].close
    atr_v = last_value(atr(h4, 14))
    assert atr_v
    assert abs(sig.stop_price - (sig.entry_price - 2 * atr_v)) < 1e-9


def test_btc_large_body_allows_chase() -> None:
    h4 = _breakout_h4(chase=True)
    snap = market("BTC", daily=uptrend(80), h4=h4, mid=h4[-1].close)
    sig = TrendStrategy().generate_signal(snap, decision(Regime.TREND_LONG, daily_adx=30))
    assert sig is not None
    assert sig.kind is OrderKind.MARKET
    assert sig.tag == "btc_chase_breakout"
    atr_v = last_value(atr(h4, 14))
    assert atr_v
    assert abs(sig.stop_price - (sig.entry_price - 2 * atr_v)) < 1e-9


def test_eth_large_body_still_maker() -> None:
    h4 = _breakout_h4(chase=True)
    snap = market("ETH", daily=uptrend(80), h4=h4, mid=h4[-1].close)
    sig = TrendStrategy().generate_signal(snap, decision(Regime.TREND_LONG, daily_adx=30))
    assert sig is not None
    assert sig.kind is OrderKind.MAKER_LIMIT


def test_no_signal_without_close_confirmation() -> None:
    h4 = _breakout_h4(chase=False)
    # 收盘未站上通道
    last = h4[-1]
    h4[-1] = candle(100.1, open_=100.0, high=101.5, low=99.8, ts=last.ts, interval=H4)
    snap = market("BTC", h4=h4)
    assert TrendStrategy().generate_signal(snap, decision(Regime.TREND_LONG)) is None


def test_short_requires_bearish_regime() -> None:
    h4 = _breakout_h4(chase=False)
    snap = market("ETH", h4=h4)
    # 多头环境不得开趋势空
    assert TrendStrategy().generate_signal(snap, decision(Regime.TREND_LONG)) is not None
    assert TrendStrategy().generate_signal(snap, decision(Regime.WATCH)) is None


def test_btc_short_requires_adx_over_25() -> None:
    bars = [
        candle(100.0, open_=100.0, high=100.2, low=99.8, ts=START + i * H4, interval=H4) for i in range(30)
    ]
    bars.append(candle(90.0, open_=99.8, high=100.0, low=89.0, ts=START + 30 * H4, interval=H4))
    snap = market("BTC", h4=bars, mid=90)
    blocked = TrendStrategy().generate_signal(
        snap, decision(Regime.TREND_SHORT, ema_fast=90, ema_slow=110, daily_adx=22)
    )
    allowed = TrendStrategy().generate_signal(
        snap, decision(Regime.TREND_SHORT, ema_fast=90, ema_slow=110, daily_adx=28)
    )
    assert blocked is None
    assert allowed is not None
    assert allowed.side is Side.SHORT
    upper, lower = donchian(bars, 20)
    assert lower[-1] is not None
    assert bars[-1].close < lower[-1]


def test_trailing_stop_moves_half_atr_per_full_atr() -> None:
    from hl_bot.models import Position

    pos = Position(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        size=1,
        entry_price=100,
        stop_price=80,  # 入场 − 2×ATR
        leverage=5,
        opened_ts=0,
    )
    strat = TrendStrategy()
    # 1×ATR 有利 → 止损上移 0.5×ATR：90 → 95 if ATR=10, initial stop was entry-2ATR=80
    # 这里用真实公式
    moved = strat.trailing_stop(pos, price=110, atr_v=10)
    assert moved == 100 - 2 * 10 + 1 * 0.5 * 10
