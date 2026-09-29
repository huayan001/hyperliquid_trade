"""独立短线震荡/均值回归策略回测（候选 A/B/C，规则见 PLAN.md，参数预注册、未调参）。
用法（backtest/short/ 下）：python short.py   → results_short.pkl + 打印；python short.py report → report_short_tables.md + equity_short.png
仓库布局 backtest/short/ → ../../src；其他位置用 PYTHONPATH=<repo>/src。"""
from __future__ import annotations

import json, math, pickle, sys, datetime as dt
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src"))
from hl_bot.indicators import adx, atr, bollinger, ema, rsi  # noqa: E402
from hl_bot.models import Candle  # noqa: E402

H = 3_600_000
FEE_T, FEE_M, SLIP = 0.00045, 0.00015, 0.0002
START_EQ = 1000.0
DEFAULT_FUNDING = 0.0000125
TICK = {"BTC": 1.0, "ETH": 0.1, "SOL": 0.01, "HYPE": 0.001}
POS_NOTIONAL_CAP, TOTAL_NOTIONAL_CAP, MIN_NOTIONAL = 3.0, 10.0, 10.0
PERIODS = [("2023Q4-2024Q1", 1696118400000, 1711929600000), ("2024Q2-Q4", 1711929600000, 1735689600000),
           ("2025H1", 1735689600000, 1751328000000), ("2025H2", 1751328000000, 1767225600000),
           ("2026", 1767225600000, 1798761600000)]
DATASETS = {  # name: (dir, test_start, oos_frac, L0 results pkl)
    "long": (HERE.parent / "data_long", 1696118400000, 0.35, HERE.parent / "results_long.pkl"),
    "hl": (HERE.parent / "data", 1773964800000, 0.40, HERE.parent / "results.pkl"),
}


