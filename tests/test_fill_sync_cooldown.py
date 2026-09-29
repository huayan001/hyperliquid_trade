"""resting 成交回写均价/计数/止损、幽灵仓成交判定、止损冷却、权益口径、GTC 限价与首仓止损按成交价。"""

from __future__ import annotations

import logging

from hl_bot.config import BotConfig
from hl_bot.exchange.client import (
    ExchangePosition,
    HyperliquidClient,
    gtc_retry_price,
    stop_from_fill,
)
from hl_bot.exchange.equity import combine_live_equity
from hl_bot.exchange.paper import PaperBroker
from hl_bot.models import IntentAction, OrderIntent, OrderKind, Position, RestingEntry, Side, StrategyName
from hl_bot.runner import FOUR_HOURS_MS, BotRunner

HOUR = 3_600_000


# ---------------------------------------------------------------- helpers


def _ok_resting(oid: int) -> dict:
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}}}


def _filled(size: float, px: float, oid: int = 1) -> dict:
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": str(size), "avgPx": str(px), "oid": oid}}]},
        },
    }


POST_ONLY_ERR = "Post only order would have immediately matched, bbo was 2722.5@2722.6. asset=1"


def _post_only_err() -> dict:
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": POST_ONLY_ERR}]}}}


class Exch:
    def __init__(self, order_results=None, market_results=None) -> None:
        self.order_results = list(order_results or [])
        self.market_results = list(market_results or [])
        self.orders: list[dict] = []
        self.cancels: list[tuple[str, int]] = []

    def update_leverage(self, *a, **k):
        return None

    def order(self, coin, is_buy, sz, px, order_type, reduce_only=False, **kwargs):
        self.orders.append(
            {"coin": coin, "is_buy": is_buy, "sz": sz, "px": px, "order_type": order_type, "reduce_only": reduce_only}
        )
        return self.order_results.pop(0)

    def market_open(self, coin, is_buy, sz, *a, **k):
        self.orders.append({"coin": coin, "is_buy": is_buy, "sz": sz, "market": True, "reduce_only": False})
        return self.market_results.pop(0)

    def cancel(self, coin, oid):
        self.cancels.append((coin, int(oid)))
        return {"status": "ok"}


def _stops(ex: Exch) -> list[dict]:
    return [o for o in ex.orders if o["reduce_only"] and "trigger" in (o.get("order_type") or {})]


def _stop_row(coin: str, oid: int, size: float, trigger: float) -> dict:
    return {
        "coin": coin,
        "side": "A",
        "limitPx": str(trigger),
        "triggerPx": str(trigger),
        "sz": str(size),
        "oid": oid,
        "reduceOnly": True,
        "isTrigger": True,
        "orderType": "Stop Market",
        "tpsl": "sl",
    }


def _runner(tmp_path, ex: Exch | None = None, symbols=("SOL",)) -> BotRunner:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.enable_live = True
    cfg.private_key = "0x" + "11" * 32
    cfg.account_address = "0x" + "22" * 20
    cfg.state_path = str(tmp_path / "paper.json")
    cfg.symbols = tuple(symbols)
    runner = BotRunner(cfg)
    runner.client.connect_sdk = lambda **kwargs: None  # type: ignore[method-assign]
    runner.client._exchange = ex or Exch()
    runner.client._sdk_ready = True
    runner.account.equity = 1000.0
    return runner


def _pos(**kw) -> Position:
    base = dict(
        symbol="SOL",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        size=1.0,
        entry_price=100.0,
        stop_price=97.0,
        leverage=10,
        opened_ts=1,
        tag="donchian_close_breakout",
        extras={"_opened_size": 1.0, "tier_add_done": True, "sl_oid": 9},
    )
    base.update(kw)
    return Position(**base)


def _rec(**kw) -> RestingEntry:
    base = dict(
        symbol="SOL",
        tier="pyramid_add",
        action="add",
        side="long",
        price=104.0,
        size=0.5,
        stop_price=102.0,
        oid=501,
        strategy="trend",
        leverage=10,
        isolated=True,
        position_size_at_submit=1.0,
        tag="pyramid_add",
        extras={"pyramid": True, "atr": 2.0, "tag": "pyramid_add"},
    )
    base.update(kw)
    return RestingEntry(**base)


CTX = {"SOL": {"sz_decimals": 2, "mark_px": 106.0}}


# ---------------------------------------------------------------- 1. adopt resting fill


