"""模拟成交：记录意图、维护本地仓位，不向交易所发送订单。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from hl_bot.models import AccountState, IntentAction, OrderIntent, Position, Side, StrategyName
from hl_bot.risk import apply_close, apply_fill, apply_pyramid

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
                "PAPER 开仓 %s %s size=%.6g @ %.6g stop=%.6g lev=%sx isolated",
                intent.side.value,
                intent.symbol,
                intent.size,
                intent.price,
                intent.stop_price or 0,
                intent.leverage,
            )
            return {"status": "paper", "action": "open"}

        if intent.action is IntentAction.ADD:
            pos = self.account.position_for(intent.symbol)
            if pos is None:
                return {"status": "paper", "action": "missing_position"}
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
        return cls(account, path)
