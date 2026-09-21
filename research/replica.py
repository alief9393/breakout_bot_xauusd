#!/usr/bin/env python3
"""
StreamTrade RESEARCH — REPLICA v2 of the channel's fade strategy.

Decoded rules:
  * Asian-morning session (their main cluster)
  * fade OVER-EXTENSION from the mean (price stretched far from the 240-min MA)
  * enter toward the mean, TP4-distance target, ~130p SL
We generate our own signals, backtest on real M1, and sweep the over-extension
threshold to see the win rate climb toward theirs. Reads parent data only.
"""

import json
import datetime

BASE = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/"

SESSION_HOURS = {22, 23, 0, 1, 2, 3}
MA_MIN        = 240        # mean reference (over-extension measured vs this MA)
COOLDOWN_MIN  = 240
TP4_PIPS      = 120
SL_PIPS       = 130
SLIP = 1.0; SLSLIP = 2.0; COMM_PER_100K = 6.0; SPREAD = 1.0
LOOK = 60 * 48


def load():
    ts = []; op = []; hi = []; lo = []
    with open(BASE + "xauusd_m1.csv") as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip().split(",")
            ts.append(datetime.datetime.fromisoformat(t)); op.append(float(o)); hi.append(float(h)); lo.append(float(l))
    pre = [0.0]
    for x in op: pre.append(pre[-1] + x)
    return ts, op, hi, lo, pre


def comm_pips(entry):
    return COMM_PER_100K * (entry * 100 / 100000.0) / 10.0


def run(ts, op, hi, lo, pre, ext_pips):
    sigs = []; last = -10**9
    for i in range(MA_MIN, len(ts) - LOOK):
        if ts[i].hour not in SESSION_HOURS or i - last < COOLDOWN_MIN:
            continue
        mean = (pre[i] - pre[i - MA_MIN]) / MA_MIN
        ext = (op[i] - mean) * 10                       # +ve = above mean
        if abs(ext) < ext_pips:
            continue
        d = 1 if ext < 0 else -1                        # below mean -> BUY toward mean; above -> SELL
        entry = op[i] + d * (SLIP / 10.0)
        tp = entry + d * (TP4_PIPS / 10.0)
        sl = entry - d * (SL_PIPS / 10.0)
        res = None
        for j in range(i, i + LOOK):
            if (lo[j] <= sl) if d == 1 else (hi[j] >= sl): res = "SL"; break
            if (hi[j] >= tp) if d == 1 else (lo[j] <= tp): res = "TP"; break
        if res is None: continue
        pips = (TP4_PIPS if res == "TP" else -(SL_PIPS + SLSLIP)) - (SPREAD + comm_pips(entry))
        sigs.append({"t": ts[i], "dir": "BUY" if d == 1 else "SELL", "res": res, "pips": pips})
        last = i
    return sigs


def main():
    ts, op, hi, lo, pre = load()
    chan = [(datetime.datetime.fromisoformat(c["date"]).replace(tzinfo=None), c["dir"])
            for c in (json.loads(l) for l in open(BASE + "signals.jsonl")) if c["date"][:10] >= "2026-03-01"]
    days = (ts[-1] - ts[0]).days or 1
    print("REPLICA v2 — fade over-extension from 240m mean, Asian session, single TP4")
    print(f"  (TP4 random baseline ≈ {100*SL_PIPS/(TP4_PIPS+SL_PIPS):.0f}%; the channel ≈ 82%)\n")
    print(f"  {'over-ext≥':>9s} {'signals':>7s} {'/day':>5s} {'WIN%':>5s} {'avg pips':>9s} {'total':>7s} {'overlap':>8s}")
    for ext in (0, 40, 60, 80, 100):
        s = run(ts, op, hi, lo, pre, ext)
        if not s: continue
        n = len(s); w = sum(1 for x in s if x["res"] == "TP"); tot = sum(x["pips"] for x in s)
        match = 0
        for x in s:
            if any(abs((x["t"] - ct).total_seconds()) <= 2*3600 and cd == x["dir"] for ct, cd in chan): match += 1
        print(f"  {ext:9d} {n:7d} {n/days:5.2f} {100*w/n:4.0f}% {tot/n:+8.1f} {tot:+7.0f} {100*match/n:6.0f}%")


if __name__ == "__main__":
    main()
