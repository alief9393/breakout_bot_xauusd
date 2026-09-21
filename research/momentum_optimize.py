#!/usr/bin/env python3
"""
RESEARCH — full grid search for the best momentum + dynamic-split config.
Sweeps session / SL / cooldown / move-threshold. Splits TRAIN (earlier) vs TEST (later),
ranks by TRAIN return, and shows TEST beside it — so we pick a config that GENERALISES,
not one overfit to the whole period. 2% risk throughout.
"""
import sys, datetime
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

ts, op, hi, lo = b.load_prices()
RISK = 0.02; BAL0 = 1000.0; START = "2026-03-01"; SPLIT_DATE = "2026-06-15"


def gen(hours, sl_pips, move_min, cooldown):
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


def equity(sigs):
    eq = BAL0; peak = eq; dd = 0; w = l = 0
    for s in sigs:
        i0 = s["i"]; d = s["d"]; entry = op[i0] + d * 0.1
        ahead = [t for t in s["tps"] if (t > entry if d == 1 else t < entry)]
        sl = s["sl"]; sl_dist = abs(entry - sl)
        if not ahead or sl_dist <= 0: continue
        total = max(0.01, min(round((eq * RISK) / (sl_dist * b.CONTRACT_OZ), 2), 100.0))
        idx, sizes = b.plan_split(total, len(ahead))
        gross = b.simulate(entry, d, sl, [ahead[k] for k in idx], sizes, i0, ts, hi, lo)
        pnl = gross - (b.SPREAD_PIPS + b.comm_pips(entry)) * 10 * sum(sizes)
        eq += pnl; peak = max(peak, eq); dd = min(dd, eq / peak - 1); w += pnl > 0; l += pnl <= 0
    n = w + l
    return dict(mult=eq / BAL0, dd=100 * dd, win=100 * w / n if n else 0, n=n)


SESSIONS = {"0-3": {0,1,2,3}, "0-5": {0,1,2,3,4,5}, "23-5": {23,0,1,2,3,4,5}}
rows = []
for sname, hrs in SESSIONS.items():
    for sl in (110, 130, 150):
        for cd in (60, 120):
            for mv in (10, 15, 20):
                sigs = gen(hrs, sl, mv, cd)
                tr = [s for s in sigs if s["t"].date().isoformat() < SPLIT_DATE]
                te = [s for s in sigs if s["t"].date().isoformat() >= SPLIT_DATE]
                if len(te) < 20 or len(tr) < 20: continue
                mt = equity(tr); me = equity(te); full = equity(sigs)
                days = len(set(s["t"].date() for s in sigs)) or 1
                rows.append((mt["mult"], me["mult"], full, sname, sl, cd, mv, len(sigs) / days))

rows.sort(reverse=True, key=lambda r: r[0])          # rank by TRAIN return
print("MOMENTUM + dynamic-split grid — ranked by TRAIN return, TEST shown for validation (2% risk)")
print(f"  {'sess':5s} {'SL':>4s} {'cd':>4s} {'mv':>3s} {'/day':>5s} | {'TRAIN x':>8s} {'TEST x':>7s} | {'full x':>7s} {'win%':>5s} {'maxDD':>7s}")
for mt, me, full, sname, sl, cd, mv, perday in rows[:15]:
    flag = " <= robust" if (mt > 1.2 and me > 1.2) else ""
    print(f"  {sname:5s} {sl:4d} {cd:4d} {mv:3d} {perday:5.2f} | {mt:7.2f}x {me:6.2f}x | {full['mult']:6.2f}x {full['win']:4.0f}% {full['dd']:6.0f}%{flag}")
