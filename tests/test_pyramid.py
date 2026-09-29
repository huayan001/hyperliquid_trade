from hl_bot.config import BotConfig, TrendConfig
from hl_bot.models import AccountState, Candle, Position, Regime, Side, StrategyName
from hl_bot.risk import RiskManager, apply_pyramid
from hl_bot.strategies.trend import TrendStrategy
from tests.candles import H4, START, decision, market


def _wide_h4(n: int = 30, atr_span: float = 10.0) -> list[Candle]:
    """高低差约 atr_span，便于把 ATR 稳定在 ~10。"""
    out: list[Candle] = []
    for i in range(n):
        ts = START + i * H4
        out.append(
            Candle(
                ts=ts,
                end_ts=ts + H4 - 1,
                open=100.0,
                high=100.0 + atr_span / 2,
                low=100.0 - atr_span / 2,
                close=100.0,
                volume=10.0,
            )
        )
    return out


def _pos(
    *,
    size: float = 2.0,
    entry: float = 100.0,
    stop: float = 80.0,
    tag: str = "donchian_close_breakout",
    extras: dict | None = None,
) -> Position:
    extra = {"_opened_size": size, "chase": False}
    if extras:
        extra.update(extras)
    return Position(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        size=size,
        entry_price=entry,
        stop_price=stop,
        leverage=5,
        opened_ts=START,
        tag=tag,
        extras=extra,
    )


def _acct(pos: Position, equity: float = 10_000) -> AccountState:
    return AccountState(
        equity=equity,
        starting_equity=equity,
        day_start_equity=equity,
        week_start_equity=equity,
        positions=[pos],
        day_key="2026-09-19",
        week_key="2026-W38",
    )


def _confirmed():
    return decision(Regime.TREND_LONG, ema_fast=120, ema_slow=100, daily_adx=30)


def test_pyramid_skipped_when_profit_below_one_atr() -> None:
    strat = TrendStrategy()
    pos = _pos()
    snap = market("ETH", h4=_wide_h4(), mid=105.0)  # +0.5×ATR if ATR~10
    assert strat.generate_pyramid(pos, snap, _confirmed()) is None


def test_pyramid_adds_at_most_half_when_profit_reaches_atr() -> None:
    strat = TrendStrategy()
    pos = _pos(size=2.0)
    old_risk = pos.loss_risk_usd()
    snap = market("ETH", h4=_wide_h4(), mid=111.0)
    sig = strat.generate_pyramid(pos, snap, _confirmed())
    assert sig is not None
    assert sig.tag == "pyramid_add"
    assert sig.extras["add_size"] <= 2.0 * 0.5 + 1e-9
    assert sig.extras["add_size"] == 1.0
    # 加权均价 (2*100+1*111)/3 = 103.666，止损应在其上方
    assert sig.stop_price > sig.extras["avg_entry"]
    rm = RiskManager(BotConfig())
    verdict = rm.evaluate_pyramid(sig, pos, _acct(pos))
    assert verdict.allowed
    assert verdict.size is not None
    assert verdict.size.size <= 1.0 + 1e-9
    apply_pyramid(_acct(pos), pos, verdict.size.size, sig.entry_price, sig.stop_price)
    assert pos.size == 3.0
    assert pos.pyramid_added_frac() <= 0.5 + 1e-9
    assert pos.loss_risk_usd() <= old_risk


def test_pyramid_skips_chase_entry() -> None:
    strat = TrendStrategy(TrendConfig(pyramid_skip_chase=True))
    pos = _pos(tag="btc_chase_breakout", extras={"chase": True, "_opened_size": 2.0})
    snap = market("BTC", h4=_wide_h4(), mid=120.0)
    pos.symbol = "BTC"
    assert strat.generate_pyramid(pos, snap, _confirmed()) is None


def test_pyramid_rejects_losing_position() -> None:
    strat = TrendStrategy()
    pos = _pos()
    snap = market("ETH", h4=_wide_h4(), mid=95.0)
    assert strat.generate_pyramid(pos, snap, _confirmed()) is None
    # 即使硬塞信号，风控也要拦马丁
    sig = strat.generate_pyramid(_pos(), market("ETH", h4=_wide_h4(), mid=111.0), _confirmed())
    assert sig is not None
    losing = _pos()
    rm = RiskManager(BotConfig())
    # 把加仓价改到成本以下
    from dataclasses import replace

    bad = replace(sig, entry_price=90.0)
    verdict = rm.evaluate_pyramid(bad, losing, _acct(losing))
    assert not verdict.allowed
    assert "摊平" in verdict.reason


def test_pyramid_rejects_when_already_added_max() -> None:
    strat = TrendStrategy()
    pos = _pos(extras={"_opened_size": 2.0, "pyramid_added_frac": 0.5})
    snap = market("ETH", h4=_wide_h4(), mid=120.0)
    assert strat.generate_pyramid(pos, snap, _confirmed()) is None


def test_evaluate_open_still_blocks_duplicate() -> None:
    rm = RiskManager(BotConfig())
    pos = _pos()
    from tests.test_risk import _signal

    verdict = rm.evaluate_open(_signal("ETH"), _acct(pos))
    assert not verdict.allowed
    assert "禁止加仓" in verdict.reason
