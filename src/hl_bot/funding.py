"""持仓期资金费率检查：方向持续付费且年化异常飙升则提前离场。"""

from __future__ import annotations

from hl_bot.models import (
    FundingInfo,
    IntentAction,
    OrderIntent,
    OrderKind,
    Position,
    Side,
    StrategyName,
)
from hl_bot.strategies.base import make_close_intent


def funding_against(side: Side, hourly_rate: float) -> bool:
    """正资金费率：多头付给空头。"""
    if side is Side.LONG:
        return hourly_rate > 0
    return hourly_rate < 0


def adverse_annualized(side: Side, funding: FundingInfo) -> float:
    """持仓方向正在支付的费率，取当前年化与 24h 均年化的较大者。"""
    annual = 0.0
    if funding_against(side, funding.hourly_rate):
        annual = max(annual, abs(funding.annualized))
    if funding_against(side, funding.avg_24h):
        annual = max(annual, abs(funding.annualized_24h))
    return annual


def should_exit_on_funding_spike(
    position: Position,
    funding: FundingInfo,
    *,
    threshold: float,
    enabled: bool,
) -> tuple[bool, str]:
    if not enabled:
        return False, ""
    annual = adverse_annualized(position.side, funding)
    if annual < threshold:
        return False, ""
    return True, (
        f"资金费率异常飙升：持仓方向持续付费，"
        f"年化 {annual:.1%} ≥ 阈值 {threshold:.0%}（当前 {funding.annualized:.1%} / "
        f"24h均 {funding.annualized_24h:.1%}），提前离场"
    )


def funding_exit_enabled_for(position: Position, trend_enabled: bool, mr_enabled: bool) -> bool:
    if position.strategy is StrategyName.TREND:
        return trend_enabled
    if position.strategy is StrategyName.MEAN_REVERSION:
        return mr_enabled
    return False


def make_funding_exit_intent(
    position: Position,
    price: float,
    reason: str,
) -> OrderIntent:
    intent = make_close_intent(position, price, reason, kind_market=True)
    extras = dict(intent.extras)
    extras["funding_exit"] = True
    return OrderIntent(
        action=IntentAction.CLOSE,
        symbol=intent.symbol,
        strategy=intent.strategy,
        side=intent.side,
        kind=OrderKind.MARKET,
        size=intent.size,
        price=intent.price,
        stop_price=None,
        leverage=intent.leverage,
        isolated=intent.isolated,
        reduce_only=True,
        reason=reason,
        notional_usd=intent.notional_usd,
        extras=extras,
    )
