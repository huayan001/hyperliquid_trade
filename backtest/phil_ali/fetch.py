#!/usr/bin/env python3
"""给菲阿里回测准备 BTC/ETH/SOL 的 1h 与 1d K 线。

只拉这三个加密货币永续对应的 Binance USDT-M 公开 K 线。
不请求黄金、PAXG、XAU。已有 `backtest/data_long/` 时直接转成本目录的 `data/`。

输出：`backtest/phil_ali/data/{SYM}_1h.json` 与 `{SYM}_1d.json`，元素为 t/o/h/l/c。
该目录已 gitignore，不要把 K 线提交进仓库。
"""

from __future__ import annotations

import csv
import io
import json
import sys
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import requests

SYMBOLS = ("BTC", "ETH", "SOL")
BANNED = ("XAU", "GOLD", "PAXG")
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data"
LONG = ROOT.parent / "data_long"
H_MS = 3_600_000
DAY_MS = 86_400_000
START = datetime(2023, 7, 1, tzinfo=timezone.utc)
BV = "https://data.binance.vision/data/futures/um"
GAP = 1.2
_last = [0.0]


def _reject(symbols: tuple[str, ...]) -> None:
    for sym in symbols:
        upper = sym.upper()
        if upper not in SYMBOLS or any(piece in upper for piece in BANNED):
            raise SystemExit(f"菲阿里数据只准备 BTC/ETH/SOL，拒绝 {sym}")


def _bar(raw: dict) -> dict:
    return {
        "t": int(raw["t"]),
        "o": float(raw["o"]),
        "h": float(raw["h"]),
        "l": float(raw["l"]),
        "c": float(raw["c"]),
    }


def _aggregate_days(hourly: list[dict]) -> list[dict]:
    buckets: dict[int, dict] = {}
    for bar in hourly:
        key = int(bar["t"]) // DAY_MS * DAY_MS
        slot = buckets.get(key)
        if slot is None:
            buckets[key] = {
                "t": key,
                "o": float(bar["o"]),
                "h": float(bar["h"]),
                "l": float(bar["l"]),
                "c": float(bar["c"]),
                "n": 1,
            }
            continue
        slot["h"] = max(slot["h"], float(bar["h"]))
        slot["l"] = min(slot["l"], float(bar["l"]))
        slot["c"] = float(bar["c"])
        slot["n"] += 1
    days = []
    for key in sorted(buckets):
        slot = buckets[key]
        if slot["n"] < 24:
            continue
        days.append({"t": slot["t"], "o": slot["o"], "h": slot["h"], "l": slot["l"], "c": slot["c"]})
    return days


def _copy_local(sym: str) -> bool:
    hourly_path = LONG / f"{sym}_1h.json"
    if not hourly_path.exists():
        return False
    hourly = [_bar(row) for row in json.loads(hourly_path.read_text())]
    hourly.sort(key=lambda item: item["t"])
    daily_path = LONG / f"{sym}_1d.json"
    if daily_path.exists():
        daily = [_bar(row) for row in json.loads(daily_path.read_text())]
        daily.sort(key=lambda item: item["t"])
    else:
        daily = _aggregate_days(hourly)
    (OUT / f"{sym}_1h.json").write_text(json.dumps(hourly))
    (OUT / f"{sym}_1d.json").write_text(json.dumps(daily))
    print(f"{sym}: 使用 data_long 1h={len(hourly)} 1d={len(daily)}", flush=True)
    return True


def _get(url: str) -> bytes | None:
    for attempt in range(5):
        wait = GAP - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        response = requests.get(url, timeout=60)
        if response.status_code == 404:
            return None
        if response.status_code == 200:
            return response.content
        print("status", response.status_code, url, "retry", attempt, file=sys.stderr, flush=True)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(url)


def _months(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _unzip_rows(payload: bytes) -> list[list[str]]:
    archive = zipfile.ZipFile(io.BytesIO(payload))
    rows = list(csv.reader(io.TextIOWrapper(archive.open(archive.namelist()[0]))))
    return [row for row in rows if row and row[0][:1].isdigit()]


def _download(sym: str) -> None:
    pair = f"{sym}USDT"
    today = datetime.now(timezone.utc).date()
    bars: dict[int, dict] = {}
    raw_dir = OUT / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for year, month in _months(START.date(), today):
        name = f"{pair}-1h-{year}-{month:02d}.zip"
        cached = raw_dir / name
        if cached.exists():
            payload = cached.read_bytes()
        else:
            payload = _get(f"{BV}/monthly/klines/{pair}/1h/{name}")
            if payload is None:
                continue
            cached.write_bytes(payload)
        for row in _unzip_rows(payload):
            ts = int(row[0])
            bars[ts] = {"t": ts, "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4])}
    if not bars:
        raise SystemExit(f"{sym}: 没有下载到 1h K 线")
    hourly = [bars[key] for key in sorted(bars)]
    daily = _aggregate_days(hourly)
    (OUT / f"{sym}_1h.json").write_text(json.dumps(hourly))
    (OUT / f"{sym}_1d.json").write_text(json.dumps(daily))
    print(f"{sym}: Binance USDT-M 1h={len(hourly)} 1d={len(daily)}", flush=True)


def main() -> None:
    _reject(SYMBOLS)
    OUT.mkdir(parents=True, exist_ok=True)
    for sym in SYMBOLS:
        if _copy_local(sym):
            continue
        _download(sym)
    print("done", OUT)


if __name__ == "__main__":
    main()
