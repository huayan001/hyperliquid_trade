"""K 线缓存与全量等价、令牌桶节流、一轮扫描共享账户快照。"""

from __future__ import annotations

import io
import logging
import urllib.error
import urllib.request

import pytest

from hl_bot.config import BotConfig
from hl_bot.exchange.candle_cache import candle_from_raw, merge_closed
from hl_bot.exchange.client import INTERVAL_MS, HyperliquidClient, RateLimitError, candle_from_hl
from hl_bot.exchange.throttle import RequestThrottle, exchange_weight, info_weight
from hl_bot.indicators import adx, ema
from hl_bot.runner import BotRunner

INTERVAL = INTERVAL_MS["1h"]
ORIGIN = (1_700_000_000_000 // INTERVAL) * INTERVAL


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


def _bar(i: int, *, close: float | None = None, interval: int = INTERVAL, origin: int = ORIGIN) -> dict:
    ts = origin + i * interval
    px = float(i if close is None else close)
    return {
        "t": ts,
        "T": ts + interval - 1,
        "o": px,
        "h": px + 1.0,
        "l": px - 1.0,
        "c": px + 0.25,
        "v": 2.0 + i,
    }


def _series(n: int, *, close_of=None) -> list[dict]:
    return [_bar(i, close=None if close_of is None else close_of(i)) for i in range(n)]


def _closed_tail(rows: list[dict], now_ms: int, count: int, *, span_bars: int | None = None) -> list:
    span = INTERVAL * ((span_bars if span_bars is not None else count) + 2)
    window = [row for row in rows if now_ms - span <= row["t"] <= now_ms]
    candles = [candle_from_hl(row) for row in window]
    candles.sort(key=lambda c: c.ts)
    closed = [c for c in candles if c.end_ts < now_ms]
    return closed[-count:]


def _client(tmp_path, post) -> HyperliquidClient:
    cfg = BotConfig()
    cfg.state_path = str(tmp_path / "paper.json")
    client = HyperliquidClient(cfg)
    client._post_info = post  # type: ignore[method-assign]
    return client


def test_merge_prefers_fresh_ohlc_and_rejects_gaps() -> None:
    cached = [candle_from_raw(_bar(i)) for i in range(5)]
    fresh_raw = _bar(4)
    fresh_raw["c"] = 99.0
    fresh = [candle_from_raw(fresh_raw), candle_from_raw(_bar(5))]
    now = _bar(6)["t"]
    merged = merge_closed(cached, fresh, interval_ms=INTERVAL, now_ms=now)
    assert merged is not None
    assert [c.ts for c in merged] == [candle_from_raw(_bar(i)).ts for i in range(6)]
    assert merged[4].close == 99.0

    gapped = [candle_from_raw(_bar(4)), candle_from_raw(_bar(6))]
    assert merge_closed(cached, gapped, interval_ms=INTERVAL, now_ms=_bar(7)["t"]) is None
    assert merge_closed(cached, [], interval_ms=INTERVAL, now_ms=now) is None


def test_info_weight_and_exchange_weight() -> None:
    assert info_weight({"type": "allMids"}) == 2
    assert info_weight({"type": "clearinghouseState"}) == 2
    assert info_weight({"type": "spotClearinghouseState"}) == 2
    assert info_weight({"type": "metaAndAssetCtxs"}) == 20
    assert info_weight({"type": "frontendOpenOrders"}) == 20
    assert info_weight({"type": "userAbstraction"}) == 20
    assert info_weight({"type": "userRole"}) == 60
    now = ORIGIN + 500 * INTERVAL
    candle_payload = {
        "type": "candleSnapshot",
        "req": {"coin": "ETH", "interval": "1h", "startTime": now - 122 * INTERVAL, "endTime": now},
    }
    assert info_weight(candle_payload) == 20 + 123 // 60
    assert info_weight(candle_payload, [{}] * 122) == 20 + 122 // 60
    funding = {"type": "fundingHistory", "coin": "ETH", "startTime": now - 24 * INTERVAL, "endTime": now}
    assert info_weight(funding, [{}] * 24) == 20 + 24 // 20
    assert exchange_weight(1) == 1
    assert exchange_weight(40) == 2
    assert exchange_weight(79) == 2


def test_throttle_gap_bucket_and_exchange_is_independent() -> None:
    clock = Clock()
    throttle = RequestThrottle(
        info_per_minute=10_000,
        info_min_gap=0.25,
        exchange_per_minute=10_000,
        exchange_min_gap=0.05,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    throttle.acquire_info(1)
    assert clock.now == 0
    throttle.acquire_info(1)
    assert clock.now == pytest.approx(0.25)
    info_tokens = throttle.info_tokens
    # 交易所请求不跟 info 间隔绑在一起，只在自己的调用之间留更短的空隙
    throttle.acquire_exchange(1)
    assert clock.now == pytest.approx(0.25)
    throttle.acquire_exchange(1)
    assert clock.now == pytest.approx(0.30)
    assert throttle.info_tokens == info_tokens

    clock = Clock()
    limited = RequestThrottle(
        info_per_minute=60,
        info_min_gap=0,
        exchange_per_minute=60,
        exchange_min_gap=0,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    limited.acquire_info(60)
    assert clock.now == 0
    limited.acquire_info(30)
    assert clock.now == pytest.approx(30)


def test_fetch_candles_incremental_equals_full_fetch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hl_bot.exchange.client.time.sleep", lambda *_a, **_k: None)
    series = _series(80)
    calls: list[dict] = []
    now = {"ms": _bar(60)["t"]}

    def post(payload):
        calls.append(payload)
        req = payload["req"]
        start, end = req["startTime"], req["endTime"]
        return [row for row in series if start <= row["t"] <= end]

    monkeypatch.setattr("hl_bot.exchange.client.time.time", lambda: now["ms"] / 1000.0)
    client = _client(tmp_path, post)
    count = 40
    first = client.fetch_candles("ETH", "1h", count)
    assert first == _closed_tail(series, now["ms"], count)
    assert len(calls) == 1
    assert calls[0]["req"]["startTime"] == now["ms"] - INTERVAL * (count + 2)

    series[59]["c"] = 1234.5
    now["ms"] = _bar(64)["t"]
    second = client.fetch_candles("ETH", "1h", count)
    full = _closed_tail(series, now["ms"], count)
    assert second == full
    assert len(calls) == 2
    assert calls[1]["req"]["startTime"] == first[-1].ts - 2 * INTERVAL
    assert calls[1]["req"]["endTime"] - calls[1]["req"]["startTime"] < INTERVAL * (count + 2)
    assert ema([c.close for c in second], 20) == ema([c.close for c in full], 20)
    assert adx(second, 14) == adx(full, 14)
    assert second[-1].ts == candle_from_hl(_bar(63)).ts
    assert any(c.close == 1234.5 for c in second)


def test_fetch_candles_gap_or_corrupt_falls_back_to_full(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hl_bot.exchange.client.time.sleep", lambda *_a, **_k: None)
    series = _series(80)
    calls: list[tuple[int, int]] = []
    now = {"ms": _bar(60)["t"]}
    drop = {"on": False}

    def post(payload):
        req = payload["req"]
        start, end = int(req["startTime"]), int(req["endTime"])
        calls.append((start, end))
        rows = [row for row in series if start <= row["t"] <= end]
        if drop["on"] and end - start < INTERVAL * 30:
            missing = _bar(61)["t"]
            rows = [row for row in rows if row["t"] != missing]
        return rows

    monkeypatch.setattr("hl_bot.exchange.client.time.time", lambda: now["ms"] / 1000.0)
    client = _client(tmp_path, post)
    count = 40
    assert client.fetch_candles("ETH", "1h", count) == _closed_tail(series, now["ms"], count)

    drop["on"] = True
    now["ms"] = _bar(64)["t"]
    before = len(calls)
    got = client.fetch_candles("ETH", "1h", count)
    assert got == _closed_tail(series, now["ms"], count)
    assert len(calls) == before + 2

    path = client.candle_cache.path_for("ETH", "1h")
    path.write_text("{", encoding="utf-8")
    before = len(calls)
    got = client.fetch_candles("ETH", "1h", count)
    assert got == _closed_tail(series, now["ms"], count)
    assert len(calls) == before + 1


def test_short_history_stays_incremental(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hl_bot.exchange.client.time.sleep", lambda *_a, **_k: None)
    series = _series(5)
    calls: list[dict] = []
    now_ms = _bar(5)["t"]

    def post(payload):
        calls.append(payload)
        req = payload["req"]
        return [row for row in series if req["startTime"] <= row["t"] <= req["endTime"]]

    monkeypatch.setattr("hl_bot.exchange.client.time.time", lambda: now_ms / 1000.0)
    client = _client(tmp_path, post)
    count = 20
    first = client.fetch_candles("SOL", "1h", count)
    second = client.fetch_candles("SOL", "1h", count)
    full = _closed_tail(series, now_ms, count)
    assert first == full == second
    assert len(full) == 5
    assert len(calls) == 2
    full_span = INTERVAL * (count + 2)
    assert calls[1]["req"]["endTime"] - calls[1]["req"]["startTime"] < full_span


def test_shared_book_and_equity_snapshot_single_read(tmp_path) -> None:
    calls: list[str] = []

    def post(payload):
        kind = payload["type"]
        calls.append(kind)
        if kind == "clearinghouseState":
            return {
                "marginSummary": {"accountValue": "12"},
                "crossMarginSummary": {"accountValue": "12"},
                "withdrawable": "0",
                "assetPositions": [],
            }
        if kind == "spotClearinghouseState":
            return {"balances": [{"coin": "USDC", "token": 0, "total": "30", "hold": "0"}]}
        if kind == "frontendOpenOrders":
            return [{"coin": "ETH", "oid": 7, "sz": "1"}]
        if kind == "userAbstraction":
            return "unifiedAccount"
        raise AssertionError(kind)

    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    cfg.state_path = str(tmp_path / "paper.json")
    client = HyperliquidClient(cfg)
    client.connect_sdk = lambda **_k: None  # type: ignore[method-assign]
    client._post_info = post  # type: ignore[method-assign]
    client.begin_scan()
    snapshot = client.fetch_account_snapshot()
    reading = client.read_live_equity(snapshot)
    assert reading is not None and reading.complete
    assert abs(reading.equity - 30.0) < 1e-9
    positions = client.fetch_perp_positions()
    again = client.fetch_perp_positions()
    orders = client.fetch_open_orders()
    client.fetch_open_orders()
    assert positions == again
    assert orders[0]["oid"] == 7
    assert calls.count("clearinghouseState") == 1
    assert calls.count("spotClearinghouseState") == 1
    assert calls.count("frontendOpenOrders") == 1
    assert calls.count("userAbstraction") == 1

    client.shared_open_orders = None
    client.fetch_open_orders()
    assert calls.count("frontendOpenOrders") == 2
    client.shared_perps_raw = None
    client.fetch_perp_positions()
    assert calls.count("clearinghouseState") == 2


def test_perps_429_is_not_fetched_again_for_positions(tmp_path) -> None:
    calls: list[str] = []

    def post(payload):
        calls.append(payload["type"])
        if payload["type"] == "clearinghouseState":
            raise RateLimitError("Hyperliquid /info HTTP 429: null")
        if payload["type"] == "spotClearinghouseState":
            return {"balances": [{"coin": "USDC", "token": 0, "total": "5", "hold": "0"}]}
        raise AssertionError(payload["type"])

    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    cfg.state_path = str(tmp_path / "paper.json")
    client = HyperliquidClient(cfg)
    client.connect_sdk = lambda **_k: None  # type: ignore[method-assign]
    client._post_info = post  # type: ignore[method-assign]
    client.begin_scan()
    snapshot = client.fetch_account_snapshot()
    assert snapshot.perps_raw is None
    reading = client.read_live_equity(snapshot)
    assert reading is not None and reading.complete is False
    with pytest.raises(RateLimitError):
        client.fetch_perp_positions()
    assert calls.count("clearinghouseState") == 1


def test_scan_shares_mids_contexts_and_account_reads(tmp_path, caplog) -> None:
    calls: list[str] = []

    def post(payload):
        kind = payload["type"]
        calls.append(kind)
        if kind == "clearinghouseState":
            return {
                "marginSummary": {"accountValue": "10"},
                "crossMarginSummary": {"accountValue": "10"},
                "withdrawable": "0",
                "assetPositions": [],
            }
        if kind == "spotClearinghouseState":
            return {"balances": [{"coin": "USDC", "token": 0, "total": "20", "hold": "0"}]}
        if kind == "userAbstraction":
            return "unifiedAccount"
        if kind == "allMids":
            return {"ETH": "100", "SOL": "20", "HYPE": "5"}
        if kind == "metaAndAssetCtxs":
            names = ("ETH", "SOL", "HYPE")
            return [
                {"universe": [{"name": name, "maxLeverage": 10, "szDecimals": 4} for name in names]},
                [{"funding": "0.0001", "markPx": "1", "midPx": "1"} for _ in names],
            ]
        if kind == "frontendOpenOrders":
            return []
        if kind == "fundingHistory":
            return [{"fundingRate": "0.0001"}]
        if kind == "candleSnapshot":
            return []
        raise AssertionError(kind)

    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    cfg.symbols = ("ETH", "SOL", "HYPE")
    cfg.state_path = str(tmp_path / "paper.json")
    runner = BotRunner(cfg)
    runner.client.connect_sdk = lambda **_k: None  # type: ignore[method-assign]
    runner.client._post_info = post  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="hl_bot.exchange.throttle"):
        report = runner.scan_once()
    assert [item.symbol for item in report.symbols] == ["ETH", "SOL", "HYPE"]
    assert calls.count("clearinghouseState") == 1
    assert calls.count("spotClearinghouseState") == 1
    assert calls.count("frontendOpenOrders") == 1
    assert calls.count("userAbstraction") == 1
    assert calls.count("allMids") == 1
    assert calls.count("metaAndAssetCtxs") == 1
    assert calls.count("fundingHistory") == 3
    assert calls.count("candleSnapshot") == 9
    assert "本轮 API 统计" in caplog.text
    assert abs(runner.account.equity - 20.0) < 1e-9


def test_post_info_records_weight(monkeypatch) -> None:
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ETH": "1.5"}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=20: Resp())
    monkeypatch.setattr("hl_bot.exchange.client.time.sleep", lambda *_a, **_k: None)
    client = HyperliquidClient(BotConfig())
    client.begin_scan()
    assert client.fetch_mids()["ETH"] == 1.5
    assert client.budget.requests == 1
    assert client.budget.weight == 2
    assert client.budget.rate_limits == 0

    def boom(req, timeout=20):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", hdrs=None, fp=io.BytesIO(b"rate"))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RateLimitError):
        client._post_info({"type": "allMids"})
    assert client.budget.rate_limits == 5
