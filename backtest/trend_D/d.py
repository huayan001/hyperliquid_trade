"""方向一 trend_D：L0 趋势 + 单笔风险 1% + chop 开关（见 D_PLAN.md / D_DEVIATIONS.md）。

BT_DATA=long python d.py repro   → 复现 L0@2%（已知 +218.6% / 51.8%），并验证子类钩子不改变结果
python d.py repro_hl              → HL 6 个月复现 L0@2%（已知 −18.4% / 27.8%）
BT_DATA=long python d.py train   → B1 TRAIN → thresholds.json；10 个候选 TRAIN → train_results.json
BT_DATA=long python d.py val     → 需 D_DEVIATIONS.md；VAL → chosen.json
BT_DATA=long python d.py test    → 只允许一次（test_done.flag）；B0/B1/选中 全程 + HL + 3 币
"""
from __future__ import annotations

import bisect, datetime as dt, json, math, os, pickle, subprocess, sys
from multiprocessing import Pool
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src"))  # 原始回测用 commit f26ef86 的 src 快照（本 PR 之前）
sys.path.insert(0, str(HERE.parent))
import bt  # noqa: E402

H = bt.H
DAY = 24 * H
TRAIN_START, TRAIN_END, VAL_END, END = 1696118400000, 1741564800000, 1757462400000, 1_900_000_000_000
HL_START = 1773964800000
SEGS = {"TRAIN": (TRAIN_START, TRAIN_END), "VAL": (TRAIN_END, VAL_END), "TEST": (VAL_END, END),
        "FULL": (TRAIN_START, END), "HL": (HL_START, END)}
FAMS = {"A": ("adx_d", "low"), "B": ("ci_d", "high"), "C": ("er_4h", "low"), "D": ("btc_adx", "low"), "E": ("r30", "low")}
CANDS = [f"{f}-{m}" for f in "ABCDE" for m in "RP"]
LIVE3 = ("ETH", "SOL", "HYPE")
CST = dt.timezone(dt.timedelta(hours=8))


def now_cst() -> str:
    return dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST")


# ------------------------------------------------------------------ 指标（只用已收盘数据）
def choppiness(c: list, n: int = 14) -> list:
    out = [None] * len(c)
    for j in range(n, len(c)):
        trs = [max(c[k].high, c[k - 1].close) - min(c[k].low, c[k - 1].close) for k in range(j - n + 1, j + 1)]
        rng = max(x.high for x in c[j - n + 1 : j + 1]) - min(x.low for x in c[j - n + 1 : j + 1])
        out[j] = 100 * math.log10(sum(trs) / rng) / math.log10(n) if rng > 0 else None
    return out


def eff_ratio(c: list, n: int = 30) -> list:
    out = [None] * len(c)
    for j in range(n, len(c)):
        path = sum(abs(c[k].close - c[k - 1].close) for k in range(j - n + 1, j + 1))
        out[j] = abs(c[j].close - c[j - n].close) / path if path > 0 else None
    return out


def prep(d: bt.Data) -> None:
    d.d_ci = choppiness(d.d1, 14)
    d.h4_er = eff_ratio(d.h4, 30)
    d.d_end = [x.end_ts for x in d.d1]


def load_all(syms) -> tuple[dict, bt.Data]:
    data = {s: bt.Data(s) for s in syms}
    btc = data["BTC"] if "BTC" in data else bt.Data("BTC")
    for d in set(list(data.values()) + [btc]):
        prep(d)
    return data, btc


def timeline_of(data: dict) -> list[int]:
    tl = sorted(set().union(*[set(d.idx) for d in data.values()]))
    return [t for t in tl if all(t in d.idx for d in data.values()) or t >= bt.TEST_START]


# ------------------------------------------------------------------ 模拟器（只加钩子，不改 L0 语义）
class CurveLog(list):
    def __init__(self, sim):
        super().__init__()
        self.sim, self.log = sim, []

    def append(self, x):
        super().append(x)
        self.log.append((self.sim.fees, self.sim.funding_paid))


