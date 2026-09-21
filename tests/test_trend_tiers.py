from dataclasses import replace

from hl_bot.config import BotConfig, TrendConfig
from hl_bot.models import AccountState, OrderKind, Position, Regime, Side, Signal, StrategyName
from hl_bot.risk import RiskManager, apply_tier_add
from hl_bot.strategies.trend import TrendStrategy
from tests.candles import H4, START, candle, decision, market
from tests.test_trend import _breakout_h4


def _inside_h4() -> list:
    h4 = _breakout_h4(chase=False)
    last = h4[-1]
    h4[-1] = candle(100.1, open_=100.0, high=101.5, low=99.8, ts=last.ts, interval=H4)
    return h4


def _acct(positions: list[Position] | None = None, equity: float = 10_000) -> AccountState:
    return AccountState(
        equity=equity,
        starting_equity=equity,
        day_start_equity=equity,
        week_start_equity=equity,
        positions=positions or [],
        day_key="2026-09-19",
        week_key="2026-W38",
    )


def _starter_pos(
    *,
    size: float = 0.35,
    entry: float = 100.0,
    stop: float = 80.0,
    intended_full: float = 1.0,
    intended_risk: float = 20.0,
    symbol: str = "BTC",
    side: Side = Side.LONG,
) -> Position:
    return Position(
        symbol=symbol,
        strategy=StrategyName.TREND,
        side=side,
        size=size,
        entry_price=entry,
        stop_price=stop,
        leverage=5,
        opened_ts=START,
        tag="trend_confirmed_starter",
        extras={
            "_opened_size": size,
            "starter": True,
            "starter_frac": 0.35,
            "starter_pending_add": True,
            "intended_full_size": intended_full,
            "intended_risk_usd": intended_risk,
            "tier": "trend_confirmed_starter",
        },
    )


def test_starter_on_trend_long_without_donchian() -> None:
    snap = market("BTC", h4=_inside_h4(), mid=104.0)
    sig = TrendStrategy().generate_signal(snap, decision(Regime.TREND_LONG, daily_adx=28))
    assert sig is not None
    assert sig.tag == "trend_confirmed_starter"
    assert sig.kind is OrderKind.MARKET
    assert sig.side is Side.LONG
    assert sig.entry_price == 104.0
    assert "第一档" in sig.reason
    assert sig.extras["starter"] is True
    assert abs(sig.extras["starter_frac"] - 0.35) < 1e-12
    assert abs(sig.stop_price - (104.0 - 2 * sig.extras["atr"])) < 1e-9


def test_starter_disabled_or_symbol_filtered() -> None:
    snap = market("BTC", h4=_inside_h4(), mid=104.0)
    dec = decision(Regime.TREND_LONG, daily_adx=28)
    assert TrendStrategy(TrendConfig(starter_enabled=False)).generate_signal(snap, dec) is None
    assert TrendStrategy(TrendConfig(starter_symbols=("ETH",))).generate_signal(snap, dec) is None
    allowed = TrendStrategy(TrendConfig(starter_symbols=("BTC",))).generate_signal(snap, dec)
    assert allowed is not None
    assert allowed.tag == "trend_confirmed_starter"


def test_donchian_breakout_still_full_size_not_starter() -> None:
    h4 = _breakout_h4(chase=False)
    snap = market("ETH", h4=h4, mid=h4[-1].close)
    sig = TrendStrategy().generate_signal(snap, decision(Regime.TREND_LONG, daily_adx=28))
    assert sig is not None
    assert sig.tag == "donchian_close_breakout"
    assert sig.kind is OrderKind.MAKER_LIMIT
    assert sig.extras.get("starter") is False


