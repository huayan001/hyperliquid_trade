from hl_bot.indicators import (
    atr,
    body_exceeds_atr,
    bollinger,
    donchian,
    ema,
    last_value,
    rsi,
    sma,
    vwap,
)
from hl_bot.models import Candle
from tests.candles import candle, series_from_closes


def test_sma_known_window() -> None:
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_seeded_from_sma() -> None:
    # period=3, seed=2, k=0.5 → 3, 4
    assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_donchian_excludes_current_bar() -> None:
    candles = [
        candle(10, high=10, low=9, ts=1, interval=10),
        candle(12, high=12, low=10, ts=11, interval=10),
        candle(11, high=11, low=8, ts=21, interval=10),
        candle(20, high=20, low=11, ts=31, interval=10),
    ]
    upper, lower = donchian(candles, 2, exclude_current=True)
    assert upper[-1] == 12  # max high of previous two (12 and 11), not 20
    assert lower[-1] == 8


def test_bollinger_mid_is_sma() -> None:
    values = [i * 1.0 for i in range(1, 31)]
    upper, mid, lower = bollinger(values, 20, 2.0)
    s = sma(values, 20)
    assert mid[-1] == s[-1]
    assert upper[-1] is not None and lower[-1] is not None and mid[-1] is not None
    assert upper[-1] > mid[-1] > lower[-1]


def test_rsi_monotone_up_is_maxed() -> None:
    values = [float(i) for i in range(1, 40)]
    series = rsi(values, 14)
    assert last_value(series) == 100.0


def test_atr_constant_range() -> None:
    candles = [
        Candle(ts=i * 10, end_ts=i * 10 + 9, open=10, high=12, low=8, close=10, volume=1)
        for i in range(20)
    ]
    series = atr(candles, 5)
    assert last_value(series) == 4.0


def test_body_exceeds_atr() -> None:
    c = candle(110, open_=100, high=111, low=99)
    assert c.body == 10
    assert body_exceeds_atr(c, 6.0, 1.5)
    assert not body_exceeds_atr(c, 8.0, 1.5)


def test_vwap_resets_by_utc_day() -> None:
    day = 86_400_000
    first = [
        Candle(ts=0, end_ts=3_599_999, open=10, high=10, low=10, close=10, volume=1),
        Candle(ts=3_600_000, end_ts=7_199_999, open=20, high=20, low=20, close=20, volume=1),
    ]
    assert last_value(vwap(first)) == 15.0
    crossed = first + [
        Candle(ts=day, end_ts=day + 3_599_999, open=40, high=40, low=40, close=40, volume=2),
    ]
    assert last_value(vwap(crossed)) == 40.0


def test_series_helper_length() -> None:
    assert len(series_from_closes([1, 2, 3])) == 3
