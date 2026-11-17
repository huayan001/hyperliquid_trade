"""实盘权益：429 / 部分读取失败时回退缓存、可疑骤降判定、无缓存时禁止开新仓。"""

from __future__ import annotations

from dataclasses import replace

from hl_bot.config import BotConfig
from hl_bot.exchange.client import HyperliquidClient, LiveEquityRead
from hl_bot.exchange.paper import PaperBroker
from hl_bot.models import IntentAction, OrderIntent, OrderKind, Side, StrategyName
from hl_bot.runner import EQUITY_CACHE_MAX_AGE_MS, BotRunner

NOW = 1_790_000_000_000
HOUR = 3_600_000


def _perps(account_value: float) -> dict:
    return {
        "marginSummary": {"accountValue": str(account_value)},
        "crossMarginSummary": {"accountValue": str(account_value)},
        "withdrawable": "0",
        "assetPositions": [],
    }


def _spot(total: float) -> dict:
    return {"balances": [{"coin": "USDC", "token": 0, "total": str(total), "hold": "0"}]}


def _live_cfg(tmp_path) -> BotConfig:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    cfg.state_path = str(tmp_path / "paper.json")
    return cfg


def _runner(tmp_path, readings: list) -> BotRunner:
    runner = BotRunner(_live_cfg(tmp_path))
    queue = list(readings)

    def fake_read():
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    runner.client.read_live_equity = fake_read  # type: ignore[method-assign]
    return runner


def _ok(value: float, abstraction: str | None = "unifiedAccount") -> LiveEquityRead:
    return LiveEquityRead(equity=value, complete=True, formula="spot_usdc_unified", abstraction=abstraction)


def _partial(value: float) -> LiveEquityRead:
    return LiveEquityRead(
        equity=value,
        complete=False,
        formula="perps_plus_spot",
        abstraction=None,
        errors=("spotClearinghouseState: (429, None, 'null')",),
    )


def _open_intent() -> OrderIntent:
    return OrderIntent(
        action=IntentAction.OPEN,
        symbol="ETH",
        strategy=StrategyName.TREND,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        size=0.01,
        price=2700.0,
        stop_price=2600.0,
        leverage=10,
        isolated=True,
        reduce_only=False,
        reason="test",
        extras={"tier": "trend_confirmed_starter"},
    )


# ---- client: complete 标记 ----


def _client(fake_info) -> HyperliquidClient:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    client = HyperliquidClient(cfg)
    client.connect_sdk = lambda **kwargs: None  # type: ignore[method-assign]
    client._post_info = fake_info  # type: ignore[method-assign]
    return client


def test_read_live_equity_spot_429_is_incomplete() -> None:
    """复现 2026-09-29 03:25：现货 429 → 只剩 perps $36.25，必须标记为不完整。"""

    def fake_info(payload):
        if payload["type"] == "clearinghouseState":
            return _perps(36.25)
        raise RuntimeError("Hyperliquid /info HTTP 429: null")

    reading = _client(fake_info).read_live_equity()
    assert reading is not None
    assert reading.complete is False
    assert abs(reading.equity - 36.25) < 1e-9


def test_read_live_equity_complete_unified() -> None:
    def fake_info(payload):
        kind = payload["type"]
        if kind == "clearinghouseState":
            return _perps(36.0)
        if kind == "spotClearinghouseState":
            return _spot(423.68)
        if kind == "userAbstraction":
            return "unifiedAccount"
        raise AssertionError(kind)

    client = _client(fake_info)
    reading = client.read_live_equity()
    assert reading is not None and reading.complete
    assert abs(reading.equity - 423.68) < 1e-9
    assert client.last_abstraction == "unifiedAccount"


def test_abstraction_429_uses_cached_mode_else_incomplete() -> None:
    """userAbstraction 429 时：有缓存模式 → 沿用且完整；无缓存 → 不完整（避免 unified 下 perps+现货重复计数）。"""

    def fake_info(payload):
        kind = payload["type"]
        if kind == "clearinghouseState":
            return _perps(46.0)
        if kind == "spotClearinghouseState":
            return _spot(440.0)
        raise RuntimeError("Hyperliquid /info HTTP 429: null")

    client = _client(fake_info)
    reading = client.read_live_equity()
    assert reading is not None and reading.complete is False

    client.last_abstraction = "unifiedAccount"
    reading = client.read_live_equity()
    assert reading is not None and reading.complete is True
    assert abs(reading.equity - 440.0) < 1e-9


