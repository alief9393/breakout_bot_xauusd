#!/usr/bin/env python3
"""
StreamTrade — shared paper-portfolio state for live_validate.py + backfill.py.

One small JSON file so the running W/L record, equity curve, and the set of
already-processed signal ids survive restarts AND are shared between the live
bot and the backfill pass (so a signal is never double-counted).
"""

import os
import json

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validate_state.json")


def load(balance0):
    """Load state, or initialise a fresh portfolio at balance0."""
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        s["processed_ids"] = set(s.get("processed_ids", []))
        return s
    except Exception:
        return {"equity": balance0, "peak": balance0, "max_dd": 0.0,
                "wins": 0, "losses": 0, "processed_ids": set()}


def save(s):
    """Persist state atomically (write temp then replace) so a crash mid-write can't corrupt it."""
    out = dict(s)
    out["processed_ids"] = sorted(s["processed_ids"])
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def apply_pnl(s, pnl):
    """Fold one trade's $ P&L into the portfolio (equity, peak, drawdown, W/L)."""
    s["equity"] += pnl
    s["peak"] = max(s["peak"], s["equity"])
    s["max_dd"] = min(s["max_dd"], s["equity"] / s["peak"] - 1 if s["peak"] else 0.0)
    if pnl > 0:
        s["wins"] += 1
    else:
        s["losses"] += 1
    return s
