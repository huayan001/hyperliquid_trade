from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from dotenv import load_dotenv

MAINNET_API_URL = "https://api.hyperliquid.xyz"
TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"
TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"

DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _as_list(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return tuple(s.strip().upper() for s in value.split(",") if s.strip())
    return tuple(str(s).upper() for s in value)


@dataclass(slots=True)
class RegimeConfig:
    clear_trend_adx: float = 22.5
    trend_adx_min: float = 20.0
    mr_adx_max: float = 20.0
    range_lookback_hours: int = 12
    range_touch_frac: float = 0.20


@dataclass(slots=True)
class TrendConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    adx_period: int = 14
    adx_exit: float = 15.0
    btc_short_adx_min: float = 25.0
    donchian_period: int = 20
    atr_period: int = 14
    stop_atr: float = 2.0
    trail_step_atr: float = 1.0
    trail_offset_atr: float = 0.5
    chase_body_atr: float = 1.5
    chase_symbols: tuple[str, ...] = ("BTC",)
    risk_pct: float = 0.015
    btc_short_risk_mult: float = 0.7
    max_positions: int = 3
    portfolio_risk_max: float = 0.06
    daily_loss_delever: float = 0.05
    weekly_loss_delever: float = 0.10
    funding_annual_warn: float = 0.50
    funding_size_mult: float = 0.5


@dataclass(slots=True)
class MeanReversionConfig:
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    adx_period: int = 14
    adx_max: float = 20.0
    adx_kill: float = 25.0
    atr_period: int = 14
    stop_atr: float = 1.0
    time_stop_hours: float = 5.0
    partial_tp_frac: float = 0.5
    risk_pct: float = 0.0075
    max_positions: int = 2
    max_trades_per_symbol_day: int = 4
    daily_loss_halt: float = 0.03
    leverage_cap: int = 5
    chase_body_atr: float = 1.5
    bandwidth_high_rank: float = 0.80


@dataclass(slots=True)
class RiskConfig:
    isolated: bool = True
    min_notional_usd: float = 10.0
    max_leverage: dict[str, int] = field(
        default_factory=lambda: {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10}
    )


@dataclass(slots=True)
class BotConfig:
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    network: str = "mainnet"
    dry_run: bool = True
    enable_live: bool = False
    paper_equity: float = 10000.0
    poll_seconds: int = 60
    state_path: str = "state/paper_state.json"
    private_key: str | None = None
    account_address: str | None = None
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    mean_reversion: MeanReversionConfig = field(default_factory=MeanReversionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    @property
    def api_url(self) -> str:
        return TESTNET_API_URL if self.network == "testnet" else MAINNET_API_URL

    @property
    def ws_url(self) -> str:
        return TESTNET_WS_URL if self.network == "testnet" else MAINNET_WS_URL

    def max_leverage_for(self, symbol: str) -> int:
        return int(self.risk.max_leverage.get(symbol.upper(), 10))

    def require_live_ready(self) -> None:
        if self.dry_run:
            raise RuntimeError("当前为 dry-run，不会发送真实订单。")
        if not self.enable_live:
            raise RuntimeError("实盘被拒绝：请同时设置 HL_ENABLE_LIVE=1 并传入 --live。")
        if not self.private_key:
            raise RuntimeError("实盘需要环境变量 HL_PRIVATE_KEY。")


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(
    config_path: str | Path | None = None,
    *,
    cli_network: str | None = None,
    cli_dry_run: bool | None = None,
    cli_live: bool = False,
    cli_symbols: str | None = None,
) -> BotConfig:
    load_dotenv()
    path = Path(config_path or os.getenv("HL_CONFIG") or "config.toml")
    raw = load_toml(path)
    regime_raw = _section(raw, "regime")
    trend_raw = _section(raw, "trend")
    mr_raw = _section(raw, "mean_reversion")
    risk_raw = _section(raw, "risk")
    max_lev = risk_raw.get("max_leverage") or {}

    cfg = BotConfig(
        symbols=_as_list(os.getenv("HL_SYMBOLS", raw.get("symbols")), DEFAULT_SYMBOLS),
        network=(cli_network or os.getenv("HL_NETWORK") or raw.get("network") or "mainnet").lower(),
        dry_run=True,
        enable_live=_as_bool(os.getenv("HL_ENABLE_LIVE"), False) and cli_live,
        paper_equity=_as_float(os.getenv("HL_PAPER_EQUITY", raw.get("paper_equity")), 10000.0),
        poll_seconds=_as_int(raw.get("poll_seconds"), 60),
        state_path=str(raw.get("state_path") or "state/paper_state.json"),
        private_key=os.getenv("HL_PRIVATE_KEY") or None,
        account_address=os.getenv("HL_ACCOUNT_ADDRESS") or None,
        regime=RegimeConfig(
            clear_trend_adx=_as_float(regime_raw.get("clear_trend_adx"), 22.5),
            trend_adx_min=_as_float(regime_raw.get("trend_adx_min"), 20.0),
            mr_adx_max=_as_float(regime_raw.get("mr_adx_max"), 20.0),
            range_lookback_hours=_as_int(regime_raw.get("range_lookback_hours"), 12),
            range_touch_frac=_as_float(regime_raw.get("range_touch_frac"), 0.20),
        ),
        trend=TrendConfig(
            ema_fast=_as_int(trend_raw.get("ema_fast"), 20),
            ema_slow=_as_int(trend_raw.get("ema_slow"), 50),
            adx_period=_as_int(trend_raw.get("adx_period"), 14),
            adx_exit=_as_float(trend_raw.get("adx_exit"), 15.0),
            btc_short_adx_min=_as_float(trend_raw.get("btc_short_adx_min"), 25.0),
            donchian_period=_as_int(trend_raw.get("donchian_period"), 20),
            atr_period=_as_int(trend_raw.get("atr_period"), 14),
            stop_atr=_as_float(trend_raw.get("stop_atr"), 2.0),
            trail_step_atr=_as_float(trend_raw.get("trail_step_atr"), 1.0),
            trail_offset_atr=_as_float(trend_raw.get("trail_offset_atr"), 0.5),
            chase_body_atr=_as_float(trend_raw.get("chase_body_atr"), 1.5),
            chase_symbols=_as_list(trend_raw.get("chase_symbols"), ("BTC",)),
            risk_pct=_as_float(trend_raw.get("risk_pct"), 0.015),
            btc_short_risk_mult=_as_float(trend_raw.get("btc_short_risk_mult"), 0.7),
            max_positions=_as_int(trend_raw.get("max_positions"), 3),
            portfolio_risk_max=_as_float(trend_raw.get("portfolio_risk_max"), 0.06),
            daily_loss_delever=_as_float(trend_raw.get("daily_loss_delever"), 0.05),
            weekly_loss_delever=_as_float(trend_raw.get("weekly_loss_delever"), 0.10),
            funding_annual_warn=_as_float(trend_raw.get("funding_annual_warn"), 0.50),
            funding_size_mult=_as_float(trend_raw.get("funding_size_mult"), 0.5),
        ),
        mean_reversion=MeanReversionConfig(
            bb_period=_as_int(mr_raw.get("bb_period"), 20),
            bb_std=_as_float(mr_raw.get("bb_std"), 2.0),
            rsi_period=_as_int(mr_raw.get("rsi_period"), 14),
            rsi_oversold=_as_float(mr_raw.get("rsi_oversold"), 30.0),
            rsi_overbought=_as_float(mr_raw.get("rsi_overbought"), 70.0),
            adx_period=_as_int(mr_raw.get("adx_period"), 14),
            adx_max=_as_float(mr_raw.get("adx_max"), 20.0),
            adx_kill=_as_float(mr_raw.get("adx_kill"), 25.0),
            atr_period=_as_int(mr_raw.get("atr_period"), 14),
            stop_atr=_as_float(mr_raw.get("stop_atr"), 1.0),
            time_stop_hours=_as_float(mr_raw.get("time_stop_hours"), 5.0),
            partial_tp_frac=_as_float(mr_raw.get("partial_tp_frac"), 0.5),
            risk_pct=_as_float(mr_raw.get("risk_pct"), 0.0075),
            max_positions=_as_int(mr_raw.get("max_positions"), 2),
            max_trades_per_symbol_day=_as_int(mr_raw.get("max_trades_per_symbol_day"), 4),
            daily_loss_halt=_as_float(mr_raw.get("daily_loss_halt"), 0.03),
            leverage_cap=_as_int(mr_raw.get("leverage_cap"), 5),
            chase_body_atr=_as_float(mr_raw.get("chase_body_atr"), 1.5),
            bandwidth_high_rank=_as_float(mr_raw.get("bandwidth_high_rank"), 0.80),
        ),
        risk=RiskConfig(
            isolated=_as_bool(risk_raw.get("isolated"), True),
            min_notional_usd=_as_float(risk_raw.get("min_notional_usd"), 10.0),
            max_leverage={str(k).upper(): int(v) for k, v in max_lev.items()}
            if max_lev
            else {"BTC": 40, "ETH": 25, "SOL": 20, "HYPE": 10},
        ),
    )

    env_dry = os.getenv("HL_DRY_RUN")
    file_dry = raw.get("dry_run")
    cfg.dry_run = _as_bool(env_dry, _as_bool(file_dry, True))
    if cli_dry_run is not None:
        cfg.dry_run = cli_dry_run
    if cli_live:
        # --live 明确要求实盘，但仍必须 HL_ENABLE_LIVE=1
        cfg.dry_run = False
        cfg.enable_live = _as_bool(os.getenv("HL_ENABLE_LIVE"), False)
    if cli_symbols:
        cfg.symbols = _as_list(cli_symbols, cfg.symbols)
    if cfg.network not in {"mainnet", "testnet"}:
        raise ValueError(f"未知网络: {cfg.network}")
    return cfg
