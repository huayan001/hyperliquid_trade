"""PURE 菲阿里规则的回测入口。

实现在 `hl_bot.phil_ali.rules`。这里再导出，方便 `python run_backtest.py` 直接引用。
只覆盖加密货币。黄金、PAXG、XAU 不在这个包里。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hl_bot.phil_ali.rules import (  # noqa: E402
    CRYPTO_SYMBOLS,
    FEE_RT_ALT,
    FEE_RT_BASE,
    RISK_FRAC,
    START_EQ,
    Trade,
    find_first_breakout,
    load_ohlc,
    simulate_portfolio,
    simulate_symbol,
    stats,
    trades_to_rows,
    utc_day,
)

__all__ = [
    "CRYPTO_SYMBOLS",
    "FEE_RT_ALT",
    "FEE_RT_BASE",
    "RISK_FRAC",
    "START_EQ",
    "Trade",
    "find_first_breakout",
    "load_ohlc",
    "simulate_portfolio",
    "simulate_symbol",
    "stats",
    "trades_to_rows",
    "utc_day",
]
