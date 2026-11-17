"""`hl-bot phil-ali` / `python -m hl_bot.phil_ali`。

默认直接退出。不构造趋势 BotRunner，不读趋势纸面状态。
"""

from __future__ import annotations

import argparse
import logging
import time

from hl_bot.config import BotConfig
from hl_bot.phil_ali.gate import PhilAliGate, live_orders_allowed, load_gate
from hl_bot.phil_ali.runner import format_events, load_cycle_book, run_cycle

logger = logging.getLogger("hl_bot.phil_ali")

DISABLED_TEXT = (
    "菲阿里未启用（HL_PHIL_ALI_ENABLED 默认 0）。"
    "不拉行情、不下单、不写趋势机器人状态。"
)
MODE_OFF_TEXT = "菲阿里总闸已开，但 HL_PHIL_ALI_MODE 仍是 off。不拉行情、不下单。"
LOCKED_TEXT = (
    "实盘下单未解锁。要真正发单必须同时满足："
    "HL_PHIL_ALI_ENABLED=1、HL_PHIL_ALI_MODE=live、HL_PHIL_ALI_ARM_LIVE=1、"
    "HL_ENABLE_LIVE=1、HL_DRY_RUN=false，以及命令行 --live。"
    "当前这一轮没有调用交易所下单。"
)


def run_phil_ali(*, loop: bool, cli_live: bool) -> int:
    try:
        gate = load_gate(cli_live=cli_live)
    except ValueError as exc:
        print(f"菲阿里配置拒绝: {exc}")
        return 2
    if not gate.enabled:
        print(DISABLED_TEXT)
        return 0
    if not gate.active:
        print(MODE_OFF_TEXT)
        return 0
    if gate.mode == "live" and not live_orders_allowed(gate):
        print(LOCKED_TEXT)
    elif live_orders_allowed(gate):
        print("*** PHIL ALI LIVE *** 将只为菲阿里信号发送订单，趋势循环不会因此启动。")
    while True:
        try:
            _run_once(gate)
        except KeyboardInterrupt:
            print("\n菲阿里已停止。")
            return 0
        if not loop:
            return 0
        time.sleep(gate.poll_seconds)


def _run_once(gate: PhilAliGate) -> None:
    from hl_bot.exchange.client import HyperliquidClient

    info = HyperliquidClient(_info_config(gate))
    markets = {}
    for symbol in gate.symbols:
        daily = _bars(info.fetch_candles(symbol, "1d", count=5))
        hourly = _bars(info.fetch_candles(symbol, "1h", count=72))
        markets[symbol] = (daily, hourly)
    book = load_cycle_book(gate)
    submit = None
    if live_orders_allowed(gate):
        trader = HyperliquidClient(_trade_config(gate))
        submit = lambda intent, client=trader: _place(client, intent)
    now_ms = int(time.time() * 1000)
    events = run_cycle(book, markets, now_ms, gate, submit=submit)
    print(format_events(events))
    logger.info("菲阿里 %s 权益 %.2f 持仓 %s", gate.mode, book.equity, ",".join(book.positions) or "-")


def _bars(candles) -> list[dict]:
    return [{"t": c.ts, "o": c.open, "h": c.high, "l": c.low, "c": c.close} for c in candles]


def _info_config(gate: PhilAliGate) -> BotConfig:
    # 行情客户端保持 dry-run，并且不带私钥。K 线缓存在 state/phil_ali/，不写趋势的 state/candles。
    return BotConfig(
        symbols=gate.symbols,
        network=gate.network,
        dry_run=True,
        enable_live=False,
        paper_equity=gate.paper_equity,
        state_path=f"{gate.state_dir}/book.json",
        private_key=None,
        account_address=None,
    )


def _trade_config(gate: PhilAliGate) -> BotConfig:
    import os

    return BotConfig(
        symbols=gate.symbols,
        network=gate.network,
        dry_run=False,
        enable_live=True,
        paper_equity=gate.paper_equity,
        state_path=f"{gate.state_dir}/book.json",
        private_key=os.getenv("HL_PRIVATE_KEY") or None,
        account_address=os.getenv("HL_ACCOUNT_ADDRESS") or None,
    )


def _place(client, intent) -> dict:
    ctxs = client.fetch_asset_contexts()
    meta = ctxs.get(intent.symbol) or {}
    decimals = int(meta.get("sz_decimals") or 4)
    return client.place(intent, sz_decimals=decimals)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="菲阿里四价独立入口（默认关闭）")
    parser.add_argument("--loop", action="store_true", help="按 HL_PHIL_ALI_POLL_SECONDS 重复")
    parser.add_argument("--live", action="store_true", help="实盘下单所需的命令行闸，单这一项不会发单")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run_phil_ali(loop=bool(args.loop), cli_live=bool(args.live))
