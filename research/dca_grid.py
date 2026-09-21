#!/usr/bin/env python3
"""
RESEARCH — DCA grid (the user's money-management idea), tested honestly on 3y gold M1.

Strategy:
  * dip entry: go LONG when price is ENTRY_ATR_MULT x ATR below the recent high (a real dip)
  * DCA: add another unit every DCA_STEP_PIPS lower, up to MAX_ADDS extra units
  * take profit: close the WHOLE basket when price returns to the FIRST entry (avg is below it -> profit)
  * hard stop: wide SL below the first entry; if hit, close the (now-large) basket for a big loss

Judge on: expectancy, WORST single-basket loss, and MAX DRAWDOWN per year — not win rate.
A DCA grid ALWAYS shows a high win rate; the tail is the whole story.
"""
import sys, collections
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

b.PRICE_CSV = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/xauusd_m1_ext.csv"
ts, op, hi, lo = b.load_prices()
PIP = 0.1                       # gold: 1 pip = 0.1 price

# ── params ──
ENTRY_ATR_MULT = 1.0           # enter when price is this many ATRs below the recent high
ATR_WIN        = 120           # bars for ATR + recent-high lookback
DCA_STEP_PIPS  = 40            # add a unit every this many pips lower
MAX_ADDS       = 3             # max extra units (basket max = 1 + MAX_ADDS)
TP_PIPS        = 30            # profit target above first entry when NO dca has triggered
SL_PIPS        = 300           # wide hard stop below the FIRST entry
LOT_PER_UNIT   = 0.10          # $/pip = 1.0 per unit (0.1 lot); fixed size per unit
COOLDOWN_MIN   = 120           # min gap between baskets
BAL0           = 10000.0


def atr(i):
    if i < ATR_WIN:
        return None
    rng = [hi[k] - lo[k] for k in range(i - ATR_WIN, i)]
    return sum(rng) / len(rng)


def run():
    eq = BAL0; peak = eq
    per_year = collections.defaultdict(lambda: {"pnl": 0.0, "w": 0, "l": 0, "maxdd": 0.0, "peak": None})
    trades = []
    i = ATR_WIN + 1
    last_close_i = -10**9
    dollars_per_pip = LOT_PER_UNIT * 10          # 0.1 lot -> $1/pip

    while i < len(ts) - 1:
        if i - last_close_i < COOLDOWN_MIN:
            i += 1; continue
        a = atr(i)
        if a is None:
            i += 1; continue
        recent_high = max(op[i - ATR_WIN:i])
        # dip entry: price fell ENTRY_ATR_MULT*ATR below the recent high
        if op[i] > recent_high - ENTRY_ATR_MULT * a:
            i += 1; continue

        first = op[i]
        fills = [first]
        hard_sl = first - SL_PIPS * PIP
        next_dca = first - DCA_STEP_PIPS * PIP
        adds = 0
        j = i + 1
        closed = None
        while j < len(ts):
            L, H = lo[j], hi[j]
            if L <= hard_sl:                       # hard stop: whole basket dies at the wide SL
                avg = sum(fills) / len(fills)
                pnl = (hard_sl - avg) * len(fills) / PIP * dollars_per_pip
                closed = ("SL", pnl, len(fills), j); break
            if adds < MAX_ADDS and L <= next_dca:  # add a unit lower
                fills.append(next_dca); adds += 1; next_dca -= DCA_STEP_PIPS * PIP
            # TP: if we've DCA'd, secure the basket at the FIRST entry (avg is now below it -> profit);
            #     if no DCA yet, need a real profit target above the first entry
            tp_price = first if adds > 0 else first + TP_PIPS * PIP
            if H >= tp_price:
                avg = sum(fills) / len(fills)
                pnl = (tp_price - avg) * len(fills) / PIP * dollars_per_pip
                closed = ("TP", pnl, len(fills), j); break
            j += 1
        if closed is None:
            j = len(ts) - 1
            avg = sum(fills) / len(fills)
            pnl = (op[j] - avg) * len(fills) / PIP * dollars_per_pip
            closed = ("EOD", pnl, len(fills), j)

        kind, pnl, units, jc = closed
        # costs: spread+comm per unit, both on entry and exit
        pnl -= (b.SPREAD_PIPS + b.comm_pips(first)) * dollars_per_pip * units
        eq += pnl
        peak = max(peak, eq)
        y = ts[i].year
        py = per_year[y]
        if py["peak"] is None:
            py["peak"] = eq
        py["peak"] = max(py["peak"], eq)
        py["maxdd"] = min(py["maxdd"], eq / py["peak"] - 1)
        py["pnl"] += pnl; py["w"] += pnl > 0; py["l"] += pnl <= 0
        trades.append((kind, pnl, units))
        last_close_i = jc
        i = jc + 1

    print(f"DCA GRID · dip-entry(ATRx{ENTRY_ATR_MULT}) · step {DCA_STEP_PIPS}p · maxadds {MAX_ADDS} · SL {SL_PIPS}p · {LOT_PER_UNIT}lot/unit")
    print(f"{ts[0].date()}→{ts[-1].date()} · {len(trades)} baskets · ${BAL0:.0f} start\n")
    print(f"  {'year':6} {'baskets':>7} {'win%':>5} {'P&L $':>10} {'maxDD':>7}")
    allpos = True
    for y in sorted(per_year):
        p = per_year[y]; n = p["w"] + p["l"]
        if p["pnl"] < 0: allpos = False
        print(f"  {y:6} {n:>7} {100*p['w']/n if n else 0:>4.0f}% {p['pnl']:>+10.0f} {100*p['maxdd']:>6.0f}%")
    tot = sum(t[1] for t in trades); w = sum(1 for t in trades if t[1] > 0)
    worst = min(trades, key=lambda t: t[1])
    sl_hits = [t for t in trades if t[0] == "SL"]
    print(f"\n  TOTAL: {tot:+.0f}$ on ${BAL0:.0f} = {100*tot/BAL0:+.0f}% · {100*w/len(trades):.0f}% win · {len(trades)} baskets")
    print(f"  WORST basket: {worst[1]:+.0f}$ ({worst[2]} units, {worst[0]}) · SL hits: {len(sl_hits)} ({100*len(sl_hits)/len(trades):.0f}%)")
    print(f"  avg SL-hit loss: {sum(t[1] for t in sl_hits)/len(sl_hits) if sl_hits else 0:+.0f}$ · avg win: {sum(t[1] for t in trades if t[1]>0)/max(1,w):+.0f}$")
    print(f"\n  VERDICT: {'positive every year ✅' if allpos else 'NOT positive every year ❌ (the tail bit)'}")


if __name__ == "__main__":
    run()
