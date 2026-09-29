from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from hl_bot.alerts import AlertSink
from hl_bot.btc_regime import BtcRegimeGate, shadow_log_path
from hl_bot.config import BotConfig
from hl_bot.exchange.client import (
    AccountSnapshot,
    ExchangePosition,
    HyperliquidClient,
    extract_oid,
    extract_order_fill,
    extract_resting_oids,
    is_open_entry_order,
    is_reduce_only_stop_order,
    order_trigger_px,
    is_rate_limit_error,
    is_transient_network_error,
    normalize_px,
    order_is_buy,
    order_limit_px,
    order_oid,
    prices_close,
    same_resting_entry,
    short_errors,
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
from hl_bot.risk import (
    RiskManager,
    apply_fill,
    apply_pyramid,
    apply_tier_add,
    clamp_stop_for_leverage,
)
from hl_bot.strategies.mean_reversion import MeanReversionStrategy
from hl_bot.strategies.trend import TrendStrategy

logger = logging.getLogger(__name__)


# 权益缓存：可接受的最大年龄；单轮下跌超过该比例且无已实现亏损视为可疑读数
EQUITY_CACHE_MAX_AGE_MS = 24 * 3600 * 1000
EQUITY_SUSPECT_DROP_PCT = 0.5
# 可疑读数连续两轮在该相对误差内一致则接受（避免真实大跌时永久卡在缓存）
EQUITY_SUSPECT_CONFIRM_TOL = 0.10
FOUR_HOURS_MS = 4 * 3_600_000
# 幽灵仓查成交：最多回看天数；查询失败最多推迟几轮再清
GHOST_FILL_LOOKBACK_MS = 7 * 24 * 3_600_000
GHOST_FILL_MAX_DEFER = 3


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
        self.btc_regime = BtcRegimeGate(cfg.trend, self.client, shadow_log_path(cfg.state_path))
        day_key, week_key, _ = _utc_keys()
        self.broker = PaperBroker.load(cfg.state_path, cfg.paper_equity, day_key, week_key)
        self._open_orders: list[dict] | None = None
        self._exchange_positions: dict[str, ExchangePosition] = {}
        self._asset_ctxs: dict = {}
        self._live_book_ok = True
        # 本轮权益是否可信（实时读数或新鲜缓存）；False 时只管理止损/离场，不开新仓/加仓
        self._equity_ok = True
        # 上一轮被判为可疑的权益读数；连续两轮一致则视为真实变化而接受
        self._suspect_equity: float | None = None
        self._reconcile_ctxs: dict = {}
        if self.account.equity_abstraction and hasattr(self.client, "last_abstraction"):
            if not getattr(self.client, "last_abstraction", None):
                self.client.last_abstraction = self.account.equity_abstraction

    @property
    def account(self) -> AccountState:
        return self.broker.account

    def scan_once(self, *, persist_paper: bool = False) -> ScanReport:
        self.client.begin_scan()
        try:
            return self._scan_once_inner(persist_paper=persist_paper)
        finally:
            self.client.end_scan()

    def _scan_once_inner(self, *, persist_paper: bool = False) -> ScanReport:
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
            snapshot = self.client.fetch_account_snapshot()
            self._refresh_live_equity(now_ms, snapshot)

        ctxs = self.client.fetch_asset_contexts()
        self._asset_ctxs = ctxs
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

            cooldown = None
            if signal is not None and existing is None and not closing:
                cooldown = self.stop_cooldown_reason(symbol, signal.side, now_ms)
                if cooldown:
                    notes.append(f"止损冷却拒绝：{cooldown}")
                    logger.info("%s 新开仓被止损冷却拒绝：%s", symbol, cooldown)
            if signal is not None and existing is None and not closing and not cooldown:
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
                    # 新趋势开仓（含 starter）在其它检查通过、真正下单之前看 BTC 日线 ADX。
                    # mode=off 不进入，不拉 K 线，订单路径与未加此功能时相同。
                    if (
                        intent is not None
                        and self.btc_regime.mode != "off"
                        and signal.strategy is StrategyName.TREND
                        and decision.regime.is_trend()
                    ):
                        try:
                            if self.btc_regime.blocks_new_entry(signal.symbol, signal.side.value, now_ms, notes):
                                intent = None
                        except Exception as exc:
                            logger.warning("BTC regime 判断异常，fail-open 不拦截 %s: %s", symbol, exc)
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

    def _cached_equity(self, now_ms: int) -> tuple[float, float] | None:
        """返回 (缓存权益, 距今小时数)；无缓存或超过 EQUITY_CACHE_MAX_AGE_MS 返回 None。"""
        acct = self.account
        if acct.last_good_equity <= 0 or acct.last_good_equity_ts_ms <= 0:
            return None
        age_ms = now_ms - acct.last_good_equity_ts_ms
        if age_ms < 0 or age_ms > EQUITY_CACHE_MAX_AGE_MS:
            return None
        return acct.last_good_equity, age_ms / 3_600_000.0

    def _realized_loss_since_last_good(self) -> float:
        acct = self.account
        if acct.last_good_day_key and acct.last_good_day_key == acct.day_key:
            delta = acct.realized_pnl_today - acct.last_good_realized_pnl
        else:
            delta = acct.realized_pnl_today
        return max(0.0, -delta)

    def _is_suspect_equity(self, value: float, now_ms: int) -> bool:
        """单轮权益较上次可信值下跌 >50% 且无对应已实现亏损 → 可疑读数。"""
        cached = self._cached_equity(now_ms)
        if cached is None:
            return False
        last_good, _ = cached
        unexplained_drop = last_good - self._realized_loss_since_last_good() - value
        return unexplained_drop > last_good * EQUITY_SUSPECT_DROP_PCT

    def _accept_equity(self, value: float, now_ms: int, abstraction: str | None) -> None:
        acct = self.account
        acct.equity = value
        acct.last_good_equity = value
        acct.last_good_equity_ts_ms = now_ms
        acct.last_good_realized_pnl = acct.realized_pnl_today
        acct.last_good_day_key = acct.day_key
        if abstraction:
            acct.equity_abstraction = str(abstraction)
        self._suspect_equity = None
        self._equity_ok = True

    def _refresh_live_equity(self, now_ms: int, snapshot: AccountSnapshot | None = None) -> None:
        """实盘权益：完整读数才采用；失败/部分/可疑时用 24h 内缓存，否则本轮禁止开新仓/加仓。"""
        self._equity_ok = True
        reading = None
        try:
            if snapshot is None:
                reading = self.client.read_live_equity()
            else:
                reading = self.client.read_live_equity(snapshot)
        except Exception as exc:
            logger.warning("读取实盘权益异常: %s", exc)

        if reading is not None and reading.complete and reading.equity > 0:
            value = float(reading.equity)
            if not self._is_suspect_equity(value, now_ms):
                self._accept_equity(value, now_ms, reading.abstraction)
                return
            prev = self._suspect_equity
            if prev is not None and prev > 0 and abs(value - prev) / prev <= EQUITY_SUSPECT_CONFIRM_TOL:
                logger.warning(
                    "权益 $%.2f 连续两轮一致（上轮 $%.2f），较缓存 $%.2f 大幅下降但视为真实变化并接受",
                    value,
                    prev,
                    self.account.last_good_equity,
                )
                self._accept_equity(value, now_ms, reading.abstraction)
                return
            self._suspect_equity = value
            reason = (
                f"权益读数 ${value:.2f} 较上次可信值 ${self.account.last_good_equity:.2f} 单轮下跌超过"
                f" {EQUITY_SUSPECT_DROP_PCT:.0%} 且无对应已实现亏损，判为可疑读数"
            )
        elif reading is None:
            reason = "权益读取异常"
        else:
            reason = f"权益读取不完整（{'; '.join(short_errors(list(reading.errors))) or '-'}，部分读数不采用）"
            self._suspect_equity = None

        cached = self._cached_equity(now_ms)
        if cached is not None:
            value, age_h = cached
            self.account.equity = value
            logger.warning("%s → 使用缓存权益 $%.2f（%.1f 小时前读取）", reason, value, age_h)
            return
        self._equity_ok = False
        logger.warning(
            "%s，且无 24h 内有效缓存权益 → 本轮不开新仓/加仓，仅管理止损与离场（沿用权益 $%.2f 仅作展示）",
            reason,
            self.account.equity,
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
        self._reconcile_ctxs = dict(ctxs or {})
        self.client.connect_sdk(for_trading=True)
        orders = self.client.fetch_open_orders()
        positions = self.client.fetch_perp_positions()
        self._open_orders = list(orders)
        self._exchange_positions = positions
        self._live_book_ok = True
        self._cancel_stacked_entries()
        self._drop_filled_resting(positions)
        self._align_open_sizes(positions)
        self._align_managed_leverage(positions)
        self._ensure_stops_before_liq(positions)
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

    def _mark_px(self, symbol: str) -> float:
        meta = self._reconcile_ctxs.get(symbol) or self._reconcile_ctxs.get(symbol.upper()) or {}
        try:
            return float(meta.get("mark_px") or meta.get("mid_px") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _stop_wrong_side(side: Side, stop: float, mark: float) -> bool:
        """止损已在现价错误一侧（挂上去会立刻触发）。mark 未知时不判定。"""
        if mark <= 0 or stop <= 0:
            return False
        return stop >= mark if side is Side.LONG else stop <= mark

    def _adopt_resting_fill(self, rec: RestingEntry, ex: ExchangePosition) -> None:
        """resting 入场/加仓单已离开盘口且持仓变大：视为随后成交。

        - 均价以交易所 entryPx 为准；
        - 首仓：止损按真实成交价平移（保持信号止损距离）；
        - Donchian 补仓：更新分层计数（tier_add_done 等），共用止损；
        - 金字塔：更新加仓计数，并按真实均价重算保本止损；
        - 按交易所实际止损核算风险，超过单笔风险上限（2% 权益）则收紧；
        随后同一轮 _ensure_managed_stops 会把 reduce-only 止损替换为覆盖整仓。
        """
        tol = max(1e-8, rec.position_size_at_submit * 0.02, 10 ** -4)
        if ex.size <= rec.position_size_at_submit + tol:
            return
        local = self.account.position_for(rec.symbol)
        side = Side(rec.side)
        if local is None:
            if not rec.stop_price or rec.stop_price <= 0:
                logger.warning("%s resting 已成交但没有策略止损价，不臆造保护单", rec.symbol)
                return
            entry = float(ex.entry_price or rec.price)
            stop = float(rec.stop_price)
            if rec.price > 0 and entry > 0:
                distance = abs(rec.price - stop)
                stop = entry - distance if side is Side.LONG else entry + distance
                stop, _ = clamp_stop_for_leverage(
                    side=side,
                    entry=entry,
                    stop=stop,
                    leverage=max(1, int(self.cfg.risk.target_leverage)),
                    buffer_frac=float(self.cfg.risk.liq_buffer_frac),
                )
            pos = Position(
                symbol=rec.symbol,
                strategy=StrategyName(rec.strategy),
                side=side,
                size=ex.size,
                entry_price=entry,
                stop_price=stop,
                leverage=rec.leverage,
                opened_ts=int(time.time() * 1000),
                tag=rec.tag,
                extras=dict(rec.extras),
            )
            pos.extras["_opened_size"] = ex.size
            apply_fill(self.account, pos)
            logger.info(
                "%s resting %s 已成交，纳入本地仓位 size=%.6g 均价=%.6g（挂单价 %.6g）止损=%.6g（原 %.6g）",
                rec.symbol,
                rec.action,
                ex.size,
                entry,
                rec.price,
                stop,
                rec.stop_price,
            )
            self._cap_position_risk(pos)
            return
        if local.side is not ex.side:
            return
        if ex.size <= local.size + max(local.size * 0.02, 1e-8):
            return
        old_size = local.size
        old_entry = local.entry_price
        add_size = ex.size - old_size
        real_entry = float(ex.entry_price or 0.0)
        if real_entry <= 0:
            real_entry = (old_entry * old_size + rec.price * add_size) / ex.size
        add_px = (real_entry * ex.size - old_entry * old_size) / add_size if add_size > 0 else rec.price
        if add_px <= 0:
            add_px = rec.price
        is_tier = bool(rec.extras.get("tier_add") or rec.extras.get("breakout_add")) or rec.tier == "donchian_close_breakout"
        is_pyramid = bool(rec.extras.get("pyramid")) or rec.tier == "pyramid_add"
        old_stop = local.stop_price
        if is_tier and local.starter_pending():
            apply_tier_add(self.account, local, add_size, add_px, local.stop_price)
            kind = "Donchian 补仓"
        elif is_pyramid:
            current = local.stop_price
            if rec.stop_price and rec.stop_price > 0:
                current = max(current, rec.stop_price) if local.side is Side.LONG else min(current, rec.stop_price)
            atr_v = float(rec.extras.get("atr") or 0.0)
            new_stop = self.trend.pyramid_breakeven_stop(real_entry, atr_v, local.side, current)
            mark = self._mark_px(local.symbol)
            if self._stop_wrong_side(local.side, new_stop, mark):
                logger.warning(
                    "%s 金字塔成交后保本止损 %.6g 已在现价 %.6g 错误一侧，暂保留原止损 %.6g",
                    local.symbol,
                    new_stop,
                    mark,
                    local.stop_price,
                )
                new_stop = local.stop_price
            apply_pyramid(self.account, local, add_size, add_px, new_stop)
            kind = "金字塔加仓"
        else:
            local.size = ex.size
            kind = "加仓"
        local.size = ex.size
        local.entry_price = real_entry
        logger.info(
            "%s resting %s 随后成交：size %.6g→%.6g，均价 %.6g→%.6g（交易所 entryPx，加仓段≈%.6g），"
            "止损 %.6g→%.6g，pyramid_count=%s tier_add_done=%s",
            rec.symbol,
            kind,
            old_size,
            ex.size,
            old_entry,
            real_entry,
            add_px,
            old_stop,
            local.stop_price,
            local.extras.get("pyramid_count", 0),
            bool(local.extras.get("tier_add_done")),
        )
        self._cap_position_risk(local)

    def _exchange_stop_px(self, symbol: str, side: Side) -> float | None:
        """交易所上该币 reduce-only 止损触发价；多张时取风险最大（最宽）的一张。"""
        best: float | None = None
        for order in self._open_orders or []:
            if not isinstance(order, dict) or not is_reduce_only_stop_order(order, symbol):
                continue
            px = order_trigger_px(order)
            if px <= 0:
                continue
            if best is None:
                best = px
            elif side is Side.LONG:
                best = min(best, px)
            else:
                best = max(best, px)
        return best

    def _risk_pct_for(self, pos: Position) -> float:
        if pos.strategy is StrategyName.MEAN_REVERSION:
            return float(self.cfg.mean_reversion.risk_pct)
        pct = float(self.cfg.trend.risk_pct)
        if pos.side is Side.SHORT and pos.symbol.upper() == "BTC":
            pct *= float(self.cfg.trend.btc_short_risk_mult)
        return pct

    def _cap_position_risk(self, pos: Position) -> None:
        """按交易所实际止损（没有则本地止损）核算该仓风险；超过 risk_pct×权益则收紧本地止损。"""
        equity = float(self.account.equity or 0.0)
        if equity <= 0 or not self._equity_ok or pos.size <= 0 or pos.entry_price <= 0:
            return
        ex_stop = self._exchange_stop_px(pos.symbol, pos.side)
        candidates = [px for px in (ex_stop, pos.stop_price) if px and px > 0]
        if not candidates:
            return
        # 取两者里更宽的一个：本轮替换前真正挂着的是交易所那张
        stop_for_risk = min(candidates) if pos.side is Side.LONG else max(candidates)
        if pos.side is Side.LONG:
            risk = max(0.0, (pos.entry_price - stop_for_risk) * pos.size)
        else:
            risk = max(0.0, (stop_for_risk - pos.entry_price) * pos.size)
        limit = equity * self._risk_pct_for(pos)
        if risk <= limit * (1.0 + 1e-6):
            return
        per_unit = limit / pos.size
        cap_stop = pos.entry_price - per_unit if pos.side is Side.LONG else pos.entry_price + per_unit
        if pos.side is Side.LONG:
            new_stop = max(pos.stop_price, cap_stop)
        else:
            new_stop = min(pos.stop_price, cap_stop)
        mark = self._mark_px(pos.symbol)
        if self._stop_wrong_side(pos.side, new_stop, mark):
            logger.warning(
                "%s 风险 $%.2f 超过上限 $%.2f，但收紧后的止损 %.6g 已在现价 %.6g 错误一侧，不自动收紧",
                pos.symbol,
                risk,
                limit,
                new_stop,
                mark,
            )
            return
        if abs(new_stop - pos.stop_price) <= 1e-12:
            return
        logger.warning(
            "%s 按交易所止损 %.6g 核算风险 $%.2f > %.1f%% 权益 $%.2f，收紧止损 %.6g → %.6g",
            pos.symbol,
            stop_for_risk,
            risk,
            self._risk_pct_for(pos) * 100,
            limit,
            pos.stop_price,
            new_stop,
        )
        pos.stop_price = new_stop
        pos.extras["stop_tightened_for_risk"] = True

    def _align_open_sizes(self, positions: dict[str, ExchangePosition]) -> None:
        """本地已在管的仓与交易所对账：无仓则清幽灵，张数以交易所为准。"""
        for local in list(self.account.open_positions()):
            ex = self._exchange_position(positions, local.symbol)
            if ex is None or ex.size <= 0:
                if self._resolve_flat_position(local):
                    self.account.positions = [p for p in self.account.positions if p is not local]
                continue
            if ex.side is not local.side:
                logger.warning(
                    "%s 本地方向 %s 与交易所 %s 不一致，清除本地幽灵仓",
                    local.symbol,
                    local.side.value,
                    ex.side.value,
                )
                self.account.positions = [p for p in self.account.positions if p is not local]
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
            if ex.entry_price and ex.entry_price > 0 and local.entry_price > 0:
                if abs(ex.entry_price - local.entry_price) / local.entry_price > 1e-4:
                    logger.info(
                        "%s 本地入场价 %.6g 与交易所 entryPx %.6g 不一致，以交易所为准",
                        local.symbol,
                        local.entry_price,
                        ex.entry_price,
                    )
                local.entry_price = float(ex.entry_price)
            local.extras.pop("ghost_fill_checks", None)

    def _resolve_flat_position(self, local: Position) -> bool:
        """交易所已无仓：先查 userFillsByTime 判定是否本方止损/平仓成交，再决定标签。

        返回 True 表示可以清除本地仓；查询失败时最多推迟 GHOST_FILL_MAX_DEFER 轮。
        """
        now_ms = int(time.time() * 1000)
        opened = int(local.opened_ts or 0)
        start = max(opened - 60_000, now_ms - GHOST_FILL_LOOKBACK_MS) if opened > 0 else now_ms - GHOST_FILL_LOOKBACK_MS
        sl_oid = local.extras.get("sl_oid")
        try:
            fills = self.client.fetch_user_fills_by_time(start)
        except Exception as exc:
            checks = int(local.extras.get("ghost_fill_checks") or 0) + 1
            local.extras["ghost_fill_checks"] = checks
            if checks < GHOST_FILL_MAX_DEFER:
                logger.warning(
                    "%s 交易所已无仓，但查询成交记录失败（%s），第 %d 次，下一轮再核对后清除",
                    local.symbol,
                    exc,
                    checks,
                )
                return False
            logger.warning(
                "%s 交易所已无仓且连续 %d 次查不到成交记录，清除本地仓（未确认原因） sl_oid=%s",
                local.symbol,
                checks,
                sl_oid,
            )
            return True
        closing = close_fills_for(fills, local)
        if not closing:
            logger.warning(
                "%s 交易所已无仓但本地仍有 %.6g %s，且未找到本方平仓成交，清除幽灵仓（疑似强平/外部平仓） sl_oid=%s",
                local.symbol,
                local.size,
                local.side.value,
                sl_oid,
            )
            return True
        qty = sum(_f(row.get("sz")) for row in closing)
        vwap = sum(_f(row.get("px")) * _f(row.get("sz")) for row in closing) / qty if qty > 0 else 0.0
        pnl = sum(_f(row.get("closedPnl")) - _f(row.get("fee")) for row in closing)
        last_ts = max(int(_f(row.get("time"))) for row in closing)
        own_stop = sl_oid is not None and any(_same_oid(row.get("oid"), sl_oid) for row in closing)
        liquidated = any(row.get("liquidation") for row in closing)
        if liquidated and not own_stop:
            label = "强平成交"
        elif own_stop:
            label = "止损成交"
        elif _stop_side_fill(local, vwap):
            label = "止损成交"
        else:
            label = "外部平仓成交"
        self.account.realized_pnl_today += pnl
        self._record_stop(local.symbol, local.side, vwap, pnl, last_ts, label=label)
        logger.warning(
            "%s %s：%s %.6g @ 均价 %.6g，已实现盈亏 $%.4f（含手续费），sl_oid=%s 本方止损=%s 强平=%s；清除本地仓",
            local.symbol,
            label,
            local.side.value,
            qty,
            vwap,
            pnl,
            sl_oid,
            own_stop,
            liquidated,
        )
        self.alerts.send(f"{local.symbol} {label} {local.side.value} {qty:.6g} @ {vwap:.6g} pnl=${pnl:.4f}")
        return True

    def _record_stop(
        self,
        symbol: str,
        side: Side,
        price: float,
        pnl: float,
        ts_ms: int,
        *,
        label: str = "止损",
    ) -> None:
        self.account.last_stops[symbol.upper()] = {
            "ts": int(ts_ms),
            "side": side.value,
            "price": float(price),
            "pnl": float(pnl),
            "label": label,
        }

    def stop_cooldown_reason(self, symbol: str, side: Side, now_ms: int) -> str | None:
        """止损后同标的同方向：至少再收 N 根 4h K 线才允许新开仓。"""
        bars = int(getattr(self.cfg.trend, "stop_cooldown_bars_4h", 0) or 0)
        if bars <= 0:
            return None
        rec = self.account.last_stops.get(symbol.upper())
        if not rec:
            return None
        if str(rec.get("side") or "") != side.value:
            return None
        stop_ts = int(rec.get("ts") or 0)
        if stop_ts <= 0:
            return None
        first_boundary = -(-stop_ts // FOUR_HOURS_MS) * FOUR_HOURS_MS
        closed = max(0, (now_ms - first_boundary) // FOUR_HOURS_MS)
        if closed >= bars:
            return None
        ready_ms = first_boundary + bars * FOUR_HOURS_MS
        ready = datetime.fromtimestamp(ready_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"{symbol} {side.value} 于 {datetime.fromtimestamp(stop_ts / 1000, tz=timezone.utc):%Y-%m-%d %H:%M} UTC "
            f"{rec.get('label') or '止损'} @ {float(rec.get('price') or 0):.6g}，"
            f"之后已收盘 4h K 线 {closed}/{bars} 根，{ready} 后才允许同向再开"
        )

    def _ensure_margin_for_target_leverage(
        self,
        symbol: str,
        ex: ExchangePosition,
        target: int,
        *,
        force_extra: float = 0.5,
    ) -> None:
        """逐仓降杠杆前补足保证金，否则交易所会拒绝 decrease leverage。"""
        if self.client._exchange is None or not self.cfg.risk.isolated:
            return
        entry = float(ex.entry_price or 0.0)
        if entry <= 0 or ex.size <= 0 or target <= 0:
            return
        notional = abs(ex.size) * entry
        need = notional / float(target)
        cur = float(ex.margin_used or 0.0)
        add_amt = need - cur + max(float(force_extra), 0.0)
        if add_amt <= 0.05:
            return
        try:
            result = self.client._exchange.update_isolated_margin(float(f"{add_amt:.4f}"), symbol)
            logger.info("%s 为降至 %sx 追加逐仓保证金 $%.4f → %s", symbol, target, add_amt, result)
        except Exception as exc:
            if is_rate_limit_error(exc):
                raise
            logger.warning("%s 追加逐仓保证金失败: %s", symbol, exc)

    def _align_managed_leverage(self, positions: dict[str, ExchangePosition]) -> None:
        """本地在管仓：交易所杠杆对齐到 target_leverage（默认 10x），保持 isolated/cross 与配置一致。

        若确实改了杠杆，会刷新 positions 映射，以便后续用新的 liquidationPx 判断止损。
        """
        if self.cfg.dry_run:
            return
        target = max(1, int(self.cfg.risk.target_leverage))
        isolated = bool(self.cfg.risk.isolated)
        changed = False
        for local in list(self.account.open_positions()):
            ex = self._exchange_position(positions, local.symbol)
            if ex is None or ex.size <= 0:
                continue
            exchange_lev = ex.leverage
            need = (exchange_lev is None or int(exchange_lev) != target) or int(local.leverage) != target
            if not need:
                continue
            try:
                self._ensure_margin_for_target_leverage(local.symbol, ex, target)
                result = self.client.update_leverage(local.symbol, target, isolated=isolated)
                if isinstance(result, dict) and str(result.get("status", "")).lower() in {"err", "error"}:
                    msg = str(result.get("response") or result)
                    if "sufficient margin" in msg.lower() or "decrease leverage" in msg.lower():
                        self._ensure_margin_for_target_leverage(local.symbol, ex, target, force_extra=1.0)
                        result = self.client.update_leverage(local.symbol, target, isolated=isolated)
                    if isinstance(result, dict) and str(result.get("status", "")).lower() in {"err", "error"}:
                        logger.warning("%s 调整杠杆至 %sx 被拒: %s", local.symbol, target, result)
                        continue
            except Exception as exc:
                if is_rate_limit_error(exc):
                    raise
                logger.warning("%s 调整杠杆至 %sx 失败: %s", local.symbol, target, exc)
                continue
            prev = local.leverage
            local.leverage = target
            changed = True
            logger.info(
                "%s 杠杆已对齐 %sx → %sx（交易所原=%s, isolated=%s）",
                local.symbol,
                prev,
                target,
                exchange_lev,
                isolated,
            )
        if not changed:
            return
        try:
            self.client.shared_perps_raw = None
            refreshed = self.client.fetch_perp_positions()
        except Exception as exc:
            if is_rate_limit_error(exc):
                raise
            logger.warning("改杠杆后刷新持仓失败，止损校验可能仍用旧强平价: %s", exc)
            return
        positions.clear()
        positions.update(refreshed)
        self._exchange_positions = positions

    def _ensure_stops_before_liq(self, positions: dict[str, ExchangePosition]) -> None:
        """若策略止损在强平价之外（或超出缓冲），收紧到安全侧，随后由 _ensure_managed_stops 重挂。"""
        target = max(1, int(self.cfg.risk.target_leverage))
        buffer_frac = float(self.cfg.risk.liq_buffer_frac)
        for local in list(self.account.open_positions()):
            if not local.stop_price or local.stop_price <= 0 or local.entry_price <= 0:
                continue
            ex = self._exchange_position(positions, local.symbol)
            adj, tightened = clamp_stop_for_leverage(
                side=local.side,
                entry=local.entry_price,
                stop=local.stop_price,
                leverage=target,
                buffer_frac=buffer_frac,
            )
            # 若交易所有明确强平价，再与之交叉校验：多头止损必须 > 强平价
            if ex is not None and ex.liquidation_px and ex.liquidation_px > 0:
                liq = float(ex.liquidation_px)
                if local.side is Side.LONG and adj <= liq:
                    # 收到强平价上方一小段（0.1% 入场），仍须在入场下方
                    safe = min(local.entry_price * 0.999, liq * 1.001)
                    if safe > adj:
                        adj = safe
                        tightened = True
                elif local.side is Side.SHORT and adj >= liq:
                    safe = max(local.entry_price * 1.001, liq * 0.999)
                    if safe < adj:
                        adj = safe
                        tightened = True
            if not tightened and abs(adj - local.stop_price) <= 1e-12:
                continue
            logger.warning(
                "%s 策略止损 %.6g 可能晚于强平（liq=%s）；收紧至 %.6g",
                local.symbol,
                local.stop_price,
                getattr(ex, "liquidation_px", None) if ex else None,
                adj,
            )
            local.stop_price = adj
            local.extras["stop_tightened_for_liq"] = True

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
        elif outcome.get("status") == "error" or outcome.get("action") in {
            "place_failed",
            "bad_size",
            "no_exchange",
        }:
            logger.warning(
                "%s 保护止损缺失或重挂失败 action=%s message=%s（本地 stop=%.6g 交易所仓=%.6g）",
                symbol,
                outcome.get("action"),
                outcome.get("message") or outcome,
                local.stop_price,
                ex.size,
            )
        elif outcome.get("action") not in {"kept", "placed", "skipped"}:
            logger.warning("%s 保护止损对账异常结果: %s", symbol, outcome)

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
            if not self._equity_ok:
                logger.warning("%s 本轮权益不可信且无有效缓存，跳过 %s", intent.symbol, intent.action.value)
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
                closed_pos = self.account.position_for(intent.symbol)
                outcome = self.broker.submit(intent, now_ms)
                self._maybe_record_bot_stop(intent, closed_pos, intent.price, outcome, now_ms)
            return
        self.client.connect_sdk(for_trading=True)
        meta = self._asset_ctxs.get(intent.symbol) or self._asset_ctxs.get(intent.symbol.upper()) or {}
        decimals = int(meta.get("sz_decimals") or 4)
        result = self.client.place(intent, sz_decimals=decimals)
        logger.info("实盘下单返回: %s", result)
        self._sync_live_fill(intent, result, now_ms)

    def _maybe_record_bot_stop(
        self,
        intent: OrderIntent,
        pos: Position | None,
        price: float,
        outcome: object,
        now_ms: int,
    ) -> None:
        """机器人自己按硬止损平仓后也记冷却（交易所止损单成交走幽灵仓路径记录）。"""
        if intent.action is not IntentAction.CLOSE or pos is None:
            return
        if "止损" not in str(intent.reason or ""):
            return
        pnl = float(outcome.get("pnl") or 0.0) if isinstance(outcome, dict) else 0.0
        self._record_stop(intent.symbol, pos.side, float(price or 0.0), pnl, now_ms, label="硬止损平仓")

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
                if is_rate_limit_error(exc):
                    logger.warning("本轮扫描遇到 429，%.1fs 后重试，进程不退出: %s", backoff, exc)
                    time.sleep(backoff)
                    backoff = min(backoff * 2.0, 120.0)
                    continue
                if is_transient_network_error(exc):
                    # 重试已在 _post_info 用尽。这里不落盘：scan_once 抛出时末尾的 persist 不会执行，
                    # 中途已完成的 broker.save() 各自是整份快照（原子替换）。
                    wait = max(5, self.cfg.poll_seconds)
                    logger.exception("本轮扫描遇到网络错误，跳过本轮，%.1fs 后继续: %s", wait, exc)
                    time.sleep(wait)
                    continue
                raise
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
        if isinstance(result, dict) and intent.action is IntentAction.OPEN:
            fill_stop = result.get("stop_price")
            if fill_stop:
                sync_intent = replace(sync_intent, stop_price=float(fill_stop))
        closed_pos = self.account.position_for(intent.symbol)
        outcome = self.broker.submit(sync_intent, now_ms)
        self._maybe_record_bot_stop(intent, closed_pos, fill.price or intent.price, outcome, now_ms)
        if isinstance(result, dict):
            sl_oid = result.get("stop_oid")
            if sl_oid is None:
                sl_oid = extract_oid(result.get("stop"))
            pos = self.account.position_for(intent.symbol)
            if pos is not None and sl_oid is not None:
                pos.extras["sl_oid"] = int(sl_oid)
        # scan --live 默认不 persist_paper，成交后仍要落盘，避免下一轮重复平已空仓
        self.broker.save()


def _f(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _same_oid(left: object, right: object) -> bool:
    try:
        return int(left) == int(right)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def close_fills_for(fills: list[dict], pos: Position) -> list[dict]:
    """userFillsByTime 里属于该仓的平仓成交（同币、Close Long/Short 或 reduce 方向、开仓之后）。"""
    want_dir = "close long" if pos.side is Side.LONG else "close short"
    close_side = "A" if pos.side is Side.LONG else "B"
    since = int(pos.opened_ts or 0) - 60_000
    out: list[dict] = []
    for row in fills or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("coin") or "").upper() != pos.symbol.upper():
            continue
        if since > 0 and int(_f(row.get("time"))) < since:
            continue
        direction = str(row.get("dir") or "").strip().lower()
        if direction:
            if not direction.startswith(want_dir) and "liquidat" not in direction:
                continue
        elif str(row.get("side") or "") != close_side:
            continue
        out.append(row)
    return out


def _stop_side_fill(pos: Position, px: float) -> bool:
    """成交价在止损附近或更差（多头 ≤ 止损×1.002）视为止损类成交。"""
    if px <= 0 or not pos.stop_price:
        return False
    if pos.side is Side.LONG:
        return px <= pos.stop_price * 1.002
    return px >= pos.stop_price * 0.998


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
