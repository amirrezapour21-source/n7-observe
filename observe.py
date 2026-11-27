"""N7 execution-cost observation (public KuCoin Futures market data only; no credentials, no trading).
usage: python observe.py full|light  -> appends JSON lines to data/YYYY-MM-DD.jsonl (UTC date of the run start)."""
import json, os, sys, time
from collections import deque
import requests

B = "https://api-futures.kucoin.com"; S = requests.Session(); ST = dict(retries=0, r429=0); WIN = deque()
SIZES = (500, 1000, 5000); MAJ = ("XBTUSDTM", "ETHUSDTM"); BIG = 20000; LIMIT = 1500  # weight per 30 s (public pool is 2000)

def now_ms(): return time.time() * 1000

def get(path, params=None, weight=5):
    """-> (data, receive-time midpoint ms) or (None, None) after 3 retries; 429 waits for the reset header."""
    for k in range(4):
        while WIN and now_ms() - WIN[0][0] > 30000: WIN.popleft()
        if sum(w for _, w in WIN) + weight > LIMIT: time.sleep(5)
        t0 = now_ms(); WIN.append((t0, weight)); wait = 2 ** k
        try:
            r = S.get(B + path, params=params, timeout=20); t1 = now_ms()
            if r.status_code == 429:
                ST["r429"] += 1; ST["retries"] += 1
                try: wait = float(r.headers.get("gw-ratelimit-reset", 2000)) / 1000
                except ValueError: wait = 2.0
            elif r.status_code == 200:
                p = r.json()
                if p.get("code") == "200000" and p.get("data") is not None: time.sleep(0.05); return p["data"], (t0 + t1) / 2
                ST["retries"] += 1
            else: ST["retries"] += 1
        except (requests.RequestException, ValueError): ST["retries"] += 1
        if k < 3: time.sleep(min(max(wait, 1.0), 10.0))
    return None, None

def walk(levels, target):
    """levels [(price, base_qty)] best first; buys use asks, sells use bids. -> VWAP of `target` USDT (quantity-weighted) or None if depth20 cannot fill it."""
    rem, notional, qty = float(target), 0.0, 0.0
    for p, q in levels:
        t = min(p * q, rem); notional += t; qty += t / p; rem -= t
        if rem <= 1e-9: return notional / qty
    return None

def book(d, mult):
    """depth20 data -> (bids, asks) as [(price, base_qty)] best first, or None if the book is invalid (empty side, non-positive, crossed)."""
    try:
        bids = sorted(((float(p), float(q) * mult) for p, q in d["bids"]), reverse=True); asks = sorted((float(p), float(q) * mult) for p, q in d["asks"])
    except (TypeError, ValueError, KeyError): return None
    if not bids or not asks or min(p for p, q in bids + asks) <= 0 or min(q for p, q in bids + asks) <= 0 or bids[0][0] >= asks[0][0]: return None
    return bids, asks

def snapshot(sym, mult, d, trecv, tk):
    """one record for a symbol from a depth20 payload and its allTickers row (tk may be None)."""
    bk = book(d, mult)
    if bk is None: return dict(k="f", t=round(trecv), s=sym, st="inv")
    bids, asks = bk; mid = (bids[0][0] + asks[0][0]) / 2; sz = SIZES + ((BIG,) if sym in MAJ else ())
    buy = [walk(asks, n) for n in sz]; sell = [walk(bids, n) for n in sz]
    r = dict(k="f", t=round(trecv), s=sym, st="ok", mid=mid, spr=round((asks[0][0] - bids[0][0]) / mid * 1e4, 3),
             cb=round(sum(p * q for p, q in bids), 1), ca=round(sum(p * q for p, q in asks), 1), db=round(bids[0][0] * bids[0][1], 1),
             b=[None if v is None else round((v / mid - 1) * 1e4, 3) for v in buy], x=[None if v is None else round((1 - v / mid) * 1e4, 3) for v in sell])
    try: r["dage"] = round(trecv - float(d["ts"]) / 1e6); r["dseq"] = int(d["sequence"]); r["dts"] = int(d["ts"])
    except (TypeError, ValueError, KeyError): pass
    if tk:
        try: r["tb"] = round(float(tk["bestBidSize"]) * mult * float(tk["bestBidPrice"]), 1); r["tage"] = round(trecv - float(tk["ts"]) / 1e6)
        except (TypeError, ValueError, KeyError): pass
    return r

def light(sym, tk, trecv):
    try:
        b, a = float(tk["bestBidPrice"]), float(tk["bestAskPrice"])
        if not (0 < b < a): return dict(k="l", t=round(trecv), s=sym, st="inv")
        return dict(k="l", t=round(trecv), s=sym, st="ok", spr=round((a - b) / ((a + b) / 2) * 1e4, 3), tage=round(trecv - float(tk["ts"]) / 1e6))
    except (TypeError, ValueError, KeyError): return dict(k="l", t=round(trecv), s=sym, st="inv")

def contracts(syms, today):
    """contracts.json = {date, c:{sym:{m,inv,tick,tf,mf}}}, refreshed once per UTC day (kept from yesterday for symbols whose detail call fails)."""
    old = {}
    try: old = json.load(open("data/contracts.json"))
    except (OSError, ValueError): pass
    if old.get("date") == today: return old["c"]
    c = dict(old.get("c", {}))
    for s in syms:
        d, _ = get(f"/api/v1/contracts/{s}", weight=2)
        try: c[s] = dict(m=float(d["multiplier"]), inv=bool(d.get("isInverse")), tick=d.get("tickSize"), tf=d.get("takerFeeRate"), mf=d.get("makerFeeRate"))
        except (TypeError, ValueError, KeyError): pass
    json.dump(dict(date=today, c=c), open("data/contracts.json", "w"), separators=(",", ":")); return c

def main(mode):
    syms = open("symbols.txt").read().split(); t0 = now_ms(); lines = []; day = time.strftime("%Y-%m-%d", time.gmtime(t0 / 1000)); os.makedirs("data", exist_ok=True)
    tick, tt = get("/api/v1/allTickers"); T = {x.get("symbol"): x for x in tick} if isinstance(tick, list) else {}
    if mode == "light":
        lines = [light(s, T[s], tt) if s in T else dict(k="l", t=round(tt or t0), s=s, st="miss") for s in syms] if tt else []
    else:
        C = contracts(syms, day)
        for s in syms:
            c = C.get(s)
            if not c or not (c["m"] > 0) or c["inv"]: lines.append(dict(k="f", t=round(now_ms()), s=s, st="inv" if c else "fail")); continue
            d, tr = get("/api/v1/level2/depth20", {"symbol": s})
            lines.append(snapshot(s, c["m"], d, tr, T.get(s)) if d else dict(k="f", t=round(now_ms()), s=s, st="fail"))
    n = {k: sum(1 for x in lines if x["st"] == k) for k in ("ok", "inv", "fail", "miss")}
    lines.append(dict(k="m", mode=mode, t0=round(t0), t1=round(now_ms()), ticker_ok=bool(tt), retries=ST["retries"], r429=ST["r429"], **n))
    with open(f"data/{day}.jsonl", "a") as f:
        for x in lines: f.write(json.dumps(x, separators=(",", ":")) + "\n")
    print(mode, n, "retries", ST["retries"], "429", ST["r429"])

if __name__ == "__main__": main(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("full", "light") else "full")
