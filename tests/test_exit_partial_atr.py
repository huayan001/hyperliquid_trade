"""方案 B（partial_atr）出场：分批止盈、止损冻结、方案 A 仍可切回。"""

from __future__ import annotations

from hl_bot.config import BotConfig, TrendConfig, load_config
from hl_bot.exchange.client import ExchangePosition
from hl_bot.exchange.paper import PaperBroker
from hl_bot.models import AccountState, IntentAction, OrderIntent, OrderKind, Position, Side, StrategyName
from hl_bot.risk import RiskManager, apply_pyramid
from hl_bot.strategies.trend import TrendStrategy
from tests.candles import DAY, H4, START, market, series_from_closes
from tests.test_fill_sync_cooldown import (
    Exch,
    _ok_resting,
    _pos as _live_pos,
    _rec,
    _runner,
    _stop_row,
    _stops,
)
from tests.test_pyramid import _acct, _confirmed, _pos, _wide_h4


def _flat_daily() -> list:
    return series_from_closes([100.0] * 10, interval=DAY)


def _trend_pos(
    *,
    side: Side = Side.LONG,
    entry: float = 100.0,
    stop: float = 80.0,
    size: float = 2.0,
    extras: dict | None = None,
) -> Position:
    extra = {"_opened_size": size, "tier_add_done": True, "atr": 10.0}
    if extras:
        extra.update(extras)
    return Position(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=side,
        size=size,
        entry_price=entry,
        stop_price=stop,
        leverage=5,
        opened_ts=START,
        tag="donchian_close_breakout",
        extras=extra,
    )


def _snap(mid: float, *, symbol: str = "ETH"):
    return market(symbol, h4=_wide_h4(), daily=_flat_daily(), mid=mid)


def _exits(pos: Position, mid: float, cfg: TrendConfig | None = None):
    strat = TrendStrategy(cfg)
    now = START + 40 * H4
    # 日线不足 EMA 周期，失效检查跳过；这里只测出场价和止损。
    return strat, strat.generate_exits(pos, _snap(mid), _confirmed(), now)


def test_partial_atr_closes_half_at_one_atr_and_keeps_stop() -> None:
    pos = _trend_pos()
    strat, exits = _exits(pos, 110.0)
    assert strat.partial_atr_enabled()
    assert len(exits) == 1
    intent = exits[0]
    assert intent.action is IntentAction.REDUCE
    assert abs(intent.size - 1.0) < 1e-9
    assert intent.extras["partial_atr_level"] == 1
    assert "trail_update_only" not in intent.extras
    assert pos.stop_price == 80.0
    assert abs(float(pos.extras["entry_atr"]) - 10.0) < 1e-9
    assert pos.extras["partial_atr_stop_frozen"] is True

    acct = AccountState(10_000, 10_000, 10_000, 10_000, positions=[pos], day_key="2026-10-25", week_key="2026-W43")
    PaperBroker(acct).submit(intent, START)
    assert abs(pos.size - 1.0) < 1e-9
    assert pos.extras["partial_atr_done"] is True
    assert pos.stop_price == 80.0

    _, again = _exits(pos, 115.0)
    assert again == []
    assert pos.stop_price == 80.0


def test_partial_atr_closes_rest_at_two_atr() -> None:
    pos = _trend_pos(extras={"entry_atr": 10.0, "partial_atr_done": True, "partial_atr_size_after": 1.0})
    pos.size = 1.0
    _, exits = _exits(pos, 120.0)
    assert len(exits) == 1
    assert exits[0].action is IntentAction.CLOSE
    assert abs(exits[0].size - 1.0) < 1e-9
    assert exits[0].extras["partial_atr_level"] == 2
    assert pos.stop_price == 80.0


def test_partial_atr_gap_through_both_targets_closes_all() -> None:
    pos = _trend_pos()
    _, exits = _exits(pos, 130.0)
    assert len(exits) == 1
    assert exits[0].action is IntentAction.CLOSE
    assert abs(exits[0].size - 2.0) < 1e-9
    assert exits[0].extras["partial_atr_level"] == 2
    assert pos.stop_price == 80.0
    assert "止损" not in exits[0].reason


def test_partial_take_profit_does_not_start_stop_cooldown(tmp_path) -> None:
    pos = _trend_pos()
    _, exits = _exits(pos, 130.0)
    runner = _runner(tmp_path)
    runner._maybe_record_bot_stop(exits[0], pos, 130.0, {"pnl": 40.0}, START)
    assert runner.account.last_stops == {}


def test_partial_atr_short_uses_frozen_entry_atr_from_average() -> None:
    pos = _trend_pos(side=Side.SHORT, entry=100.0, stop=120.0)
    _, first = _exits(pos, 90.0)
    assert len(first) == 1
    assert abs(first[0].size - 1.0) < 1e-9
    assert pos.stop_price == 120.0
    acct = AccountState(10_000, 10_000, 10_000, 10_000, positions=[pos], day_key="2026-10-25", week_key="2026-W43")
    PaperBroker(acct).submit(first[0], START)
    # 加仓后均价上移，2× 从新均价量，止损仍是初始空头止损
    apply_pyramid(acct, pos, 1.0, 95.0, pos.stop_price)
    assert pos.stop_price == 120.0
    assert pos.entry_price > 90.0
    _, second = _exits(pos, pos.entry_price - 20.0)
    assert len(second) == 1
    assert second[0].action is IntentAction.CLOSE
    assert pos.stop_price == 120.0


