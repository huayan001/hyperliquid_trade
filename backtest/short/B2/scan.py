"""B2 第 1 步：TRAIN 段特征扫描（预注册见 B2_PLAN.md）。只读 TRAIN 行；阈值只用 TRAIN 计算，写入 thresholds.json。"""
from __future__ import annotations

import csv, json, math
from collections import defaultdict
from pathlib import Path

from feat import (CATS, HALF, HORIZONS, QFEATS, TRAIN_END, TRAIN_START, Sym, bucket, edge_dir, filt, oriented, quantiles, rows)

HERE = Path(__file__).resolve().parent
DATA = HERE.parent.parent / "data_long"
SYMS = ["BTC", "ETH", "SOL", "HYPE"]
FILTERS = ["ALL", "RA", "RB", "RC"]
COST = 0.00015 + 0.00045 + 0.0002
REQ = ["dist", "rsi1", "rsi4", "pctb", "adx4", "adxs", "rv", "vov", "volz", "fundz", "ret4", "ret24", "wat", "dadx"]


def Phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def test(sel: list[dict], ykey: str, fkey: str, signer=None) -> dict:
    n = len(sel)
    if n == 0:
        return {"n": 0, "clusters": 0, "p": 1.0}
    ys = [(signer(r) if signer else 1) * r[ykey] for r in sel]
    fs = [(signer(r) if signer else 1) * r[fkey] for r in sel]  # 该交易方向「做多支付」的资金费
    mean = sum(ys) / n
    d = 1 if mean >= 0 else -1
    net = [d * y - COST - d * f for y, f in zip(ys, fs)]
    mn = sum(net) / n
    cl = defaultdict(float)
    for r, x in zip(sel, net):
        cl[r["week"]] += x - mn
    G = len(cl)
    se = math.sqrt(G / (G - 1) * sum(v * v for v in cl.values())) / n if G > 1 else float("inf")
    t = mn / se if se > 0 else 0.0
    p = 2 * (1 - Phi(t)) if (t > 0 and n >= 30 and G >= 10) else 1.0
    srt = sorted(d * y for y in ys)
    res = {"n": n, "clusters": G, "dir": d, "hit": sum(1 for y in ys if d * y > 0) / n, "hit_net": sum(1 for x in net if x > 0) / n,
           "mean": mean, "median_dir": srt[n // 2], "cost": COST + sum(d * f for f in fs) / n, "mean_net": mn, "se": se, "t": t, "p": min(1.0, p)}
    # 一致性
    for s in ("BTC", "ETH", "SOL"):
        v = [y for r, y in zip(sel, ys) if r["sym"] == s]
        res[f"n_{s}"] = len(v); res[f"m_{s}"] = sum(v) / len(v) if v else float("nan")
    for hname, cond in (("h1", lambda r: r["T"] < HALF), ("h2", lambda r: r["T"] >= HALF)):
        v = [y for r, y in zip(sel, ys) if cond(r)]
        res[f"n_{hname}"] = len(v); res[f"m_{hname}"] = sum(v) / len(v) if v else float("nan")
    ok_sym = all(res[f"n_{s}"] >= 10 and res[f"m_{s}"] * d > 0 for s in ("BTC", "ETH", "SOL"))
    ok_half = all(res[f"n_{h}"] >= 15 and res[f"m_{h}"] * d > 0 for h in ("h1", "h2"))
    res["consistent"] = bool(ok_sym and ok_half and mn > 0)
    return res


def main() -> None:
    data = {s: Sym(DATA, s) for s in SYMS}
    allrows = []
    for s in SYMS:
        for r in rows(data[s], data["BTC"]):
            if not (TRAIN_START <= r["T"] and r["T"] + 24 * 3_600_000 <= TRAIN_END):
                continue
            if any(r[k] is None for k in REQ) or any(r[f"r{h}"] is None for h in HORIZONS):
                continue
            allrows.append(r)
    by_sym = defaultdict(list)
    for r in allrows:
        by_sym[r["sym"]].append(r)
    print("TRAIN rows per symbol:", {s: len(v) for s, v in by_sym.items()}, flush=True)
    # 阈值：每币 TRAIN 五分位（F1），边缘样本上镜像后五分位（F2/F3），镜像 RSI4 的 40% 分位（F3）
    th = {"F1": {}, "F2": {}, "F3_rsi4_40": {}}
    for s, R in by_sym.items():
        th["F1"][s] = {f: quantiles([r[f] for r in R if r[f] is not None]) for f in QFEATS}
        E = [r for r in R if edge_dir(r)]
        th["F2"][s] = {f: quantiles([oriented(r, f) for r in E if oriented(r, f) is not None]) for f in QFEATS}
        o4 = sorted(oriented(r, "rsi4") for r in E)
        th["F3_rsi4_40"][s] = o4[int(len(o4) * 0.4)]
    (HERE / "thresholds.json").write_text(json.dumps(th, indent=1))

    def fbuckets(r, fam):
        out = {}
        for f in QFEATS:
            v = r[f] if fam == "F1" else oriented(r, f)
            ths = th[fam][r["sym"]].get(f)
            out[f] = None if (v is None or not ths) else bucket(v, ths)
        for c in CATS:
            out[c] = r[c]
        return out

    for r in allrows:
        r["b1"] = fbuckets(r, "F1")
        r["b1"]["pos"] = min(9, int(r["pos"] * 10))
        if edge_dir(r):
            r["b2"] = fbuckets(r, "F2")
            r["stretch"] = oriented(r, "rsi4") <= th["F3_rsi4_40"][r["sym"]]
    feats_q = QFEATS
    levels = {f: list(range(5)) for f in feats_q}
    levels.update({c: v for c, v in CATS.items()})
    out = []
    rev = lambda r: 1 if r["pos"] < 0.5 else -1 if r["pos"] > 0.5 else 0
    for flt in FILTERS:
        base = [r for r in allrows if filt(r, flt)]
        # F1
        for f, lv in [("pos", list(range(10)))] + [(f, levels[f]) for f in feats_q + list(CATS)]:
            for b in lv:
                sel = [r for r in base if r["b1"].get(f) == b]
                for h in HORIZONS:
                    for tgt, sg in (("r", None), ("s", rev)):
                        out.append({"family": "F1", "filter": flt, "feature": f, "bucket": b, "target": tgt, "h": h, **test(sel, f"r{h}", f"f{h}", sg)})
        edge = [r for r in base if edge_dir(r)]
        for fam, pool in (("F2", edge), ("F3", [r for r in edge if r["stretch"]])):
            for f in feats_q + list(CATS):
                if fam == "F3" and f == "rsi4":
                    continue
                for b in levels[f]:
                    sel = [r for r in pool if r["b2"].get(f) == b]
                    for h in HORIZONS:
                        out.append({"family": fam, "filter": flt, "feature": f, "bucket": b, "target": "s", "h": h, **test(sel, f"r{h}", f"f{h}", edge_dir)})
    m = len(out)
    order = sorted(range(m), key=lambda i: out[i]["p"])
    q = [1.0] * m; prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, out[i]["p"] * m / rank); q[i] = prev
    for i, o in enumerate(out):
        o["bh_q"] = q[i]; o["fdr"] = q[i] <= 0.10; o["survivor"] = o["fdr"] and o.get("consistent", False)
    keys = ["family", "filter", "feature", "bucket", "target", "h", "n", "clusters", "dir", "hit", "hit_net", "mean", "median_dir", "cost", "mean_net", "se", "t", "p", "bh_q", "fdr",
            "n_BTC", "m_BTC", "n_ETH", "m_ETH", "n_SOL", "m_SOL", "n_h1", "m_h1", "n_h2", "m_h2", "consistent", "survivor"]
    with (HERE / "scan_results.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore"); w.writeheader()
        for o in sorted(out, key=lambda o: o["p"]):
            w.writerow(o)
    fam_n = defaultdict(int)
    for o in out:
        fam_n[o["family"]] += 1
    elig = sum(1 for o in out if o["n"] >= 30 and o.get("clusters", 0) >= 10)
    print(f"hypotheses tested m={m} by family={dict(fam_n)}; eligible (n>=30, clusters>=10)={elig}")
    print(f"raw p<0.05: {sum(o['p'] < 0.05 for o in out)}; BH-FDR10% pass: {sum(o['fdr'] for o in out)}; +consistency (survivors): {sum(o['survivor'] for o in out)}")
    print(f"consistent (any p): {sum(o.get('consistent', False) for o in out)}")
    print("\nTop 25 by p:")
    for o in sorted(out, key=lambda o: o["p"])[:25]:
        print(f"{o['family']} {o['filter']:3s} {o['feature']:6s} b={o['bucket']!s:2s} {o['target']} h={o['h']:2d} n={o['n']:5d} G={o.get('clusters',0):3d} dir={o.get('dir',0):+d} "
              f"hit={o.get('hit',0)*100:4.1f}% mean={o.get('mean',0)*1e4:+6.1f}bp net={o.get('mean_net',0)*1e4:+6.1f}bp t={o.get('t',0):5.2f} p={o['p']:.2e} q={o['bh_q']:.3f} "
              f"cons={o.get('consistent')} BTC/ETH/SOL={o.get('m_BTC',0)*1e4:+.0f}/{o.get('m_ETH',0)*1e4:+.0f}/{o.get('m_SOL',0)*1e4:+.0f} halves={o.get('m_h1',0)*1e4:+.0f}/{o.get('m_h2',0)*1e4:+.0f}")


if __name__ == "__main__":
    main()
