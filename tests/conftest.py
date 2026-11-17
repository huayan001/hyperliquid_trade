"""测试不得访问真实 Hyperliquid：默认盘口/成交查询走固定假数据，urlopen 直接报错。"""

from __future__ import annotations

import urllib.request

import pytest

from hl_bot.exchange.client import HyperliquidClient

FAKE_BOOK = (2722.0, 2722.6)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(*_args, **_kwargs):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    monkeypatch.setattr(HyperliquidClient, "fetch_best_bid_ask", lambda self, symbol: FAKE_BOOK)
    monkeypatch.setattr(HyperliquidClient, "fetch_user_fills_by_time", lambda self, start_ms, end_ms=None: [])
    yield
