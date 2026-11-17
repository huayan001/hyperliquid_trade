"""技术指标。实现与 v1.1 文档参数对齐：EMA / ATR / ADX / Donchian / BB / RSI / VWAP。"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timezone

from hl_bot.models import Candle

EPS = 1e-12


def _require_period(period: int) -> None:
    if period <= 0:
        raise ValueError(f"period must be > 0, got {period}")


def last_value(values: Sequence[float | None]) -> float | None:
    for item in reversed(values):
        if item is not None and not math.isnan(item):
            return float(item)
    return None


def sma(values: Sequence[float], period: int) -> list[float | None]:
    _require_period(period)
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """EMA，首值用 SMA 播种。"""
    _require_period(period)
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def stdev(values: Sequence[float], period: int, *, population: bool = True) -> list[float | None]:
    """滚动标准差。布林带默认用总体标准差。"""
    _require_period(period)
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    denom = period if population else max(period - 1, 1)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / denom
        out[i] = math.sqrt(max(var, 0.0))
    return out


def true_range(candles: Sequence[Candle]) -> list[float]:
    trs: list[float] = []
    prev_close: float | None = None
    for c in candles:
        high_low = c.high - c.low
        if prev_close is None:
            trs.append(high_low)
        else:
            trs.append(max(high_low, abs(c.high - prev_close), abs(c.low - prev_close)))
        prev_close = c.close
    return trs


def _wilder_smooth(values: Sequence[float], period: int) -> list[float | None]:
    _require_period(period)
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period])
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = prev - (prev / period) + values[i]
        out[i] = prev
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    """Wilder ATR(period)。"""
    trs = true_range(candles)
    smoothed = _wilder_smooth(trs, period)
    out: list[float | None] = [None] * len(candles)
    for i, value in enumerate(smoothed):
        if value is not None:
            out[i] = value / period
    return out


def adx(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    """Wilder ADX(period)。前 period*2 根之前为 None。"""
    n = len(candles)
    out: list[float | None] = [None] * n
    if n < period * 2:
        return out

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    trs = true_range(candles)
    for i in range(1, n):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        if up > down and up > 0:
            plus_dm[i] = up
        if down > up and down > 0:
            minus_dm[i] = down

    # 与 ATR 一致：对 TR / +DM / -DM 做 Wilder 求和平滑
    sm_tr = _wilder_smooth(trs, period)
    sm_plus = _wilder_smooth(plus_dm, period)
    sm_minus = _wilder_smooth(minus_dm, period)

    dx: list[float | None] = [None] * n
    for i in range(n):
        tr_i, p_i, m_i = sm_tr[i], sm_plus[i], sm_minus[i]
        if tr_i is None or p_i is None or m_i is None or tr_i <= EPS:
            continue
        plus_di = 100.0 * p_i / tr_i
        minus_di = 100.0 * m_i / tr_i
        denom = plus_di + minus_di
        if denom <= EPS:
            dx[i] = 0.0
        else:
            dx[i] = 100.0 * abs(plus_di - minus_di) / denom

    # ADX 是 DX 的 Wilder 平均；第一个有效 DX 在 period 处
    first = period * 2 - 1
    if first >= n:
        return out
    window = [d for d in dx[period : first + 1] if d is not None]
    if len(window) < period:
        return out
    prev = sum(window) / period
    out[first] = prev
    for i in range(first + 1, n):
        if dx[i] is None:
            continue
        prev = (prev * (period - 1) + dx[i]) / period
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder RSI。"""
    _require_period(period)
    n = len(values)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains[i] = delta
        else:
            losses[i] = -delta
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    out[period] = _rsi_from_avg(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from_avg(avg_gain, avg_loss)
    return out


def _rsi_from_avg(avg_gain: float, avg_loss: float) -> float:
    if avg_loss <= EPS:
        return 100.0 if avg_gain > EPS else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def donchian(
    candles: Sequence[Candle],
    period: int = 20,
    *,
    exclude_current: bool = True,
) -> tuple[list[float | None], list[float | None]]:
    """Donchian 通道。默认用「当前 K 之前」的 period 根，避免突破当根把自己算进通道。"""
    _require_period(period)
    n = len(candles)
    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    shift = 1 if exclude_current else 0
    for i in range(n):
        end = i - shift + 1
        start = end - period
        if start < 0 or end <= 0:
            continue
        window = candles[start:end]
        upper[i] = max(c.high for c in window)
        lower[i] = min(c.low for c in window)
    return upper, lower


def bollinger(
    values: Sequence[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = sma(values, period)
    dev = stdev(values, period, population=True)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    for i, (m, d) in enumerate(zip(mid, dev)):
        if m is None or d is None:
            continue
        upper[i] = m + num_std * d
        lower[i] = m - num_std * d
    return upper, mid, lower


def bandwidth(upper: Sequence[float | None], lower: Sequence[float | None], mid: Sequence[float | None]) -> list[float | None]:
    out: list[float | None] = [None] * len(mid)
    for i, (u, l, m) in enumerate(zip(upper, lower, mid)):
        if u is None or l is None or m is None or abs(m) <= EPS:
            continue
        out[i] = (u - l) / abs(m)
    return out


def session_start_utc_ms(ts_ms: int) -> int:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000)


def vwap(candles: Sequence[Candle], *, session_ts_ms: int | None = None) -> list[float | None]:
    """按 UTC 自然日重置的成交量加权均价。"""
    out: list[float | None] = [None] * len(candles)
    cum_pv = 0.0
    cum_v = 0.0
    current_session: int | None = None
    for i, c in enumerate(candles):
        sess = session_ts_ms if session_ts_ms is not None else session_start_utc_ms(c.ts)
        if current_session != sess:
            current_session = sess
            cum_pv = 0.0
            cum_v = 0.0
        cum_pv += c.typical_price * c.volume
        cum_v += c.volume
        if cum_v > EPS:
            out[i] = cum_pv / cum_v
        elif i > 0 and out[i - 1] is not None and session_start_utc_ms(candles[i - 1].ts) == sess:
            out[i] = out[i - 1]
    return out


def closed_only(candles: Sequence[Candle], now_ms: int | None = None) -> list[Candle]:
    if now_ms is None:
        return list(candles)
    return [c for c in candles if c.end_ts < now_ms]


def body_exceeds_atr(candle: Candle, atr_value: float, multiple: float) -> bool:
    if atr_value <= EPS:
        return False
    return candle.body > multiple * atr_value
