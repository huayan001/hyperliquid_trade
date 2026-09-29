"""长周期数据（不访问 Hyperliquid API）：
- BTC/ETH/SOL：Binance USDT-M 官方公开数据集 data.binance.vision（月度 + 当月日度 1h K 线、月度 fundingRate）
  （fapi.binance.com 在本机所在地区返回 451，故用公开数据集；数据同源）
- HYPE：OKX HYPE-USDT-SWAP 1h（OKX 永续 2025-02-21 上线，是本机可访问场所里 1h 历史最长的）
- 资金费：Binance 8h（或 4h）结算费率按 funding_interval_hours 均摊为每小时费率，写到结算前 N 个小时的 1h K 线收盘时刻；
  当月（月度文件未发布）与 HYPE 2025-05 之前用 OKX funding-rate-history（仅近 3 个月）补，再缺用 BTC 同时刻费率代理
- 4h/1d 由 1h 按 UTC 对齐聚合（与 Hyperliquid K 线边界一致）
- 请求之间 ≥1.2s；已下载的原始文件缓存在 data_long/raw/，重复运行不会重新下载
用法：python fetch_long.py
"""
import csv, io, json, sys, time, zipfile
from datetime import date, datetime, timezone
from pathlib import Path
import requests

OUT = Path(__file__).parent / "data_long"
RAW = OUT / "raw"
RAW.mkdir(parents=True, exist_ok=True)
H = 3_600_000
START = datetime(2023, 7, 1, tzinfo=timezone.utc)  # 预热日线 EMA50/ADX，回测从 2023-10-01 起
BV = "https://data.binance.vision/data/futures/um"
GAP = 1.2
_last = [0.0]


def get(url: str, params=None) -> requests.Response | None:
    for i in range(5):
        wait = GAP - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 404:
            return None
        if r.status_code == 200:
            return r
        print("status", r.status_code, url, "retry", i, file=sys.stderr, flush=True)
        time.sleep(5 * (i + 1))
    raise RuntimeError(url)


def cached_zip(url: str) -> bytes | None:
    f = RAW / url.rsplit("/", 1)[1]
    if f.exists():
        return f.read_bytes()
    r = get(url)
    if r is None:
        return None
    f.write_bytes(r.content)
    return r.content


def unzip_csv(b: bytes) -> list[list[str]]:
    z = zipfile.ZipFile(io.BytesIO(b))
    rows = list(csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))))
    return [r for r in rows if r and r[0][:1].isdigit()]  # 去掉表头


def months(a: date, b: date):
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def binance_1h(sym: str) -> dict[int, dict]:
    pair = f"{sym}USDT"
    today = datetime.now(timezone.utc).date()
    bars: dict[int, dict] = {}
    for y, m in months(START.date(), today):
        b = cached_zip(f"{BV}/monthly/klines/{pair}/1h/{pair}-1h-{y}-{m:02d}.zip")
        if b is None:  # 当月：月度文件未发布，逐日
            for d in range(1, 32):
                try:
                    day = date(y, m, d)
                except ValueError:
                    break
                if day >= today:
                    break
                bd = cached_zip(f"{BV}/daily/klines/{pair}/1h/{pair}-1h-{day}.zip")
                if bd is not None:
                    for r in unzip_csv(bd):
                        bars[int(r[0])] = dict(t=int(r[0]), o=r[1], h=r[2], l=r[3], c=r[4], v=r[5])
            continue
        for r in unzip_csv(b):
            bars[int(r[0])] = dict(t=int(r[0]), o=r[1], h=r[2], l=r[3], c=r[4], v=r[5])
    return bars


