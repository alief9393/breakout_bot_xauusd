#!/usr/bin/env python3
"""
StreamTrade — generate a signals.jsonl for ANY channel's messages file.

Reuses parse_signal(), then drops absurd parses (SL > 300 pips is almost surely a
bad parse for scalp gold signals — a leaked number from a messy message).

Usage:
    ./venv/bin/python gen_signals.py messages_Gold_xauusd_pro_signals.jsonl signals_gold_pro.jsonl
"""

import sys
import json
import statistics as st
from parse import parse_signal

MAX_SL_PIPS = 300   # drop parses wider than this (bad parse guard)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "messages.jsonl"
    out = sys.argv[2] if len(sys.argv) > 2 else "signals.jsonl"

    msgs = [json.loads(l) for l in open(src)]
    signals, dropped = [], 0
    for m in msgs:
        s = parse_signal(m["text"])
        if not s:
            continue
        sl_pips = abs(s["sl"] - s["entry"]) * 10
        if not (0 < sl_pips <= MAX_SL_PIPS):
            dropped += 1
            continue
        s["id"] = m["id"]
        s["date"] = m["date"]
        s["tp_pips"] = round(abs(s["tps"][0] - s["entry"]) * 10, 1)
        s["tpN_pips"] = round(abs(s["tps"][-1] - s["entry"]) * 10, 1)
        s["sl_pips"] = round(sl_pips, 1)
        signals.append(s)
    signals.sort(key=lambda x: x["id"])

    with open(out, "w") as f:
        for s in signals:
            f.write(json.dumps(s) + "\n")

    n = len(signals)
    buys = sum(1 for s in signals if s["dir"] == "BUY")
    print(f"{src} -> {out}")
    print(f"  parsed {n} signals ({buys} BUY / {n-buys} SELL), dropped {dropped} absurd parses")
    print(f"  span: {signals[0]['date'][:10]} → {signals[-1]['date'][:10]}")
    print(f"  TP1 dist median: {st.median(s['tp_pips'] for s in signals):.0f} pips")
    print(f"  SL  dist median: {st.median(s['sl_pips'] for s in signals):.0f} pips")


if __name__ == "__main__":
    main()
