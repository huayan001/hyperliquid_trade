"""按 (coin, interval) 持久化已收盘 K 线，扫描时只补最近窗口。

合并结果必须与一次全量 candleSnapshot 裁切后的序列一致：同一 ts 以新数据为准，
时间不连续或缓存损坏时返回 None，调用方回退全量。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from hl_bot.models import Candle

logger = logging.getLogger(__name__)

# 多拉两根，刷新最后一根已收盘 K，并覆盖正在形成、下一轮才会收盘的那根
OVERLAP_BARS = 2


def candle_to_raw(candle: Candle) -> dict[str, float | int]:
    return {
        "t": int(candle.ts),
        "T": int(candle.end_ts),
        "o": float(candle.open),
        "h": float(candle.high),
        "l": float(candle.low),
        "c": float(candle.close),
        "v": float(candle.volume),
    }


def candle_from_raw(raw: dict) -> Candle:
    return Candle(
        ts=int(raw["t"]),
        end_ts=int(raw["T"]),
        open=float(raw["o"]),
        high=float(raw["h"]),
        low=float(raw["l"]),
        close=float(raw["c"]),
        volume=float(raw.get("v") or 0.0),
    )


def merge_closed(
    cached: list[Candle],
    fresh: list[Candle],
    *,
    interval_ms: int,
    now_ms: int,
) -> list[Candle] | None:
    """合并后的已收盘 K 线。无法与全量结果对齐（空响应、缺口、对不上上一根）时返回 None。"""
    if interval_ms <= 0 or not fresh:
        return None
    by_ts: dict[int, Candle] = {c.ts: c for c in cached}
    for candle in fresh:
        by_ts[candle.ts] = candle
    ordered = [by_ts[ts] for ts in sorted(by_ts)]
    closed = [c for c in ordered if c.end_ts < now_ms]
    if not closed:
        return None
    for prev, cur in zip(closed, closed[1:]):
        if cur.ts - prev.ts != interval_ms:
            return None
    if cached:
        closed_ts = {c.ts for c in closed}
        if cached[-1].ts not in closed_ts:
            return None
        for candle in cached:
            if candle.end_ts < now_ms and candle.ts not in by_ts:
                return None
    return closed


class CandleCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, coin: str, interval: str) -> Path:
        safe_coin = "".join(ch for ch in coin.upper() if ch.isalnum() or ch in {"_", "-"})
        safe_interval = "".join(ch for ch in interval if ch.isalnum())
        return self.directory / f"{safe_coin}_{safe_interval}.json"

    def load(self, coin: str, interval: str, interval_ms: int) -> tuple[list[Candle], bool] | None:
        """返回 (已收盘 K 线, exhaustive)。缺失或损坏时返回 None。"""
        path = self.path_for(coin, interval)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            logger.info("K 线缓存无法读取，视为缺失 %s", path)
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("coin") or "").upper() != coin.upper():
            return None
        if str(payload.get("interval") or "") != interval:
            return None
        if int(payload.get("interval_ms") or 0) != int(interval_ms):
            return None
        rows = payload.get("candles")
        if not isinstance(rows, list) or not rows:
            return None
        try:
            candles = [candle_from_raw(row) for row in rows if isinstance(row, dict)]
        except (KeyError, TypeError, ValueError):
            return None
        if len(candles) != len(rows):
            return None
        for prev, cur in zip(candles, candles[1:]):
            if cur.ts - prev.ts != interval_ms or cur.ts == prev.ts:
                return None
        exhaustive = bool(payload.get("exhaustive"))
        return candles, exhaustive

    def save(self, coin: str, interval: str, interval_ms: int, candles: list[Candle], *, exhaustive: bool) -> None:
        if not candles:
            return
        path = self.path_for(coin, interval)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "coin": coin.upper(),
            "interval": interval,
            "interval_ms": int(interval_ms),
            "exhaustive": bool(exhaustive),
            "candles": [candle_to_raw(c) for c in candles],
        }
        blob = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, path)
