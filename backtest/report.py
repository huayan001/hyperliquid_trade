import os, pickle, datetime as dt
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 与 bt.py 相同的开关：BT_DATA=long、BT_SYMS=ETH,SOL,HYPE
LONG = os.getenv("BT_DATA", "").lower() == "long"
SYMS = [x.strip().upper() for x in os.getenv("BT_SYMS", "BTC,ETH,SOL,HYPE").split(",") if x.strip()]
TAG = ("_long" if LONG else "") + ("" if SYMS == ["BTC", "ETH", "SOL", "HYPE"] else "_" + "-".join(SYMS))
HERE = Path(__file__).parent
R = pickle.loads((HERE / f"results{TAG}.pkl").read_bytes())
meta = R.pop("_meta")
NS = len(meta.get("syms", SYMS))
OOSP = int(round(meta.get("oos_frac", 0.4) * 100))
CST = dt.timezone(dt.timedelta(hours=8))
f = lambda ms: dt.datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d %H:%M")
print(f(meta["test_start"]), f(meta["split_ts"]), f(meta["end_ts"]), meta)

fig, (ax, ax2) = plt.subplots(2, 1, figsize=(11, 7.5), sharex=True, gridspec_kw={"height_ratios": [3, 1.3]})
labels = {"V0 现行": "V0 old", "V1 1h+分批": "V1", "V2 1h+金字塔": "V2", "V3 V0+吊灯保本": "V3",
          "L0 实盘趋势": "L0 live trend (2pos/4%)", "L0+MR 实盘现状": "L0+MR live", "L0 cc口径(3仓/6%)": "L0+MR cc caps 3pos/6%", "V4 cc口径(3仓/6%)": "V4+MR cc caps 3pos/6%", "V4 趋势": "V4 trend", "V4+MR": "V4+MR"}
for name, r in R.items():
    xs = [dt.datetime.fromtimestamp(t / 1000, CST) for t, _ in r["curve"]]
    ys = [v for _, v in r["curve"]]
    ax.plot(xs, ys, label=f"{labels.get(name, name)}  ({r['ret']*100:+.1f}%, MDD {r['mdd']*100:.1f}%)", lw=1.3)
    peak, dd = ys[0], []
    for v in ys:
        peak = max(peak, v); dd.append((v / peak - 1) * 100)
    ax2.plot(xs, dd, lw=1)
for a in (ax, ax2):
    a.axvline(dt.datetime.fromtimestamp(meta["split_ts"] / 1000, CST), color="k", ls="--", lw=0.8)
ax.axhline(1000, color="grey", lw=0.6)
src = "Binance/OKX perp data (long)" if LONG else "Hyperliquid data"
ax.set_title(f"{src}: {'/'.join(meta.get('syms', SYMS))} portfolio backtest (start $1000, 2% risk/trade, fees+funding)\n"
             f"left of dashed line = in-sample ({100-OOSP}%), right = out-of-sample ({OOSP}%)", fontsize=10)
ax.set_ylabel("equity $"); ax2.set_ylabel("drawdown %")
ax.legend(fontsize=8, loc="lower left"); ax.grid(alpha=0.3); ax2.grid(alpha=0.3)
fig.tight_layout(); fig.savefig(HERE / f"equity{TAG}.png", dpi=130)

def pct(x): return f"{x*100:+.1f}%"
def pf(x): return "∞" if x == float("inf") else f"{x:.2f}"
def gb(st): return "n/a" if st["gb_n"] == 0 else f"{st['giveback']*100:.0f}% (n={st['gb_n']})"

L = []
L.append("# 回测报告：V0–V3 与 实盘口径 L0 / V4（含均值回归）\n")
if LONG:
    ss = "；".join(f"{k} 自 {f(v)}" for k, v in meta.get("sym_start", {}).items())
    L.append(f"- 数据：BTC/ETH/SOL = Binance USDT-M 永续（data.binance.vision），HYPE = OKX HYPE-USDT-SWAP；1h K 线，4h/1d 由 1h 按 UTC 聚合；资金费 8h 结算均摊为每小时（缺失时段用 BTC 费率代理，见 fetch_long.log）。首根 1h：{ss}")
else:
    L.append(f"- 数据：Hyperliquid 官方 candleSnapshot 1h/4h/1d（{'、'.join(meta.get('syms', SYMS))}），资金费用 fundingHistory 实际值（{'、'.join(meta['funding_proxy']) or '无'} 缺数据，用 BTC 资金费代理）")
