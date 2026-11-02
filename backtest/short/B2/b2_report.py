"""由 results_b2.pkl / val_results.pkl / scan_results.csv 生成 report_B2_tables.md 与 equity_B2.png（不改变任何结果，仅汇总）。
事件级 “趋势K线” 对照为描述性检查（非假设筛选，不参与选择）。"""
from __future__ import annotations

import csv, json, math, pickle, datetime as dt
from pathlib import Path

import b2
from b2 import HERE, S, TH, SYMS, DIRS, SEGS
from feat import TRAIN_START, TRAIN_END, VAL_END, Sym, rows, filt, edge_dir, bucket, oriented

R = pickle.loads((HERE / "results_b2.pkl").read_bytes())
V = pickle.loads((HERE / "val_results.pkl").read_bytes())
ch = R["chosen"]
L = []
P = L.append
pct = lambda x: f"{x*100:+.2f}%"
f2 = lambda x: "inf" if x == float("inf") else f"{x:.2f}"


def row(name, r):
    a = r["all"]
    return (f"| {name} | {a['trades']} | {a['win']*100:.0f}% | ${r['avg_win']:.2f} ({r['avg_win_r']:+.2f}R) | ${r['avg_loss']:.2f} ({r['avg_loss_r']:+.2f}R) | "
            f"{f2(a['pf'])} | {a['avg_r']:+.3f} | {pct(r['ret'])} | {r['mdd']*100:.1f}% | ${a['fees']:.1f} ({a['fee_ratio']*100:.0f}%) | ${a['funding']:+.2f} | {r['exposure']*100:.0f}% |")


HDR = "| 段 | 笔数 | 胜率 | 平均盈利 | 平均亏损 | PF | 平均R | 收益 | 最大回撤 | 手续费(占毛利) | 资金费(+为收入) | 持仓时间占比 |\n|---|---|---|---|---|---|---|---|---|---|---|---|"
P(f"# B2 结果表（自动生成 {dt.datetime.now(dt.timezone(dt.timedelta(hours=8))):%Y-%m-%d %H:%M} CST）\n")
P(f"选中变体：**{ch}**（{json.loads((HERE/'chosen.json').read_text())['rule']}）。起始资金 $1000，单笔风险 0.5%。\n")
P("## 1. VALIDATION 上 4 个变体（仅用于选择）\n"); P(HDR)
for v, r in V.items():
    P(row(v, r))
P("\n## 2. 选中变体各段（TRAIN 为样本内；TEST 只运行一次；HL 为补充确认）\n"); P(HDR)
for k, lab in (("train", "TRAIN 2023-10-01→2025-03-09"), ("val", "VALIDATION 2025-03-10→09-09"), ("test", "TEST 2025-09-10→2026-09-28"),
               ("full", "全程连续 2023-10→2026-09"), ("hl", "HL 6个月 2026-03-20→")):
    P(row(lab, R[k]))

t = R["test"]; a = t["all"]
pos_syms = sum(1 for s in SYMS if t["per"][s]["pnl"] > 0)
crit = [("PF > 1.2", f2(a["pf"]), a["pf"] > 1.2), ("净收益 > 0", pct(t["ret"]), t["ret"] > 0), ("最大回撤 < 20%", f"{t['mdd']*100:.1f}%", t["mdd"] < 0.2),
        ("手续费 < 30% 毛利", f"{a['fee_ratio']*100:.0f}%", a["fee_ratio"] < 0.3), ("笔数 ≥ 50", str(a["trades"]), a["trades"] >= 50),
        ("≥3/4 币种盈利", f"{pos_syms}/4", pos_syms >= 3)]
P("\n## 3. TEST 预注册标准\n\n| 标准 | TEST 值 | 通过 |\n|---|---|---|")
for c, v, ok in crit:
    P(f"| {c} | {v} | {'✅' if ok else '❌'} |")
P(f"\n**总判定：{'通过' if all(x[2] for x in crit) else '未通过'}**（{sum(x[2] for x in crit)}/6 项）\n")

P("## 4. 分币种（净 PnL $ / 笔数 / PF）\n\n| 币种 | TRAIN | VAL | TEST | 全程 | HL 6个月 |\n|---|---|---|---|---|---|")
for s in SYMS:
    cells = []
    for k in ("train", "val", "test", "full", "hl"):
        p = R[k]["per"][s]
        cells.append(f"{p['pnl']:+.1f} / {p['trades']} / {f2(p['pf'])}" if p["trades"] else "—")
    P(f"| {s} | " + " | ".join(cells) + " |")

P("\n## 5. 多空拆分（净 PnL $ / 笔数 / PF）\n\n| 方向 | TRAIN | VAL | TEST | HL |\n|---|---|---|---|---|")
for sd in ("long", "short"):
    P(f"| {sd} | " + " | ".join(f"{R[k]['sides'][sd]['pnl']:+.1f} / {R[k]['sides'][sd]['trades']} / {f2(R[k]['sides'][sd]['pf'])}" for k in ("train", "val", "test", "hl")) + " |")

