"""仓位与组合风险：杠杆由风险预算 / 止损距离反推，禁止马丁加仓。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hl_bot.config import BotConfig
from hl_bot.models import (
    AccountState,
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
        # 禁止同一标的同向加仓 / 马丁
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

    def to_open_intent(self, signal: Signal, decision: RiskDecision, isolated: bool = True) -> OrderIntent | None:
        if not decision.allowed or decision.size is None:
            return None
        from hl_bot.models import IntentAction

        sz = decision.size
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
            extras={
                **signal.extras,
                "tag": signal.tag,
                "margin_usd": sz.margin_usd,
                "risk_pct": sz.risk_pct,
                "stop_pct": sz.stop_pct,
                "capped_by_max_leverage": sz.capped_by_max_leverage,
                "leverage_scale": decision.leverage_scale,
            },
        )


def apply_fill(account: AccountState, position: Position) -> None:
    position.extras.setdefault("_opened_size", position.size)
    account.positions.append(position)
    account.day_trades[position.symbol] = account.day_trades.get(position.symbol, 0) + 1


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
