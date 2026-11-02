"""事后描述性分析（不参与选择）：
1) TEST 段 D-P 与 B1 交易的重合 / 各自独有交易（只读 test_long4.pkl，不重跑 TEST）
2) D-P 阈值敏感性：只在 TRAIN+VAL 上（时间线截断到 2025-09-10），不触碰 TEST。"""
import json, pickle, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src")); sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))  # 原始回测用 commit f26ef86 的 src 快照（本 PR 之前）
import bt, d as D
from multiprocessing import Pool

T4 = pickle.loads((HERE / "test_long4.pkl").read_bytes())
out = []
P = out.append
H = bt.H
for segk in ("TEST", "FULL"):
    a, b = D.SEGS[segk]
    b1 = {(t.sym, t.side, t.open_ts): t for t in T4["runs"]["B1"]["trades"] if a <= t.open_ts < b}
    dp = {(t.sym, t.side, t.open_ts): t for t in T4["runs"]["D-P"]["trades"] if a <= t.open_ts < b}
    common = set(b1) & set(dp)
    only_b1, only_dp = set(b1) - common, set(dp) - common
    st = lambda ks, src: D.tstats([src[k] for k in ks])
    P(f"### {segk}：D-P 与 B1 交易对照（按 标的+方向+开仓时间 精确匹配）\n")
    P("| 组 | 笔数 | 胜率 | PF | 净盈亏 $ | ΣR |\n|---|---|---|---|---|---|")
    for lab, ks, src in (("两者共有（B1 口径）", common, b1), ("两者共有（D-P 口径）", common, dp), ("只有 B1 开（D-P 暂停或错过）", only_b1, b1), ("只有 D-P 开（暂停后延后入场/空出仓位）", only_dp, dp)):
        s = st(ks, src)
        P(f"| {lab} | {s['n']} | {s['win']*100:.1f}% | {s['pf']:.2f} | {s['pnl']:+.1f} | {s['sum_r']:+.1f} |")
    # 只有 B1 的交易，其开仓时 D 标志
    fl = [D.DSim.is_chop(type('x', (), {'th': json.loads((HERE/'thresholds.json').read_text())['thresholds']})(), T4['runs']['B1']['feats'].get((k[0], k[2]-H), {}), 'D') for k in only_b1]
    P(f"\n只有 B1 开的 {len(only_b1)} 笔里，开仓时被 D 判为震荡的 {sum(fl)} 笔。\n")
(HERE / "posthoc_overlap.md").write_text("\n".join(out) + "\n")
print("\n".join(out))

# 2) 敏感性（TRAIN+VAL）
tr = pickle.loads((HERE / "train_results.pkl").read_bytes())
b1 = tr["runs"]["B1"]
vals = sorted(f["btc_adx"] for t in b1["trades"] if D.TRAIN_START <= t.open_ts < D.TRAIN_END for f in [b1["feats"][(t.sym, t.open_ts - H)]] if f.get("btc_adx") is not None)
grid = {"q0.20": D.quantile(vals, 0.20), "q0.25": D.quantile(vals, 0.25), "q1/3(选中)": D.quantile(vals, 1/3), "q0.40": D.quantile(vals, 0.40), "q0.50": D.quantile(vals, 0.50), "固定20": 20.0, "固定25": 25.0}
th0 = json.loads((HERE / "thresholds.json").read_text())["thresholds"]
D.setup(th0)


def run(args):
    lab, thv = args
    sim = D.DSim(D.G["data"], D.L0(), 0.01, D.G["btc"], "D-P", {**th0, "D": thv})
    sim.run([t for t in D.G["tl"] if t + H <= D.VAL_END])
    return lab, thv, D.seg(sim, *D.SEGS["TRAIN"]), D.seg(sim, *D.SEGS["VAL"])


with Pool(7) as pool:
    res = pool.map(run, list(grid.items()))
o2 = ["### D-P 阈值敏感性（事后，只用 TRAIN+VAL，不参与选择；B1：TRAIN Sharpe 1.08 / MDD 31.9%，VAL Sharpe 1.34 / MDD 16.0%）\n",
      "| 阈值 | BTC 日线 ADX | TRAIN 收益 | TRAIN MDD | TRAIN Sharpe | TRAIN 交易 | VAL 收益 | VAL MDD | VAL Sharpe | VAL 交易 |", "|---|---|---|---|---|---|---|---|---|---|"]
for lab, thv, a, v in res:
    o2.append(f"| {lab} | {thv:.2f} | {a['ret']*100:+.1f}% | {a['mdd']*100:.1f}% | {a['sharpe']:.2f} | {a['n']} | {v['ret']*100:+.1f}% | {v['mdd']*100:.1f}% | {v['sharpe']:.2f} | {v['n']} |")
(HERE / "posthoc_sensitivity.md").write_text("\n".join(o2) + "\n")
print("\n".join(o2))
