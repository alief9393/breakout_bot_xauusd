#!/usr/bin/env python3
"""
StreamTrade RESEARCH — find the SELECTOR: what do the channel's entries have that
random Asian-session bars don't? We compare, at their entries vs random bars:
  * over-extension from moving averages (60m, 240m) in the fade direction
  * distance from the day's high/low (are they fading at extremes?)
The features where THEIR entries differ from random = their selection edge.
"""

import json
import datetime
import random
import statistics as st

BASE = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/"
random.seed(7)


def load():
    ts = []; op = []
    with open(BASE + "xauusd_m1.csv") as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip().split(",")
            ts.append(datetime.datetime.fromisoformat(t)); op.append(float(o))
    # prefix sums for fast MAs
    pre = [0.0]
    for x in op:
        pre.append(pre[-1] + x)
    return ts, op, pre


def ma(pre, i, n):
    if i - n < 0: return None
    return (pre[i] - pre[i - n]) / n


def find(ts, when):
    a, b = 0, len(ts)
    while a < b:
        m = (a + b) // 2
        if ts[m] < when: a = m + 1
        else: b = m
    return a


def features(op, pre, i, d):
    """aligned over-extension (pips): positive = price stretched in the direction we're fading
       (i.e. for a fade, price is far above MA when we SELL / far below when we BUY)."""
    f = {}
    for n in (60, 240):
        m = ma(pre, i, n)
        if m is None:
            f[f"ma{n}"] = None
        else:
            # d = fade dir (BUY=+1 means price dropped -> below MA -> (op-m) negative -> *(-d)? )
            # we want "how over-extended AGAINST our trade": for a BUY fade, price is BELOW mean => (m-op)>0
            f[f"ma{n}"] = (m - op[i]) * d * 10   # >0 = price is stretched away from mean, favoring reversion
    return f


def main():
    ts, op, pre = load()
    sigs = [json.loads(l) for l in open(BASE + "signals.jsonl") if json.loads(l)["date"][:10] >= "2026-03-01"]
    their = {"ma60": [], "ma240": []}
    for s in sigs:
        i = find(ts, datetime.datetime.fromisoformat(s["date"]).replace(tzinfo=None))
        if i < 240 or i >= len(ts) - 10: continue
        d = 1 if s["dir"] == "BUY" else -1
        f = features(op, pre, i, d)
        for k in their:
            if f[k] is not None: their[k].append(f[k])

    # random Asian-session bars, random fade direction
    rnd = {"ma60": [], "ma240": []}
    hrs = {22, 23, 0, 1, 2, 3}
    got = 0
    while got < 2000:
        i = random.randint(240, len(ts) - 10)
        if ts[i].hour not in hrs: continue
        d = random.choice([1, -1])
        f = features(op, pre, i, d)
        if f["ma240"] is not None:
            for k in rnd: rnd[k].append(f[k])
            got += 1

    print("over-extension from mean, aligned so >0 = price stretched AWAY (favoring a fade)")
    print("(pips; compare THEIR entries vs random Asian-session bars)\n")
    print(f"  {'feature':8s} {'THEIR median':>13s} {'random median':>14s} {'THEIR>0 %':>10s} {'rand>0 %':>9s}")
    for k in ("ma60", "ma240"):
        tm = st.median(their[k]); rm = st.median(rnd[k])
        tp = 100 * sum(1 for x in their[k] if x > 0) / len(their[k])
        rp = 100 * sum(1 for x in rnd[k] if x > 0) / len(rnd[k])
        print(f"  {k:8s} {tm:+13.0f} {rm:+14.0f} {tp:9.0f}% {rp:8.0f}%")
    print("\n  if THEIR median >> random median, they specifically fade OVER-EXTENDED price")
    print("  (that would be the selector to add to replica.py)")


if __name__ == "__main__":
    main()
