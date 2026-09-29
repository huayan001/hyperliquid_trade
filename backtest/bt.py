"""Hyperliquid 永续回测：V0 现行策略 vs V1/V2 短周期 + 分批止盈/保本/吊灯止损 vs V3 最小改动。

- 数据：Hyperliquid candleSnapshot（1h/4h/1d）+ fundingHistory（BTC/ETH 实际，SOL/HYPE 用 BTC 代理）
- 复用仓库 hl_bot 的指标与环境路由（EMA/ADX/ATR/Donchian/decide_from_metrics/detect_range_structure）
- 无前视：1h K 收盘时只用已收盘的 1d/4h/1h；信号在下一根 1h 开盘成交
- 费用：taker 0.045%，maker 0.015%；止损/市价额外滑点 0.02%
- 组合：共享权益，单笔风险 2%，10x 逐仓（止损距离 ≤5%）。旧版本 V0–V3 沿用最多 2 仓 / 组合风险 ≤4%；
  L0/V4 系列对齐实盘配置：趋势最多 3 仓、组合风险 ≤6%、BTC 空头风险 ×0.7、均值回归另计最多 2 仓
- V4：去掉 starter，4h Donchian 收盘突破入场；吊灯 + 1R 保本 + 利润锁定 + 1.5R/3R 分批 + 时间止损；可选接入均值回归
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 仓库布局：backtest/ 与 src/ 同级。放在别处运行时用 PYTHONPATH=<repo>/src 指定
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hl_bot.config import RegimeConfig  # noqa: E402
from hl_bot.indicators import adx, atr, bandwidth, body_exceeds_atr, bollinger, donchian, ema, rsi, vwap  # noqa: E402
from hl_bot.models import Candle, Regime  # noqa: E402
from hl_bot.regime import decide_from_metrics, detect_range_structure  # noqa: E402

# 数据集开关：默认 data/（Hyperliquid 近 ~6 个月）；BT_DATA=long → data_long/（Binance/OKX 2023-07 起，见 fetch_long.py）
LONG = os.getenv("BT_DATA", "").lower() == "long"
DATA = Path(__file__).parent / ("data_long" if LONG else "data")
SYMS = [x.strip().upper() for x in os.getenv("BT_SYMS", "BTC,ETH,SOL,HYPE").split(",") if x.strip()]
# 样本外比例：默认 40%（HL 数据），长数据 35%；BT_OOS_FRAC 可覆盖
OOS_FRAC = float(os.getenv("BT_OOS_FRAC", "0.35" if LONG else "0.40"))
TAG = ("_long" if LONG else "") + ("" if SYMS == ["BTC", "ETH", "SOL", "HYPE"] else "_" + "-".join(SYMS))
# 分段（北京时间标签，UTC 边界）：用于观察牛/熊/震荡阶段
PERIODS = [
    ("2023Q4-2024Q1", 1696118400000, 1711929600000),  # 2023-10-01 → 2024-04-01
    ("2024Q2-Q4", 1711929600000, 1735689600000),      # → 2025-01-01
    ("2025H1", 1735689600000, 1751328000000),         # → 2025-07-01
    ("2025H2", 1751328000000, 1767225600000),         # → 2026-01-01
    ("2026", 1767225600000, 1798761600000),           # → 2027-01-01
]
H = 3_600_000
FEE_T, FEE_M, SLIP = 0.00045, 0.00015, 0.0002
RISK, MAXPOS, PORT_RISK, LEV, MAX_STOP = 0.02, 2, 0.04, 10, 0.05
START_EQ = 1000.0
TEST_START = 1696118400000 if LONG else 1773964800000  # long: 2023-10-01 / HL: 2026-03-20 00:00 UTC（日线 EMA50/ADX 预热之后）
DEFAULT_FUNDING = 0.0000125  # 1h 基准费率（约 11% 年化）


def load(sym: str, iv: str) -> list[Candle]:
    raw = json.loads((DATA / f"{sym}_{iv}.json").read_text())
    out = [Candle(int(r["t"]), int(r["T"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r.get("v", 0) or 0)) for r in raw]
    return out[:-1]  # 丢掉最后一根（可能未收盘）


def load_funding(sym: str) -> dict[int, float]:
    f = DATA / f"{sym}_funding.json"
    if not f.exists():
        f = DATA / "BTC_funding.json"
    if not f.exists():  # 没有任何资金费数据：按默认费率
        return {}
    rows = json.loads(f.read_text())
    return {int(r["time"]) // H * H: float(r["fundingRate"]) for r in rows}


class Data:
    def __init__(self, sym: str) -> None:
        self.sym = sym
        self.h1, self.h4, self.d1 = load(sym, "1h"), load(sym, "4h"), load(sym, "1d")
        self.funding = load_funding(sym)
        self.funding_proxy = not (DATA / f"{sym}_funding.json").exists()
        self.funding_missing = not self.funding
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
        # 均值回归用的 1h 指标（与 hl_bot.strategies.mean_reversion 同参数）
        self.bb_u, self.bb_m, self.bb_l = bollinger(hc, 20, 2.0)
        self.bw = bandwidth(self.bb_u, self.bb_l, self.bb_m)
        self.h_rsi = rsi(hc, 14)
        self.h_vwap = vwap(h)
        cfg = RegimeConfig()
        self.rng: list = [None] * len(h)
        self.regime: list[Regime] = []
        for i in range(len(h)):
            di = self.d_at[i]
            ef, es, da, ha = (self.d_ema20[di], self.d_ema50[di], self.d_adx[di], self.h_adx[i]) if di >= 0 else (None,) * 4
            if None in (ef, es, da, ha) or i < 30:
                self.regime.append(Regime.WATCH)
                continue
            rng = detect_range_structure(h[max(0, i - 11) : i + 1], lookback_hours=cfg.range_lookback_hours, touch_frac=cfg.range_touch_frac)
            self.rng[i] = rng
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
    strat: str = "trend"  # trend / mr
    p2: bool = False  # 第二档分批已执行
    mr_partial: bool = False

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
    strat: str = "trend"


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
    # 组合规则（默认 = 旧回测口径；LIVE 预设 = 实盘配置）
    maxpos_trend: int = MAXPOS
    maxpos_mr: int = 2
    port_risk: float = PORT_RISK
    btc_short_mult: float = 1.0
    # V4 出场 / 入场开关
    starter: bool = True
    partial2_r: float | None = None
    partial2_frac: float = 0.5
    trail_atr_runner: float | None = None  # 第二档分批后改用更宽的吊灯
    lock_r: float | None = None  # 最高浮盈 ≥ lock_r·R 后启用利润锁定
    lock_frac: float = 0.5  # 止损不低于 均价 + lock_frac × 最高浮盈
    time_stop_h: float | None = None  # 持仓超过该小时数仍未到过 be_r·R 则离场
    mr_enabled: bool = False
    mr_max_day: int = 4
    mr_daily_halt: float = 0.03
    delever: bool = False
    cooldown_live: bool = False


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
        self.day_start_eq: dict[int, float] = {}
        self.day_trades: dict[tuple[str, int], int] = {}
        self.week_start_eq: dict[int, float] = {}

    # ---------------------------------------------------------------- helpers
    def equity(self, i_map: dict[str, int], use_close: bool = True) -> float:
        # FIX(lookahead): at fill time (bar open) mark open positions at the bar OPEN, not the not-yet-known close
        eq = self.cash
        for s, p in self.pos.items():
            i = i_map.get(s)
            if i is not None:
                c = self.D[s].h1[i]
                eq += p.unreal(c.close if use_close else c.open)
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
            self.trades.append(Trade(s, p.side, p.open_ts, ts, p.realized, p.risk_unit, p.peak, reason, i - p.open_i, p.strat))
            del self.pos[s]
            if self.p.cooldown_live:
                # 实盘口径 (runner.py _record_stop L812/L1192)：任何止损单成交（不论盈亏、不论策略）以及理由含「止损」的主动平仓（含 MR 时间止损）都记冷却
                trig = self.p.cooldown_4h > 0 and (reason.startswith("stop") or reason == "mr_time")
            else:
                trig = p.strat == "trend" and self.p.cooldown_4h > 0 and reason.startswith("stop") and p.realized < 0
            if trig:
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
            day = ts // (24 * H)
            if day not in self.day_start_eq:
                self.day_start_eq[day] = self.equity(i_map, use_close=False)
            wk = (day + 3) // 7  # UTC 周一为界
            if wk not in self.week_start_eq:
                self.week_start_eq[wk] = self.equity(i_map, use_close=False)
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
            if s in self.pos or sum(1 for q in self.pos.values() if q.strat == "trend") >= self.p.maxpos_trend:
                return
            eq = self.equity(i_map, use_close=False)
            px = o * (1 + SLIP * side)
            dist = min(dist, px * MAX_STOP)
            if dist <= 0:
                return
            rk = RISK * (self.p.btc_short_mult if (s == "BTC" and side < 0) else 1.0)
            full = eq * rk / dist
            size = full * frac * self._delever(eq, ts)
            # 杠杆上限
            size = min(size, eq * LEV * 0.9 / px)
            risk = size * dist
            if self.port_risk() + risk > eq * self.p.port_risk + 1e-9:
                return
            fee = self._fee(size * px, False)
            self.cash -= fee
            self.sym_realized[s] -= fee
            p = Pos(s, side, size, px, px - side * dist, dist, size, i, ts, eq * rk, realized=-fee, hh=px, orig_size=size, notional_max=size * px)
            p.peak = p.realized
            if extra.get("starter"):
                p.starter_pending = True
                p.intended_full = full
                p.intended_risk = full * dist
            else:
                p.tier_done = True
            self.pos[s] = p
            return
        if kind == "mr_partial":
            if s in self.pos:
                p = self.pos[s]
                if not p.mr_partial:
                    p.mr_partial = True
                    self._exit(s, p.size * 0.5, o, ts, "mr_partial", maker=True, i=i)
            return
        if kind == "mr_open":
            _, side, limit, stop = act
            c = d.h1[i]
            mc = self.p
            if s in self.pos or sum(1 for q in self.pos.values() if q.strat == "mr") >= mc.maxpos_mr:
                return
            day = ts // (24 * H)
            if self.day_trades.get((s, day), 0) >= self.p.mr_max_day:
                return
            eq = self.equity(i_map, use_close=False)
            start = self.day_start_eq.get(day, eq)
            if start > 0 and (start - eq) / start >= self.p.mr_daily_halt:
                return
            if side > 0:
                if c.low > limit:
                    return
                px = min(limit, c.open)
            else:
                if c.high < limit:
                    return
                px = max(limit, c.open)
            dist = abs(px - stop)
            if dist <= 0 or (side > 0 and stop >= px) or (side < 0 and stop <= px):
                return
            dist = min(dist, px * MAX_STOP)
            stop = px - side * dist
            size = min(eq * RISK / dist, eq * LEV * 0.9 / px)
            risk = size * dist
            if self.p.cooldown_live and self._cooling(s, side, ts):
                return
            if size * px < 10.0:
                return
            fee = self._fee(size * px, True)
            self.cash -= fee
            self.sym_realized[s] -= fee
            p = Pos(s, side, size, px, stop, dist, size, i, ts, eq * RISK, realized=-fee, hh=px, orig_size=size, notional_max=size * px, strat="mr")
            p.peak = p.realized
            p.tier_done = True
            self.pos[s] = p
            self.day_trades[(s, day)] = self.day_trades.get((s, day), 0) + 1
            return
        if kind == "add":
            if s not in self.pos:
                return
            p = self.pos[s]
            qty, mode = act[1], act[2]
            px = o * (1 + SLIP * p.side)
            eq = self.equity(i_map, use_close=False)
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
            init_stop = p.avg - p.side * p.r_dist
            if p.side * (p.stop - p.avg) >= 0:
                lab = "stop_be/trail+"  # 保本/吊灯/锁利止损（止损价已在均价之上）
            elif abs(p.stop - init_stop) > 1e-9 * max(1.0, abs(init_stop)):
                lab = "stop_trail-"  # 已上移但仍在均价下方
            else:
                lab = "stop_initial"
            self._exit(s, p.size, px, ts + H // 2, lab, i=i)
            return
        if p.strat != "trend":
            return
        if self.p.partial_r and not p.partial:
            tgt = p.avg + p.side * self.p.partial_r * p.r_dist
            if (p.side > 0 and c.high >= tgt) or (p.side < 0 and c.low <= tgt):
                p.partial = True
                self._exit(s, p.size * self.p.partial_frac, tgt, ts + H // 2, "partial", maker=True, i=i)
        if self.p.partial2_r and p.partial and not p.p2 and s in self.pos:
            tgt = p.avg + p.side * self.p.partial2_r * p.r_dist
            if (p.side > 0 and c.high >= tgt) or (p.side < 0 and c.low <= tgt):
                p.p2 = True
                self._exit(s, p.size * self.p.partial2_frac, tgt, ts + H // 2, "partial2", maker=True, i=i)

    # ---------------------------------------------------------------- strategy
    def _on_close(self, s: str, i: int, ts: int, eq: float) -> None:
        p = self.pos.get(s)
        if p is not None and p.strat == "mr":
            self._mr_manage(s, i, ts)
            return
        if self.p.kind in ("v0", "v3", "v4"):
            self._v0(s, i, ts, eq)
        else:
            self._v1(s, i, ts, eq)
        if self.p.mr_enabled and s not in self.pos and s not in self.pending:
            self._mr_signal(s, i, ts)

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
            elif p.tier_done and p.pyramid_frac < 0.5 - 1e-9 and d.trend_confirmed(i, p.side) and (self.p.kind != "v4" or p.be):
                if p.side * (px - p.avg) >= 1.0 * atr4:
                    self.pending[s] = ("add", p.orig_size * (0.5 - p.pyramid_frac), "pyramid", atr4)
            if self.p.kind == "v0":
                fav = max(0.0, p.side * (px - p.avg))
                steps = int(fav / atr4)
                cand = (p.avg - p.side * 2.0 * atr4) + p.side * steps * 0.5 * atr4
                p.stop = max(p.stop, cand) if p.side > 0 else min(p.stop, cand)
            elif self.p.kind == "v4":
                # 吊灯 + 1R 保本 + 利润锁定 + 时间止损；第二档分批后放宽吊灯，留给单边行情
                ta = self.p.trail_atr_runner if (p.p2 and self.p.trail_atr_runner) else self.p.trail_atr
                ch = p.hh - p.side * ta * atr4
                if p.side * (px - p.avg) >= self.p.be_r * p.r_dist:
                    p.be = True
                cand = ch
                if p.be:
                    be = p.avg * (1 + 0.001 * p.side)
                    cand = max(ch, be) if p.side > 0 else min(ch, be)
                peak_fav = p.side * (p.hh - p.avg)
                if self.p.lock_r and peak_fav >= self.p.lock_r * p.r_dist:
                    lock = p.avg + p.side * self.p.lock_frac * peak_fav
                    if p.side * (px - lock) <= 0:  # 收盘已回吐到锁定线以下：下一根开盘离场
                        self.pending[s] = ("close", "lock")
                        return
                    cand = max(cand, lock) if p.side > 0 else min(cand, lock)
                if p.side * (px - cand) > 0:
                    p.stop = max(p.stop, cand) if p.side > 0 else min(p.stop, cand)
                held_h = (ts + H - p.open_ts) / H
                if self.p.time_stop_h and held_h >= self.p.time_stop_h and peak_fav < self.p.be_r * p.r_dist:
                    self.pending[s] = ("close", "time")
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
        elif self.p.starter:
            self.pending[s] = ("open", side, 2.0 * atr4, 0.35, {"starter": True})

    # ---------------------------------------------------------------- mean reversion（对齐 strategies/mean_reversion.py）
    def _mr_halt(self, d: Data, i: int) -> bool:
        c = d.h1[i]
        hadx = d.h_adx[i]
        if hadx is not None and hadx > 25.0:
            return True
        rng = d.rng[i]
        if rng is not None and rng.high > rng.low and (c.close > rng.high or c.close < rng.low):
            return True
        u, l, a = d.bb_u[i], d.bb_l[i], d.h_atr[i]
        away = (u is not None and c.close > u) or (l is not None and c.close < l)
        if rng is not None and rng.high > rng.low and (c.close > rng.high or c.close < rng.low):
            away = True
        bw_v = d.bw[i]
        recent = [x for x in d.bw[max(0, i - 19) : i + 1] if x is not None]
        if away and a and body_exceeds_atr(c, a, 1.5) and bw_v is not None and recent:
            ranked = sorted(recent)
            zone = ranked[min(len(ranked) - 1, int(len(ranked) * 0.80))]
            if bw_v >= zone or bw_v >= max(recent) * 0.9:
                return True
        return False

    def _mr_signal(self, s: str, i: int, ts: int) -> None:
        d = self.D[s]
        if d.regime[i] is not Regime.MEAN_REVERSION or i < 26:
            return
        if self._mr_halt(d, i):
            return
        last, prev = d.h1[i], d.h1[i - 1]
        u, m, l = d.bb_u[i], d.bb_m[i], d.bb_l[i]
        rl, rp, a = d.h_rsi[i], d.h_rsi[i - 1], d.h_atr[i]
        if None in (u, m, l, rl, a):
            return
        rng = d.rng[i]

        def stop_for(side: int, entry: float) -> float:
            atr_stop = entry - side * 1.0 * a
            if rng is not None and rng.high > rng.low:
                buf = (rng.high - rng.low) * 0.02
                ext = rng.low - buf if side > 0 else rng.high + buf
                return max(atr_stop, ext) if side > 0 else min(atr_stop, ext)
            return atr_stop

        lp = d.bb_l[i - 1] if d.bb_l[i - 1] is not None else l
        up = d.bb_u[i - 1] if d.bb_u[i - 1] is not None else u
        touch_low = prev.low <= lp or prev.close <= lp
        oversold = (rp is not None and rp < 30.0) or rl < 30.0
        if touch_low and oversold and (last.close > l or last.is_hammer()):
            entry = max(l, last.low) * 1.0001
            self.pending[s] = ("mr_open", 1, entry, stop_for(1, entry))
            return
        touch_high = prev.high >= up or prev.close >= up
        overbought = (rp is not None and rp > 70.0) or rl > 70.0
        if touch_high and overbought and (last.close < u or last.is_shooting_star()):
            entry = min(u, last.high) * 0.9999
            self.pending[s] = ("mr_open", -1, entry, stop_for(-1, entry))

    def _mr_manage(self, s: str, i: int, ts: int) -> None:
        d = self.D[s]
        p = self.pos[s]
        c = d.h1[i]
        px = c.close
        a = d.h_atr[i]
        if self._mr_halt(d, i):
            self.pending[s] = ("close", "mr_env")
            return
        if a and body_exceeds_atr(c, a, 1.5):
            self.pending[s] = ("close", "mr_body")
            return
        if (ts + H - p.open_ts) / H >= 5.0:
            self.pending[s] = ("close", "mr_time")
            return
        mean = d.h_vwap[i] if d.h_vwap[i] is not None else d.bb_m[i]
        if mean is not None and not p.mr_partial and (px >= mean if p.side > 0 else px <= mean):
            self.pending[s] = ("mr_partial",)
            return
        opp = d.bb_u[i] if p.side > 0 else d.bb_l[i]
        rv = d.h_rsi[i]
        rsi_ext = rv is not None and (rv >= 70.0 if p.side > 0 else rv <= 30.0)
        if (opp is not None and (px >= opp if p.side > 0 else px <= opp)) or rsi_ext:
            self.pending[s] = ("close", "mr_target")

    def _delever(self, eq: float, ts: int) -> float:
        if not self.p.delever:
            return 1.0
        day = ts // (24 * H)
        sc = 1.0
        d0 = self.day_start_eq.get(day, eq)
        w0 = self.week_start_eq.get((day + 3) // 7, eq)
        if d0 > 0 and (d0 - eq) / d0 >= 0.05:
            sc *= 0.5
        if w0 > 0 and (w0 - eq) / w0 >= 0.10:
            sc *= 0.5
        return sc

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

# 对齐实盘配置（config.py）：趋势最多 3 仓、组合止损风险 ≤6%、BTC 空头风险 ×0.7、均值回归另计最多 2 仓；
# 止损后冷却 2 根 4h 已是实盘默认值。未建模：日/周回撤降杠杆、资金费逆向减仓、maker 挂单可能不成交、追突破市价。
LIVE_CC = dict(maxpos_trend=3, maxpos_mr=2, port_risk=0.06, btc_short_mult=0.7, cooldown_4h=2)  # cc 原口径（dataclass 默认值）
# FIX：实盘进程实际加载 config.toml（L39-40 max_positions=2 / portfolio_risk_max=0.04；[mean_reversion] L77-79 max_positions=1、
# max_trades_per_symbol_day=3、daily_loss_halt=0.02），dataclass 默认值 3/6%/2/4/3% 被覆盖；另外建模日 5%/周 10% 降杠杆与实盘冷却口径
LIVE = dict(maxpos_trend=2, maxpos_mr=1, port_risk=0.04, btc_short_mult=0.7, cooldown_4h=2,
            mr_max_day=3, mr_daily_halt=0.02, delever=True, cooldown_live=True)
# V4 参数全部事先定好，不做寻优
V4 = dict(
    kind="v4", starter=False, be_r=1.0, trail_atr=2.0, trail_atr_runner=3.0,
    partial_r=1.5, partial_frac=1 / 3, partial2_r=3.0, partial2_frac=0.5,
    lock_r=1.5, lock_frac=0.5, time_stop_h=48.0,
)
VERSIONS += [
    Params("L0 实盘趋势", "v0", partial_r=None, **LIVE),
    Params("L0+MR 实盘现状", "v0", partial_r=None, mr_enabled=True, **LIVE),
    Params("V4 趋势", **V4, **LIVE),
    Params("V4+MR", **V4, mr_enabled=True, **LIVE),
    Params("L0 cc口径(3仓/6%)", "v0", partial_r=None, mr_enabled=True, **LIVE_CC),
    Params("V4 cc口径(3仓/6%)", **V4, mr_enabled=True, **LIVE_CC),
]


def main() -> dict:
    data = {s: Data(s) for s in SYMS}
    timeline = sorted(set().union(*[set(d.idx) for d in data.values()]))
    timeline = [t for t in timeline if all(t in d.idx for d in data.values()) or t >= TEST_START]
    test_ts = [t + H for t in timeline if t >= TEST_START]
    split_ts = test_ts[int(len(test_ts) * (1 - OOS_FRAC))]
    ref = "BTC" if "BTC" in data else SYMS[0]
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
            "by_strat": {k: trade_stats([t for t in sim.trades if t.strat == k]) for k in ("trend", "mr")},
            "exits": {},
            "per": {},
            "is": {},
            "oos": {},
        }
        for t in sim.trades:
            res["exits"][t.exit_reason] = res["exits"].get(t.exit_reason, 0) + 1
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
        # 分段表现：收益/回撤按资金曲线，交易按开仓时间归属
        res["periods"] = {}
        for lab, a, b in PERIODS:
            cv = [(t, v) for t, v in sim.curve if a < t <= b]
            if len(cv) < 24:
                continue
            prev = [v for t, v in sim.curve if t <= a]
            base = prev[-1] if prev else START_EQ
            st = trade_stats([t for t in sim.trades if a <= t.open_ts < b])
            st["ret"] = cv[-1][1] / base - 1
            st["mdd"] = max_dd([v for _, v in cv], base)
            rc = [c for c in data[ref].h1 if a <= c.ts < b and c.ts >= TEST_START]
            st["ref_chg"] = rc[-1].close / rc[0].open - 1 if rc else float("nan")
            st["span"] = (cv[0][0], cv[-1][0])
            res["periods"][lab] = st
        # IS/OOS：交易按「开仓时间」归属（跨越分割点的交易算样本内）；收益按资金曲线分段
        is_curve = [v for t, v in sim.curve if t <= split_ts]
        oos_curve = [v for t, v in sim.curve if t > split_ts]
        for key, tr, cv, base in (
            ("is", [t for t in sim.trades if t.open_ts < split_ts], is_curve, START_EQ),
            ("oos", [t for t in sim.trades if t.open_ts >= split_ts], oos_curve, is_curve[-1]),
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
        "long": LONG,
        "syms": SYMS,
        "oos_frac": OOS_FRAC,
        "ref": ref,
        "sym_start": {s: data[s].h1[0].ts for s in SYMS},
    }
    return results


if __name__ == "__main__":
    import pickle

    out = main()
    (Path(__file__).parent / f"results{TAG}.pkl").write_bytes(pickle.dumps(out))
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
        for k, st in r["by_strat"].items():
            if st["trades"]:
                print(f"    [{k:5s}] n={st['trades']:3d} pnl=${st['pnl']:8.1f} win={st['win']*100:5.1f}% pf={st['pf']:.2f} avgR={st['avg_r']:.2f}")
        print("    exits:", dict(sorted(r["exits"].items(), key=lambda kv: -kv[1])))
        for s, st in r["solo"].items():
            print(
                f"    {s:5s} n={st['trades']:3d} win={st['win']*100:5.1f}% ret={st['ret']*100:6.2f}% mdd={st['mdd']*100:5.2f}% pf={st['pf']:.2f} "
                f"avgR={st['avg_r']:.2f} gb={st['giveback']*100:.0f}%(n={st['gb_n']}) exp={st['exposure']*100:.0f}%"
            )