def test_no_second_full_open_when_starter_exists() -> None:
    h4 = _breakout_h4(chase=False)
    snap = market("BTC", h4=h4, mid=h4[-1].close)
    pos = _starter_pos()
    strat = TrendStrategy()
    dec = decision(Regime.TREND_LONG, daily_adx=30)
    assert strat.generate_signal(snap, dec, existing=pos) is None
    full = strat.generate_signal(snap, dec)
    assert full is not None
    assert full.tag == "btc_chase_breakout" or full.tag == "donchian_close_breakout"
    rm = RiskManager(BotConfig())
    verdict = rm.evaluate_open(full, _acct([pos]))
    assert not verdict.allowed
    assert "禁止加仓" in verdict.reason


def test_breakout_adds_remaining_and_shares_stop() -> None:
    h4 = _breakout_h4(chase=False)
    snap = market("ETH", h4=h4, mid=h4[-1].close)
    pos = _starter_pos(symbol="ETH", size=0.35, intended_full=1.0, intended_risk=20.0, stop=80.0)
    old_stop = pos.stop_price
    strat = TrendStrategy()
    sig = strat.generate_breakout_add(pos, snap, decision(Regime.TREND_LONG, daily_adx=30))
    assert sig is not None
    assert sig.tag == "donchian_close_breakout"
    assert sig.extras["tier_add"] is True
    assert sig.stop_price == old_stop
    assert abs(sig.extras["add_size"] - 0.65) < 1e-9

    rm = RiskManager(BotConfig())
    verdict = rm.evaluate_tier_add(sig, pos, _acct([pos]))
    assert verdict.allowed
    assert verdict.size is not None
    intent = rm.to_tier_add_intent(sig, pos, verdict)
    assert intent is not None
    assert intent.stop_price == old_stop
    assert abs(float(intent.extras["current_size"]) - 0.35) < 1e-9
    assert abs(float(intent.extras["total_size"]) - (0.35 + verdict.size.size)) < 1e-9
    assert "第二档" in intent.reason or "donchian_close_breakout" in intent.reason

    apply_tier_add(_acct([pos]), pos, verdict.size.size, sig.entry_price, intent.stop_price or old_stop)
    assert pos.stop_price == old_stop
    assert not pos.starter_pending()
    assert abs(pos.size - (0.35 + verdict.size.size)) < 1e-9
    assert abs(pos.original_size() - pos.size) < 1e-9


def test_starter_risk_scaled_by_frac_and_combined_capped() -> None:
    rm = RiskManager(BotConfig())
    full = Signal(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        entry_price=100.0,
        stop_price=80.0,
        reason="full",
        tag="donchian_close_breakout",
        extras={"size_frac": 1.0},
    )
    starter = replace(
        full,
        kind=OrderKind.MARKET,
        tag="trend_confirmed_starter",
        extras={"starter": True, "starter_frac": 0.35, "size_frac": 0.35},
    )
    full_v = rm.evaluate_open(full, _acct())
    starter_v = rm.evaluate_open(starter, _acct())
    assert full_v.allowed and starter_v.allowed
    assert full_v.size and starter_v.size
    assert abs(starter_v.size.risk_usd / full_v.size.risk_usd - 0.35) < 1e-9
    assert abs(starter_v.size.size / full_v.size.size - 0.35) < 1e-9

    intent = rm.to_open_intent(starter, starter_v)
    assert intent is not None
    pos = _starter_pos(
        symbol="ETH",
        size=starter_v.size.size,
        entry=100.0,
        stop=80.0,
        intended_full=intent.extras["intended_full_size"],
        intended_risk=intent.extras["intended_risk_usd"],
    )
    add_sig = Signal(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        entry_price=100.0,
        stop_price=80.0,
        reason="add",
        tag="donchian_close_breakout",
        extras={
            "tier_add": True,
            "add_size": pos.intended_full_size() - pos.size,
            "intended_full_size": pos.intended_full_size(),
            "shared_stop": True,
        },
    )
    add_v = rm.evaluate_tier_add(add_sig, pos, _acct([pos]))
    assert add_v.allowed and add_v.size
    combined = pos.size + add_v.size.size
    assert abs(combined - full_v.size.size) < 1e-6
    combined_risk = pos.loss_risk_usd() + add_v.size.risk_usd
    assert combined_risk <= full_v.size.risk_usd + 1e-6


