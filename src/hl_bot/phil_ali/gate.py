"""菲阿里开关。只有环境变量能打开，config.toml 不能。

默认 HL_PHIL_ALI_ENABLED=0。总闸关闭时忽略 HL_PHIL_ALI_MODE。
实盘下单还要同时满足：mode=live、HL_PHIL_ALI_ARM_LIVE=1、
HL_ENABLE_LIVE=1、HL_DRY_RUN=false、命令行 --live。
少一层就只记账或直接退出，不会调用交易所下单。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 研究回测与默认监视列表。实盘趋势常用 ETH/SOL/HYPE，这里不跟着 HL_SYMBOLS 走。
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL")
MODES = ("off", "shadow", "paper", "live")

# 黄金现货、代币化黄金、以及名称里带这些片段的标的一律拒绝。
BANNED_SYMBOLS = frozenset(
    {
        "XAU",
        "XAUUSD",
        "XAUUSDT",
        "GOLD",
        "GOLDUSD",
        "GOLDUSDT",
        "PAXG",
        "PAXGUSD",
        "PAXGUSDT",
        "XAUT",
        "XAUTUSDT",
    }
)
_BANNED_FRAGMENTS = ("XAU", "GOLD", "PAXG")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _as_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def assert_crypto_symbol(symbol: str) -> str:
    """返回大写标的。黄金 / PAXG / XAU 直接拒绝。"""
    sym = str(symbol).strip().upper()
    if not sym:
        raise ValueError("菲阿里标的为空")
    if sym in BANNED_SYMBOLS or any(fragment in sym for fragment in _BANNED_FRAGMENTS):
        raise ValueError(f"菲阿里只做加密货币永续，不含黄金：{sym}")
    return sym


def parse_symbols(raw: str | None) -> tuple[str, ...]:
    text = raw if raw is not None and raw.strip() else ",".join(DEFAULT_SYMBOLS)
    symbols = tuple(assert_crypto_symbol(part) for part in text.split(",") if part.strip())
    if not symbols:
        raise ValueError("菲阿里标的列表为空")
    # 保持顺序去重
    seen: dict[str, None] = {}
    for sym in symbols:
        seen.setdefault(sym, None)
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class PhilAliGate:
    enabled: bool
    mode: str
    arm_live: bool
    symbols: tuple[str, ...]
    risk_frac: float
    paper_equity: float
    fee_rt: float
    poll_seconds: int
    network: str
    leverage: int
    state_dir: str
    cli_live: bool
    dry_run: bool
    enable_live: bool

    @property
    def active(self) -> bool:
        return self.enabled and self.mode in {"shadow", "paper", "live"}


def live_orders_allowed(gate: PhilAliGate) -> bool:
    """五道闸都打开才允许调用交易所下单。"""
    return bool(
        gate.enabled
        and gate.mode == "live"
        and gate.arm_live
        and gate.cli_live
        and gate.enable_live
        and not gate.dry_run
    )


def load_gate(*, cli_live: bool = False) -> PhilAliGate:
    load_dotenv()
    enabled = _as_bool(os.getenv("HL_PHIL_ALI_ENABLED"), False)
    raw_mode = (os.getenv("HL_PHIL_ALI_MODE") or "off").strip().lower()
    if raw_mode not in MODES:
        raise ValueError(f"HL_PHIL_ALI_MODE 必须是 off、shadow、paper 或 live，收到 {raw_mode!r}")
    # 总闸优先：没开 ENABLED 时，即使 MODE=live 也保持 off。
    mode = raw_mode if enabled else "off"
    network = (os.getenv("HL_NETWORK") or "mainnet").strip().lower()
    if network not in {"mainnet", "testnet"}:
        raise ValueError(f"未知网络: {network}")
    risk = _as_float(os.getenv("HL_PHIL_ALI_RISK_FRAC"), 0.01)
    if risk <= 0 or risk > 0.05:
        raise ValueError(f"HL_PHIL_ALI_RISK_FRAC 必须在 (0, 0.05] 内，收到 {risk}")
    equity = _as_float(os.getenv("HL_PHIL_ALI_PAPER_EQUITY"), 10_000.0)
    if equity <= 0:
        raise ValueError("HL_PHIL_ALI_PAPER_EQUITY 必须大于 0")
    fee = _as_float(os.getenv("HL_PHIL_ALI_FEE_RT"), 0.0005)
    if fee < 0:
        raise ValueError("HL_PHIL_ALI_FEE_RT 不能为负")
    poll = _as_int(os.getenv("HL_PHIL_ALI_POLL_SECONDS"), 300)
    if poll < 30:
        raise ValueError("HL_PHIL_ALI_POLL_SECONDS 至少 30 秒")
    leverage = _as_int(os.getenv("HL_PHIL_ALI_LEVERAGE"), 10)
    if leverage < 1 or leverage > 50:
        raise ValueError("HL_PHIL_ALI_LEVERAGE 必须在 1–50")
    return PhilAliGate(
        enabled=enabled,
        mode=mode,
        arm_live=_as_bool(os.getenv("HL_PHIL_ALI_ARM_LIVE"), False),
        symbols=parse_symbols(os.getenv("HL_PHIL_ALI_SYMBOLS")),
        risk_frac=risk,
        paper_equity=equity,
        fee_rt=fee,
        poll_seconds=poll,
        network=network,
        leverage=leverage,
        state_dir=os.getenv("HL_PHIL_ALI_STATE_DIR") or "state/phil_ali",
        cli_live=cli_live,
        dry_run=_as_bool(os.getenv("HL_DRY_RUN"), True),
        enable_live=_as_bool(os.getenv("HL_ENABLE_LIVE"), False),
    )
