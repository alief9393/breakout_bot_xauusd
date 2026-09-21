#!/usr/bin/env python3
"""
StreamTrade — SINGLE-TP backtest (vs real XAUUSD prices).

Exits the WHOLE position at one chosen take-profit (TARGET_TP) or the stop-loss.
Simplest mechanism: one position, one TP, one SL, no monitoring, fire-and-forget.

Edit CONFIG, then:  ./venv/bin/python bt_single_tp.py
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
TARGET_TP     = 4            # exit the whole position at this TP (1-5)
START_DATE    = "2026-03-01" # only trade signals on/after this date (""=all). Feb is partial (starts 02-18).

START_BALANCE = 1000.0        # starting account, $
RISK_PCT      = 0.30         # risk per trade off CURRENT equity -> compounding (0.02 = 2%)
LEVERAGE      = 1000         # 1:LEVERAGE (caps max lots via margin)
MIN_LOT       = 0.01
MAX_LOT       = 100.0        # broker-style ceiling: never size above this many lots
MAX_LOOK_MIN  = 60 * 48      # give each trade up to 48h to resolve
PRICE_CSV     = "xauusd_m1.csv"

# costs (IC Markets XAUUSD Raw, cTrader) — pips (1 pip = $0.10 = $10/lot)
SPREAD_PIPS      = 1.0
COMMISSION_USD_PER_100K = 6.0   # cTrader gold: $3/side = $6 round-turn per $100k notional (price-based)
ENTRY_SLIP_PIPS  = 1.0
SL_SLIP_PIPS     = 2.0


def comm_pips(entry):
    # $6 per $100k notional round-turn, expressed in pips: notional = entry * CONTRACT_OZ
    return COMMISSION_USD_PER_100K * (entry * CONTRACT_OZ / 100000.0) / PIP_VALUE_PER_LOT
# ══════════════════════════════════════════════════════════════════════════

CONTRACT_OZ = 100
PIP_VALUE_PER_LOT = 10.0


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
    wins = losses = skipped = reached = nofill = 0
    trades = []

    for s in sigs:
        if START_DATE and s["date"][:10] < START_DATE:
            continue
        when = datetime.datetime.fromisoformat(s["date"]).replace(tzinfo=None)
        i0 = find_start(ts, when)
        if i0 is None or len(s["tps"]) < TARGET_TP:
            skipped += 1; continue
        d = 1 if s["dir"] == "BUY" else -1
        if abs(s["sl"] - s["entry"]) <= 0 or not s["tps"]:
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

        # absolute TP levels still AHEAD of the fill (a late market entry can pass some TPs);
        # target the TARGET_TP-th surviving level — matches live_validate.py + the sweep.
        ahead = [t for t in s["tps"] if (t > entry if d == 1 else t < entry)]
        if len(ahead) < TARGET_TP:
            skipped += 1; continue                 # not enough TPs ahead to reach the target
        tgt = ahead[TARGET_TP - 1]
        sl_lvl = s["sl"]; sl_dist = abs(entry - sl_lvl)
        lots = (equity * RISK_PCT) / (sl_dist * CONTRACT_OZ)
        lots = min(lots, (equity * LEVERAGE) / (entry * CONTRACT_OZ))
        lots = min(lots, MAX_LOT)                             # broker-style lot ceiling
        lots = max(MIN_LOT, round(lots, 2))

        hit = False; sl_hit = False; end = min(fill + MAX_LOOK_MIN, len(ts))
        for j in range(fill, end):
            if (lo[j] <= sl_lvl) if d == 1 else (hi[j] >= sl_lvl):
                sl_hit = True; break
            if (hi[j] >= tgt) if d == 1 else (lo[j] <= tgt):
                hit = True; break

        if hit:
            pips = abs(tgt - entry) * 10; reached += 1
        elif sl_hit:
            pips = -(sl_dist * 10 + SL_SLIP_PIPS)
        else:
            pips = 0.0
        pips -= (SPREAD_PIPS + comm_pips(entry))
        pnl = pips * PIP_VALUE_PER_LOT * lots
        equity += pnl
        peak = max(peak, equity); max_dd = min(max_dd, equity / peak - 1)
        (wins := wins + 1) if pnl > 0 else (losses := losses + 1)
        trades.append({"date": when, "equity": equity})

    n = wins + losses
    print("═" * 60)
    print(f"SINGLE-TP BACKTEST — exit whole position at TP{TARGET_TP}")
    print("═" * 60)
    print(f"  {SIGNALS_FILE} · entry={ENTRY_MODE}" + (f" (fill≤{FILL_WINDOW_MIN//60}h)" if ENTRY_MODE == "limit" else ""))
    print(f"  ${START_BALANCE:.0f} start · {RISK_PCT*100:.0f}% risk · 1:{LEVERAGE} · compounding")
    print(f"  costs (pips): spread {SPREAD_PIPS} + comm ${COMMISSION_USD_PER_100K}/100k RT (~{comm_pips(4400):.1f}p @ $4400) + entry-slip {ENTRY_SLIP_PIPS} + SL-slip {SL_SLIP_PIPS}")
    print(f"  signals: {n} traded (skipped {skipped}, never-filled {nofill})")
    print(f"  reached TP{TARGET_TP}: {100*reached/n:.0f}%   ·   win rate {100*wins/n:.0f}%   ·   maxDD {100*max_dd:.0f}%")
    print(f"  BALANCE: ${START_BALANCE:,.0f} → ${equity:,.2f}  ({100*(equity/START_BALANCE-1):+,.0f}%)")
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
