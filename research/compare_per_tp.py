#!/usr/bin/env python3
"""
Per-TP comparison: the CHANNEL (stated & market entry) vs OUR replicas (fade & momentum).
% of trades that reach each TP before the 130-pip SL, same period, real M1.
"""

import json
import datetime

BASE = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/"
SL_PIPS = 130
LOOK = 60 * 48
LADDER = [30, 60, 90, 120, 150]      # replica TP ladder (pips)
MORNING = {0, 1, 2, 3}


def load():
    ts = []; op = []; hi = []; lo = []
    with open(BASE + "xauusd_m1.csv") as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip().split(",")
            ts.append(datetime.datetime.fromisoformat(t)); op.append(float(o)); hi.append(float(h)); lo.append(float(l))
    return ts, op, hi, lo


def find(ts, when):
    a, b = 0, len(ts)
    while a < b:
        m = (a + b) // 2
        if ts[m] < when: a = m + 1
        else: b = m
    return a


def per_tp(entries, ts, op, hi, lo):
    """entries: (i0, d, entry, tp_levels[list], sl_level). returns reached-counts per TP index + n."""
    reach = [0] * 6; n = 0
    for i0, d, entry, tps, sl in entries:
        if i0 >= len(ts) - 10: continue
        n += 1; hit = 0
        for j in range(i0, min(i0 + LOOK, len(ts))):
            if (lo[j] <= sl) if d == 1 else (hi[j] >= sl): break
            while hit < len(tps) and ((hi[j] >= tps[hit]) if d == 1 else (lo[j] <= tps[hit])): hit += 1
            if hit >= len(tps): break
        for k in range(1, 6):
            if hit >= k: reach[k] += 1
    return reach, n


def channel(ts, op, hi, lo, mode):
    out = []
    for s in (json.loads(x) for x in open(BASE + "signals.jsonl")):
        if s["date"][:10] < "2026-01-01": continue
        i0 = find(ts, datetime.datetime.fromisoformat(s["date"]).replace(tzinfo=None))
        d = 1 if s["dir"] == "BUY" else -1
        entry = s["entry"] if mode == "stated" else op[i0]
        entry += d * 0.1
        tps = [t for t in s["tps"] if (t > entry if d == 1 else t < entry)]   # ahead of entry
        out.append((i0, d, entry, tps, s["sl"]))
    return out


def replica(ts, op, hi, lo, direction):
    out = []; last = -10**9
    for i in range(30, len(ts) - LOOK):
        if ts[i].hour not in MORNING or i - last < 120: continue
        mv = (op[i] - op[i - 30]) * 10
        if abs(mv) < 15: continue
        sgn = 1 if mv > 0 else -1
        d = -sgn if direction == "fade" else sgn
        entry = op[i] + d * 0.1
        tps = [entry + d * p / 10 for p in LADDER]
        sl = entry - d * SL_PIPS / 10
        out.append((i, d, entry, tps, sl)); last = i
    return out


def show(name, entries, ts, op, hi, lo):
    reach, n = per_tp(entries, ts, op, hi, lo)
    days = len(set(ts[e[0]].date() for e in entries if e[0] < len(ts))) or 1
    cells = "  ".join(f"{100*reach[k]/n:3.0f}%" for k in range(1, 6))
    print(f"  {name:20s} n={n:4d} {n/days:4.2f}/d   {cells}")


def main():
    ts, op, hi, lo = load()
    print("% reaching each TP before the 130p SL (real M1, Jan–Sep 2026)\n")
    print(f"  {'strategy':20s} {'sigs':>6s} {'freq':>6s}   {'TP1':>4s} {'TP2':>5s} {'TP3':>5s} {'TP4':>5s} {'TP5':>5s}")
    show("CHANNEL (stated)", channel(ts, op, hi, lo, "stated"), ts, op, hi, lo)
    show("CHANNEL (market)", channel(ts, op, hi, lo, "market"), ts, op, hi, lo)
    show("REPLICA fade", replica(ts, op, hi, lo, "fade"), ts, op, hi, lo)
    show("REPLICA momentum", replica(ts, op, hi, lo, "momo"), ts, op, hi, lo)
    print("\n  (replicas: morning 0-3 UTC, fade/follow a >=15p move, fixed 30/60/90/120/150 ladder)")


if __name__ == "__main__":
    main()
