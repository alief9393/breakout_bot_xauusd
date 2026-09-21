#!/usr/bin/env python3
"""
RESEARCH — enhance momentum + dynamic-split WITHOUT cutting frequency.
Sweeps SL distance and session width (exit/frequency levers, not signal filters).
Reports per config: signals/day, win%, return (2% risk), maxDD. Train/test split too.
"""
import sys, datetime
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

ts, op, hi, lo = b.load_prices()
RISK = 0.02; BAL0 = 1000.0; START = "2026-03-01"; SPLIT_DATE = "2026-06-15"


def gen(hours, sl_pips, move_min=15, cooldown=120):
    out = []; last = -10**9
    for i in range(30, len(ts) - b.MAX_LOOK_MIN):
        if ts[i].hour not in hours or i - last < cooldown: continue
        if ts[i].date().isoformat() < START: continue
        mv = (op[i] - op[i - 30]) * 10
        if abs(mv) < move_min: continue
        d = 1 if mv > 0 else -1; entry = op[i]
        tps = [entry + d * p / 10.0 for p in [30, 60, 90, 120, 150, 180]]
        out.append({"i": i, "t": ts[i], "d": d, "entry": entry, "tps": tps, "sl": entry - d * sl_pips / 10.0})
        last = i
    return out


def evaluate(sigs):
    eq = BAL0; peak = eq; dd = 0; w = l = 0
    tr = te = 0; twe = tle = 0
    for s in sigs:
        i0 = s["i"]; d = s["d"]; entry = op[i0] + d * 0.1
        ahead = [t for t in s["tps"] if (t > entry if d == 1 else t < entry)]
        sl = s["sl"]; sl_dist = abs(entry - sl)
        if not ahead or sl_dist <= 0: continue
        calc = (eq * RISK) / (sl_dist * b.CONTRACT_OZ)
        total = max(0.01, min(round(calc, 2), 100.0))
        idx, sizes = b.plan_split(total, len(ahead))
        gross = b.simulate(entry, d, sl, [ahead[k] for k in idx], sizes, i0, ts, hi, lo)
        pnl = gross - (b.SPREAD_PIPS + b.comm_pips(entry)) * 10 * sum(sizes)
        eq += pnl; peak = max(peak, eq); dd = min(dd, eq / peak - 1); w += pnl > 0; l += pnl <= 0
        if s["t"].date().isoformat() < SPLIT_DATE: tr += 1; twe += pnl > 0
        else: te += 1; tle += pnl > 0
    n = w + l; days = len(set(s["t"].date() for s in sigs)) or 1
    trw = 100 * twe / tr if tr else 0; tew = 100 * tle / te if te else 0
    return dict(n=n, perday=n / days, win=100 * w / n if n else 0, mult=eq / BAL0, dd=100 * dd,
                trainwin=trw, testwin=tew)


SESSIONS = {"morning 0-3": {0,1,2,3}, "0-5": {0,1,2,3,4,5}, "22-3": {22,23,0,1,2,3}}
print("MOMENTUM + dynamic-split — enhance without cutting frequency (2% risk)")
print(f"  {'session':12s} {'SL':>4s} {'sigs':>5s} {'/day':>5s} {'win%':>5s} {'x money':>8s} {'maxDD':>7s}  {'train/test win'}")
for sname, hrs in SESSIONS.items():
    for sl in (90, 110, 130):
        m = evaluate(gen(hrs, sl))
        print(f"  {sname:12s} {sl:4d} {m['n']:5d} {m['perday']:5.2f} {m['win']:4.0f}% {m['mult']:7.2f}x {m['dd']:6.0f}%   {m['trainwin']:.0f}%/{m['testwin']:.0f}%")
