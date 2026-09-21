from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from hl_bot.alerts import AlertSink
from hl_bot.config import BotConfig
from hl_bot.exchange.client import (
    ExchangePosition,
    HyperliquidClient,
    extract_oid,
    extract_order_fill,
    extract_resting_oids,
    is_open_entry_order,
    is_rate_limit_error,
    normalize_px,
    order_is_buy,
    order_limit_px,
    order_oid,
    prices_close,
    same_resting_entry,
)
from hl_bot.exchange.paper import PaperBroker
from hl_bot.models import (
    AccountState,
    IntentAction,
    MarketSnapshot,
    OrderIntent,
    Position,
    Regime,
    RegimeDecision,
    RestingEntry,
    Side,
    Signal,
    StrategyName,
)
from hl_bot.funding import (
    funding_against,
    funding_exit_enabled_for,
    funding_exit_threshold_for,
    make_funding_exit_intent,
    should_exit_on_funding_spike,
)
from hl_bot.regime import route_regime
from hl_bot.risk import RiskManager, apply_fill
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
        self._open_orders: list[dict] | None = None
        self._exchange_positions: dict[str, ExchangePosition] = {}
        self._live_book_ok = True

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
        if not self.cfg.dry_run:
            self.reconcile_live_protection(ctxs)
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
                if is_rate_limit_error(exc):
                    raise
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
            for item in exits:
                self._execute(item, now_ms, persist_paper=persist_paper)

            closing = any(
                e.action.value in {"close", "reduce"} and not e.extras.get("trail_update_only") for e in exits
            )
            signal: Signal | None = None
            intent: OrderIntent | None = None
            existing = self.account.position_for(symbol)
            if closing:
                notes.append("本轮已有离场意图，不再开新仓或金字塔加仓")
            elif existing is not None:
                if existing.strategy is StrategyName.TREND:
                    if existing.starter_pending():
                        signal = self.trend.generate_breakout_add(existing, market, decision, now_ms)
                        if signal is not None:
                            verdict = self.risk.evaluate_tier_add(signal, existing, self.account)
                            if verdict.allowed:
                                intent = self.risk.to_tier_add_intent(
                                    signal, existing, verdict, isolated=self.cfg.risk.isolated
                                )
                                if intent:
                                    self._execute(intent, now_ms, persist_paper=persist_paper)
                            else:
                                notes.append(f"突破补仓拒绝：{verdict.reason}")
                        else:
                            notes.append(
                                "已有趋势 starter，等待 4h Donchian 收盘突破补剩余仓"
                                "（禁止二次满仓开仓，突破未触发则沿用现有止损）"
                            )
                    else:
                        signal = self.trend.generate_pyramid(existing, market, decision, now_ms)
                        if signal is not None:
                            verdict = self.risk.evaluate_pyramid(signal, existing, self.account)
                            if verdict.allowed:
                                intent = self.risk.to_pyramid_intent(
                                    signal, existing, verdict, isolated=self.cfg.risk.isolated
                                )
                                if intent:
                                    self._execute(intent, now_ms, persist_paper=persist_paper)
                            else:
                                notes.append(f"金字塔拒绝：{verdict.reason}")
                        else:
                            notes.append("已有趋势仓，等待金字塔条件或离场（禁止摊平）")
                else:
                    notes.append("已有仓位，禁止摊平加仓")
            elif decision.regime.is_trend():
                signal = self.trend.generate_signal(market, decision, now_ms, existing=existing)
            elif decision.regime is Regime.MEAN_REVERSION:
                halted, why = self.mr.expansion_halt(market.h1, decision)
                if halted:
                    notes.append(why)
                else:
                    signal = self.mr.generate_signal(market, decision, now_ms)
            else:
                notes.append("中间地带/冲突：不新开仓")

            if signal is not None and existing is None and not closing:
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
            exits = self.trend.generate_exits(pos, market, decision, now_ms)
        else:
            exits = self.mr.generate_exits(pos, market, decision, now_ms)
        if any(e.action.value == "close" for e in exits):
            return exits
        enabled = funding_exit_enabled_for(
            pos,
            self.cfg.trend.funding_exit_enabled,
            self.cfg.mean_reversion.funding_exit_enabled,
        )
        spike, why = should_exit_on_funding_spike(
            pos,
            market.funding,
            threshold=funding_exit_threshold_for(
                pos,
                self.cfg.trend.funding_annual_exit,
                self.cfg.mean_reversion.funding_annual_exit,
            ),
            enabled=enabled,
        )
        if spike:
            price = market.mid or pos.entry_price
            return [make_funding_exit_intent(pos, price, why)]
        return exits

    def reconcile_live_protection(self, ctxs: dict | None = None) -> None:
        """每轮实盘扫描：resting 随后成交的仓位补上保护止损，并丢掉已不在盘口的挂单记录。"""
        if self.cfg.dry_run:
            return
        self._live_book_ok = False
        self.client.connect_sdk(for_trading=True)
        orders = self.client.fetch_open_orders()
        positions = self.client.fetch_perp_positions()
        self._open_orders = list(orders)
        self._exchange_positions = positions
        self._live_book_ok = True
        self._cancel_stacked_entries()
        self._drop_filled_resting(positions)
        self._align_open_sizes(positions)
        self._ensure_managed_stops(ctxs or {}, positions)
        self.broker.save()

    def _exchange_position(self, positions: dict[str, ExchangePosition], symbol: str) -> ExchangePosition | None:
        if symbol in positions:
            return positions[symbol]
        for name, pos in positions.items():
            if name.upper() == symbol.upper():
                return pos
        return None

    def _cancel_stacked_entries(self) -> None:
        """同一标的、同一方向、同一限价的入场/加仓挂单只留 oid 最小的一张。"""
        managed = {symbol.upper() for symbol in self.cfg.symbols}
        groups: dict[tuple[str, bool, float], list[dict]] = {}
        for order in self._open_orders or []:
            if not isinstance(order, dict):
                continue
            coin = str(order.get("coin") or order.get("symbol") or "")
            if coin.upper() not in managed:
                continue
            if not is_open_entry_order(order, coin):
                continue
            buy = order_is_buy(order)
            px = order_limit_px(order)
            if buy is None or px <= 0:
                continue
            groups.setdefault((coin.upper(), buy, normalize_px(px)), []).append(order)
        cancelled: set[int] = set()
        for rows in groups.values():
            if len(rows) < 2:
                continue
            rows.sort(key=lambda row: order_oid(row) or 0)
            for extra in rows[1:]:
                oid = order_oid(extra)
                coin = str(extra.get("coin") or extra.get("symbol") or "")
                if oid is None or not coin:
                    continue
                cancelled.update(self.client._cancel_oids(coin, [oid]))
        if not cancelled:
            return
        logger.info("撤销同价重复入场/加仓挂单: %s", sorted(cancelled))
        self._open_orders = [row for row in (self._open_orders or []) if order_oid(row) not in cancelled]
        self.account.resting_entries = [
            rec for rec in self.account.resting_entries if rec.oid not in cancelled
        ]

    def _drop_filled_resting(self, positions: dict[str, ExchangePosition]) -> None:
        open_oids = {oid for oid in (order_oid(row) for row in self._open_orders or []) if oid is not None}
        kept: list[RestingEntry] = []
        for rec in self.account.resting_entries:
            if rec.oid is not None and rec.oid in open_oids:
                kept.append(rec)
                continue
            if rec.oid is None and self._book_has_resting(rec):
                kept.append(rec)
                continue
            ex = self._exchange_position(positions, rec.symbol)
            if ex is not None and ex.side.value == rec.side:
                self._adopt_resting_fill(rec, ex)
        self.account.resting_entries = kept

    def _book_has_resting(self, rec: RestingEntry) -> bool:
        is_buy = rec.side == Side.LONG.value
        for order in self._open_orders or []:
            if same_resting_entry(order, symbol=rec.symbol, is_buy=is_buy, price=rec.price):
                return True
        return False

    def _adopt_resting_fill(self, rec: RestingEntry, ex: ExchangePosition) -> None:
        """resting 单已离开盘口且持仓变大：视为随后成交，补上本地仓位。"""
        tol = max(1e-8, rec.position_size_at_submit * 0.02, 10 ** -4)
        if ex.size <= rec.position_size_at_submit + tol:
            return
        local = self.account.position_for(rec.symbol)
        if local is None:
            if not rec.stop_price or rec.stop_price <= 0:
                logger.warning("%s resting 已成交但没有策略止损价，不臆造保护单", rec.symbol)
                return
            pos = Position(
                symbol=rec.symbol,
                strategy=StrategyName(rec.strategy),
                side=Side(rec.side),
                size=ex.size,
                entry_price=ex.entry_price or rec.price,
                stop_price=rec.stop_price,
                leverage=rec.leverage,
                opened_ts=int(time.time() * 1000),
                tag=rec.tag,
                extras=dict(rec.extras),
            )
            pos.extras.setdefault("_opened_size", ex.size)
            apply_fill(self.account, pos)
            logger.info("%s resting %s 已成交，纳入本地仓位 size=%.6g", rec.symbol, rec.action, ex.size)
            return
        if local.side is not ex.side:
            return
        if ex.size > local.size + max(local.size * 0.02, 1e-8):
            logger.info(
                "%s 交易所仓 %.6g 大于本地 %.6g（resting 随后成交），按交易所张数对齐",
                rec.symbol,
                ex.size,
                local.size,
            )
            local.size = ex.size

    def _align_open_sizes(self, positions: dict[str, ExchangePosition]) -> None:
        """本地已在管的仓，张数以交易所为准，避免止损和后续加仓按过期的小仓计算。"""
        for local in list(self.account.open_positions()):
            ex = self._exchange_position(positions, local.symbol)
            if ex is None or ex.side is not local.side:
                continue
            tol = max(local.size * 0.02, 1e-6)
            if ex.size > local.size + tol:
                logger.info(
                    "%s 交易所仓 %.6g 大于本地 %.6g，按交易所张数对齐",
                    local.symbol,
                    ex.size,
                    local.size,
                )
                local.size = ex.size

    def _ensure_managed_stops(self, ctxs: dict, positions: dict[str, ExchangePosition]) -> None:
        symbols = list(self.cfg.symbols)
        for pos in self.account.open_positions():
            if pos.symbol not in symbols:
                symbols.append(pos.symbol)
        for symbol in symbols:
            try:
                self._ensure_one_stop(symbol, ctxs, positions)
            except Exception as exc:
                if is_rate_limit_error(exc):
                    raise
                logger.exception("对账 %s 保护止损失败", symbol)

    def _ensure_one_stop(self, symbol: str, ctxs: dict, positions: dict[str, ExchangePosition]) -> None:
        ex = self._exchange_position(positions, symbol)
        local = self.account.position_for(symbol)
        if ex is None or ex.size <= 0:
            return
        if local is None:
            logger.warning("%s 交易所有仓 %.6g 但本地无策略仓，不臆造止损价", symbol, ex.size)
            return
        if local.side is not ex.side:
            logger.warning(
                "%s 本地方向 %s 与交易所 %s 不一致，跳过止损对账",
                symbol,
                local.side.value,
                ex.side.value,
            )
            return
        if not local.stop_price or local.stop_price <= 0:
            return
        meta = ctxs.get(symbol) or ctxs.get(symbol.upper()) or {}
        decimals = int(meta.get("sz_decimals") or 4)
        outcome = self.client.ensure_protective_stop(
            symbol,
            ex.side,
            ex.size,
            local.stop_price,
            sz_decimals=decimals,
            open_orders=self._open_orders,
        )
        oid = outcome.get("oid")
        if oid is not None:
            local.extras["sl_oid"] = int(oid)

    def _resting_block_reason(self, intent: OrderIntent) -> str | None:
        """同标的同档，或交易所已有同向同价的非 reduce-only 挂单，则不再叠一张。"""
        if intent.action not in (IntentAction.OPEN, IntentAction.ADD) or intent.reduce_only:
            return None
        tier = _intent_tier(intent)
        book = self._open_orders
        orders = list(book or [])
        for rec in self.account.resting_entries:
            if rec.symbol.upper() != intent.symbol.upper():
                continue
            if book is None:
                # 还没读到本轮挂单：本地记录在，就先不要再叠一张
                if rec.tier == tier or (
                    rec.side == intent.side.value and prices_close(rec.price, float(intent.price))
                ):
                    return f"本地仍记录 resting tier={rec.tier} oid={rec.oid}"
                continue
            still_open = rec.oid is None or any(order_oid(row) == rec.oid for row in orders)
            if not still_open:
                continue
            if rec.tier == tier:
                return f"同档 {tier} oid={rec.oid}"
            if rec.side == intent.side.value and prices_close(rec.price, float(intent.price)):
                return f"同价 {intent.price} oid={rec.oid}"
        is_buy = intent.side is Side.LONG
        for order in orders:
            if same_resting_entry(order, symbol=intent.symbol, is_buy=is_buy, price=float(intent.price)):
                return f"交易所已有同价挂单 oid={order_oid(order)}"
        return None

    def _remember_resting(self, intent: OrderIntent, result: object) -> None:
        if intent.action not in (IntentAction.OPEN, IntentAction.ADD) or intent.reduce_only:
            return
        oids = _kept_resting_oids(result)
        if not oids:
            return
        local = self.account.position_for(intent.symbol)
        submitted = float(local.size) if local is not None else 0.0
        ex = self._exchange_position(self._exchange_positions, intent.symbol)
        if ex is not None and ex.side is intent.side:
            submitted = ex.size
        tier = _intent_tier(intent)
        rec = RestingEntry(
            symbol=intent.symbol,
            tier=tier,
            action=intent.action.value,
            side=intent.side.value,
            price=float(intent.price),
            size=float(intent.size),
            stop_price=float(intent.stop_price) if intent.stop_price else None,
            oid=oids[0],
            strategy=intent.strategy.value,
            leverage=int(intent.leverage),
            isolated=bool(intent.isolated),
            position_size_at_submit=submitted,
            tag=str(intent.extras.get("tag") or ""),
            extras=_json_safe(dict(intent.extras)),
        )
        self.account.resting_entries = [
            item
            for item in self.account.resting_entries
            if not (item.symbol.upper() == rec.symbol.upper() and item.tier == rec.tier)
        ]
        self.account.resting_entries.append(rec)
        logger.info(
            "%s 记录 resting %s tier=%s oid=%s px=%s，后续扫描不重复挂",
            intent.symbol,
            intent.action.value,
            tier,
            rec.oid,
            rec.price,
        )

    def _forget_resting(self, intent: OrderIntent) -> None:
        tier = _intent_tier(intent)
        self.account.resting_entries = [
            item
            for item in self.account.resting_entries
            if not (item.symbol.upper() == intent.symbol.upper() and item.tier == tier)
        ]

    def _execute(self, intent: OrderIntent, now_ms: int, *, persist_paper: bool = False) -> None:
        if intent.extras.get("trail_update_only"):
            if persist_paper or not self.cfg.dry_run:
                self.broker.submit(intent, now_ms)
            return
        if not self.cfg.dry_run and intent.action in (IntentAction.OPEN, IntentAction.ADD) and not intent.reduce_only:
            if not self._live_book_ok:
                logger.warning("%s 本轮未读到挂单，跳过 %s 以免同价堆叠", intent.symbol, intent.action.value)
                return
            blocked = self._resting_block_reason(intent)
            if blocked:
                logger.info("%s 已有 resting %s，跳过本轮以免堆叠（%s）", intent.symbol, intent.action.value, blocked)
                return
        pos_side = intent.position_side()
        tag = str(intent.extras.get("tag") or intent.extras.get("tier") or "")
        tier = str(intent.extras.get("tier") or tag)
        self.alerts.send(
            f"{intent.action.value} {intent.symbol} {pos_side.value} "
            f"({_action_side_label(intent)}) tier={tier or '-'} tag={tag or '-'} "
            f"size={intent.size:.6g} @ {intent.price:.6g} {intent.reason}"
        )
        if self.cfg.dry_run:
            logger.info("DRY-RUN 意图: %s", _format_intent(intent))
            if persist_paper:
                self.broker.submit(intent, now_ms)
            return
        self.client.connect_sdk(for_trading=True)
        result = self.client.place(intent)
        logger.info("实盘下单返回: %s", result)
        self._sync_live_fill(intent, result, now_ms)

    def run_forever(self) -> None:
        logger.info(
            "启动循环 network=%s dry_run=%s symbols=%s interval=%ss",
            self.cfg.network,
            self.cfg.dry_run,
            ",".join(self.cfg.symbols),
            self.cfg.poll_seconds,
        )
        backoff = 2.0
        while True:
            try:
                report = self.scan_once(persist_paper=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if not is_rate_limit_error(exc):
                    raise
                logger.warning("本轮扫描遇到 429，%.1fs 后重试，进程不退出: %s", backoff, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 120.0)
                continue
            print(format_report(report), flush=True)
            backoff = 2.0
            time.sleep(max(5, self.cfg.poll_seconds))

    def _sync_live_fill(self, intent: OrderIntent, result: object, now_ms: int) -> None:
        """实盘仅在确认成交后同步本地仓位；place 返回 None / 无 fill 不得假装已平。"""
        fill = extract_order_fill(result)
        if fill is None or fill.size <= 0:
            self._remember_resting(intent, result)
            logger.warning("实盘未确认成交（place=%s），不更新本地仓位", result)
            self.broker.save()
            return
        self._forget_resting(intent)
        sync_intent = intent.with_size(fill.size)
        if fill.price:
            sync_intent = replace(sync_intent, price=fill.price)
        self.broker.submit(sync_intent, now_ms)
        if isinstance(result, dict):
            sl_oid = result.get("stop_oid")
            if sl_oid is None:
                sl_oid = extract_oid(result.get("stop"))
            pos = self.account.position_for(intent.symbol)
            if pos is not None and sl_oid is not None:
                pos.extras["sl_oid"] = int(sl_oid)
        # scan --live 默认不 persist_paper，成交后仍要落盘，避免下一轮重复平已空仓
        self.broker.save()


def _intent_tier(intent: OrderIntent) -> str:
    return str(intent.extras.get("tier") or intent.extras.get("tag") or intent.action.value)


def _kept_resting_oids(result: object) -> list[int]:
    if isinstance(result, dict) and isinstance(result.get("resting_oids"), list):
        oids: list[int] = []
        for raw in result["resting_oids"]:
            try:
                oids.append(int(raw))
            except (TypeError, ValueError):
                continue
        return oids
    return extract_resting_oids(result)


def _json_safe(value: object) -> dict:
    if not isinstance(value, dict):
        return {}

    def _convert(item: object) -> object:
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        if isinstance(item, dict):
            return {str(key): _convert(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [_convert(val) for val in item]
        return str(item)

    converted = _convert(value)
    return converted if isinstance(converted, dict) else {}


def _action_side_label(intent: OrderIntent) -> str:
    pos = intent.position_side()
    if intent.action is IntentAction.OPEN:
        return "买入开多" if pos is Side.LONG else "卖出开空"
    if intent.action is IntentAction.ADD:
        if intent.extras.get("tier_add") or intent.extras.get("breakout_add"):
            return "突破补仓加多" if pos is Side.LONG else "突破补仓加空"
        return "金字塔加多" if pos is Side.LONG else "金字塔加空"
    if intent.action is IntentAction.CLOSE:
        return "平多" if pos is Side.LONG else "平空"
    if intent.action is IntentAction.REDUCE:
        return "减多" if pos is Side.LONG else "减空"
    return pos.value


def _format_intent(intent: OrderIntent) -> str:
    side = _action_side_label(intent)
    tag = str(intent.extras.get("tag") or intent.extras.get("tier") or "")
    tier = str(intent.extras.get("tier") or tag)
    return (
        f"{intent.action.value} {side} {intent.size:.6g} {intent.symbol} @ {intent.price:.6g} "
        f"({intent.kind.value}) tier={tier or '-'} tag={tag or '-'} "
        f"stop={intent.stop_price} lev={intent.leverage}x "
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
                f"{item.signal.kind.value} / tag={item.signal.tag or '-'}  "
                f"入场={item.signal.entry_price:.6g}  止损={item.signal.stop_price:.6g}"
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
