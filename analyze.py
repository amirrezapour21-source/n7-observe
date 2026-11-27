"""N7 analysis (market-data snapshots only; no trade outcomes).
python analyze.py gate    -> prints RUN or STOP <reason> (used by the observe workflow; run counts only)
python analyze.py check   -> completeness table only (no cost statistics)
python analyze.py report  -> full report (run once after the final period); also written to results/N7_report.txt"""
import glob, json, math, os, sys, time

H4, DAY, FEE = 4 * 3600e3, 86400e3, 12.0; INF = float("inf"); SZ = (500, 1000, 5000); MAJ = ("XBTUSDTM", "ETHUSDTM"); TR = ("C11", "U67S", "U67L", "LONGM1")
CLS = ("CONSERVATIVE", "ADEQUATE", "MARGINAL", "INADEQUATE")

def load():
    runs = []
    for f in sorted(glob.glob("data/2*.jsonl")):
        buf = []
        for ln in open(f):
            try: j = json.loads(ln)
            except ValueError: continue
            if j.get("k") == "m": runs.append((j, buf)); buf = []
            else: buf.append(j)
    return runs

def t_zero(runs):
    f = [m["t0"] for m, _ in runs if m["mode"] == "full"]
    if not f: return None
    b = min(f) - min(f) % H4
    return b if (min(f) - b) / 6e4 <= 45 else b + H4

def valid_runs(runs, T0, days):
    """valid full runs: inside [T0, T0+days), 3-45 min after a 4h boundary, >=80 ok symbols; first one per (boundary, nearest slot 5/15/35)."""
    seen, out = set(), []
    for m, sn in sorted((r for r in runs if r[0]["mode"] == "full"), key=lambda r: r[0]["t0"]):
        if not (T0 <= m["t0"] < T0 + days * DAY) or m["ok"] < 80: continue
        b = m["t0"] - m["t0"] % H4; mi = (m["t0"] - b) / 6e4
        if not 3 <= mi <= 45: continue
        key = (b, min((5, 15, 35), key=lambda s: abs(mi - s)))
        if key not in seen: seen.add(key); out.append((m, sn, b, key[1]))
    return out

def status(runs, now=None):
    """-> (state, text, plan days) with state in RUN / STOP; decided on run counts only."""
    T0 = t_zero(runs)
    if T0 is None: return "RUN", "no data yet", 21
    now = (time.time() * 1000) if now is None else now
    v21 = len(valid_runs(runs, T0, 21)); days = 21 if v21 >= 0.8 * 378 or now < T0 + 21 * DAY else 28
    full = [m for m, _ in runs if m["mode"] == "full" and m["t0"] >= T0]; bad = 0
    for d in range(days):                                     # 3 consecutive complete days with >20% degraded executed full runs -> terminate
        lo = T0 + d * DAY
        if lo + DAY > now: break
        ex = [m for m in full if lo <= m["t0"] < lo + DAY]; deg = sum(1 for m in ex if m["ok"] < 80)
        bad = bad + 1 if ex and deg > 0.2 * len(ex) else 0
        if bad >= 3: return "STOP", f"TERMINATED (3 consecutive degraded days ending day {d + 1}) - report INCOMPLETE", days
    if now >= T0 + days * DAY: return "STOP", f"COMPLETE planned {days} days (valid runs by day 21: {v21}/378)", days
    return "RUN", f"day plan {days}, valid runs by day 21 so far {v21}/378", days

def pct(a, q):
    if not a: return float("nan")
    a = sorted(a); return a[max(math.ceil(q * len(a)) - 1, 0)]

def wpct(vals, w, q):
    p = sorted(zip(vals, w)); tot = sum(w); c = 0.0
    for v, x in p:
        c += x
        if c >= q * tot - 1e-12: return v
    return INF

def classify(a):
    """a = fee-inclusive costs (INF = unfillable). Evaluated in order, exactly one class."""
    n = len(a); u = sum(1 for x in a if x == INF) / n; med, p90 = pct(a, .5), pct(a, .9)
    if u >= .10 or med > 30: return "INADEQUATE"
    if p90 > 30: return "MARGINAL"
    return "CONSERVATIVE" if med <= 20 else "ADEQUATE"

def cost(sn, i):
    b, x = sn["b"], sn["x"]
    return INF if i >= len(b) or b[i] is None or x[i] is None else b[i] + x[i]

