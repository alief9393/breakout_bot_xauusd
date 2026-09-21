#!/usr/bin/env python3
"""
StreamTrade — SCALE-OUT backtest (vs real XAUUSD prices).

Splits the position into SCALE_TO equal parts and closes one part at each TP,
from TP1 up to TP{SCALE_TO}. Set SCALE_TO = 2, 3, 4, or 5.
  e.g. SCALE_TO=3 -> 1/3 closed at TP1, 1/3 at TP2, 1/3 at TP3 (TP4/TP5 ignored).
The part(s) whose TP isn't reached exit at breakeven (SL_TO_BE) or the stop.

NOTE: needs SCALE_TO sub-positions of >= 0.01 lot each, so total >= SCALE_TO*0.01.
On a small account the min-lot forces huge risk — see the risk warning it prints.

Edit CONFIG, then:  ./venv/bin/python bt_scaleout.py
"""

import json
import datetime

# ══════════════════════════ CONFIG — EDIT THESE ══════════════════════════
# ── channel preset: flip CHANNEL, file + entry mode follow automatically ──
CHANNEL       = "raahim"     # "raahim" (first channel) or "gold_pro"
PRESETS = {
    "raahim":   {"file": "signals.jsonl",         "entry": "market"},  # @Raahimbfxproo — BUY/SELL NOW
    "gold_pro": {"file": "signals_gold_pro.jsonl", "entry": "limit"},   # @Gold_xauusd_pro — limit orders
}
SIGNALS_FILE  = PRESETS[CHANNEL]["file"]
ENTRY_MODE    = PRESETS[CHANNEL]["entry"]
FILL_WINDOW_MIN = 60 * 8     # limit mode: max minutes to wait for the entry to fill, else skip
SCALE_TO      = 3            # scale out across TP1..TP{SCALE_TO}  (2, 3, 4, or 5)
SL_TO_BE      = True         # move stop to breakeven once enough parts have banked (else ride SL)
BE_AFTER_TP   = 2            # arm breakeven only AFTER this many TPs bank (2 = give TP1 room to breathe)
START_DATE    = "2026-03-01" # only trade signals on/after this date (""=all). Feb is partial (starts 02-18).

START_BALANCE = 100.0        # starting account, $
RISK_PCT      = 0.50         # risk per trade off CURRENT equity -> compounding (0.02 = 2%)
LEVERAGE      = 1000         # 1:LEVERAGE
MIN_LOT       = 0.01
MAX_LOT       = 100.0        # broker-style ceiling: never size above this many lots
MAX_LOOK_MIN  = 60 * 48
PRICE_CSV     = "xauusd_m1.csv"

# costs (IC Markets XAUUSD Raw) — pips (1 pip = $0.10)
SPREAD_PIPS      = 1.0
COMMISSION_USD_PER_100K = 6.0   # cTrader gold: $3/side = $6 round-turn per $100k notional (price-based)
ENTRY_SLIP_PIPS  = 1.0
SL_SLIP_PIPS     = 2.0
# ══════════════════════════════════════════════════════════════════════════

CONTRACT_OZ = 100
PIP_VALUE_PER_LOT = 10.0


def comm_pips(entry):
    return COMMISSION_USD_PER_100K * (entry * CONTRACT_OZ / 100000.0) / PIP_VALUE_PER_LOT


def load_prices():
    ts, op, hi, lo = [], [], [], []
    with open(PRICE_CSV) as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip("\n").split(",")
            ts.append(datetime.datetime.fromisoformat(t))
            op.append(float(o)); hi.append(float(h)); lo.append(float(l))
    return ts, op, hi, lo


def find_start(ts, when):
    a, b = 0, len(ts)
    while a < b:
        m = (a + b) // 2
        if ts[m] < when: a = m + 1
        else: b = m
    return a if a < len(ts) else None