def test_pyramid_resting_fill_writes_back_entry_counters_and_breakeven_stop(tmp_path) -> None:
    ex = Exch(order_results=[_ok_resting(880)])
    runner = _runner(tmp_path, ex)
    runner.account.positions.append(_pos())
    runner.account.resting_entries.append(_rec())
    runner.client.fetch_open_orders = lambda: [_stop_row("SOL", 9, 1.0, 97.0)]  # type: ignore[method-assign]
    # 真实成交比挂单差：交易所均价 102.9（加仓段≈108.7）
    runner.client.fetch_perp_positions = lambda: {"SOL": ExchangePosition("SOL", 1.5, Side.LONG, 102.9)}  # type: ignore[method-assign]
    ctx = {"SOL": {"sz_decimals": 2, "mark_px": 109.0}}
    runner.reconcile_live_protection(ctx)

    pos = runner.account.position_for("SOL")
    assert pos is not None
    assert abs(pos.size - 1.5) < 1e-9
    assert abs(pos.entry_price - 102.9) < 1e-9
    assert pos.extras["pyramid_count"] == 1
    assert abs(pos.extras["pyramid_added_frac"] - 0.5) < 1e-9
    # 保本止损按真实均价重算：102.9 + 0.05×ATR(2) = 103.0，而不是挂单时的 102.0
    assert abs(pos.stop_price - 103.0) < 1e-9
    stops = _stops(ex)
    assert len(stops) == 1
    assert abs(stops[0]["sz"] - 1.5) < 1e-9
    assert abs(stops[0]["px"] - 103.0) < 1e-9
    assert ("SOL", 9) in ex.cancels
    assert pos.extras["sl_oid"] == 880
    assert runner.account.resting_entries == []


def test_pyramid_breakeven_not_placed_on_wrong_side_of_mark(tmp_path) -> None:
    ex = Exch(order_results=[_ok_resting(881)])
    runner = _runner(tmp_path, ex)
    runner.account.positions.append(_pos())
    runner.account.resting_entries.append(_rec())
    runner.client.fetch_open_orders = lambda: [_stop_row("SOL", 9, 1.0, 97.0)]  # type: ignore[method-assign]
    runner.client.fetch_perp_positions = lambda: {"SOL": ExchangePosition("SOL", 1.5, Side.LONG, 102.9)}  # type: ignore[method-assign]
    runner.reconcile_live_protection({"SOL": {"sz_decimals": 2, "mark_px": 102.5}})
    pos = runner.account.position_for("SOL")
    assert pos.stop_price < 102.5
    assert pos.extras["pyramid_count"] == 1
    stops = _stops(ex)
    assert len(stops) == 1 and abs(stops[0]["sz"] - 1.5) < 1e-9


def test_tier_add_resting_fill_updates_tier_flags_and_entry(tmp_path) -> None:
    ex = Exch(order_results=[_ok_resting(882)])
    runner = _runner(tmp_path, ex)
    starter = _pos(
        size=0.35,
        tag="trend_confirmed_starter",
        extras={"_opened_size": 0.35, "starter_pending_add": True, "intended_full_size": 1.0, "sl_oid": 9},
    )
    runner.account.positions.append(starter)
    runner.account.resting_entries.append(
        _rec(
            tier="donchian_close_breakout",
            price=101.0,
            size=0.65,
            stop_price=97.0,
            position_size_at_submit=0.35,
            tag="donchian_close_breakout",
            extras={"tier_add": True, "breakout_add": True},
        )
    )
    runner.client.fetch_open_orders = lambda: [_stop_row("SOL", 9, 0.35, 97.0)]  # type: ignore[method-assign]
    runner.client.fetch_perp_positions = lambda: {"SOL": ExchangePosition("SOL", 1.0, Side.LONG, 100.7)}  # type: ignore[method-assign]
    runner.reconcile_live_protection(CTX)
    pos = runner.account.position_for("SOL")
    assert abs(pos.entry_price - 100.7) < 1e-9
    assert pos.extras["tier_add_done"] is True
    assert pos.extras["starter_pending_add"] is False
    assert not pos.starter_pending()
    assert abs(pos.extras["_opened_size"] - 1.0) < 1e-9
    assert abs(pos.stop_price - 97.0) < 1e-9  # 共用止损
    stops = _stops(ex)
    assert len(stops) == 1 and abs(stops[0]["sz"] - 1.0) < 1e-9


