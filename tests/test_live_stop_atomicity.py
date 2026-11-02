"""实盘入场/补仓与保护止损必须原子化：未成交不得留下本轮新止损。"""

from __future__ import annotations

from hl_bot.config import BotConfig
from hl_bot.exchange.client import (
    HyperliquidClient,
    extract_oid,
    extract_order_fill,
    is_post_only_reject,
    is_reduce_only_stop_order,
    protective_stop_size,
)
from hl_bot.models import IntentAction, OrderIntent, OrderKind, Position, Side, StrategyName
from hl_bot.runner import BotRunner

POST_ONLY_ERR = "Post only order would have immediately matched"
STARTER_SZ = 0.0157
ADD_SZ = 0.0291
PLANNED_TOTAL = STARTER_SZ + ADD_SZ  # 0.0448，事故里失败 ADD 仍按这个张数挂了止损


def _hl_error(msg: str) -> dict:
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": msg}]}}}


def _hl_filled(size: float | str, px: float | str, oid: int = 1) -> dict:
    return {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": str(size), "avgPx": str(px), "oid": oid}}]},
        },
    }


def _hl_resting(oid: int, size: float | str = ADD_SZ) -> dict:
    return {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid, "sz": str(size)}}]}},
    }


def _add_intent(**extra) -> OrderIntent:
    extras = {
        "current_size": STARTER_SZ,
        "total_size": PLANNED_TOTAL,
        "add_size": ADD_SZ,
        "tier_add": True,
        "breakout_add": True,
        "shared_stop": True,
        "sl_oid": 111,
        "tag": "donchian_close_breakout",
    }
    extras.update(extra)
    return OrderIntent(
        action=IntentAction.ADD,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        size=ADD_SZ,
        price=2722.7,
        stop_price=2650.0,
        leverage=5,
        isolated=True,
        reduce_only=False,
        reason="donchian_close_breakout add",
        extras=extras,
    )


def _open_intent(*, kind: OrderKind = OrderKind.MARKET, size: float = STARTER_SZ) -> OrderIntent:
    return OrderIntent(
        action=IntentAction.OPEN,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=kind,
        size=size,
        price=2722.7,
        stop_price=2650.0,
        leverage=5,
        isolated=True,
        reduce_only=False,
        reason="trend_confirmed_starter",
        extras={"current_size": 0.0, "tag": "trend_confirmed_starter"},
    )


class ScriptedExchange:
    def __init__(self, order_results: list | None = None, market_results: list | None = None) -> None:
        self.order_results = list(order_results or [])
        self.market_results = list(market_results or [])
        self.orders: list[dict] = []
        self.cancels: list[tuple[str, int]] = []
        self.market_opens: list[dict] = []
        self.market_closes: list[dict] = []

    def update_leverage(self, lev: int, coin: str, is_cross: bool = True) -> None:
        return None

    def market_open(self, coin: str, is_buy: bool, sz: float, *args, **kwargs):
        self.market_opens.append({"coin": coin, "is_buy": is_buy, "sz": sz})
        if not self.market_results:
            raise AssertionError(f"unexpected market_open({coin}, {sz})")
        return self.market_results.pop(0)

    def market_close(self, coin: str, sz: float | None = None, **kwargs):
        self.market_closes.append({"coin": coin, "sz": sz})
        return _hl_filled(sz or 0, 1.0)

    def order(self, coin, is_buy, sz, px, order_type, reduce_only=False, **kwargs):
        rec = {
            "coin": coin,
            "is_buy": is_buy,
            "sz": sz,
            "px": px,
            "order_type": order_type,
            "reduce_only": reduce_only,
        }
        self.orders.append(rec)
        if not self.order_results:
            raise AssertionError(f"unexpected order() {rec}")
        return self.order_results.pop(0)

    def cancel(self, coin: str, oid: int):
        self.cancels.append((coin, int(oid)))
        return {"status": "ok"}


def _client(exchange: ScriptedExchange, open_orders: list[dict] | None = None) -> HyperliquidClient:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.enable_live = True
    cfg.private_key = "0x" + "ab" * 32
    cfg.account_address = "0x" + "cd" * 20
    client = HyperliquidClient(cfg)
    client._exchange = exchange
    client._sdk_ready = True
    snapshot = list(open_orders or [])
    client.fetch_open_orders = lambda: list(snapshot)
    return client


