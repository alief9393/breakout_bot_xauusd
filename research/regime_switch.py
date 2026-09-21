#!/usr/bin/env python3
"""
RESEARCH — regime switching: detect trend vs chop, use the right strategy for each.
  * TREND regime (trailing efficiency-ratio high): trend-follow — long above MA100, trail 2R (let run)
  * CHOP  regime (ER low): mean-revert — fade the short-MA, quick +1R target, -1R stop
Compare vs pure trend-following. 1/day, 2% risk, real costs, full range.
"""
import sys, collections
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

b.PRICE_CSV = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/xauusd_m1_ext.csv"
ts, op, hi, lo = b.load_prices()
PIP = 0.1
SL_PIPS = 150; R = SL_PIPS * PIP
HORIZON = 5 * 1440
RISK = 0.02
MA_DAYS = 100
ER_WIN = 20          # trailing window for the trend/chop detector
ER_THRESH = 0.25     # ER below this = choppy regime
MR_MA = 5            # short MA for mean-reversion direction
TREND_TRAIL = 2.0
SPREAD_PIPS = 1.0; COMM = 6.0

# daily reference price + entry bar at hour 1
day_price = collections.OrderedDict(); hour_bar = {}
for i in range(len(ts) - HORIZON):
    t = ts[i]; dk = t.date()
    if dk not in day_price:
        day_price[dk] = op[i]
    if t.hour == 1 and dk not in hour_bar:
        hour_bar[dk] = i
days = list(day_price.keys()); dpx = list(day_price.values())


def comm_pips(e):
    return COMM * (e * b.CONTRACT_OZ / 100000.0) / b.PIP_VALUE_PER_LOT


def er(px):
    if len(px) < 2:
        return 0.0
    net = abs(px[-1] - px[0]); path = sum(abs(px[i] - px[i - 1]) for i in range(1, len(px)))
    return net / path if path else 0.0


def trend_R(i0, d):                      # let winners run (trail)
    entry = op[i0]; stop = entry - d * R; ext = entry; end = min(i0 + HORIZON, len(ts))
    for j in range(i0 + 1, end):
        if d == 1:
            if lo[j] <= stop: return (stop - entry) / R
            ext = max(ext, hi[j]); stop = max(stop, ext - TREND_TRAIL * R)
        else:
            if hi[j] >= stop: return (entry - stop) / R
            ext = min(ext, lo[j]); stop = min(stop, ext + TREND_TRAIL * R)
    return (op[end - 1] - entry) * d / R


def mr_R(i0, d, tp_mult=1.0):            # mean-reversion: quick +tp_mult R, -1R stop
    entry = op[i0]; stop = entry - d * R; tp = entry + d * tp_mult * R; end = min(i0 + HORIZON, len(ts))
    for j in range(i0 + 1, end):
        if d == 1:
            if lo[j] <= stop: return -1.0
            if hi[j] >= tp: return tp_mult
        else:
            if hi[j] >= stop: return -1.0
            if lo[j] <= tp: return tp_mult
    return (op[end - 1] - entry) * d / R


def build(mode):
    """mode: 'trend_only' or 'regime'. Returns list of (i0, R_net, ym)."""
    out = []
    for k in range(MA_DAYS, len(days)):
        dk = days[k]; i0 = hour_bar.get(dk)
        if i0 is None:
            continue
        ma = sum(dpx[k - MA_DAYS:k]) / MA_DAYS
        e = er(dpx[k - ER_WIN:k])
        px = dpx[k]
        if mode == "trend_only" or e >= ER_THRESH:
            d = 1 if px > ma else -1
            Rr = trend_R(i0, d)
        else:                             # chop -> mean reversion: fade the short MA
            mshort = sum(dpx[k - MR_MA:k]) / MR_MA
            d = 1 if px < mshort else -1  # below short MA -> expect bounce (long); above -> fade (short)
            Rr = mr_R(i0, d)
        Rr -= (SPREAD_PIPS + comm_pips(op[i0])) / SL_PIPS
        out.append((i0, Rr, ts[i0].strftime("%Y-%m")))
    return out


def report(name, trades):
    bal = 100.0; peak = bal; mdd = 0.0; mon = collections.OrderedDict(); wins = 0
    for i0, Rr, ym in trades:
        wins += Rr > 0
        if ym not in mon: mon[ym] = [bal, bal]
        bal *= (1 + Rr * RISK); mon[ym][1] = bal
        peak = max(peak, bal); mdd = min(mdd, bal / peak - 1)
    negs = sum(1 for s, e in mon.values() if e < s)
    print(f"{name:14} {bal/100:>9,.1f}x  maxDD {100*mdd:>4.0f}%  win {100*wins/len(trades):>3.0f}%  "
          f"neg-months {negs}/{len(mon)}")
    return mon


if __name__ == "__main__":
    print(f"regime switch vs trend-only · 1/day · {RISK*100:.0f}% risk · ER_win {ER_WIN} thresh {ER_THRESH}\n")
    to = report("trend-only", build("trend_only"))
    rg = report("regime-switch", build("regime"))
    print("\n  per-month (trend-only -> regime):")
    for ym in to:
        ts_, te = to[ym]; rs, re = rg[ym]
        tr = te / ts_ - 1; rr = re / rs - 1
        flag = " <-- fixed" if tr < 0 <= rr else (" <-- broke" if rr < 0 <= tr else "")
        if tr < 0 or rr < 0:
            print(f"    {ym}: trend {100*tr:>+5.0f}%  regime {100*rr:>+5.0f}%{flag}")
