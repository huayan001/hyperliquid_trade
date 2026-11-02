"""趋势跟踪波段（v1.1）：日线确认可先 starter，4h Donchian 收盘突破补满；禁止等 EMA 回撤。

同向止损之后的新开仓另计一道过滤（由 runner 调用）：必须出现止损之后的日线收盘重回 EMA20。
已有仓的突破补仓和金字塔不加这道过滤，补仓仍只等 4h Donchian 收盘突破。

出场由 trend.exit_mode 切换：
- partial_atr（方案 B，默认）：开仓时冻结 4h ATR(14)；浮盈达 1× 该 ATR 平 50%，2× 平剩余。
  距离从当前均价量。初始结构止损全程冻结，金字塔和 Donchian 加仓都不把止损抬向保本。
- trail（方案 A）：v0 阶梯移动止损；金字塔加仓后止损抬到保本以上。
"""

from __future__ import annotations

import logging

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

logger = logging.getLogger(__name__)


def _pos_float(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return number


def record_entry_exit_reference(position: Position) -> None:
    """记下首笔成交时的 4h ATR 和结构止损。加仓不得刷新这两项，也不改当前止损价。"""
    if position.strategy is not StrategyName.TREND:
        return
    if _pos_float(position.extras.get("entry_atr")) <= 0:
        atr_v = _pos_float(position.extras.get("atr"))
        if atr_v > 0:
            position.extras["entry_atr"] = atr_v
            position.extras.setdefault("entry_atr_source", "open_signal")
    if _pos_float(position.extras.get("initial_stop")) <= 0 and position.stop_price > 0:
        position.extras["initial_stop"] = float(position.stop_price)
        position.extras.setdefault("initial_stop_source", "entry_fill")


class TrendStrategy(Strategy):
    name = StrategyName.TREND.value

    def __init__(self, cfg: TrendConfig | None = None) -> None:
        self.cfg = cfg or TrendConfig()

    def partial_atr_enabled(self) -> bool:
        return self.cfg.exit_mode == "partial_atr"

    def ensure_partial_atr_snapshot(self, position: Position, fallback_atr: float | None = None) -> None:
        """方案 B：把入场 ATR 和初始止损冻结一次。已有仓缺快照时不放宽止损。

        优先顺序：已写入的 entry_atr → 开仓信号 extras['atr'] → 当前止损距离 / stop_atr
        （止损仍在亏损一侧）→ 调用方给的当下 4h ATR。当前止损原样记为 initial_stop。
        """
        if not self.partial_atr_enabled() or position.strategy is not StrategyName.TREND:
            return
        wrote_stop = False
        wrote_atr = False
        if _pos_float(position.extras.get("initial_stop")) <= 0 and position.stop_price > 0:
            position.extras["initial_stop"] = float(position.stop_price)
            position.extras["initial_stop_source"] = "frozen_current"
            wrote_stop = True
        if _pos_float(position.extras.get("entry_atr")) <= 0:
            atr_v, source = self._resolve_entry_atr(position, fallback_atr)
            if atr_v > 0:
                position.extras["entry_atr"] = atr_v
                position.extras["entry_atr_source"] = source
                wrote_atr = True
        position.extras["partial_atr_stop_frozen"] = True
        if wrote_stop or wrote_atr:
            logger.info(
                "%s 方案B冻结出场参考 entry_atr=%s (%s) initial_stop=%s (%s) 当前止损=%s",
                position.symbol,
                position.extras.get("entry_atr"),
                position.extras.get("entry_atr_source"),
                position.extras.get("initial_stop"),
                position.extras.get("initial_stop_source"),
                position.stop_price,
            )

    def _resolve_entry_atr(self, position: Position, fallback_atr: float | None) -> tuple[float, str]:
        hinted = _pos_float(position.extras.get("atr"))
        if hinted > 0:
            return hinted, "open_signal"
        stop = _pos_float(position.extras.get("initial_stop")) or float(position.stop_price or 0.0)
        entry = float(position.entry_price or 0.0)
        if position.side is Side.LONG:
            dist = entry - stop
        else:
            dist = stop - entry
        mult = float(self.cfg.stop_atr or 0.0)
        if dist > 0 and mult > 0 and entry > 0:
            return dist / mult, "inferred_from_frozen_stop"
        live = _pos_float(fallback_atr)
        if live > 0:
            return live, "live_4h_atr_fallback"
        return 0.0, ""

    def generate_signal(
        self,
        market: MarketSnapshot,
        decision: RegimeDecision,
        now_ms: int | None = None,
        existing: Position | None = None,
    ) -> Signal | None:
        # 已有仓不得再发满仓开仓信号；starter 补仓走 generate_breakout_add。
        # 首仓仍可以只凭日线趋势开 starter。同向止损后的 EMA20 收回过滤在 runner，不放进这里。
        if existing is not None:
            return None
        prepared = self._prepare(market, decision, now_ms)
        if prepared is None:
            return None
        last, atr_v, upper, lower, daily_adx, side = prepared
        if self._donchian_confirmed(side, last, upper, lower):
            band = upper if side is Side.LONG else lower
            return self._build_breakout_signal(market, last, atr_v, side, band, daily_adx)
        return self._maybe_starter(market, last, atr_v, side, upper if side is Side.LONG else lower, daily_adx)

    def generate_breakout_add(
        self,
        position: Position,
        market: MarketSnapshot,
        decision: RegimeDecision,
        now_ms: int | None = None,
    ) -> Signal | None:
        """第一档 starter 已开：仅在 4h Donchian 收盘突破时补剩余计划仓，禁止再开一笔满仓。"""
        if not position.starter_pending():
            return None
        if position.strategy is not StrategyName.TREND:
            return None
        prepared = self._prepare(market, decision, now_ms)
        if prepared is None:
            return None
        last, atr_v, upper, lower, daily_adx, side = prepared
        if side is not position.side:
            return None
        if not self._donchian_confirmed(side, last, upper, lower):
            return None

        starter_frac = float(position.extras.get("starter_frac") or self.cfg.starter_frac)
        intended_full = position.intended_full_size()
        add_size = intended_full - position.size
        if add_size <= 1e-12:
            return None

        chase = (
            market.symbol.upper() in {s.upper() for s in self.cfg.chase_symbols}
            and body_exceeds_atr(last, atr_v, self.cfg.chase_body_atr)
        )
        if chase:
            kind = OrderKind.MARKET
            tag = "donchian_close_breakout"
            how = f"突破K实体{last.body:.4g}> {self.cfg.chase_body_atr}×ATR，第二档市价补仓"
        else:
            kind = OrderKind.MAKER_LIMIT
            tag = "donchian_close_breakout"
            how = "收盘突破 Donchian20，第二档补剩余仓；共用已有止损"

        band = upper if side is Side.LONG else lower
        direction = "做多" if side is Side.LONG else "做空"
        remaining_frac = max(0.0, 1.0 - starter_frac)
        reason = (
            f"趋势分层第二档 {tag}：{direction} 4h Donchian{self.cfg.donchian_period} 收盘确认"
            f"（收盘{last.close:.6g} vs 通道{'上' if side is Side.LONG else '下'}轨{band:.6g}）。"
            f"在 starter 之上补约 {remaining_frac:.0%} 计划仓（size≈{add_size:.6g} @ {last.close:.6g}）。{how}"
        )
        return Signal(
            symbol=market.symbol,
            strategy=StrategyName.TREND,
            side=side,
            kind=kind,
            entry_price=last.close,
            stop_price=position.stop_price,
            reason=reason,
            tag=tag,
            extras={
                "atr": atr_v,
                "donchian_band": band,
                "chase": chase,
                "daily_adx": daily_adx,
                "forbid_ema_pullback": True,
                "tier": "donchian_close_breakout",
                "tier_add": True,
                "breakout_add": True,
                "add_size": add_size,
                "original_size": position.original_size(),
                "intended_full_size": intended_full,
                "starter_frac": starter_frac,
                "shared_stop": True,
                "pyramid": False,
            },
        )

    def _prepare(
        self,
        market: MarketSnapshot,
        decision: RegimeDecision,
        now_ms: int | None,
    ) -> tuple | None:
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
        if side is Side.LONG and decision.regime is not Regime.TREND_LONG:
            return None
        if side is Side.SHORT:
            if decision.regime is not Regime.TREND_SHORT:
                return None
            if market.symbol.upper() == "BTC":
                if daily_adx is None or daily_adx <= self.cfg.btc_short_adx_min:
                    return None
        return last, atr_v, upper, lower, daily_adx, side

    @staticmethod
    def _donchian_confirmed(side: Side, last, upper: float, lower: float) -> bool:
        if side is Side.LONG:
            return last.close > upper
        return last.close < lower

    def post_stop_reclaim_block(
        self,
        market: MarketSnapshot,
        side: Side,
        stop_ts: int | None,
        now_ms: int | None = None,
    ) -> str | None:
        """同向止损之后，最近一根止损后收盘的日线必须重回 EMA（多在上、空在下），才允许新开仓。

        返回拒绝原因；可以再开则返回 None。没有止损记录时不拦截。
        止损前已经收盘的日线不算「重回」。相等不算站上/站下。
        """
        if stop_ts is None or stop_ts <= 0:
            return None
        period = self.cfg.ema_fast
        daily = last_closed(market.daily, now_ms)
        symbol = market.symbol
        if len(daily) < period:
            return (
                f"{symbol} {side.value} 止损后已收盘日线不足 {period} 根，"
                f"无法确认 EMA{period} 收回，拒绝同向再开"
            )
        last = daily[-1]
        if last.end_ts <= stop_ts:
            return (
                f"{symbol} {side.value} 止损后还没有新的日线收盘，"
                f"不能把止损前的收盘当作 EMA{period} 收回，拒绝同向再开"
            )
        ema_v = last_value(ema([c.close for c in daily], period))
        if ema_v is None:
            return f"{symbol} {side.value} 日线 EMA{period} 不可用，拒绝同向再开"
        close = last.close
        reclaimed = close > ema_v if side is Side.LONG else close < ema_v
        if reclaimed:
            return None
        need = "上方" if side is Side.LONG else "下方"
        return (
            f"{symbol} {side.value} 最近一根已收盘日线 {close:.6g} "
            f"尚未回到 EMA{period}={ema_v:.6g} {need}，拒绝同向再开"
        )

    def _starter_allowed_for(self, symbol: str) -> bool:
        if not self.cfg.starter_enabled:
            return False
        frac = self.cfg.starter_frac
        if frac <= 0.0 or frac >= 1.0:
            return False
        allow = tuple(s.upper() for s in self.cfg.starter_symbols)
        if not allow:
            return True
        return symbol.upper() in allow

    def _maybe_starter(
        self,
        market: MarketSnapshot,
        last,
        atr_v: float,
        side: Side,
        band: float,
        daily_adx: float | None,
    ) -> Signal | None:
        if not self._starter_allowed_for(market.symbol):
            return None
        entry = market.mid if market.mid > 0 else last.close
        if entry <= 0:
            return None
        frac = self.cfg.starter_frac
        if side is Side.LONG:
            stop = entry - self.cfg.stop_atr * atr_v
            direction = "做多"
        else:
            stop = entry + self.cfg.stop_atr * atr_v
            direction = "做空"
        reason = (
            f"趋势分层第一档 trend_confirmed_starter：日线趋势已确认（{direction}），"
            f"4h Donchian{self.cfg.donchian_period} 收盘突破尚未触发"
            f"（收盘{last.close:.6g} vs 通道{'上' if side is Side.LONG else '下'}轨{band:.6g}，mid={entry:.6g}）。"
            f"市价试探仓约为计划满仓的 {frac:.0%}，止损仍 {self.cfg.stop_atr:g}×ATR。"
        )
        return Signal(
            symbol=market.symbol,
            strategy=StrategyName.TREND,
            side=side,
            kind=OrderKind.MARKET,
            entry_price=entry,
            stop_price=stop,
            reason=reason,
            tag="trend_confirmed_starter",
            extras={
                "atr": atr_v,
                "donchian_band": band,
                "chase": False,
                "daily_adx": daily_adx,
                "forbid_ema_pullback": True,
                "starter": True,
                "starter_frac": frac,
                "size_frac": frac,
                "tier": "trend_confirmed_starter",
                "starter_pending_add": True,
            },
        )

    def _build_breakout_signal(
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
            f"趋势{direction}第二档/整笔 {tag}：4h Donchian{self.cfg.donchian_period} 收盘确认"
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
                "tier": tag,
                "starter": False,
                "size_frac": 1.0,
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

        # 方案 B：分批止盈，不跑阶梯 trail，也不在这里改止损。
        if self.partial_atr_enabled():
            self.ensure_partial_atr_snapshot(position, fallback_atr=atr_v)
            return self._partial_atr_exits(position, price)

        # 方案 A：移动止损（更新仓位止损，不立刻平仓）
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

    def _partial_atr_exits(self, position: Position, price: float) -> list[OrderIntent]:
        """浮盈相对当前均价达到冻结入场 ATR 的倍数时减仓。止损价不动。"""
        if position.extras.get("partial2_atr_done"):
            return []
        entry_atr = _pos_float(position.extras.get("entry_atr"))
        if entry_atr <= 0 or position.size <= 0:
            return []
        favorable = position.favorable_move(price)
        tp1 = self.cfg.partial_atr * entry_atr
        tp2 = self.cfg.partial2_atr * entry_atr
        done1 = bool(position.extras.get("partial_atr_done"))
        target_left = _pos_float(position.extras.get("partial_atr_size_after"))
        # 上一轮减仓请求已经把张数打到目标（含交易所先成交、本地标志没写上）则不再减一次。
        if not done1 and target_left > 0 and position.size <= target_left * 1.02 + 1e-12:
            position.extras["partial_atr_done"] = True
            done1 = True
        if tp2 > 0 and favorable + 1e-12 >= tp2:
            frac = self.cfg.partial2_atr_frac
            if not done1 and frac >= 1.0 - 1e-12:
                frac = 1.0
            return [
                self._partial_atr_intent(
                    position,
                    price,
                    level=2,
                    frac=frac,
                    entry_atr=entry_atr,
                    favorable=favorable,
                )
            ]
        if not done1 and tp1 > 0 and favorable + 1e-12 >= tp1:
            return [
                self._partial_atr_intent(
                    position,
                    price,
                    level=1,
                    frac=self.cfg.partial_atr_frac,
                    entry_atr=entry_atr,
                    favorable=favorable,
                )
            ]
        return []

    def _partial_atr_intent(
        self,
        position: Position,
        price: float,
        *,
        level: int,
        frac: float,
        entry_atr: float,
        favorable: float,
    ) -> OrderIntent:
        multiple = self.cfg.partial2_atr if level >= 2 else self.cfg.partial_atr
        if level >= 2 and frac >= 1.0 - 1e-12:
            qty = position.size
            reason = (
                f"方案B：浮盈 {favorable / entry_atr:.2f}×入场ATR ≥ {multiple:g}×，"
                f"平掉剩余仓位（入场ATR={entry_atr:.6g}，均价={position.entry_price:.6g}，保护价保持 {position.stop_price:.6g}）"
            )
        elif level == 1:
            target_left = _pos_float(position.extras.get("partial_atr_size_after"))
            if target_left > 0:
                qty = max(0.0, position.size - target_left)
            else:
                qty = position.size * frac
                position.extras["partial_atr_size_after"] = max(0.0, position.size - qty)
            reason = (
                f"方案B：浮盈 {favorable / entry_atr:.2f}×入场ATR ≥ {multiple:g}×，"
                f"平掉当前仓位的 {frac:.0%}（入场ATR={entry_atr:.6g}，均价={position.entry_price:.6g}，保护价保持 {position.stop_price:.6g}）"
            )
        else:
            qty = position.size * frac
            reason = (
                f"方案B：浮盈 {favorable / entry_atr:.2f}×入场ATR ≥ {multiple:g}×，"
                f"再平剩余仓位的 {frac:.0%}（入场ATR={entry_atr:.6g}，均价={position.entry_price:.6g}，保护价保持 {position.stop_price:.6g}）"
            )
        return make_close_intent(
            position,
            price,
            reason,
            size=qty,
            extras={
                "partial_atr_level": level,
                "partial_requested_size": qty,
                "entry_atr": entry_atr,
            },
        )

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
        starter 未补满前不加金字塔，只等 Donchian 第二档。
        方案 A 把止损抬到保本以上；方案 B 保持当前结构止损。
        """
        if not self.cfg.pyramid_enabled:
            return None
        if position.strategy is not StrategyName.TREND:
            return None
        if position.starter_pending():
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
        self.ensure_partial_atr_snapshot(position, fallback_atr=atr_v)
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
        if self.partial_atr_enabled():
            new_stop = position.stop_price
            stop_note = f"初始结构止损保持 {new_stop:.6g}，不加保本"
        else:
            new_stop = self.pyramid_breakeven_stop(avg, atr_v, position.side, position.stop_price)
            stop_note = f"止损移至保本以上 {new_stop:.6g}"
        return Signal(
            symbol=position.symbol,
            strategy=StrategyName.TREND,
            side=position.side,
            kind=OrderKind.MAKER_LIMIT,
            entry_price=price,
            stop_price=new_stop,
            reason=(
                f"趋势金字塔加仓：浮盈 {favorable / atr_v:.2f}×ATR ≥ {self.cfg.pyramid_min_atr}×ATR，"
                f"加仓不超过首仓 {self.cfg.pyramid_max_frac:.0%}，{stop_note}"
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