def _stop_orders(exchange: ScriptedExchange) -> list[dict]:
    return [
        row
        for row in exchange.orders
        if row["reduce_only"] and isinstance(row["order_type"], dict) and "trigger" in row["order_type"]
    ]


def _entry_orders(exchange: ScriptedExchange) -> list[dict]:
    return [row for row in exchange.orders if not row["reduce_only"]]


def test_post_only_reject_detected() -> None:
    err = _hl_error(POST_ONLY_ERR)
    assert extract_order_fill(err) is None
    assert is_post_only_reject(err)
    assert not is_post_only_reject(_hl_filled(0.01, 2722.7))


def test_protective_stop_size_tracks_fill_not_planned_total() -> None:
    intent = _add_intent()
    assert abs(protective_stop_size(intent, ADD_SZ) - PLANNED_TOTAL) < 1e-9
    assert abs(protective_stop_size(intent, 0.01) - (STARTER_SZ + 0.01)) < 1e-9
    open_intent = _open_intent()
    assert abs(protective_stop_size(open_intent, STARTER_SZ) - STARTER_SZ) < 1e-9


def test_reduce_only_stop_order_filter() -> None:
    starter = {
        "coin": "ETH",
        "oid": 111,
        "reduceOnly": True,
        "isTrigger": True,
        "tpsl": "sl",
        "sz": str(STARTER_SZ),
    }
    assert is_reduce_only_stop_order(starter, "ETH")
    assert not is_reduce_only_stop_order(starter, "BTC")
    assert not is_reduce_only_stop_order(
        {"coin": "ETH", "oid": 2, "reduceOnly": False, "isTrigger": True, "tpsl": "sl"},
        "ETH",
    )


def test_open_post_only_does_not_gtc_retry_or_place_stop() -> None:
    """新开仓仍走 Maker；post-only 被拒不追价，也不挂止损。"""
    ex = ScriptedExchange(order_results=[_hl_error(POST_ONLY_ERR)])
    client = _client(ex)
    result = client.place(_open_intent(kind=OrderKind.MAKER_LIMIT, size=0.04))
    assert extract_order_fill(result) is None
    assert result.get("post_only_retry") is None
    assert _stop_orders(ex) == []
    assert len(_entry_orders(ex)) == 1
    assert ex.cancels == []
    """事故路径：Alo 被拒后不得再挂一张计划加仓张数的止损。"""
    existing = {"coin": "ETH", "oid": 111, "reduceOnly": True, "isTrigger": True, "tpsl": "sl"}
    ex = ScriptedExchange(order_results=[_hl_error(POST_ONLY_ERR), _hl_error("Could not immediately match")])
    client = _client(ex, open_orders=[existing])
    result = client.place(_add_intent())
    assert extract_order_fill(result) is None
    assert result.get("post_only_retry") == "gtc"
    assert _stop_orders(ex) == []
    # 失败补仓不得去撤 starter 已有止损
    assert ex.cancels == []


def test_failed_add_retry_does_not_accumulate_stops() -> None:
    existing = {"coin": "ETH", "oid": 111, "reduceOnly": True, "isTrigger": True, "tpsl": "sl"}
    first = ScriptedExchange(order_results=[_hl_error(POST_ONLY_ERR), _hl_error(POST_ONLY_ERR)])
    second = ScriptedExchange(order_results=[_hl_error(POST_ONLY_ERR), _hl_error(POST_ONLY_ERR)])
    client1 = _client(first, open_orders=[existing])
    client2 = _client(second, open_orders=[existing])
    client1.place(_add_intent())
    client2.place(_add_intent())
    assert _stop_orders(first) == []
    assert _stop_orders(second) == []
    assert first.cancels == []
    assert second.cancels == []


def test_post_only_add_gtc_retry_fill_then_stop_matches_filled_size() -> None:
    existing = {
        "coin": "ETH",
        "oid": 551710823331,
        "reduceOnly": True,
        "isTrigger": True,
        "tpsl": "sl",
        "sz": str(PLANNED_TOTAL),
    }
    partial = 0.01
    ex = ScriptedExchange(
        order_results=[
            _hl_error(POST_ONLY_ERR),
            _hl_filled(partial, 2722.8, oid=200),
            _hl_resting(551725045852, STARTER_SZ + partial),
        ]
    )
    client = _client(ex, open_orders=[existing])
    result = client.place(_add_intent())
    fill = extract_order_fill(result)
    assert fill is not None
    assert abs(fill.size - partial) < 1e-9
    stops = _stop_orders(ex)
    assert len(stops) == 1
    assert abs(stops[0]["sz"] - (STARTER_SZ + partial)) < 1e-9
    assert abs(stops[0]["sz"] - PLANNED_TOTAL) > 1e-6
    assert ("ETH", 551710823331) in ex.cancels
    assert ("ETH", 111) in ex.cancels  # extras sl_oid
    entries = _entry_orders(ex)
    assert len(entries) == 2
    assert entries[0]["order_type"] == {"limit": {"tif": "Alo"}}
    assert entries[1]["order_type"] == {"limit": {"tif": "Gtc"}}
    assert result.get("stop_oid") == 551725045852


