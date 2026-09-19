from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from hl_bot.alerts import AlertSink
from hl_bot.config import BotConfig
from hl_bot.exchange.client import HyperliquidClient
from hl_bot.exchange.paper import PaperBroker
from hl_bot.models import (
    AccountState,
    MarketSnapshot,
    OrderIntent,
    Regime,
    RegimeDecision,
    Side,
    Signal,
    StrategyName,
)
from hl_bot.regime import route_regime
from hl_bot.risk import RiskManager
from hl_bot.strategies.mean_reversion import MeanReversionStrategy
from hl_bot.strategies.trend import TrendStrategy

logger = logging.getLogger(__name__)


@dataclass
class SymbolReport:
    symbol: str
    decision: RegimeDecision
    signal: Signal | None
    intent: OrderIntent | None
    exits: list[OrderIntent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    mid: float = 0.0
    funding_annual: float = 0.0


@dataclass
class ScanReport:
    network: str
    dry_run: bool
    equity: float
    generated_at: str
    symbols: list[SymbolReport]
    skipped: list[str] = field(default_factory=list)


def _utc_keys(now: datetime | None = None) -> tuple[str, str, int]:
    now = now or datetime.now(timezone.utc)
    iso = now.isocalendar()
    return now.strftime("%Y-%m-%d"), f"{iso.year}-W{iso.week:02d}", int(now.timestamp() * 1000)


def funding_against(side: Side, hourly_rate: float) -> bool:
    # 正资金费率：多头付给空头
    if side is Side.LONG:
        return hourly_rate > 0
    return hourly_rate < 0


class BotRunner:
    def __init__(
        self,
        cfg: BotConfig,
        client: HyperliquidClient | None = None,
        alerts: AlertSink | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client or HyperliquidClient(cfg)
        self.alerts = alerts or AlertSink()
        self.risk = RiskManager(cfg)
        self.trend = TrendStrategy(cfg.trend)
        self.mr = MeanReversionStrategy(cfg.mean_reversion)
        day_key, week_key, _ = _utc_keys()
        self.broker = PaperBroker.load(cfg.state_path, cfg.paper_equity, day_key, week_key)

    @property
    def account(self) -> AccountState:
        return self.broker.account

    def scan_once(self, *, persist_paper: bool = False) -> ScanReport:
        day_key, week_key, now_ms = _utc_keys()
        if self.account.day_key != day_key:
            self.account.day_start_equity = self.account.equity
            self.account.day_trades = {}
            self.account.realized_pnl_today = 0.0
            self.account.day_key = day_key
        if self.account.week_key != week_key:
            self.account.week_start_equity = self.account.equity
            self.account.week_key = week_key

        if not self.cfg.dry_run:
            try:
                self.account.equity = self.client.account_equity(self.account.equity)
            except Exception as exc:
                logger.warning("读取实盘权益失败: %s", exc)

        ctxs = self.client.fetch_asset_contexts()
        mids = self.client.fetch_mids()
        reports: list[SymbolReport] = []
        skipped: list[str] = []
        same_way: list[str] = []

        for symbol in self.cfg.symbols:
            if symbol not in ctxs and symbol not in mids:
                skipped.append(f"{symbol}: 行情不存在（当前网络 {self.cfg.network}）")
                continue
            try:
                market = self.client.load_market(symbol, ctxs.get(symbol), mids.get(symbol))
            except Exception as exc:
                logger.exception("拉取 %s 行情失败", symbol)
                skipped.append(f"{symbol}: {exc}")
                continue

            decision = route_regime(market.daily, market.h1, self.cfg.regime, symbol=symbol)
            notes: list[str] = [decision.reason]
            if (
                decision.range_structure
                and decision.range_structure.is_range
                and decision.hourly_adx is not None
                and decision.hourly_adx >= self.cfg.regime.mr_adx_max
            ):
                notes.append(
                    f"1h 虽有区间外形但 ADX={decision.hourly_adx:.1f}≥{self.cfg.regime.mr_adx_max}，不启用均值回归"
                )
            if market.funding.annualized_24h:
                notes.append(
                    f"资金费率 {market.funding.hourly_rate:.6%} /h "
                    f"（当前年化 {market.funding.annualized:.1%}，24h均 {market.funding.annualized_24h:.1%}）"
                )

            exits = self._exits_for(market, decision, now_ms)
            for intent in exits:
                self._execute(intent, now_ms, persist_paper=persist_paper)

            signal: Signal | None = None
            intent: OrderIntent | None = None
            if decision.regime.is_trend():
                signal = self.trend.generate_signal(market, decision, now_ms)
            elif decision.regime is Regime.MEAN_REVERSION:
                halted, why = self.mr.expansion_halt(market.h1, decision)
                if halted:
                    notes.append(why)
                else:
                    signal = self.mr.generate_signal(market, decision, now_ms)
            else:
                notes.append("中间地带/冲突：不新开仓")

            if signal is not None:
                against = funding_against(signal.side, market.funding.hourly_rate)
                huge_funding = (
                    against
                    and max(abs(market.funding.annualized), abs(market.funding.annualized_24h))
                    >= self.cfg.trend.funding_annual_warn
                )
                if huge_funding:
                    notes.append("持仓方向资金费年化>50%，按规则下调仓位")
                verdict = self.risk.evaluate_open(
                    signal,
                    self.account,
                    funding_against=huge_funding,
                    max_leverage_hint=min(market.max_leverage, self.cfg.max_leverage_for(symbol)),
                )
                if verdict.allowed:
                    intent = self.risk.to_open_intent(signal, verdict, isolated=self.cfg.risk.isolated)
                    if intent:
                        self._execute(intent, now_ms, persist_paper=persist_paper)
                        same_way.append(f"{symbol}:{signal.side.value}")
                else:
                    notes.append(f"风控拒绝：{verdict.reason}")

            reports.append(
                SymbolReport(
                    symbol=symbol,
                    decision=decision,
                    signal=signal,
                    intent=intent,
                    exits=exits,
                    notes=notes,
                    mid=market.mid,
                    funding_annual=market.funding.annualized,
                )
            )

        if len(same_way) >= 2:
            logger.info("多标的同向信号（高度相关，不额外加风险预算）: %s", ", ".join(same_way))

        if persist_paper:
            self.broker.save()
        return ScanReport(
            network=self.cfg.network,
            dry_run=self.cfg.dry_run,
            equity=self.account.equity,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            symbols=reports,
            skipped=skipped,
        )

    def _exits_for(self, market: MarketSnapshot, decision: RegimeDecision, now_ms: int) -> list[OrderIntent]:
        pos = self.account.position_for(market.symbol)
        if pos is None:
            return []
        if pos.strategy is StrategyName.TREND:
            return self.trend.generate_exits(pos, market, decision, now_ms)
        return self.mr.generate_exits(pos, market, decision, now_ms)

    def _execute(self, intent: OrderIntent, now_ms: int, *, persist_paper: bool = False) -> None:
        if intent.extras.get("trail_update_only"):
            if persist_paper or not self.cfg.dry_run:
                self.broker.submit(intent, now_ms)
            return
        self.alerts.send(f"{intent.action.value} {intent.symbol} {intent.side.value} {intent.reason}")
        if self.cfg.dry_run:
            logger.info("DRY-RUN 意图: %s", _format_intent(intent))
            if persist_paper:
                self.broker.submit(intent, now_ms)
            return
        self.client.connect_sdk(for_trading=True)
        result = self.client.place(intent)
        logger.info("实盘下单返回: %s", result)

    def run_forever(self) -> None:
        logger.info(
            "启动循环 network=%s dry_run=%s symbols=%s interval=%ss",
            self.cfg.network,
            self.cfg.dry_run,
            ",".join(self.cfg.symbols),
            self.cfg.poll_seconds,
        )
        while True:
            report = self.scan_once(persist_paper=True)
            print(format_report(report), flush=True)
            time.sleep(max(5, self.cfg.poll_seconds))


def _format_intent(intent: OrderIntent) -> str:
    side = "买入开多" if intent.side is Side.LONG and intent.action.value == "open" else intent.side.value
    if intent.action.value == "open":
        side = "买入开多" if intent.side is Side.LONG else "卖出开空"
    return (
        f"{intent.action.value} {side} {intent.size:.6g} {intent.symbol} @ {intent.price:.6g} "
        f"({intent.kind.value}) stop={intent.stop_price} lev={intent.leverage}x "
        f"{'isolated' if intent.isolated else 'cross'} 风险=${intent.risk_usd:.2f}"
    )


def format_report(report: ScanReport) -> str:
    mode = "DRY-RUN 模拟" if report.dry_run else "LIVE 实盘"
    lines = [
        "",
        "=" * 88,
        f"Hyperliquid 策略扫描  [{mode}]  网络={report.network}  时间={report.generated_at}",
        f"账户权益: ${report.equity:,.2f}    标的: {', '.join(s.symbol for s in report.symbols)}",
        "=" * 88,
    ]
    for item in report.symbols:
        d = item.decision
        ema_part = ""
        if d.daily_ema_fast is not None and d.daily_ema_slow is not None:
            ema_part = f"EMA20={d.daily_ema_fast:.6g} EMA50={d.daily_ema_slow:.6g}"
        adx_part = f"日线ADX={_fmt(d.daily_adx)}  1hADX={_fmt(d.hourly_adx)}"
        lines.append(f"\n[{item.symbol}]  mid={item.mid:.6g}   制度={d.regime.value}")
        lines.append(f"  {ema_part}  {adx_part}")
        if d.range_structure:
            rs = d.range_structure
            lines.append(
                f"  区间结构: {'是' if rs.is_range else '否'}  "
                f"高={rs.high:.6g} 低={rs.low:.6g}  {rs.reason}"
            )
        for note in item.notes:
            lines.append(f"  · {note}")
        if item.signal:
            lines.append(
                f"  信号: {item.signal.side.value} / {item.signal.strategy.value} / "
                f"{item.signal.kind.value}  入场={item.signal.entry_price:.6g}  "
                f"止损={item.signal.stop_price:.6g}"
            )
            lines.append(f"       {item.signal.reason}")
        else:
            lines.append("  信号: 无")
        if item.intent:
            lines.append(f"  订单意图: {_format_intent(item.intent)}")
        for ex in item.exits:
            lines.append(f"  离场意图: {_format_intent(ex)} | {ex.reason}")
    for skip in report.skipped:
        lines.append(f"\n跳过 {skip}")
    lines.append("\n" + "=" * 88)
    lines.append("规则来源: docs/ 下 v1.1 趋势跟踪 + 均值回归。默认不下单。")
    lines.append("=" * 88 + "\n")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"
