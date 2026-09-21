#!/usr/bin/env python3
"""
RESEARCH — one trade per morning, swept across 2023–2026, gated on real M1.
Goal: a config that is POSITIVE EVERY YEAR (no overfitting to one regime) with a
respectable win rate. Trades at most once per day at a chosen morning hour.

Knobs: entry hour (UTC), lookback window, trigger threshold, side (follow/fade),
SL size, TP ladder. Reuses bt_dynamic_split's dynamic-split simulate + plan_split
so the exit matches the live bots (TP2+TP3 mapping, trail behind each hit TP).
"""
import sys, collections
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

b.PRICE_CSV = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/xauusd_m1_ext.csv"
b.MAX_LOOK_MIN = 1200          # cap sim horizon at ~20h so a morning trade resolves same day (speed)
ts, op, hi, lo = b.load_prices()

RISK = 0.02; BAL0 = 1000.0
TP_FRACS = [0.2, 0.4, 0.6, 0.8, 1.0, 1.2]     # x SL distance -> 6-rung ladder

# Pre-index: first bar of each (date, hour) -> bar index
hour_idx = {}
last_key = None
for i in range(60, len(ts) - b.MAX_LOOK_MIN):
    t = ts[i]
    key = (t.date(), t.hour)
    if key != last_key and key not in hour_idx:
        hour_idx[key] = i
    last_key = key


def signals(entry_hour, lookback, trigger, side):
    """One signal per day at entry_hour, if |lookback move| >= trigger."""
    out = []
    for (d, h), i in hour_idx.items():
        if h != entry_hour or i - lookback < 0:
            continue
        base = op[i - lookback]
        if base <= 0:
            continue
        move = (op[i] - base) / base
        if abs(move) < trigger:
            continue
        dirn = (1 if move > 0 else -1)
        if side == "fade":
            dirn = -dirn
        out.append((i, dirn))
    return sorted(out)


def equity(sigs, sl_pct):
    eq = BAL0; peak = eq; dd = 0.0; w = l = 0
    byyear = collections.defaultdict(lambda: [BAL0, BAL0, 0.0, 0, 0])   # eq, peak, dd, w, l
    yr_eq = {}
    for i0, d in sigs:
        y = ts[i0].year
        if y not in yr_eq:
            yr_eq[y] = [BAL0, BAL0, 0.0, 0, 0]
        st = yr_eq[y]
        entry = op[i0] + d * (b.ENTRY_SLIP_PIPS / 10.0)
        sl_dist = sl_pct * entry
        sl = entry - d * sl_dist
        tps = [entry + d * f * sl_dist for f in TP_FRACS]
        ahead = [t for t in tps if (t > entry if d == 1 else t < entry)]
        if not ahead or sl_dist <= 0:
            continue
        total = max(0.01, min(round((st[0] * RISK) / (sl_dist * b.CONTRACT_OZ), 2), 100.0))
        idx, sizes = b.plan_split(total, len(ahead))
        gross = b.simulate(entry, d, sl, [ahead[k] for k in idx], sizes, i0, ts, hi, lo)
        pnl = gross - (b.SPREAD_PIPS + b.comm_pips(entry)) * 10 * sum(sizes)
        st[0] += pnl
        st[1] = max(st[1], st[0]); st[2] = min(st[2], st[0] / st[1] - 1)
        st[3] += pnl > 0; st[4] += pnl <= 0
    res = {}
    for y, st in yr_eq.items():
        n = st[3] + st[4]
        res[y] = dict(ret=st[0] / BAL0 - 1, dd=st[2], win=(st[3] / n if n else 0), n=n)
    return res


def summarize(cfg):
    eh, lb, tr, side, sl_pct = cfg
    sigs = signals(eh, lb, tr, side)
    res = equity(sigs, sl_pct)
    years = sorted(res)
    allpos = years and all(res[y]["ret"] > 0 for y in years)
    worst = min((res[y]["ret"] for y in years), default=-9)
    avgwin = sum(res[y]["win"] for y in years) / len(years) if years else 0
    return dict(cfg=cfg, res=res, allpos=allpos, worst=worst, avgwin=avgwin,
                ntot=sum(res[y]["n"] for y in years))


if __name__ == "__main__":
    HOURS = [1, 2, 3]
    LOOKBACKS = [30, 60]
    TRIGGERS = [0.0004]
    SIDES = ["follow", "fade"]
    SL_PCTS = [0.0015, 0.002, 0.003, 0.004, 0.006]
    configs = [(eh, lb, tr, s, sl) for eh in HOURS for lb in LOOKBACKS
               for tr in TRIGGERS for s in SIDES for sl in SL_PCTS]
    print(f"sweeping {len(configs)} configs · one trade/morning · 2% risk · TP2+TP3 split\n")
    rows = [summarize(c) for c in configs]
    rows.sort(key=lambda r: (r["allpos"], r["worst"]), reverse=True)
    print(f"{'hour':>4} {'lb':>3} {'trig':>6} {'side':>6} {'sl%':>5} | "
          f"{'2023':>6} {'2024':>6} {'2025':>6} {'2026':>6} | {'worstYr':>7} {'avgWin':>6} {'n':>4} {'ALL+':>4}")
    for r in rows:
        eh, lb, tr, side, sl = r["cfg"]
        cells = " ".join(f"{100*r['res'].get(y,{}).get('ret',0):+6.0f}" for y in (2023, 2024, 2025, 2026))
        flag = "✅" if r["allpos"] else ""
        print(f"{eh:>4} {lb:>3} {tr:>6.4f} {side:>6} {sl:>5.3f} | {cells} | "
              f"{100*r['worst']:+6.0f}% {100*r['avgwin']:5.0f}% {r['ntot']:>4} {flag:>4}")