def load(d: Path, sym: str, iv: str) -> list[Candle]:
    raw = json.loads((d / f"{sym}_{iv}.json").read_text())
    return [Candle(int(r["t"]), int(r["T"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r.get("v", 0) or 0)) for r in raw][:-1]


class SD:
    def __init__(self, d: Path, sym: str) -> None:
        self.sym, self.tick = sym, TICK[sym]
        self.h1, self.h4, self.d1 = load(d, sym, "1h"), load(d, sym, "4h"), load(d, sym, "1d")
        f = d / f"{sym}_funding.json"
        self.funding = {int(r["time"]) // H * H: float(r["fundingRate"]) for r in json.loads(f.read_text())} if f.exists() else {}
        hc = [c.close for c in self.h1]
        self.atr1, self.rsi14, self.rsi2 = atr(self.h1, 14), rsi(hc, 14), rsi(hc, 2)
        self.bbu, self.bbm, self.bbl = bollinger(hc, 20, 2.0)
        self.atr4, self.adx4 = atr(self.h4, 14), adx(self.h4, 14)
        n4 = len(self.h4)
        self.U, self.L = [None] * n4, [None] * n4
        for j in range(29, n4):  # 最近 30 根 4h（含当根）
            w = self.h4[j - 29 : j + 1]
            self.U[j], self.L[j] = max(c.high for c in w), min(c.low for c in w)
        dc = [c.close for c in self.d1]
        self.dadx, self.de20, self.de50 = adx(self.d1, 14), ema(dc, 20), ema(dc, 50)
        self.idx = {c.ts: i for i, c in enumerate(self.h1)}
        self.h4_at, self.d_at = [], []
        di = hi = -1
        for c in self.h1:
            t = c.ts + H
            while di + 1 < len(self.d1) and self.d1[di + 1].end_ts < t:
                di += 1
            while hi + 1 < n4 and self.h4[hi + 1].end_ts < t:
                hi += 1
            self.d_at.append(di); self.h4_at.append(hi)

    def dv(self, arr, i):
        j = self.d_at[i]
        return arr[j] if j >= 0 else None

    def fv(self, arr, i):
        j = self.h4_at[i]
        return arr[j] if j >= 0 else None


@dataclass
class Order:
    side: int; limit: float; stop: float; target: float | None; start_i: int; expire_i: int; risk_pct: float
    rng: tuple | None = None


@dataclass
class Pos:
    sym: str; side: int; size: float; entry: float; stop: float; target: float | None; open_i: int; open_ts: int
    risk_usd: float; fees: float = 0.0; fund: float = 0.0; exit_next: str | None = None


@dataclass
class Trade:
    sym: str; side: int; open_ts: int; close_ts: int; pnl: float; fees: float; fund: float; risk: float; reason: str; bars: int


class Sim:
    def __init__(self, cand: str, data: dict[str, SD], test_start: int) -> None:
        self.c, self.D, self.t0 = cand, data, test_start
        self.cash = START_EQ
        self.pos: dict[str, Pos] = {}
        self.orders: dict[str, Order] = {}
        self.cool: dict[str, int] = {}  # sym -> 冷却到的 ts（不含）
        self.trades: list[Trade] = []
        self.curve: list[tuple[int, float]] = []
        self.fees = 0.0
        self.bars_any = self.bars_total = 0

    # ------------------------------------------------------------ helpers
    def _eq(self, px: dict[str, float]) -> float:
        return self.cash + sum(p.side * (px[s] - p.entry) * p.size for s, p in self.pos.items() if s in px)

    def _close(self, s: str, px: float, ts: int, i: int, reason: str, maker: bool) -> None:
        p = self.pos.pop(s)
        fee = p.size * px * (FEE_M if maker else FEE_T)
        self.fees += fee
        gross = p.side * (px - p.entry) * p.size
        self.cash += gross - fee
        p.fees += fee
        self.trades.append(Trade(s, p.side, p.open_ts, ts, gross - p.fees - p.fund, p.fees, p.fund, p.risk_usd, reason, i - p.open_i))
        if reason == "stop":
            self.cool[s] = ts + 4 * H if self.c != "B" else (ts // (4 * H) + 1) * 4 * H + 4 * H

    # ------------------------------------------------------------ main loop
    def run(self, timeline: list[int]) -> None:
        for ts in timeline:
            im = {s: d.idx[ts] for s, d in self.D.items() if ts in d.idx}
            opens = {s: self.D[s].h1[i].open for s, i in im.items()}
            # 1) 上根收盘决定的市价离场
            for s in list(self.pos):
                p = self.pos[s]
                if s in im and p.exit_next:
                    o = opens[s] * (1 - SLIP * p.side)
                    self._close(s, o, ts, im[s], p.exit_next, False)
            eq_open = self._eq(opens)
            # 2) 盘中：持仓先止损后止盈；挂单成交
            for s, i in im.items():
                c = self.D[s].h1[i]; tk = self.D[s].tick
                if s in self.pos:
                    p = self.pos[s]
                    if (p.side > 0 and c.low <= p.stop) or (p.side < 0 and c.high >= p.stop):
                        px = (min(p.stop, c.open) if p.side > 0 else max(p.stop, c.open)) * (1 - SLIP * p.side)
                        self._close(s, px, ts + H // 2, i, "stop", False)
                    elif p.target is not None and i > p.open_i and ((p.side > 0 and c.high >= p.target + tk) or (p.side < 0 and c.low <= p.target - tk)):
                        self._close(s, p.target, ts + H // 2, i, "target", True)
                    continue
                o = self.orders.get(s)
                if o is None or i < o.start_i:
                    continue
                if i > o.expire_i:
                    del self.orders[s]; continue
                if (o.side > 0 and c.low <= o.limit - tk) or (o.side < 0 and c.high >= o.limit + tk):
                    del self.orders[s]
                    dist = abs(o.limit - o.stop)
                    if dist <= 0 or eq_open <= 0:
                        continue
                    size = eq_open * o.risk_pct / dist
                    size = min(size, POS_NOTIONAL_CAP * eq_open / o.limit)
                    used = sum(q.size * opens.get(k, q.entry) for k, q in self.pos.items())
                    size = min(size, max(0.0, (TOTAL_NOTIONAL_CAP * eq_open - used) / o.limit))
                    if size * o.limit < MIN_NOTIONAL:
                        continue
                    fee = size * o.limit * FEE_M
                    self.fees += fee; self.cash -= fee
                    p = Pos(s, o.side, size, o.limit, o.stop, o.target, i, ts, size * dist, fees=fee)
                    self.pos[s] = p
                    if (p.side > 0 and c.low <= p.stop) or (p.side < 0 and c.high >= p.stop):  # 保守：同根被止损
                        self._close(s, p.stop * (1 - SLIP * p.side), ts + H // 2, i, "stop", False)
            # 3) 收盘：资金费、管理、信号
            for s, i in im.items():
                d = self.D[s]; c = d.h1[i]
                if s in self.pos:
                    p = self.pos[s]
                    rate = d.funding.get(ts + H, d.funding.get(ts, DEFAULT_FUNDING))
                    f = p.side * p.size * c.close * rate
                    self.cash -= f; p.fund += f
                    self._manage(s, d, i, p)
                    continue
                if ts < self.t0:
                    continue
                if s in self.orders:
                    self._maintain(s, d, i)
                    continue
                if self.cool.get(s, 0) > ts + H:
                    continue
                o = self._signal(d, i)
                if o is not None:
                    self.orders[s] = o
            if ts >= self.t0:
                self.bars_total += 1
                self.bars_any += bool(self.pos)
                self.curve.append((ts + H, self._eq({s: self.D[s].h1[i].close for s, i in im.items()})))
        last = timeline[-1]
        for s in list(self.pos):
            i = self.D[s].idx.get(last, len(self.D[s].h1) - 1)
            self._close(s, self.D[s].h1[i].close, last + H, i, "end", False)

    # ------------------------------------------------------------ candidates
    def _manage(self, s: str, d: SD, i: int, p: Pos) -> None:
        held = i - p.open_i + 1
        T = {"A": 24, "B": 72, "C": 12}[self.c]
        if held >= T:
            p.exit_next = "time"; return
        if self.c == "A":
            m = d.bbm[i]
            if m is not None:
                if (p.side > 0 and m <= d.h1[i].close) or (p.side < 0 and m >= d.h1[i].close):
                    p.exit_next = "mid_cross"  # 收盘已越过中轨（止盈限价会成为吃单）→ 下根开盘市价离场
                else:
                    p.target = m

    def _maintain(self, s: str, d: SD, i: int) -> None:
        o = self.orders[s]
        if self.c == "B" and o.rng is not None and i > 0 and d.h4_at[i] != d.h4_at[i - 1]:
            cl = d.h4[d.h4_at[i]].close
            if cl > o.rng[0] or cl < o.rng[1]:
                del self.orders[s]

    def _signal(self, d: SD, i: int) -> Order | None:
        c = d.h1[i]; a = d.atr1[i]
        if self.c == "A":
            da, fa = d.dv(d.dadx, i), d.fv(d.adx4, i)
            u, m, l, r = d.bbu[i], d.bbm[i], d.bbl[i], d.rsi14[i]
            if None in (da, fa, u, m, l, r, a) or not (da < 22.5 and fa < 25):
                return None
            side = 1 if (c.close < l and r < 35) else -1 if (c.close > u and r > 65) else 0
            if not side:
                return None
            e = c.close - side * 0.25 * a
            dist = max(2.0 * a, 0.006 * e)
            if side * (m - e) < 0.5 * dist:
                return None
            return Order(side, e, e - side * dist, m, i + 1, i + 3, 0.0075)
        if self.c == "B":
            if i == 0 or d.h4_at[i] == d.h4_at[i - 1]:
                return None  # 只在新的 4h 收盘
            j = d.h4_at[i]
            if j < 12 or d.U[j] is None or d.U[j - 12] is None:
                return None
            U, L, a4, fa, da = d.U[j], d.L[j], d.atr4[j], d.adx4[j], d.dv(d.dadx, i)
            if None in (a4, fa, da):
                return None
            W, W0 = U - L, d.U[j - 12] - d.L[j - 12]
            if not (fa < 20 and da < 25 and W >= 4 * a4 and W0 > 0 and 0.8 <= W / W0 <= 1.25):
                return None
            cl = d.h4[j].close
            if cl <= L + 0.25 * W:
                return Order(1, L + 0.10 * W, L - 0.5 * a4, (U + L) / 2, i + 1, i + 16, 0.0075, (U, L))
            if cl >= U - 0.25 * W:
                return Order(-1, U - 0.10 * W, U + 0.5 * a4, (U + L) / 2, i + 1, i + 16, 0.0075, (U, L))
            return None
        # C
        da, e20, e50, r2 = d.dv(d.dadx, i), d.dv(d.de20, i), d.dv(d.de50, i), d.rsi2[i]
        if None in (da, e20, e50, r2, a) or da < 20:
            return None
        side = 1 if (e20 > e50 and r2 < 10) else -1 if (e20 < e50 and r2 > 90) else 0
        if not side:
            return None
        e = c.close - side * 0.2 * a
        dist = max(1.5 * a, 0.006 * e)
        return Order(side, e, e - side * dist, e + side * 1.2 * a, i + 1, i + 2, 0.005)


# ---------------------------------------------------------------- metrics
def stats(trades: list[Trade]) -> dict:
    n = len(trades)
    gw = sum(t.pnl for t in trades if t.pnl > 0); gl = -sum(t.pnl for t in trades if t.pnl <= 0)
    gross_pre = sum(t.pnl + t.fees for t in trades if t.pnl + t.fees > 0)
    fees = sum(t.fees for t in trades)
    return {"trades": n, "win": sum(t.pnl > 0 for t in trades) / n if n else 0.0, "pnl": sum(t.pnl for t in trades),
            "pf": gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0),
            "avg_r": sum(t.pnl / t.risk for t in trades if t.risk > 0) / n if n else 0.0,
            "fees": fees, "fee_ratio": fees / gross_pre if gross_pre > 0 else float("nan"),
            "funding": sum(t.fund for t in trades), "avg_bars": sum(t.bars for t in trades) / n if n else 0.0}


def max_dd(vals, base):
    pk, dd = base, 0.0
    for v in vals:
        pk = max(pk, v); dd = max(dd, (pk - v) / pk if pk > 0 else 0)
    return dd


def monthly(curve: list[tuple[int, float]]) -> dict[str, float]:
    last: dict[str, float] = {}
    for t, v in curve:
        last[dt.datetime.fromtimestamp((t - 1) / 1000, dt.timezone.utc).strftime("%Y-%m")] = v
    ks = sorted(last); out = {}; prev = START_EQ
    for k in ks:
        out[k] = last[k] / prev - 1; prev = last[k]
    return out


def corr(a: dict, b: dict) -> float:
    ks = sorted(set(a) & set(b))
    if len(ks) < 3:
        return float("nan")
    x, y = [a[k] for k in ks], [b[k] for k in ks]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x)); sy = math.sqrt(sum((v - my) ** 2 for v in y))
    return sum((p - mx) * (q - my) for p, q in zip(x, y)) / (sx * sy) if sx and sy else float("nan")


def run_set(ds: str, syms: list[str]) -> dict:
    d, t0, oosf, l0pkl = DATASETS[ds]
    data = {s: SD(d, s) for s in syms}
    tl = sorted(set().union(*[set(x.idx) for x in data.values()]))
    test_ts = [t + H for t in tl if t >= t0]
    split = test_ts[int(len(test_ts) * (1 - oosf))]
    ref = data["BTC" if "BTC" in data else syms[0]]
    l0 = pickle.loads(l0pkl.read_bytes())["L0 实盘趋势"]["curve"] if l0pkl.exists() else None
    out = {"_meta": dict(ds=ds, syms=syms, t0=t0, split=split, end=test_ts[-1], oos_frac=oosf, bars=len(test_ts))}
    for cand in "ABC":
        sim = Sim(cand, data, t0); sim.run(tl)
        cv = [v for _, v in sim.curve]
        r = {"all": stats(sim.trades), "ret": cv[-1] / START_EQ - 1, "mdd": max_dd(cv, START_EQ),
             "exposure": sim.bars_any / sim.bars_total, "curve": sim.curve, "exits": {}, "per": {}, "solo": {}, "periods": {}}
        for t in sim.trades:
            r["exits"][t.reason] = r["exits"].get(t.reason, 0) + 1
        is_cv = [v for t, v in sim.curve if t <= split]; oos_cv = [v for t, v in sim.curve if t > split]
        r["is"] = stats([t for t in sim.trades if t.open_ts < split]); r["is"]["ret"] = is_cv[-1] / START_EQ - 1
        r["oos"] = stats([t for t in sim.trades if t.open_ts >= split]); r["oos"]["ret"] = oos_cv[-1] / is_cv[-1] - 1
        r["oos"]["mdd"] = max_dd(oos_cv, is_cv[-1])
        for s in syms:
            r["per"][s] = stats([t for t in sim.trades if t.sym == s])
            so = Sim(cand, {s: data[s]}, t0); so.run(sorted(data[s].idx))
            sc = [v for _, v in so.curve]
            st = stats(so.trades); st["ret"] = sc[-1] / START_EQ - 1; st["mdd"] = max_dd(sc, START_EQ)
            r["solo"][s] = st
        for lab, a, b in PERIODS:
            seg = [(t, v) for t, v in sim.curve if a < t <= b]
            if len(seg) < 24:
                continue
            prev = [v for t, v in sim.curve if t <= a]; base = prev[-1] if prev else START_EQ
            st = stats([t for t in sim.trades if a <= t.open_ts < b])
            st["ret"] = seg[-1][1] / base - 1; st["mdd"] = max_dd([v for _, v in seg], base)
            rc = [c for c in ref.h1 if a <= c.ts < b and c.ts >= t0]
            st["ref_chg"] = rc[-1].close / rc[0].open - 1 if rc else float("nan")
            r["periods"][lab] = st
        r["monthly"] = monthly(sim.curve)
        if l0:
            r["corr_l0"] = corr(r["monthly"], monthly(l0))
        out[cand] = r
    if l0:
        out["_L0"] = {"curve": l0, "monthly": monthly(l0)}
    return out


def main() -> None:
    res = {}
    for key, ds, syms in (("long4", "long", ["BTC", "ETH", "SOL", "HYPE"]), ("long3", "long", ["ETH", "SOL", "HYPE"]),
                          ("hl4", "hl", ["BTC", "ETH", "SOL", "HYPE"]), ("hl3", "hl", ["ETH", "SOL", "HYPE"])):
        res[key] = run_set(ds, syms)
        for c in "ABC":
            r = res[key][c]; a = r["all"]
            print(f"[{key}] {c}: ret={r['ret']*100:+.1f}% mdd={r['mdd']*100:.1f}% n={a['trades']} win={a['win']*100:.0f}% pf={a['pf']:.2f} "
                  f"avgR={a['avg_r']:+.3f} fees=${a['fees']:.0f} ({a['fee_ratio']*100:.0f}% of gross) fund=${a['funding']:.1f} exp={r['exposure']*100:.0f}% "
                  f"| IS n={r['is']['trades']} pf={r['is']['pf']:.2f} ret={r['is']['ret']*100:+.1f}% | OOS n={r['oos']['trades']} pf={r['oos']['pf']:.2f} "
                  f"ret={r['oos']['ret']*100:+.1f}% | corrL0={r.get('corr_l0', float('nan')):+.2f}", flush=True)
            print("      exits", r["exits"], "| per-sym pnl", {s: round(v["pnl"], 1) for s, v in r["per"].items()},
                  "| solo ret", {s: f"{v['ret']*100:+.1f}%" for s, v in r["solo"].items()})
            print("      periods", {k: f"{v['ret']*100:+.1f}%/n{v['trades']}/pf{v['pf']:.2f}" for k, v in r["periods"].items()})
    (HERE / "results_short.pkl").write_bytes(pickle.dumps(res))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        import short_report  # noqa: F401
    else:
        main()