def main(cmd):
    runs = load(); T0 = t_zero(runs)
    if cmd == "gate": s, t, _ = status(runs); print(s, t); return
    if T0 is None: print("no data"); return
    st, why, days = status(runs)
    V = valid_runs(runs, T0, days); P = 18 * days; L = []; o = L.append
    o(f"N7 EXECUTION-COST OBSERVATION - {'COMPLETENESS CHECK' if cmd == 'check' else 'REPORT'}  (T0 {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(T0 / 1000))}, plan {days} days)")
    o(f"status: {st} - {why}")
    rate = len(V) / P; o(f"A. COMPLETENESS: planned full runs {P}; valid runs {len(V)}; success rate {rate:.1%} -> {'OK (>=80%)' if rate >= .8 else 'INCOMPLETE (<80%)'}")
    allfull = [m for m, _ in runs if m["mode"] == "full" and T0 <= m["t0"] < T0 + days * DAY]
    o(f"   executed full runs {len(allfull)}; degraded (<80 ok) {sum(1 for m in allfull if m['ok'] < 80)}; light runs {sum(1 for m, _ in runs if m['mode'] == 'light' and T0 <= m['t0'] < T0 + days * DAY)}; retries {sum(m['retries'] for m, _ in runs)}; HTTP 429 {sum(m['r429'] for m, _ in runs)}")
    o("   valid runs per day: " + " ".join(str(sum(1 for m, *_ in V if T0 + d * DAY <= m['t0'] < T0 + (d + 1) * DAY)) for d in range(days)))
    if cmd == "check" or not V:
        if not V: o("no valid runs - report INCOMPLETE, no cost statistics")
        print("\n".join(L)); return
    W = json.load(open("weights.json")); syms = sorted(W); mn = math.ceil(.66 * P)
    data = {s: [] for s in syms}                                 # per symbol: (snapshot, run index)
    for k, (m, sn, b, slot) in enumerate(V):
        for x in sn: data[x["s"]].append((x, k, b, slot, m))
    info, back = {}, 0
    for s in syms:
        ok = [r for r in data[s] if r[0]["st"] == "ok"]; ninv = sum(1 for r in data[s] if r[0]["st"] == "inv")
        ratio = sorted(r[0]["tb"] / r[0]["db"] for r in ok if r[0].get("tb") and r[0].get("db"))
        info[s] = dict(ok=ok, n=len(ok), ninv=ninv, nfail=len(data[s]) - len(ok) - ninv, ratio=ratio[len(ratio) // 2] if ratio else None)
        prev = None
        for r in sorted(ok, key=lambda r: r[0]["t"]):
            if prev is not None and r[0].get("dts") is not None and r[0]["dts"] < prev: back += 1
            prev = r[0].get("dts", prev)
    for s in syms:
        i = info[s]
        i["cls"] = {}
        bad = "INVALID" if (i["ninv"] > len(data[s]) / 2 and len(data[s]) > 0) or (i["ratio"] is not None and not .2 <= i["ratio"] <= 5) else None
        i["flag"] = bad or ("INSUFFICIENT" if i["n"] < mn else None)
        for z, n in enumerate(SZ):
            if i["flag"]: i["cls"][n] = i["flag"]; continue
            a = [cost(r[0], z) + FEE for r in i["ok"]]; i["cls"][n] = classify(a)
    o(f"   per-symbol minimum {mn} valid snapshots; below it: " + (", ".join(s for s in syms if info[s]["flag"] == "INSUFFICIENT") or "none") + "; invalid/unit-error: " + (", ".join(s for s in syms if info[s]["flag"] == "INVALID") or "none"))
    stale = sum(1 for s in syms for r in info[s]["ok"] if r[0].get("dage", 0) > 5000); tot = sum(info[s]["n"] for s in syms)
    o(f"   valid snapshots {tot}; stale (depth age >5 s) {stale} ({stale / max(tot, 1):.1%}); depth ts moving backwards (diagnostic) {back}")
    o("\nB. PER SYMBOL (fee-inclusive illustrative = gross + 12 bps; all valid snapshots; U = unfillable share; coverage = valid/planned)")
    o("symbol            cov   spr_med spr_p90 | " + " | ".join(f"{n}: med p90 U class" for n in SZ))
    for s in syms:
        i = info[s]; row = f"{s:17s} {i['n'] / P:4.0%} "
        if i["n"]: sp = [r[0]["spr"] for r in i["ok"]]; row += f"{pct(sp, .5):7.1f} {pct(sp, .9):7.1f} | "
        else: row += "    -       - | "
        for z, n in enumerate(SZ):
            if i["n"]: a = [cost(r[0], z) + FEE for r in i["ok"]]; u = sum(1 for x in a if x == INF) / len(a); row += f"{pct(a, .5):6.1f} {pct(a, .9):6.1f} {u:4.0%} {i['cls'][n]} | "
            else: row += f"{i['cls'][n]} | "
        o(row)
    for s in MAJ:
        if info[s]["n"]:
            a = [cost(r[0], 3) for r in info[s]["ok"]]; u = sum(1 for x in a if x == INF) / len(a); o(f"   {s} 20,000 USDT gross round trip: median {pct(a, .5):.1f}, p90 {pct(a, .9):.1f}, unfillable {u:.1%}")
    o("\nC. GROSS (no fees) median per size, equal-weighted symbols: " + "; ".join(f"{n}: {pct([pct([cost(r[0], z) for r in info[s]['ok']], .5) for s in syms if info[s]['n']], .5):.1f}" for z, n in enumerate(SZ)))
    o("   fee-inclusive illustrative cost = gross + 2 x 6 bps (public taker base rate; owner tier unknown; maker not modelled)")
    o("\nD. CLASSES AND TRACK CONCLUSIONS (sizes 1,000 and 5,000 USDT; weights = signal counts per symbol; LONGM1 uses C11's weights; unclassified weight treated as INADEQUATE (conservative), favourable view = ADEQUATE)")
    for n in SZ:
        c = {k: sum(1 for s in syms if info[s]["cls"][n] == k) for k in CLS + ("INSUFFICIENT", "INVALID")}; o(f"   {n}: symbols by class (equal weight) {c}")
    for n in (1000, 5000):
        for t in TR:
            w = {s: W[s]["C11" if t == "LONGM1" else t] for s in syms}; tw = sum(w.values())
            if not tw: o(f"   {n} {t}: no weight"); continue
            sh = {k: sum(w[s] for s in syms if info[s]["cls"][n] == k) / tw for k in CLS + ("INSUFFICIENT", "INVALID")}
            ok_c = sh["CONSERVATIVE"] + sh["ADEQUATE"]; inad = sh["INADEQUATE"] + sh["INSUFFICIENT"] + sh["INVALID"]; fav = ok_c + sh["INSUFFICIENT"] + sh["INVALID"]
            vals = [INF if info[s]["flag"] else pct([cost(r[0], SZ.index(n)) + FEE for r in info[s]["ok"]], .5) for s in syms]; ws = [w[s] for s in syms]
            concl = "30 bps REASONABLE" if ok_c >= .8 else "INADEQUATE" if inad >= .3 else "MIXED"
            o(f"   {n} {t}: " + " ".join(f"{k[:4]} {v:.0%}" for k, v in sh.items()) + f" | conclusion {concl} (favourable view: {'REASONABLE' if fav >= .8 else 'not reasonable'}) | weighted median/p75/p90 {wpct(vals, ws, .5):.1f}/{wpct(vals, ws, .75):.1f}/{wpct(vals, ws, .9):.1f}; weight above 30 bps {sum(x for v, x in zip(vals, ws) if v > 30) / tw:.0%}")
    o("   sensitivity without stale rows (equal-weight symbols whose class changes at 1,000 / 5,000): " + " / ".join(str(sum(1 for s in syms if not info[s]["flag"] and (lambda a: bool(a) and classify(a) != info[s]["cls"][n])([cost(r[0], SZ.index(n)) + FEE for r in info[s]["ok"] if r[0].get("dage", 0) <= 5000]))) for n in (1000, 5000)))
    o("\nE. TEMPORAL VARIABILITY (cross-symbol median fee-inclusive cost at 1,000 USDT per valid run)")
    per = []
    for k, (m, sn, b, slot) in enumerate(V):
        a = [cost(x, 1) + FEE for x in sn if x["st"] == "ok"]
        if a: per.append((pct(a, .5), b, slot, m["t0"]))
    for name, key in (("4h slot (UTC hour)", lambda r: time.gmtime(r[1] / 1000).tm_hour), ("minute after boundary", lambda r: r[2]), ("day", lambda r: int((r[3] - T0) // DAY) + 1)):
        g = {}
        for r in per: g.setdefault(key(r), []).append(r[0])
        o(f"   by {name}: " + "; ".join(f"{k} med {pct(v, .5):.1f} [{min(v):.1f}-{max(v):.1f}]" for k, v in sorted(g.items())))
    allc = [cost(x, 1) + FEE for s in syms for x in (r[0] for r in info[s]["ok"])]
    if allc: o(f"   pooled snapshots at 1,000 USDT: median {pct(allc, .5):.1f}, p90 {pct(allc, .9):.1f}, worst decile starts at {pct(allc, .9):.1f}, share >30 bps {sum(1 for x in allc if x > 30) / len(allc):.0%}")
    lg = {}
    for m, sn in runs:
        if m["mode"] == "light" and T0 <= m["t0"] < T0 + days * DAY:
            for x in sn:
                if x["st"] == "ok": lg.setdefault(time.gmtime(m["t0"] / 1000).tm_hour, []).append(x["spr"])
    o("   quoted spread by UTC hour (light runs, all symbols pooled) median/p90 bps: " + "; ".join(f"{h:02d}h {pct(v, .5):.1f}/{pct(v, .9):.1f}" for h, v in sorted(lg.items())))
    o("\nF. LIMITATIONS: snapshots are not fills (no queue, latency adverse selection or stop slippage); calm-period sample; depth20 only; order size unspecified (hence three sizes); fee tier unknown; KuCoin only; no extrapolation to fills or stress. Classification is descriptive and changes no registered rule, parameter, universe or state.")
    txt = "\n".join(L); print(txt); os.makedirs("results", exist_ok=True); open("results/N7_report.txt", "w").write(txt + "\n")

if __name__ == "__main__": main(sys.argv[1] if len(sys.argv) > 1 else "report")