# ---- runner: 缓存 / 可疑 / 禁止开仓 ----


def test_partial_read_uses_fresh_cache(tmp_path) -> None:
    runner = _runner(tmp_path, [_ok(423.68), _partial(36.25)])
    runner._refresh_live_equity(NOW)
    assert abs(runner.account.last_good_equity - 423.68) < 1e-9
    assert runner.account.last_good_equity_ts_ms == NOW

    runner._refresh_live_equity(NOW + 15 * 60_000)
    assert abs(runner.account.equity - 423.68) < 1e-9
    assert runner._equity_ok is True
    # 缓存不被部分值覆盖
    assert abs(runner.account.last_good_equity - 423.68) < 1e-9
    assert runner.account.last_good_equity_ts_ms == NOW


def test_exception_uses_fresh_cache(tmp_path) -> None:
    runner = _runner(tmp_path, [_ok(400.0), RuntimeError("boom")])
    runner._refresh_live_equity(NOW)
    runner._refresh_live_equity(NOW + HOUR)
    assert abs(runner.account.equity - 400.0) < 1e-9
    assert runner._equity_ok is True


def test_stale_cache_blocks_new_entries(tmp_path) -> None:
    runner = _runner(tmp_path, [_ok(400.0), _partial(36.25)])
    runner._refresh_live_equity(NOW)
    runner._refresh_live_equity(NOW + EQUITY_CACHE_MAX_AGE_MS + 1)
    assert runner._equity_ok is False
    # 不得采用部分值
    assert abs(runner.account.equity - 400.0) < 1e-9


def test_no_cache_blocks_open_but_not_exits(tmp_path) -> None:
    runner = _runner(tmp_path, [_partial(36.25)])
    runner.account.equity = 423.68
    runner._refresh_live_equity(NOW)
    assert runner._equity_ok is False
    assert abs(runner.account.equity - 423.68) < 1e-9

    placed: list = []
    runner.client.place = lambda intent, **kw: placed.append(intent) or None  # type: ignore[method-assign]
    runner.client.connect_sdk = lambda **kw: None  # type: ignore[method-assign]
    runner._live_book_ok = True
    runner._execute(_open_intent(), NOW)
    assert placed == []

    reduce = replace(_open_intent(), action=IntentAction.CLOSE, reduce_only=True, side=Side.SHORT)
    runner._execute(reduce, NOW)
    assert len(placed) == 1


def test_suspect_drop_uses_cache_then_confirms(tmp_path) -> None:
    runner = _runner(tmp_path, [_ok(423.68), _ok(150.0), _ok(152.0)])
    runner._refresh_live_equity(NOW)
    runner._refresh_live_equity(NOW + 15 * 60_000)
    assert abs(runner.account.equity - 423.68) < 1e-9, "单轮 >50% 骤降且无已实现亏损 → 用缓存"
    assert runner._equity_ok is True
    # 第二轮读数一致 → 视为真实变化，接受
    runner._refresh_live_equity(NOW + 30 * 60_000)
    assert abs(runner.account.equity - 152.0) < 1e-9
    assert abs(runner.account.last_good_equity - 152.0) < 1e-9


def test_drop_explained_by_realized_loss_is_accepted(tmp_path) -> None:
    runner = _runner(tmp_path, [_ok(400.0), _ok(150.0)])
    runner._refresh_live_equity(NOW)
    runner.account.realized_pnl_today -= 240.0
    runner._refresh_live_equity(NOW + 15 * 60_000)
    assert abs(runner.account.equity - 150.0) < 1e-9


def test_moderate_drop_is_accepted(tmp_path) -> None:
    runner = _runner(tmp_path, [_ok(400.0), _ok(250.0)])
    runner._refresh_live_equity(NOW)
    runner._refresh_live_equity(NOW + 15 * 60_000)
    assert abs(runner.account.equity - 250.0) < 1e-9


def test_last_good_persisted_and_seeds_abstraction(tmp_path) -> None:
    runner = _runner(tmp_path, [_ok(423.68)])
    runner._refresh_live_equity(NOW)
    runner.broker.save()

    loaded = PaperBroker.load(runner.cfg.state_path, 10_000.0, runner.account.day_key, runner.account.week_key)
    assert abs(loaded.account.last_good_equity - 423.68) < 1e-9
    assert loaded.account.last_good_equity_ts_ms == NOW
    assert loaded.account.equity_abstraction == "unifiedAccount"

    reloaded = BotRunner(runner.cfg)
    assert reloaded.client.last_abstraction == "unifiedAccount"
