"""由 results_short.pkl 生成 report_short_tables.md 与 equity_short.png（python short.py report）。"""
import datetime as dt, pickle
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
R = pickle.loads((HERE / "results_short.pkl").read_bytes())
CST = dt.timezone(dt.timedelta(hours=8))
f = lambda ms: dt.datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d")
pct = lambda x: f"{x*100:+.1f}%"
pf = lambda x: "∞" if x == float("inf") else f"{x:.2f}"
NAMES = {"A": "A 1h布林回归", "B": "B 4h区间边缘", "C": "C RSI2趋势回调"}
TITLES = {"long4": "长数据 4 币", "long3": "长数据 实盘3币 ETH/SOL/HYPE", "hl4": "HL 6个月 4 币", "hl3": "HL 6个月 实盘3币"}


def max_dd(vals, base):
    pk, dd = base, 0.0
    for v in vals:
        pk = max(pk, v); dd = max(dd, (pk - v) / pk if pk > 0 else 0)
    return dd


def check(r, ns):
    o, a = r["oos"], r["all"]
    chop = r["periods"].get("2024Q2-Q4", {}).get("ret")
    npos = sum(1 for st in r["per"].values() if st["pnl"] > 0)
    c = [o["pf"] > 1.2, a["fee_ratio"] < 0.30, r["mdd"] < 0.20, a["trades"] >= 150, (chop is not None and chop > 0), npos >= 3]
    return c, npos, chop


