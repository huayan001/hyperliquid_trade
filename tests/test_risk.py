import logging

from hl_bot.config import BotConfig
from hl_bot.models import AccountState, IntentAction, OrderIntent, OrderKind, Position, Side, Signal, StrategyName
from hl_bot.risk import RiskManager, derive_position_size
from hl_bot.runner import format_report


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


def test_fixed_target_leverage_10x() -> None:
    """名义仍按风险/止损反推，但杠杆固定为目标 10x，不再随止损距离涨到 50。"""
    sized = derive_position_size(
        equity=10_000,
        risk_pct=0.02,
        entry=100,
        stop=94,
        max_leverage=10,
        target_leverage=10,
    )
    assert sized is not None
    assert sized.leverage == 10
    # stop 6% → notional = 200 / 0.06 ≈ 3333.33
    assert abs(sized.notional_usd - (10_000 * 0.02 / 0.06)) < 1e-6
    assert abs(sized.risk_usd - 200) < 1e-6
    assert abs(sized.margin_usd - sized.notional_usd / 10) < 1e-6


def test_stop_clamped_inside_liq_buffer() -> None:
    from hl_bot.risk import clamp_stop_for_leverage

    # 10x × 0.5 buffer → 最大止损距离 5%；策略 8% 应被收紧
    stop, tightened = clamp_stop_for_leverage(
        side=Side.LONG, entry=100, stop=92, leverage=10, buffer_frac=0.5
    )
    assert tightened
    assert abs(stop - 95.0) < 1e-9


def test_evaluate_open_uses_2pct_and_10x() -> None:
    cfg = BotConfig()
    cfg.trend.risk_pct = 0.02
    cfg.risk.target_leverage = 10
    cfg.risk.max_leverage = {"ETH": 10, "BTC": 10, "SOL": 10, "HYPE": 10}
    rm = RiskManager(cfg)
    verdict = rm.evaluate_open(_signal("ETH", entry=100, stop=97), _account(10_000))
    assert verdict.allowed and verdict.size
    assert verdict.size.leverage == 10
    assert abs(verdict.size.risk_usd - 200) < 1e-6


def test_evaluate_open_tightens_wide_stop_before_sizing() -> None:
    cfg = BotConfig()
    cfg.trend.risk_pct = 0.02
    cfg.risk.target_leverage = 10
    cfg.risk.liq_buffer_frac = 0.5
    cfg.risk.max_leverage = {"ETH": 10}
    rm = RiskManager(cfg)
    # 止损 12% 超出 5% 缓冲 → 收到 5%，名义按收紧后距离计
    verdict = rm.evaluate_open(_signal("ETH", entry=100, stop=88), _account(10_000))
    assert verdict.allowed and verdict.size
    assert verdict.adjusted_stop is not None
    assert abs(verdict.adjusted_stop - 95.0) < 1e-9
    intent = rm.to_open_intent(_signal("ETH", entry=100, stop=88), verdict)
    assert intent is not None
    assert abs(intent.stop_price - 95.0) < 1e-9
    assert intent.leverage == 10


def _trend_cfg(*, halt: float = 0.0, max_positions: int = 4) -> BotConfig:
    cfg = BotConfig()
    cfg.trend.daily_loss_halt = halt
    cfg.trend.max_positions = max_positions
    cfg.trend.portfolio_risk_max = 0.04
    cfg.trend.risk_pct = 0.01
    cfg.risk.target_leverage = 10
    cfg.risk.liq_buffer_frac = 0.5
    return cfg


def _one_pct_position(symbol: str, *, equity: float = 10_000) -> Position:
    """止损距离 5%，仓位风险正好是权益的 1%。"""
    entry, stop = 100.0, 95.0
    size = (equity * 0.01) / (entry - stop)
    return Position(
        symbol=symbol,
        strategy=StrategyName.TREND,
        side=Side.LONG,
        size=size,
        entry_price=entry,
        stop_price=stop,
        leverage=10,
        opened_ts=0,
    )


def _loss_account(loss_pct: float, positions: list[Position] | None = None, equity: float = 10_000) -> AccountState:
    acct = _account(equity, positions)
    acct.day_start_equity = equity
    acct.equity = equity * (1.0 - loss_pct)
    return acct


def test_trend_daily_loss_halt_blocks_new_entry_at_threshold() -> None:
    rm = RiskManager(_trend_cfg(halt=0.03))
    verdict = rm.evaluate_open(_signal("ETH", entry=100, stop=95), _loss_account(0.03))
    assert not verdict.allowed
    assert verdict.reason == "趋势策略：当日亏损已达 3% 上限，停止新开仓"
    # 趋势停开仓不套到均值回归；均值回归仍只用自己的 daily_loss_halt
    cfg = _trend_cfg(halt=0.03)
    cfg.mean_reversion.daily_loss_halt = 0.10
    mr = RiskManager(cfg).evaluate_open(
        _signal("SOL", strategy=StrategyName.MEAN_REVERSION, entry=100, stop=98),
        _loss_account(0.03),
    )
    assert mr.allowed, mr.reason


