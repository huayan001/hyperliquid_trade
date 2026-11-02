"""菲阿里实盘/纸面状态机。

第一次见到某个标的时，把游标快进到最新已收盘 1h，不追已经发生的突破。
之后只处理新收盘的 K 线：首破 YH/YL 进场，UTC 23:00 那根收盘平仓。
paper / shadow 在本地记账；live 是否下单由调用方决定，这里不碰交易所。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from hl_bot.phil_ali.rules import (
    H_MS,
    breakout_status,
    is_eod_bar,
    position_r,
    prior_day_levels,
    size_qty,
    utc_day,
)


@dataclass
class Decision:
    symbol: str
    action: str
    day: str = ""
    side: str = ""
    price: float = 0.0
    qty: float = 0.0
    r: float = 0.0
    bar_ts: int = 0
    reason: str = ""
    pnl_net: float = 0.0
    yh: float = 0.0
    yl: float = 0.0
    submitted: bool | None = None


@dataclass
class Book:
    equity: float
    risk_frac: float = 0.01
    fee_rt: float = 0.0005
    positions: dict[str, dict] = field(default_factory=dict)
    consumed: dict[str, str] = field(default_factory=dict)
    last_bar_ts: dict[str, int] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "equity": self.equity,
            "risk_frac": self.risk_frac,
            "fee_rt": self.fee_rt,
            "positions": self.positions,
            "consumed": self.consumed,
            "last_bar_ts": self.last_bar_ts,
            "trades": self.trades,
        }

    @classmethod
    def from_dict(cls, raw: dict, *, equity: float, risk_frac: float, fee_rt: float) -> Book:
        return cls(
            equity=float(raw.get("equity", equity)),
            risk_frac=float(raw.get("risk_frac", risk_frac)),
            fee_rt=float(raw.get("fee_rt", fee_rt)),
            positions=dict(raw.get("positions") or {}),
            consumed={str(k): str(v) for k, v in dict(raw.get("consumed") or {}).items()},
            last_bar_ts={str(k): int(v) for k, v in dict(raw.get("last_bar_ts") or {}).items()},
            trades=list(raw.get("trades") or []),
        )


def load_book(path: Path, *, equity: float, risk_frac: float, fee_rt: float) -> Book:
    if not path.exists():
        return Book(equity=equity, risk_frac=risk_frac, fee_rt=fee_rt)
    return Book.from_dict(json.loads(path.read_text()), equity=equity, risk_frac=risk_frac, fee_rt=fee_rt)


def save_book(path: Path, book: Book) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(book.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _closed(hourly: list[dict], now_ms: int) -> list[dict]:
    bars = [bar for bar in hourly if int(bar["t"]) + H_MS <= now_ms]
    bars.sort(key=lambda item: item["t"])
    return bars


def _pnl(side: str, qty: float, entry: float, exit_px: float, fee_rt: float) -> tuple[float, float, float]:
    gross = qty * (exit_px - entry) if side == "long" else qty * (entry - exit_px)
    fee = (fee_rt / 2.0) * (qty * entry + qty * exit_px)
    return gross, fee, gross - fee


def _open_position(book: Book, decision: Decision, levels: dict) -> None:
    book.positions[decision.symbol] = {
        "symbol": decision.symbol,
        "day": decision.day,
        "side": decision.side,
        "entry": decision.price,
        "entry_ts": decision.bar_ts,
        "qty": decision.qty,
        "r": decision.r,
        "yh": levels["yh"],
        "yl": levels["yl"],
        "yo": levels["yo"],
        "yc": levels["yc"],
    }


def _close_position(book: Book, symbol: str, exit_px: float, exit_ts: int) -> float:
    pos = book.positions.pop(symbol)
    _gross, _fee, net = _pnl(pos["side"], float(pos["qty"]), float(pos["entry"]), exit_px, book.fee_rt)
    before = book.equity
    book.equity = before + net
    book.trades.append(
        {
            "sym": symbol,
            "day": pos["day"],
            "side": pos["side"],
            "entry": pos["entry"],
            "exit": exit_px,
            "entry_ts": pos["entry_ts"],
            "exit_ts": exit_ts,
            "qty": pos["qty"],
            "r": pos["r"],
            "pnl_net": net,
            "equity_before": before,
            "equity_after": book.equity,
            "yo": pos.get("yo"),
            "yc": pos.get("yc"),
            "yh": pos.get("yh"),
            "yl": pos.get("yl"),
        }
    )
    return net


def apply_decision(book: Book, decision: Decision, levels: dict | None = None) -> None:
    """把已经决定的进场/出场记到本地账上。"""
    if decision.action == "enter":
        if levels is None:
            raise ValueError("进场记账需要昨日四价")
        _open_position(book, decision, levels)
        return
    if decision.action == "exit":
        decision.pnl_net = _close_position(book, decision.symbol, decision.price, decision.bar_ts)


def prime_symbol(book: Book, symbol: str, daily: list[dict], hourly: list[dict], now_ms: int) -> Decision | None:
    """首次启动：快进到最新已收盘 K，当天若已突破则标记为已消费。"""
    if symbol in book.last_bar_ts:
        return None
    closed = _closed(hourly, now_ms)
    if not closed:
        book.last_bar_ts[symbol] = 0
        return Decision(symbol=symbol, action="prime", reason="还没有已收盘的 1h K 线")
    latest = closed[-1]
    day = utc_day(int(latest["t"]))
    today_bars = [bar for bar in closed if utc_day(int(bar["t"])) == day]
    levels = prior_day_levels(daily, closed, day)
    reason = "已快进到当前 K 线，不追历史突破"
    if levels is not None:
        status, _side, _bar, _px = breakout_status(today_bars, levels["yh"], levels["yl"])
        if status in {"hit", "ambiguous"} or is_eod_bar(latest):
            book.consumed[symbol] = day
            reason = "启动时当日突破已经发生或已到日末，今天不再进场"
    elif is_eod_bar(latest):
        book.consumed[symbol] = day
    book.last_bar_ts[symbol] = int(latest["t"])
    return Decision(symbol=symbol, action="prime", day=day, bar_ts=int(latest["t"]), reason=reason)


def on_new_bars(
    book: Book,
    symbol: str,
    daily: list[dict],
    hourly: list[dict],
    now_ms: int,
    *,
    fill: bool,
) -> list[Decision]:
    """处理上次游标之后新收盘的 1h。fill=True 时立刻记入本地持仓和权益。"""
    primed = prime_symbol(book, symbol, daily, hourly, now_ms)
    if primed is not None:
        return [primed]

    closed = _closed(hourly, now_ms)
    cursor = int(book.last_bar_ts.get(symbol, 0))
    fresh = [bar for bar in closed if int(bar["t"]) > cursor]
    events: list[Decision] = []
    for bar in fresh:
        events.extend(_on_bar(book, symbol, daily, closed, bar, fill=fill))
        book.last_bar_ts[symbol] = int(bar["t"])
    return events


def _on_bar(
    book: Book,
    symbol: str,
    daily: list[dict],
    closed: list[dict],
    bar: dict,
    *,
    fill: bool,
) -> list[Decision]:
    day = utc_day(int(bar["t"]))
    events: list[Decision] = []
    pos = book.positions.get(symbol)
    if pos is not None and (day > str(pos["day"]) or (day == str(pos["day"]) and is_eod_bar(bar))):
        exit_px = float(bar["c"]) if day == str(pos["day"]) else _late_exit_price(pos, closed, bar)
        decision = Decision(
            symbol=symbol,
            action="exit",
            day=str(pos["day"]),
            side=str(pos["side"]),
            price=exit_px,
            qty=float(pos["qty"]),
            r=float(pos["r"]),
            bar_ts=int(bar["t"]),
            reason="UTC 日末平仓" if day == str(pos["day"]) else "跨日仍持仓，按错过的日末价平仓",
            yh=float(pos.get("yh") or 0.0),
            yl=float(pos.get("yl") or 0.0),
        )
        if fill:
            apply_decision(book, decision)
        events.append(decision)
        if day == str(pos["day"]):
            book.consumed[symbol] = day
            return events
        # 跨日平仓后，这根新 K 还可以当作新的一天来判断进场。
        if not fill:
            # 未记账时仓位还在，不能再当成空仓进场。
            return events

    if book.consumed.get(symbol) == day or symbol in book.positions:
        return events

    levels = prior_day_levels(daily, closed, day)
    today = [item for item in closed if utc_day(int(item["t"])) == day and int(item["t"]) <= int(bar["t"])]
    if levels is None:
        if is_eod_bar(bar):
            book.consumed[symbol] = day
        return events

    status, side, hit_bar, price = breakout_status(today, levels["yh"], levels["yl"])
    if status == "ambiguous":
        book.consumed[symbol] = day
        events.append(Decision(symbol=symbol, action="skip", day=day, bar_ts=int(bar["t"]), reason="同一根 1h 同时突破昨高和昨低，跳过"))
        return events
    if status == "none":
        if is_eod_bar(bar):
            book.consumed[symbol] = day
        return events
    assert hit_bar is not None
    if int(hit_bar["t"]) != int(bar["t"]):
        book.consumed[symbol] = day
        events.append(Decision(symbol=symbol, action="skip", day=day, bar_ts=int(bar["t"]), reason="首个突破不在这根 K 线上，不追单"))
        return events
    if is_eod_bar(bar):
        book.consumed[symbol] = day
        events.append(Decision(symbol=symbol, action="skip", day=day, bar_ts=int(bar["t"]), reason="日末那根 K 线不新开仓"))
        return events

    r = position_r(side, price, levels["yh"], levels["yl"])
    qty = size_qty(book.equity, book.risk_frac, r)
    book.consumed[symbol] = day
    if qty <= 0:
        events.append(Decision(symbol=symbol, action="skip", day=day, bar_ts=int(bar["t"]), reason="R 无效，跳过"))
        return events
    decision = Decision(
        symbol=symbol,
        action="enter",
        day=day,
        side=side,
        price=price,
        qty=qty,
        r=r,
        bar_ts=int(bar["t"]),
        reason=f"首破 {'昨高' if side == 'long' else '昨低'}",
        yh=levels["yh"],
        yl=levels["yl"],
    )
    if fill:
        apply_decision(book, decision, levels)
    events.append(decision)
    return events


def _late_exit_price(pos: dict, closed: list[dict], bar: dict) -> float:
    prev = [item for item in closed if utc_day(int(item["t"])) == str(pos["day"])]
    if prev:
        return float(prev[-1]["c"])
    return float(bar["o"])