L = []
for key, res in R.items():
    m = res["_meta"]; ns = len(m["syms"])
    L.append(f"## {TITLES[key]}（{f(m['t0'])} → {f(m['end'])}；样本外 {m['oos_frac']*100:.0f}% 自 {f(m['split'])}，按开仓时间）\n")
    L.append("| 候选 | 收益 | MDD | PF | 胜率 | 交易 | 平均R | 手续费/毛利 | 资金费$ | 持仓时间 | IS PF(n)/收益 | OOS PF(n)/收益 | 与L0月相关 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c in "ABC":
        r = res[c]; a = r["all"]
        L.append(f"| {NAMES[c]} | {pct(r['ret'])} | {r['mdd']*100:.1f}% | {pf(a['pf'])} | {a['win']*100:.0f}% | {a['trades']} | {a['avg_r']:+.3f} | "
                 f"{a['fee_ratio']*100:.0f}% (${a['fees']:.0f}) | {a['funding']:+.1f} | {r['exposure']*100:.0f}% | {pf(r['is']['pf'])}({r['is']['trades']}) / {pct(r['is']['ret'])} | "
                 f"{pf(r['oos']['pf'])}({r['oos']['trades']}) / {pct(r['oos']['ret'])} | {r.get('corr_l0', float('nan')):+.2f} |")
    L.append("\n分币种（组合内盈亏$ / 独立回测收益）：\n")
    L.append("| 候选 | " + " | ".join(m["syms"]) + " |"); L.append("|---|" + "---|" * ns)
    for c in "ABC":
        r = res[c]
        L.append(f"| {NAMES[c]} | " + " | ".join(f"{r['per'][s]['pnl']:+.1f} / {pct(r['solo'][s]['ret'])}" for s in m["syms"]) + " |")
    labs = list(res["A"]["periods"])
    if len(labs) > 1:
        L.append("\n分阶段（组合收益 / 交易数 / PF）：\n")
        L.append("| 候选 | " + " | ".join(labs) + " |"); L.append("|---|" + "---|" * len(labs))
        L.append(f"| *{'BTC' if 'BTC' in m['syms'] else m['syms'][0]} 涨跌* | " + " | ".join(pct(res["A"]["periods"][k]["ref_chg"]) for k in labs) + " |")
        for c in "ABC":
            L.append(f"| {NAMES[c]} | " + " | ".join(f"{pct(res[c]['periods'][k]['ret'])} / {res[c]['periods'][k]['trades']} / {pf(res[c]['periods'][k]['pf'])}" for k in labs) + " |")
    L.append("\n出场原因：" + "；".join(f"{c} {res[c]['exits']}" for c in "ABC"))
    L.append("\n预注册标准（OOS PF>1.2 / 手续费<毛利30% / MDD<20% / 交易≥150 / 2024Q2–Q4>0 / ≥3币正）：\n")
    L.append("| 候选 | OOS PF | 费/毛利 | MDD | 交易 | 震荡段 | 正收益币 | 结果 |"); L.append("|---|---|---|---|---|---|---|---|")
    for c in "ABC":
        r = res[c]; ok, npos, chop = check(r, ns); mk = lambda b: "✓" if b else "✗"
        L.append(f"| {NAMES[c]} | {pf(r['oos']['pf'])} {mk(ok[0])} | {r['all']['fee_ratio']*100:.0f}% {mk(ok[1])} | {r['mdd']*100:.1f}% {mk(ok[2])} | {r['all']['trades']} {mk(ok[3])} | "
                 f"{'n/a' if chop is None else pct(chop)} {mk(ok[4])} | {npos}/{ns} {mk(ok[5])} | {'PASS' if all(ok) else 'FAIL'} |")
    L.append("")

# 合并预览：长数据 4 币；最佳 = 样本内 PF 最高
res = R["long4"]
best = max("ABC", key=lambda c: res[c]["is"]["pf"])
l0 = dict(res["_L0"]["curve"]); sc = dict(res[best]["curve"])
ts = sorted(set(l0) & set(sc))
comb = [(t, 0.5 * l0[t] + 0.5 * sc[t]) for t in ts]
cv = [v for _, v in comb]
L.append(f"## 合并预览（长数据 4 币）：L0 趋势 + 候选 {best}（按预注册规则取样本内 PF 最高：" + "、".join(f"{c} {res[c]['is']['pf']:.2f}" for c in "ABC") + "），各 $500、不切换不再平衡\n")
L.append("| 曲线 | 收益 | MDD | " + " | ".join(p for p in res["A"]["periods"]) + " |")
L.append("|---|---|---|" + "---|" * len(res["A"]["periods"]))
periods = [("2023Q4-2024Q1", 1696118400000, 1711929600000), ("2024Q2-Q4", 1711929600000, 1735689600000),
           ("2025H1", 1735689600000, 1751328000000), ("2025H2", 1751328000000, 1767225600000), ("2026", 1767225600000, 1798761600000)]
def seg(curve):
    out = []
    for lab, a, b in periods:
        s = [v for t, v in curve if a < t <= b]; p = [v for t, v in curve if t <= a]
        out.append(pct(s[-1] / (p[-1] if p else 1000.0) - 1) if s else "-")
    return out
for nm, curve in (("L0 趋势 $1000", [(t, l0[t]) for t in ts]), (f"候选 {best} $1000", [(t, sc[t]) for t in ts]), ("合并 各半", comb)):
    v = [x for _, x in curve]
    L.append(f"| {nm} | {pct(v[-1]/1000-1)} | {max_dd(v,1000)*100:.1f}% | " + " | ".join(seg(curve)) + " |")
(HERE / "report_short_tables.md").write_text("\n".join(L) + "\n")

fig, axs = plt.subplots(2, 1, figsize=(11, 8))
for ax, key in zip(axs, ("long4", "hl4")):
    r = R[key]
    for c in "ABC":
        ax.plot([dt.datetime.fromtimestamp(t / 1000, CST) for t, _ in r[c]["curve"]], [v for _, v in r[c]["curve"]],
                label=f"{c} ({r[c]['ret']*100:+.1f}%, MDD {r[c]['mdd']*100:.1f}%)", lw=1.1)
    if "_L0" in r:
        t0 = r["_meta"]["t0"]
        ax.plot([dt.datetime.fromtimestamp(t / 1000, CST) for t, _ in r["_L0"]["curve"] if t > t0], [v for t, v in r["_L0"]["curve"] if t > t0], label="L0 trend (ref)", lw=1, color="grey", alpha=0.7)
    if key == "long4":
        ax.plot([dt.datetime.fromtimestamp(t / 1000, CST) for t, _ in comb], cv, label=f"L0 + {best} half/half ({cv[-1]/1000*100-100:+.1f}%)", lw=1.3, color="k")
    ax.axvline(dt.datetime.fromtimestamp(r["_meta"]["split"] / 1000, CST), color="k", ls="--", lw=0.8)
    ax.axhline(1000, color="grey", lw=0.5); ax.set_yscale("log"); ax.grid(alpha=0.3); ax.legend(fontsize=8, loc="upper left")
    ax.set_title(f"{'Long data (Binance/OKX) 2023-10→2026-09' if key=='long4' else 'Hyperliquid 2026-03→09'}: short-term candidates, BTC/ETH/SOL/HYPE, $1000 (log scale)", fontsize=10)
fig.tight_layout(); fig.savefig(HERE / "equity_short.png", dpi=120)
print("\n".join(L))
