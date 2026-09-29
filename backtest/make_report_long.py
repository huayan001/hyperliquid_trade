"""汇总长周期两组结果（4 币 / 实盘 3 币）为 report_long.md。先运行：
BT_DATA=long python bt.py && BT_DATA=long BT_SYMS=ETH,SOL,HYPE python bt.py"""
import datetime as dt, pickle
from pathlib import Path
HERE = Path(__file__).parent
CST = dt.timezone(dt.timedelta(hours=8))
f = lambda ms: dt.datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d")
pct = lambda x: f"{x*100:+.1f}%"
pf = lambda x: "∞" if x == float("inf") else f"{x:.2f}"
runs = [("4 币 BTC/ETH/SOL/HYPE", "results_long.pkl"), ("实盘标的 ETH/SOL/HYPE", "results_long_ETH-SOL-HYPE.pkl")]
L = ["# 长周期回测（2023-10 → 2026-09，Binance/OKX 永续数据）\n"]
first = True
for title, fn in runs:
    R = pickle.loads((HERE / fn).read_bytes()); meta = R.pop("_meta"); ns = len(meta["syms"])
    if first:
        ss = "、".join(f"{k} {f(v)}" for k, v in meta["sym_start"].items())
        L.append(f"- 区间（北京时间）：{f(meta['test_start'])} → {f(meta['end_ts'])}，{meta['bars']} 根 1h；样本外 = 最后 {meta['oos_frac']*100:.0f}%（{f(meta['split_ts'])} 起）")
        L.append(f"- 数据：BTC/ETH/SOL = Binance USDT-M（data.binance.vision 公开数据集），HYPE = OKX HYPE-USDT-SWAP（各场所中 1h 历史最长，{f(meta['sym_start']['HYPE'])} 起，日线预热后约 2025-04 才开始交易）。首根 1h：{ss}")
        L.append("- 资金费：8h（或 4h）结算费率均摊为每小时，记在结算前各小时 K 线收盘时刻；HYPE 2025-02-21→2025-05（Binance 上线前）用 BTC 费率代理（2353 小时）")
        L.append("- OOS 交易按**开仓时间**归属（跨越分割点的交易算样本内）；OOS 收益按资金曲线分段")
        L.append("- 规则/成本同 report_tables.md（修复版 bt.py；L0/V4 = 实盘 config.toml 口径；cc口径 = Claude 原设 3 仓/6%）\n")
        first = False
    L.append(f"## {title}\n")
    L.append("| 版本 | 总收益 | MDD | PF | 胜率 | 交易 | IS 收益/PF | OOS 收益/PF (n) | 正收益币(独立/组合) | 通过? |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, r in R.items():
        p = r["portfolio"]; o = r["oos"]
        npos = sum(1 for st in r["solo"].values() if st["ret"] > 0); npp = sum(1 for st in r["per"].values() if st["pnl"] > 0)
        ok = o["pf"] > 1.2 and npos >= 3 and r["mdd"] < 0.20 and p["trades"] >= 100
        L.append(f"| {name} | {pct(r['ret'])} | {r['mdd']*100:.1f}% | {pf(p['pf'])} | {p['win']*100:.0f}% | {p['trades']} | {pct(r['is']['ret'])} / {pf(r['is']['pf'])} | {pct(o['ret'])} / {pf(o['pf'])} ({o['trades']}) | {npos}/{ns} · {npp}/{ns} | {'PASS' if ok else 'FAIL'} |")
    labs = [k for k in next(iter(R.values()))["periods"]]
    ref = meta["ref"]
    L.append(f"\n分阶段（组合收益 / 阶段内 MDD）：\n")
    L.append("| 版本 | " + " | ".join(labs) + " |"); L.append("|---|" + "---|" * len(labs))
    any_r = next(iter(R.values()))
    L.append(f"| *{ref} 涨跌* | " + " | ".join(pct(any_r["periods"][k]["ref_chg"]) for k in labs) + " |")
    for name, r in R.items():
        L.append(f"| {name} | " + " | ".join(f"{pct(r['periods'][k]['ret'])} / {r['periods'][k]['mdd']*100:.0f}%" for k in labs) + " |")
    L.append("\n分币种独立回测收益（各 $1000）：\n")
    L.append("| 版本 | " + " | ".join(meta["syms"]) + " |"); L.append("|---|" + "---|" * ns)
    for name, r in R.items():
        L.append(f"| {name} | " + " | ".join(f"{pct(r['solo'][s]['ret'])} (PF {pf(r['solo'][s]['pf'])})" for s in meta["syms"]) + " |")
    mr = {name: r["by_strat"]["mr"] for name, r in R.items() if r["by_strat"]["mr"]["trades"]}
    L.append("\n均值回归交易数：" + "；".join(f"{k} {v['trades']} 笔（{v['pnl']:+.1f}$）" for k, v in mr.items()) + "；其余版本 0 笔\n")
    L.append("出场原因（L0 / V4）：")
    for name in ("L0 实盘趋势", "V4 趋势"):
        L.append(f"- {name}: {dict(sorted(R[name]['exits'].items(), key=lambda kv: -kv[1]))}")
    L.append("")
(HERE / "report_long_tables.md").write_text("\n".join(L) + "\n")
print("\n".join(L))