P("\n## 6. 分阶段（全程连续回测；2023Q4–2025Q1 前段属 TRAIN 样本内）\n\n| 阶段 | 笔数 | 胜率 | PF | 收益 | 阶段内回撤 |\n|---|---|---|---|---|---|")
for lab, ps in R["full"]["periods"].items():
    P(f"| {lab} | {ps['trades']} | {ps['win']*100:.0f}% | {f2(ps['pf'])} | {pct(ps['ret'])} | {ps['mdd']*100:.1f}% |")


# ---- 事件级：同一组条件在 震荡(RB) vs 趋势(非RB) K线上的 +24h 顺势收益（扣 0.08% 成本与资金费）
def conds_any(r, regime):
    s = r["sym"]
    if r["dadx"] is None or not edge_dir(r):
        return False
    if (regime == "range") != filt(r, "RB"):
        return False
    f = TH["F2"][s]; out = []
    if r["adxs"] is not None: out.append(bucket(r["adxs"], f["adxs"]) == 0)
    if r["dist"] is not None and r["rsi4"] is not None:
        out.append(oriented(r, "rsi4") <= TH["F3_rsi4_40"][s] and bucket(r["dist"], f["dist"]) == 0)
    if r["vov"] is not None: out.append(bucket(r["vov"], f["vov"]) == 3)
    return any(out)


def ev(ds, segs):
    fs = {s: Sym(DIRS[ds], s) for s in SYMS}
    res = {}
    for s in SYMS:
        for r in rows(fs[s], fs["BTC"], with_forward=True):
            if r.get("r24") is None: continue
            seg = next((k for k, (a0, b0) in segs.items() if a0 <= r["T"] < b0), None)
            if seg is None: continue
            for reg in ("range", "trend"):
                if conds_any(r, reg):
                    d = -edge_dir(r)
                    res.setdefault((seg, reg), []).append(d * r["r24"] - 0.0008 - d * r["f24"])
    return res


def summ(v):
    if not v: return "—"
    m = sum(v) / len(v); return f"n={len(v)}, 净 {m*1e4:+.0f}bp, 胜率 {sum(x>0 for x in v)/len(v)*100:.0f}%"


E = ev("long", {"TRAIN": (TRAIN_START, TRAIN_END), "VAL": (TRAIN_END, VAL_END), "TEST": (VAL_END, 1_900_000_000_000)})
EH = ev("hl", {"HL": SEGS["hl"]})
E.update(EH)
P("\n## 7. 市场状态：同一组条件（RS2 的三个条件，任一成立，顺突破方向，持有24h）在震荡K线 vs 趋势K线上的事件级收益\n")
P("（事件级 = 4h收盘时刻进场、+24h 平仓的理论收益，已扣 0.08% 往返成本和资金费，未经限价成交筛选；重叠持有，非独立样本）\n")
P("| 段 | 震荡K线 (RB: 4h ADX<25 且 日线ADX<22.5) | 趋势K线 (非RB) |\n|---|---|---|")
for seg in ("TRAIN", "VAL", "TEST", "HL"):
    P(f"| {seg} | {summ(E.get((seg,'range')))} | {summ(E.get((seg,'trend')))} |")

# scan 中三个条件在其它震荡过滤器下的 TRAIN 表现
SC = list(csv.DictReader(open(HERE / "scan_results.csv")))
P("\n扫描（TRAIN）中三个存活条件在其它过滤器下（目标 s24，负均值=顺突破方向；t 为按周聚类）：\n\n| 条件 | 过滤器 | n | 均值(bp,s24) | 扣成本后顺势(bp) | t | BH q |\n|---|---|---|---|---|---|---|")
for fam, feat, bk, lab in (("F2", "adxs", "0", "adxs 最低五分位"), ("F3", "dist", "0", "RSI拉伸 且 距边最近"), ("F2", "vov", "3", "vol-of-vol 第4五分位")):
    for flt in ("ALL", "RA", "RB", "RC"):
        m = [r for r in SC if r["family"] == fam and r["feature"] == feat and r["bucket"] == bk and r["filter"] == flt and r["target"] == "s" and r["h"] == "24"]
        for r in m:
            P(f"| {lab} | {flt} | {r['n']} | {float(r['mean'])*1e4:+.0f} | {float(r['mean_net'])*1e4:+.0f} | {float(r['t']):.2f} | {float(r['bh_q']):.3f} |")

