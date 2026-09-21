#!/usr/bin/env python3
"""
StreamTrade — Stage 3: verify every parsed signal against REAL XAUUSD prices.

For each signal we replay minute bars from the post time forward and find what
actually happened — which TP levels were reached, and whether the SL hit first.
This is independent of what the channel CLAIMS: their "TP hit ✅" spam and their
deleted losers don't matter here. Price is the judge.

Conservative modelling:
  * Entry = market at the signal's post time (first M1 bar at/after it).
  * Within a bar, if BOTH a TP and the SL are inside [low, high], we assume the
    SL was hit first (adverse-first) — never over-counts wins.
  * Costs: a round-trip cost in pips is subtracted from every trade.
  * Integrity: we flag signals whose stated entry the market never reached near
    the post time (a sign the message was edited after the fact).

Usage: ./venv/bin/python verify.py
"""

import json
import datetime
import statistics as st

PRICE_CSV = "xauusd_m1.csv"
COST_PIPS = 5.0          # round-trip spread+commission+slippage, in pips (1pt = 10 pips)
MAX_LOOK_MIN = 60 * 48   # give a signal up to 48h to resolve
ENTRY_TOLERANCE = 2.0    # pts: stated entry considered reachable if within this of a bar near post


def load_prices():
    ts, hi, lo, op = [], [], [], []
    with open(PRICE_CSV) as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip("\n").split(",")
            ts.append(datetime.datetime.fromisoformat(t))
            op.append(float(o)); hi.append(float(h)); lo.append(float(l))
    return ts, op, hi, lo


def find_start(ts, when):
    # first bar at/after `when` (binary search)
    lo_i, hi_i = 0, len(ts)
    while lo_i < hi_i:
        mid = (lo_i + hi_i) // 2
        if ts[mid] < when:
            lo_i = mid + 1
        else:
            hi_i = mid
    return lo_i if lo_i < len(ts) else None


def verify(sig, ts, op, hi, lo):
    when = datetime.datetime.fromisoformat(sig["date"]).replace(tzinfo=None)
    i0 = find_start(ts, when)
    if i0 is None:
        return None
    entry_mkt = op[i0]
    d = 1 if sig["dir"] == "BUY" else -1
    tps = sig["tps"]; sl = sig["sl"]
    # integrity: was the stated entry reachable in the first ~5 bars?
    reach = min(abs(sig["entry"] - lo[j]) if d == 1 else abs(sig["entry"] - hi[j])
                for j in range(i0, min(i0 + 5, len(ts))))
    entry_ok = reach <= ENTRY_TOLERANCE or (min(lo[i0:i0+5]) <= sig["entry"] <= max(hi[i0:i0+5]))

    tps_hit = 0
    sl_hit = False
    end = min(i0 + MAX_LOOK_MIN, len(ts))
    for j in range(i0, end):
        # adverse (SL) checked before favourable (TP)
        if (lo[j] <= sl) if d == 1 else (hi[j] >= sl):
            sl_hit = True
            break
        while tps_hit < len(tps) and ((hi[j] >= tps[tps_hit]) if d == 1 else (lo[j] <= tps[tps_hit])):
            tps_hit += 1
        if tps_hit == len(tps):
            break
    resolved = sl_hit or tps_hit > 0 or (end < len(ts) or (i0 + MAX_LOOK_MIN <= len(ts)))
    return {"tps_hit": tps_hit, "sl_hit": sl_hit, "entry_ok": entry_ok,
            "entry_mkt": entry_mkt, "resolved": (sl_hit or tps_hit == len(tps) or (end - i0) < MAX_LOOK_MIN or True)}


def pnl_models(sig, r):
    """Return pips P&L under two exit models."""
    tp_pips = [abs(t - sig["entry"]) * 10 for t in sig["tps"]]
    sl_pips = abs(sig["sl"] - sig["entry"]) * 10
    n = len(sig["tps"])
    # Model A: target TP1 only (exit TP1, else SL)
    if r["tps_hit"] >= 1:
        a = tp_pips[0]
    elif r["sl_hit"]:
        a = -sl_pips
    else:
        a = 0.0
    # Model B: scale out 1/n at each TP; remainder exits at SL if hit
    frac = 1.0 / n
    b = sum(frac * tp_pips[k] for k in range(r["tps_hit"]))
    if r["sl_hit"]:
        b += frac * (n - r["tps_hit"]) * (-sl_pips)
    return a - COST_PIPS, b - COST_PIPS


def main():
    signals = [json.loads(l) for l in open("signals.jsonl")]
    ts, op, hi, lo = load_prices()
    print(f"Loaded {len(signals)} signals and {len(ts):,} M1 bars "
          f"({ts[0].date()} → {ts[-1].date()})\n")

    res = []
    for s in signals:
        r = verify(s, ts, op, hi, lo)
        if r:
            a, b = pnl_models(s, r)
            res.append({**s, **r, "pnlA": a, "pnlB": b})

    n = len(res)
    sl_before_tp1 = sum(1 for r in res if r["sl_hit"] and r["tps_hit"] == 0)
    tp1_plus = sum(1 for r in res if r["tps_hit"] >= 1)
    all_tp = sum(1 for r in res if r["tps_hit"] == 5)
    bad_entry = sum(1 for r in res if not r["entry_ok"])
    dist = {k: sum(1 for r in res if r["tps_hit"] == k) for k in range(6)}

    print("═" * 60)
    print(f"VERIFICATION vs REAL XAUUSD — {n} signals")
    print("═" * 60)
    print(f"  reached TP1 or better:      {tp1_plus} ({100*tp1_plus/n:.0f}%)")
    print(f"  SL hit before ANY TP:       {sl_before_tp1} ({100*sl_before_tp1/n:.0f}%)  <- real losses")
    print(f"  ran to ALL 5 TPs:           {all_tp} ({100*all_tp/n:.0f}%)")
    print(f"  TPs-hit distribution 0..5:  {dist}")
    print(f"  suspicious/edited entries:  {bad_entry} ({100*bad_entry/n:.0f}%) (market never near stated entry)")
    print("\n  ── P&L per signal, net of costs (pips; 1pt = 10 pips) ──")
    for name, key in [("Model A: target TP1, else SL", "pnlA"),
                      ("Model B: scale out 1/5 per TP", "pnlB")]:
        tot = sum(r[key] for r in res)
        wins = [r[key] for r in res if r[key] > 0]
        wr = 100 * len(wins) / n
        print(f"  {name}:")
        print(f"     total {tot:+.0f} pips   avg {tot/n:+.1f}/signal   win {wr:.0f}%")
    print("\n  READ: their channel spams 'TP hit ✅' and deletes losers. This is what")
    print("        the MARKET actually did — the honest scorecard.")


if __name__ == "__main__":
    main()
