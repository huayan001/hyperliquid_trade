"""模拟成交：记录意图、维护本地仓位，不向交易所发送订单。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from hl_bot.models import (
    AccountState,
    IntentAction,
    OrderIntent,
    Position,
    RestingEntry,
    Side,
    StrategyName,
)
from hl_bot.risk import apply_close, apply_fill, apply_pyramid, apply_tier_add

logger = logging.getLogger(__name__)


class PaperBroker:
    def __init__(self, account: AccountState, state_path: str | None = None) -> None:
        self.account = account
        self.state_path = Path(state_path) if state_path else None

    def submit(self, intent: OrderIntent, now_ms: int) -> dict:
        if intent.extras.get("trail_update_only"):
            pos = self.account.position_for(intent.symbol)
            if pos and intent.stop_price:
                pos.stop_price = intent.stop_price
                logger.info("PAPER 移动止损 %s -> %s", intent.symbol, intent.stop_price)
            return {"status": "paper", "action": "trail"}

        if intent.action is IntentAction.OPEN:
            pos = Position(
                symbol=intent.symbol,
                strategy=intent.strategy,
                side=intent.side,
                size=intent.size,
                entry_price=intent.price,
                stop_price=intent.stop_price or intent.price,
                leverage=intent.leverage,
                opened_ts=now_ms,
                tag=str(intent.extras.get("tag") or ""),
                extras=dict(intent.extras),
            )
            apply_fill(self.account, pos)
            logger.info(
                "PAPER 开仓 %s %s tier=%s tag=%s size=%.6g @ %.6g stop=%.6g lev=%sx isolated  %s",
                intent.side.value,
                intent.symbol,
                intent.extras.get("tier") or "-",
                intent.extras.get("tag") or pos.tag or "-",
                intent.size,
                intent.price,
                intent.stop_price or 0,
                intent.leverage,
                intent.reason,
            )
            return {"status": "paper", "action": "open"}

        if intent.action is IntentAction.ADD:
            pos = self.account.position_for(intent.symbol)
            if pos is None:
                return {"status": "paper", "action": "missing_position"}
            if intent.extras.get("chase"):
                pos.extras["chase"] = True
            if intent.extras.get("tier_add") or intent.extras.get("breakout_add"):
                apply_tier_add(
                    self.account, pos, intent.size, intent.price, intent.stop_price or pos.stop_price
                )
                logger.info(
                    "PAPER 分层补仓 %s tier=%s tag=%s +%.6g @ %.6g avg=%.6g stop=%.6g  %s",
                    intent.symbol,
                    intent.extras.get("tier") or "-",
                    intent.extras.get("tag") or "-",
                    intent.size,
                    intent.price,
                    pos.entry_price,
                    pos.stop_price,
                    intent.reason,
                )
            else:
                apply_pyramid(self.account, pos, intent.size, intent.price, intent.stop_price or pos.stop_price)
                logger.info(
                    "PAPER 金字塔加仓 %s +%.6g @ %.6g avg=%.6g stop=%.6g",
                    intent.symbol,
                    intent.size,
                    intent.price,
                    pos.entry_price,
                    pos.stop_price,
                )
            return {"status": "paper", "action": "add"}

        pos = self.account.position_for(intent.symbol)
        if pos is None:
            return {"status": "paper", "action": "missing_position"}
        pnl = apply_close(self.account, pos, intent.price, intent.size)
        logger.info("PAPER 平仓 %s pnl=%.4f equity=%.2f", intent.symbol, pnl, self.account.equity)
        return {"status": "paper", "action": "close", "pnl": pnl}

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "equity": self.account.equity,
            "starting_equity": self.account.starting_equity,
            "day_start_equity": self.account.day_start_equity,
            "week_start_equity": self.account.week_start_equity,
            "day_key": self.account.day_key,
            "week_key": self.account.week_key,
            "realized_pnl_today": self.account.realized_pnl_today,
            "day_trades": self.account.day_trades,
            "positions": [
                {
                    "symbol": p.symbol,
                    "strategy": p.strategy.value,
                    "side": p.side.value,
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "stop_price": p.stop_price,
                    "leverage": p.leverage,
                    "opened_ts": p.opened_ts,
                    "remaining_frac": p.remaining_frac,
                    "tag": p.tag,
                    "extras": p.extras,
                }
                for p in self.account.open_positions()
            ],
            "resting_entries": [_resting_to_dict(item) for item in self.account.resting_entries],
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str, default_equity: float, day_key: str, week_key: str) -> PaperBroker:
        p = Path(path)
        if not p.exists():
            account = AccountState(
                equity=default_equity,
                starting_equity=default_equity,
                day_start_equity=default_equity,
                week_start_equity=default_equity,
                day_key=day_key,
                week_key=week_key,
            )
            return cls(account, path)
        raw = json.loads(p.read_text(encoding="utf-8"))
        account = AccountState(
            equity=float(raw.get("equity", default_equity)),
            starting_equity=float(raw.get("starting_equity", default_equity)),
            day_start_equity=float(raw.get("day_start_equity", default_equity)),
            week_start_equity=float(raw.get("week_start_equity", default_equity)),
            day_key=str(raw.get("day_key") or day_key),
            week_key=str(raw.get("week_key") or week_key),
            realized_pnl_today=float(raw.get("realized_pnl_today") or 0.0),
            day_trades={str(k): int(v) for k, v in (raw.get("day_trades") or {}).items()},
        )
        if account.day_key != day_key:
            account.day_start_equity = account.equity
            account.realized_pnl_today = 0.0
            account.day_trades = {}
            account.day_key = day_key
        if account.week_key != week_key:
            account.week_start_equity = account.equity
            account.week_key = week_key
        for item in raw.get("positions") or []:
            account.positions.append(
                Position(
                    symbol=item["symbol"],
                    strategy=StrategyName(item["strategy"]),
                    side=Side(item["side"]),
                    size=float(item["size"]),
                    entry_price=float(item["entry_price"]),
                    stop_price=float(item["stop_price"]),
                    leverage=int(item["leverage"]),
                    opened_ts=int(item["opened_ts"]),
                    remaining_frac=float(item.get("remaining_frac") or 1.0),
                    tag=str(item.get("tag") or ""),
                    extras=dict(item.get("extras") or {}),
                )
            )
        for item in raw.get("resting_entries") or []:
            if isinstance(item, dict):
                account.resting_entries.append(_resting_from_dict(item))
        return cls(account, path)


def _resting_to_dict(item: RestingEntry) -> dict:
    return {
        "symbol": item.symbol,
        "tier": item.tier,
        "action": item.action,
        "side": item.side,
        "price": item.price,
        "size": item.size,
        "stop_price": item.stop_price,
        "oid": item.oid,
        "strategy": item.strategy,
        "leverage": item.leverage,
        "isolated": item.isolated,
        "position_size_at_submit": item.position_size_at_submit,
        "tag": item.tag,
        "extras": item.extras,
    }


def _resting_from_dict(item: dict) -> RestingEntry:
    oid = item.get("oid")
    try:
        parsed_oid = int(oid) if oid is not None else None
    except (TypeError, ValueError):
        parsed_oid = None
    stop = item.get("stop_price")
    return RestingEntry(
        symbol=str(item.get("symbol") or ""),
        tier=str(item.get("tier") or ""),
        action=str(item.get("action") or ""),
        side=str(item.get("side") or ""),
        price=float(item.get("price") or 0.0),
        size=float(item.get("size") or 0.0),
        stop_price=float(stop) if stop not in (None, "") else None,
        oid=parsed_oid,
        strategy=str(item.get("strategy") or ""),
        leverage=int(item.get("leverage") or 1),
        isolated=bool(item.get("isolated", True)),
        position_size_at_submit=float(item.get("position_size_at_submit") or 0.0),
        tag=str(item.get("tag") or ""),
        extras=dict(item.get("extras") or {}),
    )
