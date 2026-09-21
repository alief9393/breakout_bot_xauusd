#!/usr/bin/env python3
"""
StreamTrade — CHANNEL HONESTY AUDIT.

Scans every signal in a channel's message history, then checks the channel's own
"TP HIT ✅" claims against what the REAL cTrader price actually did.

For each signal we compute, from real M1 prices and the channel's OWN stated levels:
  * real_tps  = how many TPs the price genuinely reached before hitting the SL
  * sl_hit    = did price hit the stated SL (a real loss/stop)
and compare to what the channel POSTED:
  * claimed_tps = the highest "TP{n} HIT" (or "ALL HIT") it announced
  * admitted_sl = did it ever admit the stop

Then it flags:
  * FABRICATED  — claimed more TPs than price actually reached (e.g. Sep 1's fake TP3)
  * HIDDEN LOSS — price hit SL but the channel never admitted it

Usage:  ./venv/bin/python honesty_audit.py            (defaults to messages.jsonl)
        ./venv/bin/python honesty_audit.py messages.jsonl
"""

import re
import sys
import json
import datetime
from parse import parse_signal

MSG_FILE   = sys.argv[1] if len(sys.argv) > 1 else "messages.jsonl"
PRICE_CSV  = "xauusd_m1.csv"
LOOK_MIN   = 60 * 48
START_DATE = "2026-03-01"     # skip partial Feb / pre-price-data


def load_prices():
    ts, hi, lo = [], [], []
    with open(PRICE_CSV) as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip("\n").split(",")
            ts.append(datetime.datetime.fromisoformat(t)); hi.append(float(h)); lo.append(float(l))
    return ts, hi, lo


def find(ts, when):
    a, b = 0, len(ts)
    while a < b:
        m = (a + b) // 2
        if ts[m] < when: a = m + 1
        else: b = m
    return a if a < len(ts) else None


def real_outcome(sig, ts, hi, lo):
    """From the channel's OWN stated entry/TPs/SL: how many TPs did price really reach
    before the SL, and did the SL get hit? (adverse-first within each bar)."""
    when = datetime.datetime.fromisoformat(sig["date"]).replace(tzinfo=None)
    i0 = find(ts, when)
    if i0 is None:
        return None
    d = 1 if sig["dir"] == "BUY" else -1
    tps, sl = sig["tps"], sig["sl"]
    reached = 0; sl_hit = False
    for j in range(i0, min(i0 + LOOK_MIN, len(ts))):
        if (lo[j] <= sl) if d == 1 else (hi[j] >= sl):
            sl_hit = True; break
        while reached < len(tps) and ((hi[j] >= tps[reached]) if d == 1 else (lo[j] <= tps[reached])):
            reached += 1
        if reached == len(tps):
            break
    return reached, sl_hit


# ── parse the channel's CLAIM messages ──
CLAIM_TP  = re.compile(r'TP\s*(\d+)\s*HIT', re.I)
CLAIM_ALL = re.compile(r'TP\s*ALL\s*HIT|ALL\s*TP.*HIT', re.I)
SL_ADMIT  = re.compile(r'\bSL\b.*(part of trading|hit)|stop\s*loss\s*hit', re.I)


def main():
    ts, hi, lo = load_prices()
    msgs = sorted((json.loads(l) for l in open(MSG_FILE)), key=lambda m: m["id"])

    # walk messages: track the current open signal per direction, attach claims to it
    signals = []           # each: dict with sig fields + claimed_tp + admitted_sl
    cur = {"BUY": None, "SELL": None}
    for m in msgs:
        text = m["text"] or ""
        sig = parse_signal(text)
        if sig and (m["date"] or "")[:10] >= START_DATE:
            rec = {**sig, "date": m["date"], "id": m["id"],
                   "claimed_tp": 0, "claimed_all": False, "admitted_sl": False}
            signals.append(rec)
            cur[sig["dir"]] = rec
            continue
        # claim messages reference "GOLD BUY ..." / "GOLD SELL ..."
        tu = text.upper()
        d = "BUY" if "BUY" in tu else ("SELL" if "SELL" in tu else None)
        tgt = cur.get(d) if d else None
        if tgt is None:
            continue
        if CLAIM_ALL.search(text):
            tgt["claimed_all"] = True
            tgt["claimed_tp"] = max(tgt["claimed_tp"], len(tgt["tps"]))
        mt = CLAIM_TP.search(text)
        if mt:
            tgt["claimed_tp"] = max(tgt["claimed_tp"], int(mt.group(1)))
        if SL_ADMIT.search(text):
            tgt["admitted_sl"] = True

    # verify each signal against real price
    audited = fab = hidden = clean = 0
    real_win = real_sl = 0
    claimed_tp_sum = real_tp_sum = 0
    examples = []
    for s in signals:
        out = real_outcome(s, ts, hi, lo)
        if out is None:
            continue
        real_tps, sl_hit = out
        audited += 1
        real_win += real_tps >= 1
        real_sl  += sl_hit
        claimed = len(s["tps"]) if s["claimed_all"] else s["claimed_tp"]
        claimed_tp_sum += claimed; real_tp_sum += real_tps
        is_fab = claimed > real_tps
        is_hidden = sl_hit and not s["admitted_sl"] and claimed >= 1
        if is_fab: fab += 1
        if is_hidden: hidden += 1
        if not is_fab and not is_hidden: clean += 1
        if is_fab or is_hidden:
            examples.append((s["date"][:16], s["dir"], claimed, real_tps, sl_hit,
                             s["admitted_sl"], is_fab, is_hidden))

    print("═" * 66)
    print(f"CHANNEL HONESTY AUDIT — {MSG_FILE}")
    print("═" * 66)
    print(f"  signals audited (real price): {audited}   from {START_DATE}")
    print(f"\n  REAL price outcomes (using the channel's OWN levels):")
    print(f"    hit TP1+ before SL: {real_win} ({100*real_win/audited:.0f}%)")
    print(f"    hit the stated SL:  {real_sl} ({100*real_sl/audited:.0f}%)")
    print(f"    avg TPs really reached: {real_tp_sum/audited:.2f}")
    print(f"\n  CHANNEL's claims:")
    print(f"    avg TPs claimed hit:    {claimed_tp_sum/audited:.2f}")
    print(f"\n  HONESTY:")
    print(f"    ✅ claim matches price: {clean} ({100*clean/audited:.0f}%)")
    print(f"    ❌ FABRICATED TPs (claimed > real): {fab} ({100*fab/audited:.0f}%)")
    print(f"    ❌ HIDDEN loss (SL hit, never admitted): {hidden} ({100*hidden/audited:.0f}%)")
    print(f"\n  worst offenders (up to 15):")
    print(f"    {'date':16s} {'dir':4s} {'claimed':>7s} {'real':>4s} {'SL?':>4s} {'admit?':>6s}  flags")
    for d, dr, cl, rl, slh, adm, isf, ish in examples[:15]:
        fl = ("FABRICATED " if isf else "") + ("HIDDEN-SL" if ish else "")
        print(f"    {d:16s} {dr:4s} {cl:7d} {rl:4d} {str(slh):>4s} {str(adm):>6s}  {fl}")


if __name__ == "__main__":
    main()
