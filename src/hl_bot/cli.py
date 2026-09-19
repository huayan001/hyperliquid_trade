from __future__ import annotations

import argparse
import logging
import sys

from hl_bot import __version__
from hl_bot.config import load_config
from hl_bot.runner import BotRunner, format_report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hl-bot",
        description="Hyperliquid 永续合约机器人（趋势跟踪 + 均值回归，v1.1）",
    )
    parser.add_argument("--config", default=None, help="config.toml 路径")
    parser.add_argument("--network", choices=("mainnet", "testnet"), default=None)
    parser.add_argument("--symbols", default=None, help="逗号分隔，如 BTC,ETH")
    parser.add_argument("--live", action="store_true", help="实盘下单（还需 HL_ENABLE_LIVE=1）")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="拉一次公开行情，打印制度/信号/模拟订单意图（默认不下单）")
    run_p = sub.add_parser("run", help="按间隔循环扫描；dry-run 写入本地纸盘状态")
    run_p.add_argument("--interval", type=int, default=None, help="轮询秒数，覆盖配置")
    sub.add_parser("version", help="打印版本")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "version":
        print(__version__)
        return 0

    cfg = load_config(
        args.config,
        cli_network=args.network,
        cli_live=args.live,
        cli_symbols=args.symbols,
    )
    if args.command == "run" and args.interval:
        cfg.poll_seconds = args.interval

    if args.live:
        try:
            cfg.require_live_ready()
        except RuntimeError as exc:
            print(f"拒绝实盘: {exc}", file=sys.stderr)
            return 2
        print("*** LIVE MODE *** 将向 Hyperliquid 发送真实订单。请确认逐仓与止损。", file=sys.stderr)

    runner = BotRunner(cfg)
    if args.command == "scan":
        report = runner.scan_once(persist_paper=False)
        print(format_report(report))
        return 0
    if args.command == "run":
        try:
            runner.run_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