# ---- 衰减
ed = R["event_decay"]
P("\n## 8. 过拟合 / 衰减\n\n| 指标 | TRAIN | VAL | TEST | HL |\n|---|---|---|---|---|")
P("| 策略 PF | " + " | ".join(f2(R[k]["all"]["pf"]) for k in ("train", "val", "test", "hl")) + " |")
P("| 策略 平均R | " + " | ".join(f"{R[k]['all']['avg_r']:+.3f}" for k in ("train", "val", "test", "hl")) + " |")
P("| 事件级净收益(bp/次, 震荡K线) | " + " | ".join(summ(E.get((k, "range"))) for k in ("TRAIN", "VAL", "TEST", "HL")) + " |")
P(f"\n（b2.py test 输出的 event_decay 中 train 段未设下界，含 2023-10-01 之前的预热期事件：{ed}；上表按 TRAIN 下界重算。）")

# ---- 与 L0 相关、合并
l0 = R["L0_curve"]; bc = R["full"]["curve"]
m_b, m_l = S.monthly(bc), S.monthly(l0)
P(f"\n## 9. 与 L0 趋势（实盘口径）月度收益相关性：全程 {R['corr_l0_full']:+.3f}（{len(set(m_b)&set(m_l))} 个月）\n")


def step(curve, ts):
    out, j, last = [], 0, curve[0][1]
    for t0 in ts:
        while j < len(curve) and curve[j][0] <= t0:
            last = curve[j][1]; j += 1
        out.append(last)
    return out


ts = sorted(t0 for t0, _ in l0 if t0 >= max(bc[0][0], l0[0][0]))
lv, bv = step(l0, ts), step(bc, ts)
ln = [x / lv[0] for x in lv]; bn = [x / bv[0] for x in bv]; cn = [0.5 * x + 0.5 * y for x, y in zip(ln, bn)]


def mret(ts, v):
    d = {}
    for t0, x in zip(ts, v):
        d[dt.datetime.fromtimestamp((t0 - 1) / 1000, dt.timezone.utc).strftime("%Y-%m")] = x
    ks = sorted(d); prev = v[0]; o = []
    for k in ks:
        o.append(d[k] / prev - 1); prev = d[k]
    return o


def sh(o):
    m = sum(o) / len(o); sd = math.sqrt(sum((x - m) ** 2 for x in o) / (len(o) - 1)); return m / sd * math.sqrt(12) if sd else float("nan")


P("合并预览（各半资金 $500+$500，不再平衡，全程 2023-10→2026-09）：\n\n| 组合 | 收益 | 最大回撤 | 月度Sharpe(年化) |\n|---|---|---|---|")
for lab, v in (("L0 单独 ($1000)", ln), ("B2 单独 ($1000)", bn), ("L0 $500 + B2 $500", cn)):
    P(f"| {lab} | {pct(v[-1]-1)} | {S.max_dd(v, 1.0)*100:.1f}% | {sh(mret(ts, v)):.2f} |")
# TEST 段合并
k0 = next(i for i, t0 in enumerate(ts) if t0 >= VAL_END)
P("\nTEST 段（2025-09-10→）同口径：\n\n| 组合 | 收益 | 最大回撤 |\n|---|---|---|")
for lab, v in (("L0 单独", ln), ("B2 单独", bn), ("L0+B2 各半", cn)):
    seg = [x / v[k0] for x in v[k0:]]
    P(f"| {lab} | {pct(seg[-1]-1)} | {S.max_dd(seg, 1.0)*100:.1f}% |")

(HERE / "report_B2_tables.md").write_text("\n".join(L) + "\n")

import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
dts = [dt.datetime.fromtimestamp(t0 / 1000, dt.timezone.utc) for t0 in ts]
fig, ax = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
ax[0].plot(dts, bn, label=f"B2 {ch} (0.5% risk)"); ax[0].set_ylabel("equity (x)")
for a0, b0, c, lab in ((TRAIN_START, TRAIN_END, "#ddeeff", "TRAIN"), (TRAIN_END, VAL_END, "#fff2cc", "VAL"), (VAL_END, ts[-1], "#f4cccc", "TEST")):
    for x in ax: x.axvspan(dt.datetime.fromtimestamp(a0/1000, dt.timezone.utc), dt.datetime.fromtimestamp(b0/1000, dt.timezone.utc), color=c, alpha=0.6, label=lab if x is ax[0] else None)
ax[0].legend(); ax[0].set_title("B2 equity, long data (TEST run once)")
ax[1].plot(dts, ln, label="L0 alone"); ax[1].plot(dts, cn, label="L0 50% + B2 50%"); ax[1].set_yscale("log"); ax[1].legend(); ax[1].set_ylabel("equity (x, log)")
hc = R["hl"]["curve"]
if hc:
    ins = ax[0].inset_axes([0.05, 0.55, 0.3, 0.35])
    ins.plot([dt.datetime.fromtimestamp(t0/1000, dt.timezone.utc) for t0, _ in hc], [v / S.START_EQ for _, v in hc], color="k")
    ins.set_title("HL 6m", fontsize=8); ins.tick_params(labelsize=6)
fig.tight_layout(); fig.savefig(HERE / "equity_B2.png", dpi=110)
print("\n".join(L))
