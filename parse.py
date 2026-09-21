#!/usr/bin/env python3
"""
StreamTrade — Stage 2: parse the raw messages into structured signals.

A FULL signal looks like:
    XAUUSD BUY NOW 4985
    TP 4988 / TP 4991 / ...
    SL 4975
We extract: direction, entry, all TP levels, SL, and the post timestamp.
Output -> signals.jsonl / signals.csv, ready for price-verification (Stage 3).
"""

import re
import json
import csv
import datetime

NUM = r'(\d{3,5}(?:\.\d+)?)'


def parse_signal(text):
    tu = text.upper()
    # this channel is "XAUUSD Gold Signals" but occasionally posts BTC/other symbols —
    # keep GOLD only, or they pollute the gold backtest (wrong price scale entirely).
    if "XAU" not in tu and "GOLD" not in tu:
        return None
    if any(sym in tu for sym in ("BTC", "ETH", "XRP", "SOL", "DOGE")):
        return None
    m_dir = re.search(r'\b(BUY|SELL)\b', tu)
    if not m_dir:
        return None
    direction = m_dir.group(1)
    # entry = first standalone price in the message (on the direction line)
    m_entry = re.search(NUM, text)
    tps = [float(x) for x in re.findall(r'TP\d*\s*[:\-]?\s*' + NUM, tu)]
    m_sl = re.search(r'\bSL\b\s*[:\-]?\s*' + NUM, tu)
    if not (m_entry and tps and m_sl):
        return None
    entry = float(m_entry.group(1))
    sl = float(m_sl.group(1))
    tps = [t for t in tps if t != entry and t != sl]
    if not tps:
        return None
    # sanity: TP/SL must be on the correct sides of entry
    if direction == "BUY":
        if not (all(t > entry for t in tps) and sl < entry):
            return None
    else:
        if not (all(t < entry for t in tps) and sl > entry):
            return None
    return {"dir": direction, "entry": entry, "tps": sorted(tps, reverse=(direction == "SELL")), "sl": sl}


def main():
    msgs = [json.loads(l) for l in open("messages.jsonl")]
    signals = []
    for m in msgs:
        s = parse_signal(m["text"])
        if s:
            s["id"] = m["id"]
            s["date"] = m["date"]
            s["tp_pips"] = round(abs(s["tps"][0] - s["entry"]) * 10, 1)      # nearest TP, 1pt=10pips
            s["tpN_pips"] = round(abs(s["tps"][-1] - s["entry"]) * 10, 1)    # furthest TP
            s["sl_pips"] = round(abs(s["sl"] - s["entry"]) * 10, 1)
            signals.append(s)
    signals.sort(key=lambda x: x["id"])

    with open("signals.jsonl", "w") as f:
        for s in signals:
            f.write(json.dumps(s) + "\n")
    with open("signals.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "date", "dir", "entry", "sl", "tps", "tp_pips", "tpN_pips", "sl_pips"])
        w.writeheader()
        for s in signals:
            w.writerow({**s, "tps": "|".join(str(t) for t in s["tps"])})

    n = len(signals)
    buys = sum(1 for s in signals if s["dir"] == "BUY")
    import statistics as st
    print(f"Parsed {n} signals  ({buys} BUY / {n-buys} SELL)")
    print(f"  span: {signals[0]['date'][:10]} → {signals[-1]['date'][:10]}")
    print(f"  TP levels per signal: {st.mode([len(s['tps']) for s in signals])} (typical)")
    print(f"  nearest-TP distance: median {st.median(s['tp_pips'] for s in signals):.0f} pips")
    print(f"  SL distance:         median {st.median(s['sl_pips'] for s in signals):.0f} pips")
    rr = st.median((s["tpN_pips"] / s["sl_pips"]) for s in signals if s["sl_pips"])
    print(f"  furthest-TP : SL ratio (median): {rr:.2f}")
    print(f"\nSaved {n} signals to signals.jsonl / signals.csv")
    print("Next (Stage 3): pull real XAUUSD price data and check which hit first — TP or SL.")


if __name__ == "__main__":
    main()
