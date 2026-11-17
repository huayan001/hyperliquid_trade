"""不同入场档互斥，以及 1h 强吞没：平剩余仓、冻结该方向、方案 B 仍在。

2026-11-15（Asia/Shanghai）SOL 的两根 1h 只作夹具，策略里不写死价格：
- 09:00 O 69.366 H 69.461 L 67.378 C 67.797（阴线）
- 10:00 O 67.801 H 71.552 L 67.504 C 71.313（阳线，开盘只比前收高 0.004）
当天 07:00 的较弱吞没用「实体 < 1×ATR」代替，不应触发。
10:11 仍在 10:00 这根 K 线内部，不能提前冻结。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from hl_bot.config import BotConfig, TrendConfig, load_config
from hl_bot.indicators import atr, last_value
from hl_bot.models import (
    IntentAction,
    OrderIntent,
    OrderKind,
    Position,
    Regime,
    RestingEntry,
    Side,
    Signal,
    StrategyName,
)
from hl_bot.runner import BotRunner, _entry_band
from hl_bot.strategies.trend import TrendStrategy, detect_strong_engulfing
from tests.candles import H1, H4, START, candle, decision, downtrend, market, uptrend

# 09:00 / 10:00 这两根的开盘时间只用于夹具对齐，不进入策略代码。
SOL_PREV = (69.366, 69.461, 67.378, 67.797)  # open, high, low, close
SOL_CUR = (67.801, 71.552, 67.504, 71.313)
DONCHIAN_OID = 605380367615
STOP_OID = 42


def _bars(spec: tuple, ts: int):
    open_, high, low, close = spec
    return candle(close, open_=open_, high=high, low=low, ts=ts, interval=H1)


def _calm(n: int, start: int, *, px: float = 69.2, span: float = 0.12) -> list:
    out = []
    for i in range(n):
        close = px + (0.04 if i % 2 == 0 else -0.04)
        out.append(
            candle(close, open_=px, high=px + span, low=px - span, ts=start + i * H1, interval=H1)
        )
    return out


def _sol_bullish_h1() -> tuple[list, int]:
    start = START
    prev_ts = start + 30 * H1
    cur_ts = prev_ts + H1
    bars = _calm(30, start) + [_bars(SOL_PREV, prev_ts), _bars(SOL_CUR, cur_ts)]
    return bars, cur_ts


def _bearish_mirror_h1() -> tuple[list, int]:
    """看跌镜像：前阳后阴，开盘略低于前收，收盘低于前低，实体同样大。"""
    start = START
    prev_ts = start + 30 * H1
    cur_ts = prev_ts + H1
    prev = candle(69.366, open_=67.797, high=69.461, low=67.378, ts=prev_ts, interval=H1)
    cur = candle(65.90, open_=69.360, high=69.50, low=65.70, ts=cur_ts, interval=H1)
    return _calm(30, start) + [prev, cur], cur_ts


def _weak_engulf_h1() -> list:
    """结构上像吞没，但实体小于 1h ATR。对应 SOL 当天 07:00 那种较弱反弹。"""
    start = START
    bars = []
    for i in range(30):
        close = 100.2 if i % 2 == 0 else 99.8
        bars.append(candle(close, open_=100.0, high=104.0, low=96.0, ts=start + i * H1, interval=H1))
    prev_ts = start + 30 * H1
    cur_ts = prev_ts + H1
    prev = candle(100.0, open_=101.0, high=101.2, low=99.5, ts=prev_ts, interval=H1)
    cur = candle(101.4, open_=100.05, high=101.6, low=99.8, ts=cur_ts, interval=H1)
    bars.extend([prev, cur])
    return bars


def _h4_book(*, break_close: float | None, start: int, n: int = 30) -> list:
    bars = []
    for i in range(n):
        ts = start + i * H4
        if break_close is not None and i == n - 1:
            bars.append(candle(break_close, open_=100.5, high=101.0, low=break_close, ts=ts, interval=H4))
        else:
            bars.append(candle(100.5, open_=100.4, high=101.0, low=100.0, ts=ts, interval=H4))
    return bars


class _Book:
    def __init__(self) -> None:
        self.cancels: list[tuple[str, int]] = []

    def cancel(self, coin: str, oid: int):
        self.cancels.append((coin, int(oid)))
        return {"status": "ok"}


def _intent(side: Side, band: str, *, price: float, symbol: str = "SOL") -> OrderIntent:
    if band == "starter":
        extras = {"tier": "trend_confirmed_starter", "tag": "trend_confirmed_starter", "starter": True}
        kind = OrderKind.MARKET
        action = IntentAction.OPEN
    else:
        extras = {"tier": "donchian_close_breakout", "tag": "donchian_close_breakout", "breakout_add": True}
        kind = OrderKind.MAKER_LIMIT
        action = IntentAction.OPEN
    return OrderIntent(
        action=action,
        symbol=symbol,
        strategy=StrategyName.TREND,
        side=side,
        kind=kind,
        size=0.48 if band == "starter" else 1.37,
        price=price,
        stop_price=price + 2 if side is Side.SHORT else price - 2,
        leverage=5,
        isolated=True,
        reduce_only=False,
        reason=band,
        extras=extras,
    )


def _rest(side: Side, band: str, oid: int, price: float, *, symbol: str = "SOL") -> RestingEntry:
    tier = "trend_confirmed_starter" if band == "starter" else "donchian_close_breakout"
    return RestingEntry(
        symbol=symbol,
        tier=tier,
        action="open",
        side=side.value,
        price=price,
        size=1.37 if band == "breakout" else 0.48,
        stop_price=None,
        oid=oid,
        strategy="trend",
        leverage=5,
        isolated=True,
        tag=tier,
        extras={"starter": True} if band == "starter" else {"tier": tier},
    )


def _entry_row(side: Side, oid: int, price: float, *, symbol: str = "SOL") -> dict:
    return {
        "coin": symbol,
        "side": "B" if side is Side.LONG else "A",
        "limitPx": str(price),
        "sz": "1.37",
        "oid": oid,
        "reduceOnly": False,
        "orderType": "Limit",
    }


def _stop_row(symbol: str = "SOL", oid: int = STOP_OID) -> dict:
    return {
        "coin": symbol,
        "side": "B" if symbol else "B",
        "limitPx": "80",
        "triggerPx": "80",
        "sz": "1",
        "oid": oid,
        "reduceOnly": True,
        "isTrigger": True,
        "orderType": "Stop Market",
        "tpsl": "sl",
    }


def _live_runner(tmp_path, symbol: str = "SOL") -> BotRunner:
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.enable_live = True
    cfg.private_key = "0x" + "11" * 32
    cfg.account_address = "0x" + "22" * 20
    cfg.state_path = str(tmp_path / "state.json")
    cfg.symbols = (symbol,)
    cfg.trend.btc_regime_mode = "off"
    cfg.trend.btc_bearish_long_mode = "off"
    runner = BotRunner(cfg)
    book = _Book()
    runner.client.connect_sdk = lambda **kwargs: None  # type: ignore[method-assign]
    runner.client._exchange = book
    runner.client._sdk_ready = True
    runner._live_book_ok = True
    runner._equity_ok = True
    return runner


def _filled(size: str, px: str, oid: int = 99) -> dict:
    return {
        "status": "ok",
        "response": {"data": {"statuses": [{"filled": {"totalSz": size, "avgPx": px, "oid": oid}}]}},
    }


def _resting_result(oid: int) -> dict:
    return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}


def _freeze_clock(monkeypatch, clock: dict) -> None:
    def keys(now=None):
        now_ms = int(clock["now"])
        dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
        iso = dt.isocalendar()
        return dt.strftime("%Y-%m-%d"), f"{iso.year}-W{iso.week:02d}", now_ms

    monkeypatch.setattr("hl_bot.runner._utc_keys", keys)


def _paper_runner(tmp_path, monkeypatch, snap, *, now_ms: int, regime: Regime, symbol: str = "SOL"):
    clock = {"now": now_ms}
    _freeze_clock(monkeypatch, clock)
    cfg = BotConfig()
    cfg.dry_run = True
    cfg.symbols = (symbol,)
    cfg.paper_equity = 10_000.0
    cfg.state_path = str(tmp_path / "paper.json")
    cfg.trend.btc_regime_mode = "off"
    cfg.trend.btc_bearish_long_mode = "off"
    cfg.trend.stop_cooldown_bars_4h = 0
    runner = BotRunner(cfg)
    runner.client.fetch_asset_contexts = lambda: {  # type: ignore[method-assign]
        symbol: {"funding": 0.0, "mark_px": snap.mid, "mid_px": snap.mid, "max_leverage": 20, "sz_decimals": 4}
    }
    runner.client.fetch_mids = lambda: {symbol: snap.mid}  # type: ignore[method-assign]
    runner.client.load_market = lambda *_a, **_k: snap  # type: ignore[method-assign]
    monkeypatch.setattr(
        "hl_bot.runner.route_regime",
        lambda *_a, **_k: decision(regime, daily_adx=30, ema_fast=80, ema_slow=100),
    )
    return runner, clock


def test_entry_bands_classify_starter_and_donchian() -> None:
    assert _entry_band("trend_confirmed_starter", "trend_confirmed_starter", {"starter": True}) == "starter"
    assert _entry_band("donchian_close_breakout", "donchian_close_breakout", {}) == "breakout"
    assert _entry_band("btc_chase_breakout", "", {"chase": True}) == "breakout"
    assert _entry_band("pyramid_add", "", {"pyramid": True}) == "pyramid"


@pytest.mark.parametrize("side", [Side.SHORT, Side.LONG])
def test_starter_fill_cancels_other_band_and_keeps_stop(tmp_path, side: Side, caplog) -> None:
    runner = _live_runner(tmp_path)
    book: _Book = runner.client._exchange
    price = 71.245 if side is Side.SHORT else 71.4
    donchian_px = 72.182 if side is Side.SHORT else 70.1
    runner.account.resting_entries.append(_rest(side, "breakout", DONCHIAN_OID, donchian_px))
    runner._open_orders = [
        _entry_row(side, DONCHIAN_OID, donchian_px),
        _stop_row(),
    ]
    placed: list[OrderIntent] = []

    def place(intent, **kwargs):
        placed.append(intent)
        return _filled("0.48", str(price))

    runner.client.place = place  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO):
        runner._execute(_intent(side, "starter", price=price), now_ms=1)

    assert len(placed) == 1
    assert placed[0].extras.get("starter") is True
    assert book.cancels == [("SOL", DONCHIAN_OID)]
    assert STOP_OID not in {oid for _, oid in book.cancels}
    assert all(rec.oid != DONCHIAN_OID for rec in runner.account.resting_entries)
    assert "撤销冲突挂单" in caplog.text
    assert str(DONCHIAN_OID) in caplog.text


@pytest.mark.parametrize("side", [Side.SHORT, Side.LONG])
def test_donchian_cancels_older_starter_rest(tmp_path, side: Side) -> None:
    runner = _live_runner(tmp_path)
    book: _Book = runner.client._exchange
    starter_px = 71.0 if side is Side.SHORT else 70.2
    donchian_px = 72.182 if side is Side.SHORT else 73.0
    runner.account.resting_entries.append(_rest(side, "starter", 100, starter_px))
    runner._open_orders = [_entry_row(side, 100, starter_px), _stop_row()]

    def place(intent, **kwargs):
        return _resting_result(700)

    runner.client.place = place  # type: ignore[method-assign]
    runner._execute(_intent(side, "breakout", price=donchian_px), now_ms=2)

    assert book.cancels == [("SOL", 100)]
    assert [rec.oid for rec in runner.account.resting_entries] == [700]
    assert runner.account.resting_entries[0].tier == "donchian_close_breakout"


@pytest.mark.parametrize("side", [Side.SHORT, Side.LONG])
def test_same_band_keeps_existing_rest_and_skips_the_new_order(tmp_path, side: Side) -> None:
    runner = _live_runner(tmp_path)
    book: _Book = runner.client._exchange
    price = 72.182
    runner.account.resting_entries.append(_rest(side, "breakout", DONCHIAN_OID, price))
    runner._open_orders = [_entry_row(side, DONCHIAN_OID, price), _stop_row()]
    calls: list[OrderIntent] = []

    def place(intent, **kwargs):
        calls.append(intent)
        return _resting_result(800)

    runner.client.place = place  # type: ignore[method-assign]
    runner._execute(_intent(side, "breakout", price=price), now_ms=3)

    assert calls == []
    assert book.cancels == []
    assert [rec.oid for rec in runner.account.resting_entries] == [DONCHIAN_OID]


def test_rejected_entry_does_not_cancel_the_other_band(tmp_path) -> None:
    runner = _live_runner(tmp_path)
    book: _Book = runner.client._exchange
    runner.account.resting_entries.append(_rest(Side.SHORT, "breakout", DONCHIAN_OID, 72.182))
    runner._open_orders = [_entry_row(Side.SHORT, DONCHIAN_OID, 72.182), _stop_row()]
    runner.client.place = lambda *a, **k: {"status": "error", "message": "rejected"}  # type: ignore[method-assign]
    runner._execute(_intent(Side.SHORT, "starter", price=71.245), now_ms=4)
    assert book.cancels == []
    assert [rec.oid for rec in runner.account.resting_entries] == [DONCHIAN_OID]


def test_unfilled_market_does_not_cancel_the_other_band(tmp_path) -> None:
    runner = _live_runner(tmp_path)
    book: _Book = runner.client._exchange
    runner.account.resting_entries.append(_rest(Side.LONG, "breakout", DONCHIAN_OID, 70.1))
    runner._open_orders = [_entry_row(Side.LONG, DONCHIAN_OID, 70.1)]
    runner.client.place = lambda *a, **k: {  # type: ignore[method-assign]
        "status": "ok",
        "entry_unfilled": True,
        "resting_oids": [],
    }
    runner._execute(_intent(Side.LONG, "starter", price=71.4), now_ms=5)
    assert book.cancels == []
    assert [rec.oid for rec in runner.account.resting_entries] == [DONCHIAN_OID]


def test_opposite_direction_rest_is_left_in_place(tmp_path) -> None:
    runner = _live_runner(tmp_path)
    book: _Book = runner.client._exchange
    runner.account.resting_entries.append(_rest(Side.LONG, "breakout", DONCHIAN_OID, 70.1))
    runner._open_orders = [_entry_row(Side.LONG, DONCHIAN_OID, 70.1), _stop_row()]
    runner.client.place = lambda *a, **k: _filled("0.48", "71.245")  # type: ignore[method-assign]
    runner._execute(_intent(Side.SHORT, "starter", price=71.245), now_ms=6)
    assert book.cancels == []
    assert [rec.oid for rec in runner.account.resting_entries] == [DONCHIAN_OID]


def test_reconcile_keeps_newer_band_and_does_not_cancel_stop(tmp_path) -> None:
    runner = _live_runner(tmp_path)
    book: _Book = runner.client._exchange
    runner.account.resting_entries.extend(
        [
            _rest(Side.SHORT, "breakout", 100, 72.182),
            _rest(Side.SHORT, "starter", 500, 71.2),
        ]
    )
    runner._open_orders = [
        _entry_row(Side.SHORT, 100, 72.182),
        _entry_row(Side.SHORT, 500, 71.2),
        _stop_row(),
    ]
    runner._cancel_older_cross_band_rests()
    assert book.cancels == [("SOL", 100)]
    assert [rec.oid for rec in runner.account.resting_entries] == [500]


def test_sol_bullish_engulf_fires_and_strict_gap_does_not() -> None:
    bars, _cur_ts = _sol_bullish_h1()
    trigger = detect_strong_engulfing(bars, TrendConfig())
    assert trigger is not None
    assert trigger.kind == "bullish"
    assert trigger.against is Side.SHORT
    assert trigger.extreme == pytest.approx(67.504)
    assert trigger.body == pytest.approx(71.313 - 67.801)
    assert trigger.body >= trigger.atr

    strict = detect_strong_engulfing(bars, TrendConfig(engulf_gap_tol_frac=0.0))
    assert strict is None


def test_bearish_mirror_fires_against_longs() -> None:
    bars, _cur_ts = _bearish_mirror_h1()
    trigger = detect_strong_engulfing(bars, TrendConfig())
    assert trigger is not None
    assert trigger.kind == "bearish"
    assert trigger.against is Side.LONG
    assert trigger.extreme == pytest.approx(69.50)
    assert trigger.close < 67.378


def test_weak_engulf_below_one_atr_does_not_fire() -> None:
    bars = _weak_engulf_h1()
    atr_v = last_value(atr(bars, 14))
    assert atr_v is not None
    assert bars[-1].body < atr_v
    assert bars[-1].close > bars[-2].high
    assert detect_strong_engulfing(bars, TrendConfig()) is None


def test_close_must_clear_the_previous_extreme() -> None:
    bars, cur_ts = _sol_bullish_h1()
    weak_close = list(bars)
    prev = bars[-2]
    weak_close[-1] = candle(
        prev.high - 0.01,
        open_=SOL_CUR[0],
        high=prev.high + 0.2,
        low=SOL_CUR[2],
        ts=cur_ts,
        interval=H1,
    )
    assert detect_strong_engulfing(weak_close, TrendConfig()) is None


def test_mid_bar_does_not_arm_and_close_does(tmp_path, monkeypatch) -> None:
    bars, cur_ts = _sol_bullish_h1()
    forming = bars[-1]
    snap = market("SOL", h1=bars, mid=70.0, daily=downtrend(80), h4=_h4_book(break_close=None, start=START))
    runner, clock = _paper_runner(tmp_path, monkeypatch, snap, now_ms=forming.ts + 11 * 60_000, regime=Regime.TREND_SHORT)
    clock["now"] = forming.ts + 11 * 60_000
    assert runner._refresh_engulfing(snap, clock["now"]) == []
    assert runner._entry_freeze_reason("SOL", Side.SHORT) is None

    clock["now"] = forming.end_ts + 1
    runner._refresh_engulfing(snap, clock["now"])
    assert runner._entry_freeze_reason("SOL", Side.SHORT)
    assert runner._entry_freeze_reason("SOL", Side.LONG) is None
    assert runner._entry_freeze_reason("ETH", Side.SHORT) is None


def test_bullish_engulf_flattens_short_cancels_entry_not_stop(tmp_path, caplog) -> None:
    bars, cur_ts = _sol_bullish_h1()
    snap = market("SOL", h1=bars, mid=71.0, daily=downtrend(80), h4=_h4_book(break_close=None, start=START))
    runner = _live_runner(tmp_path)
    book: _Book = runner.client._exchange
    runner.account.positions.append(
        Position(
            symbol="SOL",
            strategy=StrategyName.TREND,
            side=Side.SHORT,
            size=0.48,
            entry_price=71.245,
            stop_price=80.0,
            leverage=5,
            opened_ts=1,
            tag="trend_confirmed_starter",
            extras={"entry_atr": 1.0, "_opened_size": 0.48},
        )
    )
    runner.account.resting_entries.append(_rest(Side.SHORT, "breakout", DONCHIAN_OID, 72.182))
    runner._open_orders = [_entry_row(Side.SHORT, DONCHIAN_OID, 72.182), _stop_row()]
    with caplog.at_level(logging.INFO):
        exits = runner._refresh_engulfing(snap, bars[-1].end_ts + 1)
    assert len(exits) == 1
    assert exits[0].action is IntentAction.CLOSE
    assert exits[0].reduce_only is True
    assert exits[0].size == pytest.approx(0.48)
    assert exits[0].extras.get("engulf_reversal") is True
    assert book.cancels == [("SOL", DONCHIAN_OID)]
    assert "强吞没" in caplog.text
    reloaded = BotRunner(runner.cfg)
    assert reloaded._entry_freeze_reason("SOL", Side.SHORT)
    assert reloaded.account.entry_freezes["SOL"]["short"]["extreme"] == pytest.approx(67.504)


def test_bearish_engulf_flattens_long_only(tmp_path) -> None:
    bars, _cur_ts = _bearish_mirror_h1()
    snap = market("SOL", h1=bars, mid=66.0, daily=uptrend(80), h4=_h4_book(break_close=None, start=START))
    runner = _live_runner(tmp_path)
    runner.account.positions.append(
        Position(
            symbol="SOL",
            strategy=StrategyName.TREND,
            side=Side.LONG,
            size=1.2,
            entry_price=68.0,
            stop_price=60.0,
            leverage=5,
            opened_ts=1,
            tag="trend_confirmed_starter",
            extras={"entry_atr": 1.0, "_opened_size": 1.2},
        )
    )
    exits = runner._refresh_engulfing(snap, bars[-1].end_ts + 1)
    assert len(exits) == 1
    assert exits[0].action is IntentAction.CLOSE
    assert exits[0].size == pytest.approx(1.2)
    assert exits[0].extras["engulf_kind"] == "bearish"
    assert runner._entry_freeze_reason("SOL", Side.LONG)
    assert runner._entry_freeze_reason("SOL", Side.SHORT) is None


def test_freeze_lifts_on_close_beyond_extreme_or_later_donchian(tmp_path) -> None:
    bars, cur_ts = _sol_bullish_h1()
    h4_old = _h4_book(break_close=99.0, start=START)
    # 这根 4h 的结束时间不晚于吞没 K，旧突破不能解除冻结。
    h4_old[-1] = candle(99.0, open_=100.5, high=101.0, low=99.0, ts=bars[-1].ts - H4, interval=H4)
    snap = market("SOL", h1=bars, mid=71.0, daily=downtrend(80), h4=h4_old)
    runner = _live_runner(tmp_path)
    runner.client._exchange = _Book()
    runner._refresh_engulfing(snap, bars[-1].end_ts + 1)
    assert runner._entry_freeze_reason("SOL", Side.SHORT)

    # 收盘等于极值不算突破。
    equal_ts = cur_ts + H1
    equal = candle(67.504, open_=67.504, high=67.8, low=67.4, ts=equal_ts, interval=H1)
    held = market("SOL", h1=bars + [equal], mid=67.5, daily=downtrend(80), h4=h4_old)
    runner._refresh_engulfing(held, equal.end_ts + 1)
    assert runner._entry_freeze_reason("SOL", Side.SHORT)

    below_ts = equal_ts + H1
    below = candle(67.0, open_=68.0, high=68.2, low=66.8, ts=below_ts, interval=H1)
    lifted = market("SOL", h1=bars + [equal, below], mid=67.0, daily=downtrend(80), h4=h4_old)
    runner._refresh_engulfing(lifted, below.end_ts + 1)
    assert runner._entry_freeze_reason("SOL", Side.SHORT) is None


def test_later_donchian_break_lifts_short_freeze_without_touching_longs(tmp_path) -> None:
    bars, _cur_ts = _sol_bullish_h1()
    h4_old = _h4_book(break_close=99.0, start=START)
    h4_old[-1] = candle(99.0, open_=100.5, high=101.0, low=99.0, ts=bars[-1].ts - H4, interval=H4)
    snap = market("SOL", h1=bars, mid=71.0, daily=downtrend(80), h4=h4_old)
    runner = _live_runner(tmp_path)
    runner._refresh_engulfing(snap, bars[-1].end_ts + 1)
    assert runner._entry_freeze_reason("SOL", Side.SHORT)
    assert runner._entry_freeze_reason("SOL", Side.LONG) is None

    # 突破这根 4h 在吞没 K 之后收盘，扫描时刻也晚于这根 4h。
    later = _h4_book(break_close=99.0, start=bars[-1].end_ts - 28 * H4)
    released = market("SOL", h1=bars, mid=71.2, daily=downtrend(80), h4=later)
    assert later[-1].end_ts > bars[-1].end_ts
    runner._refresh_engulfing(released, later[-1].end_ts + 1)
    assert runner._entry_freeze_reason("SOL", Side.SHORT) is None


def test_scan_blocks_frozen_side_only_and_ignores_mid_bar(tmp_path, monkeypatch) -> None:
    bars, cur_ts = _sol_bullish_h1()
    forming = bars[-1]
    snap = market("SOL", h1=bars, mid=70.5, daily=downtrend(80), h4=_h4_book(break_close=None, start=START))
    runner, clock = _paper_runner(
        tmp_path, monkeypatch, snap, now_ms=forming.ts + 11 * 60_000, regime=Regime.TREND_SHORT
    )
    side = {"value": Side.SHORT}

    def generate(market, decision, now_ms, existing=None):
        return Signal(
            symbol="SOL",
            strategy=StrategyName.TREND,
            side=side["value"],
            kind=OrderKind.MARKET,
            entry_price=70.5,
            stop_price=74.5 if side["value"] is Side.SHORT else 66.5,
            reason="stub starter",
            tag="trend_confirmed_starter",
            extras={"starter": True, "starter_frac": 0.35, "size_frac": 0.35, "tier": "trend_confirmed_starter"},
        )

    runner.trend.generate_signal = generate  # type: ignore[method-assign]
    mid = runner.scan_once(persist_paper=True)
    assert mid.symbols[0].intent is not None
    assert runner._entry_freeze_reason("SOL", Side.SHORT) is None
    assert runner.account.position_for("SOL") is not None

    clock["now"] = forming.end_ts + 1
    closed = runner.scan_once(persist_paper=True)
    assert runner.account.position_for("SOL") is None
    assert any(item.extras.get("engulf_reversal") for item in closed.symbols[0].exits)
    assert runner._entry_freeze_reason("SOL", Side.SHORT)

    blocked = runner.scan_once(persist_paper=True)
    assert blocked.symbols[0].intent is None
    assert any("吞没冻结" in note for note in blocked.symbols[0].notes)

    side["value"] = Side.LONG
    monkeypatch.setattr(
        "hl_bot.runner.route_regime",
        lambda *_a, **_k: decision(Regime.TREND_LONG, daily_adx=30, ema_fast=120, ema_slow=100),
    )
    allowed = runner.scan_once(persist_paper=True)
    assert allowed.symbols[0].intent is not None
    assert allowed.symbols[0].intent.side is Side.LONG
    assert runner._entry_freeze_reason("SOL", Side.SHORT)


def test_scheme_b_still_scales_out_when_engulf_does_not_fire(tmp_path, monkeypatch) -> None:
    pos = Position(
        symbol="SOL",
        strategy=StrategyName.TREND,
        side=Side.SHORT,
        size=2.0,
        entry_price=100.0,
        stop_price=130.0,
        leverage=5,
        opened_ts=1,
        tag="donchian_close_breakout",
        extras={"entry_atr": 10.0, "initial_stop": 130.0, "partial_atr_stop_frozen": True, "_opened_size": 2.0},
    )
    strat = TrendStrategy(TrendConfig())
    assert strat.partial_atr_enabled()
    assert strat.cfg.engulf_reversal_enabled is True
    one = strat.generate_exits(pos, market("SOL", mid=90.0, daily=downtrend(80)), decision(Regime.TREND_SHORT), START)
    assert len(one) == 1
    assert one[0].extras["partial_atr_level"] == 1
    assert one[0].size == pytest.approx(1.0)
    two = strat.generate_exits(
        pos,
        market("SOL", mid=80.0, daily=downtrend(80)),
        decision(Regime.TREND_SHORT),
        START,
    )
    assert two[0].extras["partial_atr_level"] == 2
    assert two[0].action is IntentAction.CLOSE

    snap = market("SOL", mid=90.0, daily=downtrend(80), h1=_calm(40, START, px=100.0, span=0.2))
    runner, _clock = _paper_runner(tmp_path, monkeypatch, snap, now_ms=snap.h1[-1].end_ts + 1, regime=Regime.TREND_SHORT)
    runner.account.positions.append(pos)
    report = runner.scan_once(persist_paper=True)
    exits = report.symbols[0].exits
    assert len(exits) == 1
    assert exits[0].extras.get("partial_atr_level") == 1
    assert exits[0].extras.get("engulf_reversal") is not True
    assert runner.account.position_for("SOL") is not None
    assert runner.account.position_for("SOL").size == pytest.approx(1.0)


def test_engulf_flatten_replaces_scheme_b_partial_on_that_scan(tmp_path, monkeypatch) -> None:
    bars, _cur_ts = _sol_bullish_h1()
    pos = Position(
        symbol="SOL",
        strategy=StrategyName.TREND,
        side=Side.SHORT,
        size=2.0,
        entry_price=71.0,
        stop_price=80.0,
        leverage=5,
        opened_ts=1,
        tag="trend_confirmed_starter",
        extras={"entry_atr": 1.0, "initial_stop": 80.0, "partial_atr_stop_frozen": True, "_opened_size": 2.0},
    )
    # 浮盈已到 1×入场 ATR，方案 B 本来只会平一半；吞没要把剩余一次平掉。
    snap = market("SOL", h1=bars, mid=70.0, daily=downtrend(80), h4=_h4_book(break_close=None, start=START))
    runner, _clock = _paper_runner(
        tmp_path, monkeypatch, snap, now_ms=bars[-1].end_ts + 1, regime=Regime.TREND_SHORT
    )
    runner.account.positions.append(pos)
    report = runner.scan_once(persist_paper=True)
    exits = report.symbols[0].exits
    assert len(exits) == 1
    assert exits[0].extras.get("engulf_reversal") is True
    assert exits[0].action is IntentAction.CLOSE
    assert exits[0].size == pytest.approx(2.0)
    assert runner.account.position_for("SOL") is None


def test_engulf_config_defaults_on_and_rejects_bad_values(tmp_path) -> None:
    cfg = BotConfig()
    assert cfg.trend.engulf_reversal_enabled is True
    assert cfg.trend.engulf_timeframe == "1h"
    assert cfg.trend.engulf_min_body_atr == pytest.approx(1.0)
    assert cfg.trend.engulf_gap_tol_frac == pytest.approx(0.001)
    loaded = load_config("config.toml")
    assert loaded.trend.engulf_reversal_enabled is True
    assert loaded.trend.engulf_timeframe == "1h"

    missing = tmp_path / "omit.toml"
    missing.write_text("[trend]\nexit_mode = \"partial_atr\"\n", encoding="utf-8")
    assert load_config(missing).trend.engulf_reversal_enabled is True

    bad = tmp_path / "bad.toml"
    bad.write_text("[trend]\nengulf_timeframe = \"2h\"\n", encoding="utf-8")
    with pytest.raises(ValueError, match="engulf_timeframe"):
        load_config(bad)
    bad.write_text("[trend]\nengulf_gap_tol_frac = 0.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="engulf_gap_tol_frac"):
        load_config(bad)
    bad.write_text("[trend]\nengulf_min_body_atr = 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="engulf_min_body_atr"):
        load_config(bad)