class DSim(bt.Sim):
    def __init__(self, data, p, risk: float, btc: bt.Data, variant: str | None = None, th: dict | None = None):
        super().__init__(data, p)
        self.curve = CurveLog(self)
        self.risk, self.btc, self.variant, self.th = risk, btc, variant, th or {}
        self.fam, self.mode = (variant.split("-") if variant else (None, None))
        self.feats: dict = {}  # (sym, 信号 1h 开盘 ts) -> 指标
        self.actions: list = []  # (ts, sym, "pause"/"reduce")

    def features(self, s: str, i: int, ts: int) -> dict:
        d = self.D[s]
        di, j = d.d_at[i], d.h4_at[i]
        close_t = ts + H
        bj = bisect.bisect_left(self.btc.d_end, close_t) - 1  # BTC 最后一根已收盘日线
        r30 = sum(t.pnl / t.risk_unit for t in self.trades if t.risk_unit > 0 and close_t - 30 * DAY < t.close_ts <= close_t)
        return {
            "adx_d": d.d_adx[di] if di >= 0 else None,
            "ci_d": d.d_ci[di] if di >= 0 else None,
            "er_4h": d.h4_er[j] if j >= 0 else None,
            "btc_adx": self.btc.d_adx[bj] if bj >= 0 else None,
            "r30": r30,
        }

    def is_chop(self, f: dict, fam: str) -> bool:
        key, direction = FAMS[fam]
        v, t = f.get(key), self.th.get(fam)
        if v is None or t is None:
            return False
        return v > t if direction == "high" else v < t

    def _v0(self, s, i, ts, eq):
        super()._v0(s, i, ts, eq)
        act = self.pending.get(s)
        if not act or act[0] != "open" or s in self.pos:
            return
        f = self.features(s, i, ts)
        self.feats[(s, ts)] = f
        if self.fam and self.is_chop(f, self.fam):
            if self.mode == "P":
                del self.pending[s]
                self.actions.append((ts, s, "pause"))
            else:
                act[4]["chop_mult"] = 0.5
                self.actions.append((ts, s, "reduce"))

    def _do_pending(self, s, act, i, i_map):
        if act[0] != "open":
            return super()._do_pending(s, act, i, i_map)
        old = bt.RISK
        bt.RISK = self.risk * act[4].get("chop_mult", 1.0)
        try:
            return super()._do_pending(s, act, i, i_map)
        finally:
            bt.RISK = old


def L0() -> bt.Params:
    return bt.Params("L0 实盘趋势", "v0", partial_r=None, **bt.LIVE)


def spec(name: str) -> tuple[float, str | None]:
    if name == "B0":
        return 0.02, None
    if name == "B1":
        return 0.01, None
    return 0.01, name


# ------------------------------------------------------------------ 统计
def trade_feats(sim: DSim, t) -> dict:
    return sim.feats.get((t.sym, t.open_ts - H)) or {}