def test_open_resting_fill_shifts_stop_to_real_fill_and_caps_risk(tmp_path) -> None:
    """首仓 resting 随后成交：止损按真实成交价平移；按交易所实际止损算风险 > 2% 权益则收紧。"""
    ex = Exch(order_results=[_ok_resting(883)])
    runner = _runner(tmp_path, ex)
    runner.account.equity = 1000.0  # 2% = $20
    runner.account.resting_entries.append(
        _rec(
            tier="donchian_close_breakout",
            action="open",
            price=100.0,
            size=5.0,
            stop_price=96.0,
            position_size_at_submit=0.0,
            tag="donchian_close_breakout",
            extras={"tag": "donchian_close_breakout"},
        )
    )
    # 交易所上残留一张更宽的止损 94（例如旧单），风险按它算：(101-94)*5 = 35 > 20
    runner.client.fetch_open_orders = lambda: [_stop_row("SOL", 7, 5.0, 94.0)]  # type: ignore[method-assign]
    runner.client.fetch_perp_positions = lambda: {"SOL": ExchangePosition("SOL", 5.0, Side.LONG, 101.0)}  # type: ignore[method-assign]
    runner.reconcile_live_protection(CTX)
    pos = runner.account.position_for("SOL")
    assert pos is not None
    assert abs(pos.entry_price - 101.0) < 1e-9
    # 平移后 97.0，风险 (101-97)*5=20 → 刚好不超；但交易所那张 94 更宽 → 收紧到 101-20/5 = 97.0
    assert abs(pos.stop_price - 97.0) < 1e-9
    stops = _stops(ex)
    assert len(stops) == 1
    assert abs(stops[0]["px"] - 97.0) < 1e-9
    assert abs(stops[0]["sz"] - 5.0) < 1e-9
    assert ("SOL", 7) in ex.cancels


