"""实盘保护止损对账、同价挂单去重，以及 429 不退出循环。"""

from __future__ import annotations

import io
import sys
import types
import urllib.error
import urllib.request

import pytest

from hl_bot.config import BotConfig
from hl_bot.exchange.client import (
    ExchangePosition,
    HyperliquidClient,
    RateLimitError,
    extract_order_fill,
    is_rate_limit_error,
)
from hl_bot.models import IntentAction, OrderIntent, OrderKind, Position, RestingEntry, Side, StrategyName
from hl_bot.runner import BotRunner

STARTER = 0.0157
FULL = 0.2873
ADD = 0.1
STOP = 2650.0  # 须落在 10x×0.5 缓冲内，避免 reconcile 收紧止损
ENTRY = 2727.2


def _hl_resting(oid: int, size: float) -> dict:
    return {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid, "sz": str(size)}}]}},
    }


def _add_intent() -> OrderIntent:
    return OrderIntent(
        action=IntentAction.ADD,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        size=ADD,
        price=ENTRY,
        stop_price=STOP,
        leverage=5,
        isolated=True,
        reduce_only=False,
        reason="donchian add",
        extras={
            "tier": "donchian_close_breakout",
            "tag": "donchian_close_breakout",
            "tier_add": True,
            "current_size": STARTER,
            "total_size": STARTER + ADD,
        },
    )


def _open_intent() -> OrderIntent:
    return OrderIntent(
        action=IntentAction.OPEN,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        size=STARTER,
        price=ENTRY,
        stop_price=STOP,
        leverage=5,
        isolated=True,
        reduce_only=False,
        reason="trend_confirmed_starter",
        extras={"tier": "trend_confirmed_starter", "tag": "trend_confirmed_starter", "current_size": 0.0},
    )


def _starter_pos(size: float = STARTER) -> Position:
    return Position(
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        size=size,
        entry_price=2722.7,
        stop_price=STOP,
        leverage=5,
        opened_ts=1,
        tag="trend_confirmed_starter",
        extras={"_opened_size": size, "starter_pending_add": True, "intended_full_size": FULL, "sl_oid": 9},
    )


class ScriptedExchange:
    def __init__(self, order_results: list | None = None) -> None:
        self.order_results = list(order_results or [])
        self.orders: list[dict] = []
        self.cancels: list[tuple[str, int]] = []

    def update_leverage(self, lev: int, coin: str, is_cross: bool = True) -> None:
        return None

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


def _client(exchange: ScriptedExchange | None = None) -> HyperliquidClient:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.enable_live = True
    cfg.private_key = "0x" + "ab" * 32
    cfg.account_address = "0x" + "cd" * 20
    client = HyperliquidClient(cfg)
    client._exchange = exchange or ScriptedExchange()
    client._sdk_ready = True
    return client


def _stop_orders(exchange: ScriptedExchange) -> list[dict]:
    return [
        row
        for row in exchange.orders
        if row["reduce_only"] and isinstance(row["order_type"], dict) and "trigger" in row["order_type"]
    ]