def test_below_one_atr_does_not_partial_or_trail() -> None:
    pos = _trend_pos()
    _, exits = _exits(pos, 109.0)
    assert exits == []
    assert pos.stop_price == 80.0


def test_trail_mode_still_ratchets_and_skips_partial() -> None:
    pos = _trend_pos()
    cfg = TrendConfig(exit_mode="trail")
    _, exits = _exits(pos, 110.0, cfg)
    assert len(exits) == 1
    assert exits[0].extras.get("trail_update_only") is True
    assert "partial_atr_level" not in exits[0].extras
    assert abs(pos.stop_price - 85.0) < 1e-9


def test_trail_mode_pyramid_lifts_stop_to_breakeven() -> None:
    cfg = TrendConfig(exit_mode="trail")
    strat = TrendStrategy(cfg)
    pos = _pos(size=2.0)
    old_risk = pos.loss_risk_usd()
    sig = strat.generate_pyramid(pos, market("ETH", h4=_wide_h4(), mid=111.0), _confirmed())
    assert sig is not None
    assert sig.stop_price > sig.extras["avg_entry"]
    bot = BotConfig()
    bot.trend.exit_mode = "trail"
    verdict = RiskManager(bot).evaluate_pyramid(sig, pos, _acct(pos))
    assert verdict.allowed and verdict.size is not None
    apply_pyramid(_acct(pos), pos, verdict.size.size, sig.entry_price, sig.stop_price)
    assert pos.loss_risk_usd() <= old_risk + 1e-9


def test_partial_pyramid_rejected_only_by_portfolio_cap() -> None:
    strat = TrendStrategy()
    pos = _pos(size=2.0)
    sig = strat.generate_pyramid(pos, market("ETH", h4=_wide_h4(), mid=111.0), _confirmed())
    assert sig is not None
    assert sig.stop_price == 80.0
    bot = BotConfig()
    small = _acct(pos, equity=1_000)
    verdict = RiskManager(bot).evaluate_pyramid(sig, pos, small)
    assert not verdict.allowed
    assert "组合" in verdict.reason


def test_partial_then_pyramid_still_counts_added_fraction() -> None:
    pos = _trend_pos()
    _, exits = _exits(pos, 110.0)
    acct = AccountState(10_000, 10_000, 10_000, 10_000, positions=[pos], day_key="2026-10-25", week_key="2026-W43")
    PaperBroker(acct).submit(exits[0], START)
    assert abs(pos.size - 1.0) < 1e-9
    apply_pyramid(acct, pos, 1.0, 112.0, pos.stop_price)
    assert abs(pos.pyramid_added_frac() - 0.5) < 1e-9
    assert pos.stop_price == 80.0
    strat = TrendStrategy()
    assert strat.generate_pyramid(pos, _snap(130.0), _confirmed()) is None


def test_migration_prefers_stored_atr_and_freezes_current_stop() -> None:
    pos = _trend_pos(stop=88.0, extras={"atr": 8.0})
    pos.extras.pop("entry_atr", None)
    TrendStrategy().ensure_partial_atr_snapshot(pos, fallback_atr=3.0)
    assert abs(float(pos.extras["entry_atr"]) - 8.0) < 1e-9
    assert pos.extras["entry_atr_source"] == "open_signal"
    assert abs(float(pos.extras["initial_stop"]) - 88.0) < 1e-9
    assert pos.extras["initial_stop_source"] == "frozen_current"
    assert pos.stop_price == 88.0


def test_migration_infers_atr_from_protective_stop() -> None:
    pos = _trend_pos(stop=80.0)
    pos.extras.pop("atr", None)
    pos.extras.pop("entry_atr", None)
    TrendStrategy().ensure_partial_atr_snapshot(pos)
    assert pos.extras["entry_atr_source"] == "inferred_from_frozen_stop"
    assert abs(float(pos.extras["entry_atr"]) - 10.0) < 1e-9
    assert pos.stop_price == 80.0


def test_migration_uses_live_atr_when_stop_already_at_breakeven() -> None:
    pos = _trend_pos(entry=100.0, stop=100.5)
    pos.extras.pop("atr", None)
    pos.extras.pop("entry_atr", None)
    TrendStrategy().ensure_partial_atr_snapshot(pos, fallback_atr=7.0)
    assert pos.extras["entry_atr_source"] == "live_4h_atr_fallback"
    assert abs(float(pos.extras["entry_atr"]) - 7.0) < 1e-9
    assert pos.stop_price == 100.5
    assert abs(float(pos.extras["initial_stop"]) - 100.5) < 1e-9


