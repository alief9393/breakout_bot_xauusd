#!/usr/bin/env python3
"""
Bidirectional TREND-FOLLOWING + the winning money management.
  * direction: price above its MA -> LONG ; below -> SHORT   (trades both ways)
  * exit: hard -1R stop, let winners run on a wide TRAIL_R trailing stop
  * compounding, % risk, real costs, per-month + per-year, date-range selectable

This tests whether the edge is real trend-capture (works both directions) or just
long-gold-in-a-bull. Edit CONFIG, run: ./venv/bin/python research/mm_trend.py
"""
import sys, collections
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

# ═══════════════════════════════════════════════════════════════════════════
BAL0        = 100.0
RISK_PCT    = 0.05        # 2% risk per trade (compounding)
COMPOUND    = True
SL_PIPS     = 150         # 1R
TRAIL_R     = 2.0         # let-winners-run leash
MA_DAYS     = 100         # trend filter: MA of the last N daily reference prices
MODE        = "long_short"  # "long_short" (trade both ways) or "long_flat" (long above MA, sit out below)
ENTRY_HOURS = [1,9,17]  # UTC hours to enter each day — [1] = 1/day, [1,9,17] = 3/day
HORIZON_DAYS = 7
START_YM    = "2026-01"        # "YYYY-MM" or None
END_YM      = "2026-08"
SPREAD_PIPS = 1.0
COMMISSION_USD_PER_100K = 6.0
MIN_LOT     = 0.01        # broker min — forces over-risk on small balances
MAX_LOT     = 50.0        # broker max — caps compounding on big balances
# ═══════════════════════════════════════════════════════════════════════════

b.PRICE_CSV = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/xauusd_m1_ext.csv"
ts, op, hi, lo = b.load_prices()
PIP = 0.1
HORIZON = HORIZON_DAYS * 1440

# daily reference price (first bar of each day) drives the trend MA;
# entry bars are looked up per (day, hour) so intraday frequency doesn't change the MA meaning
_day_first = collections.OrderedDict()
_hour_bar = {}
for i in range(len(ts) - HORIZON):
    t = ts[i]; dkey = t.date()
    if dkey not in _day_first:
        _day_first[dkey] = op[i]
    if t.hour in ENTRY_HOURS and (dkey, t.hour) not in _hour_bar:
        _hour_bar[(dkey, t.hour)] = i
_days = list(_day_first.keys())
_dprices = list(_day_first.values())


def comm_pips(entry):
    return COMMISSION_USD_PER_100K * (entry * b.CONTRACT_OZ / 100000.0) / b.PIP_VALUE_PER_LOT


def trade_R(i0, d):
    """d=+1 long, -1 short. Hard -1R stop; trail TRAIL_R behind the favorable extreme."""
    entry = op[i0]; R = SL_PIPS * PIP
    stop = entry - d * R
    ext = entry
    end = min(i0 + HORIZON, len(ts))
    for j in range(i0 + 1, end):
        if d == 1:
            if lo[j] <= stop:
                return (stop - entry) / R
            ext = max(ext, hi[j]); stop = max(stop, ext - TRAIL_R * R)
        else:
            if hi[j] >= stop:
                return (entry - stop) / R
            ext = min(ext, lo[j]); stop = min(stop, ext + TRAIL_R * R)
    return (op[end - 1] - entry) * d / R


def run():
    # daily trend direction from the MA of prior MA_DAYS daily reference prices;
    # take every ENTRY_HOURS entry that day in that direction
    trades = []
    for k in range(MA_DAYS, len(_days)):
        dkey = _days[k]
        ym = dkey.strftime("%Y-%m")
        if START_YM and ym < START_YM:
            continue
        if END_YM and ym > END_YM:
            continue
        ma = sum(_dprices[k - MA_DAYS:k]) / MA_DAYS
        up = _dprices[k] > ma
        if MODE == "long_flat" and not up:      # sit out downtrends; no shorts
            continue
        d = 1 if up else -1
        for hr in ENTRY_HOURS:
            i0 = _hour_bar.get((dkey, hr))
            if i0 is not None:
                trades.append((i0, d))
    trades.sort()                                # chronological for compounding
    if not trades:
        print("no trades in range."); return

    bal = BAL0; peak = bal; mdd = 0.0
    months = collections.OrderedDict(); Rs = []; longs = shorts = 0
    min_clamp = max_clamp = 0
    for i0, d in trades:
        longs += d == 1; shorts += d == -1
        grossR = trade_R(i0, d)
        netR = grossR - (SPREAD_PIPS + comm_pips(op[i0])) / SL_PIPS
        Rs.append(netR)
        # real lot sizing: round to 0.01, clamp [MIN_LOT, MAX_LOT], recompute actual risk
        base = bal if COMPOUND else BAL0
        lots = round((RISK_PCT * base) / (SL_PIPS * b.PIP_VALUE_PER_LOT), 2)
        if lots < MIN_LOT: lots = MIN_LOT; min_clamp += 1
        elif lots > MAX_LOT: lots = MAX_LOT; max_clamp += 1
        pnl = netR * lots * SL_PIPS * b.PIP_VALUE_PER_LOT
        bal += pnl
        if bal <= 0:
            bal = 0.01
        peak = max(peak, bal); mdd = min(mdd, bal / peak - 1)
        ym = ts[i0].strftime("%Y-%m")
        m = months.get(ym)
        if m is None:
            m = months[ym] = {"start": bal - pnl, "end": bal, "w": 0, "l": 0, "n": 0}
        m["end"] = bal; m["n"] += 1; m["w"] += netR > 0; m["l"] += netR <= 0

    n = len(Rs); wins = sum(1 for r in Rs if r > 0)
    days = (ts[trades[-1][0]] - ts[trades[0][0]]).days or 1
    cagr = (bal / BAL0) ** (365.0 / days) - 1
    print(f"TREND-FOLLOW (long+short) · MA{MA_DAYS} · cut -1R/trail {TRAIL_R}R · {RISK_PCT*100:.0f}% risk")
    print(f"cost ~{SPREAD_PIPS+comm_pips(op[trades[0][0]]):.1f}p RT · {ts[trades[0][0]].date()}→{ts[trades[-1][0]].date()} · {n} trades ({longs}L/{shorts}S)\n")
    print(f"  START ${BAL0:,.2f}  →  FINAL ${bal:,.2f}   ({bal/BAL0:.1f}x · CAGR {100*cagr:+.0f}%/yr)")
    print(f"  win {100*wins/n:.0f}% · avg {sum(Rs)/n:+.2f}R · maxDD {100*mdd:.0f}%")
    print(f"  lots: {min_clamp} min-clamped (over-risked, bal too small) · {max_clamp} max-clamped (capped at {MAX_LOT} lot)\n")
    print(f"  {'month':>8} {'start':>12} {'end':>12} {'return':>8} {'win%':>5} {'n':>4}")
    pos = 0
    for ym, m in months.items():
        ret = m["end"] / m["start"] - 1 if m["start"] else 0
        pos += ret > 0
        print(f"  {ym:>8} ${m['start']:>11,.2f} ${m['end']:>11,.2f} {100*ret:>+7.0f}% {100*m['w']/m['n'] if m['n'] else 0:>4.0f}% {m['n']:>4}")
    print(f"\n  {pos}/{len(months)} months positive ({100*pos/len(months):.0f}%)")


if __name__ == "__main__":
    run()
