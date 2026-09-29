"""Fetch ~6+ months of Hyperliquid candles & funding, paced >=2s between calls."""
import json, time, requests, sys
from pathlib import Path
U = "https://api.hyperliquid.xyz/info"
OUT = Path(__file__).parent / "data"
NOW = int(time.time() * 1000)
DAY = 86_400_000
START = NOW - 250 * DAY   # 6 months test + warmup for daily EMA50
def q(payload):
    for i in range(6):
        time.sleep(2.0)
        r = requests.post(U, json=payload, timeout=30)
        if r.status_code == 200:
            return r.json()
        print("status", r.status_code, "retry", i, file=sys.stderr, flush=True)
        time.sleep(10 * (i + 1))
    raise RuntimeError("failed")
for sym in ["BTC", "ETH", "SOL", "HYPE"]:
    for iv, ms in [("1d", DAY), ("4h", 4 * 3600_000), ("1h", 3600_000)]:
        f = OUT / f"{sym}_{iv}.json"
        if f.exists():
            continue
        rows, start = [], START
        while start < NOW:
            raw = q({"type": "candleSnapshot", "req": {"coin": sym, "interval": iv, "startTime": start, "endTime": NOW}})
            if not raw:
                break
            rows += raw
            last = raw[-1]["t"]
            if len(raw) < 4000:
                break
            start = last + ms
        d = {}
        for r in rows:
            d[r["t"]] = r
        f.write_text(json.dumps([d[k] for k in sorted(d)]))
        print(sym, iv, len(d), flush=True)
    f = OUT / f"{sym}_funding.json"
    if True:
        continue
    if not f.exists():
        rows, start = [], START
        while start < NOW:
            raw = q({"type": "fundingHistory", "coin": sym, "startTime": start, "endTime": NOW})
            if not raw:
                break
            rows += raw
            nxt = raw[-1]["time"] + 1
            if nxt <= start or len(raw) < 450:
                break
            start = nxt
        f.write_text(json.dumps(rows))
        print(sym, "funding", len(rows), flush=True)
print("done")
