import pickle, datetime as dt
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

R = pickle.loads(Path("results.pkl").read_bytes())
meta = R.pop("_meta")
CST = dt.timezone(dt.timedelta(hours=8))
f = lambda ms: dt.datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d %H:%M")
print(f(meta["test_start"]), f(meta["split_ts"]), f(meta["end_ts"]), meta)

fig, (ax, ax2) = plt.subplots(2, 1, figsize=(11, 7.5), sharex=True, gridspec_kw={"height_ratios": [3, 1.3]})
labels = {"V0 现行": "V0 current", "V1 1h+分批": "V1 1h entry+BE+partial1.5R+chandelier2ATR",
          "V2 1h+金字塔": "V2 V1+partial2R+chandelier2.5ATR+2 adds", "V3 V0+吊灯保本": "V3 V0+chandelier+BE@1R"}
for name, r in R.items():
    xs = [dt.datetime.fromtimestamp(t / 1000, CST) for t, _ in r["curve"]]
    ys = [v for _, v in r["curve"]]
    ax.plot(xs, ys, label=f"{labels[name]}  ({r['ret']*100:+.1f}%, MDD {r['mdd']*100:.1f}%)", lw=1.3)
    peak, dd = ys[0], []
    for v in ys:
        peak = max(peak, v); dd.append((v / peak - 1) * 100)
    ax2.plot(xs, dd, lw=1)
for a in (ax, ax2):
    a.axvline(dt.datetime.fromtimestamp(meta["split_ts"] / 1000, CST), color="k", ls="--", lw=0.8)
ax.axhline(1000, color="grey", lw=0.6)
ax.set_title("Hyperliquid BTC/ETH/SOL/HYPE portfolio backtest (start $1000, 2% risk/trade, max 2 pos, fees+funding)\n"
             "left of dashed line = in-sample (60%), right = out-of-sample (40%)", fontsize=10)
ax.set_ylabel("equity $"); ax2.set_ylabel("drawdown %")
ax.legend(fontsize=8, loc="lower left"); ax.grid(alpha=0.3); ax2.grid(alpha=0.3)
fig.tight_layout(); fig.savefig("equity.png", dpi=130)

def pct(x): return f"{x*100:+.1f}%"
def pf(x): return "∞" if x == float("inf") else f"{x:.2f}"
def gb(st): return "n/a" if st["gb_n"] == 0 else f"{st['giveback']*100:.0f}% (n={st['gb_n']})"

L = []
L.append("# 回测报告：浮盈回吐问题 —— V0 现行策略 vs 短周期方案（V1/V2）vs 最小改动（V3）\n")
L.append(f"- 数据：Hyperliquid 官方 candleSnapshot 1h/4h/1d（BTC、ETH、SOL、HYPE），资金费用 fundingHistory 实际值（{'、'.join(meta['funding_proxy']) or '无'} 缺数据，用 BTC 资金费代理）")
L.append(f"- 回测区间（北京时间）：{f(meta['test_start'])} → {f(meta['end_ts'])}，共 {meta['bars']} 根 1h；之前的数据只用来预热日线 EMA50/ADX")
L.append(f"- 样本内/样本外：前 60% 为样本内（至 {f(meta['split_ts'])}），后 40% 为样本外。所有参数都是事先定好的（用你给的数值或现行配置），**没有做任何参数寻优**")
L.append("- 成本：taker 0.045%、maker 0.015%（只有 V1/V2 的分批止盈限价单按 maker 计），止损/市价单另加 0.02% 滑点，按小时计资金费")
L.append("- 组合规则与实盘一致：初始 $1000，单笔风险 2% 权益，最多同时 2 仓，组合止损风险 ≤4%，10x 逐仓（止损距离 ≤5%）；信号在 1h 收盘判定、下一根 1h 开盘成交，无前视")
L.append("- 环境过滤：直接调用仓库 `hl_bot.regime`（日线 EMA20/50 + ADX>22.5，1h 区间冲突时观望；BTC 做空要求 ADX>25）\n")
L.append("## 1. 组合结果（四个币共用一个账户）\n")
L.append("| 版本 | 总收益 | 最大回撤 | 交易数 | 胜率 | 盈亏比 PF | 平均 R | 峰值浮盈回吐率* | 持仓时间占比 | 手续费 | 样本内 收益/PF | 样本外 收益/PF |")
L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
for name, r in R.items():
    p = r["portfolio"]
    L.append(f"| {name} | {pct(r['ret'])} | {r['mdd']*100:.1f}% | {p['trades']} | {p['win']*100:.0f}% | {pf(p['pf'])} | {p['avg_r']:+.2f} | {gb(p)} | {r['exposure']*100:.0f}% | ${r['fees']:.0f} | {pct(r['is']['ret'])} / {pf(r['is']['pf'])} | {pct(r['oos']['ret'])} / {pf(r['oos']['pf'])} |")
L.append("\n\\* 峰值浮盈回吐率 = (持仓期间最高浮盈 − 最终盈亏) / 最高浮盈，只统计最高浮盈 ≥0.5R 的交易（R = 开仓时权益的 2%）；>100% 表示由盈转亏。浮盈按 1h 收盘价计。\n")
L.append("## 2. 分币种（每个币单独 $1000 独立回测，不受「最多 2 仓 / 币种顺序」影响）\n")
L.append("| 版本 | 币种 | 交易数 | 胜率 | 总收益 | 最大回撤 | PF | 平均 R | 浮盈回吐率 | 持仓时间占比 |")
L.append("|---|---|---|---|---|---|---|---|---|---|")
for name, r in R.items():
    for s, st in r["solo"].items():
        L.append(f"| {name} | {s} | {st['trades']} | {st['win']*100:.0f}% | {pct(st['ret'])} | {st['mdd']*100:.1f}% | {pf(st['pf'])} | {st['avg_r']:+.2f} | {gb(st)} | {st['exposure']*100:.0f}% |")
Path("report_tables.md").write_text("\n".join(L) + "\n")
print("\n".join(L))
