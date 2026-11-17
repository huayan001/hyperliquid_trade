"""B2 策略（规则由 scan.py 通过 FDR+一致性的条件机械组装，见 B2_PLAN.md / B2_DEVIATIONS.md）。
python b2.py val     → 4 个变体只在 VALIDATION 上运行，按预注册规则选 1 个 → chosen.json
python b2.py test    → 选中变体在 TEST 上只跑一次（写 test_done.flag，重复运行会拒绝），另跑 HL 6 个月、TRAIN（衰减对比）、全程连续（分阶段/相关/合并）
python b2.py report  → report_B2_tables.md + equity_B2.png
"""
from __future__ import annotations

import json, pickle, statistics, sys, datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # short.py
import short as S  # noqa: E402
from feat import TRAIN_END, TRAIN_START, VAL_END, Sym, bucket, edge_dir, filt, oriented, rows  # noqa: E402

H = S.H
TH = json.loads((HERE / "thresholds.json").read_text())
SYMS = ["BTC", "ETH", "SOL", "HYPE"]
# HYPE 无 TRAIN 数据：阈值取 BTC/ETH/SOL 的中位数（B2_DEVIATIONS.md 第 2 条）
for fam in ("F1", "F2"):
    TH[fam]["HYPE"] = {f: [statistics.median(TH[fam][s][f][k] for s in ("BTC", "ETH", "SOL")) for k in range(4)]
                       for f in TH[fam]["BTC"] if TH[fam]["BTC"][f]}
TH["F3_rsi4_40"]["HYPE"] = statistics.median(TH["F3_rsi4_40"][s] for s in ("BTC", "ETH", "SOL"))
DIRS = {"long": HERE.parent.parent / "data_long", "hl": HERE.parent.parent / "data"}
SEGS = {"train": (TRAIN_START, TRAIN_END), "val": (TRAIN_END, VAL_END), "test": (VAL_END, 1_900_000_000_000),
        "full": (TRAIN_START, 1_900_000_000_000), "hl": (1773964800000, 1_900_000_000_000)}
HOLD = 24


def conds(r: dict) -> dict:
    s = r["sym"]
    if r["dadx"] is None or not filt(r, "RB") or not edge_dir(r):
        return {}
    f2 = TH["F2"][s]
    out = {}
    if r["adxs"] is not None:
        out["adxs0"] = bucket(r["adxs"], f2["adxs"]) == 0
    if r["dist"] is not None and r["rsi4"] is not None:
        out["stretch_dist0"] = oriented(r, "rsi4") <= TH["F3_rsi4_40"][s] and bucket(r["dist"], f2["dist"]) == 0
    if r["vov"] is not None:
        out["vov3"] = bucket(r["vov"], f2["vov"]) == 3
    return out


RULES = {"RS1": lambda c: c.get("adxs0", False), "RS2": lambda c: any(c.values())}
VARIANTS = {f"{rs}-{e}": (rs, e) for rs in ("RS1", "RS2") for e in ("E1", "E3")}


def signals(data_dir: Path, rs: str) -> dict:
    fs = {s: Sym(data_dir, s) for s in SYMS}
    sig = {}
    for s in SYMS:
        for r in rows(fs[s], fs["BTC"], with_forward=False):
            c = conds(r)
            if c and RULES[rs](c):
                sig[(s, r["T"])] = (-edge_dir(r), r["C"], r["atr4"], r["atr1"], [k for k, v in c.items() if v])
    return sig


