"""Hyperliquid 永续回测：V0 现行策略 vs V1/V2 短周期 + 分批止盈/保本/吊灯止损 vs V3 最小改动。

- 数据：Hyperliquid candleSnapshot（1h/4h/1d）+ fundingHistory（BTC/ETH 实际，SOL/HYPE 用 BTC 代理）
- 复用仓库 hl_bot 的指标与环境路由（EMA/ADX/ATR/Donchian/decide_from_metrics/detect_range_structure）
- 无前视：1h K 收盘时只用已收盘的 1d/4h/1h；信号在下一根 1h 开盘成交
- 费用：taker 0.045%，maker 0.015%；止损/市价额外滑点 0.02%
- 组合：共享权益，单笔风险 2%，最多 2 仓，组合止损风险 ≤4%，10x 逐仓（止损距离 ≤5%）
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hl_bot.config import RegimeConfig  # noqa: E402
from hl_bot.indicators import adx, atr, donchian, ema  # noqa: E402
from hl_bot.models import Candle, Regime  # noqa: E402
from hl_bot.regime import decide_from_metrics, detect_range_structure  # noqa: E402

DATA = Path(__file__).parent / "data"
SYMS = ["BTC", "ETH", "SOL", "HYPE"]
H = 3_600_000
FEE_T, FEE_M, SLIP = 0.00045, 0.00015, 0.0002
RISK, MAXPOS, PORT_RISK, LEV, MAX_STOP = 0.02, 2, 0.04, 10, 0.05
START_EQ = 1000.0
TEST_START = 1773964800000  # 2026-03-20 00:00 UTC（日线 EMA50/ADX 预热之后）
DEFAULT_FUNDING = 0.0000125  # 1h 基准费率（约 11% 年化）


def load(sym: str, iv: str) -> list[Candle]:
    raw = json.loads((DATA / f"{sym}_{iv}.json").read_text())
    out = [Candle(int(r["t"]), int(r["T"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])) for r in raw]
    return out[:-1]  # 丢掉最后一根（可能未收盘）


def load_funding(sym: str) -> dict[int, float]:
    f = DATA / f"{sym}_funding.json"
    if not f.exists():
        f = DATA / "BTC_funding.json"
    rows = json.loads(f.read_text())
    return {int(r["time"]) // H * H: float(r["fundingRate"]) for r in rows}


class Data:
    def __init__(self, sym: str) -> None:
        self.sym = sym
        self.h1, self.h4, self.d1 = load(sym, "1h"), load(sym, "4h"), load(sym, "1d")
        self.funding = load_funding(sym)
        self.funding_proxy = not (DATA / f"{sym}_funding.json").exists()
        d = self.d1
        closes = [c.close for c in d]
        self.d_ema20, self.d_ema50, self.d_adx = ema(closes, 20), ema(closes, 50), adx(d, 14)
        self.h4_atr = atr(self.h4, 14)
        self.h4_up, self.h4_lo = donchian(self.h4, 20, exclude_current=True)
        h = self.h1
        hc = [c.close for c in h]
        self.h_atr, self.h_ema20, self.h_ema50, self.h_adx = atr(h, 14), ema(hc, 20), ema(hc, 50), adx(h, 14)
        self.h_up, self.h_lo = donchian(h, 20, exclude_current=True)
        self.idx = {c.ts: i for i, c in enumerate(h)}
        # 每根 1h 收盘时刻可用的最后一根已收盘 1d / 4h 索引
        self.d_at, self.h4_at = [], []
        di = hi = -1
        for c in h:
            close_t = c.ts + H
            while di + 1 < len(d) and d[di + 1].end_ts < close_t:
                di += 1
            while hi + 1 < len(self.h4) and self.h4[hi + 1].end_ts < close_t:
                hi += 1
            self.d_at.append(di)
            self.h4_at.append(hi)
        cfg = RegimeConfig()
        self.regime: list[Regime] = []
        for i in range(len(h)):
            di = self.d_at[i]
            ef, es, da, ha = (self.d_ema20[di], self.d_ema50[di], self.d_adx[di], self.h_adx[i]) if di >= 0 else (None,) * 4
            if None in (ef, es, da, ha) or i < 30:
                self.regime.append(Regime.WATCH)
                continue
            rng = detect_range_structure(h[max(0, i - 11) : i + 1], lookback_hours=cfg.range_lookback_hours, touch_frac=cfg.range_touch_frac)
            self.regime.append(decide_from_metrics(ema_fast=ef, ema_slow=es, daily_adx=da, hourly_adx=ha, rng=rng, cfg=cfg).regime)

    def trend_side(self, i: int) -> int:
        r = self.regime[i]
        if r is Regime.TREND_LONG:
            return 1
        if r is Regime.TREND_SHORT:
            if self.sym == "BTC" and (self.d_adx[self.d_at[i]] or 0) <= 25.0:
                return 0
            return -1
        return 0

    def trend_invalid(self, i: int, side: int) -> bool:
        di = self.d_at[i]
        ef, es, da = self.d_ema20[di], self.d_ema50[di], self.d_adx[di]
        if ef is None or es is None:
            return False
        if (side > 0 and ef < es) or (side < 0 and ef > es):
            return True
        return da is not None and da < 15.0

    def trend_confirmed(self, i: int, side: int) -> bool:
        di = self.d_at[i]
        ef, es, da = self.d_ema20[di], self.d_ema50[di], self.d_adx[di]
        if ef is None or es is None or da is None:
            return False
        return ((side > 0 and ef > es) or (side < 0 and ef < es)) and da >= 15.0

    def atr4(self, i: int) -> float | None:
        j = self.h4_at[i]
        return self.h4_atr[j] if j >= 0 else None

    def donchian4_confirmed(self, i: int, side: int) -> bool:
        j = self.h4_at[i]
        if j < 0 or self.h4_up[j] is None:
            return False
        c = self.h4[j].close
        return c > self.h4_up[j] if side > 0 else c < self.h4_lo[j]

    def new_4h_bar(self, i: int) -> bool:
        return i > 0 and self.h4_at[i] != self.h4_at[i - 1]


@dataclass
class Pos:
    sym: str
    side: int
    size: float
    avg: float
    stop: float
    r_dist: float  # 初始止损距离（价格单位）
    init_size: float
    open_i: int
    open_ts: int
    risk_unit: float  # 2% × 开仓时权益（R 单位，美元）
    realized: float = 0.0
    peak: float = 0.0
    hh: float = 0.0  # 持仓以来最高价（多）/最低价（空）
    be: bool = False
    partial: bool = False
    adds: int = 0
    starter_pending: bool = False
    intended_full: float = 0.0
    intended_risk: float = 0.0
    tier_done: bool = False
    pyramid_frac: float = 0.0
    orig_size: float = 0.0
    notional_max: float = 0.0

    def unreal(self, px: float) -> float:
        return self.side * (px - self.avg) * self.size

    def risk_usd(self) -> float:
        return max(0.0, self.side * (self.avg - self.stop) * self.size)


@dataclass
class Trade:
    sym: str
    side: int
    open_ts: int
    close_ts: int
    pnl: float
    risk_unit: float
    peak: float
    exit_reason: str
    bars: int


@dataclass
class Params:
    name: str
    kind: str  # v0 / v1 / v3
    stop_atr: float = 1.5
    be_r: float = 1.0
    partial_r: float | None = 1.5
    partial_frac: float = 0.5
    trail_atr: float = 2.0
    cooldown_4h: int = 2
    max_adds: int = 0
    add_frac: float = 0.5


class Sim:
    def __init__(self, data: dict[str, Data], p: Params) -> None:
        self.D, self.p = data, p
        self.cash = START_EQ
        self.pos: dict[str, Pos] = {}
        self.pending: dict[str, tuple] = {}  # sym -> action tuple，下一根开盘执行
        self.trades: list[Trade] = []
        self.curve: list[tuple[int, float]] = []
        self.sym_curve: dict[str, list[float]] = {s: [] for s in data}
        self.sym_realized: dict[str, float] = {s: 0.0 for s in data}
        self.sym_bars_in: dict[str, int] = {s: 0 for s in data}
        self.bars_any = 0
        self.bars_total = 0
        self.cooldown_until: dict[tuple[str, int], int] = {}
        self.fees = 0.0
        self.funding_paid = 0.0

    # ---------------------------------------------------------------- helpers
    def equity(self, i_map: dict[str, int], use_close: bool = True) -> float:
        eq = self.cash
        for s, p in self.pos.items():
            i = i_map.get(s)
            if i is not None:
                eq += p.unreal(self.D[s].h1[i].close)
        return eq

    def port_risk(self) -> float:
        return sum(p.risk_usd() for p in self.pos.values())

    def _fee(self, notional: float, maker: bool) -> float:
        f = notional * (FEE_M if maker else FEE_T)
        self.fees += f
        return f

    def _exit(self, s: str, qty: float, px: float, ts: int, reason: str, maker: bool = False, i: int = 0) -> None:
        p = self.pos[s]
        qty = min(qty, p.size)
        pnl = p.side * (px - p.avg) * qty - self._fee(qty * px, maker)
        p.realized += pnl
        self.cash += pnl
        self.sym_realized[s] += pnl
        p.size -= qty
        if p.size <= 1e-12:
            self.trades.append(Trade(s, p.side, p.open_ts, ts, p.realized, p.risk_unit, p.peak, reason, i - p.open_i))
            del self.pos[s]
            if self.p.cooldown_4h > 0 and reason == "stop" and p.realized < 0:
                bars = self.p.cooldown_4h
                first = -(-ts // (4 * H)) * (4 * H)
                self.cooldown_until[(s, p.side)] = first + bars * 4 * H

    def _add(self, p: Pos, qty: float, px: float) -> None:
        fee = self._fee(qty * px, False)
        p.realized -= fee
        self.cash -= fee
        self.sym_realized[p.sym] -= fee
        p.avg = (p.avg * p.size + px * qty) / (p.size + qty)
        p.size += qty
        p.notional_max = max(p.notional_max, p.size * px)

    # ---------------------------------------------------------------- main loop
    def run(self, timeline: list[int]) -> None:
        for ts in timeline:
            i_map = {s: d.idx[ts] for s, d in self.D.items() if ts in d.idx}
            # 1) 上一根收盘决定的挂单在本根开盘执行
            for s, act in list(self.pending.items()):
                if s not in i_map:
                    continue
                self._do_pending(s, act, i_map[s], i_map)
            self.pending.clear()
            # 2) 盘中：止损优先，其次止盈限价
            for s in list(self.pos):
                if s not in i_map:
                    continue
                self._intrabar(s, i_map[s], ts)
            # 3) 收盘：资金费、峰值、管理、信号
            for s in list(self.pos):
                if s not in i_map:
                    continue
                i = i_map[s]
                p = self.pos[s]
                c = self.D[s].h1[i]
                rate = self.D[s].funding.get(ts + H, self.D[s].funding.get(ts, DEFAULT_FUNDING))
                fund = p.side * p.size * c.close * rate
                p.realized -= fund
                self.cash -= fund
                self.sym_realized[s] -= fund
                self.funding_paid += fund
                p.hh = max(p.hh, c.high) if p.side > 0 else min(p.hh, c.low)
                p.peak = max(p.peak, p.realized + p.unreal(c.close))
            eq = self.equity(i_map)
            for s, i in i_map.items():
                if ts < TEST_START:
                    continue
                self._on_close(s, i, ts, eq)
            if ts >= TEST_START:
                self.bars_total += 1
                if self.pos:
                    self.bars_any += 1
                for s in self.pos:
                    self.sym_bars_in[s] += 1
                self.curve.append((ts + H, self.equity(i_map)))
                for s in self.D:
                    unreal = self.pos[s].unreal(self.D[s].h1[i_map[s]].close) if s in self.pos and s in i_map else 0.0
                    self.sym_curve[s].append(self.sym_realized[s] + unreal)
        # 期末按最后收盘价平掉
        last = timeline[-1]
        for s in list(self.pos):
            i = self.D[s].idx.get(last, len(self.D[s].h1) - 1)
            self._exit(s, self.pos[s].size, self.D[s].h1[i].close, last + H, "end", i=i)

    # ---------------------------------------------------------------- execution
    def _do_pending(self, s: str, act: tuple, i: int, i_map: dict[str, int]) -> None:
        d = self.D[s]
        o = d.h1[i].open
        ts = d.h1[i].ts
        kind = act[0]
        if kind == "close":
            if s in self.pos:
                p = self.pos[s]
                px = o * (1 - SLIP * p.side)
                self._exit(s, p.size, px, ts, act[1], i=i)
            return
        if kind == "open":
            _, side, dist, frac, extra = act
            if s in self.pos or len(self.pos) >= MAXPOS:
                return
            eq = self.equity({k: v for k, v in i_map.items()})
            px = o * (1 + SLIP * side)
            dist = min(dist, px * MAX_STOP)
            if dist <= 0:
                return
            full = eq * RISK / dist
            size = full * frac
            # 杠杆上限
            size = min(size, eq * LEV * 0.9 / px)
            risk = size * dist
            if self.port_risk() + risk > eq * PORT_RISK + 1e-9:
                return
            fee = self._fee(size * px, False)
            self.cash -= fee
            self.sym_realized[s] -= fee
            p = Pos(s, side, size, px, px - side * dist, dist, size, i, ts, eq * RISK, realized=-fee, hh=px, orig_size=size, notional_max=size * px)
            p.peak = p.realized
            if extra.get("starter"):
                p.starter_pending = True
                p.intended_full = full
                p.intended_risk = full * dist
            else:
                p.tier_done = True
            self.pos[s] = p
            return
        if kind == "add":
            if s not in self.pos:
                return
            p = self.pos[s]
            qty, mode = act[1], act[2]
            px = o * (1 + SLIP * p.side)
            eq = self.equity(i_map)
            qty = min(qty, max(0.0, eq * LEV * 0.9 / px - p.size))
            if qty <= 0:
                return
            if mode == "tier":
                room = p.intended_risk - p.risk_usd()
                add_risk = max(0.0, p.side * (px - p.stop)) * qty
                if add_risk > room and add_risk > 0:
                    qty *= max(0.0, room) / add_risk
                if qty * px < 10.0 * START_EQ / 1000:
                    return
                self._add(p, qty, px)
                p.starter_pending = False
                p.tier_done = True
                p.orig_size = p.size
            elif mode == "pyramid":
                atr_v = act[3] if len(act) > 3 else 0.0
                self._add(p, qty, px)
                be = p.avg + p.side * max(0.05 * atr_v, p.avg * 1e-5)
                p.stop = max(p.stop, be) if p.side > 0 else min(p.stop, be)
                p.pyramid_frac += qty / p.orig_size
            elif mode == "v2add":
                self._add(p, qty, px)
                be = p.avg * (1 + 0.001 * p.side)
                p.stop = max(p.stop, be) if p.side > 0 else min(p.stop, be)
                p.adds += 1

    def _intrabar(self, s: str, i: int, ts: int) -> None:
        p = self.pos[s]
        c = self.D[s].h1[i]
        hit = c.low <= p.stop if p.side > 0 else c.high >= p.stop
        if hit:
            px = min(p.stop, c.open) if p.side > 0 else max(p.stop, c.open)
            px *= 1 - SLIP * p.side
            self._exit(s, p.size, px, ts + H // 2, "stop", i=i)
            return
        if self.p.partial_r and not p.partial:
            tgt = p.avg + p.side * self.p.partial_r * p.r_dist
            if (p.side > 0 and c.high >= tgt) or (p.side < 0 and c.low <= tgt):
                p.partial = True
                self._exit(s, p.size * self.p.partial_frac, tgt, ts + H // 2, "partial", maker=True, i=i)

    # ---------------------------------------------------------------- strategy
    def _on_close(self, s: str, i: int, ts: int, eq: float) -> None:
        if self.p.kind in ("v0", "v3"):
            self._v0(s, i, ts, eq)
        else:
            self._v1(s, i, ts, eq)

    def _v0(self, s: str, i: int, ts: int, eq: float) -> None:
        d = self.D[s]
        c = d.h1[i]
        atr4 = d.atr4(i)
        p = self.pos.get(s)
        if p is not None:
            if d.trend_invalid(i, p.side):
                self.pending[s] = ("close", "trend_exit")
                return
            if not atr4:
                return
            px = c.close
            if p.starter_pending and d.trend_side(i) == p.side and d.donchian4_confirmed(i, p.side):
                add = p.intended_full - p.size
                if add > 1e-12:
                    self.pending[s] = ("add", add, "tier")
            elif p.tier_done and p.pyramid_frac < 0.5 - 1e-9 and d.trend_confirmed(i, p.side):
                if p.side * (px - p.avg) >= 1.0 * atr4:
                    self.pending[s] = ("add", p.orig_size * (0.5 - p.pyramid_frac), "pyramid", atr4)
            if self.p.kind == "v0":
                fav = max(0.0, p.side * (px - p.avg))
                steps = int(fav / atr4)
                cand = (p.avg - p.side * 2.0 * atr4) + p.side * steps * 0.5 * atr4
                p.stop = max(p.stop, cand) if p.side > 0 else min(p.stop, cand)
            else:  # v3：吊灯 + 1R 保本
                ch = p.hh - p.side * self.p.trail_atr * atr4
                if p.side * (px - p.avg) >= self.p.be_r * p.r_dist:
                    p.be = True
                cand = ch
                if p.be:
                    be = p.avg * (1 + 0.001 * p.side)
                    cand = max(ch, be) if p.side > 0 else min(ch, be)
                if p.side * (px - cand) > 0:
                    p.stop = max(p.stop, cand) if p.side > 0 else min(p.stop, cand)
            return
        side = d.trend_side(i)
        if side == 0 or not atr4 or self._cooling(s, side, ts):
            return
        if d.donchian4_confirmed(i, side):
            self.pending[s] = ("open", side, 2.0 * atr4, 1.0, {})
        else:
            self.pending[s] = ("open", side, 2.0 * atr4, 0.35, {"starter": True})

    def _cooling(self, s: str, side: int, ts: int) -> bool:
        until = self.cooldown_until.get((s, side))
        return until is not None and ts + H < until

    def _v1(self, s: str, i: int, ts: int, eq: float) -> None:
        d = self.D[s]
        c = d.h1[i]
        a = d.h_atr[i]
        e20, e50 = d.h_ema20[i], d.h_ema50[i]
        if a is None or e20 is None or e50 is None or i < 3:
            return
        p = self.pos.get(s)
        if p is not None:
            if d.trend_invalid(i, p.side):
                self.pending[s] = ("close", "trend_exit")
                return
            px = c.close
            if p.side * (px - p.avg) >= self.p.be_r * p.r_dist:
                p.be = True
            cand = p.hh - p.side * self.p.trail_atr * a
            if p.be:
                be = p.avg * (1 + 0.001 * p.side)
                cand = max(cand, be) if p.side > 0 else min(cand, be)
            if p.side * (px - cand) > 0:
                p.stop = max(p.stop, cand) if p.side > 0 else min(p.stop, cand)
            # V2 金字塔：保本已锁、新的 1h/4h 突破、最多 2 次，每次加后整仓止损 ≥ 保本
            if self.p.max_adds and p.be and p.adds < self.p.max_adds and d.trend_side(i) == p.side:
                up, lo = d.h_up[i], d.h_lo[i]
                brk1h = up is not None and ((p.side > 0 and px > up) or (p.side < 0 and px < lo))
                brk4h = d.new_4h_bar(i) and d.donchian4_confirmed(i, p.side)
                if brk1h or brk4h:
                    qty = p.init_size * self.p.add_frac
                    new_avg = (p.avg * p.size + px * qty) / (p.size + qty)
                    if p.side * (px - new_avg) >= 0.5 * a:
                        self.pending[s] = ("add", qty, "v2add")
            return
        side = d.trend_side(i)
        if side == 0 or self._cooling(s, side, ts):
            return
        lows = [d.h1[k].low for k in range(i - 2, i + 1)]
        highs = [d.h1[k].high for k in range(i - 2, i + 1)]
        up, lo = d.h_up[i], d.h_lo[i]
        if side > 0:
            pull = e20 > e50 and min(lows) <= e20 < c.close and c.close > d.h1[i - 1].high
            brk = up is not None and c.close > up and c.close > e20
        else:
            pull = e20 < e50 and max(highs) >= e20 > c.close and c.close < d.h1[i - 1].low
            brk = lo is not None and c.close < lo and c.close < e20
        if pull or brk:
            self.pending[s] = ("open", side, self.p.stop_atr * a, 1.0, {})


# -------------------------------------------------------------------- metrics


def max_dd(values: list[float], base: float) -> float:
    peak, dd = base, 0.0
    for v in values:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak if peak > 0 else 0.0)
    return dd


def trade_stats(trades: list[Trade]) -> dict:
    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    gw = sum(t.pnl for t in wins)
    gl = -sum(t.pnl for t in trades if t.pnl <= 0)
    rs = [t.pnl / t.risk_unit for t in trades if t.risk_unit > 0]
    gb = [(t.peak - t.pnl) / t.peak for t in trades if t.peak >= 0.5 * t.risk_unit and t.peak > 0]
    return {
        "trades": n,
        "win": len(wins) / n if n else 0.0,
        "pnl": sum(t.pnl for t in trades),
        "pf": gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0),
        "avg_r": sum(rs) / len(rs) if rs else 0.0,
        "giveback": sum(gb) / len(gb) if gb else float("nan"),
        "gb_n": len(gb),
    }


VERSIONS = [
    Params("V0 现行", "v0", cooldown_4h=0, partial_r=None),
    Params("V1 1h+分批", "v1", stop_atr=1.5, be_r=1.0, partial_r=1.5, trail_atr=2.0, cooldown_4h=2),
    Params("V2 1h+金字塔", "v1", stop_atr=1.5, be_r=1.0, partial_r=2.0, trail_atr=2.5, cooldown_4h=2, max_adds=2),
    Params("V3 V0+吊灯保本", "v3", cooldown_4h=0, partial_r=None, be_r=1.0, trail_atr=2.0),
]


def main() -> dict:
    data = {s: Data(s) for s in SYMS}
    timeline = sorted(set().union(*[set(d.idx) for d in data.values()]))
    timeline = [t for t in timeline if all(t in d.idx for d in data.values()) or t >= TEST_START]
    test_ts = [t + H for t in timeline if t >= TEST_START]
    split_ts = test_ts[int(len(test_ts) * 0.6)]
    results = {}
    for p in VERSIONS:
        sim = Sim(data, p)
        sim.run(timeline)
        curve = [v for _, v in sim.curve]
        res = {
            "portfolio": trade_stats(sim.trades),
            "ret": curve[-1] / START_EQ - 1,
            "mdd": max_dd(curve, START_EQ),
            "exposure": sim.bars_any / sim.bars_total,
            "fees": sim.fees,
            "funding": sim.funding_paid,
            "curve": sim.curve,
            "per": {},
            "is": {},
            "oos": {},
        }
        for s in SYMS:
            ts_ = [t for t in sim.trades if t.sym == s]
            st = trade_stats(ts_)
            st["ret"] = st["pnl"] / START_EQ
            st["mdd"] = max_dd([START_EQ + v for v in sim.sym_curve[s]], START_EQ)
            st["exposure"] = sim.sym_bars_in[s] / sim.bars_total
            res["per"][s] = st
            # 单标的独立回测（各自 $1000，不受「最多 2 仓 / 标的顺序」影响）
            solo = Sim({s: data[s]}, p)
            solo.run(sorted(data[s].idx))
            sc = [v for _, v in solo.curve]
            st2 = trade_stats(solo.trades)
            st2["ret"] = sc[-1] / START_EQ - 1
            st2["mdd"] = max_dd(sc, START_EQ)
            st2["exposure"] = solo.bars_any / solo.bars_total
            st2["fees"] = solo.fees
            res.setdefault("solo", {})[s] = st2
        # IS/OOS：按平仓时间分段；收益按资金曲线分段
        is_curve = [v for t, v in sim.curve if t <= split_ts]
        oos_curve = [v for t, v in sim.curve if t > split_ts]
        for key, tr, cv, base in (
            ("is", [t for t in sim.trades if t.close_ts <= split_ts], is_curve, START_EQ),
            ("oos", [t for t in sim.trades if t.close_ts > split_ts], oos_curve, is_curve[-1]),
        ):
            st = trade_stats(tr)
            st["ret"] = cv[-1] / base - 1
            st["mdd"] = max_dd(cv, base)
            res[key] = st
        results[p.name] = res
    results["_meta"] = {
        "test_start": TEST_START,
        "split_ts": split_ts,
        "end_ts": test_ts[-1],
        "bars": len(test_ts),
        "funding_proxy": [s for s in SYMS if data[s].funding_proxy],
    }
    return results


if __name__ == "__main__":
    import pickle

    out = main()
    (Path(__file__).parent / "results.pkl").write_bytes(pickle.dumps(out))
    for name, r in out.items():
        if name.startswith("_"):
            print(name, r)
            continue
        pf = r["portfolio"]
        print(
            f"{name:12s} ret={r['ret']*100:7.2f}% mdd={r['mdd']*100:6.2f}% trades={pf['trades']:3d} win={pf['win']*100:5.1f}% "
            f"pf={pf['pf']:.2f} avgR={pf['avg_r']:.2f} giveback={pf['giveback']*100:.0f}%(n={pf['gb_n']}) exp={r['exposure']*100:.0f}% "
            f"fees={r['fees']:.1f} fund={r['funding']:.1f} | IS {r['is']['ret']*100:.1f}% PF {r['is']['pf']:.2f} | OOS {r['oos']['ret']*100:.1f}% PF {r['oos']['pf']:.2f}"
        )
        for s, st in r["solo"].items():
            print(
                f"    {s:5s} n={st['trades']:3d} win={st['win']*100:5.1f}% ret={st['ret']*100:6.2f}% mdd={st['mdd']*100:5.2f}% pf={st['pf']:.2f} "
                f"avgR={st['avg_r']:.2f} gb={st['giveback']*100:.0f}%(n={st['gb_n']}) exp={st['exposure']*100:.0f}%"
            )
