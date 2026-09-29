"""BTC 日线 ADX 趋势开关。

只观察「新的趋势开仓」（含 starter）。加仓、止损、离场、均值回归不经过这里。

- off：调用方不应进入；本模块也不拉 K 线。
- shadow：低于阈值只写日志，从不拦截。
- enforce：低于阈值跳过这笔新开仓。
- 拉 K 线或算 ADX 失败：任何模式都放行，并在一段时间后再试，避免每个扫描周期打一次 /info。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_bot.config import TrendConfig
from hl_bot.indicators import adx

logger = logging.getLogger(__name__)

BTC_REGIME_SYMBOL = "BTC"
BTC_REGIME_INTERVAL = "1d"
# ADX 预热大约要几十根；100 根已收盘日线足够，同时控制 candleSnapshot 的条数。
BTC_DAILY_BARS = 100
# 失败后不要每个轮询周期都重试（info 与实盘共用 IP，容易 429）。
BTC_REGIME_RETRY_MS = 60 * 60 * 1000
SHADOW_LOG_NAME = "btc_regime_shadow.jsonl"


def shadow_log_path(state_path: str) -> Path:
    """与 state 文件同目录。默认 state_path 下就是 state/btc_regime_shadow.jsonl。"""
    return Path(state_path).expanduser().parent / SHADOW_LOG_NAME


def _utc_day(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def _utc_iso(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BtcRegimeGate:
    def __init__(self, trend: TrendConfig, client: Any, log_path: Path) -> None:
        self.mode = str(trend.btc_regime_mode or "off").strip().lower()
        self.threshold = float(trend.btc_regime_adx_min)
        self.period = int(trend.btc_regime_adx_period)
        self.client = client
        self.log_path = log_path
        self._cached_day: str | None = None
        self._cached_adx: float | None = None
        self._retry_not_before_ms = 0
        self._logged_day: str | None = None

    def blocks_new_entry(self, symbol: str, side: str, now_ms: int, notes: list[str]) -> bool:
        """True 只在 enforce 且 ADX 低于阈值时出现。其余情况（含失败）一律 False。"""
        if self.mode == "off":
            return False
        try:
            return self._blocks_new_entry(symbol, side, now_ms, notes)
        except Exception as exc:
            logger.warning("BTC regime 判断异常，fail-open 不拦截 %s: %s", symbol, exc)
            return False

    def _blocks_new_entry(self, symbol: str, side: str, now_ms: int, notes: list[str]) -> bool:
        reading = self._reading(now_ms)
        if reading is None:
            return False
        would_block = reading < self.threshold
        if self.mode == "shadow":
            self._record(
                symbol=symbol,
                side=side,
                now_ms=now_ms,
                adx_value=reading,
                would_block=would_block,
                notes=notes,
                enforce=False,
            )
            return False
        if self.mode == "enforce" and would_block:
            self._record(
                symbol=symbol,
                side=side,
                now_ms=now_ms,
                adx_value=reading,
                would_block=True,
                notes=notes,
                enforce=True,
            )
            return True
        return False

    def _reading(self, now_ms: int) -> float | None:
        """当天已有读数则直接返回。失败处于退避窗口时不再请求，返回 None（放行）。"""
        if self.mode == "off":
            return None
        day = _utc_day(now_ms)
        if self._cached_day == day and self._cached_adx is not None:
            return self._cached_adx
        if now_ms < self._retry_not_before_ms:
            return None
        try:
            bars = max(BTC_DAILY_BARS, self.period * 3)
            # 走现有 fetch_candles：candle_cache + /info 节流，不另开请求通道。
            candles = list(self.client.fetch_candles(BTC_REGIME_SYMBOL, BTC_REGIME_INTERVAL, bars))
            closed = [c for c in candles if int(c.end_ts) < int(now_ms)]
            if len(closed) < self.period * 2:
                raise RuntimeError(f"BTC 已收盘日线不足（{len(closed)} < {self.period * 2}）")
            series = adx(closed, self.period)
            value = series[-1] if series else None
            if value is None or (isinstance(value, float) and math.isnan(value)):
                raise RuntimeError("BTC 日线 ADX 尚未就绪")
            value = float(value)
        except Exception as exc:
            self._retry_not_before_ms = now_ms + BTC_REGIME_RETRY_MS
            logger.warning(
                "BTC regime 日线 ADX 读取失败，fail-open 不拦截，%d 秒后再试: %s",
                BTC_REGIME_RETRY_MS // 1000,
                exc,
            )
            return None
        self._cached_day = day
        self._cached_adx = value
        self._retry_not_before_ms = 0
        if self._logged_day != day:
            logger.info(
                "BTC 日线 ADX(%d)=%.2f 阈值=%.2f UTC日=%s mode=%s",
                self.period,
                value,
                self.threshold,
                day,
                self.mode,
            )
            self._logged_day = day
        return value

    def _record(
        self,
        *,
        symbol: str,
        side: str,
        now_ms: int,
        adx_value: float,
        would_block: bool,
        notes: list[str],
        enforce: bool,
    ) -> None:
        payload: dict[str, Any] = {
            "utc": _utc_iso(now_ms),
            "symbol": symbol,
            "side": side,
            "btc_adx": adx_value,
            "threshold": self.threshold,
        }
        if enforce:
            payload["blocked"] = True
            msg = (
                f"BTC regime enforce：{symbol} {side} 跳过新趋势开仓 "
                f"BTC日线ADX={adx_value:.2f} < {self.threshold:.2f}"
            )
            logger.info(msg)
            notes.append(msg)
        else:
            payload["would_block"] = bool(would_block)
            if would_block:
                msg = (
                    f"BTC regime shadow：{symbol} {side} 新趋势开仓 "
                    f"BTC日线ADX={adx_value:.2f} < {self.threshold:.2f}，仅记录不拦截"
                )
                logger.info(msg)
                notes.append(msg)
        self._append(payload)

    def _append(self, payload: dict[str, Any]) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            logger.warning("写入 %s 失败，不拦截开仓: %s", self.log_path, exc)
