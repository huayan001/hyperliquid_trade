from hl_bot.config import BotConfig
from hl_bot.funding import (
    adverse_annualized,
    funding_against,
    funding_exit_enabled_for,
    make_funding_exit_intent,
    should_exit_on_funding_spike,
)
from hl_bot.models import FundingInfo, IntentAction, Position, Side, StrategyName
from hl_bot.runner import BotRunner
from tests.candles import decision, market
from hl_bot.models import Regime


def _pos(*, strategy: StrategyName = StrategyName.TREND, side: Side = Side.LONG) -> Position:
    return Position(
        symbol="ETH",
        strategy=strategy,
        side=side,
        size=1.0,
        entry_price=100.0,
        stop_price=90.0,
        leverage=5,
        opened_ts=1_700_000_000_000,
        extras={"_opened_size": 1.0},
    )


def test_long_pays_when_funding_positive() -> None:
    assert funding_against(Side.LONG, 0.0001)
    assert not funding_against(Side.LONG, -0.0001)
    assert funding_against(Side.SHORT, -0.0001)
    assert not funding_against(Side.SHORT, 0.0001)


def test_high_funding_triggers_trend_exit() -> None:
    # 0.02%/h → 年化约 175%
    funding = FundingInfo(hourly_rate=0.0002, avg_24h=0.00015)
    assert adverse_annualized(Side.LONG, funding) > 0.5
    ok, reason = should_exit_on_funding_spike(_pos(), funding, threshold=0.50, enabled=True)
    assert ok
    assert "资金费率异常飙升" in reason
    intent = make_funding_exit_intent(_pos(), 110.0, reason)
    assert intent.action is IntentAction.CLOSE
    assert intent.reduce_only
    assert intent.extras.get("funding_exit") is True


def test_receiving_funding_does_not_exit() -> None:
    funding = FundingInfo(hourly_rate=-0.0002, avg_24h=-0.0002)
    ok, _ = should_exit_on_funding_spike(_pos(), funding, threshold=0.50, enabled=True)
    assert not ok


def test_below_threshold_does_not_exit() -> None:
    # 0.001%/h → 年化约 8.8%
    funding = FundingInfo(hourly_rate=0.00001, avg_24h=0.00001)
    ok, _ = should_exit_on_funding_spike(_pos(), funding, threshold=0.50, enabled=True)
    assert not ok


def test_mr_funding_exit_off_by_default() -> None:
    pos = _pos(strategy=StrategyName.MEAN_REVERSION)
    assert not funding_exit_enabled_for(pos, trend_enabled=True, mr_enabled=False)
    funding = FundingInfo(0.0002, 0.0002)
    ok, _ = should_exit_on_funding_spike(
        pos, funding, threshold=0.50, enabled=funding_exit_enabled_for(pos, True, False)
    )
    assert not ok


def test_runner_scan_exits_on_high_funding(tmp_path) -> None:
    cfg = BotConfig()
    cfg.state_path = str(tmp_path / "paper.json")
    runner = BotRunner(cfg)
    runner.account.positions.append(_pos())
    snap = market("ETH", mid=110.0, funding=0.0002)
    exits = runner._exits_for(snap, decision(Regime.TREND_LONG, daily_adx=30), now_ms=1_700_000_000_000)
    assert exits
    assert "资金费" in exits[0].reason
    assert exits[0].extras.get("funding_exit") is True
