#!/usr/bin/env python3
"""
RESEARCH — FINAL test: percentage-based momentum (scale-invariant) across 3 years.
Everything scales with price so the strategy behaves the same at $1,900 and $4,400:
  * trigger: |30-min move| >= MOVE_PCT of price
  * SL     : SL_PCT of price
  * TP ladder: [0.2,0.4,0.6,0.8,1.0,1.2] x SL_dist  (= 30..180 pips at a 150-pip SL)
Calibrated to match the 2026 config's proportions. Run per year, 2% risk.
If it's positive EVERY year -> real edge. If not -> we stop.
"""
import sys, collections
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b
b.PRICE_CSV = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/xauusd_m1_ext.csv"
ts, op, hi, lo = b.load_prices()

HOURS = {0, 1, 2, 3, 4, 5}; COOLDOWN = 120
MOVE_PCT = 0.00034          # ~15 pips at $4400
SL_PCT   = 0.00341          # ~150 pips at $4400
TP_FRACS = [0.2, 0.4, 0.6, 0.8, 1.0, 1.2]   # x SL distance
RISK = 0.02; BAL0 = 1000.0


def gen():
    out = []; last = -10**9
    for i in range(30, len(ts) - b.MAX_LOOK_MIN):
        if ts[i].hour not in HOURS or i - last < COOLDOWN: continue
        base = op[i - 30]
        if abs(op[i] - base) / base < MOVE_PCT: continue
        d = 1 if op[i] > base else -1
        entry = op[i]; sl_dist = SL_PCT * entry
        tps = [entry + d * f * sl_dist for f in TP_FRACS]
        out.append({"i": i, "t": ts[i], "d": d, "entry": entry, "tps": tps, "sl": entry - d * sl_dist})
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
print(f"PERCENTAGE-BASED momentum + dynamic-split · {ts[0].date()}→{ts[-1].date()} · 2% risk · ${BAL0:.0f} fresh/year")
print(f"  total signals: {len(sigs)}\n")
print(f"  {'year':6s} {'sigs':>5s} {'/day':>5s} {'win%':>5s} {'return':>8s} {'maxDD':>7s}")
byyear = collections.defaultdict(list)
for s in sigs: byyear[s["t"].year].append(s)
allpos = True
for y in sorted(byyear):
    m = equity(byyear[y])
    if m["mult"] < 1: allpos = False
    print(f"  {y:6d} {m['n']:5d} {m['perday']:5.2f} {m['win']:4.0f}% {100*(m['mult']-1):+7.0f}% {m['dd']:6.0f}%")
full = equity(sigs)
print(f"\n  ALL 3Y: {full['n']} sigs · {full['perday']:.2f}/day · {full['win']:.0f}% win · {100*(full['mult']-1):+.0f}% · maxDD {full['dd']:.0f}%")
print(f"\n  VERDICT: {'POSITIVE EVERY YEAR — real edge ✅' if allpos else 'NOT positive every year — stop ❌'}")
