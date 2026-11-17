"""B2 特征构造（scan.py 与 b2.py 共用）。每个币每根已收盘 4h K 线（收盘时刻 T）一行，特征只用 ≤T 的数据。"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent.parent / "src"))
from hl_bot.indicators import adx, atr, bollinger, rsi  # noqa: E402
from hl_bot.models import Candle  # noqa: E402

H = 3_600_000
H4 = 4 * H
TRAIN_START, HALF, TRAIN_END, VAL_END = 1696118400000, 1718841600000, 1741564800000, 1757462400000
HORIZONS = (4, 12, 24)
QFEATS = ["dist", "rsi1", "rsi4", "pctb", "adx4", "adxs", "rv", "vov", "volz", "fundz", "ret4", "ret24", "btcrel", "wat"]
CATS = {"hour": [0, 4, 8, 12, 16, 20], "dow": [0, 1, 2, 3, 4, 5, 6]}
ORIENT = {"rsi1": lambda x: 100 - x, "rsi4": lambda x: 100 - x, "pctb": lambda x: 1 - x,
          "ret4": lambda x: -x, "ret24": lambda x: -x, "fundz": lambda x: -x, "btcrel": lambda x: -x}


def load(d: Path, sym: str, iv: str) -> list[Candle]:
    raw = json.loads((d / f"{sym}_{iv}.json").read_text())
    return [Candle(int(r["t"]), int(r["T"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r.get("v", 0) or 0)) for r in raw][:-1]


class Sym:
    def __init__(self, d: Path, sym: str) -> None:
        self.sym = sym
        self.h1, self.h4, self.d1 = load(d, sym, "1h"), load(d, sym, "4h"), load(d, sym, "1d")
        f = d / f"{sym}_funding.json"
        self.fund = {int(r["time"]) // H * H: float(r["fundingRate"]) for r in json.loads(f.read_text())} if f.exists() else {}
        self.i1 = {c.ts: i for i, c in enumerate(self.h1)}
        hc = [c.close for c in self.h1]
        self.hc = hc
        self.rsi1, self.atr1 = rsi(hc, 14), atr(self.h1, 14)
        c4 = [c.close for c in self.h4]
        self.rsi4, self.atr4, self.adx4 = rsi(c4, 14), atr(self.h4, 14), adx(self.h4, 14)
        self.bbu, self.bbm, self.bbl = bollinger(c4, 20, 2.0)
        self.dadx = adx(self.d1, 14)
        # 1h 对数收益的 24h 滚动标准差
        lr = [None] + [math.log(hc[i] / hc[i - 1]) for i in range(1, len(hc))]
        self.rv = [None] * len(hc)
        for i in range(25, len(hc)):
            w = lr[i - 23 : i + 1]
            m = sum(w) / 24
            self.rv[i] = math.sqrt(sum((x - m) ** 2 for x in w) / 23)

    def close_at(self, T: int) -> float | None:  # 收盘时刻 T 的 1h 收盘价
        i = self.i1.get(T - H)
        return self.hc[i] if i is not None else None

    def daily_adx_at(self, T: int) -> float | None:
        lo, hi = 0, len(self.d1) - 1; j = -1
        while lo <= hi:
            m = (lo + hi) // 2
            if self.d1[m].end_ts < T:
                j = m; lo = m + 1
            else:
                hi = m - 1
        return self.dadx[j] if j >= 0 else None


def rows(S: Sym, btc: Sym | None, with_forward: bool = True) -> list[dict]:
    out = []
    h4 = S.h4
    for j in range(35, len(h4)):
        T = h4[j].ts + H4
        k = S.i1.get(T - H)
        if k is None or k < 200:
            continue
        w = h4[j - 29 : j + 1]
        U, L = max(c.high for c in w), min(c.low for c in w)
        W = U - L
        C = S.hc[k]
        a4, x4 = S.atr4[j], S.adx4[j]
        if W <= 0 or not a4 or x4 is None or S.adx4[j - 3] is None:
            continue
        pos = (C - L) / W
        r = {"sym": S.sym, "T": T, "C": C, "U": U, "L": L, "atr4": a4, "atr1": S.atr1[k], "pos": pos,
             "dist": min(C - L, U - C) / a4, "rsi1": S.rsi1[k], "rsi4": S.rsi4[j],
             "pctb": (C - S.bbl[j]) / (S.bbu[j] - S.bbl[j]) if S.bbu[j] is not None and S.bbu[j] > S.bbl[j] else None,
             "adx4": x4, "adxs": x4 - S.adx4[j - 3], "rv": S.rv[k], "wat": W / a4, "dadx": S.daily_adx_at(T)}
        rvs = [S.rv[k - 4 * q] for q in range(42) if k - 4 * q >= 0 and S.rv[k - 4 * q] is not None]
        r["vov"] = (math.sqrt(sum((x - sum(rvs) / len(rvs)) ** 2 for x in rvs) / (len(rvs) - 1)) / (sum(rvs) / len(rvs))) if len(rvs) == 42 else None
        vols = [c.volume for c in h4[j - 30 : j]]
        mv = sum(vols) / 30; sv = math.sqrt(sum((v - mv) ** 2 for v in vols) / 29)
        r["volz"] = (h4[j].volume - mv) / sv if sv > 0 else None
        f24 = [S.fund.get(T - q * H) for q in range(24)]
        f720 = [S.fund.get(T - q * H) for q in range(720)]
        if None in f24 or None in f720:
            r["fundz"] = None
        else:
            m7 = sum(f720) / 720; s7 = math.sqrt(sum((x - m7) ** 2 for x in f720) / 719)
            r["fundz"] = (sum(f24) / 24 - m7) / s7 if s7 > 0 else 0.0
        c4, c24 = S.close_at(T - 4 * H), S.close_at(T - 24 * H)
        r["ret4"] = C / c4 - 1 if c4 else None
        r["ret24"] = C / c24 - 1 if c24 else None
        if btc is not None and S.sym != "BTC":
            b0, b24 = btc.close_at(T), btc.close_at(T - 24 * H)
            r["btcrel"] = (r["ret24"] - (b0 / b24 - 1)) if (b0 and b24 and r["ret24"] is not None) else None
        else:
            r["btcrel"] = None
        r["hour"] = (T // H) % 24
        r["dow"] = dt.datetime.fromtimestamp(T / 1000, dt.timezone.utc).weekday()
        r["week"] = (T // 86_400_000 + 3) // 7  # UTC 周一为界
        if with_forward:
            for h in HORIZONS:
                ch = S.close_at(T + h * H)
                fs = [S.fund.get(T + q * H) for q in range(1, h + 1)]
                r[f"r{h}"] = ch / C - 1 if ch else None
                r[f"f{h}"] = sum(x for x in fs if x is not None) if ch else None
        out.append(r)
    return out


def filt(r: dict, name: str) -> bool:
    if name == "ALL":
        return True
    if name == "RA":
        return r["adx4"] < 20
    if name == "RB":
        return r["adx4"] < 25 and r["dadx"] is not None and r["dadx"] < 22.5
    if name == "RC":
        return r["adx4"] < 25 and 4 <= r["wat"] <= 12
    raise KeyError(name)


def edge_dir(r: dict) -> int:
    return 1 if r["pos"] <= 0.2 else -1 if r["pos"] >= 0.8 else 0


def oriented(r: dict, f: str) -> float | None:
    v = r.get(f)
    if v is None:
        return None
    return ORIENT[f](v) if (f in ORIENT and edge_dir(r) < 0) else v


def quantiles(vals: list[float], q: int = 5) -> list[float]:
    s = sorted(vals)
    if not s:
        return []
    return [s[int(len(s) * i / q)] for i in range(1, q)]


def bucket(v: float, th: list[float]) -> int:
    b = 0
    for x in th:
        if v >= x:
            b += 1
    return b