def test_open_fill_records_entry_atr_without_refresh_on_add() -> None:
    bot = BotConfig()
    sig_acct = AccountState(10_000, 10_000, 10_000, 10_000, day_key="2026-10-25", week_key="2026-W43")
    intent = OrderIntent(
        action=IntentAction.OPEN,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MARKET,
        size=1.0,
        price=100.0,
        stop_price=80.0,
        leverage=5,
        isolated=True,
        reduce_only=False,
        reason="open",
        extras={"atr": 10.0, "tag": "donchian_close_breakout"},
    )
    broker = PaperBroker(sig_acct)
    broker.submit(intent, START)
    pos = sig_acct.position_for("ETH")
    assert pos is not None
    assert abs(float(pos.extras["entry_atr"]) - 10.0) < 1e-9
    assert abs(float(pos.extras["initial_stop"]) - 80.0) < 1e-9
    add = OrderIntent(
        action=IntentAction.ADD,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        size=0.5,
        price=110.0,
        stop_price=80.0,
        leverage=5,
        isolated=True,
        reduce_only=False,
        reason="pyramid",
        extras={"pyramid": True, "atr": 4.0},
    )
    broker.submit(add, START)
    assert abs(float(pos.extras["entry_atr"]) - 10.0) < 1e-9
    assert pos.stop_price == 80.0
    assert RiskManager(bot).cfg.trend.exit_mode == "partial_atr"


def test_config_defaults_to_partial_atr_and_rejects_unknown_mode(tmp_path) -> None:
    cfg = load_config("config.toml")
    assert cfg.trend.exit_mode == "partial_atr"
    assert cfg.trend.partial_atr == 1.0
    assert cfg.trend.partial_atr_frac == 0.5
    assert cfg.trend.partial2_atr == 2.0
    assert cfg.trend.partial2_atr_frac == 1.0
    assert BotConfig().trend.exit_mode == "partial_atr"

    bad = tmp_path / "bad.toml"
    bad.write_text("[trend]\nexit_mode = \"chandelier\"\n", encoding="utf-8")
    try:
        load_config(bad)
        raise AssertionError("invalid exit_mode should fail")
    except ValueError as exc:
        assert "exit_mode" in str(exc)

    trail = tmp_path / "trail.toml"
    trail.write_text("[trend]\nexit_mode = \"trail\"\n", encoding="utf-8")
    loaded = load_config(trail)
    assert loaded.trend.exit_mode == "trail"


def test_live_pyramid_fill_does_not_lift_stop_under_partial_atr(tmp_path) -> None:
    ex = Exch(order_results=[_ok_resting(880)])
    runner = _runner(tmp_path, ex)
    assert runner.cfg.trend.exit_mode == "partial_atr"
    runner.account.positions.append(_live_pos())
    runner.account.resting_entries.append(_rec())
    runner.client.fetch_open_orders = lambda: [_stop_row("SOL", 9, 1.0, 97.0)]  # type: ignore[method-assign]
    runner.client.fetch_perp_positions = lambda: {"SOL": ExchangePosition("SOL", 1.5, Side.LONG, 102.9)}  # type: ignore[method-assign]
    runner.reconcile_live_protection({"SOL": {"sz_decimals": 2, "mark_px": 109.0}})
    pos = runner.account.position_for("SOL")
    assert pos is not None
    assert abs(pos.stop_price - 97.0) < 1e-9
    assert pos.extras["pyramid_count"] == 1
    assert pos.extras["partial_atr_stop_frozen"] is True
    stops = _stops(ex)
    assert len(stops) == 1
    assert abs(stops[0]["px"] - 97.0) < 1e-9
    assert abs(stops[0]["sz"] - 1.5) < 1e-9


def test_frozen_stop_is_not_tightened_to_fit_risk_pct(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.account.equity = 100.0
    pos = _live_pos(size=1.5, entry_price=102.9, stop_price=97.0)
    pos.extras["partial_atr_stop_frozen"] = True
    runner._open_orders = [_stop_row("SOL", 9, 1.5, 97.0)]
    runner._cap_position_risk(pos)
    assert abs(pos.stop_price - 97.0) < 1e-9

    runner.cfg.trend.exit_mode = "trail"
    runner._cap_position_risk(pos)
    assert pos.stop_price > 97.0


def test_frozen_stop_skips_leverage_formula_but_respects_exchange_liq(tmp_path) -> None:
    runner = _runner(tmp_path)
    pos = _live_pos(entry_price=100.0, stop_price=80.0)
    pos.extras["partial_atr_stop_frozen"] = True
    runner.account.positions.append(pos)
    runner._ensure_stops_before_liq({"SOL": ExchangePosition("SOL", 1.0, Side.LONG, 100.0)})
    assert abs(pos.stop_price - 80.0) < 1e-9

    runner._ensure_stops_before_liq(
        {"SOL": ExchangePosition("SOL", 1.0, Side.LONG, 100.0, liquidation_px=90.0)}
    )
    assert pos.stop_price > 90.0
    assert pos.extras.get("stop_tightened_for_liq") is True
