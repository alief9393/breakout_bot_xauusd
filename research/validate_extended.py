#!/usr/bin/env python3
"""
RESEARCH — validate the FROZEN momentum config across 3 years (2023-2026), per year.
Config was chosen on 2026 only; here we run it UNCHANGED on years it never saw.
If it stays profitable across regimes, that's real out-of-sample confidence.
"""
import sys, datetime, collections
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b
b.PRICE_CSV = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/xauusd_m1_ext.csv"
ts, op, hi, lo = b.load_prices()

# ── FROZEN config (do NOT re-tune) ──
HOURS = {0, 1, 2, 3, 4, 5}; MOVE_MIN = 15; COOLDOWN = 120; SL_PIPS = 150
LADDER = [30, 60, 90, 120, 150, 180]
RISK = 0.02; BAL0 = 1000.0


def gen():
    out = []; last = -10**9
    for i in range(30, len(ts) - b.MAX_LOOK_MIN):
        if ts[i].hour not in HOURS or i - last < COOLDOWN: continue
        mv = (op[i] - op[i - 30]) * 10
        if abs(mv) < MOVE_MIN: continue
        d = 1 if mv > 0 else -1; entry = op[i]
        tps = [entry + d * p / 10.0 for p in LADDER]
        out.append({"i": i, "t": ts[i], "d": d, "entry": entry, "tps": tps, "sl": entry - d * SL_PIPS / 10.0})
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
    n = w + l; days = len(set(s["t"].date() for s in sigs)) or 1
    return dict(n=n, perday=n / days, win=100 * w / n if n else 0, mult=eq / BAL0, dd=100 * dd)


sigs = gen()
print(f"FROZEN momentum+dynamic-split across {ts[0].date()} → {ts[-1].date()}  ·  2% risk  ·  ${BAL0:.0f} fresh/year")
print(f"  total signals: {len(sigs)}\n")
print(f"  {'year':6s} {'gold range':>16s} {'sigs':>5s} {'/day':>5s} {'win%':>5s} {'return':>8s} {'maxDD':>7s}")
byyear = collections.defaultdict(list)
for s in sigs: byyear[s["t"].year].append(s)
for y in sorted(byyear):
    ys = byyear[y]; m = equity(ys)
    prices = [op[s["i"]] for s in ys]
    gr = f"${min(prices):.0f}-{max(prices):.0f}"
    print(f"  {y:6d} {gr:>16s} {m['n']:5d} {m['perday']:5.2f} {m['win']:4.0f}% {100*(m['mult']-1):+7.0f}% {m['dd']:6.0f}%")
full = equity(sigs)
print(f"\n  ALL 3Y (continuous): {full['n']} sigs · {full['perday']:.2f}/day · {full['win']:.0f}% win · {100*(full['mult']-1):+.0f}% · maxDD {full['dd']:.0f}%")
