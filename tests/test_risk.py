from hl_bot.config import BotConfig
from hl_bot.models import AccountState, Position, Side, Signal, StrategyName
from hl_bot.risk import RiskManager, derive_position_size


def _account(equity: float = 10_000, positions: list[Position] | None = None) -> AccountState:
    return AccountState(
        equity=equity,
        starting_equity=equity,
        day_start_equity=equity,
        week_start_equity=equity,
        positions=positions or [],
        day_key="2026-09-19",
        week_key="2026-W38",
    )


def _signal(
    symbol: str = "ETH",
    *,
    side: Side = Side.LONG,
    strategy: StrategyName = StrategyName.TREND,
    entry: float = 100,
    stop: float = 94,
) -> Signal:
    from hl_bot.models import OrderKind

    return Signal(
        symbol=symbol,
        strategy=strategy,
        side=side,
        kind=OrderKind.MAKER_LIMIT,
        entry_price=entry,
        stop_price=stop,
        reason="test",
    )


def test_size_is_risk_over_stop_distance() -> None:
    sized = derive_position_size(equity=10_000, risk_pct=0.01, entry=100, stop=98, max_leverage=50)
    assert sized is not None
    # stop 2% → notional = 100 / 0.02 = 5000，杠杆 = 1/0.02 = 50
    assert abs(sized.notional_usd - 5000) < 1e-6
    assert sized.size == 50
    assert sized.leverage == 50
    assert abs(sized.risk_usd - 100) < 1e-6


def test_leverage_is_derived_and_capped() -> None:
    sized = derive_position_size(equity=10_000, risk_pct=0.01, entry=100, stop=99, max_leverage=20)
    assert sized is not None
    # stop 1% → raw lev 100, cap 20, 名义仍按风险反推
    assert sized.capped_by_max_leverage
    assert sized.leverage == 20
    assert abs(sized.notional_usd - 10_000 * 0.01 / 0.01) < 1e-6
    assert abs(sized.margin_usd - sized.notional_usd / 20) < 1e-6


def test_below_min_notional_rejected() -> None:
    assert derive_position_size(equity=100, risk_pct=0.001, entry=100, stop=50, max_leverage=5, min_notional=10) is None


def test_btc_short_uses_70_percent_risk() -> None:
    rm = RiskManager(BotConfig())
    long_v = rm.evaluate_open(_signal("BTC", side=Side.LONG, entry=100, stop=94), _account())
    short_v = rm.evaluate_open(_signal("BTC", side=Side.SHORT, entry=100, stop=106), _account())
    assert long_v.allowed and short_v.allowed
    assert long_v.size and short_v.size
    assert abs(short_v.size.risk_pct / long_v.size.risk_pct - 0.7) < 1e-9


def test_rejects_averaging_same_symbol() -> None:
    pos = Position(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        size=1,
        entry_price=100,
        stop_price=94,
        leverage=5,
        opened_ts=0,
    )
    rm = RiskManager(BotConfig())
    verdict = rm.evaluate_open(_signal("ETH"), _account(positions=[pos]))
    assert not verdict.allowed
    assert "禁止加仓" in verdict.reason


def test_mr_daily_loss_halt() -> None:
    acct = _account(10_000)
    acct.day_start_equity = 10_000
    acct.equity = 9_600  # -4%
    rm = RiskManager(BotConfig())
    verdict = rm.evaluate_open(_signal("SOL", strategy=StrategyName.MEAN_REVERSION), acct)
    assert not verdict.allowed
    assert "停止新开仓" in verdict.reason


def test_trend_portfolio_risk_cap() -> None:
    existing = [
        Position(
            symbol=sym,
            strategy=StrategyName.TREND,
            side=Side.LONG,
            size=10,  # 每仓止损距离 6 → 风险 60，三仓已 180 > 6% of 10k=600? 10*6=60, two=120
            entry_price=100,
            stop_price=94,
            leverage=5,
            opened_ts=0,
        )
        for sym in ("ETH", "SOL")
    ]
    # 再开 BTC 风险约 150，合计超过 6%? 120+150=270 < 600。把 size 加大
    existing = [
        Position(
            symbol=sym,
            strategy=StrategyName.TREND,
            side=Side.LONG,
            size=50,
            entry_price=100,
            stop_price=94,
            leverage=5,
            opened_ts=0,
        )
        for sym in ("ETH", "SOL")
    ]
    # 每仓风险 300，两仓 600 == 6%，再开应拒绝
    rm = RiskManager(BotConfig())
    verdict = rm.evaluate_open(_signal("BTC", entry=100, stop=94), _account(positions=existing))
    assert not verdict.allowed
    assert "6%" in verdict.reason


def test_mr_max_two_positions() -> None:
    positions = [
        Position(
            symbol=sym,
            strategy=StrategyName.MEAN_REVERSION,
            side=Side.LONG,
            size=1,
            entry_price=100,
            stop_price=98,
            leverage=3,
            opened_ts=0,
        )
        for sym in ("BTC", "ETH")
    ]
    rm = RiskManager(BotConfig())
    verdict = rm.evaluate_open(_signal("SOL", strategy=StrategyName.MEAN_REVERSION, entry=100, stop=98), _account(positions=positions))
    assert not verdict.allowed
