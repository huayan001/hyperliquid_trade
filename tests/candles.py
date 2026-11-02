from __future__ import annotations

from hl_bot.models import Candle, FundingInfo, MarketSnapshot, RangeStructure, Regime, RegimeDecision

DAY = 86_400_000
H4 = 14_400_000
H1 = 3_600_000
START = 1_700_000_000_000


def candle(
    close: float,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    ts: int = START,
    interval: int = H1,
    volume: float = 10.0,
) -> Candle:
    o = close if open_ is None else open_
    hi = max(o, close) if high is None else high
    lo = min(o, close) if low is None else low
    return Candle(ts=ts, end_ts=ts + interval - 1, open=o, high=hi, low=lo, close=close, volume=volume)


def series_from_closes(
    closes: list[float],
    *,
    start: int = START,
    interval: int = H1,
    wick: float = 0.001,
    volume: float = 10.0,
) -> list[Candle]:
    out: list[Candle] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) * (1 + wick)
        lo = min(o, c) * (1 - wick)
        out.append(
            candle(c, open_=o, high=hi, low=lo, ts=start + i * interval, interval=interval, volume=volume)
        )
        prev = c
    return out


def flat_range(n: int, lo: float = 100.0, hi: float = 110.0, start: int = START, interval: int = H1) -> list[Candle]:
    closes: list[float] = []
    for i in range(n):
        cycle = i % 10
        if cycle <= 5:
            closes.append(lo + (hi - lo) * cycle / 5)
        else:
            closes.append(hi - (hi - lo) * (cycle - 5) / 5)
    candles = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        # 保证上下沿被影线触及
        high = max(o, c, hi if c > (lo + hi) / 2 else c)
        low = min(o, c, lo if c < (lo + hi) / 2 else c)
        candles.append(candle(c, open_=o, high=high, low=low, ts=start + i * interval, interval=interval))
        prev = c
    return candles


def uptrend(n: int, start_px: float = 100.0, step: float = 1.5, interval: int = DAY) -> list[Candle]:
    closes = [start_px + i * step for i in range(n)]
    return series_from_closes(closes, interval=interval, wick=0.002)


def downtrend(n: int, start_px: float = 200.0, step: float = 1.5, interval: int = DAY) -> list[Candle]:
    closes = [start_px - i * step for i in range(n)]
    return series_from_closes(closes, interval=interval, wick=0.002)


def dummy_range(is_range: bool = False, high: float = 110.0, low: float = 100.0) -> RangeStructure:
    return RangeStructure(is_range, high, low, is_range, is_range, False, "fixture")


def decision(
    regime: Regime,
    *,
    ema_fast: float = 120.0,
    ema_slow: float = 100.0,
    daily_adx: float = 30.0,
    hourly_adx: float = 28.0,
    is_range: bool = False,
    range_high: float = 110.0,
    range_low: float = 100.0,
) -> RegimeDecision:
    return RegimeDecision(
        regime,
        ema_fast,
        ema_slow,
        daily_adx,
        hourly_adx,
        dummy_range(is_range, range_high, range_low),
        "fixture",
        False,
    )


def market(
    symbol: str,
    *,
    daily: list[Candle] | None = None,
    h4: list[Candle] | None = None,
    h1: list[Candle] | None = None,
    mid: float = 100.0,
    funding: float = 0.0,
    max_leverage: int = 20,
) -> MarketSnapshot:
    daily = daily or uptrend(80)
    h4 = h4 or series_from_closes([100.0] * 40, interval=H4)
    h1 = h1 or flat_range(80)
    return MarketSnapshot(
        symbol=symbol,
        mid=mid,
        daily=tuple(daily),
        h4=tuple(h4),
        h1=tuple(h1),
        funding=FundingInfo(funding, funding),
        max_leverage=max_leverage,
    )
