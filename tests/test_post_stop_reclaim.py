"""同向止损后再开趋势仓：冷却结束后仍要等日线收盘重回 EMA20。补仓仍只看 4h 突破。"""

from __future__ import annotations

from datetime import datetime, timezone

from hl_bot.config import BotConfig
from hl_bot.indicators import ema, last_value
from hl_bot.models import IntentAction, OrderKind, Regime, Side, Signal, StrategyName
from hl_bot.runner import BotRunner
from hl_bot.strategies.trend import TrendStrategy
from tests.candles import DAY, START, decision, market, series_from_closes
from tests.test_pyramid import _pos, _wide_h4
from tests.test_trend import _breakout_h4
from tests.test_trend_tiers import _inside_h4, _starter_pos


def _daily(last_close: float, n: int = 60) -> list:
    # 至少 ema_slow+2 根，generate_signal / 补仓的 _prepare 才会放行
    closes = [100.0] * (n - 1) + [last_close]
    return series_from_closes(closes, start=START, interval=DAY)


def _ema(daily) -> float:
    value = last_value(ema([c.close for c in daily], 20))
    assert value is not None
    return value


def _freeze(monkeypatch, now_ms: int) -> None:
    def keys(now=None):
        dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
        iso = dt.isocalendar()
        return dt.strftime("%Y-%m-%d"), f"{iso.year}-W{iso.week:02d}", now_ms

    monkeypatch.setattr("hl_bot.runner._utc_keys", keys)


def _runner(tmp_path, monkeypatch, daily, h4, *, now_ms: int, mid: float, regime: Regime = Regime.TREND_LONG):
    _freeze(monkeypatch, now_ms)
    cfg = BotConfig()
    cfg.dry_run = True
    cfg.symbols = ("ETH",)
    cfg.paper_equity = 10_000.0
    cfg.state_path = str(tmp_path / "paper_state.json")
    cfg.trend.btc_regime_mode = "off"
    cfg.trend.stop_cooldown_bars_4h = 0
    runner = BotRunner(cfg)
    runner.client.fetch_asset_contexts = lambda: {  # type: ignore[method-assign]
        "ETH": {"funding": 0.0, "mark_px": mid, "mid_px": mid, "max_leverage": 20, "sz_decimals": 4}
    }
    runner.client.fetch_mids = lambda: {"ETH": mid}  # type: ignore[method-assign]
    snap = market("ETH", daily=daily, h4=h4, mid=mid)

    def load_market(symbol, ctx=None, mid_px=None):
        return snap

    runner.client.load_market = load_market  # type: ignore[method-assign]
    monkeypatch.setattr(
        "hl_bot.runner.route_regime",
        lambda *a, **k: decision(regime, daily_adx=30, ema_fast=120, ema_slow=100),
    )
    return runner


def test_reclaim_block_requires_post_stop_daily_close_above_ema() -> None:
    below = _daily(80.0)
    ema_below = _ema(below)
    assert below[-1].close < ema_below
    stop_ts = below[-1].ts + 60_000
    now_ms = below[-1].end_ts + 1
    snap = market("ETH", daily=below, h4=_inside_h4(), mid=80.0)
    strat = TrendStrategy()
    blocked = strat.post_stop_reclaim_block(snap, Side.LONG, stop_ts, now_ms)
    assert blocked is not None
    assert "尚未回到" in blocked

    # 止损发生在这根日线收盘之后：收盘虽在 EMA 上，但不是止损后的收回
    above = _daily(130.0)
    assert above[-1].close > _ema(above)
    pre = market("ETH", daily=above, h4=_inside_h4(), mid=130.0)
    still = strat.post_stop_reclaim_block(pre, Side.LONG, above[-1].end_ts + 1, above[-1].end_ts + 2)
    assert still is not None
    assert "还没有新的日线收盘" in still

    reclaimed = strat.post_stop_reclaim_block(pre, Side.LONG, above[-1].ts + 60_000, above[-1].end_ts + 1)
    assert reclaimed is None
    # 没有止损记录不拦截，即使收盘在 EMA 下方
    assert strat.post_stop_reclaim_block(snap, Side.LONG, None, now_ms) is None

    # 中途曾收回、最近一根又收到 EMA 下方：以最近收盘为准，继续拒绝
    faded = series_from_closes([100.0] * 58 + [130.0, 80.0], start=START, interval=DAY)
    assert faded[-1].close < _ema(faded)
    faded_block = strat.post_stop_reclaim_block(
        market("ETH", daily=faded), Side.LONG, faded[0].ts, faded[-1].end_ts + 1
    )
    assert faded_block is not None
    assert "尚未回到" in faded_block


