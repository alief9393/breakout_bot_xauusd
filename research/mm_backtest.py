#!/usr/bin/env python3
"""
Money-management engine: cut losers at a hard 1R stop, let winners run on a wide
TRAIL_R trailing stop. Compounding, % risk, real costs. Edit CONFIG, run:
  ./venv/bin/python research/mm_backtest.py

Entry (fixed): one LONG per day at ENTRY_HOUR on gold — we're testing the EXIT/sizing.
"""
import sys, collections
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG — edit here
# ═══════════════════════════════════════════════════════════════════════════
BAL0         = 100.0     # starting balance
RISK_PCT     = 0.02       # risk per trade as % of CURRENT balance (compounding)
COMPOUND     = True       # True = compound; False = fixed-fraction of BAL0 (additive)

SL_PIPS      = 150        # 1R = hard stop distance (pips). losers capped at -1R.
TRAIL_R      = 2.0        # trail the stop this many R behind the high-water (let winners run)

ENTRY_HOUR   = 1          # one long/day at this UTC hour
HORIZON_DAYS = 3          # max hold per trade

# date range (inclusive) — "YYYY-MM" strings, or None for all available
START_YM     = None       # e.g. "2024-01"
END_YM       = None       # e.g. "2025-12"

# real costs (set to your Exness numbers) — round-turn
SPREAD_PIPS         = 1.0    # gold spread in pips (Exness raw ~0.1-0.5; standard ~1-2)
COMMISSION_USD_PER_100K = 6.0  # round-turn commission per $100k notional (Exness zero-comm -> 0)
# ═══════════════════════════════════════════════════════════════════════════

b.PRICE_CSV = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/xauusd_m1_ext.csv"
ts, op, hi, lo = b.load_prices()
PIP = 0.1
HORIZON = HORIZON_DAYS * 1440

entries = []
_seen = set()
for i in range(len(ts) - HORIZON):
    t = ts[i]
    if t.hour == ENTRY_HOUR and t.date() not in _seen:
        ym = t.strftime("%Y-%m")
        if START_YM and ym < START_YM:
            continue
        if END_YM and ym > END_YM:
            continue
        entries.append(i); _seen.add(t.date())


def comm_pips(entry):
    return COMMISSION_USD_PER_100K * (entry * b.CONTRACT_OZ / 100000.0) / b.PIP_VALUE_PER_LOT


def trade_R(i0):
    """LONG from op[i0]. Hard stop -1R; trail TRAIL_R behind high-water once in profit. Returns gross R."""
    entry = op[i0]; R = SL_PIPS * PIP
    stop = entry - R          # hard 1R stop
    hw = entry
    for j in range(i0 + 1, min(i0 + HORIZON, len(ts))):
        if lo[j] <= stop:
            return (stop - entry) / R
        hw = max(hw, hi[j])
        stop = max(stop, hw - TRAIL_R * R)   # trail up only; wide leash
    return (op[min(i0 + HORIZON, len(ts)) - 1] - entry) / R


def run():
    if not entries:
        print("no trades in the selected range."); return
    bal = BAL0; peak = bal; mdd = 0.0
    months = collections.OrderedDict()     # "YYYY-MM" -> stats
    Rs = []
    for i0 in entries:
        grossR = trade_R(i0)
        netR = grossR - (SPREAD_PIPS + comm_pips(op[i0])) / SL_PIPS   # cost in R
        Rs.append(netR)
        pnl = netR * RISK_PCT * (bal if COMPOUND else BAL0)
        bal += pnl
        if bal <= 0:
            bal = 0.01
        peak = max(peak, bal); mdd = min(mdd, bal / peak - 1)
        ym = ts[i0].strftime("%Y-%m")
        m = months.get(ym)
        if m is None:
            m = months[ym] = {"start": bal - pnl, "end": bal, "w": 0, "l": 0, "Rsum": 0.0, "n": 0}
        m["end"] = bal; m["Rsum"] += netR; m["n"] += 1
        m["w"] += netR > 0; m["l"] += netR <= 0
    n = len(Rs); wins = sum(1 for r in Rs if r > 0)
    days = (ts[entries[-1]] - ts[entries[0]]).days or 1
    cagr = (bal / BAL0) ** (365.0 / days) - 1

    rng = f"{ts[entries[0]].date()}→{ts[entries[-1]].date()}"
    print(f"MM ENGINE · cut -1R / trail {TRAIL_R}R · {'COMPOUND' if COMPOUND else 'FIXED'} {RISK_PCT*100:.1f}% risk · 1R={SL_PIPS}p")
    print(f"cost: {SPREAD_PIPS}p spread + {COMMISSION_USD_PER_100K}/100k comm  (~{SPREAD_PIPS+comm_pips(op[entries[0]]):.1f}p round-turn)")
    print(f"range {rng} · {n} trades (1 long/day @ {ENTRY_HOUR}:00)\n")
    print(f"  START:  ${BAL0:,.2f}")
    print(f"  FINAL:  ${bal:,.2f}   ({bal/BAL0:.1f}x · CAGR {100*cagr:+.0f}%/yr)")
    print(f"  win rate {100*wins/n:.0f}% · avg {sum(Rs)/n:+.2f}R/trade · max drawdown {100*mdd:.0f}%\n")
    print(f"  {'month':>8} {'start':>12} {'end':>12} {'return':>8} {'Rsum':>7} {'win%':>5} {'n':>4}")
    pos = 0
    for ym, m in months.items():
        ret = m["end"] / m["start"] - 1 if m["start"] else 0
        pos += ret > 0
        print(f"  {ym:>8} ${m['start']:>11,.2f} ${m['end']:>11,.2f} {100*ret:>+7.0f}% {m['Rsum']:>+7.0f} {100*m['w']/m['n'] if m['n'] else 0:>4.0f}% {m['n']:>4}")
    print(f"\n  {pos}/{len(months)} months positive ({100*pos/len(months):.0f}%)")


if __name__ == "__main__":
    run()
