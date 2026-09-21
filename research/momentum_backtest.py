#!/usr/bin/env python3
"""
StreamTrade RESEARCH — MOMENTUM backtest engine (our own strategy).

Strategy: in the morning session, when gold has moved >= MOVE_MIN pips over the last
LOOKBACK_MIN minutes, enter WITH the move (momentum), targeting a fixed TP, ~130p SL.
Enter at MARKET (open of the signal bar + slippage) — the achievable, realistic basis.

This sweeps TP1..TP5 and reports which exit makes the most money (pips, $ equity,
profit factor, drawdown). Edit the CONFIG block and re-run to experiment.

    ./venv/bin/python research/momentum_backtest.py
"""

import json
import datetime
import collections

BASE = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/"

# ══════════════════════════ CONFIG — EDIT THESE ══════════════════════════
SESSION_HOURS  = {0, 1, 2, 3}      # UTC morning window (07-10 JKT). set() = all hours
MOVE_MIN       = 15                # min move (pips) over LOOKBACK to trigger
LOOKBACK_MIN   = 30                # window to measure the move
COOLDOWN_MIN   = 120               # min gap between signals (120=~2/day, 240=~1/day)
DIRECTION      = "momo"            # "momo" = follow the move; "fade" = against it
SL_PIPS        = 130               # stop (their structure)
TP_LADDER      = {1: 30, 2: 60, 3: 90, 4: 120, 5: 150}   # exits to compare (pips)

START          = "2026-01-01"      # data actually begins 2026-01-29
END            = "2026-09-07"
START_BALANCE  = 100.0
RISK_PCT       = 0.10              # risk/trade for the $ equity curve
MAX_LOT        = 100.0
MIN_LOT        = 0.01

# costs (IC Markets cTrader XAUUSD) — pips
SPREAD_PIPS      = 1.0
ENTRY_SLIP_PIPS  = 1.0
SL_SLIP_PIPS     = 2.0
COMMISSION_USD_PER_100K = 6.0      # $6 round-turn per $100k notional (price-based)
# ══════════════════════════════════════════════════════════════════════════

CONTRACT_OZ = 100
PIP_VALUE_PER_LOT = 10.0
LOOK = 60 * 48


def load():
    ts = []; op = []; hi = []; lo = []
    with open(BASE + "xauusd_m1.csv") as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip().split(",")
            ts.append(datetime.datetime.fromisoformat(t)); op.append(float(o)); hi.append(float(h)); lo.append(float(l))
    return ts, op, hi, lo


def comm_pips(entry):
    return COMMISSION_USD_PER_100K * (entry * CONTRACT_OZ / 100000.0) / PIP_VALUE_PER_LOT


def gen_signals(ts, op, hi, lo):
    out = []; last = -10**9
    for i in range(LOOKBACK_MIN, len(ts) - LOOK):
        if (SESSION_HOURS and ts[i].hour not in SESSION_HOURS) or i - last < COOLDOWN_MIN:
            continue
        ds = ts[i].date().isoformat()
        if ds < START or ds > END:
            continue
        mv = (op[i] - op[i - LOOKBACK_MIN]) * 10
        if abs(mv) < MOVE_MIN:
            continue
        sgn = 1 if mv > 0 else -1
        d = sgn if DIRECTION == "momo" else -sgn
        out.append({"i": i, "t": ts[i], "d": d, "entry": op[i] + d * ENTRY_SLIP_PIPS / 10})
        last = i
    return out


def resolve(sig, ts, op, hi, lo, tp_pips):
    d = sig["d"]; entry = sig["entry"]; i = sig["i"]
    tp = entry + d * tp_pips / 10; sl = entry - d * SL_PIPS / 10
    res = None
    for j in range(i, i + LOOK):
        if (lo[j] <= sl) if d == 1 else (hi[j] >= sl): res = "SL"; break
        if (hi[j] >= tp) if d == 1 else (lo[j] <= tp): res = "TP"; break
    gross = tp_pips if res == "TP" else -(SL_PIPS + SL_SLIP_PIPS) if res == "SL" else 0.0
    return res, gross - (SPREAD_PIPS + comm_pips(entry))


