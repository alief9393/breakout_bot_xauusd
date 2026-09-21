#!/usr/bin/env python3
"""
StreamTrade RESEARCH — full profile of the honest fade replica.

Rules (the reproducible edge): Asian-morning session, fade a >=MOVE_THRESHOLD move
over LOOKBACK min, cooldown between signals. We generate our OWN signals and report:
  * how many trades/day (must clear ~1/day like the channel)
  * win rate + expectancy at EACH exit (TP1..TP4)
  * per-day distribution and month-by-month consistency
"""

import json
import datetime
import collections
import statistics as st

BASE = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/"

SESSION_HOURS  = {22, 23, 0, 1, 2, 3}
LOOKBACK_MIN   = 30
MOVE_THRESHOLD = 30
COOLDOWN_MIN   = 240
SL_PIPS        = 130
TP_PIPS        = {1: 30, 2: 60, 3: 90, 4: 120}
SLIP = 1.0; SLSLIP = 2.0; COMM_PER_100K = 6.0; SPREAD = 1.0
LOOK = 60 * 48


def load():
    ts = []; op = []; hi = []; lo = []
    with open(BASE + "xauusd_m1.csv") as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip().split(",")
            ts.append(datetime.datetime.fromisoformat(t)); op.append(float(o)); hi.append(float(h)); lo.append(float(l))
    return ts, op, hi, lo


def comm_pips(entry):
    return COMM_PER_100K * (entry * 100 / 100000.0) / 10.0


def main():
    ts, op, hi, lo = load()
    # generate fade signals
    raw = []; last = -10**9
    for i in range(LOOKBACK_MIN, len(ts) - LOOK):
        if ts[i].hour not in SESSION_HOURS or i - last < COOLDOWN_MIN:
            continue
        move = (op[i] - op[i - LOOKBACK_MIN]) * 10
        if abs(move) < MOVE_THRESHOLD:
            continue
        d = -1 if move > 0 else 1
        raw.append({"i": i, "t": ts[i], "d": d, "entry": op[i] + d * SLIP / 10})
        last = i

    n = len(raw); days = (raw[-1]["t"] - raw[0]["t"]).days or 1
    print("═" * 62)
    print("HONEST FADE REPLICA — profile")
    print("═" * 62)
    print(f"  rules: fade >={MOVE_THRESHOLD}p / {LOOKBACK_MIN}min, hours {sorted(SESSION_HOURS)} UTC, cooldown {COOLDOWN_MIN}min")
    print(f"  signals: {n} over {days} days  =  {n/days:.2f}/day\n")

    # per-day distribution
    byday = collections.Counter(r["t"].strftime("%Y-%m-%d") for r in raw)
    dist = collections.Counter(byday.values())
    active = len(byday)
    print(f"  active trading days: {active} of {days} ({100*active/days:.0f}%)")
    for k in sorted(dist):
        print(f"    {k} signal(s)/day: {dist[k]} days")

    # win rate + expectancy per exit
    print(f"\n  {'exit':6s} {'win%':>5s} {'avg pips':>9s} {'pips/day':>9s} {'total':>8s}")
    for tpn, tpp in TP_PIPS.items():
        wins = 0; pips = []
        for r in raw:
            d = r["d"]; entry = r["entry"]
            tp = entry + d * tpp / 10; sl = entry - d * SL_PIPS / 10
            res = None
            for j in range(r["i"], r["i"] + LOOK):
                if (lo[j] <= sl) if d == 1 else (hi[j] >= sl): res = "SL"; break
                if (hi[j] >= tp) if d == 1 else (lo[j] <= tp): res = "TP"; break
            p = (tpp if res == "TP" else -(SL_PIPS + SLSLIP) if res == "SL" else 0.0) - (SPREAD + comm_pips(entry))
            wins += res == "TP"; pips.append(p)
        tot = sum(pips)
        print(f"  TP{tpn:<4d} {100*wins/n:4.0f}% {tot/n:+8.1f} {tot/days:+8.1f} {tot:+8.0f}")

    # monthly consistency at the best-looking exit (TP2)
    print(f"\n  month-by-month (TP2 exit, pips):")
    mon = collections.defaultdict(float)
    for r in raw:
        d = r["d"]; entry = r["entry"]; tpp = TP_PIPS[2]
        tp = entry + d * tpp / 10; sl = entry - d * SL_PIPS / 10
        res = None
        for j in range(r["i"], r["i"] + LOOK):
            if (lo[j] <= sl) if d == 1 else (hi[j] >= sl): res = "SL"; break
            if (hi[j] >= tp) if d == 1 else (lo[j] <= tp): res = "TP"; break
        p = (tpp if res == "TP" else -(SL_PIPS + SLSLIP) if res == "SL" else 0.0) - (SPREAD + comm_pips(entry))
        mon[r["t"].strftime("%Y-%m")] += p
    for m in sorted(mon):
        print(f"    {m}: {mon[m]:+.0f} pips")


if __name__ == "__main__":
    main()
