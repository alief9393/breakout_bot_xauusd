#!/usr/bin/env python3
"""
StreamTrade RESEARCH — characterize the channel's mean-reversion edge precisely.

Goal: pin down the exact rules behind their ~10pp edge above random, so we can
replicate it (see replica.py). Reads the parent folder's signals + M1 prices;
writes nothing outside research/. Safe to run alongside the live demo.

We measure, per signal:
  * pre-move over several lookbacks (which timeframe defines their fade?)
  * the trigger threshold (how big a move before they fire?)
  * time-of-day (is the edge session-specific?)
  * level context (round numbers, distance from the day's high/low)
  * does a bigger prior move => higher win rate?
"""

import json
import datetime
import statistics as st
import collections

BASE = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/"


def load():
    ts = []; op = []; hi = []; lo = []
    with open(BASE + "xauusd_m1.csv") as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip().split(",")
            ts.append(datetime.datetime.fromisoformat(t)); op.append(float(o)); hi.append(float(h)); lo.append(float(l))
    sigs = [json.loads(l) for l in open(BASE + "signals.jsonl") if json.loads(l)["date"][:10] >= "2026-03-01"]
    return ts, op, hi, lo, sigs


def find(ts, when):
    a, b = 0, len(ts)
    while a < b:
        m = (a + b) // 2
        if ts[m] < when: a = m + 1
        else: b = m
    return a


def real_win_tp4(s, ts, op, hi, lo, i0):
    """did it reach TP4 (from stated entry) before SL? (their outcome)"""
    d = 1 if s["dir"] == "BUY" else -1
    tps = s["tps"][:4]; sl = s["sl"]
    if len(tps) < 4: return None
    tgt = tps[3]; reached = False
    for j in range(i0, min(i0 + 60 * 48, len(ts))):
        if (lo[j] <= sl) if d == 1 else (hi[j] >= sl): return False
        if (hi[j] >= tgt) if d == 1 else (lo[j] <= tgt): return True
    return False


def main():
    ts, op, hi, lo, sigs = load()
    rows = []
    for s in sigs:
        when = datetime.datetime.fromisoformat(s["date"]).replace(tzinfo=None)
        i0 = find(ts, when)
        if i0 < 120 or i0 >= len(ts) - 10: continue
        d = 1 if s["dir"] == "BUY" else -1
        rows.append({"s": s, "i0": i0, "d": d, "when": when})

    print(f"signals: {len(rows)}\n")

    # 1) which lookback defines the fade?
    print("── pre-move by lookback (aligned to signal dir; negative = they FADE) ──")
    for w in (15, 30, 60, 120):
        al = [(op[r["i0"]] - op[r["i0"] - w]) * 10 * r["d"] for r in rows]
        rev = sum(1 for x in al if x < 0)
        print(f"  {w:3d}min: median aligned {st.median(al):+5.0f}p   fade {100*rev/len(al):.0f}%   |move| median {st.median(abs(x) for x in al):.0f}p")

    # 2) trigger threshold (magnitude of the 30-min move they fade)
    mags = sorted(abs((op[r["i0"]] - op[r["i0"] - 30]) * 10) for r in rows)
    print(f"\n── 30-min move magnitude at signal (the 'how big before they fade') ──")
    for p in (10, 25, 50, 75, 90):
        print(f"  p{p}: {mags[int(p/100*len(mags))]:.0f} pips")

    # 3) time-of-day
    print(f"\n── signal hour (UTC) ──")
    hrs = collections.Counter(r["when"].hour for r in rows)
    for h in sorted(hrs):
        print(f"  {h:02d}:00 UTC ({(h+7)%24:02d} JKT): {'#'*hrs[h]} {hrs[h]}")

    # 4) level context: round-number proximity + distance from day extreme
    rnd10 = sum(1 for r in rows if min(op[r["i0"]] % 10, 10 - op[r["i0"]] % 10) <= 1.0)
    print(f"\n── level context ──")
    print(f"  entries within $1 of a round $10 level: {rnd10} ({100*rnd10/len(rows):.0f}%)")

    # 5) does a bigger prior move => higher TP4 win rate?
    print(f"\n── does a bigger fade => better outcome? (TP4 win by 30-min move size) ──")
    buckets = {"<20p": [], "20-40p": [], "40-60p": [], "60p+": []}
    for r in rows:
        mv = abs((op[r["i0"]] - op[r["i0"] - 30]) * 10)
        win = real_win_tp4(r["s"], ts, op, hi, lo, r["i0"])
        if win is None: continue
        k = "<20p" if mv < 20 else "20-40p" if mv < 40 else "40-60p" if mv < 60 else "60p+"
        buckets[k].append(win)
    for k, v in buckets.items():
        if v: print(f"  {k:8s}: {100*sum(v)/len(v):.0f}% TP4-win  (n={len(v)})")


if __name__ == "__main__":
    main()
