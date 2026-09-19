"""环境路由：日线趋势 vs 1h 震荡 vs 中间地带/冲突观望。严格按 v1.1 两份文档第 6 节。"""

from __future__ import annotations

from collections.abc import Sequence

from hl_bot.config import RegimeConfig
from hl_bot.indicators import adx, ema, last_value
from hl_bot.models import Candle, RangeStructure, Regime, RegimeDecision


def detect_range_structure(
    hourly: Sequence[Candle],
    *,
    lookback_hours: int = 12,
    touch_frac: float = 0.20,
) -> RangeStructure:
    """近 8–12 小时在同一区间反复触及上下沿，且最近 K 未创新高新低。"""
    lookback = max(8, min(lookback_hours, 12 if lookback_hours >= 12 else lookback_hours))
    if lookback_hours > 12:
        lookback = lookback_hours
    if len(hourly) < max(8, lookback):
        return RangeStructure(False, 0.0, 0.0, False, False, False, "1h K 线不足，无法确认区间")

    window = list(hourly[-lookback:])
    prior = window[:-2] if len(window) > 4 else window[:-1]
    recent = window[len(prior) :]
    high = max(c.high for c in prior)
    low = min(c.low for c in prior)
    width = high - low
    if width <= 0:
        return RangeStructure(False, high, low, False, False, False, "区间宽度为 0")

    new_extreme = any(c.high > high or c.low < low for c in recent)
    upper_zone = high - touch_frac * width
    lower_zone = low + touch_frac * width
    touched_high = any(c.high >= upper_zone for c in window)
    touched_low = any(c.low <= lower_zone for c in window)

    # 单向趋势推进：收盘几乎都在同一半区，不算区间
    mid = (high + low) / 2.0
    closes = [c.close for c in window]
    above = sum(1 for x in closes if x >= mid)
    one_way = above <= 1 or above >= len(closes) - 1

    is_range = touched_high and touched_low and not new_extreme and not one_way
    if is_range:
        reason = f"近{lookback}h 触及上下沿且未收盘创新高新低"
    elif new_extreme:
        reason = "近端 K 线已突破既有高低点，区间假设失效"
    elif one_way:
        reason = "价格单向运行，缺少来回触及"
    else:
        reason = "未同时触及区间上下沿"
    return RangeStructure(is_range, high, low, touched_high, touched_low, new_extreme, reason)


def decide_from_metrics(
    *,
    ema_fast: float,
    ema_slow: float,
    daily_adx: float,
    hourly_adx: float,
    rng: RangeStructure,
    cfg: RegimeConfig | None = None,
) -> RegimeDecision:
    """
    日线趋势明确（EMA20/50 方向 + ADX > 20~25）→ 趋势
    1h ADX < 20 且区间结构成立 → 均值回归
    中间地带 / 冲突 → 观望
    """
    cfg = cfg or RegimeConfig()
    bullish = ema_fast > ema_slow
    bearish = ema_fast < ema_slow
    ema_aligned = bullish or bearish
    daily_trend_ok = ema_aligned and daily_adx >= cfg.trend_adx_min
    daily_trend_clear = ema_aligned and daily_adx >= cfg.clear_trend_adx
    hourly_range = hourly_adx < cfg.mr_adx_max and rng.is_range

    if daily_trend_ok and hourly_range:
        return RegimeDecision(
            Regime.WATCH,
            ema_fast,
            ema_slow,
            daily_adx,
            hourly_adx,
            rng,
            "日线趋势与 1h 区间同时成立，冲突观望",
            True,
        )

    if daily_trend_clear:
        regime = Regime.TREND_LONG if bullish else Regime.TREND_SHORT
        direction = "多头" if bullish else "空头"
        cmp = ">" if bullish else "<"
        return RegimeDecision(
            regime,
            ema_fast,
            ema_slow,
            daily_adx,
            hourly_adx,
            rng,
            f"日线{direction}趋势明确 EMA20{cmp}EMA50 且 ADX={daily_adx:.1f}>{cfg.clear_trend_adx}",
            False,
        )

    if daily_trend_ok and not daily_trend_clear:
        return RegimeDecision(
            Regime.WATCH,
            ema_fast,
            ema_slow,
            daily_adx,
            hourly_adx,
            rng,
            f"日线 ADX={daily_adx:.1f} 位于 20~{cfg.clear_trend_adx} 中间地带，观望",
            False,
        )

    if hourly_range:
        return RegimeDecision(
            Regime.MEAN_REVERSION,
            ema_fast,
            ema_slow,
            daily_adx,
            hourly_adx,
            rng,
            f"1h ADX={hourly_adx:.1f}<{cfg.mr_adx_max} 且{rng.reason}",
            False,
        )

    return RegimeDecision(
        Regime.WATCH,
        ema_fast,
        ema_slow,
        daily_adx,
        hourly_adx,
        rng,
        f"无明确趋势/震荡：日线ADX={daily_adx:.1f} 1hADX={hourly_adx:.1f} {rng.reason}",
        False,
    )


def route_regime(
    daily: Sequence[Candle],
    hourly: Sequence[Candle],
    cfg: RegimeConfig | None = None,
    *,
    symbol: str = "",
) -> RegimeDecision:
    cfg = cfg or RegimeConfig()
    if len(daily) < 60 or len(hourly) < 30:
        return RegimeDecision(
            Regime.WATCH, None, None, None, None, None, "K 线数量不足，观望", False
        )

    closes = [c.close for c in daily]
    ema_fast = last_value(ema(closes, 20))
    ema_slow = last_value(ema(closes, 50))
    daily_adx = last_value(adx(daily, 14))
    hourly_adx = last_value(adx(hourly, 14))
    rng = detect_range_structure(
        hourly,
        lookback_hours=cfg.range_lookback_hours,
        touch_frac=cfg.range_touch_frac,
    )
    if ema_fast is None or ema_slow is None or daily_adx is None or hourly_adx is None:
        return RegimeDecision(
            Regime.WATCH, ema_fast, ema_slow, daily_adx, hourly_adx, rng, "指标未就绪，观望", False
        )
    return decide_from_metrics(
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        daily_adx=daily_adx,
        hourly_adx=hourly_adx,
        rng=rng,
        cfg=cfg,
    )
