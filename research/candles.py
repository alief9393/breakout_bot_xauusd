#!/usr/bin/env python3
"""
StreamTrade RESEARCH — is the channel's entry a CANDLE pattern?

Builds M15 & H1 candles from M1, then at each signal checks the last COMPLETED
candle before entry for reversal signatures aligned to their direction:
  * pin/rejection wick (BUY after long LOWER wick; SELL after long UPPER wick)
  * prior candle opposite (BUY after a red candle; SELL after a green) = they fade it
  * engulfing in their direction
  * entry at a swing extreme (near the last N-candle low for BUY / high for SELL)
Compares their prevalence to a random baseline. If a signature shows up far more
often than random, that's their setup.
"""

import json
import datetime
import random
random.seed(11)

BASE = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/"


def load_m1():
    ts = []; o = []; h = []; l = []; c = []
    with open(BASE + "xauusd_m1.csv") as f:
        next(f)
        for line in f:
            t, oo, hh, ll, cc, v = line.rstrip().split(",")
            ts.append(datetime.datetime.fromisoformat(t)); o.append(float(oo)); h.append(float(hh)); l.append(float(ll)); c.append(float(cc))
    return ts, o, h, l, c


def aggregate(ts, o, h, l, c, minutes):
    """M1 -> higher timeframe candles keyed by bucket start time."""
    bars = {}
    for i in range(len(ts)):
        b = ts[i].replace(minute=(ts[i].minute // minutes) * minutes if minutes < 60 else 0,
                          second=0, microsecond=0)
        if minutes >= 60:
            b = ts[i].replace(minute=0, second=0, microsecond=0)
        if b not in bars:
            bars[b] = [o[i], h[i], l[i], c[i]]
        else:
            bk = bars[b]; bk[1] = max(bk[1], h[i]); bk[2] = min(bk[2], l[i]); bk[3] = c[i]
    keys = sorted(bars)
    return keys, bars


def sig_of(candle):
    o, h, l, c = candle
    body = abs(c - o); rng = (h - l) or 1e-9
    upper = h - max(o, c); lower = min(o, c) - l
    return {"green": c > o, "body": body, "upper": upper, "lower": lower,
            "hammer": lower >= 2 * body and lower >= upper,        # long lower wick
            "star": upper >= 2 * body and upper >= lower}          # long upper wick


def prev_candle_idx(keys, when, minutes):
    # last completed candle strictly before `when`
    lo, hi = 0, len(keys)
    while lo < hi:
        m = (lo + hi) // 2
        end = keys[m] + datetime.timedelta(minutes=minutes)
        if end <= when: lo = m + 1
        else: hi = m
    return lo - 1


def analyze(tag, entries, keys, bars, minutes):
    """entries: list of (when, dir) — dir 1=BUY, -1=SELL"""
    pin = opp = eng = swing = n = 0
    K = 20
    for when, d in entries:
        idx = prev_candle_idx(keys, when, minutes)
        if idx < K: continue
        n += 1
        cur = sig_of(bars[keys[idx]]); prv = sig_of(bars[keys[idx - 1]])
        # aligned pin: BUY wants hammer (lower wick), SELL wants star (upper wick)
        if (cur["hammer"] if d == 1 else cur["star"]): pin += 1
        # prior candle opposite to their trade (they fade it): BUY after red / SELL after green
        if (not cur["green"]) if d == 1 else cur["green"]: opp += 1
        # engulfing in their direction
        o0, h0, l0, c0 = bars[keys[idx]]; o1, h1, l1, c1 = bars[keys[idx - 1]]
        if d == 1 and c0 > o0 and o0 <= c1 and c0 >= o1: eng += 1
        if d == -1 and c0 < o0 and o0 >= c1 and c0 <= o1: eng += 1
        # swing extreme: entry near the last-K candle low (BUY) / high (SELL)
        lows = min(bars[keys[k]][2] for k in range(idx - K, idx + 1))
        highs = max(bars[keys[k]][1] for k in range(idx - K, idx + 1))
        px = bars[keys[idx]][3]
        if (abs(px - lows) * 10 <= 20) if d == 1 else (abs(highs - px) * 10 <= 20): swing += 1
    if not n: return
    print(f"  {tag:16s} n={n:4d}  pin {100*pin/n:3.0f}%  fade-prev {100*opp/n:3.0f}%  engulf {100*eng/n:3.0f}%  at-swing {100*swing/n:3.0f}%")


def main():
    ts, o, h, l, c = load_m1()
    sigs = [json.loads(x) for x in open(BASE + "signals.jsonl") if json.loads(x)["date"][:10] >= "2026-01-01"]
    their = [(datetime.datetime.fromisoformat(s["date"]).replace(tzinfo=None), 1 if s["dir"] == "BUY" else -1) for s in sigs]
    # random baseline: same count, random times (morning), random dir
    randoms = []
    while len(randoms) < 1500:
        i = random.randint(0, len(ts) - 1)
        if ts[i].hour in {0, 1, 2, 3}:
            randoms.append((ts[i], random.choice([1, -1])))

    for mins, name in ((15, "M15"), (60, "H1")):
        keys, bars = aggregate(ts, o, h, l, c, mins)
        print(f"\n── {name} candle before entry — signature prevalence ──")
        analyze(f"CHANNEL {name}", their, keys, bars, mins)
        analyze(f"random {name}", randoms, keys, bars, mins)


if __name__ == "__main__":
    main()