def _stop_row(oid: int, size: float, trigger: float = STOP) -> dict:
    return {
        "coin": "ETH",
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


def _entry_row(oid: int, price: float = ENTRY, size: float = ADD) -> dict:
    return {
        "coin": "ETH",
        "side": "B",
        "limitPx": str(price),
        "sz": str(size),
        "oid": oid,
        "reduceOnly": False,
        "orderType": "Limit",
    }


def _runner(tmp_path, exchange: ScriptedExchange | None = None) -> BotRunner:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.enable_live = True
    cfg.private_key = "0x" + "11" * 32
    cfg.account_address = "0x" + "22" * 20
    cfg.state_path = str(tmp_path / "paper.json")
    cfg.symbols = ("ETH",)
    runner = BotRunner(cfg)
    runner.client.connect_sdk = lambda **kwargs: None  # type: ignore[method-assign]
    runner.client._exchange = exchange or ScriptedExchange()
    runner.client._sdk_ready = True
    return runner


def test_resting_add_later_fill_places_full_stop(tmp_path) -> None:
    """Maker ADD 当时只 resting、不挂止损；随后成交后下一轮把止损补到整仓。"""
    entry_ex = ScriptedExchange(order_results=[_hl_resting(501, ADD)])
    client = _client(entry_ex)
    placed = client.place(_add_intent())
    assert extract_order_fill(placed) is None
    assert placed.get("resting_oids") == [501]
    assert _stop_orders(entry_ex) == []

    ex = ScriptedExchange(order_results=[_hl_resting(880, FULL)])
    runner = _runner(tmp_path, ex)
    runner.account.positions.append(_starter_pos())
    runner.account.resting_entries.append(
        RestingEntry(
            symbol="ETH",
            tier="donchian_close_breakout",
            action="add",
            side="long",
            price=ENTRY,
            size=ADD,
            stop_price=STOP,
            oid=501,
            strategy="trend",
            leverage=5,
            isolated=True,
            position_size_at_submit=STARTER,
            tag="donchian_close_breakout",
        )
    )
    runner.client.fetch_open_orders = lambda: [_stop_row(9, 0.0128)]  # type: ignore[method-assign]
    runner.client.fetch_perp_positions = lambda: {  # type: ignore[method-assign]
        "ETH": ExchangePosition("ETH", FULL, Side.LONG, 2724.0)
    }
    runner.reconcile_live_protection({"ETH": {"sz_decimals": 4}})

    stops = _stop_orders(ex)
    assert len(stops) == 1
    assert abs(stops[0]["sz"] - FULL) < 1e-9
    assert stops[0]["is_buy"] is False
    assert abs(stops[0]["px"] - STOP) < 1e-9
    assert ("ETH", 9) in ex.cancels
    pos = runner.account.position_for("ETH")
    assert pos is not None
    assert abs(pos.size - FULL) < 1e-9
    assert pos.extras.get("sl_oid") == 880
    assert runner.account.resting_entries == []


def test_undersized_stop_replaced_once_and_duplicates_cancelled(tmp_path) -> None:
    ex = ScriptedExchange(order_results=[_hl_resting(77, FULL)])
    runner = _runner(tmp_path, ex)
    runner.account.positions.append(_starter_pos(size=FULL))
    runner.client.fetch_open_orders = lambda: [  # type: ignore[method-assign]
        _stop_row(9, 0.0128),
        _stop_row(10, 0.05),
    ]
    runner.client.fetch_perp_positions = lambda: {  # type: ignore[method-assign]
        "ETH": ExchangePosition("ETH", FULL, Side.LONG, 2724.0)
    }
    runner.reconcile_live_protection({"ETH": {"sz_decimals": 4}})
    stops = _stop_orders(ex)
    assert len(stops) == 1
    assert abs(stops[0]["sz"] - FULL) < 1e-9
    assert ex.cancels == [("ETH", 9), ("ETH", 10)]

    # 下一轮盘口只剩这张对齐后的止损：保留，不再挂、不再撤
    kept = ScriptedExchange()
    runner.client._exchange = kept
    runner.client.fetch_open_orders = lambda: [_stop_row(77, FULL)]  # type: ignore[method-assign]
    runner.reconcile_live_protection({"ETH": {"sz_decimals": 4}})
    assert kept.orders == []
    assert kept.cancels == []
    assert runner.account.position_for("ETH").extras.get("sl_oid") == 77


def test_duplicate_matching_stop_is_cancelled_without_new_order(tmp_path) -> None:
    ex = ScriptedExchange()
    runner = _runner(tmp_path, ex)
    runner.account.positions.append(_starter_pos(size=FULL))
    runner.client.fetch_open_orders = lambda: [_stop_row(77, FULL), _stop_row(78, FULL)]  # type: ignore[method-assign]
    runner.client.fetch_perp_positions = lambda: {  # type: ignore[method-assign]
        "ETH": ExchangePosition("ETH", FULL, Side.LONG, 2724.0)
    }
    runner.reconcile_live_protection({"ETH": {"sz_decimals": 4}})
    assert ex.orders == []
    assert ex.cancels == [("ETH", 78)]


def test_second_scan_does_not_stack_same_tier_or_same_price_add(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.account.positions.append(_starter_pos())
    calls: list[OrderIntent] = []

    def place(intent, **kwargs):
        calls.append(intent)
        return _hl_resting(501, ADD)

    runner.client.place = place  # type: ignore[method-assign]
    runner._live_book_ok = True
    runner._open_orders = []
    runner._execute(_add_intent(), now_ms=1)
    assert len(calls) == 1
    assert runner.account.resting_entries[0].oid == 501

    # 重启后本地记录还在，且交易所仍挂着同价 Alo：不得再下一张
    reloaded = BotRunner(runner.cfg)
    reloaded.client.connect_sdk = lambda **kwargs: None  # type: ignore[method-assign]
    reloaded.client.place = place  # type: ignore[method-assign]
    reloaded._live_book_ok = True
    reloaded._open_orders = [_entry_row(501), _entry_row(502), _entry_row(503)]
    reloaded._execute(_add_intent(), now_ms=2)
    assert len(calls) == 1

    # 没有本地记录时，交易所同价挂单本身也足以拦住
    reloaded.account.resting_entries.clear()
    reloaded._execute(_add_intent(), now_ms=3)
    assert len(calls) == 1



def test_align_clears_ghost_when_exchange_flat(tmp_path) -> None:
    """交易所已无仓时，本地幽灵仓必须清掉，否则占满趋势配额。"""
    runner = _runner(tmp_path)
    runner.account.positions.append(_starter_pos())
    runner.account.positions.append(
        Position(
            symbol="BTC",
            strategy=StrategyName.TREND,
            side=Side.LONG,
            size=0.0006,
            entry_price=84505.0,
            stop_price=82535.7,
            leverage=40,
            opened_ts=1,
            tag="trend_confirmed_starter",
            extras={"_opened_size": 0.0006, "sl_oid": 554280411026, "starter_pending_add": True},
        )
    )
    # 仅 ETH 仍在交易所；BTC 已被强平
    positions = {
        "ETH": ExchangePosition(symbol="ETH", size=STARTER, side=Side.LONG, entry_price=2722.7),
    }
    runner._align_open_sizes(positions)
    assert runner.account.position_for("BTC") is None
    eth = runner.account.position_for("ETH")
    assert eth is not None
    assert abs(eth.size - STARTER) < 1e-9


def test_align_clears_ghost_on_side_mismatch(tmp_path) -> None:
    runner = _runner(tmp_path)
    runner.account.positions.append(_starter_pos())
    positions = {
        "ETH": ExchangePosition(symbol="ETH", size=STARTER, side=Side.SHORT, entry_price=2722.7),
    }
    runner._align_open_sizes(positions)
    assert runner.account.position_for("ETH") is None


def test_ensure_one_stop_warns_on_place_failed(tmp_path, caplog) -> None:
    import logging

    runner = _runner(tmp_path)
    runner.account.positions.append(_starter_pos())
    positions = {
        "ETH": ExchangePosition(symbol="ETH", size=STARTER, side=Side.LONG, entry_price=2722.7),
    }

    def boom(*_a, **_k):
        return {"status": "error", "action": "place_failed", "message": "simulated"}

    runner.client.ensure_protective_stop = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        runner._ensure_one_stop("ETH", {"ETH": {"sz_decimals": 4}}, positions)
    assert any("保护止损缺失或重挂失败" in r.message for r in caplog.records)


def test_math_floor_keeps_btc_six_ten_thousandths() -> None:
    from hl_bot.exchange.client import math_floor

    # 回归：0.0006 * 1e4 浮点误差不得 floor 成 0.0005
    assert math_floor(0.0006, 10_000) == 0.0006
    assert math_floor(0.000601693, 10_000) == 0.0006


def test_reconcile_cancels_duplicate_same_price_entries(tmp_path) -> None:
    ex = ScriptedExchange()
    runner = _runner(tmp_path, ex)
    runner.account.positions.append(_starter_pos())
    runner.account.resting_entries.append(
        RestingEntry(
            symbol="ETH",
            tier="donchian_close_breakout",
            action="add",
            side="long",
            price=ENTRY,
            size=ADD,
            stop_price=STOP,
            oid=501,
            strategy="trend",
            leverage=5,
            isolated=True,
            position_size_at_submit=STARTER,
            tag="donchian_close_breakout",
        )
    )
    runner.client.fetch_open_orders = lambda: [  # type: ignore[method-assign]
        _entry_row(501),
        _entry_row(502),
        _entry_row(503),
        _entry_row(600, price=2800.0),
        _stop_row(9, STARTER),
    ]
    runner.client.fetch_perp_positions = lambda: {  # type: ignore[method-assign]
        "ETH": ExchangePosition("ETH", STARTER, Side.LONG, 2722.7)
    }
    runner.reconcile_live_protection({"ETH": {"sz_decimals": 4}})
    assert ex.cancels == [("ETH", 502), ("ETH", 503)]
    assert ex.orders == []
    assert [item.oid for item in runner.account.resting_entries] == [501]


def test_resting_entry_fill_adopts_position_and_stop(tmp_path) -> None:
    ex = ScriptedExchange(order_results=[_hl_resting(42, STARTER)])
    runner = _runner(tmp_path, ex)
    runner.account.resting_entries.append(
        RestingEntry(
            symbol="ETH",
            tier="trend_confirmed_starter",
            action="open",
            side="long",
            price=ENTRY,
            size=STARTER,
            stop_price=STOP,
            oid=700,
            strategy="trend",
            leverage=5,
            isolated=True,
            position_size_at_submit=0.0,
            tag="trend_confirmed_starter",
            extras={"starter_pending_add": True, "_opened_size": STARTER},
        )
    )
    runner.client.fetch_open_orders = lambda: []  # type: ignore[method-assign]
    runner.client.fetch_perp_positions = lambda: {  # type: ignore[method-assign]
        "ETH": ExchangePosition("ETH", STARTER, Side.LONG, ENTRY)
    }
    runner.reconcile_live_protection({"ETH": {"sz_decimals": 4}})
    pos = runner.account.position_for("ETH")
    assert pos is not None
    assert abs(pos.size - STARTER) < 1e-9
    assert abs(pos.stop_price - STOP) < 1e-9
    stops = _stop_orders(ex)
    assert len(stops) == 1
    assert abs(stops[0]["sz"] - STARTER) < 1e-9


def test_run_forever_survives_429_on_connect(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hl_bot.runner.time.sleep", lambda *_a, **_k: None)
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.enable_live = True
    cfg.poll_seconds = 5
    cfg.state_path = str(tmp_path / "paper.json")
    cfg.symbols = ("ETH",)
    runner = BotRunner(cfg)
    state = {"n": 0}

    def contexts():
        return {"ETH": {"sz_decimals": 4, "funding": 0.0, "max_leverage": 25}}

    def connect(**kwargs):
        state["n"] += 1
        if state["n"] == 1:
            err = RuntimeError("(429, None, 'Too Many Requests', {})")
            err.status_code = 429
            raise err
        raise KeyboardInterrupt

    runner.client.fetch_asset_contexts = contexts  # type: ignore[method-assign]
    runner.client.fetch_mids = lambda: {"ETH": ENTRY}  # type: ignore[method-assign]
    runner.client.connect_sdk = connect  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        runner.run_forever()
    assert state["n"] == 2


def test_run_forever_still_exits_on_non_rate_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hl_bot.runner.time.sleep", lambda *_a, **_k: None)
    cfg = BotConfig()
    cfg.state_path = str(tmp_path / "paper.json")
    runner = BotRunner(cfg)

    def scan_once(*, persist_paper: bool = False):
        raise RuntimeError("disk full")

    runner.scan_once = scan_once  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="disk full"):
        runner.run_forever()


def test_post_info_retries_429(monkeypatch) -> None:
    calls = {"n": 0}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def urlopen(req, timeout=20):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(
                req.full_url,
                429,
                "Too Many Requests",
                hdrs=None,
                fp=io.BytesIO(b"rate"),
            )
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("hl_bot.exchange.client.time.sleep", lambda *_a, **_k: None)
    client = HyperliquidClient(BotConfig())
    assert client._post_info({"type": "allMids"}) == {"ok": True}
    assert calls["n"] == 3


def test_post_info_exhausted_429_is_rate_limit(monkeypatch) -> None:
    def urlopen(req, timeout=20):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", hdrs=None, fp=io.BytesIO(b"rate"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("hl_bot.exchange.client.time.sleep", lambda *_a, **_k: None)
    client = HyperliquidClient(BotConfig())
    with pytest.raises(RateLimitError) as caught:
        client._post_info({"type": "meta"})
    assert is_rate_limit_error(caught.value)


def test_connect_sdk_retries_info_429(monkeypatch) -> None:
    monkeypatch.setattr("hl_bot.exchange.client.time.sleep", lambda *_a, **_k: None)
    constants = types.ModuleType("hyperliquid.utils.constants")
    constants.MAINNET_API_URL = "https://api.hyperliquid.xyz"
    constants.TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
    utils = types.ModuleType("hyperliquid.utils")
    utils.constants = constants
    info = types.ModuleType("hyperliquid.info")

    class Info:
        def __init__(self, *args, **kwargs):
            raise AssertionError("应走 _build_info")

    info.Info = Info
    hl = types.ModuleType("hyperliquid")
    hl.utils = utils
    hl.info = info
    monkeypatch.setitem(sys.modules, "hyperliquid", hl)
    monkeypatch.setitem(sys.modules, "hyperliquid.utils", utils)
    monkeypatch.setitem(sys.modules, "hyperliquid.utils.constants", constants)
    monkeypatch.setitem(sys.modules, "hyperliquid.info", info)

    client = HyperliquidClient(BotConfig())
    calls = {"n": 0}

    def build(url: str):
        calls["n"] += 1
        if calls["n"] == 1:
            err = RuntimeError("(429, None, rate limit, None)")
            err.status_code = 429
            raise err
        return {"url": url}

    client._build_info = build  # type: ignore[method-assign]
    client.connect_sdk(for_trading=False)
    assert calls["n"] == 2
    assert client._sdk_ready is True
    assert client._info == {"url": "https://api.hyperliquid.xyz"}


def test_math_ceil_covers_stop_size() -> None:
    from hl_bot.exchange.client import math_ceil, math_floor

    # floor 会把 0.01421 → 0.0142；cover 对略大的值向上，已对齐的保持不变
    assert math_floor(0.01421, 10_000) == 0.0142
    assert math_ceil(0.0142, 10_000) == 0.0142
    assert math_ceil(0.01420001, 10_000) == 0.0143
