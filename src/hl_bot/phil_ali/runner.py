"""菲阿里独立循环。不读取、不写入趋势机器人的 paper_state，也不从趋势扫描里被调用。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from hl_bot.models import IntentAction, OrderIntent, OrderKind, Side, StrategyName
from hl_bot.phil_ali.gate import PhilAliGate, live_orders_allowed
from hl_bot.phil_ali.rules import H_MS, prior_day_levels, utc_day
from hl_bot.phil_ali.session import Book, Decision, _on_bar, apply_decision, load_book, on_new_bars, prime_symbol, save_book

logger = logging.getLogger("hl_bot.phil_ali")


def book_path(gate: PhilAliGate) -> Path:
    # live 未解锁时也用 live.json 记录「今天已看过」，避免解锁后回头追单。
    name = gate.mode if gate.mode in {"shadow", "paper", "live"} else "off"
    return Path(gate.state_dir) / f"{name}.json"


def shadow_log_path(gate: PhilAliGate) -> Path:
    return Path(gate.state_dir) / "shadow.jsonl"


def run_cycle(
    book: Book,
    markets: dict[str, tuple[list[dict], list[dict]]],
    now_ms: int,
    gate: PhilAliGate,
    submit=None,
) -> list[Decision]:
    """走一遍每个标的的新 K 线。

    shadow / paper：本地记账，submit 被忽略。
    live 且五道闸没齐：只推进游标和「当日已消费」，不建仓、不调用 submit。
    live 且允许下单：submit(intent) 返回 status=ok 才记入本地持仓。
    """
    events: list[Decision] = []
    armed = live_orders_allowed(gate)
    fill_local = gate.mode in {"shadow", "paper"}
    for symbol in gate.symbols:
        if symbol not in markets:
            events.append(Decision(symbol=symbol, action="skip", reason="没有行情"))
            continue
        daily, hourly = markets[symbol]
        if gate.mode == "live" and not armed:
            fresh = on_new_bars(book, symbol, daily, hourly, now_ms, fill=False)
            for event in fresh:
                if event.action in {"enter", "exit"}:
                    event.submitted = False
                    event.reason = f"{event.reason}；实盘下单未解锁，未发送"
            events.extend(fresh)
            continue
        if gate.mode == "live" and armed:
            events.extend(_live_symbol(book, symbol, daily, hourly, now_ms, gate, submit))
            continue
        events.extend(on_new_bars(book, symbol, daily, hourly, now_ms, fill=fill_local))
    if gate.mode == "shadow":
        _append_shadow(gate, events)
    if gate.mode in {"shadow", "paper", "live"}:
        save_book(book_path(gate), book)
    return events


def _live_symbol(book, symbol, daily, hourly, now_ms, gate: PhilAliGate, submit) -> list[Decision]:
    """逐根 K 线：先问状态机要不要交易，下单成功才记账。"""
    primed = prime_symbol(book, symbol, daily, hourly, now_ms)
    if primed is not None:
        return [primed]

    closed = [bar for bar in hourly if int(bar["t"]) + H_MS <= now_ms]
    closed.sort(key=lambda item: item["t"])
    cursor = int(book.last_bar_ts.get(symbol, 0))
    fresh = [bar for bar in closed if int(bar["t"]) > cursor]
    events: list[Decision] = []
    for bar in fresh:
        before_pos = dict(book.positions.get(symbol) or {})
        # fill=False 只推进「这一天已经看过」，成功下单后再记持仓。
        decisions = _on_bar(book, symbol, daily, closed, bar, fill=False)
        book.last_bar_ts[symbol] = int(bar["t"])
        for decision in decisions:
            if decision.action not in {"enter", "exit"}:
                events.append(decision)
                continue
            intent = decision_to_intent(decision, gate.leverage)
            result = submit(intent) if submit is not None else {"status": "blocked", "reason": "没有提交函数"}
            ok = isinstance(result, dict) and result.get("status") == "ok"
            decision.submitted = ok
            if ok and decision.action == "enter":
                levels = prior_day_levels(daily, closed, utc_day(int(bar["t"])))
                apply_decision(book, decision, levels)
            elif ok and symbol in book.positions:
                apply_decision(book, decision)
            elif not ok:
                decision.reason = f"{decision.reason}；下单未确认 {result!r}"
                if decision.action == "exit" and before_pos and symbol not in book.positions:
                    book.positions[symbol] = before_pos
            events.append(decision)
    return events


def decision_to_intent(decision: Decision, leverage: int) -> OrderIntent:
    """纯规则没有盘中止损，stop_price 保持为空，避免交易所挂保护止损。"""
    if decision.action == "enter":
        side = Side.LONG if decision.side == "long" else Side.SHORT
        return OrderIntent(
            action=IntentAction.OPEN,
            symbol=decision.symbol,
            strategy=StrategyName.PHIL_ALI,
            side=side,
            kind=OrderKind.MARKET,
            size=decision.qty,
            price=decision.price,
            stop_price=None,
            leverage=leverage,
            isolated=True,
            reduce_only=False,
            reason=decision.reason,
            risk_usd=decision.qty * decision.r,
            notional_usd=decision.qty * decision.price,
        )
    pos_side = Side.LONG if decision.side == "long" else Side.SHORT
    return OrderIntent(
        action=IntentAction.CLOSE,
        symbol=decision.symbol,
        strategy=StrategyName.PHIL_ALI,
        side=pos_side.opposite(),
        kind=OrderKind.MARKET,
        size=decision.qty,
        price=decision.price,
        stop_price=None,
        leverage=leverage,
        isolated=True,
        reduce_only=True,
        reason=decision.reason,
        notional_usd=decision.qty * decision.price,
    )


def _append_shadow(gate: PhilAliGate, events: list[Decision]) -> None:
    path = shadow_log_path(gate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            if event.action in {"prime", "wait", "hold"}:
                continue
            handle.write(
                json.dumps(
                    {
                        "action": event.action,
                        "symbol": event.symbol,
                        "day": event.day,
                        "side": event.side,
                        "price": event.price,
                        "qty": event.qty,
                        "r": event.r,
                        "bar_ts": event.bar_ts,
                        "reason": event.reason,
                        "pnl_net": event.pnl_net,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def format_events(events: list[Decision]) -> str:
    if not events:
        return "菲阿里：没有新动作"
    lines = []
    for event in events:
        extra = ""
        if event.action == "enter":
            extra = f" {event.side} px={event.price:.6g} qty={event.qty:.6g} R={event.r:.6g}"
        elif event.action == "exit":
            extra = f" {event.side} px={event.price:.6g} pnl={event.pnl_net:.4f}"
        lines.append(f"{event.symbol} {event.action}{extra} {event.reason}".rstrip())
    return "\n".join(lines)


def load_cycle_book(gate: PhilAliGate) -> Book:
    return load_book(
        book_path(gate),
        equity=gate.paper_equity,
        risk_frac=gate.risk_frac,
        fee_rt=gate.fee_rt,
    )
