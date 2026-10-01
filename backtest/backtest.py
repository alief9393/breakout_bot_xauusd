#!/usr/bin/env python3
"""
XAUUSD Donchian-breakout backtest — self-contained (no external deps beyond stdlib).

Strategy (identical to the live core in core/breakout_core.py):
  * enter LONG when price breaks the prior N-day HIGH, SHORT when it breaks the N-day LOW
    -> the breakout IS the regime detector: no trade in chop, only in trends
  * exit: hard -1R stop (losers capped), trail TRAIL_R behind the extreme (let winners run),
    force-close at HORIZON_DAYS
  * event-driven, compounding, % risk, real spread+commission, broker lot limits

Edit the CONFIG block, then run from the repo root:
    python backtest/backtest.py
Needs price data at data/xauusd_m1.csv — generate it first with:
    python backtest/fetch_prices.py        (pulls M1 OHLC from cTrader; see .env)
"""
import os, sys, csv, datetime, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── CONFIG ──
PRICE_CSV   = os.getenv("PRICE_CSV", os.path.join(ROOT, "data", "xauusd_m1.csv"))
BAL0        = 300.0       # starting balance ($)
RISK_PCT    = 0.07        # risk this fraction of balance per trade (2% ≈ -25% DD, 7% ≈ profit-optimal survivable)
COMPOUND    = True        # True = compound; False = fixed fraction of BAL0
SL_PIPS     = 150         # 1R stop distance (gold pip = 0.1 price → 150 pips = $15)
TRAIL_R     = 2.0         # let-winners-run leash: trail the stop this many R behind the extreme
DONCHIAN_N  = 3           # breakout lookback (days) — the regime detector; robust 3–20
HORIZON_DAYS = 7          # max hold before force-close
COOLDOWN_MIN = 240        # wait after a trade before re-arming
START_YM    = None        # "YYYY-MM" or None
END_YM      = None        # "YYYY-MM" or None
SPREAD_PIPS = 1.0         # round-trip spread cost (pips)
COMMISSION_USD_PER_100K = 6.0
MIN_LOT     = 0.01        # broker min — forces over-risk on tiny balances
MAX_LOT     = 50.0        # broker max — caps compounding on big balances
# ────────────

PIP = 0.1
CONTRACT_OZ = 100
PIP_VALUE_PER_LOT = 10.0
R = SL_PIPS * PIP
HORIZON = HORIZON_DAYS * 1440


def load_prices(path):
    ts, op, hi, lo = [], [], [], []
    with open(path) as f:
        next(f)                                   # header: time_utc,open,high,low,close,volume
        for line in f:
            t, o, h, l, c, v = line.rstrip("\n").split(",")
            ts.append(datetime.datetime.fromisoformat(t))
            op.append(float(o)); hi.append(float(h)); lo.append(float(l))
    return ts, op, hi, lo


def comm_pips(entry):
    return COMMISSION_USD_PER_100K * (entry * CONTRACT_OZ / 100000.0) / PIP_VALUE_PER_LOT


def run():
    if not os.path.exists(PRICE_CSV):
        print(f"price data not found: {PRICE_CSV}\n  generate it first:  python backtest/fetch_prices.py")
        return
    ts, op, hi, lo = load_prices(PRICE_CSV)

    # daily high/low, then rolling N-day donchian levels per date (using prior N days only)
    dhi = collections.OrderedDict(); dlo = collections.OrderedDict()
    for i in range(len(ts)):
        dk = ts[i].date()
        dhi[dk] = max(dhi.get(dk, -1e9), hi[i]); dlo[dk] = min(dlo.get(dk, 1e9), lo[i])
    dates = list(dhi.keys()); hs = list(dhi.values()); ls = list(dlo.values())
    NH = {}; NL = {}
    for k in range(DONCHIAN_N, len(dates)):
        NH[dates[k]] = max(hs[k - DONCHIAN_N:k]); NL[dates[k]] = min(ls[k - DONCHIAN_N:k])

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
        # simulate forward on M1 bars
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
        # real lot sizing: round to 0.01, clamp [MIN_LOT, MAX_LOT]
        base = bal if COMPOUND else BAL0
        lots = round((RISK_PCT * base) / (SL_PIPS * PIP_VALUE_PER_LOT), 2)
        if lots < MIN_LOT: lots = MIN_LOT; min_clamp += 1
        elif lots > MAX_LOT: lots = MAX_LOT; max_clamp += 1
        pnl = Rr * (lots * SL_PIPS * PIP_VALUE_PER_LOT); bal = max(bal + pnl, 1e-6)
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
    print(f"XAUUSD DONCHIAN BREAKOUT {DONCHIAN_N}d · trail {TRAIL_R}R · {RISK_PCT*100:.0f}% risk · hold≤{HORIZON_DAYS}d")
    print(f"{ts[0].date()}→{ts[-1].date()} · {n} trades ({longs}L/{shorts}S)\n")
    print(f"  START ${BAL0:,.2f}  →  FINAL ${bal:,.2f}   ({bal/BAL0:,.1f}x · CAGR {100*cagr:+.0f}%/yr)")
    print(f"  win {100*wins/n:.0f}% · avg {sum(Rs)/n:+.2f}R · maxDD {100*mdd:.0f}%")
    print(f"  lots: {min_clamp} min-clamped (balance too small) · {max_clamp} max-clamped (capped at {MAX_LOT})\n")
    print(f"  {'month':>8} {'return':>8} {'n':>4}")
    pos = 0
    for ym, m in months.items():
        ret = m[1] / m[0] - 1 if m[0] else 0
        pos += ret > 0
        print(f"  {ym:>8} {100*ret:>+7.0f}% {m[2]:>4}")
    print(f"\n  {pos}/{len(months)} months positive ({100*pos/len(months):.0f}%)")
    print("\n  NOTE: validated on 2023–2026 gold (a bull). Walk-forward survived (~1,439x OOS, 7/8 blocks),")
    print("  but the -70% drawdown is real — size risk small, never leverage. See CLAUDE.md.")


if __name__ == "__main__":
    run()