def run():
    sigs = sorted((json.loads(l) for l in open(SIGNALS_FILE)), key=lambda s: s["date"])
    ts, op, hi, lo = load_prices()
    equity = START_BALANCE; peak = equity; max_dd = 0.0
    wins = losses = skipped = full = nofill = 0
    trades = []
    min_lot_forced = 0

    for s in sigs:
        if START_DATE and s["date"][:10] < START_DATE:
            continue
        when = datetime.datetime.fromisoformat(s["date"]).replace(tzinfo=None)
        i0 = find_start(ts, when)
        if i0 is None or len(s["tps"]) < 1:
            skipped += 1; continue
        d = 1 if s["dir"] == "BUY" else -1
        tp_dist = [abs(t - s["entry"]) for t in s["tps"]]
        sl_dist = abs(s["sl"] - s["entry"])
        if sl_dist <= 0 or not tp_dist:
            skipped += 1; continue

        # ── ENTRY: market (at signal bar) or limit (wait for price to reach stated entry) ──
        if ENTRY_MODE == "market":
            fill = i0
            entry = op[i0] + d * (ENTRY_SLIP_PIPS / 10.0)
        else:
            fill = None
            for j in range(i0, min(i0 + FILL_WINDOW_MIN, len(ts))):
                if lo[j] <= s["entry"] <= hi[j]:
                    fill = j; break
            if fill is None:
                nofill += 1; continue                 # limit never triggered — no trade
            entry = s["entry"] + d * (ENTRY_SLIP_PIPS / 10.0)

        n = min(SCALE_TO, len(tp_dist))                       # parts = TPs we scale across
        tp_lvl = [entry + d * tp_dist[k] for k in range(n)]
        sl_lvl = entry - d * sl_dist
        lots = (equity * RISK_PCT) / (sl_dist * CONTRACT_OZ)
        lots = min(lots, (equity * LEVERAGE) / (entry * CONTRACT_OZ))
        lots = min(lots, MAX_LOT)                             # broker-style lot ceiling
        lots = max(MIN_LOT * n, round(lots, 2))               # need n sub-lots of >= MIN_LOT
        if lots <= MIN_LOT * n + 1e-9:
            min_lot_forced += 1

        # realistic moving-stop sim: bank one part per TP; after BE_AFTER_TP parts the stop
        # jumps to breakeven (real BE = 0 for unreached parts), which can then be tapped.
        stop = sl_lvl; tps_hit = 0; banked = 0.0; end = min(fill + MAX_LOOK_MIN, len(ts))
        for j in range(fill, end):
            adverse = (lo[j] <= stop) if d == 1 else (hi[j] >= stop)
            if adverse:
                rem = n - tps_hit
                at_be = abs(stop - entry) < 1e-9
                banked += rem / n * (0.0 if at_be else -(sl_dist * 10 + SL_SLIP_PIPS))
                break
            while tps_hit < n and ((hi[j] >= tp_lvl[tps_hit]) if d == 1 else (lo[j] <= tp_lvl[tps_hit])):
                banked += tp_dist[tps_hit] * 10 / n
                tps_hit += 1
                if SL_TO_BE and tps_hit >= BE_AFTER_TP:
                    stop = entry
            if tps_hit == n:
                full += 1; break
        pips = banked - (SPREAD_PIPS + comm_pips(entry))
        pnl = pips * PIP_VALUE_PER_LOT * lots
        equity += pnl
        peak = max(peak, equity); max_dd = min(max_dd, equity / peak - 1)
        (wins := wins + 1) if pnl > 0 else (losses := losses + 1)
        trades.append({"date": when, "equity": equity})

    n_tr = wins + losses
    print("═" * 60)
    print(f"SCALE-OUT BACKTEST — scale across TP1..TP{SCALE_TO}  ({SCALE_TO} parts)")
    print("═" * 60)
    print(f"  {SIGNALS_FILE} · entry={ENTRY_MODE}" + (f" (fill≤{FILL_WINDOW_MIN//60}h)" if ENTRY_MODE == "limit" else ""))
    print(f"  ${START_BALANCE:.0f} start · {RISK_PCT*100:.0f}% risk · 1:{LEVERAGE} · compounding · SL→BE={SL_TO_BE}")
    print(f"  costs (pips): spread {SPREAD_PIPS} + comm ${COMMISSION_USD_PER_100K}/100k RT (~{comm_pips(4400):.1f}p @ $4400) + entry-slip {ENTRY_SLIP_PIPS} + SL-slip {SL_SLIP_PIPS}")
    print(f"  signals: {n_tr} traded (skipped {skipped}, never-filled {nofill})")
    print(f"  ran to TP{SCALE_TO}: {100*full/n_tr:.0f}%   ·   win rate {100*wins/n_tr:.0f}%   ·   maxDD {100*max_dd:.0f}%")
    print(f"  BALANCE: ${START_BALANCE:,.0f} → ${equity:,.2f}  ({100*(equity/START_BALANCE-1):+,.0f}%)")
    if min_lot_forced:
        print(f"\n  ⚠ min-lot forced on {100*min_lot_forced/n_tr:.0f}% of trades — you need {SCALE_TO}×0.01="
              f"{SCALE_TO*0.01:.2f} lot min, which exceeds your {RISK_PCT*100:.0f}% risk budget at ${START_BALANCE:.0f}."
              f"  Fund more or lower SCALE_TO to trade this properly.")
    print("\n  month-by-month:")
    cur = None
    for t in trades:
        ym = t["date"].strftime("%Y-%m")
        if ym != cur:
            if cur is not None: print(f"    {cur}:  ${last:,.2f}")
            cur = ym
        last = t["equity"]
    print(f"    {cur}:  ${last:,.2f}")


if __name__ == "__main__":
    run()
