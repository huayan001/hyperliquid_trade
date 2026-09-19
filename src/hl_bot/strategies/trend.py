"""趋势跟踪波段（v1.1）：4h Donchian 收盘突破，禁止等 EMA 回撤；BTC 急涨可追。"""

from __future__ import annotations

from hl_bot.config import TrendConfig
from hl_bot.indicators import adx, atr, body_exceeds_atr, donchian, ema, last_value
from hl_bot.models import (
    IntentAction,
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


class TrendStrategy(Strategy):
    name = StrategyName.TREND.value

    def __init__(self, cfg: TrendConfig | None = None) -> None:
        self.cfg = cfg or TrendConfig()

    def generate_signal(
        self,
        market: MarketSnapshot,
        decision: RegimeDecision,
        now_ms: int | None = None,
    ) -> Signal | None:
        if not decision.regime.is_trend():
            return None

        h4 = last_closed(market.h4, now_ms)
        daily = last_closed(market.daily, now_ms)
        need = max(self.cfg.donchian_period + 2, self.cfg.atr_period + 2)
        if len(h4) < need or len(daily) < self.cfg.ema_slow + 2:
            return None

        atr_s = atr(h4, self.cfg.atr_period)
        atr_v = last_value(atr_s)
        if atr_v is None or atr_v <= 0:
            return None

        uppers, lowers = donchian(h4, self.cfg.donchian_period, exclude_current=True)
        last = h4[-1]
        upper = uppers[-1]
        lower = lowers[-1]
        if upper is None or lower is None:
            return None

        daily_adx = decision.daily_adx if decision.daily_adx is not None else last_value(adx(daily, self.cfg.adx_period))
        side = decision.regime.trend_side()
        if side is None:
            return None

        # v1.1：入场只看收盘突破，明确不要求价格回到 EMA。
        if side is Side.LONG:
            if decision.regime is not Regime.TREND_LONG:
                return None
            if last.close <= upper:
                return None
            return self._build_signal(market, last, atr_v, Side.LONG, upper, daily_adx)

        # 做空：仅空头环境；BTC 额外要求 ADX>25
        if decision.regime is not Regime.TREND_SHORT:
            return None
        if market.symbol.upper() == "BTC":
            if daily_adx is None or daily_adx <= self.cfg.btc_short_adx_min:
                return None
        if last.close >= lower:
            return None
        return self._build_signal(market, last, atr_v, Side.SHORT, lower, daily_adx)

    def _build_signal(
        self,
        market: MarketSnapshot,
        last,
        atr_v: float,
        side: Side,
        band: float,
        daily_adx: float | None,
    ) -> Signal:
        chase = (
            market.symbol.upper() in {s.upper() for s in self.cfg.chase_symbols}
            and body_exceeds_atr(last, atr_v, self.cfg.chase_body_atr)
        )
        if chase:
            kind = OrderKind.MARKET
            entry = last.close
            tag = "btc_chase_breakout"
            how = f"突破K实体{last.body:.4g}> {self.cfg.chase_body_atr}×ATR，允许市价/紧限价追突破"
        else:
            kind = OrderKind.MAKER_LIMIT
            entry = last.close
            tag = "donchian_close_breakout"
            how = "收盘突破 Donchian20，Maker 限价；不等 EMA 回撤"

        if side is Side.LONG:
            stop = entry - self.cfg.stop_atr * atr_v
            direction = "做多"
        else:
            stop = entry + self.cfg.stop_atr * atr_v
            direction = "做空"

        reason = (
            f"趋势{direction}：4h Donchian{self.cfg.donchian_period} 收盘确认"
            f"（收盘{last.close:.6g} vs 通道{'上' if side is Side.LONG else '下'}轨{band:.6g}）。{how}"
        )
        return Signal(
            symbol=market.symbol,
            strategy=StrategyName.TREND,
            side=side,
            kind=kind,
            entry_price=entry,
            stop_price=stop,
            reason=reason,
            tag=tag,
            extras={
                "atr": atr_v,
                "donchian_band": band,
                "chase": chase,
                "daily_adx": daily_adx,
                "forbid_ema_pullback": True,
            },
        )

    def trailing_stop(self, position: Position, price: float, atr_v: float) -> float:
        """价格每向有利方向走 1×ATR，止损沿趋势移动 0.5×ATR。"""
        if atr_v <= 0:
            return position.stop_price
        if position.side is Side.LONG:
            favorable = max(0.0, price - position.entry_price)
            steps = int(favorable / (self.cfg.trail_step_atr * atr_v))
            candidate = (position.entry_price - self.cfg.stop_atr * atr_v) + steps * self.cfg.trail_offset_atr * atr_v
            return max(position.stop_price, candidate)
        favorable = max(0.0, position.entry_price - price)
        steps = int(favorable / (self.cfg.trail_step_atr * atr_v))
        candidate = (position.entry_price + self.cfg.stop_atr * atr_v) - steps * self.cfg.trail_offset_atr * atr_v
        return min(position.stop_price, candidate)

    def generate_exits(
        self,
        position: Position,
        market: MarketSnapshot,
        decision: RegimeDecision,
        now_ms: int,
    ) -> list[OrderIntent]:
        if position.strategy is not StrategyName.TREND:
            return []
        h4 = last_closed(market.h4, now_ms) or list(market.h4)
        daily = last_closed(market.daily, now_ms) or list(market.daily)
        price = market.mid or (h4[-1].close if h4 else position.entry_price)
        atr_v = last_value(atr(h4, self.cfg.atr_period)) if h4 else None

        # 硬止损
        hit_stop = (
            price <= position.stop_price if position.side is Side.LONG else price >= position.stop_price
        )
        if hit_stop:
            return [make_close_intent(position, price, "趋势硬止损触发")]

        # 趋势失效：EMA 交叉或 ADX 回落到 15 以下
        if len(daily) >= self.cfg.ema_slow:
            closes = [c.close for c in daily]
            fast = last_value(ema(closes, self.cfg.ema_fast))
            slow = last_value(ema(closes, self.cfg.ema_slow))
            daily_adx = decision.daily_adx
            if fast is not None and slow is not None:
                flipped = (position.side is Side.LONG and fast < slow) or (
                    position.side is Side.SHORT and fast > slow
                )
                if flipped:
                    return [make_close_intent(position, price, "日线 EMA20/50 交叉，趋势失效离场")]
            if daily_adx is not None and daily_adx < self.cfg.adx_exit:
                return [make_close_intent(position, price, f"日线 ADX={daily_adx:.1f}<{self.cfg.adx_exit} 趋势衰竭离场")]

        # 移动止损（更新仓位止损，不立刻平仓）
        if atr_v:
            new_stop = self.trailing_stop(position, price, atr_v)
            if (position.side is Side.LONG and new_stop > position.stop_price + 1e-9) or (
                position.side is Side.SHORT and new_stop < position.stop_price - 1e-9
            ):
                position.stop_price = new_stop
                return [
                    OrderIntent(
                        action=IntentAction.REDUCE,
                        symbol=position.symbol,
                        strategy=StrategyName.TREND,
                        side=position.side.opposite(),
                        kind=OrderKind.MARKET,
                        size=0.0,
                        price=price,
                        stop_price=new_stop,
                        leverage=position.leverage,
                        isolated=True,
                        reduce_only=True,
                        reason=f"趋势移动止损更新至 {new_stop:.6g}",
                        extras={"trail_update_only": True},
                    )
                ]
        return []

    def trend_still_confirmed(self, position: Position, decision: RegimeDecision) -> bool:
        """趋势指标继续确认：EMA 方向与持仓一致，且 ADX 未落到衰竭线以下。"""
        if decision.daily_ema_fast is None or decision.daily_ema_slow is None:
            return False
        aligned = (
            position.side is Side.LONG and decision.daily_ema_fast > decision.daily_ema_slow
        ) or (position.side is Side.SHORT and decision.daily_ema_fast < decision.daily_ema_slow)
        adx_ok = decision.daily_adx is not None and decision.daily_adx >= self.cfg.adx_exit
        return aligned and adx_ok

    def pyramid_breakeven_stop(self, avg_entry: float, atr_v: float, side: Side, current_stop: float) -> float:
        """止损上移至加权均价保本以上（再加一小段 ATR 缓冲），且不往回拉。"""
        buffer = max(self.cfg.pyramid_be_buffer_atr * atr_v, avg_entry * 1e-5)
        if side is Side.LONG:
            return max(current_stop, avg_entry + buffer)
        return min(current_stop, avg_entry - buffer)

    def generate_pyramid(
        self,
        position: Position,
        market: MarketSnapshot,
        decision: RegimeDecision,
        now_ms: int | None = None,
    ) -> Signal | None:
        """
        首仓浮盈 ≥ 1×ATR 且趋势仍确认 → 可加仓不超过首仓 50%。
        急涨追突破首仓默认跳过；浮亏/无浮盈不得加仓（那是马丁）。
        """
        if not self.cfg.pyramid_enabled:
            return None
        if position.strategy is not StrategyName.TREND:
            return None
        if position.is_chase() and self.cfg.pyramid_skip_chase:
            return None
        original = position.original_size()
        if original <= 0:
            return None
        already = position.pyramid_added_frac()
        if already >= self.cfg.pyramid_max_frac - 1e-9:
            return None
        if not self.trend_still_confirmed(position, decision):
            return None

        h4 = last_closed(market.h4, now_ms) or list(market.h4)
        atr_v = last_value(atr(h4, self.cfg.atr_period)) if h4 else None
        if atr_v is None or atr_v <= 0:
            return None
        price = market.mid or (h4[-1].close if h4 else position.entry_price)
        favorable = position.favorable_move(price)
        if favorable <= 0:
            return None
        if favorable < self.cfg.pyramid_min_atr * atr_v:
            return None

        add_frac = min(self.cfg.pyramid_max_frac - already, self.cfg.pyramid_max_frac)
        if add_frac <= 1e-9:
            return None
        add_size = original * add_frac
        new_size = position.size + add_size
        avg = (position.entry_price * position.size + price * add_size) / new_size
        new_stop = self.pyramid_breakeven_stop(avg, atr_v, position.side, position.stop_price)
        return Signal(
            symbol=position.symbol,
            strategy=StrategyName.TREND,
            side=position.side,
            kind=OrderKind.MAKER_LIMIT,
            entry_price=price,
            stop_price=new_stop,
            reason=(
                f"趋势金字塔加仓：浮盈 {favorable / atr_v:.2f}×ATR ≥ {self.cfg.pyramid_min_atr}×ATR，"
                f"加仓不超过首仓 {self.cfg.pyramid_max_frac:.0%}，止损移至保本以上 {new_stop:.6g}"
            ),
            tag="pyramid_add",
            extras={
                "pyramid": True,
                "atr": atr_v,
                "add_frac": add_frac,
                "add_size": add_size,
                "original_size": original,
                "avg_entry": avg,
                "favorable": favorable,
            },
        )