def evaluate(sigs, ts, op, hi, lo, tp_pips):
    eq = START_BALANCE; peak = eq; ddp = 0.0
    cum = 0.0; peakp = 0.0; maxdd_pips = 0.0
    wins = 0; gw = gl = 0.0; pips = []
    monthly = collections.defaultdict(float)
    for s in sigs:
        res, p = resolve(s, ts, op, hi, lo, tp_pips)
        pips.append(p); wins += res == "TP"
        if p > 0: gw += p
        else: gl += -p
        cum += p; peakp = max(peakp, cum); maxdd_pips = min(maxdd_pips, cum - peakp)
        monthly[s["t"].strftime("%Y-%m")] += p
        # $ equity at risk
        sl_dist = SL_PIPS / 10.0
        lots = max(MIN_LOT, min(round((eq * RISK_PCT) / (sl_dist * CONTRACT_OZ), 2), MAX_LOT))
        eq += p * PIP_VALUE_PER_LOT * lots
        peak = max(peak, eq); ddp = min(ddp, eq / peak - 1)
    n = len(pips); days = len(set(s["t"].date() for s in sigs)) or 1
    return {"n": n, "perday": n / days, "win": 100 * wins / n if n else 0,
            "avg": sum(pips) / n if n else 0, "total": sum(pips),
            "pf": gw / gl if gl else 99, "maxdd_pips": maxdd_pips,
            "equity": eq, "eq_dd": 100 * ddp, "monthly": monthly}


def main():
    ts, op, hi, lo = load()
    sigs = gen_signals(ts, op, hi, lo)
    print("═" * 74)
    print(f"MOMENTUM BACKTEST — {DIRECTION}, morning {sorted(SESSION_HOURS)} UTC, {START}→{END}")
    print("═" * 74)
    print(f"  trigger: move>={MOVE_MIN}p/{LOOKBACK_MIN}min, cooldown {COOLDOWN_MIN}m, SL {SL_PIPS}p")
    print(f"  signals: {len(sigs)}  ({len(sigs)/(len(set(s['t'].date() for s in sigs)) or 1):.2f}/day)")
    print(f"  ${START_BALANCE:.0f} start · {RISK_PCT*100:.0f}% risk/trade · real cTrader costs\n")
    print(f"  {'exit':5s} {'win%':>5s} {'avg pips':>9s} {'total p':>8s} {'PF':>5s} {'DD(pips)':>9s} {'$ equity':>10s} {'$ maxDD':>8s}")
    results = []
    for tpn, tpp in TP_LADDER.items():
        m = evaluate(sigs, ts, op, hi, lo, tpp)
        results.append((tpn, tpp, m))
        print(f"  TP{tpn:<3d} {m['win']:4.0f}% {m['avg']:+8.1f} {m['total']:+8.0f} {m['pf']:5.2f} {m['maxdd_pips']:9.0f} {m['equity']:10,.0f} {m['eq_dd']:7.0f}%")

    best = max(results, key=lambda r: r[2]["total"])
    bestpf = max(results, key=lambda r: r[2]["pf"])
    print(f"\n  most PROFIT (pips):   TP{best[0]}  (+{best[2]['total']:.0f}p, ${best[2]['equity']:,.0f}, PF {best[2]['pf']:.2f})")
    print(f"  best PROFIT-FACTOR:   TP{bestpf[0]}  (PF {bestpf[2]['pf']:.2f}, +{bestpf[2]['total']:.0f}p)")
    print(f"\n  month-by-month (TP{best[0]}, pips):")
    for k in sorted(best[2]["monthly"]):
        print(f"    {k}: {best[2]['monthly'][k]:+.0f}")


if __name__ == "__main__":
    main()