def daily_series(curve, a, b, base):
    pts = [(t, v) for t, v in curve if a < t <= b]
    days = {}
    for t, v in pts:
        days[(t - 1) // DAY] = v
    vals = [base] + [days[k] for k in sorted(days)]
    return vals


def sharpe(vals):
    r = [vals[k] / vals[k - 1] - 1 for k in range(1, len(vals)) if vals[k - 1] > 0]
    if len(r) < 2:
        return float("nan")
    m = sum(r) / len(r)
    sd = (sum((x - m) ** 2 for x in r) / (len(r) - 1)) ** 0.5
    return m / sd * math.sqrt(365) if sd > 0 else float("nan")


def tstats(trades) -> dict:
    n = len(trades)
    w = [t for t in trades if t.pnl > 0]
    l = [t for t in trades if t.pnl <= 0]
    gw, gl = sum(t.pnl for t in w), -sum(t.pnl for t in l)
    R = lambda ts: [t.pnl / t.risk_unit for t in ts if t.risk_unit > 0]  # noqa: E731
    return {
        "n": n, "win": len(w) / n if n else float("nan"), "pnl": sum(t.pnl for t in trades),
        "pf": gw / gl if gl > 0 else (float("inf") if gw > 0 else float("nan")),
        "avg_win": gw / len(w) if w else float("nan"), "avg_loss": -gl / len(l) if l else float("nan"),
        "avg_win_r": (sum(R(w)) / len(R(w))) if R(w) else float("nan"),
        "avg_loss_r": (sum(R(l)) / len(R(l))) if R(l) else float("nan"),
        "sum_r": sum(R(trades)),
    }


def seg(sim: DSim, a: int, b: int) -> dict:
    curve = list(sim.curve)
    cv = [(t, v) for t, v in curve if a < t <= b]
    if len(cv) < 2:
        return {}
    prev = [(k, v) for k, (t, v) in enumerate(curve) if t <= a]
    base = prev[-1][1] if prev else bt.START_EQ
    k0 = prev[-1][0] if prev else None
    k1 = max(k for k, (t, _) in enumerate(curve) if t <= b)
    f0 = sim.curve.log[k0] if k0 is not None else (0.0, 0.0)
    f1 = sim.curve.log[k1]
    vals = [v for _, v in cv]
    mdd = bt.max_dd(vals, base)
    t0 = max(a, cv[0][0] - H)
    days = (cv[-1][0] - t0) / DAY
    ret = vals[-1] / base - 1
    cagr = (vals[-1] / base) ** (365 / days) - 1 if days > 0 and vals[-1] > 0 else float("nan")
    tr = [t for t in sim.trades if a <= t.open_ts < b]
    st = tstats(tr)
    st.update({
        "ret": ret, "mdd": mdd, "cagr": cagr, "calmar": cagr / mdd if mdd > 0 else float("nan"),
        "sharpe": sharpe(daily_series(curve, a, b, base)), "days": days,
        "fees": f1[0] - f0[0], "funding": f1[1] - f0[1],
        "per_sym": {s: tstats([t for t in tr if t.sym == s]) for s in sim.D},
        "paused": sum(1 for x in sim.actions if a <= x[0] < b and x[2] == "pause"),
        "reduced": sum(1 for x in sim.actions if a <= x[0] < b and x[2] == "reduce"),
        "span": (t0, cv[-1][0]),
    })
    return st


def run_one(args):
    name, end, syms_key = args
    data, btc, th = G["data"], G["btc"], G["th"]
    risk, var = spec(name)
    sim = DSim(data, L0(), risk, btc, var, th)
    tl = [t for t in G["tl"] if t + H <= end]
    sim.run(tl)
    return name, sim


def pack(sim: DSim) -> dict:
    """可 pickle 的结果（交易、曲线、特征、动作）。"""
    return {"trades": sim.trades, "curve": list(sim.curve), "log": sim.curve.log, "feats": sim.feats,
            "actions": sim.actions, "fees": sim.fees, "funding": sim.funding_paid, "risk": sim.risk,
            "variant": sim.variant, "syms": list(sim.D)}


G: dict = {}


def setup(th=None):
    syms = bt.SYMS
    G["data"], G["btc"] = load_all(syms)
    G["tl"] = timeline_of(G["data"])
    G["th"] = th or {}


def run_many(names, end, procs=6):
    with Pool(min(procs, len(names))) as pool:
        return dict(pool.map(run_one, [(n, end, None) for n in names]))


def fmt(x, pct=False, d=2):
    if x is None or (isinstance(x, float) and (math.isnan(x))):
        return "—"
    if isinstance(x, float) and math.isinf(x):
        return "∞"
    return f"{x*100:+.{d-1}f}%" if pct else f"{x:.{d}f}"


def line(name, st):
    return (f"{name:6s} ret={fmt(st['ret'],1)} mdd={st['mdd']*100:5.1f}% sharpe={fmt(st['sharpe'])} calmar={fmt(st['calmar'])} "
            f"n={st['n']:3d} win={st['win']*100:4.1f}% pf={fmt(st['pf'])} paused={st['paused']} reduced={st['reduced']}")


# ------------------------------------------------------------------ 阶段
def phase_repro():
    assert bt.LONG
    setup()
    tl = G["tl"]
    ref = bt.Sim(G["data"], L0()); ref.run(tl)
    sim = DSim(G["data"], L0(), 0.02, G["btc"]); sim.run(tl)
    cr = [v for _, v in ref.curve]; cs = [v for _, v in sim.curve]
    same = cr == cs and [(t.sym, t.open_ts, t.close_ts, t.pnl) for t in ref.trades] == [(t.sym, t.open_ts, t.close_ts, t.pnl) for t in sim.trades]
    out = f"[{now_cst()}] repro long L0@2%: bt.Sim ret={cr[-1]/1000-1:+.4f} mdd={bt.max_dd(cr,1000):.4f} n={len(ref.trades)} | DSim ret={cs[-1]/1000-1:+.4f} mdd={bt.max_dd(cs,1000):.4f} n={len(sim.trades)} | identical={same}"
    print(out)
    (HERE / "repro_output.txt").open("a").write(out + "\n")


def phase_repro_hl():
    assert not bt.LONG
    setup()
    tl = G["tl"]
    ref = bt.Sim(G["data"], L0()); ref.run(tl)
    sim = DSim(G["data"], L0(), 0.02, G["btc"]); sim.run(tl)
    cr = [v for _, v in ref.curve]; cs = [v for _, v in sim.curve]
    same = cr == cs
    out = f"[{now_cst()}] repro HL L0@2%: bt.Sim ret={cr[-1]/1000-1:+.4f} mdd={bt.max_dd(cr,1000):.4f} n={len(ref.trades)} | DSim identical={same}"
    print(out)
    (HERE / "repro_output.txt").open("a").write(out + "\n")


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    pos = q * (len(xs) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def phase_train():
    assert bt.LONG
    setup()
    b = run_many(["B0", "B1"], TRAIN_END)
    b1 = b["B1"]
    entries = [trade_feats(b1, t) for t in b1.trades if TRAIN_START <= t.open_ts < TRAIN_END]
    th, info = {}, {}
    for fam, (key, direction) in FAMS.items():
        vals = [f[key] for f in entries if f.get(key) is not None]
        q = quantile(vals, 2 / 3 if direction == "high" else 1 / 3)
        if fam == "E":
            q = min(q, 0.0)
        th[fam] = q
        info[fam] = {"key": key, "direction": direction, "n": len(vals), "threshold": q}
    (HERE / "thresholds.json").write_text(json.dumps({"thresholds": th, "info": info, "n_entries": len(entries), "time": now_cst()}, ensure_ascii=False, indent=1))
    G["th"] = th
    c = run_many(CANDS, TRAIN_END, procs=8)
    res = {n: seg(s, *SEGS["TRAIN"]) for n, s in {**b, **c}.items()}
    ref = res["B1"]
    lines = [f"[{now_cst()}] TRAIN 阈值 {json.dumps(th)}  (B1 TRAIN 开仓 {len(entries)} 笔)"]
    for n in ["B0", "B1"] + CANDS:
        st = res[n]
        if n in CANDS:
            st["eligible"] = bool(st["sharpe"] >= ref["sharpe"] - 0.05 and st["mdd"] <= ref["mdd"] and st["n"] >= 0.5 * ref["n"])
        lines.append(line(n, st) + (f" eligible={st['eligible']}" if n in CANDS else ""))
    print("\n".join(lines))
    (HERE / "train_output.txt").write_text("\n".join(lines) + "\n")
    (HERE / "train_results.pkl").write_bytes(pickle.dumps({"seg": res, "th": th, "runs": {n: pack(s) for n, s in {**b, **c}.items()}}))


def phase_val():
    assert bt.LONG
    if not (HERE / "D_DEVIATIONS.md").exists():
        sys.exit("拒绝：D_DEVIATIONS.md 尚未写入")
    if (HERE / "chosen.json").exists():
        sys.exit("拒绝：chosen.json 已存在（VAL 已跑过）")
    tr = pickle.loads((HERE / "train_results.pkl").read_bytes())
    th = json.loads((HERE / "thresholds.json").read_text())["thresholds"]
    setup(th)
    runs = run_many(["B0", "B1"] + CANDS, VAL_END, procs=8)
    res = {n: {"TRAIN": seg(s, *SEGS["TRAIN"]), "VAL": seg(s, *SEGS["VAL"])} for n, s in runs.items()}
    for n in CANDS:  # TRAIN 部分应与 train 阶段完全一致（因果模拟）
        a, b = res[n]["TRAIN"], tr["seg"][n]
        if abs(a["ret"] - b["ret"]) > 1e-9 or abs(a["mdd"] - b["mdd"]) > 1e-9:
            print(f"警告：{n} TRAIN 段资金曲线与 train 阶段不一致", a["ret"], b["ret"])
    elig = [n for n in CANDS if tr["seg"][n]["eligible"]]
    pool = elig or CANDS
    best = max(res[n]["VAL"]["sharpe"] for n in pool)
    top = [n for n in pool if res[n]["VAL"]["sharpe"] >= best - 0.01]
    chosen = max(top, key=lambda n: res[n]["VAL"]["calmar"])
    support = bool(res[chosen]["VAL"]["sharpe"] >= res["B1"]["VAL"]["sharpe"])
    lines = [f"[{now_cst()}] VAL（{len(elig)} 个 TRAIN 合格：{elig}）"]
    for n in ["B0", "B1"] + CANDS:
        lines.append(line(n, res[n]["VAL"]) + (" *合格" if n in elig else ""))
    lines.append(f"chosen={chosen} VAL支持={support} 并列候选={top}")
    print("\n".join(lines))
    (HERE / "val_output.txt").write_text("\n".join(lines) + "\n")
    (HERE / "val_results.pkl").write_bytes(pickle.dumps({"seg": res, "runs": {n: pack(s) for n, s in runs.items()}}))
    (HERE / "chosen.json").write_text(json.dumps({"chosen": chosen, "eligible": elig, "val_support": support, "tied": top,
                                                   "rule": "TRAIN 合格中 VAL Sharpe 最高（差≤0.01 取 VAL Calmar 高）", "time": now_cst()}, ensure_ascii=False))


def worker(tag: str):
    """在当前环境变量（数据集/标的）下跑 B0/B1/选中 全程，保存 test_<tag>.pkl（每个 tag 只允许一次）。"""
    if not (HERE / "test_done.flag").exists():
        sys.exit("拒绝：test 尚未启动")
    out = HERE / f"test_{tag}.pkl"
    if out.exists():
        sys.exit(f"拒绝：{out.name} 已存在")
    th = json.loads((HERE / "thresholds.json").read_text())["thresholds"]
    ch = json.loads((HERE / "chosen.json").read_text())["chosen"]
    setup(th)
    runs = run_many(["B0", "B1", ch], END, procs=3)
    out.write_bytes(pickle.dumps({"tag": tag, "chosen": ch, "long": bt.LONG, "syms": bt.SYMS, "test_start": bt.TEST_START,
                                  "runs": {n: pack(s) for n, s in runs.items()},
                                  "seg": {n: {k: seg(s, *v) for k, v in SEGS.items()} | {"PERIODS": {lab: seg(s, a, b) for lab, a, b in bt.PERIODS}}
                                          for n, s in runs.items()}}))
    print(f"[{now_cst()}] worker {tag} done")


def phase_test():
    assert bt.LONG
    flag = HERE / "test_done.flag"
    if flag.exists():
        sys.exit("拒绝：test_done.flag 已存在，TEST 只允许运行一次")
    if not (HERE / "chosen.json").exists():
        sys.exit("拒绝：尚未完成 VAL 选择")
    ch = json.loads((HERE / "chosen.json").read_text())["chosen"]
    flag.write_text(f"TEST started {now_cst()} chosen={ch}\n")
    envs = {
        "long4": {"BT_DATA": "long", "BT_SYMS": "BTC,ETH,SOL,HYPE"},
        "long3": {"BT_DATA": "long", "BT_SYMS": "ETH,SOL,HYPE"},
        "hl4": {"BT_DATA": "", "BT_SYMS": "BTC,ETH,SOL,HYPE"},
        "hl3": {"BT_DATA": "", "BT_SYMS": "ETH,SOL,HYPE"},
    }
    procs = [subprocess.Popen([sys.executable, __file__, "worker", tag], env={**os.environ, **e}) for tag, e in envs.items()]
    codes = [p.wait() for p in procs]
    flag.open("a").write(f"TEST finished {now_cst()} exit={codes}\n")
    print("done", codes)


if __name__ == "__main__":
    cmd = sys.argv[1]
    {"repro": phase_repro, "repro_hl": phase_repro_hl, "train": phase_train, "val": phase_val, "test": phase_test,
     "worker": lambda: worker(sys.argv[2])}[cmd]()
