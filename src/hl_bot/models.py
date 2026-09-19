from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    def is_long(self) -> bool:
        return self is Side.LONG

    def opposite(self) -> Side:
        return Side.SHORT if self is Side.LONG else Side.LONG


class Regime(str, Enum):
    TREND_LONG = "trend_long"
    TREND_SHORT = "trend_short"
    MEAN_REVERSION = "mean_reversion"
    WATCH = "watch"

    def is_trend(self) -> bool:
        return self in (Regime.TREND_LONG, Regime.TREND_SHORT)

    def trend_side(self) -> Side | None:
        if self is Regime.TREND_LONG:
            return Side.LONG
        if self is Regime.TREND_SHORT:
            return Side.SHORT
        return None


class StrategyName(str, Enum):
    TREND = "trend"
    MEAN_REVERSION = "mean_reversion"


class OrderKind(str, Enum):
    MARKET = "market"
    MAKER_LIMIT = "maker_limit"
    AGGRESSIVE_LIMIT = "aggressive_limit"


class IntentAction(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    REDUCE = "reduce"


@dataclass(frozen=True, slots=True)
class Candle:
    ts: int
    end_ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    def is_bullish(self) -> bool:
        return self.close >= self.open

    def is_hammer(self) -> bool:
        return self.lower_wick >= 2.0 * max(self.body, 1e-12) and self.upper_wick <= max(self.body, 1e-12)

    def is_shooting_star(self) -> bool:
        return self.upper_wick >= 2.0 * max(self.body, 1e-12) and self.lower_wick <= max(self.body, 1e-12)


@dataclass(frozen=True, slots=True)
class FundingInfo:
    hourly_rate: float
    avg_24h: float

    @property
    def annualized(self) -> float:
        return self.hourly_rate * 24.0 * 365.0

    @property
    def annualized_24h(self) -> float:
        return self.avg_24h * 24.0 * 365.0


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    symbol: str
    mid: float
    daily: tuple[Candle, ...]
    h4: tuple[Candle, ...]
    h1: tuple[Candle, ...]
    funding: FundingInfo
    max_leverage: int = 20


@dataclass(frozen=True, slots=True)
class RangeStructure:
    is_range: bool
    high: float
    low: float
    touched_high: bool
    touched_low: bool
    new_extreme: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RegimeDecision:
    regime: Regime
    daily_ema_fast: float | None
    daily_ema_slow: float | None
    daily_adx: float | None
    hourly_adx: float | None
    range_structure: RangeStructure | None
    reason: str
    conflict: bool = False


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    strategy: StrategyName
    side: Side
    kind: OrderKind
    entry_price: float
    stop_price: float
    reason: str
    tag: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def stop_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return self.stop_distance / self.entry_price


@dataclass(frozen=True, slots=True)
class PositionSize:
    notional_usd: float
    size: float
    leverage: int
    margin_usd: float
    risk_usd: float
    risk_pct: float
    stop_pct: float
    capped_by_max_leverage: bool = False


@dataclass(frozen=True, slots=True)
class OrderIntent:
    action: IntentAction
    symbol: str
    strategy: StrategyName
    side: Side
    kind: OrderKind
    size: float
    price: float
    stop_price: float | None
    leverage: int
    isolated: bool
    reduce_only: bool
    reason: str
    risk_usd: float = 0.0
    notional_usd: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    def with_size(self, size: float, notional: float | None = None) -> OrderIntent:
        return replace(
            self,
            size=size,
            notional_usd=self.price * size if notional is None else notional,
        )


@dataclass
class Position:
    symbol: str
    strategy: StrategyName
    side: Side
    size: float
    entry_price: float
    stop_price: float
    leverage: int
    opened_ts: int
    remaining_frac: float = 1.0
    tag: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def notional_usd(self) -> float:
        return abs(self.size) * self.entry_price

    def unrealized_pnl(self, price: float) -> float:
        direction = 1.0 if self.side is Side.LONG else -1.0
        return direction * (price - self.entry_price) * abs(self.size)


@dataclass
class AccountState:
    equity: float
    starting_equity: float
    day_start_equity: float
    week_start_equity: float
    positions: list[Position] = field(default_factory=list)
    day_trades: dict[str, int] = field(default_factory=dict)
    realized_pnl_today: float = 0.0
    day_key: str = ""
    week_key: str = ""

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.size > 0]

    def position_for(self, symbol: str) -> Position | None:
        for p in self.open_positions():
            if p.symbol == symbol:
                return p
        return None

    def daily_loss_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return max(0.0, (self.day_start_equity - self.equity) / self.day_start_equity)

    def weekly_loss_pct(self) -> float:
        if self.week_start_equity <= 0:
            return 0.0
        return max(0.0, (self.week_start_equity - self.equity) / self.week_start_equity)

    def open_risk_usd(self) -> float:
        total = 0.0
        for p in self.open_positions():
            total += abs(p.entry_price - p.stop_price) * abs(p.size)
        return total