def test_trend_daily_loss_halt_allows_below_threshold() -> None:
    rm = RiskManager(_trend_cfg(halt=0.03))
    verdict = rm.evaluate_open(_signal("ETH", entry=100, stop=95), _loss_account(0.029))
    assert verdict.allowed, verdict.reason


def test_trend_daily_loss_halt_zero_disables() -> None:
    rm = RiskManager(_trend_cfg(halt=0.0))
    # 4% 高于配置里的 3% 停开仓，但仍低于 5% 降杠杆，用来证明 halt=0 不拦截
    verdict = rm.evaluate_open(_signal("ETH", entry=100, stop=95), _loss_account(0.04))
    assert verdict.allowed, verdict.reason
    assert verdict.size is not None
    # 权益已是 9600，单笔风险仍是当前权益的 1%，只是没有被 halt 拦住
    assert abs(verdict.size.risk_usd - 96) < 1e-6
    assert abs(verdict.size.risk_pct - 0.01) < 1e-9


def test_trend_four_positions_fit_portfolio_cap_and_fifth_rejected() -> None:
    cfg = _trend_cfg(halt=0.0, max_positions=4)
    rm = RiskManager(cfg)
    third = rm.evaluate_open(
        _signal("BTC", entry=100, stop=95),
        _loss_account(0.0, [_one_pct_position("ETH"), _one_pct_position("SOL")]),
    )
    assert third.allowed, third.reason
    assert third.size is not None
    assert abs(third.size.risk_usd - 100) < 1e-6

    fourth = rm.evaluate_open(
        _signal("BTC", entry=100, stop=95),
        _loss_account(0.0, [_one_pct_position(s) for s in ("ETH", "SOL", "HYPE")]),
    )
    assert fourth.allowed, fourth.reason
    assert fourth.size is not None
    assert abs(fourth.size.risk_usd - 100) < 1e-6

    full = [_one_pct_position(s) for s in ("ETH", "SOL", "HYPE", "BTC")]
    fifth = rm.evaluate_open(_signal("DOGE", entry=100, stop=95), _loss_account(0.0, full))
    assert not fifth.allowed
    assert "持仓数已满" in fifth.reason

    # 即使把仓位数上限放开，第 5 笔 1% 也会顶破 4% 组合风险
    cfg.trend.max_positions = 5
    fifth_by_cap = rm.evaluate_open(_signal("DOGE", entry=100, stop=95), _loss_account(0.0, full))
    assert not fifth_by_cap.allowed
    assert "4%" in fifth_by_cap.reason


def test_runner_logs_trend_halt_without_blocking_exit(tmp_path, monkeypatch, caplog) -> None:
    from tests.test_btc_regime import _arm_open, _runner

    runner, fetches, executed = _runner(tmp_path, monkeypatch, mode="shadow", symbols=("ETH", "SOL"))
    runner.cfg.trend.daily_loss_halt = 0.03
    runner.account.day_start_equity = 10_000
    runner.account.equity = 9_700
    _arm_open(runner, starter=False)
    runner.account.positions.append(
        Position(
            symbol="SOL",
            strategy=StrategyName.TREND,
            side=Side.LONG,
            size=1.0,
            entry_price=100.0,
            stop_price=90.0,
            leverage=10,
            opened_ts=1,
            tag="donchian_close_breakout",
            extras={"_opened_size": 1.0},
        )
    )
    close = OrderIntent(
        action=IntentAction.CLOSE,
        symbol="SOL",
        strategy=StrategyName.TREND,
        side=Side.SHORT,
        kind=OrderKind.MARKET,
        size=1.0,
        price=100.0,
        stop_price=None,
        leverage=10,
        isolated=True,
        reduce_only=True,
        reason="趋势失效离场",
    )

    def exits(position, market, decision, now_ms):
        if market.symbol == "SOL":
            return [close]
        return []

    runner.trend.generate_exits = exits  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="hl_bot.runner"):
        report = runner.scan_once()

    opens = [item for item in executed if item.action is IntentAction.OPEN]
    closes = [item for item in executed if item.action is IntentAction.CLOSE]
    assert opens == []
    assert closes == [close]
    assert fetches == []
    eth = next(item for item in report.symbols if item.symbol == "ETH")
    assert any("停止新开仓" in note for note in eth.notes)
    assert any("ETH 新开仓跳过" in rec.message and "停止新开仓" in rec.message for rec in caplog.records)
    assert any("停止新开仓与金字塔加仓" in line for line in report.status)
    printed = format_report(report)
    assert "趋势策略：当日亏损已达 3% 上限" in printed
    assert "停止新开仓与金字塔加仓" in printed
