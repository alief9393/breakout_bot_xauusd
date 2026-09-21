#!/usr/bin/env python3
"""
Gate the crypto channel's signals against REAL spot price (Binance spot + OKX fallback).
Model = channel's own method: scale out 1/3 at each TP, move SL to breakeven after TP1,
report in R. We resolve BOTH possible entries so we can see where the edge lives:

  * MARKET entry  — you buy immediately when the signal posts (FOMO).
  * LIMIT  entry  — you place their "entry limit" (the dip) and only enter if price reaches it.

SPOT_ONLY skips SHORTs (can't short on spot). Config at top. Run: ../venv/bin/python backtest.py
"""
import os, json, datetime, collections, statistics
import prices

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNALS = os.path.join(HERE, "signals.jsonl")

HORIZON_HOURS = 336         # max hold (crypto trades run days)
SPOT_ONLY = True            # LONGs only (no shorting on spot)
VERBOSE = True


def _simulate(bars, entry, sl, tps, d):
    """Scale out 1/3 per TP, SL->BE after TP1. Returns (R, outcome, hit) or None."""
    ahead = [tp for tp in tps if (tp > entry if d == 1 else tp < entry)]
    risk = abs(entry - sl)
    if risk <= 0 or not ahead:
        return None
    n = len(ahead); frac = 1.0 / n
    open_idx = list(range(n)); banked = 0.0; hit = 0; stop = sl
    for (t, o, h, l, c) in bars:
        if hit == 0:
            sl_touch = (l <= stop) if d == 1 else (h >= stop)
            if sl_touch:
                return (-1.0, "SL", 0)
            tp_touch = (h >= ahead[0]) if d == 1 else (l <= ahead[0])
            if not tp_touch:
                continue
        while open_idx:
            nxt = ahead[open_idx[0]]
            if (h >= nxt) if d == 1 else (l <= nxt):
                banked += (nxt - entry) * d / risk * frac
                open_idx.pop(0); hit += 1
                if hit == 1:
                    stop = entry
            else:
                break
        if not open_idx:
            return (banked, f"TP{hit}full", hit)
        if hit >= 1 and ((l <= stop) if d == 1 else (h >= stop)):
            return (banked, f"TP{hit}+BE", hit)
    last = bars[-1][4]
    banked += (last - entry) * d / risk * frac * len(open_idx)
    return (banked, f"open@{hit}TP", hit)


def resolve(sig):
    d = 1 if sig["dir"] == "BUY" else -1
    post = datetime.datetime.fromisoformat(sig["date"])
    start_ms = int(post.timestamp() * 1000)
    end_ms = start_ms + HORIZON_HOURS * 3600 * 1000
    base = sig["binance"][:-4] if sig["binance"].endswith("USDT") else sig["binance"]
    try:
        bars, source = prices.get_klines(base, start_ms, end_ms)
    except Exception as e:
        return {"status": "fetch_error", "err": repr(e)[:60]}
    if not bars:
        return {"status": "unverifiable"}

    market_entry = bars[0][1]
    mkt = _simulate(bars, market_entry, sig["sl"], sig["tps"], d)

    lim = None; lim_fill = None
    le = sig.get("entry_limit")
    if le:
        for i, (t, o, h, l, c) in enumerate(bars):        # did price reach the limit?
            if (l <= le) if d == 1 else (h >= le):
                lim_fill = i; break
        if lim_fill is not None:
            lim = _simulate(bars[lim_fill:], le, sig["sl"], sig["tps"], d)
    gap = 100 * (market_entry - sig["entry_market"]) / sig["entry_market"] * d
    return {"status": "ok", "source": source, "gap": gap,
            "mkt": mkt, "lim": lim, "lim_filled": lim_fill is not None}


def _agg(results, key):
    """Aggregate the market or limit leg."""
    vals = []
    for r in results:
        leg = r[key]
        if key == "lim" and not r["lim_filled"]:
            continue                       # limit never triggered -> no trade
        if leg is None:
            continue
        vals.append(leg[0])
    if not vals:
        return None
    wins = sum(1 for v in vals if v > 0.01)
    return dict(n=len(vals), total=sum(vals), avg=sum(vals) / len(vals),
                win=100 * wins / len(vals))


def main():
    sigs = load_signals()
    print(f"resolving {len(sigs)} signals vs real spot (Binance+OKX) · horizon {HORIZON_HOURS}h · spot_only={SPOT_ONLY}\n")
    rows = []; skipped = collections.Counter()
    if VERBOSE:
        print(f"  {'coin':>9} {'dir':>4} | {'MARKET entry':>18} | {'LIMIT (dip) entry':>20} | {'src':>7}")
    for s in sigs:
        if SPOT_ONLY and s["dir"] == "SELL":
            skipped["short(spot)"] += 1; continue
        r = resolve(s)
        if r["status"] != "ok":
            skipped[r["status"]] += 1; continue
        rows.append(r)
        if VERBOSE:
            m = r["mkt"]; L = r["lim"]
            ms = f"{m[1]:>8} {m[0]:>+6.2f}R" if m else "     n/a"
            if not r["lim_filled"]:
                ls = "never dipped"
            elif L:
                ls = f"{L[1]:>8} {L[0]:>+6.2f}R"
            else:
                ls = "n/a"
            print(f"  {s['symbol']:>9} {s['dir']:>4} | {ms:>18} | {ls:>20} | {r.get('source','?'):>7}")

    if not rows:
        print("\nno resolvable signals."); print("skipped:", dict(skipped)); return
    mkt = _agg(rows, "mkt"); lim = _agg(rows, "lim")
    print("\n" + "=" * 64)
    print(f"SPOT long-only · {len(rows)} verifiable signals")
    if mkt:
        print(f"  MARKET entry (FOMO):   {mkt['total']:+6.1f}R total · {mkt['avg']:+.2f}R avg · {mkt['win']:.0f}% win · n={mkt['n']}")
    if lim:
        print(f"  LIMIT entry (dip-buy): {lim['total']:+6.1f}R total · {lim['avg']:+.2f}R avg · {lim['win']:.0f}% win · n={lim['n']} (only trades where price dipped to the limit)")
    if skipped:
        print(f"  skipped: {dict(skipped)}")
    print("=" * 64)


def load_signals():
    return [json.loads(l) for l in open(SIGNALS)]


if __name__ == "__main__":
    main()