def binance_funding(sym: str, first: date) -> list[tuple[int, float, int]]:
    pair = f"{sym}USDT"
    out = []
    for y, m in months(first, datetime.now(timezone.utc).date()):
        b = cached_zip(f"{BV}/monthly/fundingRate/{pair}/{pair}-fundingRate-{y}-{m:02d}.zip")
        if b is None:
            continue
        for r in unzip_csv(b):
            out.append((int(r[0]) // H * H, float(r[2]), int(r[1] or 8)))
    return out


def okx_funding(inst: str) -> list[tuple[int, float, int]]:
    f = RAW / f"okx_funding_{inst}.json"
    if f.exists():
        rows = json.loads(f.read_text())
    else:
        rows, after = [], None
        while True:
            p = {"instId": inst, "limit": 100}
            if after:
                p["after"] = after
            d = get("https://www.okx.com/api/v5/public/funding-rate-history", p).json()["data"]
            if not d:
                break
            rows += d
            after = d[-1]["fundingTime"]
            if len(d) < 100:
                break
        f.write_text(json.dumps(rows))
    ts = sorted({int(r["fundingTime"]) // H * H: float(r["realizedRate"] or r["fundingRate"]) for r in rows}.items())
    out = []
    for k, (t, v) in enumerate(ts):
        iv = (t - ts[k - 1][0]) // H if k else 8
        out.append((t, v, int(iv) if 1 <= iv <= 8 else 8))
    return out


def okx_1h(inst: str) -> dict[int, dict]:
    f = RAW / f"okx_1h_{inst}.json"
    if f.exists():
        rows = json.loads(f.read_text())
    else:
        rows, after = [], None
        while True:
            p = {"instId": inst, "bar": "1H", "limit": 100}
            if after:
                p["after"] = after
            d = get("https://www.okx.com/api/v5/market/history-candles", p).json()["data"]
            if not d:
                break
            rows += d
            after = d[-1][0]
            if len(rows) % 2000 == 0:
                print(inst, len(rows), flush=True)
        f.write_text(json.dumps(rows))
    return {int(r[0]): dict(t=int(r[0]), o=r[1], h=r[2], l=r[3], c=r[4], v=r[6]) for r in rows if r[8] == "1"}


def hourly_funding(events: list[tuple[int, float, int]]) -> dict[int, float]:
    """结算时刻 F、费率 r、周期 n 小时 → 每小时 r/n，键 = 该小时 K 线收盘时刻（F-(n-1)H … F）"""
    out = {}
    for t, r, n in events:
        for k in range(n):
            out[t - k * H] = r / n
    return out


def agg(bars: list[dict], ms: int) -> list[dict]:
    out: dict[int, dict] = {}
    for b in bars:
        k = b["t"] // ms * ms
        o = out.get(k)
        if o is None:
            out[k] = dict(t=k, T=k + ms - 1, o=b["o"], h=float(b["h"]), l=float(b["l"]), c=b["c"], v=float(b["v"]), n=1)
        else:
            o["h"] = max(o["h"], float(b["h"])); o["l"] = min(o["l"], float(b["l"])); o["c"] = b["c"]
            o["v"] += float(b["v"]); o["n"] += 1
    full = ms // H
    res = [o for k, o in sorted(out.items())]
    # 丢弃不完整的最后一根（其余缺小时的 K 保留，并在日志里报告）
    if res and res[-1]["n"] < full:
        res = res[:-1]
    return res


def write(sym: str, bars: dict[int, dict], fund: dict[int, float], src: str) -> None:
    h1 = [dict(t=t, T=t + H - 1, o=b["o"], h=b["h"], l=b["l"], c=b["c"], v=b["v"]) for t, b in sorted(bars.items())]
    ts = [b["t"] for b in h1]
    gaps = sum(1 for a, b in zip(ts, ts[1:]) if b - a != H)
    (OUT / f"{sym}_1h.json").write_text(json.dumps(h1))
    (OUT / f"{sym}_4h.json").write_text(json.dumps(agg(h1, 4 * H)))
    (OUT / f"{sym}_1d.json").write_text(json.dumps(agg(h1, 24 * H)))
    rows = [{"time": t, "fundingRate": v} for t, v in sorted(fund.items()) if ts[0] <= t <= ts[-1] + H]
    (OUT / f"{sym}_funding.json").write_text(json.dumps(rows))
    f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cover = sum(1 for t in ts if t + H in fund) / len(ts)
    print(f"{sym}: src={src} 1h={len(h1)} {f(ts[0])} → {f(ts[-1])} gaps={gaps} funding_hours={len(rows)} coverage={cover:.1%}", flush=True)


def main() -> None:
    fund_btc: dict[int, float] = {}
    end = 0
    for sym in ["BTC", "ETH", "SOL", "HYPE"]:
        if sym == "HYPE":
            bars, src = okx_1h("HYPE-USDT-SWAP"), "OKX HYPE-USDT-SWAP"
            bars = {t: b for t, b in bars.items() if t <= end}  # 与 Binance 数据同一截止时刻
        else:
            bars, src = binance_1h(sym), "Binance USDT-M (data.binance.vision)"
            end = max(bars) if sym == "BTC" else min(end, max(bars))
        first = datetime.fromtimestamp(min(bars) / 1000, timezone.utc).date()
        fund = hourly_funding(okx_funding(f"{sym}-USDT-SWAP"))          # 近 3 个月（补当月）
        fund.update(hourly_funding(binance_funding(sym, first)))        # Binance 优先
        if sym == "BTC":
            fund_btc = dict(fund)
        proxied = 0
        for t in bars:  # 仍缺的小时：用 BTC 同时刻费率代理
            if t + H not in fund and t + H in fund_btc:
                fund[t + H] = fund_btc[t + H]; proxied += 1
        write(sym, bars, fund, src)
        print(f"   {sym} funding proxied from BTC: {proxied} hours", flush=True)


if __name__ == "__main__":
    main()
