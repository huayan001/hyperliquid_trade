"""汇总 trend_D 结果 → report_D_tables.md + equity_D.png（只读取已有 pkl，不重跑 TEST）。BT_DATA=long 运行。"""
from __future__ import annotations

import bisect, json, math, pickle, random, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src")); sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))  # 原始回测用 commit f26ef86 的 src 快照（本 PR 之前）
import bt  # noqa: E402
import d as D  # noqa: E402

assert bt.LONG
L = lambda f: pickle.loads((HERE / f).read_bytes())  # noqa: E731
T4, T3, H4, H3 = L("test_long4.pkl"), L("test_long3.pkl"), L("test_hl4.pkl"), L("test_hl3.pkl")
TR, VA = L("train_results.pkl"), L("val_results.pkl")
TH = json.loads((HERE / "thresholds.json").read_text())["thresholds"]
CH = json.loads((HERE / "chosen.json").read_text())
C = CH["chosen"]
NAMES = ["B0", "B1", C]
LAB = {"B0": "B0 L0@2%（实盘现状）", "B1": "B1 L0@1%", C: f"{C} 1%+BTC日线ADX<{TH['D']:.2f}暂停新开仓"}
fam = C.split("-")[0]
key, direction = D.FAMS[fam]
out: list[str] = []
P = out.append


def pc(x, d=1):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x*100:+.{d}f}%"


def pp(x, d=1):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x*100:.{d}f}%"


