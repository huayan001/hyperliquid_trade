"""BTC 日线过滤，两条独立的闸，共用一次已收盘日线读取。

1. ADX 强度（`btc_regime_mode`）：低于阈值时，shadow 只记日志，enforce 才跳过新的趋势开仓
   （多空都看）。加仓、止损、离场、均值回归不经过这条。
2. 方向（`btc_bearish_long_mode`）：日线明确空头时，只拦新的做多。明确空头与环境路由同一套
   （`daily_clear_trend_side`：EMA 快线 < 慢线且 ADX ≥ clear_trend_adx），不含 1h 冲突覆盖。
   off 不看方向；shadow 只记日志；enforce 硬拦截。读数失败两条都放行。

- 两条都是 off 时不拉 K 线。
- 拉 K 线或算 ADX 失败：任何模式都放行，并在一段时间后再试，避免每个扫描周期打一次 /info。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_bot.config import TrendConfig
from hl_bot.indicators import adx, ema, last_value
from hl_bot.models import Side
from hl_bot.regime import daily_clear_trend_side

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


@dataclass(frozen=True, slots=True)
class _DailyRead:
    adx: float
    ema_fast: float | None
    ema_slow: float | None


class BtcRegimeGate:
    def __init__(
        self,
        trend: TrendConfig,
        client: Any,
        log_path: Path,
        *,
        bearish_adx_min: float = 22.5,
    ) -> None:
        self.mode = str(trend.btc_regime_mode or "off").strip().lower()
        self.threshold = float(trend.btc_regime_adx_min)
        self.period = int(trend.btc_regime_adx_period)
        self.bearish_mode = str(getattr(trend, "btc_bearish_long_mode", "off") or "off").strip().lower()
        self.bearish_adx_min = float(bearish_adx_min)
        self.ema_fast_period = int(trend.ema_fast)
        self.ema_slow_period = int(trend.ema_slow)
        self.client = client
        self.log_path = log_path
        self._cached_day: str | None = None
        self._cached_adx: float | None = None
        self._cached_ema_fast: float | None = None
        self._cached_ema_slow: float | None = None
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

    def blocks_bearish_long(self, symbol: str, side: str, now_ms: int, notes: list[str]) -> bool:
        """True 只在 enforce 且 BTC 日线明确空头、这笔是新多时出现。其余情况一律 False。"""
        if self.bearish_mode == "off" or str(side).lower() != Side.LONG.value:
            return False
        try:
            return self._blocks_bearish_long(symbol, side, now_ms, notes)
        except Exception as exc:
            logger.warning("BTC 日线偏空判断异常，fail-open 不拦截 %s: %s", symbol, exc)
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

    def _blocks_bearish_long(self, symbol: str, side: str, now_ms: int, notes: list[str]) -> bool:
        snap = self._snapshot(now_ms)
        if snap is None or snap.ema_fast is None or snap.ema_slow is None:
            return False
        clear = daily_clear_trend_side(
            snap.ema_fast,
            snap.ema_slow,
            snap.adx,
            clear_trend_adx=self.bearish_adx_min,
        )
        if clear is not Side.SHORT:
            return False
        enforce = self.bearish_mode == "enforce"
        self._record_bearish(
            symbol=symbol,
            side=side,
            now_ms=now_ms,
            snap=snap,
            notes=notes,
            enforce=enforce,
        )
        return enforce

    def _reading(self, now_ms: int) -> float | None:
        snap = self._snapshot(now_ms)
        if snap is None:
            return None
        return snap.adx

    def _snapshot(self, now_ms: int) -> _DailyRead | None:
        """当天已有读数则直接返回。失败处于退避窗口时不再请求，返回 None（放行）。"""
        if self.mode == "off" and self.bearish_mode == "off":
            return None
        day = _utc_day(now_ms)
        if self._cached_day == day and self._cached_adx is not None:
            return _DailyRead(self._cached_adx, self._cached_ema_fast, self._cached_ema_slow)
        if now_ms < self._retry_not_before_ms:
            return None
        try:
            bars = max(BTC_DAILY_BARS, self.period * 3, self.ema_slow_period + 10)
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
            ema_fast, ema_slow = self._emas(closed)
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
        self._cached_ema_fast = ema_fast
        self._cached_ema_slow = ema_slow
        self._retry_not_before_ms = 0
        if self._logged_day != day:
            ema_part = ""
            if ema_fast is not None and ema_slow is not None:
                ema_part = (
                    f" EMA{self.ema_fast_period}={ema_fast:.2f}"
                    f" EMA{self.ema_slow_period}={ema_slow:.2f}"
                )
            logger.info(
                "BTC 日线 ADX(%d)=%.2f 阈值=%.2f%s UTC日=%s mode=%s bearish_long=%s",
                self.period,
                value,
                self.threshold,
                ema_part,
                day,
                self.mode,
                self.bearish_mode,
            )
            self._logged_day = day
        return _DailyRead(value, ema_fast, ema_slow)

    def _emas(self, closed: list[Any]) -> tuple[float | None, float | None]:
        """EMA 失败不影响 ADX 闸；方向闸拿不到均线就 fail-open。"""
        try:
            need = max(self.ema_fast_period, self.ema_slow_period) + 1
            if len(closed) < need:
                logger.warning("BTC 日线不足以计算 EMA，方向过滤 fail-open（%d < %d）", len(closed), need)
                return None, None
            closes = [float(c.close) for c in closed]
            fast = last_value(ema(closes, self.ema_fast_period))
            slow = last_value(ema(closes, self.ema_slow_period))
            if fast is None or slow is None:
                logger.warning("BTC 日线 EMA 尚未就绪，方向过滤 fail-open")
                return None, None
            return fast, slow
        except Exception as exc:
            logger.warning("BTC 日线 EMA 计算失败，方向过滤 fail-open: %s", exc)
            return None, None

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

    def _record_bearish(
        self,
        *,
        symbol: str,
        side: str,
        now_ms: int,
        snap: _DailyRead,
        notes: list[str],
        enforce: bool,
    ) -> None:
        assert snap.ema_fast is not None and snap.ema_slow is not None
        detail = (
            f"EMA{self.ema_fast_period}={snap.ema_fast:.2f}<EMA{self.ema_slow_period}={snap.ema_slow:.2f}"
            f" 且 ADX={snap.adx:.2f}≥{self.bearish_adx_min:.2f}"
        )
        payload: dict[str, Any] = {
            "utc": _utc_iso(now_ms),
            "symbol": symbol,
            "side": side,
            "gate": "btc_bearish_long",
            "btc_adx": snap.adx,
            "ema_fast": snap.ema_fast,
            "ema_slow": snap.ema_slow,
            "clear_trend_adx": self.bearish_adx_min,
        }
        if enforce:
            payload["blocked"] = True
            msg = f"BTC日线偏空，禁止新开多：{symbol} {side} {detail}"
        else:
            payload["would_block"] = True
            msg = f"BTC日线偏空 shadow：{symbol} {side} {detail}，仅记录不拦截"
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
