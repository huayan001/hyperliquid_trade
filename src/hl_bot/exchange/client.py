"""Hyperliquid 行情 / 交易封装。公开数据走官方 /info；实盘下单走官方 Python SDK。"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from hl_bot.config import BotConfig
from hl_bot.exchange.equity import combine_live_equity
from hl_bot.models import (
    Candle,
    FundingInfo,
    IntentAction,
    MarketSnapshot,
    OrderIntent,
    OrderKind,
    Side,
)

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


@dataclass(frozen=True, slots=True)
class OrderFill:
    size: float
    price: float | None = None


def _as_positive_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def _collect_statuses(result: dict[str, Any]) -> list[Any]:
    statuses: list[Any] = []
    response = result.get("response")
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict) and isinstance(data.get("statuses"), list):
            statuses.extend(data["statuses"])
        elif isinstance(data, list):
            statuses.extend(data)
    if isinstance(result.get("statuses"), list):
        statuses.extend(result["statuses"])
    return statuses


def extract_order_fill(result: Any) -> OrderFill | None:
    """从 Hyperliquid SDK 返回值提取已成交数量。None / 无成交 / 仅 resting / error 视为未成交。"""
    if result is None or not isinstance(result, dict):
        return None
    status = str(result.get("status") or "").lower()
    if status in {"error", "err", "dry_run"}:
        return None

    nested = result.get("order")
    if isinstance(nested, dict):
        inner = extract_order_fill(nested)
        if inner is not None:
            return inner

    filled_sz = 0.0
    px_num = 0.0
    px_den = 0.0
    for item in _collect_statuses(result):
        if not isinstance(item, dict):
            continue
        if item.get("error"):
            continue
        filled = item.get("filled")
        if not isinstance(filled, dict):
            if "totalSz" in item or "sz" in item:
                filled = item
            else:
                continue
        sz = _as_positive_float(filled.get("totalSz") or filled.get("sz"))
        if sz <= 0:
            continue
        filled_sz += sz
        px = _as_positive_float(filled.get("avgPx") or filled.get("px"))
        if px > 0:
            px_num += px * sz
            px_den += sz

    if filled_sz <= 0:
        return None
    return OrderFill(size=filled_sz, price=(px_num / px_den) if px_den > 0 else None)


def collect_error_messages(result: Any) -> list[str]:
    """收集 SDK /info 下单返回里的 error 文案（含嵌套 order）。"""
    msgs: list[str] = []
    if result is None:
        return msgs
    if isinstance(result, str):
        if result.strip():
            msgs.append(result)
        return msgs
    if not isinstance(result, dict):
        return msgs
    err = result.get("error")
    if err:
        msgs.append(str(err))
    message = result.get("message")
    if message:
        msgs.append(str(message))
    nested = result.get("order")
    if isinstance(nested, dict):
        msgs.extend(collect_error_messages(nested))
    for item in _collect_statuses(result):
        if isinstance(item, dict) and item.get("error"):
            msgs.append(str(item["error"]))
        elif isinstance(item, str) and item.strip():
            msgs.append(item)
    return msgs


def is_post_only_reject(result: Any) -> bool:
    """Alo/post-only 因会立刻吃单被拒。"""
    for msg in collect_error_messages(result):
        low = msg.lower()
        if "post only" in low or "post-only" in low or "alonotallowed" in low:
            return True
    return False


def extract_resting_oids(result: Any) -> list[int]:
    """入场单 resting oid；不把同批 stop 的 oid 算进来。"""
    if result is None or not isinstance(result, dict):
        return []
    found: list[int] = []
    nested = result.get("order")
    if isinstance(nested, dict):
        found.extend(extract_resting_oids(nested))
    for item in _collect_statuses(result):
        if not isinstance(item, dict):
            continue
        resting = item.get("resting")
        if isinstance(resting, dict) and resting.get("oid") is not None:
            try:
                found.append(int(resting["oid"]))
            except (TypeError, ValueError):
                continue
    return _unique_oids(found)


def extract_oid(result: Any) -> int | None:
    oids = extract_oids(result)
    return oids[0] if oids else None


def extract_oids(result: Any) -> list[int]:
    if result is None or not isinstance(result, dict):
        return []
    found: list[int] = []
    nested = result.get("order")
    if isinstance(nested, dict):
        found.extend(extract_oids(nested))
    for item in _collect_statuses(result):
        if not isinstance(item, dict):
            continue
        for key in ("resting", "filled"):
            blob = item.get(key)
            if isinstance(blob, dict) and blob.get("oid") is not None:
                try:
                    found.append(int(blob["oid"]))
                except (TypeError, ValueError):
                    continue
        if item.get("oid") is not None:
            try:
                found.append(int(item["oid"]))
            except (TypeError, ValueError):
                continue
    return _unique_oids(found)


def _unique_oids(oids: list[int]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for oid in oids:
        if oid in seen:
            continue
        seen.add(oid)
        out.append(oid)
    return out


def protective_stop_size(intent: OrderIntent, filled_size: float) -> float:
    """保护止损张数 = 当前仓 + 本笔确认成交，绝不用失败加仓的计划 total_size。"""
    filled = max(0.0, float(filled_size))
    current = intent.extras.get("current_size")
    if current is None:
        planned = intent.extras.get("total_size")
        if planned is not None and intent.action is IntentAction.ADD:
            current = max(0.0, float(planned) - float(intent.size))
        else:
            current = 0.0
    else:
        current = max(0.0, float(current))
    return current + filled


def is_reduce_only_stop_order(order: dict[str, Any], symbol: str) -> bool:
    """frontendOpenOrders 里该币的 reduce-only 止损（含 trigger / tpsl=sl）。"""
    coin = str(order.get("coin") or order.get("symbol") or "")
    if coin.upper() != symbol.upper():
        return False
    reduce_only = bool(order.get("reduceOnly") or order.get("reduce_only"))
    is_trigger = bool(order.get("isTrigger") or order.get("is_trigger"))
    tpsl = str(order.get("tpsl") or "").lower()
    otype = str(order.get("orderType") or order.get("origType") or order.get("type") or "").lower()
    stop_like = is_trigger or tpsl == "sl" or "stop" in otype
    return reduce_only and stop_like


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

    def _fetch_info(self, payload: dict[str, Any]) -> Any:
        """公开 /info；调用方若已 connect_sdk，优先走官方 SDK。"""
        info_type = str(payload.get("type") or "")
        if self._info is not None:
            if info_type == "clearinghouseState" and hasattr(self._info, "user_state"):
                return self._info.user_state(payload["user"])
            if info_type == "spotClearinghouseState" and hasattr(self._info, "spot_user_state"):
                return self._info.spot_user_state(payload["user"])
        return self._post_info(payload)

    def _safe_info(self, payload: dict[str, Any], *, label: str) -> tuple[Any, str | None]:
        try:
            return self._fetch_info(payload), None
        except Exception as exc:
            logger.warning("读取%s失败: %s", label, exc)
            return None, f"{label}: {exc}"

    def account_equity(self, fallback: float) -> float:
        """实盘权益对齐 App「可用」：perps accountValue + 可用现货 USDC（按账户模式去重）。

        dry-run / 无地址时原样返回 fallback，纸盘路径不变。
        任一 /info 失败时：能读到的一侧仍计入；两侧都失败或结果为 0 且有失败则回退 fallback，
        避免「现货其实有 USDC、却因 perps=0 / 429 静默显示 $0」。
        """
        if self.cfg.dry_run or not self.cfg.account_address:
            return fallback

        try:
            self.connect_sdk(for_trading=False)
        except Exception as exc:  # pragma: no cover
            logger.debug("connect_sdk 跳过，改走 REST /info: %s", exc)

        address = self.cfg.account_address
        errors: list[str] = []
        perps_raw, perps_err = self._safe_info(
            {"type": "clearinghouseState", "user": address},
            label="perps clearinghouseState",
        )
        spot_raw, spot_err = self._safe_info(
            {"type": "spotClearinghouseState", "user": address},
            label="spotClearinghouseState",
        )
        if perps_err:
            errors.append(perps_err)
        if spot_err:
            errors.append(spot_err)

        if perps_raw is None and spot_raw is None:
            logger.warning("读取账户权益失败，回退模拟权益: %s", "; ".join(errors))
            return fallback

        abstraction = None
        # 仅当两侧都有正数时才需要模式，用来避免 unified 下同一桶 USDC 加两次
        preview = combine_live_equity(perps_raw, spot_raw, None)
        if preview.perps_value > 0 and preview.free_spot_usdc > 0:
            abstraction, abs_err = self._safe_info(
                {"type": "userAbstraction", "user": address},
                label="userAbstraction",
            )
            if abs_err:
                logger.info("未读到账户模式，按标准账户相加 perps+现货: %s", abs_err)

        breakdown = combine_live_equity(perps_raw, spot_raw, abstraction)
        if breakdown.equity <= 0 and errors:
            logger.warning(
                "权益计算结果为 0 且部分接口失败，回退模拟权益: %s",
                "; ".join(errors),
            )
            return fallback

        logger.info(
            "实盘权益 $%.2f [%s] 计入=%s perps=$%.2f 可用现货USDC=$%.2f abstraction=%s",
            breakdown.equity,
            breakdown.formula,
            ",".join(breakdown.included) or "none",
            breakdown.perps_value,
            breakdown.free_spot_usdc,
            breakdown.abstraction or "unknown",
        )
        logger.debug(
            "权益明细 formula=%s perps=%s spot=%s abstraction=%s errors=%s",
            breakdown.formula,
            breakdown.perps_value,
            breakdown.free_spot_usdc,
            breakdown.abstraction,
            errors or None,
        )
        return breakdown.equity

    def round_size(self, symbol: str, size: float, sz_decimals: int | None = None) -> float:
        dec = sz_decimals if sz_decimals is not None else 4
        factor = 10**dec
        return math_floor(size, factor)

    def fetch_open_orders(self) -> list[dict[str, Any]]:
        """当前账户挂单（含 trigger 止损）。无地址时返回空列表。"""
        address = self.cfg.account_address
        if not address:
            return []
        if self._info is not None and hasattr(self._info, "frontend_open_orders"):
            try:
                raw = self._info.frontend_open_orders(address)
                if isinstance(raw, list):
                    return raw
            except Exception as exc:
                logger.debug("SDK frontend_open_orders 失败，改 REST: %s", exc)
        raw = self._post_info({"type": "frontendOpenOrders", "user": address})
        return list(raw or [])

    def _limit_tif(self, kind: OrderKind) -> str:
        return "Alo" if kind is OrderKind.MAKER_LIMIT else "Gtc"

    def _submit_entry(self, intent: OrderIntent, *, is_buy: bool, size: float, tif: str | None = None) -> Any:
        if intent.kind is OrderKind.MARKET and tif is None:
            return self._exchange.market_open(intent.symbol, is_buy, size)
        use_tif = tif or self._limit_tif(intent.kind)
        return self._exchange.order(
            intent.symbol,
            is_buy,
            size,
            float(f"{intent.price:.5g}"),
            {"limit": {"tif": use_tif}},
            reduce_only=False,
        )

    def _cancel_oids(self, symbol: str, oids: list[int]) -> list[int]:
        cancelled: list[int] = []
        if self._exchange is None:
            return cancelled
        for oid in _unique_oids(oids):
            try:
                self._exchange.cancel(symbol, int(oid))
                cancelled.append(int(oid))
            except Exception as exc:
                logger.warning("取消 %s 订单 %s 失败: %s", symbol, oid, exc)
        return cancelled

    def _cancel_resting_entry(self, symbol: str, result: Any) -> list[int]:
        oids = extract_resting_oids(result)
        if not oids:
            return []
        logger.warning("%s 入场未成交但仍 resting，撤销以免无保护挂单: oids=%s", symbol, oids)
        return self._cancel_oids(symbol, oids)

    def reduce_only_stop_oids(self, symbol: str, orders: list[dict[str, Any]] | None = None) -> list[int]:
        rows = orders if orders is not None else self.fetch_open_orders()
        oids: list[int] = []
        for item in rows:
            if not isinstance(item, dict) or not is_reduce_only_stop_order(item, symbol):
                continue
            raw_oid = item.get("oid")
            if raw_oid is None:
                continue
            try:
                oids.append(int(raw_oid))
            except (TypeError, ValueError):
                continue
        return _unique_oids(oids)

    def cancel_reduce_only_stops(self, symbol: str, extra_oids: list[int] | None = None) -> list[int]:
        """撤掉该币已有 reduce-only 止损，避免重复堆积。查询失败时仍尝试 extras 里的 oid。"""
        oids = list(extra_oids or [])
        try:
            oids.extend(self.reduce_only_stop_oids(symbol))
        except Exception as exc:
            logger.warning("查询 %s 挂单失败，仅按已知 oid 撤止损: %s", symbol, exc)
        if not oids:
            return []
        logger.info("%s 替换保护止损前先撤已有 reduce-only 止损: %s", symbol, _unique_oids(oids))
        return self._cancel_oids(symbol, oids)

    def _place_protective_stop(
        self,
        intent: OrderIntent,
        fill: OrderFill,
        *,
        is_buy: bool,
        sz_decimals: int,
    ) -> dict[str, Any]:
        sl_sz = self.round_size(intent.symbol, protective_stop_size(intent, fill.size), sz_decimals)
        if sl_sz <= 0:
            return {"status": "skipped", "message": "stop size is 0"}
        extra: list[int] = []
        old_oid = intent.extras.get("sl_oid")
        if old_oid is not None:
            try:
                extra.append(int(old_oid))
            except (TypeError, ValueError):
                logger.warning("忽略非法 sl_oid=%s", old_oid)
        self.cancel_reduce_only_stops(intent.symbol, extra_oids=extra)
        sl_is_buy = not is_buy
        stop_px = float(f"{intent.stop_price:.5g}")
        sl_type = {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}}
        sl = self._exchange.order(intent.symbol, sl_is_buy, sl_sz, stop_px, sl_type, reduce_only=True)
        return {"result": sl, "size": sl_sz, "oid": extract_oid(sl)}

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

        if intent.reduce_only:
            if intent.kind is OrderKind.MARKET:
                return self._exchange.market_close(intent.symbol, sz=size)
            tif = self._limit_tif(intent.kind)
            return self._exchange.order(
                intent.symbol,
                is_buy,
                size,
                float(f"{intent.price:.5g}"),
                {"limit": {"tif": tif}},
                reduce_only=True,
            )

        result = self._submit_entry(intent, is_buy=is_buy, size=size)
        fill = extract_order_fill(result)
        retried_gtc = False
        # 补仓 post-only 被拒：同价再试一次 GTC（会立刻成交的 Alo 本就会吃单）。
        # 不改 chase_symbols / 不用市价追 ETH：BTC 大实体补仓信号层已是 MARKET。
        if (
            fill is None
            and intent.action is IntentAction.ADD
            and intent.kind is OrderKind.MAKER_LIMIT
            and is_post_only_reject(result)
        ):
            logger.warning(
                "%s post-only ADD 被拒（%s），同价再试一次 GTC，而不是市价追价",
                intent.symbol,
                "; ".join(collect_error_messages(result)) or result,
            )
            retried_gtc = True
            retry = self._submit_entry(intent, is_buy=is_buy, size=size, tif="Gtc")
            result = {
                "status": "ok",
                "order": retry,
                "post_only_reject": result,
                "post_only_retry": "gtc",
            }
            fill = extract_order_fill(retry)

        if fill is None or fill.size <= 0:
            # 市价本意却只 resting，或 GTC 补试未成交：撤掉本轮入场挂单，绝不挂新止损。
            if intent.kind is OrderKind.MARKET or retried_gtc:
                self._cancel_resting_entry(intent.symbol, result)
            logger.warning("入场/加仓未确认成交，不挂本轮保护止损: %s", result)
            return result

        if not intent.stop_price:
            return {"status": "ok", "order": result, "stop": None}

        stop_payload = self._place_protective_stop(intent, fill, is_buy=is_buy, sz_decimals=sz_decimals)
        return {
            "status": "ok",
            "order": result,
            "stop": stop_payload.get("result"),
            "stop_size": stop_payload.get("size"),
            "stop_oid": stop_payload.get("oid"),
        }


def math_floor(size: float, factor: int) -> float:
    return int(size * factor) / factor