def test_short_reclaim_is_close_below_ema() -> None:
    dumped = _daily(80.0)
    popped = _daily(130.0)
    strat = TrendStrategy()
    assert dumped[-1].close < _ema(dumped)
    assert popped[-1].close > _ema(popped)
    now = dumped[-1].end_ts + 1
    stop_during = dumped[-1].ts + 60_000
    assert strat.post_stop_reclaim_block(market("ETH", daily=dumped), Side.SHORT, stop_during, now) is None
    blocked = strat.post_stop_reclaim_block(
        market("ETH", daily=popped), Side.SHORT, popped[-1].ts + 60_000, popped[-1].end_ts + 1
    )
    assert blocked is not None
    assert "下方" in blocked


def test_unclosed_daily_bar_does_not_count_as_reclaim() -> None:
    above = _daily(130.0)
    strat = TrendStrategy()
    # now 还在这根日线内部，已收盘序列里没有止损后的日线
    now_ms = above[-1].ts + 3_600_000
    blocked = strat.post_stop_reclaim_block(
        market("ETH", daily=above), Side.LONG, above[-1].ts + 60_000, now_ms
    )
    assert blocked is not None
    assert "还没有新的日线收盘" in blocked


def test_scan_blocks_starter_below_ema_then_allows_after_reclaim(tmp_path, monkeypatch) -> None:
    below = _daily(80.0)
    h4 = _inside_h4()
    now_ms = below[-1].end_ts + 1
    runner = _runner(tmp_path, monkeypatch, below, h4, now_ms=now_ms, mid=80.0)
    runner.cfg.trend.stop_cooldown_bars_4h = 6
    stop_ts = now_ms - 60_000
    runner._record_stop("ETH", Side.LONG, 90.0, -8.0, stop_ts)

    cooled = runner.scan_once()
    assert cooled.symbols[0].intent is None
    assert cooled.symbols[0].signal is not None
    assert cooled.symbols[0].signal.tag == "trend_confirmed_starter"
    assert any("止损冷却拒绝" in note for note in cooled.symbols[0].notes)

    runner.cfg.trend.stop_cooldown_bars_4h = 0
    still_below = runner.scan_once()
    assert still_below.symbols[0].intent is None
    assert still_below.symbols[0].signal.tag == "trend_confirmed_starter"
    assert any("止损后再入场拒绝" in note and "尚未回到" in note for note in still_below.symbols[0].notes)

    above = _daily(130.0)
    runner.client.load_market = lambda symbol, ctx=None, mid=None: market(  # type: ignore[method-assign]
        "ETH", daily=above, h4=h4, mid=130.0
    )
    allowed = runner.scan_once()
    assert allowed.symbols[0].intent is not None
    assert allowed.symbols[0].intent.action is IntentAction.OPEN
    assert allowed.symbols[0].signal.tag == "trend_confirmed_starter"

    # 日线已经收回，但 6 根 4h 冷却还没走完：仍然不能开
    runner.cfg.trend.stop_cooldown_bars_4h = 6
    runner._record_stop("ETH", Side.LONG, 120.0, -1.0, now_ms - 60_000)
    held = runner.scan_once()
    assert held.symbols[0].intent is None
    assert any("止损冷却拒绝" in note for note in held.symbols[0].notes)
    assert not any("止损后再入场拒绝" in note for note in held.symbols[0].notes)


def test_donchian_reentry_also_waits_for_ema_reclaim(tmp_path, monkeypatch) -> None:
    below = _daily(80.0)
    h4 = _breakout_h4(chase=False)
    now_ms = max(below[-1].end_ts, h4[-1].end_ts) + 1
    runner = _runner(tmp_path, monkeypatch, below, h4, now_ms=now_ms, mid=h4[-1].close)
    runner._record_stop("ETH", Side.LONG, 90.0, -8.0, below[-1].ts + 60_000)
    blocked = runner.scan_once()
    assert blocked.symbols[0].signal is not None
    assert blocked.symbols[0].signal.tag == "donchian_close_breakout"
    assert blocked.symbols[0].intent is None
    assert any("尚未回到" in note for note in blocked.symbols[0].notes)

    above = _daily(130.0)
    runner.client.load_market = lambda symbol, ctx=None, mid=None: market(  # type: ignore[method-assign]
        "ETH", daily=above, h4=h4, mid=h4[-1].close
    )
    allowed = runner.scan_once()
    assert allowed.symbols[0].intent is not None
    assert allowed.symbols[0].signal.tag == "donchian_close_breakout"
    assert allowed.symbols[0].signal.kind is OrderKind.MAKER_LIMIT


def test_no_stop_still_allows_starter_below_ema(tmp_path, monkeypatch) -> None:
    below = _daily(80.0)
    now_ms = below[-1].end_ts + 1
    runner = _runner(tmp_path, monkeypatch, below, _inside_h4(), now_ms=now_ms, mid=80.0)
    report = runner.scan_once()
    assert report.symbols[0].signal.tag == "trend_confirmed_starter"
    assert report.symbols[0].intent is not None
    assert report.symbols[0].intent.action is IntentAction.OPEN


