from __future__ import annotations

from collections.abc import Sequence

from hl_bot.models import (
    Candle,
    IntentAction,
    MarketSnapshot,
    OrderIntent,
    Position,
    RegimeDecision,
    Signal,
)


class Strategy:
    name: str = "base"

    def generate_signal(
        self,
        market: MarketSnapshot,
        decision: RegimeDecision,
        now_ms: int | None = None,
    ) -> Signal | None:
        raise NotImplementedError

    def generate_exits(
        self,
        position: Position,
        market: MarketSnapshot,
        decision: RegimeDecision,
        now_ms: int,
    ) -> list[OrderIntent]:
        return []


def last_closed(candles: Sequence[Candle], now_ms: int | None = None) -> list[Candle]:
    if now_ms is None:
        return list(candles)
    return [c for c in candles if c.end_ts < now_ms]


def make_close_intent(
    position: Position,
    price: float,
    reason: str,
    *,
    size: float | None = None,
    kind_market: bool = True,
) -> OrderIntent:
    from hl_bot.models import OrderKind, StrategyName

    qty = position.size if size is None else min(size, position.size)
    action = IntentAction.CLOSE if qty >= position.size - 1e-12 else IntentAction.REDUCE
    return OrderIntent(
        action=action,
        symbol=position.symbol,
        strategy=position.strategy if isinstance(position.strategy, StrategyName) else position.strategy,
        side=position.side.opposite(),
        kind=OrderKind.MARKET if kind_market else OrderKind.MAKER_LIMIT,
        size=qty,
        price=price,
        stop_price=None,
        leverage=position.leverage,
        isolated=True,
        reduce_only=True,
        reason=reason,
        notional_usd=qty * price,
    )