def test_cap_position_risk_tightens_when_over_two_pct(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.account.equity = 500.0  # 2% = $10
    pos = _pos(size=4.0, entry_price=100.0, stop_price=96.0)  # 风险 16
    runner._open_orders = [_stop_row("SOL", 9, 4.0, 96.0)]
    runner._reconcile_ctxs = CTX
    runner._cap_position_risk(pos)
    assert abs(pos.stop_price - 97.5) < 1e-9  # 100 - 10/4
    assert pos.extras.get("stop_tightened_for_risk") is True


def test_reconcile_syncs_entry_price_from_exchange(tmp_path) -> None:
    ex = Exch()
    runner = _runner(tmp_path, ex)
    runner.account.positions.append(_pos(entry_price=100.0))
    runner._align_open_sizes({"SOL": ExchangePosition("SOL", 1.0, Side.LONG, 100.37)})
    assert abs(runner.account.position_for("SOL").entry_price - 100.37) < 1e-9


# ---------------------------------------------------------------- 2. ghost cleanup


def _sol_ghost(runner: BotRunner) -> Position:
    pos = _pos(size=2.55, entry_price=116.4, stop_price=112.5, opened_ts=1_790_000_000_000, extras={"sl_oid": 557812669666})
    runner.account.positions.append(pos)
    return pos


def test_ghost_with_own_stop_fill_records_pnl_and_cooldown(tmp_path, caplog) -> None:
    runner = _runner(tmp_path)
    _sol_ghost(runner)
    t = 1_790_000_000_000 + 5 * HOUR
    fills = [
        {"coin": "SOL", "dir": "Open Long", "px": "116.4", "sz": "2.55", "closedPnl": "0", "fee": "0.13", "oid": 1, "time": 1_790_000_000_000},
        {"coin": "SOL", "dir": "Close Long", "px": "112.49", "sz": "2.55", "closedPnl": "-10.2255", "fee": "0.129", "oid": 557812669666, "time": t},
    ]
    runner.client.fetch_user_fills_by_time = lambda start_ms, end_ms=None: fills  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        runner._align_open_sizes({})
    assert runner.account.position_for("SOL") is None
    assert abs(runner.account.realized_pnl_today - (-10.2255 - 0.129)) < 1e-9
    rec = runner.account.last_stops["SOL"]
    assert rec["side"] == "long" and rec["ts"] == t and abs(rec["price"] - 112.49) < 1e-9
    assert any("止损成交" in r.message for r in caplog.records)
    assert not any("疑似强平" in r.message for r in caplog.records)


def test_ghost_without_fill_is_labelled_suspected_liquidation(tmp_path, caplog) -> None:
    runner = _runner(tmp_path)
    _sol_ghost(runner)
    runner.client.fetch_user_fills_by_time = lambda start_ms, end_ms=None: []  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        runner._align_open_sizes({})
    assert runner.account.position_for("SOL") is None
    assert runner.account.realized_pnl_today == 0.0
    assert "SOL" not in runner.account.last_stops
    assert any("疑似强平/外部平仓" in r.message for r in caplog.records)


def test_ghost_liquidation_fill_labelled(tmp_path, caplog) -> None:
    runner = _runner(tmp_path)
    _sol_ghost(runner)
    fills = [
        {
            "coin": "SOL",
            "dir": "Close Long",
            "px": "105.0",
            "sz": "2.55",
            "closedPnl": "-29.6",
            "fee": "0",
            "oid": 42,
            "time": 1_790_000_000_000 + HOUR,
            "liquidation": {"markPx": "105.0", "method": "market"},
        }
    ]
    runner.client.fetch_user_fills_by_time = lambda start_ms, end_ms=None: fills  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        runner._align_open_sizes({})
    assert any("强平成交" in r.message for r in caplog.records)
    assert runner.account.last_stops["SOL"]["label"] == "强平成交"


def test_ghost_fill_query_failure_defers_then_clears(tmp_path) -> None:
    runner = _runner(tmp_path)
    _sol_ghost(runner)

    def boom(start_ms, end_ms=None):
        raise RuntimeError("429")

    runner.client.fetch_user_fills_by_time = boom  # type: ignore[method-assign]
    runner._align_open_sizes({})
    assert runner.account.position_for("SOL") is not None
    runner._align_open_sizes({})
    assert runner.account.position_for("SOL") is not None
    runner._align_open_sizes({})
    assert runner.account.position_for("SOL") is None


# ---------------------------------------------------------------- 3. stop cooldown


def test_stop_cooldown_blocks_same_side_for_two_closed_4h_bars(tmp_path) -> None:
    runner = _runner(tmp_path)
    stop_ts = 10 * FOUR_HOURS_MS + HOUR  # 某根 4h K 线中途止损
    runner._record_stop("SOL", Side.LONG, 112.5, -10.0, stop_ts)
    assert runner.stop_cooldown_reason("SOL", Side.LONG, stop_ts + 10 * 60_000)
    # 下一根 4h 收盘（1 根）仍冷却
    assert runner.stop_cooldown_reason("SOL", Side.LONG, 12 * FOUR_HOURS_MS - 1)
    # 止损后第 2 根完整 4h 收盘 → 放行
    assert runner.stop_cooldown_reason("SOL", Side.LONG, 13 * FOUR_HOURS_MS) is None
    # 反方向不受影响
    assert runner.stop_cooldown_reason("SOL", Side.SHORT, stop_ts + 60_000) is None
    # 其他标的不受影响
    assert runner.stop_cooldown_reason("ETH", Side.LONG, stop_ts + 60_000) is None


def test_stop_cooldown_configurable_and_persisted(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner._record_stop("SOL", Side.LONG, 112.5, -10.0, 1000)
    runner.broker.save()
    loaded = PaperBroker.load(runner.cfg.state_path, 100.0, runner.account.day_key, runner.account.week_key)
    assert loaded.account.last_stops["SOL"]["side"] == "long"
    runner.cfg.trend.stop_cooldown_bars_4h = 0
    assert runner.stop_cooldown_reason("SOL", Side.LONG, 2000) is None


def test_bot_hard_stop_close_records_cooldown(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.account.positions.append(_pos())
    runner.client.place = lambda intent, **kw: _filled(1.0, 96.9)  # type: ignore[method-assign]
    intent = OrderIntent(
        action=IntentAction.CLOSE,
        symbol="SOL",
        strategy=StrategyName.TREND,
        side=Side.SHORT,
        kind=OrderKind.MARKET,
        size=1.0,
        price=96.9,
        stop_price=None,
        leverage=10,
        isolated=True,
        reduce_only=True,
        reason="趋势硬止损触发",
    )
    runner._execute(intent, now_ms=5_000)
    assert runner.account.position_for("SOL") is None
    assert runner.account.last_stops["SOL"]["side"] == "long"
    assert runner.account.last_stops["SOL"]["ts"] == 5_000


def test_config_default_cooldown_is_two_bars() -> None:
    from hl_bot.config import load_config

    assert BotConfig().trend.stop_cooldown_bars_4h == 2
    assert load_config().trend.stop_cooldown_bars_4h == 2


# ---------------------------------------------------------------- 4. equity


def _perps_state(account_value: float, upnls: list[float]) -> dict:
    return {
        "marginSummary": {"accountValue": str(account_value)},
        "crossMarginSummary": {"accountValue": "0.0"},
        "withdrawable": "0.0",
        "assetPositions": [{"position": {"coin": f"C{i}", "szi": "1", "unrealizedPnl": str(u)}} for i, u in enumerate(upnls)],
    }


def _spot_state(total: float, hold: float) -> dict:
    return {"balances": [{"coin": "USDC", "token": 0, "total": str(total), "hold": str(hold)}]}


def test_unified_equity_consistent_in_and_out_of_position() -> None:
    """2026-09-29 实盘数值：持仓时 free=440.35/hold=12.12/perps=12.10；空仓时 free=448.19。"""
    in_pos = combine_live_equity(_perps_state(12.104104, [-0.24992, -0.1541]), _spot_state(452.470276, 12.123184), "unifiedAccount")
    assert abs(in_pos.equity - (440.347092 + 12.104104)) < 1e-6
    assert in_pos.formula == "spot_usdc_plus_isolated_unified"
    flat = combine_live_equity(_perps_state(0.0, []), _spot_state(448.19, 0.0), "unifiedAccount")
    assert abs(flat.equity - 448.19) < 1e-9
    # 旧口径持仓时只算 free=440.35，与空仓相差 ~$8；新口径相差 <1%
    assert abs(in_pos.equity - flat.equity) / flat.equity < 0.01


def test_unified_equity_caps_isolated_part_to_hold_to_avoid_double_count() -> None:
    # perps 远大于 hold（接口异常/同一桶）：只计 hold 附近，不把 478 加两次
    b = combine_live_equity(_perps_state(478.0, [0.0]), _spot_state(490.0, 10.0), "unifiedAccount")
    # free 480 + min(perps, hold×1.05+1) = 480 + 11.5，而不是 480 + 478
    assert abs(b.equity - 491.5) < 1e-9
    # 浮盈计入
    b2 = combine_live_equity(_perps_state(46.9, [16.9]), _spot_state(430.0, 30.0), "unifiedAccount")
    assert abs(b2.equity - (400.0 + 46.9)) < 1e-9


def test_unified_resting_order_hold_is_not_lost() -> None:
    flat_with_order = combine_live_equity(_perps_state(0.0, []), _spot_state(420.0, 20.0), "unifiedAccount")
    assert abs(flat_with_order.equity - 420.0) < 1e-9
    pos_with_order = combine_live_equity(_perps_state(10.0, [0.0]), _spot_state(450.0, 30.0), "unifiedAccount")
    assert abs(pos_with_order.equity - 450.0) < 1e-9


def _client(fake_info) -> HyperliquidClient:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    client = HyperliquidClient(cfg)
    client.connect_sdk = lambda **kwargs: None  # type: ignore[method-assign]
    client._post_info = fake_info  # type: ignore[method-assign]
    return client


def test_partial_equity_read_never_logs_partial_amount(caplog) -> None:
    def fake_info(payload):
        if payload["type"] == "clearinghouseState":
            return _perps_state(35.13, [0.1])
        raise RuntimeError("Hyperliquid /info HTTP 429: null")

    with caplog.at_level(logging.INFO):
        reading = _client(fake_info).read_live_equity()
    assert reading is not None and reading.complete is False
    assert not any("35.13" in r.getMessage() for r in caplog.records)


def test_runner_partial_read_uses_cache_without_printing_partial(tmp_path, caplog) -> None:
    from hl_bot.exchange.client import LiveEquityRead

    runner = _runner(tmp_path)
    reads = [
        LiveEquityRead(452.45, True, "spot_usdc_plus_isolated_unified", "unifiedAccount"),
        LiveEquityRead(35.13, False, "perps_plus_spot", None, ("spotClearinghouseState: (429, None, 'null')",)),
    ]
    runner.client.read_live_equity = lambda *a: reads.pop(0)  # type: ignore[method-assign]
    runner._refresh_live_equity(1_000)
    with caplog.at_level(logging.WARNING):
        runner._refresh_live_equity(2_000)
    assert abs(runner.account.equity - 452.45) < 1e-9
    assert runner._equity_ok
    assert not any("35.13" in r.getMessage() for r in caplog.records)


def test_cached_abstraction_skips_extra_request() -> None:
    calls: list[str] = []

    def fake_info(payload):
        calls.append(payload["type"])
        if payload["type"] == "clearinghouseState":
            return _perps_state(12.1, [-0.4])
        if payload["type"] == "spotClearinghouseState":
            return _spot_state(452.47, 12.12)
        raise AssertionError(payload["type"])

    client = _client(fake_info)
    client.last_abstraction = "unifiedAccount"
    reading = client.read_live_equity()
    assert reading.complete
    assert "userAbstraction" not in calls
    assert abs(reading.equity - (440.35 + 12.1)) < 1e-6


# ---------------------------------------------------------------- minor: GTC retry cap, stop from fill


def _add_intent(price: float = 2730.0) -> OrderIntent:
    return OrderIntent(
        action=IntentAction.ADD,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        size=0.02,
        price=price,
        stop_price=2650.0,
        leverage=10,
        isolated=True,
        reduce_only=False,
        reason="add",
        extras={"tier_add": True, "current_size": 0.01, "total_size": 0.03},
    )


def _live_client(ex: Exch, book=(2722.0, 2722.6)) -> HyperliquidClient:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.enable_live = True
    cfg.private_key = "0x" + "ab" * 32
    cfg.account_address = "0x" + "cd" * 20
    client = HyperliquidClient(cfg)
    client._exchange = ex
    client._sdk_ready = True
    client.fetch_open_orders = lambda: []  # type: ignore[method-assign]
    client.fetch_best_bid_ask = lambda symbol: book  # type: ignore[method-assign]
    return client


def test_gtc_retry_price_capped_at_best_ask() -> None:
    assert gtc_retry_price(True, 2730.0, (2722.0, 2722.6)) == 2722.6
    assert gtc_retry_price(True, 2720.0, (2719.0, 2722.6)) == 2720.0
    assert gtc_retry_price(False, 2700.0, (2722.0, 2722.6)) == 2722.0
    assert gtc_retry_price(True, 2730.0, None) is None
    ex = Exch(order_results=[_post_only_err(), _filled(0.02, 2722.6), _ok_resting(99)])
    client = _live_client(ex)
    result = client.place(_add_intent(2730.0))
    entries = [o for o in ex.orders if not o["reduce_only"]]
    assert len(entries) == 2
    assert entries[1]["order_type"] == {"limit": {"tif": "Gtc"}}
    assert abs(entries[1]["px"] - 2722.6) < 1e-9
    assert result["order"].get("post_only_retry_px") == 2722.6


def test_gtc_retry_skipped_without_book() -> None:
    ex = Exch(order_results=[_post_only_err()])
    client = _live_client(ex, book=None)
    result = client.place(_add_intent(2730.0))
    assert len([o for o in ex.orders if not o["reduce_only"]]) == 1
    assert result.get("post_only_retry") is None
    assert result.get("entry_unfilled") is True


def test_initial_stop_recomputed_from_fill_price() -> None:
    intent = OrderIntent(
        action=IntentAction.OPEN,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MARKET,
        size=0.0142,
        price=2670.0,  # mid
        stop_price=2620.0,
        leverage=10,
        isolated=True,
        reduce_only=False,
        reason="starter",
        extras={"tier": "trend_confirmed_starter", "current_size": 0.0},
    )
    assert abs(stop_from_fill(intent, 2676.6) - 2626.6) < 1e-9
    ex = Exch(market_results=[_filled(0.0142, 2676.6)], order_results=[_ok_resting(77)])
    client = _live_client(ex)
    result = client.place(intent)
    stops = _stops(ex)
    assert len(stops) == 1
    assert abs(stops[0]["px"] - 2626.6) < 1e-9
    assert abs(result["stop_price"] - 2626.6) < 1e-9


def test_runner_open_fill_uses_fill_based_stop(tmp_path) -> None:
    runner = _runner(tmp_path, symbols=("ETH",))
    runner._live_book_ok = True
    runner._open_orders = []
    runner.client.place = lambda intent, **kw: {  # type: ignore[method-assign]
        "status": "ok",
        "order": _filled(0.0142, 2676.6),
        "stop_oid": 77,
        "stop_price": 2626.6,
    }
    intent = OrderIntent(
        action=IntentAction.OPEN,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MARKET,
        size=0.0142,
        price=2670.0,
        stop_price=2620.0,
        leverage=10,
        isolated=True,
        reduce_only=False,
        reason="starter",
        extras={"tier": "trend_confirmed_starter", "tag": "trend_confirmed_starter"},
    )
    runner._execute(intent, now_ms=1)
    pos = runner.account.position_for("ETH")
    assert abs(pos.entry_price - 2676.6) < 1e-9
    assert abs(pos.stop_price - 2626.6) < 1e-9
    assert pos.extras["sl_oid"] == 77
