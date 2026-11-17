"""/info 传输错误退避重试，扫描循环遇到网络错误跳过本轮而不退出。"""

from __future__ import annotations

import http.client
import io
import json
import logging
import ssl
import urllib.error
import urllib.request

import pytest

from hl_bot.config import BotConfig
from hl_bot.exchange.client import (
    HTTP_TIMEOUT_S,
    HyperliquidClient,
    RateLimitError,
    _bind_http_timeout,
    is_http_timeout,
    is_transient_network_error,
)
from hl_bot.exchange.paper import PaperBroker
from hl_bot.runner import BotRunner

_ATTEMPTS = 5


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"ok": true}'


def _patch_sleep(monkeypatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr("hl_bot.exchange.client.time.sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


def _client() -> HyperliquidClient:
    return HyperliquidClient(BotConfig())


def _url_error() -> urllib.error.URLError:
    return urllib.error.URLError(
        ssl.SSLError(1, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
    )


def _ssl_error() -> ssl.SSLError:
    return ssl.SSLError(1, "UNEXPECTED_EOF_WHILE_READING")


def _remote_disconnected() -> http.client.RemoteDisconnected:
    return http.client.RemoteDisconnected("Remote end closed connection without response")


def _incomplete_read() -> http.client.IncompleteRead:
    return http.client.IncompleteRead(b"partial")


def _connection_reset() -> ConnectionResetError:
    return ConnectionResetError("Connection reset by peer")


def _timeout() -> TimeoutError:
    return TimeoutError("timed out")


_TRANSPORT = [
    ("URLError", _url_error, urllib.error.URLError),
    ("SSLError", _ssl_error, ssl.SSLError),
    ("RemoteDisconnected", _remote_disconnected, http.client.RemoteDisconnected),
    ("IncompleteRead", _incomplete_read, http.client.IncompleteRead),
    ("ConnectionResetError", _connection_reset, ConnectionResetError),
    ("TimeoutError", _timeout, TimeoutError),
]


def _install_urlopen(monkeypatch, fail_times: int, error_factory):
    calls = {"n": 0}

    def urlopen(req, timeout=20):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise error_factory()
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


@pytest.mark.parametrize("name, factory, exc_type", _TRANSPORT, ids=[row[0] for row in _TRANSPORT])
def test_post_info_retries_transport_then_succeeds(monkeypatch, caplog, name, factory, exc_type) -> None:
    calls = _install_urlopen(monkeypatch, fail_times=2, error_factory=factory)
    sleeps = _patch_sleep(monkeypatch)
    client = _client()
    with caplog.at_level(logging.WARNING, logger="hl_bot.exchange.client"):
        assert client._post_info({"type": "allMids"}) == {"ok": True}
    assert calls["n"] == 3
    warnings = [r for r in caplog.records if "传输错误" in r.getMessage()]
    assert len(warnings) == 2
    assert all(r.levelno == logging.WARNING for r in warnings)
    assert [s for s in sleeps if s >= 1.0] == [2.0, 4.0]
    assert client.budget.rate_limits == 0
    assert client.budget.requests == 1


@pytest.mark.parametrize("name, factory, exc_type", _TRANSPORT, ids=[row[0] for row in _TRANSPORT])
def test_post_info_transport_retries_exhausted(monkeypatch, caplog, name, factory, exc_type) -> None:
    calls = _install_urlopen(monkeypatch, fail_times=_ATTEMPTS, error_factory=factory)
    sleeps = _patch_sleep(monkeypatch)
    client = _client()
    with caplog.at_level(logging.WARNING, logger="hl_bot.exchange.client"):
        with pytest.raises(exc_type):
            client._post_info({"type": "metaAndAssetCtxs"})
    assert calls["n"] == _ATTEMPTS
    warnings = [r for r in caplog.records if "传输错误" in r.getMessage()]
    assert len(warnings) == _ATTEMPTS - 1
    assert [s for s in sleeps if s >= 1.0] == [2.0, 4.0, 8.0, 16.0]
    assert client.budget.requests == 0
    assert client.budget.rate_limits == 0


def test_post_info_retries_http_5xx_then_succeeds(monkeypatch, caplog) -> None:
    calls = {"n": 0}

    def urlopen(req, timeout=20):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(
                req.full_url,
                503,
                "Service Unavailable",
                hdrs=None,
                fp=io.BytesIO(b"down"),
            )
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    _patch_sleep(monkeypatch)
    client = _client()
    with caplog.at_level(logging.WARNING, logger="hl_bot.exchange.client"):
        assert client._post_info({"type": "allMids"}) == {"ok": True}
    assert calls["n"] == 3
    warnings = [r for r in caplog.records if "HTTP 503" in r.getMessage()]
    assert len(warnings) == 2
    assert all(r.levelno == logging.WARNING for r in warnings)
    assert client.budget.rate_limits == 0


def test_post_info_http_5xx_exhausted_is_transient(monkeypatch) -> None:
    def urlopen(req, timeout=20):
        raise urllib.error.HTTPError(
            req.full_url,
            502,
            "Bad Gateway",
            hdrs=None,
            fp=io.BytesIO(b"bad"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    _patch_sleep(monkeypatch)
    client = _client()
    with pytest.raises(RuntimeError, match="HTTP 502") as caught:
        client._post_info({"type": "meta"})
    assert is_transient_network_error(caught.value)
    assert not isinstance(caught.value, RateLimitError)


def test_post_info_does_not_retry_http_4xx(monkeypatch) -> None:
    calls = {"n": 0}

    def urlopen(req, timeout=20):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=io.BytesIO(b"no"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    _patch_sleep(monkeypatch)
    client = _client()
    with pytest.raises(RuntimeError, match="HTTP 400") as caught:
        client._post_info({"type": "allMids"})
    assert calls["n"] == 1
    assert not is_transient_network_error(caught.value)


def test_post_info_does_not_retry_bad_json(monkeypatch) -> None:
    calls = {"n": 0}

    class Bad(_Resp):
        def read(self):
            return b"not-json"

    def urlopen(req, timeout=20):
        calls["n"] += 1
        return Bad()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    _patch_sleep(monkeypatch)
    client = _client()
    with pytest.raises(json.JSONDecodeError):
        client._post_info({"type": "allMids"})
    assert calls["n"] == 1


def test_exchange_post_is_not_retried(monkeypatch) -> None:
    """下单/撤单走 SDK /exchange：传输错误只抛一次，避免重复下单。"""
    sleeps = _patch_sleep(monkeypatch)
    client = _client()
    calls = {"n": 0}

    class Exchange:
        def post(self, url_path, payload=None):
            calls["n"] += 1
            raise _url_error()

    exchange = Exchange()
    client._install_exchange_throttle(exchange)
    with pytest.raises(urllib.error.URLError):
        exchange.post("/exchange", {"action": {"type": "order", "orders": [{"a": 1}]}})
    assert calls["n"] == 1
    assert [s for s in sleeps if s >= 1.0] == []


def test_run_forever_skips_network_iteration(tmp_path, monkeypatch, caplog) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("hl_bot.runner.time.sleep", lambda seconds: sleeps.append(seconds))
    cfg = BotConfig()
    cfg.poll_seconds = 15
    cfg.symbols = ("ETH",)
    cfg.state_path = str(tmp_path / "paper.json")
    runner = BotRunner(cfg)
    runner.broker.save()
    before = (tmp_path / "paper.json").read_bytes()
    saves = {"n": 0}
    real_save = runner.broker.save

    def counting_save() -> None:
        saves["n"] += 1
        real_save()

    runner.broker.save = counting_save  # type: ignore[method-assign]
    calls = {"n": 0}

    def contexts():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _url_error()
        raise KeyboardInterrupt

    runner.client.fetch_asset_contexts = contexts  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="hl_bot.runner"):
        with pytest.raises(KeyboardInterrupt):
            runner.run_forever()
    assert calls["n"] == 2
    assert sleeps == [15]
    skipped = [r for r in caplog.records if "跳过本轮" in r.getMessage()]
    assert len(skipped) == 1
    assert skipped[0].exc_info is not None
    assert skipped[0].exc_info[0] is urllib.error.URLError
    assert (tmp_path / "paper.json").read_bytes() == before
    assert not (tmp_path / "paper.json.tmp").exists()
    assert saves["n"] == 0


def test_run_forever_skips_wrapped_http_5xx(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hl_bot.runner.time.sleep", lambda *_a, **_k: None)
    cfg = BotConfig()
    cfg.state_path = str(tmp_path / "paper.json")
    cfg.poll_seconds = 15
    runner = BotRunner(cfg)
    calls = {"n": 0}

    def scan_once(*, persist_paper: bool = False):
        calls["n"] += 1
        if calls["n"] == 1:
            try:
                raise urllib.error.HTTPError(
                    "https://api.hyperliquid.xyz/info",
                    503,
                    "Service Unavailable",
                    hdrs=None,
                    fp=io.BytesIO(b"down"),
                )
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"Hyperliquid /info HTTP {exc.code}: down") from exc
        raise KeyboardInterrupt

    runner.scan_once = scan_once  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        runner.run_forever()
    assert calls["n"] == 2


def _requests_read_timeout(message: str) -> OSError:
    """requests.ReadTimeout 不是内置 TimeoutError，但扫描循环必须把它当成超时。"""

    class ReadTimeout(OSError):
        pass

    ReadTimeout.__module__ = "requests.exceptions"
    return ReadTimeout(message)


def test_requests_read_timeout_is_transient() -> None:
    exc = _requests_read_timeout(
        "HTTPSConnectionPool(host='api.hyperliquid.xyz', port=443): Read timed out. (read timeout=20)"
    )
    assert type(exc).__name__ == "ReadTimeout"
    assert is_http_timeout(exc)
    assert is_transient_network_error(exc)
    assert not is_http_timeout(RateLimitError("429"))


def test_post_info_passes_finite_timeout(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert _client()._post_info({"type": "allMids"}) == {"ok": True}
    assert seen["timeout"] == HTTP_TIMEOUT_S
    assert HTTP_TIMEOUT_S == 20


class _Session:
    def __init__(self, *, raise_timeout: bool = False) -> None:
        self.calls = 0
        self.closed = False
        self.timeouts: list[object] = []
        self.raise_timeout = raise_timeout

    def request(self, method, url, **kwargs):
        self.calls += 1
        self.timeouts.append(kwargs.get("timeout"))
        if self.raise_timeout:
            raise _requests_read_timeout("Read timed out. (read timeout=20)")
        return {"ok": True}

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self.closed = True


class _SdkApi:
    """模拟官方 SDK：post 把 self.timeout 原样传给 session，默认是 None。"""

    def __init__(self, *, raise_timeout: bool = False, force_none: bool = False) -> None:
        self.timeout = None
        self.session = _Session(raise_timeout=raise_timeout)
        self.force_none = force_none

    def post(self, url_path, payload=None):
        timeout = None if self.force_none else self.timeout
        return self.session.post("https://api.hyperliquid.xyz" + url_path, json=payload, timeout=timeout)


def test_real_sdk_and_requests_timeout_are_finite() -> None:
    """官方 SDK 的 post 会把 timeout=self.timeout 传给 requests，默认是 None。"""
    import requests
    from hyperliquid.api import API

    real_timeout = requests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='api.hyperliquid.xyz', port=443): Read timed out. (read timeout=None)"
    )
    assert is_http_timeout(real_timeout)
    assert is_transient_network_error(real_timeout)

    api = API("https://api.hyperliquid.xyz")
    assert api.timeout is None
    seen: dict[str, object] = {}

    def request(method, url, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        seen["url"] = url

        class Resp:
            status_code = 200
            text = '{"ok": true}'
            headers: dict[str, str] = {}

            def json(self):
                return {"ok": True}

        return Resp()

    api.session.request = request
    _bind_http_timeout(api)
    assert api.timeout == HTTP_TIMEOUT_S
    assert api.post("/info", {"type": "clearinghouseState", "user": "0xabc"}) == {"ok": True}
    assert seen["timeout"] == HTTP_TIMEOUT_S
    assert str(seen["url"]).endswith("/info")


def test_sdk_timeout_none_becomes_20_seconds() -> None:
    api = _SdkApi()
    _bind_http_timeout(api)
    assert api.timeout == HTTP_TIMEOUT_S
    assert api.post("/info", {"type": "clearinghouseState", "user": "0xabc"}) == {"ok": True}
    assert api.session.timeouts == [HTTP_TIMEOUT_S]

    forced = _SdkApi(force_none=True)
    _bind_http_timeout(forced)
    assert forced.post("/exchange", {"action": {}}) == {"ok": True}
    assert forced.session.timeouts == [HTTP_TIMEOUT_S]


def test_sdk_keeps_explicit_positive_timeout() -> None:
    api = _SdkApi()
    api.timeout = 7
    _bind_http_timeout(api)
    assert api.timeout == 7
    api.post("/info", {"type": "allMids"})
    assert api.session.timeouts == [7]


def test_sdk_read_timeout_closes_session_and_is_not_retried(monkeypatch, caplog) -> None:
    sleeps = _patch_sleep(monkeypatch)
    client = _client()
    api = _SdkApi(raise_timeout=True)
    client._install_exchange_throttle(api)
    _bind_http_timeout(api)
    with caplog.at_level(logging.WARNING, logger="hl_bot.exchange.client"):
        with pytest.raises(OSError, match="Read timed out"):
            api.post("/exchange", {"action": {"type": "order", "orders": [{"a": 1}]}})
    assert api.session.calls == 1
    assert api.session.closed is True
    assert [s for s in sleeps if s >= 1.0] == []
    assert any("HTTP 超时" in record.getMessage() for record in caplog.records)


def test_clearinghouse_timeout_uses_cached_equity(tmp_path, monkeypatch, caplog) -> None:
    _patch_sleep(monkeypatch)
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.account_address = "0xabc"
    cfg.state_path = str(tmp_path / "paper.json")
    runner = BotRunner(cfg)
    now_ms = 1_790_000_000_000
    runner._accept_equity(400.0, now_ms, "unifiedAccount")

    class Info:
        def user_state(self, user):
            raise TimeoutError("HTTPSConnectionPool(host='api.hyperliquid.xyz', port=443): Read timed out. (read timeout=20)")

        def spot_user_state(self, user):
            raise TimeoutError("Read timed out. (read timeout=20)")

    runner.client._info = Info()
    runner.client.connect_sdk = lambda **kwargs: None  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        runner._refresh_live_equity(now_ms + 1_000)
    assert runner._equity_ok is True
    assert runner.account.equity == 400.0
    assert "超时" in caplog.text
    assert "使用缓存权益" in caplog.text
    assert "perps clearinghouseState" in caplog.text


def test_run_forever_continues_after_http_timeout_without_marking_scan(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("HL_SCAN_OK_FILE", str(tmp_path / "live_scan.ok"))
    monkeypatch.setattr("hl_bot.runner.time.sleep", lambda *_a, **_k: None)
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.state_path = str(tmp_path / "paper.json")
    cfg.poll_seconds = 15
    runner = BotRunner(cfg)
    calls = {"n": 0}

    def scan_once(*, persist_paper: bool = False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _requests_read_timeout("Read timed out. (read timeout=20)")
        raise KeyboardInterrupt

    runner.scan_once = scan_once  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="hl_bot.runner"):
        with pytest.raises(KeyboardInterrupt):
            runner.run_forever()
    assert calls["n"] == 2
    assert any("HTTP 超时" in record.getMessage() and "跳过本轮" in record.getMessage() for record in caplog.records)
    assert not (tmp_path / "live_scan.ok").exists()


def test_state_save_replace_failure_keeps_previous_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "paper.json"
    broker = PaperBroker.load(str(path), 100.0, "2026-09-29", "2026-W40")
    broker.account.equity = 100.0
    broker.save()
    original = path.read_bytes()
    assert not (tmp_path / "paper.json.tmp").exists()
    broker.account.equity = 50.0

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr("hl_bot.exchange.paper.os.replace", boom)
    with pytest.raises(OSError, match="disk"):
        broker.save()
    assert path.read_bytes() == original
    assert b'"equity": 100.0' in original
    assert (tmp_path / "paper.json.tmp").exists()
    reloaded = PaperBroker.load(str(path), 1.0, "d", "w")
    assert reloaded.account.equity == 100.0
