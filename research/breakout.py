#!/usr/bin/env python3
"""
RESEARCH — Donchian breakout entry (the entry IS the regime detector) + let-winners-run exit.
  * enter LONG when price breaks the prior N-day HIGH, SHORT when it breaks the N-day LOW
  * exit: hard -1R stop, trail TRAIL_R behind the extreme (ride the trend), timeout at HORIZON
  * event-driven (trades only fire on a breakout -> flat in chop), compounding, % risk, real costs

Keeps mm_trend.py separate. Edit CONFIG, run: ./venv/bin/python research/breakout.py
"""
import sys, collections
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

b.PRICE_CSV = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/xauusd_m1_ext.csv"
ts, op, hi, lo = b.load_prices()
PIP = 0.1

# ── CONFIG ──
BAL0        = 300.0
RISK_PCT    = 0.07        # 5% = profit-optimal survivable (207x @ -51% DD); try 0.02 for -25% DD
COMPOUND    = True        # True = compound; False = fixed-fraction of BAL0
SL_PIPS     = 150         # 1R
TRAIL_R     = 2.0         # let-winners-run leash
DONCHIAN_N  = 3          # breakout lookback (days) — the regime detector; shorter = earlier/noisier
HORIZON_DAYS = 7          # max hold
COOLDOWN_MIN = 240        # wait after a trade before re-arming
START_YM    = "2026-01"        # "YYYY-MM" or None
END_YM      = "2026-08"
SPREAD_PIPS = 1.0
COMMISSION_USD_PER_100K = 6.0
MIN_LOT     = 0.01        # broker min — forces over-risk on small balances
MAX_LOT     = 50.0        # broker max — caps compounding on big balances
# ────────────

R = SL_PIPS * PIP
HORIZON = HORIZON_DAYS * 1440


def comm_pips(e):
    return COMMISSION_USD_PER_100K * (e * b.CONTRACT_OZ / 100000.0) / b.PIP_VALUE_PER_LOT


# daily high/low, then rolling N-day donchian levels per date (using prior N days)
dhi = collections.OrderedDict(); dlo = collections.OrderedDict()
for i in range(len(ts)):
    dk = ts[i].date()
    dhi[dk] = max(dhi.get(dk, -1e9), hi[i])
    dlo[dk] = min(dlo.get(dk, 1e9), lo[i])
dates = list(dhi.keys())
NH = {}; NL = {}
hs = list(dhi.values()); ls = list(dlo.values())
for k in range(DONCHIAN_N, len(dates)):
    NH[dates[k]] = max(hs[k - DONCHIAN_N:k])
    NL[dates[k]] = min(ls[k - DONCHIAN_N:k])


def run():
    bal = BAL0; peak = bal; mdd = 0.0
    months = collections.OrderedDict(); Rs = []; longs = shorts = 0
    min_clamp = max_clamp = 0
    i = 0
    while i < len(ts) - 1:
        dk = ts[i].date()
        nh = NH.get(dk); nl = NL.get(dk)
        if nh is None:
            i += 1; continue
        d = 0
        if hi[i] >= nh:
            d = 1; entry = max(op[i], nh)
        elif lo[i] <= nl:
            d = -1; entry = min(op[i], nl)
        if d == 0:
            i += 1; continue
        ym = ts[i].strftime("%Y-%m")
        if (START_YM and ym < START_YM) or (END_YM and ym > END_YM):
            i += 1; continue
        # simulate
        stop = entry - d * R; ext = entry; exit_i = None; Rr = None
        for j in range(i + 1, min(i + HORIZON, len(ts))):
            if d == 1:
                if lo[j] <= stop: Rr = (stop - entry) / R; exit_i = j; break
                ext = max(ext, hi[j]); stop = max(stop, ext - TRAIL_R * R)
            else:
                if hi[j] >= stop: Rr = (entry - stop) / R; exit_i = j; break
                ext = min(ext, lo[j]); stop = min(stop, ext + TRAIL_R * R)
        if exit_i is None:
            exit_i = min(i + HORIZON, len(ts)) - 1
            Rr = (op[exit_i] - entry) * d / R
        Rr -= (SPREAD_PIPS + comm_pips(entry)) / SL_PIPS
        Rs.append(Rr); longs += d == 1; shorts += d == -1
        # real lot sizing: round to 0.01, clamp [MIN_LOT, MAX_LOT], recompute actual risk
        base = bal if COMPOUND else BAL0
        raw_lots = (RISK_PCT * base) / (SL_PIPS * b.PIP_VALUE_PER_LOT)
        lots = round(raw_lots, 2)
        if lots < MIN_LOT: lots = MIN_LOT; min_clamp += 1
        elif lots > MAX_LOT: lots = MAX_LOT; max_clamp += 1
        risk_usd = lots * SL_PIPS * b.PIP_VALUE_PER_LOT     # actual $ risked this trade
        pnl = Rr * risk_usd; bal = max(bal + pnl, 1e-6)
        peak = max(peak, bal); mdd = min(mdd, bal / peak - 1)
        m = months.get(ym)
        if m is None: m = months[ym] = [bal - pnl, bal, 0]
        m[1] = bal; m[2] += 1
        i = exit_i + COOLDOWN_MIN

    if not Rs:
        print("no trades."); return
    n = len(Rs); wins = sum(1 for r in Rs if r > 0)
    days = (ts[-1] - ts[0]).days or 1
    cagr = (bal / BAL0) ** (365.0 / days) - 1
    print(f"DONCHIAN BREAKOUT {DONCHIAN_N}d · trail {TRAIL_R}R · {RISK_PCT*100:.0f}% risk · hold≤{HORIZON_DAYS}d")
    print(f"{ts[0].date()}→{ts[-1].date()} · {n} trades ({longs}L/{shorts}S)\n")
    print(f"  START ${BAL0:,.2f}  →  FINAL ${bal:,.2f}   ({bal/BAL0:,.1f}x · CAGR {100*cagr:+.0f}%/yr)")
    print(f"  win {100*wins/n:.0f}% · avg {sum(Rs)/n:+.2f}R · maxDD {100*mdd:.0f}%")
    print(f"  lots: {min_clamp} min-clamped (over-risked, bal too small) · {max_clamp} max-clamped (capped at {MAX_LOT} lot)\n")
    print(f"  {'month':>8} {'return':>8} {'n':>4}")
    pos = 0
    for ym, m in months.items():
        ret = m[1] / m[0] - 1 if m[0] else 0
        pos += ret > 0
        print(f"  {ym:>8} {100*ret:>+7.0f}% {m[2]:>4}")
    print(f"\n  {pos}/{len(months)} months positive ({100*pos/len(months):.0f}%)")


if __name__ == "__main__":
    run()
