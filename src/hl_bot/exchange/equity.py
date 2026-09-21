"""实盘权益：对齐 Hyperliquid App 下单页「可用」，而不是只读 perps accountValue。

调查结论（官方 /info + 实盘地址核对）
--------------------------------
- `clearinghouseState.marginSummary.accountValue` 只描述 **永续清算所** 里已划转/占用的保证金。
  空仓且资金停在现货时经常是 `0`，但 App 永续下单页「可用」仍显示现货 USDC。
- `spotClearinghouseState` 的 USDC `total - hold` 即未被挂单冻结的现货 USDC。
  对地址 `0x17d6e255…`：perps accountValue=0，现货 USDC≈478.749，与 App「可用」一致。
- `userAbstraction`：`unifiedAccount` / `portfolioMargin` 下，官方写明
  **spot 才是跨现货/永续的余额真相**，单个 perp dex 的 user state「没有意义」。
  标准/手动账户则现货与永续是两桶资金，必须相加。

公式（避免重复计数）
--------------------
    free_spot_usdc = max(0, USDC.total - USDC.hold)
    perps_value    = marginSummary.accountValue
                     若 ≤0 则退回 max(crossMarginSummary.accountValue, withdrawable)

    unified / portfolioMargin:
        equity = free_spot_usdc          # 不加 perps，避免同一桶 USDC 计两次
        若现货为 0 而 perps>0，则用 perps（迁移残留 / 接口缺字段）
    标准账户或 abstraction 未知:
        equity = perps_value + free_spot_usdc

纸盘 / dry-run 不走这里，仍用 HL_PAPER_EQUITY 与本地 state。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UNIFIED_ABSTRACTIONS = frozenset({"unifiedaccount", "portfoliomargin"})


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def perps_account_value(perps_state: Any) -> float:
    """永续清算所权益。accountValue 优先；为 0 时才看 cross / withdrawable。"""
    if not isinstance(perps_state, dict):
        return 0.0
    summary = perps_state.get("marginSummary")
    value = _as_float(summary.get("accountValue") if isinstance(summary, dict) else 0.0)
    if value > 0:
        return value
    cross = perps_state.get("crossMarginSummary")
    cross_value = _as_float(cross.get("accountValue") if isinstance(cross, dict) else 0.0)
    withdrawable = _as_float(perps_state.get("withdrawable"))
    return max(cross_value, withdrawable, 0.0)


def free_spot_usdc(spot_state: Any) -> float:
    """现货 USDC 可用：total - hold。只认 coin=USDC 或 token=0，不含 USDH/USDT。"""
    if not isinstance(spot_state, dict):
        return 0.0
    balances = spot_state.get("balances")
    if not isinstance(balances, list):
        return 0.0
    for item in balances:
        if not isinstance(item, dict):
            continue
        coin = str(item.get("coin") or "").strip().upper()
        token = item.get("token")
        if coin != "USDC" and token != 0:
            continue
        total = _as_float(item.get("total"))
        hold = _as_float(item.get("hold"))
        return max(0.0, total - hold)
    return 0.0


def normalize_abstraction(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("abstraction") or value.get("userAbstraction") or value.get("mode")
    text = str(value or "").strip().strip('"')
    return text or None


def is_unified_abstraction(abstraction: str | None) -> bool:
    if not abstraction:
        return False
    return abstraction.strip().lower() in UNIFIED_ABSTRACTIONS


@dataclass(frozen=True, slots=True)
class EquityBreakdown:
    equity: float
    perps_value: float
    free_spot_usdc: float
    abstraction: str | None
    formula: str
    included: tuple[str, ...]


def combine_live_equity(
    perps_state: Any,
    spot_state: Any,
    abstraction: Any = None,
) -> EquityBreakdown:
    """把 perps / spot 两个 /info 响应当成权益，按账户模式避免重复计数。"""
    perps = max(0.0, perps_account_value(perps_state))
    spot = max(0.0, free_spot_usdc(spot_state))
    mode = normalize_abstraction(abstraction)

    if is_unified_abstraction(mode):
        if spot > 0:
            return EquityBreakdown(
                equity=spot,
                perps_value=perps,
                free_spot_usdc=spot,
                abstraction=mode,
                formula="spot_usdc_unified",
                included=("spot_usdc",),
            )
        return EquityBreakdown(
            equity=perps,
            perps_value=perps,
            free_spot_usdc=spot,
            abstraction=mode,
            formula="perps_fallback_unified",
            included=("perps",) if perps > 0 else (),
        )

    included: list[str] = []
    if perps > 0:
        included.append("perps")
    if spot > 0:
        included.append("spot_usdc")
    return EquityBreakdown(
        equity=perps + spot,
        perps_value=perps,
        free_spot_usdc=spot,
        abstraction=mode,
        formula="perps_plus_spot",
        included=tuple(included),
    )