def test_successful_add_replaces_existing_stop_once() -> None:
    existing = {"coin": "ETH", "oid": 42, "reduceOnly": True, "isTrigger": True, "tpsl": "sl"}
    ex = ScriptedExchange(order_results=[_hl_filled(ADD_SZ, 2722.7, oid=7), _hl_resting(99, PLANNED_TOTAL)])
    client = _client(ex, open_orders=[existing])
    result = client.place(_add_intent())
    assert extract_order_fill(result) is not None
    stops = _stop_orders(ex)
    assert len(stops) == 1
    assert abs(stops[0]["sz"] - PLANNED_TOTAL) < 1e-9
    assert ex.cancels.count(("ETH", 42)) == 1


def test_market_open_places_stop_only_after_fill() -> None:
    ex = ScriptedExchange(
        market_results=[_hl_filled(STARTER_SZ, 2722.7, oid=5)],
        order_results=[_hl_resting(88, STARTER_SZ)],
    )
    client = _client(ex, open_orders=[])
    result = client.place(_open_intent())
    fill = extract_order_fill(result)
    assert fill is not None
    stops = _stop_orders(ex)
    assert len(stops) == 1
    assert abs(stops[0]["sz"] - STARTER_SZ) < 1e-9


def test_market_resting_is_cancelled_without_stop() -> None:
    ex = ScriptedExchange(market_results=[_hl_resting(77, STARTER_SZ)])
    client = _client(ex)
    result = client.place(_open_intent())
    assert extract_order_fill(result) is None
    assert _stop_orders(ex) == []
    assert ("ETH", 77) in ex.cancels


def test_gtc_retry_resting_is_cancelled_without_stop() -> None:
    ex = ScriptedExchange(order_results=[_hl_error(POST_ONLY_ERR), _hl_resting(66, ADD_SZ)])
    client = _client(ex, open_orders=[{"coin": "ETH", "oid": 111, "reduceOnly": True, "isTrigger": True, "tpsl": "sl"}])
    result = client.place(_add_intent())
    assert extract_order_fill(result) is None
    assert _stop_orders(ex) == []
    assert ex.cancels == [("ETH", 66)]


def test_extract_oid_from_stop_payload() -> None:
    assert extract_oid(_hl_resting(551725045852)) == 551725045852
    assert extract_oid(_hl_filled(0.02, 2500, oid=9)) == 9


def _starter_pos() -> Position:
    return Position(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        size=STARTER_SZ,
        entry_price=2722.7,
        stop_price=2650.0,
        leverage=5,
        opened_ts=1,
        tag="trend_confirmed_starter",
        extras={
            "_opened_size": STARTER_SZ,
            "starter_pending_add": True,
            "intended_full_size": PLANNED_TOTAL,
            "sl_oid": 111,
        },
    )


def test_live_fill_persists_new_sl_oid(tmp_path) -> None:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.enable_live = True
    cfg.state_path = str(tmp_path / "paper.json")
    runner = BotRunner(cfg)
    runner.client.connect_sdk = lambda **kwargs: None
    runner.client.place = lambda intent, **kwargs: {
        "status": "ok",
        "order": _hl_filled(0.01, 2722.8, oid=200),
        "stop": _hl_resting(551725045852, STARTER_SZ + 0.01),
        "stop_oid": 551725045852,
        "stop_size": STARTER_SZ + 0.01,
    }
    runner.account.positions.append(_starter_pos())
    runner._execute(_add_intent(), now_ms=2)
    pos = runner.account.position_for("ETH")
    assert pos is not None
    assert pos.extras["sl_oid"] == 551725045852
    assert abs(pos.size - (STARTER_SZ + 0.01)) < 1e-9
