#!/usr/bin/env python3
"""跑 PURE 菲阿里：只含 BTC/ETH/SOL。不读黄金数据，不改实盘配置。"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT.parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_bot.phil_ali.rules import (  # noqa: E402
    CRYPTO_SYMBOLS,
    FEE_RT_ALT,
    FEE_RT_BASE,
    START_EQ,
    load_ohlc,
    simulate_portfolio,
    simulate_symbol,
    stats,
    trades_to_rows,
    utc_day,
)

DATA = ROOT / "data"
OUT = ROOT / "out"
OOS_FRAC = 0.30


def fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def fmt_pf(value: float) -> str:
    if value is None or (isinstance(value, float) and math.isinf(value)):
        return "inf"
    return f"{value:.2f}"


def split_ts_for(hourly: list[dict]) -> int:
    start, end = hourly[0]["t"], hourly[-1]["t"]
    return int(start + (end - start) * (1.0 - OOS_FRAC))


def period_buckets(trades):
    buckets = {
        "2023Q3-Q4": (1688169600000, 1704067200000),
        "2024H1": (1704067200000, 1719792000000),
        "2024H2": (1719792000000, 1735689600000),
        "2025H1": (1735689600000, 1751328000000),
        "2025H2": (1751328000000, 1767225600000),
        "2026YTD": (1767225600000, 1800000000000),
    }
    return {name: stats([trade for trade in trades if start <= trade.entry_ts < end]) for name, (start, end) in buckets.items()}


def plot_equity(curves: dict[str, list[tuple[int, float]]], path: Path, title: str, split_ts: int | None) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("未安装 matplotlib，跳过权益图", flush=True)
        return
    fig, ax = plt.subplots(figsize=(11, 5))
    for label, curve in curves.items():
        if not curve:
            continue
        xs = [datetime.fromtimestamp(ts / 1000, tz=timezone.utc) for ts, _eq in curve]
        ys = [eq for _ts, eq in curve]
        ax.plot(xs, ys, label=label, linewidth=1.2)
    ax.axhline(START_EQ, color="gray", linestyle="--", linewidth=0.8)
    if split_ts:
        ax.axvline(datetime.fromtimestamp(split_ts / 1000, tz=timezone.utc), color="orange", linestyle="--", label="IS|OOS")
    ax.set_title(title)
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def md_row(label: str, row: dict) -> str:
    return (
        f"| {label} | {row['n']} | {fmt_pct(row['wr'])} | {fmt_money(row['avg_win'])} | "
        f"{fmt_money(row['avg_loss'])} | {fmt_money(row['expectancy'])} | {fmt_pf(row['pf'])} | "
        f"{fmt_pct(row['max_dd'])} | {fmt_money(row['net'])} | {fmt_money(row['end_eq'])} | "
        f"{fmt_money(row['fees'])} |"
    )


HEADER = "| Set | N | WinRate | AvgWin | AvgLoss | Expectancy $/trade | PF | MaxDD | Net | EndEq | Fees |"
SEP = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"


def clean(value):
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return None
    return value


def main() -> None:
    OUT.mkdir(exist_ok=True)
    symbols = list(CRYPTO_SYMBOLS)
    symbols_data = {}
    meta = {}
    for sym in symbols:
        daily_path = DATA / f"{sym}_1d.json"
        hourly_path = DATA / f"{sym}_1h.json"
        if not daily_path.exists() or not hourly_path.exists():
            raise SystemExit(
                f"缺少 {sym} 的 K 线。在 backtest/phil_ali 下执行 python fetch.py "
                "（只下载 BTC/ETH/SOL；data/ 不入库）。"
            )
        daily = load_ohlc(daily_path)
        hourly = load_ohlc(hourly_path)
        symbols_data[sym] = (daily, hourly)
        meta[sym] = {
            "n_1d": len(daily),
            "n_1h": len(hourly),
            "from": utc_day(hourly[0]["t"]),
            "to": utc_day(hourly[-1]["t"]),
        }

    split_ts = split_ts_for(symbols_data["BTC"][1])
    split_day = utc_day(split_ts)
    results = {
        "meta": {
            "version": "pure_v2_crypto",
            "symbols": symbols,
            "gold": "excluded",
            "start_equity": START_EQ,
            "fee_rt_base": FEE_RT_BASE,
            "fee_rt_alt": FEE_RT_ALT,
            "funding_assumption": "EOD flatten → funding≈0",
            "oos_frac": OOS_FRAC,
            "split_day_utc": split_day,
            "coverage": meta,
        },
        "per_symbol": {},
        "portfolio_crypto": {},
    }

    lines = [
        "# 菲阿里四价 — 纯规则回测（BTC/ETH/SOL）",
        "",
        f"生成时间（本地时区）：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        "黄金、PAXG、XAU 不在本回测里。",
        "",
        "## 规则",
        "",
        "- 四价（昨）：YO 昨开，YC 昨收，YH 昨高，YL 昨低。进场只用 YH/YL。",
        "- 做多：当日第一根 1h 的 high > YH。开盘已在 YH 上方按开盘成交，否则按 YH。",
        "- 做空：当日第一根 1h 的 low < YL。成交价镜像。",
        "- 没有开盘相对昨收的偏向过滤，没有 1.5R 止盈，没有盘中止损。",
        "- 同日最多 1 笔。同一根同时破 YH 和 YL 则整天跳过。",
        "- 日末用当日最后一根 1h 收盘平仓。R：多 = entry−YL，空 = YH−entry。风险 1% 权益。",
        f"- 费用往返 0.05%（另报 0.08%）。时间分割前 70% / 后 30%，分割日 UTC {split_day}。",
        "",
        "## 数据",
        "",
        "| Symbol | 1h | 1d | From (UTC) | To (UTC) |",
        "|---|---:|---:|---|---|",
    ]
    for sym in symbols:
        row = meta[sym]
        lines.append(f"| {sym} | {row['n_1h']} | {row['n_1d']} | {row['from']} | {row['to']} |")
    lines.extend(["", "## 单标的（fee RT 0.05%）", "", HEADER, SEP])

    curves = {}
    for sym in symbols:
        daily, hourly = symbols_data[sym]
        trades, curve = simulate_symbol(sym, daily, hourly, fee_rt=FEE_RT_BASE, split_ts=split_ts)
        sample_in = [trade for trade in trades if trade.split == "IS"]
        sample_out = [trade for trade in trades if trade.split == "OOS"]
        all_stats, in_stats, out_stats = stats(trades), stats(sample_in), stats(sample_out)
        alt_trades, _alt_curve = simulate_symbol(sym, daily, hourly, fee_rt=FEE_RT_ALT, split_ts=split_ts)
        alt_stats = stats(alt_trades)
        curves[sym] = curve
        results["per_symbol"][sym] = {
            "all": all_stats,
            "IS": in_stats,
            "OOS": out_stats,
            "alt_fee_0.08": {"all": alt_stats},
            "regimes": period_buckets(trades),
            "n_trades": len(trades),
        }
        (OUT / f"trades_{sym}.json").write_text(json.dumps(trades_to_rows(trades), indent=2))
        for label, row in (
            (f"{sym} ALL", all_stats),
            (f"{sym} IS", in_stats),
            (f"{sym} OOS", out_stats),
            (f"{sym} ALL fee0.08%", alt_stats),
        ):
            lines.append(md_row(label, row))

    lines.extend(["", "### 分阶段", ""])
    for sym in symbols:
        lines.extend([f"**{sym}**", "", HEADER, SEP])
        for name, row in results["per_symbol"][sym]["regimes"].items():
            if row["n"]:
                lines.append(md_row(name, row))
        lines.append("")

    port_trades, port_curve = simulate_portfolio(symbols_data, fee_rt=FEE_RT_BASE, split_ts=split_ts)
    port_in = [trade for trade in port_trades if trade.split == "IS"]
    port_out = [trade for trade in port_trades if trade.split == "OOS"]
    port = {
        "all": stats(port_trades),
        "IS": stats(port_in),
        "OOS": stats(port_out),
        "regimes": period_buckets(port_trades),
    }
    alt_port, _alt = simulate_portfolio(symbols_data, fee_rt=FEE_RT_ALT, split_ts=split_ts)
    port["alt_fee_0.08"] = {"all": stats(alt_port)}
    results["portfolio_crypto"] = port
    curves["PORT_CRYPTO"] = port_curve
    (OUT / "trades_PORT_CRYPTO.json").write_text(json.dumps(trades_to_rows(port_trades), indent=2))

    lines.extend(["## 组合 BTC+ETH+SOL", "", HEADER, SEP])
    for label, row in (
        ("PORT_CRYPTO ALL", port["all"]),
        ("PORT_CRYPTO IS", port["IS"]),
        ("PORT_CRYPTO OOS", port["OOS"]),
        ("PORT_CRYPTO fee0.08%", port["alt_fee_0.08"]["all"]),
    ):
        lines.append(md_row(label, row))
    lines.extend(["", "分阶段：", "", HEADER, SEP])
    for name, row in port["regimes"].items():
        if row["n"]:
            lines.append(md_row(name, row))
    lines.append("")

    plot_equity({sym: curves[sym] for sym in symbols}, OUT / "equity_per_symbol.png", "Phil Ali PURE — BTC/ETH/SOL", split_ts)
    plot_equity({"PORT_CRYPTO": curves["PORT_CRYPTO"]}, OUT / "equity_portfolio.png", "Phil Ali PURE — portfolio", split_ts)

    best6 = max(results["per_symbol"][sym]["all"].get("best_6m_ret", 0.0) for sym in symbols)
    port_net = port["all"]["net"]
    lines.extend(
        [
            "## 说明",
            "",
            f"- 组合净利 {fmt_money(port_net)}（IS {fmt_money(port['IS']['net'])} / OOS {fmt_money(port['OOS']['net'])}）。",
            f"- 单标的滚动约 6 个月的最好一段约 {fmt_pct(best6)}，组合约 {fmt_pct(port['all'].get('best_6m_ret', 0.0))}。",
            "- 这是研究回测，不是实盘开关。实盘入口默认关闭，见仓库 `docs/phil_ali.md`。",
            "",
        ]
    )
    results["best_6m_ret_max_symbol"] = best6
    (OUT / "summary.json").write_text(json.dumps(clean(results), indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("DONE", split_day)
    print("PORT_CRYPTO", port["all"]["n"], fmt_money(port_net))


if __name__ == "__main__":
    main()
