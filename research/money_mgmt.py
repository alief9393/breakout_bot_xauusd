#!/usr/bin/env python3
"""
RESEARCH — find the SMARTEST money management (exit), holding the entry fixed.
Same entries for every scheme, so the only variable is how you manage the trade.
Judged on expectancy (avg R) + WORST-YEAR + max drawdown across 2023-2026, real gold M1.

Entry (fixed): one LONG per day at a set hour (neutral, frequent). We're isolating the EXIT.
1R = SL_PIPS. All results in R multiples.
"""
import sys, collections
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

b.PRICE_CSV = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/xauusd_m1_ext.csv"
ts, op, hi, lo = b.load_prices()
PIP = 0.1
SL_PIPS = 150            # 1R
ENTRY_HOUR = 1          # one long/day at this UTC hour
HORIZON = 3 * 1440      # max hold 3 days (bars)

# entry bars: first bar of ENTRY_HOUR each day
entries = []
seen = set()
for i in range(len(ts) - HORIZON):
    t = ts[i]
    if t.hour == ENTRY_HOUR and t.date() not in seen:
        entries.append(i); seen.add(t.date())


def sim(i0, scheme):
    """LONG from op[i0]. Returns R. 1R = SL_PIPS."""
    entry = op[i0]; R = SL_PIPS * PIP
    sl = entry - R
    d = 1
    # scheme params
    if scheme.startswith("fixed_"):
        mult = float(scheme.split("_")[1].rstrip("R"))
        tp = entry + mult * R
        for j in range(i0 + 1, i0 + HORIZON):
            if lo[j] <= sl:  return -1.0
            if hi[j] >= tp:  return mult
        return (op[i0 + HORIZON - 1] - entry) / R
    if scheme == "trail_1R":                       # pure runner: trail 1R below the high-water
        stop = sl; hw = entry
        for j in range(i0 + 1, i0 + HORIZON):
            if lo[j] <= stop: return (stop - entry) / R
            hw = max(hw, hi[j]); stop = max(stop, hw - R)
        return (op[i0 + HORIZON - 1] - entry) / R
    if scheme == "be_then_trail":                  # BE at +1R, then trail 1R
        stop = sl; hw = entry; armed = False
        for j in range(i0 + 1, i0 + HORIZON):
            if lo[j] <= stop: return (stop - entry) / R
            if hi[j] >= entry + R and not armed:
                armed = True; stop = max(stop, entry)
            hw = max(hw, hi[j])
            if armed: stop = max(stop, hw - R)
        return (op[i0 + HORIZON - 1] - entry) / R
    if scheme == "scale_half_trail":               # bank 0.5 at +1R, BE the rest, trail 1R
        stop = sl; hw = entry; banked = 0.0; half = False
        for j in range(i0 + 1, i0 + HORIZON):
            if lo[j] <= stop: return banked + 0.5 * (stop - entry) / R if half else (stop - entry) / R
            if hi[j] >= entry + R and not half:
                half = True; banked = 0.5 * 1.0; stop = max(stop, entry)
            hw = max(hw, hi[j])
            if half: stop = max(stop, hw - R)
        last = (op[i0 + HORIZON - 1] - entry) / R
        return banked + 0.5 * last if half else last
    if scheme == "trail_wide_2R":                  # let it breathe: trail 2R below high-water
        stop = sl; hw = entry
        for j in range(i0 + 1, i0 + HORIZON):
            if lo[j] <= stop: return (stop - entry) / R
            hw = max(hw, hi[j]); stop = max(stop, hw - 2 * R)
        return (op[i0 + HORIZON - 1] - entry) / R
    raise ValueError(scheme)


SCHEMES = ["fixed_1R", "fixed_2R", "fixed_3R", "be_then_trail",
           "scale_half_trail", "trail_1R", "trail_wide_2R"]


def evaluate(scheme):
    by_year = collections.defaultdict(list)
    for i0 in entries:
        r = sim(i0, scheme)
        # costs: spread+comm ~ (SPREAD+comm)/SL_PIPS of an R, both sides
        cost_R = (b.SPREAD_PIPS + b.comm_pips(op[i0])) / SL_PIPS
        by_year[ts[i0].year].append(r - cost_R)
    years = sorted(by_year)
    yr_tot = {y: sum(by_year[y]) for y in years}
    allR = [r for y in years for r in by_year[y]]
    n = len(allR); wins = sum(1 for r in allR if r > 0)
    # max drawdown on the R equity curve
    eq = 0.0; peak = 0.0; mdd = 0.0
    for r in allR:
        eq += r; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    worst_year = min(yr_tot.values())
    return dict(total=sum(allR), avg=sum(allR) / n, win=100 * wins / n,
                mdd=mdd, worst=worst_year, yr=yr_tot,
                allpos=all(v > 0 for v in yr_tot.values()))


if __name__ == "__main__":
    print(f"MONEY-MANAGEMENT SEARCH · fixed entry (1 long/day @ {ENTRY_HOUR}:00) · 1R={SL_PIPS}p · {len(entries)} trades")
    print(f"{ts[0].date()}→{ts[-1].date()} · real gold M1 · all values in R\n")
    print(f"  {'scheme':>17} {'totalR':>8} {'avgR':>6} {'win%':>5} {'maxDD_R':>8} {'worstYr':>8} {'2023':>6}{'2024':>7}{'2025':>7}{'2026':>7}  all+")
    rows = [(s, evaluate(s)) for s in SCHEMES]
    rows.sort(key=lambda x: x[1]["worst"], reverse=True)
    for s, r in rows:
        yrs = " ".join(f"{r['yr'].get(y,0):>+6.0f}" for y in (2023, 2024, 2025, 2026))
        print(f"  {s:>17} {r['total']:>+8.0f} {r['avg']:>+6.2f} {r['win']:>4.0f}% {r['mdd']:>+8.0f} {r['worst']:>+8.0f} {yrs}  {'✅' if r['allpos'] else ''}")
    print("\n  worst-year is the honest ranking key: positive every year = robust across regimes.")
