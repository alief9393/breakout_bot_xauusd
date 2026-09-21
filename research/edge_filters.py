#!/usr/bin/env python3
"""
RESEARCH — hunt for a PREDICTIVE morning entry filter (2023–2026, real M1).
Exit = let winners run (single runner to RUN_TP x SL) — the profitable direction we found.
Judge by WORST-YEAR return (must be positive every year), never win rate.
Filters tested: day-of-week, prior-day trend (momentum/reversal), volatility regime.
"""
import sys, collections, datetime
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

b.PRICE_CSV = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/xauusd_m1_ext.csv"
b.MAX_LOOK_MIN = 1200
ts, op, hi, lo = b.load_prices()

ENTRY_HOUR = 3
LOOKBACK = 30
RISK = 0.02; BAL0 = 1000.0
SL_PCT = 0.004
RUN_TP = 1.2          # runner: single TP at 1.2x SL distance (let it run)

# ---- daily aggregates: per date -> open, close, high, low, return, range_pct ----
days = collections.OrderedDict()
for i in range(len(ts)):
    d = ts[i].date()
    if d not in days:
        days[d] = {"o": op[i], "c": op[i], "hi": hi[i], "lo": lo[i], "i0": i}
    dd = days[d]
    dd["c"] = op[i]; dd["hi"] = max(dd["hi"], hi[i]); dd["lo"] = min(dd["lo"], lo[i])
dlist = list(days.items())
for k in range(len(dlist)):
    d, dd = dlist[k]
    dd["ret"] = (dd["c"] - dd["o"]) / dd["o"]
    dd["range"] = (dd["hi"] - dd["lo"]) / dd["o"]
    dd["prev"] = dlist[k - 1][1] if k > 0 else None

# entry bar per day at ENTRY_HOUR
entry_bar = {}
seen = set()
for i in range(LOOKBACK, len(ts) - b.MAX_LOOK_MIN):
    t = ts[i]
    if t.hour == ENTRY_HOUR and t.date() not in seen:
        entry_bar[t.date()] = i; seen.add(t.date())


def trade_pnl(i0, d, bal):
    entry = op[i0] + d * (b.ENTRY_SLIP_PIPS / 10.0)
    sl_dist = SL_PCT * entry
    sl = entry - d * sl_dist
    tp = entry + d * RUN_TP * sl_dist
    total = max(0.01, min(round((bal * RISK) / (sl_dist * b.CONTRACT_OZ), 2), 100.0))
    gross = b.simulate(entry, d, sl, [tp], [total], i0, ts, hi, lo)
    return gross - (b.SPREAD_PIPS + b.comm_pips(entry)) * 10 * total


def evaluate(name, pick):
    """pick(date, dd) -> direction (+1/-1) or 0 to skip. Runs per-year, fresh $1000."""
    yr = {}
    for d, i0 in entry_bar.items():
        dd = days[d]
        if dd["prev"] is None:
            continue
        dirn = pick(d, dd)
        if not dirn:
            continue
        y = ts[i0].year
        st = yr.setdefault(y, [BAL0, 0, 0])
        st[0] += trade_pnl(i0, dirn, st[0])
        pnl_pos = st[0]  # not per-trade; track w/l separately below
    # recompute with win tracking
    yr = {}
    for d, i0 in entry_bar.items():
        dd = days[d]
        if dd["prev"] is None:
            continue
        dirn = pick(d, dd)
        if not dirn:
            continue
        y = ts[i0].year
        st = yr.setdefault(y, [BAL0, 0, 0])
        p = trade_pnl(i0, dirn, st[0])
        st[0] += p; st[1] += p > 0; st[2] += p <= 0
    years = sorted(yr)
    rets = {y: yr[y][0] / BAL0 - 1 for y in years}
    ns = {y: yr[y][1] + yr[y][2] for y in years}
    wins = {y: (yr[y][1] / ns[y] if ns[y] else 0) for y in years}
    worst = min(rets.values()) if rets else -9
    allpos = bool(rets) and all(v > 0 for v in rets.values())
    cells = " ".join(f"{100*rets.get(y,0):+6.0f}" for y in (2023, 2024, 2025, 2026))
    wc = sum(ns.values())
    avgwin = sum(wins.values()) / len(wins) if wins else 0
    print(f"{name:34} | {cells} | worst {100*worst:+5.0f}% win {100*avgwin:3.0f}% n {wc:>4} {'✅' if allpos else ''}")


print(f"morning entry {ENTRY_HOUR}:00 UTC · runner exit ({RUN_TP}x SL) · judged on WORST YEAR\n")
print(f"{'filter':34} |   2023   2024   2025   2026 | worst      win    n")
print("-- baselines --")
mv = lambda d, dd: (1 if op[entry_bar[d]] > op[entry_bar[d] - LOOKBACK] else -1)
evaluate("follow morning move (all days)", mv)
evaluate("fade morning move (all days)", lambda d, dd: -mv(d, dd))

print("-- day of week (follow) --")
for wd, nm in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri"]):
    evaluate(f"only {nm}", lambda d, dd, wd=wd: mv(d, dd) if d.weekday() == wd else 0)

print("-- prior-day trend conditioning --")
evaluate("prev up -> BUY (daily momentum)", lambda d, dd: 1 if dd["prev"]["ret"] > 0 else 0)
evaluate("prev down -> SELL (daily momentum)", lambda d, dd: -1 if dd["prev"]["ret"] < 0 else 0)
evaluate("prev up -> SELL (daily reversal)", lambda d, dd: -1 if dd["prev"]["ret"] > 0 else 0)
evaluate("prev down -> BUY (daily reversal)", lambda d, dd: 1 if dd["prev"]["ret"] < 0 else 0)

print("-- volatility regime (follow morning move) --")
ranges = sorted(dd["range"] for _, dd in days.items() if dd["range"] > 0)
med = ranges[len(ranges) // 2]
evaluate("prev-day range LOW -> follow", lambda d, dd: mv(d, dd) if dd["prev"]["range"] < med else 0)
evaluate("prev-day range HIGH -> follow", lambda d, dd: mv(d, dd) if dd["prev"]["range"] >= med else 0)