class B2Sim(S.Sim):
    def __init__(self, variant: str, data, sig: dict, seg: tuple[int, int]) -> None:
        super().__init__("B2", data, seg[0])
        self.rs, self.e = VARIANTS[variant]
        self.sig, self.seg = sig, seg

    def _close(self, s, px, ts, i, reason, maker):
        super()._close(s, px, ts, i, reason, maker)
        self.cool.clear()  # B2 不设冷却

    def _manage(self, s, d, i, p):
        if i - p.open_i + 1 >= HOLD:
            p.exit_next = "time"

    def _maintain(self, s, d, i):
        pass

    def _signal(self, d, i):
        T = d.h1[i].ts + H
        if not (self.seg[0] <= T < self.seg[1]):
            return None
        x = self.sig.get((d.sym, T))
        if x is None:
            return None
        side, C, a4, a1, tags = x
        lim = C if self.e == "E1" else C - side * 0.25 * (a1 or 0)
        return S.Order(side, lim, lim - side * 2.0 * a4, None, i + 1, i + 4, 0.005)


def run_variant(variant: str, ds: str, seg_key: str, cache: dict) -> dict:
    rs, _ = VARIANTS[variant]
    key = (ds, rs)
    if key not in cache:
        cache[key] = (signals(DIRS[ds], rs), {s: S.SD(DIRS[ds], s) for s in SYMS})
    sig, data = cache[key]
    seg = SEGS[seg_key]
    tl = sorted(set().union(*[set(x.idx) for x in data.values()]))
    sim = B2Sim(variant, data, sig, seg)
    sim.run(tl)
    lo = max(seg[0], min(t for t, _ in sim.curve)) if sim.curve else seg[0]
    last_exit = max([t.close_ts for t in sim.trades], default=lo)
    curve = [(t, v) for t, v in sim.curve if t <= max(min(seg[1], sim.curve[-1][0]), last_exit)]
    cv = [v for _, v in curve]
    st = S.stats(sim.trades)
    wins = [t.pnl for t in sim.trades if t.pnl > 0]; losses = [t.pnl for t in sim.trades if t.pnl <= 0]
    r = {"variant": variant, "ds": ds, "seg": seg_key, "all": st, "ret": cv[-1] / S.START_EQ - 1 if cv else 0.0,
         "mdd": S.max_dd(cv, S.START_EQ) if cv else 0.0, "curve": curve,
         "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0,
         "avg_win_r": sum(t.pnl / t.risk for t in sim.trades if t.pnl > 0) / len(wins) if wins else 0.0,
         "avg_loss_r": sum(t.pnl / t.risk for t in sim.trades if t.pnl <= 0) / len(losses) if losses else 0.0,
         "per": {s: S.stats([t for t in sim.trades if t.sym == s]) for s in SYMS},
         "exits": {}, "n_signals": sum(1 for (s, T) in sig if seg[0] <= T < seg[1]),
         "sides": {k: S.stats([t for t in sim.trades if t.side == v]) for k, v in (("long", 1), ("short", -1))},
         "trades": [(t.sym, t.side, t.open_ts, t.close_ts, t.pnl, t.fees, t.fund, t.risk, t.reason) for t in sim.trades],
         "exposure": sim.bars_any / max(1, sim.bars_total)}
    gross = sum(t.pnl + t.fees + t.fund for t in sim.trades)
    r["gross_before_costs"] = gross
    for t in sim.trades:
        r["exits"][t.reason] = r["exits"].get(t.reason, 0) + 1
    r["periods"] = {}
    for lab, a, b in S.PERIODS:
        seg_c = [(t, v) for t, v in curve if a < t <= b]
        if len(seg_c) < 24:
            continue
        prev = [v for t, v in curve if t <= a]; base = prev[-1] if prev else S.START_EQ
        ps = S.stats([t for t in sim.trades if a <= t.open_ts < b])
        ps["ret"] = seg_c[-1][1] / base - 1; ps["mdd"] = S.max_dd([v for _, v in seg_c], base)
        r["periods"][lab] = ps
    r["monthly"] = S.monthly(curve)
    return r


def fmt(r: dict) -> str:
    a = r["all"]
    return (f"{r['variant']:7s} [{r['ds']}/{r['seg']}] signals={r['n_signals']} trades={a['trades']} ret={r['ret']*100:+.2f}% mdd={r['mdd']*100:.1f}% "
            f"pf={a['pf']:.2f} win={a['win']*100:.0f}% avgR={a['avg_r']:+.3f} avgWin=${r['avg_win']:.2f} avgLoss=${r['avg_loss']:.2f} "
            f"fees=${a['fees']:.1f} ({a['fee_ratio']*100:.0f}% of gross) fund=${a['funding']:+.2f} exits={r['exits']} per-sym pnl={ {s: round(v['pnl'], 1) for s, v in r['per'].items()} }")