def test_opposite_side_and_other_symbol_are_not_gated(tmp_path, monkeypatch) -> None:
    below = _daily(80.0)
    now_ms = below[-1].end_ts + 1
    runner = _runner(tmp_path, monkeypatch, below, _inside_h4(), now_ms=now_ms, mid=80.0)
    runner._record_stop("ETH", Side.SHORT, 90.0, -8.0, below[-1].ts + 60_000)
    runner._record_stop("SOL", Side.LONG, 90.0, -8.0, below[-1].ts + 60_000)
    report = runner.scan_once()
    assert report.symbols[0].intent is not None
    assert report.symbols[0].signal.side is Side.LONG


def test_mean_reversion_ignores_ema_reclaim_gate(tmp_path, monkeypatch) -> None:
    below = _daily(80.0)
    now_ms = below[-1].end_ts + 1
    runner = _runner(
        tmp_path, monkeypatch, below, _inside_h4(), now_ms=now_ms, mid=80.0, regime=Regime.MEAN_REVERSION
    )
    runner._record_stop("ETH", Side.LONG, 90.0, -8.0, below[-1].ts + 60_000)
    runner.mr.expansion_halt = lambda *a, **k: (False, "")  # type: ignore[method-assign]
    runner.mr.generate_signal = lambda *a, **k: Signal(  # type: ignore[method-assign]
        symbol="ETH",
        strategy=StrategyName.MEAN_REVERSION,
        side=Side.LONG,
        kind=OrderKind.MAKER_LIMIT,
        entry_price=80.0,
        stop_price=76.0,
        reason="mr",
        tag="mr_long",
    )
    report = runner.scan_once()
    assert report.symbols[0].intent is not None
    assert report.symbols[0].intent.action is IntentAction.OPEN
    assert not any("止损后再入场拒绝" in note for note in report.symbols[0].notes)


def test_breakout_add_still_requires_4h_donchian_after_reclaim() -> None:
    above = _daily(130.0)
    assert above[-1].close > _ema(above)
    pos = _starter_pos(symbol="ETH")
    strat = TrendStrategy()
    dec = decision(Regime.TREND_LONG, daily_adx=30)
    assert strat.generate_breakout_add(pos, market("ETH", daily=above, h4=_inside_h4(), mid=104.0), dec) is None
    added = strat.generate_breakout_add(
        pos, market("ETH", daily=above, h4=_breakout_h4(chase=False), mid=101.0), dec
    )
    assert added is not None
    assert added.tag == "donchian_close_breakout"
    assert added.extras["breakout_add"] is True


def test_runner_add_ignores_reclaim_gate_and_still_needs_breakout(tmp_path, monkeypatch) -> None:
    above = _daily(130.0)
    now_ms = above[-1].end_ts + 1
    runner = _runner(tmp_path, monkeypatch, above, _inside_h4(), now_ms=now_ms, mid=100.1)
    runner._record_stop("ETH", Side.LONG, 90.0, -8.0, above[-1].ts + 60_000)
    runner.account.positions.append(_starter_pos(symbol="ETH", entry=100.0, stop=80.0))
    waiting = runner.scan_once()
    assert waiting.symbols[0].intent is None
    assert waiting.symbols[0].signal is None
    assert any("等待 4h Donchian" in note for note in waiting.symbols[0].notes)

    breakout = _breakout_h4(chase=False)
    runner.client.load_market = lambda symbol, ctx=None, mid=None: market(  # type: ignore[method-assign]
        "ETH", daily=above, h4=breakout, mid=breakout[-1].close
    )
    added = runner.scan_once()
    assert added.symbols[0].intent is not None
    assert added.symbols[0].intent.action is IntentAction.ADD
    assert added.symbols[0].signal.tag == "donchian_close_breakout"


def test_pyramid_unchanged_and_not_blocked_by_reclaim_gate(tmp_path, monkeypatch) -> None:
    # 日线短于 EMA50，离场逻辑不会因均线交叉先平仓；收盘仍在 EMA20 下方，用来确认金字塔不走收回过滤
    below = _daily(80.0, n=40)
    h4 = _wide_h4()
    now_ms = max(below[-1].end_ts, h4[-1].end_ts) + 1
    runner = _runner(tmp_path, monkeypatch, below, h4, now_ms=now_ms, mid=111.0)
    runner._record_stop("ETH", Side.LONG, 90.0, -8.0, below[-1].ts + 60_000)
    runner.account.positions.append(_pos(entry=100.0, stop=80.0))
    # 金字塔不看日线是否在 EMA20 下，也不改成必须 4h 突破
    report = runner.scan_once()
    assert report.symbols[0].signal is not None
    assert report.symbols[0].signal.tag == "pyramid_add"
    assert report.symbols[0].intent is not None
    assert report.symbols[0].intent.action is IntentAction.ADD
    assert not any("止损后再入场拒绝" in note for note in report.symbols[0].notes)