def f2(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return "∞" if math.isinf(x) else f"{x:.2f}"


def flag(feat: dict) -> bool | None:
    v = feat.get(key) if feat else None
    if v is None:
        return None
    return v > TH[fam] if direction == "high" else v < TH[fam]


def tfeat(run, t):
    return run["feats"].get((t.sym, t.open_ts - H)) or {}


H = bt.H
S4 = T4["seg"]

# ---------------------------------------------------------------- 1 总表
P(f"# trend_D 结果明细（自动生成，rep.py）\n\n选中变体：**{C}**（{CH['rule']}；TRAIN 合格 {CH['eligible']}；VAL 支持={CH['val_support']}）。阈值：{json.dumps({k: round(v, 4) for k, v in TH.items()})}\n")
P("## 1. 全程（2023-10-01 → 2026-09-28，4 币）\n")
P("| 版本 | 交易 | 胜率 | 平均盈利 $ / R | 平均亏损 $ / R | PF | 总收益 | MDD | Sharpe | Calmar | 手续费 $ | 资金费 $ | 扣费后盈利? |")
P("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for n in NAMES:
    s = S4[n]["FULL"]
    P(f"| {LAB[n]} | {s['n']} | {pp(s['win'])} | {s['avg_win']:.1f} / {s['avg_win_r']:.2f}R | {s['avg_loss']:.1f} / {s['avg_loss_r']:.2f}R | {f2(s['pf'])} | {pc(s['ret'])} | {pp(s['mdd'])} | {f2(s['sharpe'])} | {f2(s['calmar'])} | {s['fees']:.0f} | {s['funding']:.0f} | {'是' if s['ret'] > 0 and s['pnl'] > 0 else '否'} |")
P("\n（起始 $1000；平均盈利/亏损为单笔净额，已扣手续费与资金费；R = 净盈亏 / 开仓时风险单位）\n")

# ---------------------------------------------------------------- 2 分段
P("## 2. TRAIN / VAL / TEST / HL 6 个月\n")
P("| 版本 | 段 | 收益 | MDD | Sharpe | Calmar | 交易 | 胜率 | PF | 平均盈利 $ | 平均亏损 $ | 手续费 $ | 资金费 $ |")
P("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for n in NAMES:
    for k in ("TRAIN", "VAL", "TEST"):
        s = S4[n][k]
        P(f"| {LAB[n]} | {k} | {pc(s['ret'])} | {pp(s['mdd'])} | {f2(s['sharpe'])} | {f2(s['calmar'])} | {s['n']} | {pp(s['win'])} | {f2(s['pf'])} | {s['avg_win']:.1f} | {s['avg_loss']:.1f} | {s['fees']:.0f} | {s['funding']:.0f} |")
    s = H4["seg"][n]["HL"]
    P(f"| {LAB[n]} | HL 6个月 | {pc(s['ret'])} | {pp(s['mdd'])} | {f2(s['sharpe'])} | {f2(s['calmar'])} | {s['n']} | {pp(s['win'])} | {f2(s['pf'])} | {s['avg_win']:.1f} | {s['avg_loss']:.1f} | {s['fees']:.0f} | {s['funding']:.0f} |")
P("\n（TRAIN/VAL/TEST 来自同一条 2023-10 起的连续回测，交易按开仓时间归属；HL 6 个月 = data/ 2026-03-20 → 2026-09-29 单独回测，HL 真实资金费）\n")

# ---------------------------------------------------------------- 3 分阶段
P("## 3. 分阶段（组合收益 / 阶段内 MDD / 交易数）\n")
labs = [x[0] for x in bt.PERIODS]
P("| 版本 | " + " | ".join(labs) + " |")
P("|---|" + "---|" * len(labs))
for n in NAMES:
    P(f"| {LAB[n]} | " + " | ".join(f"{pc(S4[n]['PERIODS'][l]['ret'])} / {pp(S4[n]['PERIODS'][l]['mdd'],0)} / {S4[n]['PERIODS'][l]['n']}" for l in labs) + " |")

# ---------------------------------------------------------------- 4 逐币
P("\n## 4. 逐币（4 币组合内，按开仓时间归属，净盈亏 $ / 笔数 / PF）\n")
syms = ["BTC", "ETH", "SOL", "HYPE"]
P("| 版本 | 段 | " + " | ".join(syms) + " |")
P("|---|---|" + "---|" * len(syms))
for n in NAMES:
    for k in ("FULL", "TEST"):
        ps = S4[n][k]["per_sym"]
        P(f"| {LAB[n]} | {k} | " + " | ".join(f"{ps[s]['pnl']:+.1f} / {ps[s]['n']} / {f2(ps[s]['pf'])}" for s in syms) + " |")

# ---------------------------------------------------------------- 5 趋势 vs 震荡
P(f"\n## 5. 趋势 vs 震荡（按选中开关的判定：{key} {'>' if direction == 'high' else '<'} {TH[fam]:.2f} 记为「震荡」，在开仓信号时点打标）\n")
P("### 5a. 交易按开仓时的状态分组\n")
P("| 版本 | 段 | 状态 | 交易 | 胜率 | PF | 净盈亏 $ | ΣR | 平均 R |")
P("|---|---|---|---|---|---|---|---|---|")
cover = {}
for n in NAMES:
    run = T4["runs"][n]
    fl = [(t, flag(tfeat(run, t))) for t in run["trades"]]
    cover[n] = sum(1 for _, f in fl if f is not None) / len(fl)
    for k in ("TRAIN", "VAL", "TEST", "FULL"):
        a, b = D.SEGS[k]
        for st_lab, want in (("趋势", False), ("震荡", True)):
            tr = [t for t, f in fl if a <= t.open_ts < b and f is want]
            s = D.tstats(tr)
            if not tr:
                P(f"| {LAB[n]} | {k} | {st_lab} | 0 | — | — | 0 | 0 | — |")
                continue
            P(f"| {LAB[n]} | {k} | {st_lab} | {s['n']} | {pp(s['win'])} | {f2(s['pf'])} | {s['pnl']:+.1f} | {s['sum_r']:+.1f} | {s['sum_r']/s['n']:+.2f} |")
P(f"\n特征覆盖率（能找到开仓信号时点指标的交易占比）：" + "，".join(f"{n} {v*100:.0f}%" for n, v in cover.items()) + "\n")

# 5b 按小时状态拆分资金曲线
btc = bt.Data("BTC")
d_end = [x.end_ts for x in btc.d1]


def btc_chop_at(t):  # t = 1h 收盘时刻
    j = bisect.bisect_left(d_end, t) - 1
    v = btc.d_adx[j] if j >= 0 else None
    return None if v is None else v < TH["D"]


P("### 5b. 资金曲线按每小时市场状态（BTC 日线 ADX 是否 < 阈值）拆分：该状态下累计收益（各小时收益连乘）\n")
P("| 版本 | 段 | 趋势小时 | 趋势段累计收益 | 震荡小时 | 震荡段累计收益 |")
P("|---|---|---|---|---|---|")
for n in NAMES:
    cv = T4["runs"][n]["curve"]
    for k in ("TRAIN", "VAL", "TEST", "FULL"):
        a, b = D.SEGS[k]
        g = {False: [1.0, 0], True: [1.0, 0]}
        for (t0, v0), (t1, v1) in zip(cv, cv[1:]):
            if not (a < t1 <= b) or v0 <= 0:
                continue
            c = btc_chop_at(t1 - H)  # 该小时开始时已知的状态
            if c is None:
                continue
            g[c][0] *= v1 / v0
            g[c][1] += 1
        P(f"| {LAB[n]} | {k} | {g[False][1]} | {pc(g[False][0]-1)} | {g[True][1]} | {pc(g[True][0]-1)} |")

# ---------------------------------------------------------------- 6 自助法破产风险
def daily_vals(cv):
    days = {}
    for t, v in cv:
        days[(t - 1) // D.DAY] = v
    return [bt.START_EQ] + [days[k] for k in sorted(days)]


def boot(cv, seed=42, paths=10000, horizon=365, block=20):
    v = daily_vals(cv)
    r = [v[i] / v[i - 1] - 1 for i in range(1, len(v))]
    rng = random.Random(seed)
    n = len(r)
    mdds = []
    for _ in range(paths):
        eq, peak, mdd, k = 1.0, 1.0, 0.0, 0
        while k < horizon:
            st = rng.randrange(n)
            for j in range(block):
                if k >= horizon:
                    break
                eq *= 1 + r[(st + j) % n]
                peak = max(peak, eq)
                mdd = max(mdd, 1 - eq / peak)
                k += 1
        mdds.append(mdd)
    mdds.sort()
    return {"p30": sum(m >= 0.30 for m in mdds) / paths, "p50": sum(m >= 0.50 for m in mdds) / paths,
            "med": mdds[paths // 2], "p95": mdds[int(paths * 0.95)]}


BO = {n: boot(T4["runs"][n]["curve"]) for n in NAMES}
P("\n## 6. 破产风险（全程日收益块自助：块长 20 天，10000 条 365 天路径，seed=42）\n")
P("| 版本 | P(1 年内 MDD ≥ 30%) | P(1 年内 MDD ≥ 50%) | MDD 中位数 | MDD 95% 分位 |")
P("|---|---|---|---|---|")
for n in NAMES:
    b = BO[n]
    P(f"| {LAB[n]} | {pp(b['p30'])} | {pp(b['p50'])} | {pp(b['med'])} | {pp(b['p95'])} |")

# ---------------------------------------------------------------- 7 判定
s0, s1, sc = S4["B0"], S4["B1"], S4[C]
tr_elig = TR["seg"][C].get("eligible", False)
live_pos = [s for s in D.LIVE3 if sc["TEST"]["per_sym"][s]["pnl"] > 0]
crit = [
    ("T0 TRAIN 合格且 VAL 支持", f"合格={tr_elig}，VAL Sharpe {VA['seg'][C]['VAL']['sharpe']:.2f} vs B1 {VA['seg']['B1']['VAL']['sharpe']:.2f}", bool(tr_elig and CH["val_support"])),
    ("T1 TEST PF ≥ 1.2", f2(sc["TEST"]["pf"]), sc["TEST"]["pf"] >= 1.2),
    ("T2 TEST 净收益 > 0", pc(sc["TEST"]["ret"]), sc["TEST"]["ret"] > 0),
    ("T3 TEST MDD ≤ B1 TEST MDD", f"{pp(sc['TEST']['mdd'])} vs {pp(s1['TEST']['mdd'])}", sc["TEST"]["mdd"] <= s1["TEST"]["mdd"]),
    ("T4 全程 MDD ≤ 30%", pp(sc["FULL"]["mdd"]), sc["FULL"]["mdd"] <= 0.30),
    ("T5 全程 Sharpe ≥ B1 − 0.1", f"{f2(sc['FULL']['sharpe'])} vs {f2(s1['FULL']['sharpe'])}−0.1", sc["FULL"]["sharpe"] >= s1["FULL"]["sharpe"] - 0.1),
    ("T6 2024Q2–Q4 收益 > B1（样本内）", f"{pc(sc['PERIODS']['2024Q2-Q4']['ret'])} vs {pc(s1['PERIODS']['2024Q2-Q4']['ret'])}", sc["PERIODS"]["2024Q2-Q4"]["ret"] > s1["PERIODS"]["2024Q2-Q4"]["ret"]),
    ("T7 ETH/SOL/HYPE ≥2 个 TEST 净盈利", "，".join(f"{s} {sc['TEST']['per_sym'][s]['pnl']:+.1f}$" for s in D.LIVE3), len(live_pos) >= 2),
    ("T8 HL 6 个月收益 ≥ B1", f"{pc(H4['seg'][C]['HL']['ret'])} vs {pc(H4['seg']['B1']['HL']['ret'])}", H4["seg"][C]["HL"]["ret"] >= H4["seg"]["B1"]["HL"]["ret"]),
    ("T9 TEST 交易 ≥ 40", str(sc["TEST"]["n"]), sc["TEST"]["n"] >= 40),
]
P(f"\n## 7. 判定\n\n### 7a. 1% + 开关（{C}）\n")
P("| 标准 | 数值 | 结果 |\n|---|---|---|")
for a, b, ok in crit:
    P(f"| {a} | {b} | {'通过' if ok else '**不通过**'} |")
passT = all(ok for *_, ok in crit)
P(f"\n**1% + 开关：{'通过' if passT else '不通过'}**\n")
crit_s = [
    ("S1 全程 MDD ≤ 30%", pp(s1["FULL"]["mdd"]), s1["FULL"]["mdd"] <= 0.30),
    ("S2 全程 Sharpe ≥ B0 − 0.1", f"{f2(s1['FULL']['sharpe'])} vs {f2(s0['FULL']['sharpe'])}−0.1", s1["FULL"]["sharpe"] >= s0["FULL"]["sharpe"] - 0.1),
    ("S3 全程 Calmar ≥ 0.9 × B0", f"{f2(s1['FULL']['calmar'])} vs 0.9×{f2(s0['FULL']['calmar'])}", s1["FULL"]["calmar"] >= 0.9 * s0["FULL"]["calmar"]),
    ("S4 P(1 年 MDD ≥ 50%) ≤ 5%", f"{pp(BO['B1']['p50'])}（B0 {pp(BO['B0']['p50'])}）", BO["B1"]["p50"] <= 0.05),
    ("S5 TEST 与 HL MDD 均低于 B0", f"TEST {pp(s1['TEST']['mdd'])} vs {pp(s0['TEST']['mdd'])}；HL {pp(H4['seg']['B1']['HL']['mdd'])} vs {pp(H4['seg']['B0']['HL']['mdd'])}",
     s1["TEST"]["mdd"] < s0["TEST"]["mdd"] and H4["seg"]["B1"]["HL"]["mdd"] < H4["seg"]["B0"]["HL"]["mdd"]),
]
P("### 7b. 只改 1% 风险（B1 相对 B0）\n")
P("| 标准 | 数值 | 结果 |\n|---|---|---|")
for a, b, ok in crit_s:
    P(f"| {a} | {b} | {'通过' if ok else '**不通过**'} |")
passS = all(ok for *_, ok in crit_s)
P(f"\n**只改 1%：{'通过' if passS else '不通过'}**（附：TEST 收益 B1 {pc(s1['TEST']['ret'])} / B0 {pc(s0['TEST']['ret'])}，HL B1 {pc(H4['seg']['B1']['HL']['ret'])} / B0 {pc(H4['seg']['B0']['HL']['ret'])}，符号相同={((s1['TEST']['ret']>0)==(s0['TEST']['ret']>0)) and ((H4['seg']['B1']['HL']['ret']>0)==(H4['seg']['B0']['HL']['ret']>0))}）\n")

# ---------------------------------------------------------------- 8 候选全表
P("## 8. 全部候选（选择阶段；TRAIN 来自截断到 2025-03-10 的运行，VAL 来自截断到 2025-09-10 的运行）\n")
P("| 变体 | TRAIN 收益 | TRAIN MDD | TRAIN Sharpe | TRAIN 交易 | TRAIN PF | 合格 | VAL 收益 | VAL MDD | VAL Sharpe | VAL Calmar | VAL 交易 | VAL PF |")
P("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for n in ["B0", "B1"] + D.CANDS:
    a, v = TR["seg"][n], VA["seg"][n]["VAL"]
    el = a.get("eligible")
    P(f"| {n}{' ←选中' if n == C else ''} | {pc(a['ret'])} | {pp(a['mdd'])} | {f2(a['sharpe'])} | {a['n']} | {f2(a['pf'])} | {'—' if el is None else ('是' if el else '否')} | {pc(v['ret'])} | {pp(v['mdd'])} | {f2(v['sharpe'])} | {f2(v['calmar'])} | {v['n']} | {f2(v['pf'])} |")
nbeat = sum(1 for n in D.CANDS if VA["seg"][n]["VAL"]["sharpe"] > VA["seg"]["B1"]["VAL"]["sharpe"])
P(f"\nVAL Sharpe 高于 B1 的候选：{nbeat}/10。\n")

# ---------------------------------------------------------------- 9 衰减
P("## 9. TRAIN → VAL → TEST 衰减（同一条连续回测）\n")
P("| 版本 | 指标 | TRAIN | VAL | TEST |\n|---|---|---|---|---|")
for n in ("B1", C):
    for lab, f in (("Sharpe", lambda s: f2(s["sharpe"])), ("PF", lambda s: f2(s["pf"])), ("平均 R/笔", lambda s: f2(s["sum_r"] / s["n"]) if s["n"] else "—"),
                   ("MDD", lambda s: pp(s["mdd"])), ("交易", lambda s: str(s["n"]))):
        P(f"| {LAB[n]} | {lab} | " + " | ".join(f(S4[n][k]) for k in ("TRAIN", "VAL", "TEST")) + " |")

# ---------------------------------------------------------------- 10 实盘 3 币
P("\n## 10. 辅助：实盘 3 币 ETH/SOL/HYPE 组合（BTC 只作开关指标，不交易；不计入判定）\n")
P("| 版本 | 全程收益 | 全程 MDD | 全程 Sharpe | 交易 | PF | TEST 收益 | TEST MDD | TEST PF | 2024Q2–Q4 | HL 6个月收益 | HL MDD | HL 交易 |")
P("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for n in NAMES:
    a, h = T3["seg"][n], H3["seg"][n]["HL"]
    P(f"| {LAB[n]} | {pc(a['FULL']['ret'])} | {pp(a['FULL']['mdd'])} | {f2(a['FULL']['sharpe'])} | {a['FULL']['n']} | {f2(a['FULL']['pf'])} | {pc(a['TEST']['ret'])} | {pp(a['TEST']['mdd'])} | {f2(a['TEST']['pf'])} | {pc(a['PERIODS']['2024Q2-Q4']['ret'])} | {pc(h['ret'])} | {pp(h['mdd'])} | {h['n']} |")

(HERE / "report_D_tables.md").write_text("\n".join(out) + "\n")
(HERE / "summary.json").write_text(json.dumps({"pass_switch": passT, "pass_1pct": passS, "chosen": C,
                                               "crit": [(a, b, bool(ok)) for a, b, ok in crit], "crit_s": [(a, b, bool(ok)) for a, b, ok in crit_s],
                                               "boot": BO}, ensure_ascii=False, indent=1))
print("\n".join(out))

# ---------------------------------------------------------------- 图
import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
import datetime as dt  # noqa: E402

plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "WenQuanYi Zen Hei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
tz = dt.timezone(dt.timedelta(hours=8))
X = lambda ts: [dt.datetime.fromtimestamp(t / 1000, tz) for t in ts]  # noqa: E731
col = {"B0": "tab:red", "B1": "tab:blue", C: "tab:green"}
ENG = {"B0": "B0 L0@2% (live)", "B1": "B1 L0@1%", C: f"{C} 1% + pause if BTC dADX<{TH['D']:.1f}"}
fig, ax = plt.subplots(3, 1, figsize=(13, 12), gridspec_kw={"height_ratios": [3, 1.4, 2]})
for n in NAMES:
    cv = T4["runs"][n]["curve"]
    ts, vs = [t for t, _ in cv], [v for _, v in cv]
    ax[0].plot(X(ts), vs, color=col[n], lw=1.2, label=ENG[n])
    pk, dd = 0, []
    for v in vs:
        pk = max(pk, v); dd.append(-(1 - v / pk) * 100)
    ax[1].plot(X(ts), dd, color=col[n], lw=0.9)
    hv = H4["runs"][n]["curve"]
    ax[2].plot(X([t for t, _ in hv]), [v for _, v in hv], color=col[n], lw=1.2, label=ENG[n])
ax[0].set_yscale("log")
for a_ in ax[:2]:
    for t, lab in ((D.TRAIN_END, "VAL"), (D.VAL_END, "TEST")):
        a_.axvline(X([t])[0], color="gray", ls="--", lw=0.8)
    a_.axvspan(X([1711929600000])[0], X([1735689600000])[0], color="orange", alpha=0.08)
ax[0].text(X([D.TRAIN_END])[0], ax[0].get_ylim()[1] * 0.8, " VAL", color="gray")
ax[0].text(X([D.VAL_END])[0], ax[0].get_ylim()[1] * 0.8, " TEST", color="gray")
ax[0].set_title("Long data 2023-10 → 2026-09 (4 coins, $1000 start, log scale; shaded = 2024Q2–Q4 chop)")
ax[0].legend(loc="upper left"); ax[0].grid(alpha=0.3)
ax[1].set_title("Drawdown %"); ax[1].grid(alpha=0.3)
ax[2].set_title("Hyperliquid 6 months 2026-03-20 → 2026-09-29 (4 coins)"); ax[2].legend(loc="lower left"); ax[2].grid(alpha=0.3)
for a_ in ax:
    a_.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m", tz=tz))
fig.tight_layout()
fig.savefig(HERE / "equity_D.png", dpi=110)