def test_breakout_add_keeps_shared_stop_when_entry_higher() -> None:
    pos = _starter_pos(symbol="ETH", size=0.35, intended_full=1.0, intended_risk=0.35 * 20.0, stop=80.0, entry=100.0)
    # intended_risk 7.0 is only starter-sized; remaining room is 0 → should cap/reject or shrink.
    # Use full intended risk 20 so combined stays ≤ original full.
    pos.extras["intended_risk_usd"] = 20.0
    sig = Signal(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        entry_price=110.0,
        stop_price=80.0,
        reason="add",
        tag="donchian_close_breakout",
        extras={"tier_add": True, "add_size": 0.65, "intended_full_size": 1.0, "shared_stop": True},
    )
    rm = RiskManager(BotConfig())
    verdict = rm.evaluate_tier_add(sig, pos, _acct([pos]))
    assert verdict.allowed and verdict.size
    apply_tier_add(_acct([pos]), pos, verdict.size.size, 110.0, 80.0)
    assert pos.stop_price == 80.0
    assert pos.loss_risk_usd() <= 20.0 + 1e-6


def test_no_breakout_add_without_donchian_close() -> None:
    snap = market("ETH", h4=_inside_h4(), mid=104.0)
    pos = _starter_pos(symbol="ETH")
    assert TrendStrategy().generate_breakout_add(pos, snap, decision(Regime.TREND_LONG, daily_adx=30)) is None


def test_open_and_add_logs_include_tier_size_price() -> None:
    from hl_bot.runner import _format_intent

    rm = RiskManager(BotConfig())
    starter = Signal(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MARKET,
        entry_price=100.0,
        stop_price=80.0,
        reason="趋势分层第一档 trend_confirmed_starter：测试",
        tag="trend_confirmed_starter",
        extras={"starter": True, "starter_frac": 0.35, "size_frac": 0.35, "tier": "trend_confirmed_starter"},
    )
    verdict = rm.evaluate_open(starter, _acct())
    intent = rm.to_open_intent(starter, verdict)
    assert intent is not None
    text = _format_intent(intent)
    assert "trend_confirmed_starter" in text
    assert "tier=" in text
    assert "@ 100" in text
    assert str(intent.size)[:4] in text or f"{intent.size:.6g}" in text
    pos = _starter_pos(symbol="ETH")
    from tests.test_pyramid import _wide_h4

    snap = market("ETH", h4=_wide_h4(), mid=120.0)
    assert TrendStrategy().generate_pyramid(pos, snap, decision(Regime.TREND_LONG, ema_fast=120, ema_slow=100, daily_adx=30)) is None


def test_short_starter_respects_asymmetric_rules() -> None:
    bars = [
        candle(100.0, open_=100.0, high=100.2, low=99.8, ts=START + i * H4, interval=H4) for i in range(30)
    ]
    bars.append(candle(99.9, open_=100.0, high=100.2, low=99.85, ts=START + 30 * H4, interval=H4))
    snap = market("BTC", h4=bars, mid=99.9)
    blocked = TrendStrategy().generate_signal(
        snap, decision(Regime.TREND_SHORT, ema_fast=90, ema_slow=110, daily_adx=22)
    )
    allowed = TrendStrategy().generate_signal(
        snap, decision(Regime.TREND_SHORT, ema_fast=90, ema_slow=110, daily_adx=28)
    )
    assert blocked is None
    assert allowed is not None
    assert allowed.tag == "trend_confirmed_starter"
    assert allowed.side is Side.SHORT
    assert allowed.kind is OrderKind.MARKET
    assert TrendStrategy().generate_signal(snap, decision(Regime.TREND_LONG, daily_adx=28)) is not None
    # 多头环境的 inside bar 只能出多头 starter，不能空
    long_only = TrendStrategy().generate_signal(snap, decision(Regime.TREND_LONG, daily_adx=28))
    assert long_only is not None and long_only.side is Side.LONG
