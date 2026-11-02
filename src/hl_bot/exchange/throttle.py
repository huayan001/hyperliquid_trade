"""Hyperliquid REST 权重节流。

官方限额（IP 聚合，每分钟 1200 weight）：
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits

- weight 2: l2Book, allMids, clearinghouseState, orderStatus, spotClearinghouseState, exchangeStatus
- weight 60: userRole
- 其余已文档化的 info 请求: 20
- candleSnapshot: 再按返回条数每 60 根 +1
- fundingHistory 等: 再按返回条数每 20 条 +1
- exchange 动作: 1 + floor(batch_len / 40)

info 桶维持在限额的一半（600/min），并在请求之间留一点间隔，避免把 1200 打满。
下单/撤单走独立的小桶，只做很短的间隔，不跟 info 桶互相阻塞。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

INFO_BUDGET_PER_MIN = 600.0
INFO_MIN_GAP_SEC = 0.20
EXCHANGE_BUDGET_PER_MIN = 120.0
EXCHANGE_MIN_GAP_SEC = 0.05

_WEIGHT_2 = frozenset(
    {
        "l2Book",
        "allMids",
        "clearinghouseState",
        "orderStatus",
        "spotClearinghouseState",
        "exchangeStatus",
    }
)
_EXTRA_PER_20 = frozenset(
    {
        "recentTrades",
        "historicalOrders",
        "userFills",
        "userFillsByTime",
        "fundingHistory",
        "userFunding",
        "nonUserFundingUpdates",
        "twapHistory",
        "userTwapSliceFills",
        "userTwapSliceFillsByTime",
        "delegatorHistory",
        "delegatorRewards",
        "validatorStats",
    }
)


# 与 client.INTERVAL_MS 保持一致，放在这里是为了避免 throttle ↔ client 循环导入
_INTERVAL_MS = {
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


def _estimate_items(payload: dict[str, Any]) -> int:
    kind = str(payload.get("type") or "")
    if kind == "candleSnapshot":
        req = payload.get("req") if isinstance(payload.get("req"), dict) else {}
        start = req.get("startTime")
        end = req.get("endTime")
        interval = str(req.get("interval") or "")
        step = _INTERVAL_MS.get(interval, 0)
        if step and isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            return int((float(end) - float(start)) // step) + 1
        return 0
    if kind in _EXTRA_PER_20:
        start = payload.get("startTime")
        end = payload.get("endTime")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            # funding 等按小时一条估算；多估会在拿到响应后退回
            return int((end - start) // 3_600_000) + 1
    return 0


def info_weight(payload: dict[str, Any] | None, response: Any | None = None) -> int:
    """近似 /info 权重。有响应时用真实条数，否则按请求窗口估算。"""
    if not isinstance(payload, dict):
        return 20
    kind = str(payload.get("type") or "")
    if kind in _WEIGHT_2:
        return 2
    if kind == "userRole":
        return 60
    base = 20
    if response is not None:
        n = len(response) if isinstance(response, list) else 0
    else:
        n = _estimate_items(payload)
    if kind == "candleSnapshot":
        return base + n // 60
    if kind in _EXTRA_PER_20:
        return base + n // 20
    return base


def exchange_weight(batch_len: int = 1) -> int:
    """1 + floor(batch_len / 40)。单笔下单/撤单为 1。"""
    n = max(int(batch_len), 0)
    return 1 + n // 40


def exchange_weight_from_payload(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 1
    action = payload.get("action")
    if not isinstance(action, dict):
        return 1
    for key in ("orders", "cancels", "modifies"):
        rows = action.get(key)
        if isinstance(rows, list) and rows:
            return exchange_weight(len(rows))
    return 1


class RequestThrottle:
    """info / exchange 分开的令牌桶。sleep 与 clock 可注入，便于测试。"""

    def __init__(
        self,
        *,
        info_per_minute: float = INFO_BUDGET_PER_MIN,
        info_min_gap: float = INFO_MIN_GAP_SEC,
        exchange_per_minute: float = EXCHANGE_BUDGET_PER_MIN,
        exchange_min_gap: float = EXCHANGE_MIN_GAP_SEC,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.info_capacity = float(info_per_minute)
        self.info_rate = float(info_per_minute) / 60.0
        self.info_min_gap = float(info_min_gap)
        self.exchange_capacity = float(exchange_per_minute)
        self.exchange_rate = float(exchange_per_minute) / 60.0
        self.exchange_min_gap = float(exchange_min_gap)
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self.info_tokens = self.info_capacity
        self.exchange_tokens = self.exchange_capacity
        self._info_updated: float | None = None
        self._exchange_updated: float | None = None
        self._info_last: float | None = None
        self._exchange_last: float | None = None
        self._lock = threading.Lock()

    def acquire_info(self, weight: float) -> None:
        self._acquire(
            weight,
            capacity=self.info_capacity,
            rate=self.info_rate,
            min_gap=self.info_min_gap,
            kind="info",
        )

    def acquire_exchange(self, weight: float) -> None:
        self._acquire(
            weight,
            capacity=self.exchange_capacity,
            rate=self.exchange_rate,
            min_gap=self.exchange_min_gap,
            kind="exchange",
        )

    def adjust_info(self, delta: float) -> None:
        """响应条数和预扣不一致时修正余额。不再额外插入间隔。"""
        if delta == 0:
            return
        with self._lock:
            now = self._clock()
            self._refill("info", now)
            self.info_tokens = min(self.info_capacity, self.info_tokens - delta)

    def _acquire(self, weight: float, *, capacity: float, rate: float, min_gap: float, kind: str) -> None:
        need = max(float(weight), 0.0)
        if need > capacity:
            need = capacity
        with self._lock:
            now = self._clock()
            self._refill(kind, now)
            tokens = self.info_tokens if kind == "info" else self.exchange_tokens
            last = self._info_last if kind == "info" else self._exchange_last
            wait_tokens = 0.0 if tokens >= need or rate <= 0 else (need - tokens) / rate
            wait_gap = 0.0 if last is None else max(0.0, min_gap - (now - last))
            wait = max(wait_tokens, wait_gap)
        if wait > 0:
            self._sleep(wait)
        with self._lock:
            t0 = now
            t1 = self._clock()
            if wait > 0 and t1 < t0 + wait:
                # 测试里 sleep 被替换成空操作时，时钟不会自己往前走
                t1 = t0 + wait
            self._refill(kind, t1)
            if kind == "info":
                self.info_tokens = min(self.info_capacity, self.info_tokens) - need
                self._info_last = t1
            else:
                self.exchange_tokens = min(self.exchange_capacity, self.exchange_tokens) - need
                self._exchange_last = t1

    def _refill(self, kind: str, now: float) -> None:
        if kind == "info":
            updated = self._info_updated
            rate = self.info_rate
            capacity = self.info_capacity
        else:
            updated = self._exchange_updated
            rate = self.exchange_rate
            capacity = self.exchange_capacity
        if updated is None:
            if kind == "info":
                self._info_updated = now
            else:
                self._exchange_updated = now
            return
        elapsed = now - updated
        if elapsed <= 0:
            return
        if kind == "info":
            self.info_tokens = min(capacity, self.info_tokens + elapsed * rate)
            self._info_updated = now
        else:
            self.exchange_tokens = min(capacity, self.exchange_tokens + elapsed * rate)
            self._exchange_updated = now


class ScanBudget:
    """一轮扫描内的请求次数、权重和 429 次数。"""

    def __init__(self) -> None:
        self.requests = 0
        self.weight = 0
        self.rate_limits = 0
        self.info_requests = 0
        self.info_weight = 0
        self.exchange_requests = 0
        self.exchange_weight = 0
        self.candle_incremental = 0
        self.candle_full = 0
        self.by_type: dict[str, dict[str, int]] = {}

    def begin(self) -> None:
        self.__init__()

    def record(self, name: str, weight: int, *, kind: str = "info", rate_limited: bool = False) -> None:
        w = max(int(weight), 0)
        self.requests += 1
        self.weight += w
        if kind == "exchange":
            self.exchange_requests += 1
            self.exchange_weight += w
        else:
            self.info_requests += 1
            self.info_weight += w
        if rate_limited:
            self.rate_limits += 1
        bucket = self.by_type.setdefault(str(name or kind), {"n": 0, "w": 0})
        bucket["n"] += 1
        bucket["w"] += w

    def log(self) -> None:
        detail = " ".join(f"{name}:{row['n']}/{row['w']}" for name, row in sorted(self.by_type.items()))
        logger.info(
            "本轮 API 统计 requests=%d weight=%d 429=%d info=%d/%d exchange=%d/%d candles_incremental=%d candles_full=%d%s",
            self.requests,
            self.weight,
            self.rate_limits,
            self.info_requests,
            self.info_weight,
            self.exchange_requests,
            self.exchange_weight,
            self.candle_incremental,
            self.candle_full,
            f" | {detail}" if detail else "",
        )
