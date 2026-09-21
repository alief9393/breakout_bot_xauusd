#!/usr/bin/env python3
"""
StreamTrade RESEARCH — deep feature mining for a >=75% TP4 morning fade.

Method (anti-overfitting from the start):
  1. Generate MORNING fade candidates (Asian session only).
  2. Compute a rich, mechanistically-sensible feature set at each.
  3. Label each with the TP4 outcome (TP4 before SL, from a market entry).
  4. Split TRAIN (earlier) / TEST (later) chronologically.
  5. For each feature, find the threshold that best lifts TRAIN win-rate while
     keeping frequency up, then REPORT TEST win-rate + frequency.
  A rule only counts if it holds on TEST too. We report honestly either way.
"""

import json
import datetime
import statistics as st

BASE = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/"

MORNING_HOURS = {0, 1, 2, 3}     # the channel's 100%-morning window (UTC); 07-10 JKT
MOVE_MIN      = 15               # minimum 30-min move to consider a fade (broad pool)
COOLDOWN_MIN  = 60
TP4_PIPS      = 120
SL_PIPS       = 130
SLSLIP        = 2.0
LOOK          = 60 * 48
SPLIT_DATE    = "2026-07-01"     # train < this, test >= this
MIN_PER_DAY   = 0.7              # a rule must keep at least ~0.7 signals/day to qualify


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


def daily_refs(ts, op, hi, lo):
    """session open (00:00 UTC price) and prior-day high/low, per calendar day."""
    day_open = {}; day_hi = {}; day_lo = {}
    for i in range(len(ts)):
        d = ts[i].date()
        if d not in day_open: day_open[d] = op[i]
        day_hi[d] = max(day_hi.get(d, -1e9), hi[i])
        day_lo[d] = min(day_lo.get(d, 1e9), lo[i])
    return day_open, day_hi, day_lo


def build(ts, op, hi, lo, pre):
    day_open, day_hi, day_lo = daily_refs(ts, op, hi, lo)
    rows = []; last = -10**9
    for i in range(240, len(ts) - LOOK):
        if ts[i].hour not in MORNING_HOURS or i - last < COOLDOWN_MIN:
            continue
        mv30 = (op[i] - op[i - 30]) * 10
        if abs(mv30) < MOVE_MIN:
            continue
        d = -1 if mv30 > 0 else 1                       # fade
        entry = op[i] + d * 0.1
        # ── features (aligned so larger = "more extreme fade", hypothesised better) ──
        mean240 = (pre[i] - pre[i - 240]) / 240
        ext240 = (mean240 - entry) * d * 10             # >0 = stretched away from mean (room to revert)
        so = day_open.get(ts[i].date(), entry)
        sess_ext = (so - entry) * d * 10                # >0 = below/above session open in fade-favor dir
        hi240 = max(hi[i - 240:i]); lo240 = min(lo[i - 240:i]); rng = (hi240 - lo240) or 1e-9
        # extremeness within the 240m range in the fade direction (1 = at the extreme we fade)
        rpos = (entry - lo240) / rng if d == -1 else (hi240 - entry) / rng
        yday = ts[i].date() - datetime.timedelta(days=1)
        pdh = day_hi.get(yday); pdl = day_lo.get(yday)
        # distance to the relevant prior-day level we might be fading into (pips; smaller=closer)
        lvl_dist = None
        if d == -1 and pdh: lvl_dist = abs(entry - pdh) * 10
        if d == 1 and pdl:  lvl_dist = abs(entry - pdl) * 10
        atr30 = sum(hi[k] - lo[k] for k in range(i - 30, i)) / 30 * 10
        consec = 0                                       # consecutive bars in the move's direction just before i
        md = 1 if mv30 > 0 else -1
        for k in range(i - 1, i - 20, -1):
            if (op[k + 1] - op[k]) * md > 0: consec += 1
            else: break
        # ── label: TP4 before SL ──
        tp = entry + d * TP4_PIPS / 10; sl = entry - d * SL_PIPS / 10
        res = None
        for j in range(i, i + LOOK):
            if (lo[j] <= sl) if d == 1 else (hi[j] >= sl): res = 0; break
            if (hi[j] >= tp) if d == 1 else (lo[j] <= tp): res = 1; break
        if res is None: continue
        rows.append({"date": ts[i].date().isoformat(), "hour": ts[i].hour, "win": res,
                     "mv30": abs(mv30), "ext240": ext240, "sess_ext": sess_ext,
                     "rpos": rpos, "lvl_dist": lvl_dist if lvl_dist is not None else 9999,
                     "atr30": atr30, "consec": consec, "d": d})
        last = i
    return rows


def rate(rows):
    return (100 * sum(r["win"] for r in rows) / len(rows), len(rows)) if rows else (0, 0)


def main():
    ts, op, hi, lo, pre = load()
    rows = build(ts, op, hi, lo, pre)
    train = [r for r in rows if r["date"] < SPLIT_DATE]
    test = [r for r in rows if r["date"] >= SPLIT_DATE]
    tr_days = len(set(r["date"] for r in train)); te_days = len(set(r["date"] for r in test))
    bw, bn = rate(train); tw, tn = rate(test)
    print("═" * 66)
    print("DEEP MINE — morning fade, TP4, train/test")
    print("═" * 66)
    print(f"  morning hours {sorted(MORNING_HOURS)} UTC · pool: {len(rows)} candidates")
    print(f"  TRAIN (<{SPLIT_DATE}): {bn} cand, {bw:.0f}% TP4 win, {bn/max(tr_days,1):.2f}/day")
    print(f"  TEST  (>={SPLIT_DATE}): {tn} cand, {tw:.0f}% TP4 win, {tn/max(te_days,1):.2f}/day")
    print(f"\n  single-feature filters (threshold learned on TRAIN, reported on both):")
    print(f"  {'feature/rule':22s} {'TRAIN win% (/day)':>20s} {'TEST win% (/day)':>18s}")

    feats = ["mv30", "ext240", "sess_ext", "rpos", "atr30", "consec"]
    for f in feats:
        best = None
        vals = sorted(set(r[f] for r in train))
        # try keeping the TOP portion (feature >= thr) and BOTTOM (feature <= thr)
        for thr in vals:
            for side in (">=", "<="):
                sub = [r for r in train if (r[f] >= thr if side == ">=" else r[f] <= thr)]
                if not sub or len(sub) / max(tr_days, 1) < MIN_PER_DAY:
                    continue
                w, n = rate(sub)
                if best is None or w > best[0]:
                    best = (w, n, thr, side)
        if not best: continue
        w, n, thr, side = best
        te = [r for r in test if (r[f] >= thr if side == ">=" else r[f] <= thr)]
        tew, ten = rate(te)
        lbl = f"{f} {side} {thr:.1f}"
        print(f"  {lbl:22s} {w:6.0f}% ({n/max(tr_days,1):.2f})     {tew:6.0f}% ({ten/max(te_days,1):.2f})")

    # lvl_dist handled separately (smaller = nearer a prior-day level)
    for thr in (10, 20, 30, 50, 80):
        sub = [r for r in train if r["lvl_dist"] <= thr]
        if not sub or len(sub)/max(tr_days,1) < 0.2: continue
        w, n = rate(sub); te = [r for r in test if r["lvl_dist"] <= thr]; tew, ten = rate(te)
        print(f"  {'lvl_dist <= '+str(thr):22s} {w:6.0f}% ({n/max(tr_days,1):.2f})     {tew:6.0f}% ({ten/max(te_days,1):.2f})")


if __name__ == "__main__":
    main()