L.append(f"- 回测区间（北京时间）：{f(meta['test_start'])} → {f(meta['end_ts'])}，共 {meta['bars']} 根 1h；之前的数据只用来预热日线 EMA50/ADX")
L.append(f"- 样本内/样本外：前 {100-OOSP}% 为样本内（至 {f(meta['split_ts'])}），后 {OOSP}% 为样本外；**交易按开仓时间归属**（跨越分割点的交易算样本内），收益按资金曲线分段。所有参数事先定好，**没有做任何参数寻优**")
L.append("- 成本：taker 0.045%、maker 0.015%（只有 V1/V2 的分批止盈限价单按 maker 计），止损/市价单另加 0.02% 滑点，按小时计资金费")
L.append("- 组合规则：初始 $1000，单笔风险 2% 权益，10x 逐仓（止损距离 ≤5%）；V0–V3 最多 2 仓/组合风险 ≤4%；L0/V4 按实盘 config.toml（趋势 2 仓/4%、BTC 空 ×0.7、MR 1 仓/每币每日 3 次/日亏 2% 停、日 5%/周 10% 降杠杆、实盘冷却口径）；cc口径 行 = Claude 原设 3 仓/6%。信号在 1h 收盘判定、下一根 1h 开盘成交；成交时权益按开盘价估值（无前视）")
L.append("- 环境过滤：直接调用仓库 `hl_bot.regime`（日线 EMA20/50 + ADX>22.5，1h 区间冲突时观望；BTC 做空要求 ADX>25）\n")
L.append(f"## 1. 组合结果（{NS} 个币共用一个账户）\n")
L.append("| 版本 | 总收益 | 最大回撤 | 交易数 | 胜率 | 盈亏比 PF | 平均 R | 峰值浮盈回吐率* | 持仓时间占比 | 手续费 | 样本内 收益/PF | 样本外 收益/PF |")
L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
for name, r in R.items():
    p = r["portfolio"]
    L.append(f"| {name} | {pct(r['ret'])} | {r['mdd']*100:.1f}% | {p['trades']} | {p['win']*100:.0f}% | {pf(p['pf'])} | {p['avg_r']:+.2f} | {gb(p)} | {r['exposure']*100:.0f}% | ${r['fees']:.0f} | {pct(r['is']['ret'])} / {pf(r['is']['pf'])} | {pct(r['oos']['ret'])} / {pf(r['oos']['pf'])} |")
L.append("\n\\* 峰值浮盈回吐率 = (持仓期间最高浮盈 − 最终盈亏) / 最高浮盈，只统计最高浮盈 ≥0.5R 的交易（R = 开仓时权益的 2%）；>100% 表示由盈转亏。浮盈按 1h 收盘价计。\n")
L.append("## 2. 分币种（每个币单独 $1000 独立回测，不受「最多仓位 / 币种顺序」影响）\n")
L.append("| 版本 | 币种 | 交易数 | 胜率 | 总收益 | 最大回撤 | PF | 平均 R | 浮盈回吐率 | 持仓时间占比 |")
L.append("|---|---|---|---|---|---|---|---|---|---|")
for name, r in R.items():
    for s, st in r["solo"].items():
        L.append(f"| {name} | {s} | {st['trades']} | {st['win']*100:.0f}% | {pct(st['ret'])} | {st['mdd']*100:.1f}% | {pf(st['pf'])} | {st['avg_r']:+.2f} | {gb(st)} | {st['exposure']*100:.0f}% |")
L.append("\n## 3. 分策略拆分（组合内）\n")
L.append("| 版本 | 策略 | 交易数 | 盈亏 $ | 胜率 | PF | 平均 R |")
L.append("|---|---|---|---|---|---|---|")
for name, r in R.items():
    for k, st in r.get("by_strat", {}).items():
        if st["trades"]:
            L.append(f"| {name} | {k} | {st['trades']} | {st['pnl']:+.1f} | {st['win']*100:.0f}% | {pf(st['pf'])} | {st['avg_r']:+.2f} |")
L.append("\n出场原因计数：")
for name, r in R.items():
    L.append(f"- {name}: {dict(sorted(r.get('exits', {}).items(), key=lambda kv: -kv[1]))}")
L.append("\n## 4. Claude 通过标准（OOS PF>1.2、≥3 个币正收益、组合 MDD<20%、交易数≥100）\n")
L.append("| 版本 | OOS PF | 正收益币数(独立回测) | 正收益币数(组合内) | 组合 MDD | 交易数 | 通过? |")
L.append("|---|---|---|---|---|---|---|")
for name, r in R.items():
    oos = r["oos"]["pf"]; npos = sum(1 for st in r["solo"].values() if st["ret"] > 0)
    npos_p = sum(1 for st in r["per"].values() if st["pnl"] > 0)
    n = r["portfolio"]["trades"]; ok = oos > 1.2 and npos >= 3 and r["mdd"] < 0.20 and n >= 100
    L.append(f"| {name} | {pf(oos)} | {npos}/{NS} | {npos_p}/{NS} | {r['mdd']*100:.1f}% | {n} | {'PASS' if ok else 'FAIL'} |")
labs = []
for r in R.values():
    for k in r.get("periods", {}):
        if k not in labs:
            labs.append(k)
if labs:
    L.append(f"\n## 5. 分阶段表现（组合收益 / 阶段内最大回撤 / 开仓交易数 / PF）\n")
    any_r = next(iter(R.values()))
    ref_row = " | ".join(f"{pct(any_r['periods'][k]['ref_chg'])}" if k in any_r["periods"] else "-" for k in labs)
    L.append("| 版本 | " + " | ".join(labs) + " |")
    L.append("|---|" + "---|" * len(labs))
    L.append(f"| *{meta.get('ref', 'BTC')} 涨跌（参考）* | {ref_row} |")
    for name, r in R.items():
        cells = []
        for k in labs:
            st = r.get("periods", {}).get(k)
            cells.append("-" if st is None else f"{pct(st['ret'])} / {st['mdd']*100:.0f}% / {st['trades']} / {pf(st['pf'])}")
        L.append(f"| {name} | " + " | ".join(cells) + " |")
(HERE / f"report_tables{TAG}.md").write_text("\n".join(L) + "\n")
print("\n".join(L))
