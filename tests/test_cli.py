from datetime import datetime, timezone

from hl_bot.cli import _build_parser
from hl_bot.runner import _utc_keys


def test_parse_scan_after_flags() -> None:
    args = _build_parser().parse_args(["scan", "--network", "testnet", "--symbols", "BTC,ETH"])
    assert args.command == "scan"
    assert args.network == "testnet"
    assert args.symbols == "BTC,ETH"
    assert args.live is False


def test_utc_week_key() -> None:
    day, week, ts = _utc_keys(datetime(2026, 9, 19, tzinfo=timezone.utc))
    assert day == "2026-09-19"
    assert week.startswith("2026-W")
    assert ts > 0
