"""菲阿里四价 — 纯规则（无开收偏向、无 1.5R 止盈，仅突破 YH/YL + UTC 日末平仓）。

四价（昨）：YO=昨开, YC=昨收, YH=昨高, YL=昨低。入场只用 YH/YL；YO/YC 记在成交里备查。
- 多：首根已收盘 1h 的 high > YH → 入场；空：首根 low < YL → 入场
- 同日最多 1 笔；先发生者优先；同一根既破 YH 又破 YL → 整天跳过
- 出场：仅 UTC 日末（回测用当日最后一根收盘；实盘用 23:00 那根 1h 收盘）
- 仓位 R：多 = entry−YL；空 = YH−entry；R<=0 跳过
- 风险：权益的 1% / R。费用默认往返 0.05%（另可报 0.08%）；日末平仓，funding≈0

只服务加密货币。调用方不得传入黄金标的。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from hl_bot.phil_ali.gate import BANNED_SYMBOLS, assert_crypto_symbol

H_MS = 3_600_000
START_EQ = 10_000.0
RISK_FRAC = 0.01
FEE_RT_BASE = 0.0005
FEE_RT_ALT = 0.0008
CRYPTO_SYMBOLS = ("BTC", "ETH", "SOL")

ExitReason = Literal["eod", "skipped"]
BreakStatus = Literal["none", "ambiguous", "hit"]


@dataclass
class Trade:
    sym: str
    day: str
    side: str
    yo: float
    yc: float
    yh: float
    yl: float
    entry_ts: int
    entry: float
    r: float
    exit_ts: int
    exit: float
    reason: ExitReason
    qty: float
    pnl_gross: float
    fee: float
    pnl_net: float
    equity_before: float
    equity_after: float
    split: str


def load_ohlc(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    return [
        {
            "t": int(row["t"]),
            "o": float(row["o"]),
            "h": float(row["h"]),
            "l": float(row["l"]),
            "c": float(row["c"]),
        }
        for row in raw
    ]


def utc_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def is_eod_bar(bar: dict) -> bool:
    """UTC 23:00 开盘的那根 1h，收盘即自然日结束。"""
    return datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc).hour == 23


def entry_fill_long(bar: dict, yh: float) -> float:
    """突破昨高：开盘已在 YH 上方按开盘成交，否则按 YH 成交。"""
    return bar["o"] if bar["o"] > yh else yh


def entry_fill_short(bar: dict, yl: float) -> float:
    return bar["o"] if bar["o"] < yl else yl


def breakout_status(bars: list[dict], yh: float, yl: float) -> tuple[BreakStatus, str, dict | None, float]:
    """扫描到第一根触及为止。

    返回 (status, side, bar, price)。
    ambiguous：第一根有方向的 K 同时破了 YH 和 YL，整天不作数。
    hit：第一根单边突破。
    none：到目前还没破。
    """
    for bar in bars:
        up = bar["h"] > yh
        down = bar["l"] < yl
        if up and down:
            return "ambiguous", "", bar, 0.0
        if up:
            return "hit", "long", bar, entry_fill_long(bar, yh)
        if down:
            return "hit", "short", bar, entry_fill_short(bar, yl)
    return "none", "", None, 0.0


def find_first_breakout(bars: list[dict], yh: float, yl: float):
    """(side, entry_bar, entry_px) 或 None。同根双破视为整天没有信号。"""
    status, side, bar, price = breakout_status(bars, yh, yl)
    if status != "hit" or bar is None:
        return None
    return side, bar, price


def _check_symbol(sym: str) -> str:
    cleaned = assert_crypto_symbol(sym)
    if cleaned in BANNED_SYMBOLS:
        raise ValueError(f"菲阿里只做加密货币永续，不含黄金：{cleaned}")
    return cleaned


def simulate_symbol(
    sym: str,
    daily: list[dict],
    hourly: list[dict],
    fee_rt: float = FEE_RT_BASE,
    start_eq: float = START_EQ,
    risk_frac: float = RISK_FRAC,
    split_ts: int | None = None,
) -> tuple[list[Trade], list[tuple[int, float]]]:
    sym = _check_symbol(sym)
    side_fee = fee_rt / 2.0
    by_day: dict[str, list[dict]] = {}
    for bar in hourly:
        by_day.setdefault(utc_day(bar["t"]), []).append(bar)
    for bars in by_day.values():
        bars.sort(key=lambda item: item["t"])

    equity = start_eq
    trades: list[Trade] = []
    curve: list[tuple[int, float]] = [(hourly[0]["t"], equity)] if hourly else []

    for i in range(1, len(daily)):
        prev, today = daily[i - 1], daily[i]
        day = utc_day(today["t"])
        yo, yc, yh, yl = prev["o"], prev["c"], prev["h"], prev["l"]
        bars = by_day.get(day, [])
        if len(bars) < 2:
            continue

        hit = find_first_breakout(bars, yh, yl)
        if hit is None:
            continue
        side, entry_bar, entry = hit
        if entry_bar is bars[-1]:
            continue

        r = (entry - yl) if side == "long" else (yh - entry)
        if r <= 0:
            continue
        risk_cash = equity * risk_frac
        qty = risk_cash / r
        if qty <= 0 or qty != qty:
            continue

        last = bars[-1]
        exit_px, exit_ts = last["c"], last["t"]
        if side == "long":
            pnl_gross = qty * (exit_px - entry)
        else:
            pnl_gross = qty * (entry - exit_px)
        fee = side_fee * (qty * entry + qty * exit_px)
        pnl_net = pnl_gross - fee
        split = "OOS" if split_ts is not None and entry_bar["t"] >= split_ts else "IS"
        eq_before = equity
        equity = equity + pnl_net
        trades.append(
            Trade(
                sym=sym,
                day=day,
                side=side,
                yo=yo,
                yc=yc,
                yh=yh,
                yl=yl,
                entry_ts=entry_bar["t"],
                entry=entry,
                r=r,
                exit_ts=exit_ts,
                exit=exit_px,
                reason="eod",
                qty=qty,
                pnl_gross=pnl_gross,
                fee=fee,
                pnl_net=pnl_net,
                equity_before=eq_before,
                equity_after=equity,
                split=split,
            )
        )
        curve.append((exit_ts, equity))

    return trades, curve


def simulate_portfolio(
    symbols_data: dict[str, tuple[list[dict], list[dict]]],
    fee_rt: float = FEE_RT_BASE,
    start_eq: float = START_EQ,
    risk_frac: float = RISK_FRAC,
    split_ts: int | None = None,
) -> tuple[list[Trade], list[tuple[int, float]]]:
    """共享权益。同一天的进场都按当日第一笔之前的权益计算，出场再入账。"""
    for sym in symbols_data:
        _check_symbol(sym)
    side_fee = fee_rt / 2.0
    by_sym_day: dict[str, dict[str, list[dict]]] = {}
    day_levels: dict[str, dict[str, tuple[dict, dict]]] = {sym: {} for sym in symbols_data}
    all_days: set[str] = set()

    for sym, (daily, hourly) in symbols_data.items():
        grouped: dict[str, list[dict]] = {}
        for bar in hourly:
            grouped.setdefault(utc_day(bar["t"]), []).append(bar)
        for bars in grouped.values():
            bars.sort(key=lambda item: item["t"])
        by_sym_day[sym] = grouped
        for i in range(1, len(daily)):
            day = utc_day(daily[i]["t"])
            day_levels[sym][day] = (daily[i - 1], daily[i])
            all_days.add(day)

    equity = start_eq
    trades: list[Trade] = []
    first_ts = min(symbols_data[sym][1][0]["t"] for sym in symbols_data if symbols_data[sym][1])
    curve: list[tuple[int, float]] = [(first_ts, equity)]

    for day in sorted(all_days):
        candidates = []
        for sym in symbols_data:
            levels = day_levels[sym].get(day)
            if not levels:
                continue
            prev, _today = levels
            yo, yc, yh, yl = prev["o"], prev["c"], prev["h"], prev["l"]
            bars = by_sym_day[sym].get(day, [])
            if len(bars) < 2:
                continue
            hit = find_first_breakout(bars, yh, yl)
            if hit is None:
                continue
            side, entry_bar, entry = hit
            if entry_bar is bars[-1]:
                continue
            r = (entry - yl) if side == "long" else (yh - entry)
            if r <= 0:
                continue
            candidates.append((entry_bar["t"], sym, side, entry, r, yh, yl, yo, yc, bars))

        candidates.sort(key=lambda item: (item[0], item[1]))
        open_meta = []
        seen: set[str] = set()
        for entry_ts, sym, side, entry, r, yh, yl, yo, yc, bars in candidates:
            if sym in seen:
                continue
            seen.add(sym)
            qty = (equity * risk_frac) / r
            if qty <= 0:
                continue
            last = bars[-1]
            exit_px, exit_ts = last["c"], last["t"]
            if side == "long":
                pnl_gross = qty * (exit_px - entry)
            else:
                pnl_gross = qty * (entry - exit_px)
            fee = side_fee * (qty * entry + qty * exit_px)
            pnl_net = pnl_gross - fee
            # 同一天后发生的进场仍用这一天开始时的权益（出场尚未入账）。
            open_meta.append(
                {
                    "sym": sym,
                    "side": side,
                    "entry": entry,
                    "r": r,
                    "yh": yh,
                    "yl": yl,
                    "yo": yo,
                    "yc": yc,
                    "qty": qty,
                    "entry_ts": entry_ts,
                    "exit_ts": exit_ts,
                    "exit": exit_px,
                    "pnl_gross": pnl_gross,
                    "fee": fee,
                    "pnl_net": pnl_net,
                }
            )

        open_meta.sort(key=lambda item: (item["exit_ts"], item["sym"]))
        for meta in open_meta:
            split = "OOS" if split_ts is not None and meta["entry_ts"] >= split_ts else "IS"
            eq_before = equity
            equity = equity + meta["pnl_net"]
            trades.append(
                Trade(
                    sym=meta["sym"],
                    day=day,
                    side=meta["side"],
                    yo=meta["yo"],
                    yc=meta["yc"],
                    yh=meta["yh"],
                    yl=meta["yl"],
                    entry_ts=meta["entry_ts"],
                    entry=meta["entry"],
                    r=meta["r"],
                    exit_ts=meta["exit_ts"],
                    exit=meta["exit"],
                    reason="eod",
                    qty=meta["qty"],
                    pnl_gross=meta["pnl_gross"],
                    fee=meta["fee"],
                    pnl_net=meta["pnl_net"],
                    equity_before=eq_before,
                    equity_after=equity,
                    split=split,
                )
            )
            curve.append((meta["exit_ts"], equity))

    return trades, curve


def stats(trades: list[Trade]) -> dict:
    if not trades:
        return {
            "n": 0,
            "wr": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "pf": 0.0,
            "net": 0.0,
            "gross": 0.0,
            "fees": 0.0,
            "max_dd": 0.0,
            "end_eq": START_EQ,
            "avg_r": 0.0,
            "long_n": 0,
            "short_n": 0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "best_6m_net": 0.0,
            "best_6m_ret": 0.0,
        }
    wins = [trade for trade in trades if trade.pnl_net > 0]
    losses = [trade for trade in trades if trade.pnl_net <= 0]
    n = len(trades)
    wr = len(wins) / n
    avg_win = sum(trade.pnl_net for trade in wins) / len(wins) if wins else 0.0
    avg_loss = sum(trade.pnl_net for trade in losses) / len(losses) if losses else 0.0
    expectancy = wr * avg_win + (1 - wr) * avg_loss
    gross_profit = sum(trade.pnl_net for trade in wins)
    gross_loss = -sum(trade.pnl_net for trade in losses)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0
    net = sum(trade.pnl_net for trade in trades)
    fees = sum(trade.fee for trade in trades)
    gross = sum(trade.pnl_gross for trade in trades)

    equity = trades[0].equity_before
    peak = equity
    max_dd = 0.0
    for trade in trades:
        equity = trade.equity_after
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    r_mults = []
    for trade in trades:
        if trade.r <= 0:
            continue
        if trade.side == "long":
            r_mults.append((trade.exit - trade.entry) / trade.r)
        else:
            r_mults.append((trade.entry - trade.exit) / trade.r)

    best_6m_net = 0.0
    best_6m_ret = 0.0
    window_ms = 182 * 86_400_000
    for i, first in enumerate(trades):
        end = first.entry_ts + window_ms
        window = []
        j = i
        while j < len(trades) and trades[j].entry_ts < end:
            window.append(trades[j])
            j += 1
        if not window:
            continue
        window_net = sum(trade.pnl_net for trade in window)
        ret = window_net / window[0].equity_before if window[0].equity_before > 0 else 0.0
        if window_net > best_6m_net:
            best_6m_net = window_net
        if ret > best_6m_ret:
            best_6m_ret = ret

    return {
        "n": n,
        "wr": wr,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "pf": profit_factor,
        "net": net,
        "gross": gross,
        "fees": fees,
        "max_dd": max_dd,
        "end_eq": trades[-1].equity_after,
        "avg_r": sum(r_mults) / len(r_mults) if r_mults else 0.0,
        "long_n": sum(1 for trade in trades if trade.side == "long"),
        "short_n": sum(1 for trade in trades if trade.side == "short"),
        "best_trade": max(trade.pnl_net for trade in trades),
        "worst_trade": min(trade.pnl_net for trade in trades),
        "best_6m_net": best_6m_net,
        "best_6m_ret": best_6m_ret,
    }


def trades_to_rows(trades: list[Trade]) -> list[dict]:
    return [asdict(trade) for trade in trades]


def prior_day_levels(daily: list[dict], hourly: list[dict], day: str) -> dict | None:
    """`day` 的昨开/昨收/昨高/昨低。优先用日线，否则用昨日 1h 聚合。"""
    prev = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    prev_day = datetime.fromtimestamp(prev.timestamp() - 86_400, tz=timezone.utc).strftime("%Y-%m-%d")
    for bar in daily:
        if utc_day(int(bar["t"])) == prev_day:
            return {"yo": float(bar["o"]), "yc": float(bar["c"]), "yh": float(bar["h"]), "yl": float(bar["l"])}
    bars = [bar for bar in hourly if utc_day(int(bar["t"])) == prev_day]
    if not bars:
        return None
    bars.sort(key=lambda item: item["t"])
    return {
        "yo": float(bars[0]["o"]),
        "yc": float(bars[-1]["c"]),
        "yh": max(float(bar["h"]) for bar in bars),
        "yl": min(float(bar["l"]) for bar in bars),
    }


def position_r(side: str, entry: float, yh: float, yl: float) -> float:
    return (entry - yl) if side == "long" else (yh - entry)


def size_qty(equity: float, risk_frac: float, r: float) -> float:
    if r <= 0 or equity <= 0 or risk_frac <= 0:
        return 0.0
    qty = (equity * risk_frac) / r
    if qty <= 0 or qty != qty:
        return 0.0
    return qty
