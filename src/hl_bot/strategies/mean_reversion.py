"""均值回归日内短线（v1.1）：BB+RSI+VWAP，必须反转确认；急涨停开并提前离场。"""

from __future__ import annotations

from collections.abc import Sequence

from hl_bot.config import MeanReversionConfig
from hl_bot.indicators import (
    adx,
    atr,
    bandwidth,
    body_exceeds_atr,
    bollinger,
    last_value,
    rsi,
    vwap,
)
from hl_bot.models import (
    Candle,
    MarketSnapshot,
    OrderIntent,
    OrderKind,
    Position,
    Regime,
    RegimeDecision,
    Side,
    Signal,
    StrategyName,
)
from hl_bot.strategies.base import Strategy, last_closed, make_close_intent


class MeanReversionStrategy(Strategy):
    name = StrategyName.MEAN_REVERSION.value

    def __init__(self, cfg: MeanReversionConfig | None = None) -> None:
        self.cfg = cfg or MeanReversionConfig()

    def expansion_halt(
        self,
        hourly: Sequence[Candle],
        decision: RegimeDecision,
    ) -> tuple[bool, str]:
        """ADX 抬头 / 区间收盘突破 / 带宽高位且实体>1.5×ATR → 停止新开仓。"""
        if not hourly:
            return True, "无 1h 数据"
        last = hourly[-1]
        hourly_adx = decision.hourly_adx if decision.hourly_adx is not None else last_value(adx(hourly, self.cfg.adx_period))
        if hourly_adx is not None and hourly_adx > self.cfg.adx_kill:
            return True, f"1h ADX={hourly_adx:.1f}>{self.cfg.adx_kill}，停止均值回归新开仓"

        rng = decision.range_structure
        if rng is not None and rng.high > rng.low:
            if last.close > rng.high or last.close < rng.low:
                return True, "价格收盘突破区间边界，停止新开仓"

        closes = [c.close for c in hourly]
        upper, mid, lower = bollinger(closes, self.cfg.bb_period, self.cfg.bb_std)
        bw = bandwidth(upper, lower, mid)
        atr_s = atr(hourly, self.cfg.atr_period)
        atr_v = last_value(atr_s)
        bw_v = last_value(bw)
        recent = [x for x in bw[-20:] if x is not None]
        last_u = last_value(upper)
        last_l = last_value(lower)
        # 收盘仍在带外/区间外的大实体才视为急涨急跌「启动」；
        # 收回带内的反转确认 K 即使实体较大也不停用（否则永远等不到确认）。
        away = False
        if last_u is not None and last.close > last_u:
            away = True
        if last_l is not None and last.close < last_l:
            away = True
        if rng is not None and rng.high > rng.low and (last.close > rng.high or last.close < rng.low):
            away = True
        if away and atr_v and body_exceeds_atr(last, atr_v, self.cfg.chase_body_atr) and bw_v is not None and recent:
            ranked = sorted(recent)
            idx = min(len(ranked) - 1, int(len(ranked) * self.cfg.bandwidth_high_rank))
            high_zone = ranked[idx]
            if bw_v >= high_zone or bw_v >= max(recent) * 0.9:
                return True, "布林带带宽进入近20期高位且K实体>1.5×ATR，急涨/急跌启动，停用"
        return False, ""

    def generate_signal(
        self,
        market: MarketSnapshot,
        decision: RegimeDecision,
        now_ms: int | None = None,
    ) -> Signal | None:
        if decision.regime is not Regime.MEAN_REVERSION:
            return None

        h1 = last_closed(market.h1, now_ms)
        if len(h1) < max(self.cfg.bb_period, self.cfg.rsi_period) + 5:
            return None

        halted, why = self.expansion_halt(h1, decision)
        if halted:
            return None

        closes = [c.close for c in h1]
        upper, mid, lower = bollinger(closes, self.cfg.bb_period, self.cfg.bb_std)
        rsi_s = rsi(closes, self.cfg.rsi_period)
        atr_s = atr(h1, self.cfg.atr_period)
        vwap_s = vwap(h1)
        last = h1[-1]
        prev = h1[-2]
        last_i = len(h1) - 1
        prev_i = last_i - 1

        u, m, l = upper[last_i], mid[last_i], lower[last_i]
        rsi_last = rsi_s[last_i]
        rsi_prev = rsi_s[prev_i]
        atr_v = last_value(atr_s)
        if u is None or m is None or l is None or atr_v is None or rsi_last is None:
            return None

        # 做多：前一根触及/跌破下轨且 RSI<30，本根反转确认（不在触及当根入场）
        touch_low = prev.low <= (lower[prev_i] if lower[prev_i] is not None else l) or prev.close <= (
            lower[prev_i] if lower[prev_i] is not None else l
        )
        oversold = (rsi_prev is not None and rsi_prev < self.cfg.rsi_oversold) or rsi_last < self.cfg.rsi_oversold
        confirm_long = last.close > l or last.is_hammer()
        if touch_low and oversold and confirm_long:
            entry = min(l, last.low) if last.low > l else l
            # Maker：挂在下轨附近或确认K低点上方一点
            entry = max(l, last.low) * 1.0001
            stop = self._stop_price(Side.LONG, entry, atr_v, decision, last)
            return Signal(
                symbol=market.symbol,
                strategy=StrategyName.MEAN_REVERSION,
                side=Side.LONG,
                kind=OrderKind.MAKER_LIMIT,
                entry_price=entry,
                stop_price=stop,
                reason="震荡做多：触及布林下轨 + RSI 超卖，且出现反转确认K（不接刀）",
                tag="mr_long_reversal",
                extras={
                    "bb_lower": l,
                    "bb_mid": m,
                    "rsi": rsi_last,
                    "vwap": last_value(vwap_s),
                    "atr": atr_v,
                    "require_reversal": True,
                },
            )

        # 做空：仅作为区间上沿均值回归，不是趋势破位空
        touch_high = prev.high >= (upper[prev_i] if upper[prev_i] is not None else u) or prev.close >= (
            upper[prev_i] if upper[prev_i] is not None else u
        )
        overbought = (rsi_prev is not None and rsi_prev > self.cfg.rsi_overbought) or rsi_last > self.cfg.rsi_overbought
        confirm_short = last.close < u or last.is_shooting_star()
        if touch_high and overbought and confirm_short:
            entry = min(u, last.high) * 0.9999
            stop = self._stop_price(Side.SHORT, entry, atr_v, decision, last)
            return Signal(
                symbol=market.symbol,
                strategy=StrategyName.MEAN_REVERSION,
                side=Side.SHORT,
                kind=OrderKind.MAKER_LIMIT,
                entry_price=entry,
                stop_price=stop,
                reason="震荡做空：区间上沿超买消退（布林上轨 + RSI>70 + 反转确认），非趋势空",
                tag="mr_short_fade",
                extras={
                    "bb_upper": u,
                    "bb_mid": m,
                    "rsi": rsi_last,
                    "vwap": last_value(vwap_s),
                    "atr": atr_v,
                    "require_reversal": True,
                },
            )
        return None

    def _stop_price(
        self,
        side: Side,
        entry: float,
        atr_v: float,
        decision: RegimeDecision,
        last: Candle,
    ) -> float:
        """止损取「区间外极值」与 1×ATR 中更保守（更近、更小风险）者。"""
        atr_stop = entry - self.cfg.stop_atr * atr_v if side is Side.LONG else entry + self.cfg.stop_atr * atr_v
        rng = decision.range_structure
        if rng is not None and rng.high > rng.low:
            extreme = rng.low if side is Side.LONG else rng.high
            # 再略外侧一点，避免刚好扫到区间沿
            buffer = (rng.high - rng.low) * 0.02
            extreme_stop = extreme - buffer if side is Side.LONG else extreme + buffer
            # 更保守 = 止损距离更小
            if side is Side.LONG:
                return max(atr_stop, extreme_stop)
            return min(atr_stop, extreme_stop)
        return atr_stop

    def generate_exits(
        self,
        position: Position,
        market: MarketSnapshot,
        decision: RegimeDecision,
        now_ms: int,
    ) -> list[OrderIntent]:
        if position.strategy is not StrategyName.MEAN_REVERSION:
            return []
        h1 = last_closed(market.h1, now_ms) or list(market.h1)
        if not h1:
            return []
        price = market.mid or h1[-1].close
        last = h1[-1]
        closes = [c.close for c in h1]
        upper, mid, lower = bollinger(closes, self.cfg.bb_period, self.cfg.bb_std)
        rsi_s = rsi(closes, self.cfg.rsi_period)
        vwap_s = vwap(h1)
        atr_v = last_value(atr(h1, self.cfg.atr_period))
        mid_v = last_value(mid)
        vwap_v = last_value(vwap_s)
        rsi_v = last_value(rsi_s)

        hit_stop = (
            price <= position.stop_price if position.side is Side.LONG else price >= position.stop_price
        )
        if hit_stop:
            return [make_close_intent(position, price, "均值回归硬止损，区间假设失效")]

        halted, why = self.expansion_halt(h1, decision)
        if halted or (decision.hourly_adx is not None and decision.hourly_adx > self.cfg.adx_kill):
            return [make_close_intent(position, price, f"环境破坏提前离场：{why or 'ADX/带宽扩张'}")]

        if atr_v and body_exceeds_atr(last, atr_v, self.cfg.chase_body_atr):
            return [make_close_intent(position, price, "出现 1.5×ATR 实体突破K，优先主动平仓")]

        hours_held = (now_ms - position.opened_ts) / 3_600_000
        if hours_held >= self.cfg.time_stop_hours:
            return [make_close_intent(position, price, f"时间止损：持仓 {hours_held:.1f}h ≥ {self.cfg.time_stop_hours}h")]

        mean = vwap_v if vwap_v is not None else mid_v
        intents: list[OrderIntent] = []
        if mean is not None and position.remaining_frac > 0.6:
            reached_mean = (
                price >= mean if position.side is Side.LONG else price <= mean
            )
            if reached_mean:
                qty = position.size * self.cfg.partial_tp_frac
                intents.append(
                    make_close_intent(
                        position,
                        price,
                        "回归 VWAP/中轨，先平 50% 锁定利润",
                        size=qty,
                        kind_market=False,
                    )
                )
                return intents

        # 目标 2：对侧边界或 RSI 对侧极值
        opp = last_value(upper) if position.side is Side.LONG else last_value(lower)
        rsi_extreme = False
        if rsi_v is not None:
            rsi_extreme = (
                rsi_v >= self.cfg.rsi_overbought
                if position.side is Side.LONG
                else rsi_v <= self.cfg.rsi_oversold
            )
        hit_opp = opp is not None and (
            price >= opp if position.side is Side.LONG else price <= opp
        )
        if hit_opp or rsi_extreme:
            return [make_close_intent(position, price, "触及对侧边界或 RSI 对侧极值，全部离场")]
        return intents
