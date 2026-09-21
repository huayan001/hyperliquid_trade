"""仓位与组合风险：杠杆由风险预算 / 止损距离反推，禁止马丁加仓。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hl_bot.config import BotConfig
from hl_bot.models import (
    AccountState,
    IntentAction,
    OrderIntent,
    Position,
    PositionSize,
    Side,
    Signal,
    StrategyName,
)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    size: PositionSize | None = None
    risk_pct_used: float = 0.0
    leverage_scale: float = 1.0


def derive_position_size(
    *,
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
    max_leverage: int,
    min_notional: float = 10.0,
    size_mult: float = 1.0,
) -> PositionSize | None:
    """
    仓位名义价值 = (账户资金 × 单笔风险比例) / 止损距离百分比
    实际杠杆 = 名义价值 / 保证金，且不超过标的上限。
    """
    if equity <= 0 or entry <= 0:
        return None
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return None
    stop_pct = stop_dist / entry
    risk_usd = equity * risk_pct * size_mult
    if risk_usd <= 0:
        return None
    notional = risk_usd / stop_pct
    raw_leverage = notional / risk_usd  # == 1 / stop_pct
    capped = raw_leverage > max_leverage
    leverage = max(1, min(int(math.ceil(raw_leverage - 1e-9)), max_leverage))
    if capped:
        leverage = max_leverage
    margin = notional / leverage
    size = notional / entry
    if notional < min_notional:
        return None
    return PositionSize(
        notional_usd=notional,
        size=size,
        leverage=leverage,
        margin_usd=margin,
        risk_usd=risk_usd,
        risk_pct=risk_pct * size_mult,
        stop_pct=stop_pct,
        capped_by_max_leverage=capped,
    )


class RiskManager:
    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg

    def leverage_scale(self, account: AccountState, strategy: StrategyName) -> float:
        """日/周回撤触发趋势策略降杠杆（仓位等比例缩小）。"""
        scale = 1.0
        if strategy is StrategyName.TREND:
            if account.daily_loss_pct() >= self.cfg.trend.daily_loss_delever:
                scale *= 0.5
            if account.weekly_loss_pct() >= self.cfg.trend.weekly_loss_delever:
                scale *= 0.5
        return scale

    def evaluate_open(
        self,
        signal: Signal,
        account: AccountState,
        *,
        funding_against: bool = False,
        max_leverage_hint: int | None = None,
    ) -> RiskDecision:
        # 禁止马丁/摊平：浮亏或无条件的同标的再开。金字塔走 evaluate_pyramid。
        existing = account.position_for(signal.symbol)
        if existing is not None:
            return RiskDecision(False, f"{signal.symbol} 已有仓位，禁止加仓或摊平")

        if signal.strategy is StrategyName.MEAN_REVERSION:
            if account.daily_loss_pct() >= self.cfg.mean_reversion.daily_loss_halt:
                return RiskDecision(False, "均值回归：当日回撤已达 3%，停止新开仓")
            day_count = account.day_trades.get(signal.symbol, 0)
            if day_count >= self.cfg.mean_reversion.max_trades_per_symbol_day:
                return RiskDecision(False, f"{signal.symbol} 当日开仓次数已达上限")
            open_mr = [p for p in account.open_positions() if p.strategy is StrategyName.MEAN_REVERSION]
            if len(open_mr) >= self.cfg.mean_reversion.max_positions:
                return RiskDecision(False, "均值回归同时持仓数已满")
            risk_pct = self.cfg.mean_reversion.risk_pct
        else:
            open_tr = [p for p in account.open_positions() if p.strategy is StrategyName.TREND]
            if len(open_tr) >= self.cfg.trend.max_positions:
                return RiskDecision(False, "趋势策略同时持仓数已满")
            risk_pct = self.cfg.trend.risk_pct
            if signal.side is Side.SHORT and signal.symbol.upper() == "BTC":
                risk_pct *= self.cfg.trend.btc_short_risk_mult

        scale = self.leverage_scale(account, signal.strategy)
        size_mult = scale
        if funding_against:
            size_mult *= self.cfg.trend.funding_size_mult
        if signal.strategy is StrategyName.TREND:
            size_mult *= self._trend_open_frac(signal)

        max_lev = max_leverage_hint or self.cfg.max_leverage_for(signal.symbol)
        if signal.strategy is StrategyName.MEAN_REVERSION:
            max_lev = min(max_lev, self.cfg.mean_reversion.leverage_cap)

        sized = derive_position_size(
            equity=account.equity,
            risk_pct=risk_pct,
            entry=signal.entry_price,
            stop=signal.stop_price,
            max_leverage=max_lev,
            min_notional=self.cfg.risk.min_notional_usd,
            size_mult=size_mult,
        )
        if sized is None:
            return RiskDecision(False, "仓位过小或止损距离无效，跳过")

        projected_risk = account.open_risk_usd() + sized.risk_usd
        if signal.strategy is StrategyName.TREND:
            if projected_risk > account.equity * self.cfg.trend.portfolio_risk_max + 1e-9:
                return RiskDecision(False, "组合潜在止损损失将超过账户 6%")

        return RiskDecision(True, "风控通过", sized, sized.risk_pct, scale)

    def _trend_open_frac(self, signal: Signal) -> float:
        """starter 按计划满仓的一小部分计风险；整笔突破仍为 1。"""
        if signal.extras.get("starter"):
            frac = float(signal.extras.get("starter_frac") or self.cfg.trend.starter_frac)
        else:
            frac = float(signal.extras.get("size_frac") or 1.0)
        if frac <= 0.0:
            return 1.0
        return min(frac, 1.0)

    def evaluate_tier_add(
        self,
        signal: Signal,
        position: Position,
        account: AccountState,
    ) -> RiskDecision:
        """starter 之后的 Donchian 补仓：允许把风险补到原计划满仓，但不得超过组合上限。"""
        if not position.starter_pending():
            return RiskDecision(False, "仅待补仓的趋势 starter 允许 Donchian 补仓")
        if position.symbol != signal.symbol or position.side is not signal.side:
            return RiskDecision(False, "补仓必须与已有仓同标的同向")
        if position.strategy is not StrategyName.TREND:
            return RiskDecision(False, "仅趋势仓允许分层补仓")

        intended_full = float(signal.extras.get("intended_full_size") or position.intended_full_size())
        remaining = intended_full - position.size
        add_size = float(signal.extras.get("add_size") or remaining)
        add_size = min(add_size, remaining)
        scale = self.leverage_scale(account, StrategyName.TREND)
        add_size *= scale
        if add_size <= 1e-12:
            return RiskDecision(False, "starter 已达计划满仓，不再二次开仓")

        new_stop = position.stop_price
        current_risk = position.loss_risk_usd()
        intended_risk = float(position.extras.get("intended_risk_usd") or 0.0)
        if intended_risk <= 0:
            risk_pct = self.cfg.trend.risk_pct
            if position.side is Side.SHORT and position.symbol.upper() == "BTC":
                risk_pct *= self.cfg.trend.btc_short_risk_mult
            intended_risk = account.equity * risk_pct * scale

        def _risks(size: float) -> tuple[float, float, float]:
            new_size = position.size + size
            avg = (position.entry_price * position.size + signal.entry_price * size) / new_size
            if position.side is Side.LONG:
                add_risk = max(0.0, (signal.entry_price - new_stop) * size)
                combined = max(0.0, (avg - new_stop) * new_size)
            else:
                add_risk = max(0.0, (new_stop - signal.entry_price) * size)
                combined = max(0.0, (new_stop - avg) * new_size)
            return add_risk, combined, avg

        add_risk, combined_risk, _avg = _risks(add_size)
        room = intended_risk - current_risk
        if add_risk > room + 1e-6 and add_risk > 0:
            add_size *= max(0.0, room) / add_risk
            if add_size <= 1e-12:
                return RiskDecision(False, "补仓后将超过原计划满仓风险，拒绝加仓")
            add_risk, combined_risk, _avg = _risks(add_size)

        if add_size * signal.entry_price < self.cfg.risk.min_notional_usd:
            return RiskDecision(False, "突破补仓名义价值低于最小下单额")

        others = account.open_risk_usd() - current_risk
        if others + combined_risk > account.equity * self.cfg.trend.portfolio_risk_max + 1e-9:
            return RiskDecision(False, "组合潜在止损损失将超过账户 6%")

        sized = PositionSize(
            notional_usd=add_size * signal.entry_price,
            size=add_size,
            leverage=position.leverage,
            margin_usd=(add_size * signal.entry_price) / max(position.leverage, 1),
            risk_usd=add_risk,
            risk_pct=0.0,
            stop_pct=abs(signal.entry_price - new_stop) / signal.entry_price if signal.entry_price else 0.0,
        )
        return RiskDecision(True, "分层补仓风控通过", sized, 0.0, scale)

    def to_tier_add_intent(
        self,
        signal: Signal,
        position: Position,
        decision: RiskDecision,
        isolated: bool = True,
    ) -> OrderIntent | None:
        if not decision.allowed or decision.size is None:
            return None
        add_size = decision.size.size
        total = position.size + add_size
        return OrderIntent(
            action=IntentAction.ADD,
            symbol=signal.symbol,
            strategy=StrategyName.TREND,
            side=signal.side,
            kind=signal.kind,
            size=add_size,
            price=signal.entry_price,
            stop_price=position.stop_price,
            leverage=position.leverage,
            isolated=isolated,
            reduce_only=False,
            reason=signal.reason,
            risk_usd=decision.size.risk_usd,
            notional_usd=add_size * signal.entry_price,
            extras={
                **signal.extras,
                "tag": signal.tag,
                "tier": signal.tag or "donchian_close_breakout",
                "tier_add": True,
                "breakout_add": True,
                "pyramid": False,
                "shared_stop": True,
                "total_size": total,
                "add_size": add_size,
                "sl_oid": position.extras.get("sl_oid"),
            },
        )

    def evaluate_pyramid(
        self,
        signal: Signal,
        position: Position,
        account: AccountState,
    ) -> RiskDecision:
        """允许符合条件的趋势金字塔；拒绝浮亏摊平，且加仓后账户止损风险不得上升。"""
        if not self.cfg.trend.pyramid_enabled:
            return RiskDecision(False, "金字塔加仓已关闭")
        if position.symbol != signal.symbol or position.side is not signal.side:
            return RiskDecision(False, "金字塔加仓必须与已有仓同标的同向")
        if position.strategy is not StrategyName.TREND:
            return RiskDecision(False, "仅趋势仓允许金字塔加仓")
        if signal.side is not position.side:
            return RiskDecision(False, "反向开仓不是金字塔")
        if position.favorable_move(signal.entry_price) <= 0:
            return RiskDecision(False, "浮亏或无浮盈，禁止摊平/马丁加仓")

        original = float(signal.extras.get("original_size") or position.original_size())
        add_size = float(signal.extras.get("add_size") or original * self.cfg.trend.pyramid_max_frac)
        max_add = original * self.cfg.trend.pyramid_max_frac - (position.size - original)
        if add_size > max_add + 1e-9:
            return RiskDecision(False, f"加仓不得超过首仓 {self.cfg.trend.pyramid_max_frac:.0%}")
        if add_size * signal.entry_price < self.cfg.risk.min_notional_usd:
            return RiskDecision(False, "金字塔加仓名义价值低于最小下单额")

        new_size = position.size + add_size
        avg = float(
            signal.extras.get("avg_entry")
            or (position.entry_price * position.size + signal.entry_price * add_size) / new_size
        )
        new_stop = signal.stop_price
        if position.side is Side.LONG:
            new_risk = max(0.0, (avg - new_stop) * new_size)
        else:
            new_risk = max(0.0, (new_stop - avg) * new_size)
        others = account.open_risk_usd() - position.loss_risk_usd()
        old_total = account.open_risk_usd()
        new_total = others + new_risk
        if new_total > old_total + 1e-6:
            return RiskDecision(False, "加仓后组合止损风险将扩大，拒绝金字塔")
        if new_total > account.equity * self.cfg.trend.portfolio_risk_max + 1e-9:
            return RiskDecision(False, "组合潜在止损损失将超过账户 6%")

        sized = PositionSize(
            notional_usd=add_size * signal.entry_price,
            size=add_size,
            leverage=position.leverage,
            margin_usd=(add_size * signal.entry_price) / max(position.leverage, 1),
            risk_usd=new_risk,
            risk_pct=0.0,
            stop_pct=abs(signal.entry_price - new_stop) / signal.entry_price if signal.entry_price else 0.0,
        )
        return RiskDecision(True, "金字塔风控通过", sized, 0.0, 1.0)

    def to_pyramid_intent(
        self,
        signal: Signal,
        position: Position,
        decision: RiskDecision,
        isolated: bool = True,
    ) -> OrderIntent | None:
        if not decision.allowed or decision.size is None:
            return None
        add_size = decision.size.size
        total = position.size + add_size
        return OrderIntent(
            action=IntentAction.ADD,
            symbol=signal.symbol,
            strategy=StrategyName.TREND,
            side=signal.side,
            kind=signal.kind,
            size=add_size,
            price=signal.entry_price,
            stop_price=signal.stop_price,
            leverage=position.leverage,
            isolated=isolated,
            reduce_only=False,
            reason=signal.reason,
            risk_usd=0.0,
            notional_usd=add_size * signal.entry_price,
            extras={
                **signal.extras,
                "tag": signal.tag,
                "pyramid": True,
                "total_size": total,
                "sl_oid": position.extras.get("sl_oid"),
            },
        )

    def to_open_intent(self, signal: Signal, decision: RiskDecision, isolated: bool = True) -> OrderIntent | None:
        if not decision.allowed or decision.size is None:
            return None
        sz = decision.size
        extras = {
            **signal.extras,
            "tag": signal.tag,
            "margin_usd": sz.margin_usd,
            "risk_pct": sz.risk_pct,
            "stop_pct": sz.stop_pct,
            "capped_by_max_leverage": sz.capped_by_max_leverage,
            "leverage_scale": decision.leverage_scale,
            "tier": signal.extras.get("tier") or signal.tag,
        }
        frac = self._trend_open_frac(signal) if signal.strategy is StrategyName.TREND else 1.0
        if signal.extras.get("starter") and 0.0 < frac < 1.0 and sz.size > 0:
            extras["intended_full_size"] = sz.size / frac
            extras["intended_risk_usd"] = sz.risk_usd / frac
            extras["starter_pending_add"] = True
            extras["starter_frac"] = frac
        return OrderIntent(
            action=IntentAction.OPEN,
            symbol=signal.symbol,
            strategy=signal.strategy,
            side=signal.side,
            kind=signal.kind,
            size=sz.size,
            price=signal.entry_price,
            stop_price=signal.stop_price,
            leverage=sz.leverage,
            isolated=isolated,
            reduce_only=False,
            reason=signal.reason,
            risk_usd=sz.risk_usd,
            notional_usd=sz.notional_usd,
            extras=extras,
        )


def apply_fill(account: AccountState, position: Position) -> None:
    position.extras.setdefault("_opened_size", position.size)
    account.positions.append(position)
    account.day_trades[position.symbol] = account.day_trades.get(position.symbol, 0) + 1


def apply_pyramid(account: AccountState, position: Position, add_size: float, add_price: float, new_stop: float) -> None:
    """合并加仓段：加权均价，止损提到保本以上；首仓规模保持 _opened_size。"""
    if add_size <= 0:
        return
    old_size = position.size
    new_size = old_size + add_size
    position.entry_price = (position.entry_price * old_size + add_price * add_size) / new_size
    position.size = new_size
    position.stop_price = new_stop
    opened = position.original_size()
    position.extras["_opened_size"] = opened
    position.extras["pyramid_added_frac"] = max(0.0, (new_size - opened) / opened) if opened else 0.0
    position.remaining_frac = new_size / opened if opened else 1.0
    position.extras["pyramid_count"] = int(position.extras.get("pyramid_count") or 0) + 1


def apply_tier_add(account: AccountState, position: Position, add_size: float, add_price: float, new_stop: float) -> None:
    """合并 Donchian 第二档：加权均价，止损保持共用；完成后的规模视为后续金字塔的首仓。"""
    if add_size <= 0:
        return
    old_size = position.size
    new_size = old_size + add_size
    position.entry_price = (position.entry_price * old_size + add_price * add_size) / new_size
    position.size = new_size
    position.stop_price = new_stop
    position.extras["_opened_size"] = new_size
    position.extras["starter_pending_add"] = False
    position.extras["starter"] = False
    position.extras["tier_add_done"] = True
    position.extras["pyramid_added_frac"] = 0.0
    position.remaining_frac = 1.0
    tags = list(position.extras.get("entry_tags") or ([position.tag] if position.tag else []))
    tags.append("donchian_close_breakout")
    position.extras["entry_tags"] = tags
    if position.extras.get("chase"):
        position.tag = "btc_chase_breakout"
    else:
        position.tag = "donchian_close_breakout"


def apply_close(account: AccountState, position: Position, exit_price: float, size: float) -> float:
    opened = float(position.extras.get("_opened_size") or 0.0)
    if opened <= 0:
        opened = position.size + max(size, 0.0)
        position.extras["_opened_size"] = opened
    qty = min(size, position.size)
    direction = 1.0 if position.side is Side.LONG else -1.0
    pnl = direction * (exit_price - position.entry_price) * qty
    position.size -= qty
    position.remaining_frac = 0.0 if opened <= 1e-12 else max(position.size, 0.0) / opened
    if position.size <= 1e-12:
        account.positions = [p for p in account.positions if p is not position]
    account.equity += pnl
    account.realized_pnl_today += pnl
    return pnl