def main(cmd: str) -> None:
    cache: dict = {}
    if cmd == "val":
        res = {v: run_variant(v, "long", "val", cache) for v in VARIANTS}
        for v, r in res.items():
            print(fmt(r), flush=True)
        ok = [v for v, r in res.items() if r["all"]["trades"] >= 30 and r["all"]["fee_ratio"] < 0.30]
        if ok:
            chosen = max(ok, key=lambda v: res[v]["all"]["pf"]); why = "trades>=30 且 费/毛利<30% 中 PF 最高"
        else:
            chosen = max(res, key=lambda v: res[v]["all"]["trades"]); why = "没有变体满足 trades>=30 → 取交易数最多者"
        (HERE / "chosen.json").write_text(json.dumps({"chosen": chosen, "rule": why, "time_cst": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()}, ensure_ascii=False))
        (HERE / "val_results.pkl").write_bytes(pickle.dumps(res))
        print(f"CHOSEN = {chosen} ({why})")
    elif cmd == "test":
        flag = HERE / "test_done.flag"
        if flag.exists():
            raise SystemExit(f"TEST 已运行过（{flag.read_text().strip()}），按预注册不再重跑")
        chosen = json.loads((HERE / "chosen.json").read_text())["chosen"]
        flag.write_text(f"{chosen} {dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()}")
        out = {"chosen": chosen}
        for seg in ("test", "train", "full"):
            out[seg] = run_variant(chosen, "long", seg, cache); print(fmt(out[seg]), flush=True)
        out["val"] = pickle.loads((HERE / "val_results.pkl").read_bytes())[chosen]
        out["hl"] = run_variant(chosen, "hl", "hl", cache); print(fmt(out["hl"]), flush=True)
        out["event_decay"] = event_decay(VARIANTS[chosen][0])
        for k, v in out["event_decay"].items():
            print("event-level", k, v)
        l0 = pickle.loads((HERE.parent.parent / "results_long.pkl").read_bytes())["L0 实盘趋势"]["curve"]
        out["L0_curve"] = l0
        out["corr_l0_full"] = S.corr(out["full"]["monthly"], S.monthly(l0))
        print("monthly corr with L0 (full period):", round(out["corr_l0_full"], 3))
        (HERE / "results_b2.pkl").write_bytes(pickle.dumps(out))
    elif cmd == "report":
        import b2_report  # noqa: F401


def event_decay(rs: str) -> dict:
    """规则触发时刻（4h 收盘）到 +24h 的方向收益（未含撮合），按 TRAIN / VAL / TEST 分段：原始统计优势的衰减。"""
    fs = {s: Sym(DIRS["long"], s) for s in SYMS}
    seg = {"train": [], "val": [], "test": []}
    for s in SYMS:
        for r in rows(fs[s], fs["BTC"], with_forward=True):
            c = conds(r)
            if not (c and RULES[rs](c)) or r.get("r24") is None:
                continue
            d = -edge_dir(r)
            net = d * r["r24"] - 0.0008 - d * r["f24"]
            k = "train" if r["T"] < TRAIN_END else "val" if r["T"] < VAL_END else "test"
            seg[k].append((d * r["r24"], net))
    out = {}
    for k, v in seg.items():
        if v:
            g = [x for x, _ in v]; n = [y for _, y in v]
            out[k] = {"n": len(v), "mean_gross_bp": round(sum(g) / len(g) * 1e4, 1), "mean_net_bp": round(sum(n) / len(n) * 1e4, 1),
                      "hit_net": round(sum(1 for y in n if y > 0) / len(n), 3)}
    return out


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "val")
