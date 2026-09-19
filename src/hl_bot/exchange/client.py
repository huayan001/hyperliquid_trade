"""Hyperliquid 行情 / 交易封装。公开数据走官方 /info；实盘下单走官方 Python SDK。"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from hl_bot.config import BotConfig
from hl_bot.models import Candle, FundingInfo, MarketSnapshot, OrderIntent, OrderKind, Side

logger = logging.getLogger(__name__)

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def candle_from_hl(raw: dict[str, Any]) -> Candle:
    return Candle(
        ts=int(raw["t"]),
        end_ts=int(raw["T"]),
        open=float(raw["o"]),
        high=float(raw["h"]),
        low=float(raw["l"]),
        close=float(raw["c"]),
        volume=float(raw.get("v") or 0.0),
    )


class HyperliquidClient:
    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self.base_url = cfg.api_url.rstrip("/")
        self._info = None
        self._exchange = None
        self._sdk_ready = False

    def _post_info(self, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/info",
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "hl-bot/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Hyperliquid /info HTTP {exc.code}: {body}") from exc

    def connect_sdk(self, *, for_trading: bool = False) -> None:
        if self._sdk_ready and (self._exchange is not None or not for_trading):
            return
        try:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
        except ImportError as exc:  # pragma: no cover
            if for_trading:
                raise RuntimeError("实盘需要安装 hyperliquid-python-sdk") from exc
            logger.warning("未安装官方 SDK，公开行情改用 REST /info")
            return

        url = constants.TESTNET_API_URL if self.cfg.network == "testnet" else constants.MAINNET_API_URL
        self._info = Info(url, skip_ws=True)
        if for_trading:
            self.cfg.require_live_ready()
            import eth_account
            from hyperliquid.exchange import Exchange

            account = eth_account.Account.from_key(self.cfg.private_key)
            address = self.cfg.account_address or account.address
            self._exchange = Exchange(account, url, account_address=address)
            logger.info("已连接 Exchange，账户 %s（网络 %s）", address, self.cfg.network)
        self._sdk_ready = True

    def fetch_candles(self, symbol: str, interval: str, count: int = 120) -> list[Candle]:
        now = int(time.time() * 1000)
        span = INTERVAL_MS[interval] * (count + 2)
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": interval,
                "startTime": now - span,
                "endTime": now,
            },
        }
        raw = self._post_info(payload)
        candles = [candle_from_hl(item) for item in raw]
        candles.sort(key=lambda c: c.ts)
        # 去掉尚未收盘的最后一根，避免用影线/未完成K做突破确认
        closed = [c for c in candles if c.end_ts < now]
        return closed[-count:]

    def fetch_mids(self) -> dict[str, float]:
        raw = self._post_info({"type": "allMids"})
        return {str(k): float(v) for k, v in raw.items()}

    def fetch_asset_contexts(self) -> dict[str, dict[str, Any]]:
        raw = self._post_info({"type": "metaAndAssetCtxs"})
        universe = raw[0]["universe"]
        ctxs = raw[1]
        out: dict[str, dict[str, Any]] = {}
        for meta, ctx in zip(universe, ctxs):
            name = str(meta["name"])
            out[name] = {
                "funding": float(ctx.get("funding") or 0.0),
                "mark_px": float(ctx.get("markPx") or ctx.get("midPx") or 0.0),
                "mid_px": float(ctx.get("midPx") or ctx.get("markPx") or 0.0),
                "max_leverage": int(meta.get("maxLeverage") or 10),
                "only_isolated": bool(meta.get("onlyIsolated") or False),
                "sz_decimals": int(meta.get("szDecimals") or 4),
            }
        return out

    def fetch_funding_history(self, symbol: str, hours: int = 24) -> list[dict[str, Any]]:
        now = int(time.time() * 1000)
        raw = self._post_info(
            {
                "type": "fundingHistory",
                "coin": symbol,
                "startTime": now - hours * 3_600_000,
                "endTime": now,
            }
        )
        return list(raw or [])

    def funding_info(self, symbol: str, current_hourly: float) -> FundingInfo:
        hist = self.fetch_funding_history(symbol, 24)
        rates = [float(x.get("fundingRate") or 0.0) for x in hist]
        avg = sum(rates) / len(rates) if rates else current_hourly
        return FundingInfo(hourly_rate=current_hourly, avg_24h=avg)

    def load_market(self, symbol: str, ctx: dict[str, Any] | None = None, mid: float | None = None) -> MarketSnapshot:
        ctxs = {symbol: ctx} if ctx else self.fetch_asset_contexts()
        info = ctxs.get(symbol) or {}
        mids = {} if mid is not None else self.fetch_mids()
        px = mid if mid is not None else float(mids.get(symbol) or info.get("mid_px") or 0.0)
        daily = self.fetch_candles(symbol, "1d", 120)
        h4 = self.fetch_candles(symbol, "4h", 80)
        h1 = self.fetch_candles(symbol, "1h", 120)
        funding = self.funding_info(symbol, float(info.get("funding") or 0.0))
        return MarketSnapshot(
            symbol=symbol,
            mid=px,
            daily=tuple(daily),
            h4=tuple(h4),
            h1=tuple(h1),
            funding=funding,
            max_leverage=int(info.get("max_leverage") or self.cfg.max_leverage_for(symbol)),
        )

    def account_equity(self, fallback: float) -> float:
        if self.cfg.dry_run or not self.cfg.account_address:
            return fallback
        try:
            self.connect_sdk(for_trading=False)
            if self._info is None:
                raw = self._post_info({"type": "clearinghouseState", "user": self.cfg.account_address})
            else:
                raw = self._info.user_state(self.cfg.account_address)
            return float(raw["marginSummary"]["accountValue"])
        except Exception as exc:  # pragma: no cover
            logger.warning("读取账户权益失败，回退模拟权益: %s", exc)
            return fallback

    def round_size(self, symbol: str, size: float, sz_decimals: int | None = None) -> float:
        dec = sz_decimals if sz_decimals is not None else 4
        factor = 10**dec
        return math_floor(size, factor)

    def place(self, intent: OrderIntent, *, sz_decimals: int = 4) -> dict[str, Any]:
        if self.cfg.dry_run or self._exchange is None:
            return {"status": "dry_run", "intent": intent.reason}
        self.cfg.require_live_ready()
        is_buy = intent.side is Side.LONG
        size = self.round_size(intent.symbol, intent.size, sz_decimals)
        if size <= 0:
            return {"status": "error", "message": "rounded size is 0"}

        if not intent.reduce_only:
            self._exchange.update_leverage(int(intent.leverage), intent.symbol, is_cross=not intent.isolated)

        if intent.kind is OrderKind.MARKET:
            if intent.reduce_only:
                return self._exchange.market_close(intent.symbol, sz=size)
            return self._exchange.market_open(intent.symbol, is_buy, size)
        tif = "Alo" if intent.kind is OrderKind.MAKER_LIMIT else "Gtc"
        result = self._exchange.order(
            intent.symbol,
            is_buy,
            size,
            float(f"{intent.price:.5g}"),
            {"limit": {"tif": tif}},
            reduce_only=intent.reduce_only,
        )
        if intent.stop_price and not intent.reduce_only:
            sl_is_buy = not is_buy
            stop_px = float(f"{intent.stop_price:.5g}")
            sl_type = {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}}
            sl = self._exchange.order(
                intent.symbol, sl_is_buy, size, stop_px, sl_type, reduce_only=True
            )
            return {"status": "ok", "order": result, "stop": sl}
        return result


def math_floor(size: float, factor: int) -> float:
    return int(size * factor) / factor
