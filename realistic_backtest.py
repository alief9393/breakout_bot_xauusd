#!/usr/bin/env python3
"""
StreamTrade — REALISTIC backtest (vs real XAUUSD prices, not the channel's claims).

For every signal:
  * ENTRY at MARKET the moment they post (open of the first M1 bar at/after the post)
    — this is what a copy-bot actually gets, and it sidesteps unreachable stated entries.
  * TP/SL placed at the SAME distances they gave, measured from YOUR real fill.
  * Real M1 prices decide the outcome, bar by bar, adverse-first (SL checked before TP
    within a bar, so wins are never over-counted). Scale-out 1/5 at each TP.
  * Compounding: each trade risks RISK_PCT of CURRENT equity, sized off the SL distance,
    capped by 1:LEVERAGE margin. Min lot enforced.

Run:  ./venv/bin/python realistic_backtest.py     (edit CONFIG below)
"""

import json
import datetime

# ══════════════════════════ CONFIG — EDIT THESE ══════════════════════════
START_BALANCE = 100.0        # starting account, $
RISK_PCT      = 0.02         # risk per trade (0.02 = 2%) off CURRENT equity -> compounding
LEVERAGE      = 1000          # 1:500
SCALE_OUT     = True         # 1/5 of the position at each of the 5 TPs (ignored if TARGET_TP set)
SL_TO_BE      = True         # after TP1, remainder exits at breakeven
TARGET_TP     = None         # (single mode) 1-5 to exit the WHOLE position at that TP
EXIT_MODE     = "trail"      # "scaleout" | "single" (uses TARGET_TP) | "trail" (step SL up behind each TP)
BE_LOCK_PIPS  = 0.0          # (trail) lock this many pips of profit at TP1 instead of exact breakeven
SWEEP         = True         # print a comparison of scale-out, trail, and every single-TP target
MIN_LOT       = 0.01
MAX_LOOK_MIN  = 60 * 48      # give each trade up to 48h to resolve
PRICE_CSV     = "xauusd_m1.csv"

# ── costs (IC Markets XAUUSD Raw, researched) — all in PIPS (1 pip = $0.10) ──
SPREAD_PIPS      = 1.0    # ~10c raw spread (use ~0 for overlap-only, up to 2-3 in news)
COMMISSION_PIPS  = 0.7    # $7 round-trip/lot, confirmed
ENTRY_SLIP_PIPS  = 1.0    # adverse fill from slippage + reaction latency (M1 can't resolve <1min,
                          #   so latency is folded in here; raise it to stress-test)
SL_SLIP_PIPS     = 2.0    # extra adverse slippage when a stop fills (fast moves)
# ══════════════════════════════════════════════════════════════════════════

CONTRACT_OZ = 100
PIP_VALUE_PER_LOT = 10.0     # $ per pip per lot (1pt=$1=10pips; 1 pip=$0.10 -> $10/lot)


def load_prices():
    ts, op, hi, lo, cl = [], [], [], [], []
    with open(PRICE_CSV) as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip("\n").split(",")
            ts.append(datetime.datetime.fromisoformat(t))
            op.append(float(o)); hi.append(float(h)); lo.append(float(l)); cl.append(float(c))
    return ts, op, hi, lo, cl


def find_start(ts, when):
    a, b = 0, len(ts)
    while a < b:
        m = (a + b) // 2
        if ts[m] < when: a = m + 1
        else: b = m
    return a if a < len(ts) else None


def simulate(sigs, ts, op, hi, lo, cl, target):
    """target: None=scale-out · int k=exit whole at TPk · 'trail'=step SL up behind each TP."""
    equity = START_BALANCE; peak = equity; max_dd = 0.0
    wins = losses = skipped = 0
    reach = [0] * 7
    trades = []
    for s in sigs:
        when = datetime.datetime.fromisoformat(s["date"]).replace(tzinfo=None)
        i0 = find_start(ts, when)
        if i0 is None:
            skipped += 1; continue
        d = 1 if s["dir"] == "BUY" else -1
        entry = op[i0] + d * (ENTRY_SLIP_PIPS / 10.0)
        tp_dist = [abs(t - s["entry"]) for t in s["tps"]]
        sl_dist = abs(s["sl"] - s["entry"])
        if sl_dist <= 0 or not tp_dist or (isinstance(target, int) and len(tp_dist) < target):
            skipped += 1; continue
        tp_lvl = [entry + d * dd for dd in tp_dist]; sl_lvl = entry - d * sl_dist; n = len(tp_lvl)
        lots = (equity * RISK_PCT) / (sl_dist * CONTRACT_OZ)
        lots = min(lots, (equity * LEVERAGE) / (entry * CONTRACT_OZ))
        lots = max(MIN_LOT, round(lots, 2))
        end = min(i0 + MAX_LOOK_MIN, len(ts))
        tp_pips = [dd * 10 for dd in tp_dist]; sl_pips = sl_dist * 10; sl_loss = sl_pips + SL_SLIP_PIPS

        if target == "trail":
            cur_sl = sl_lvl; tps_hit = 0; exit_px = None; via_stop = False
            for j in range(i0, end):
                if (lo[j] <= cur_sl) if d == 1 else (hi[j] >= cur_sl):
                    exit_px = cur_sl; via_stop = True; break
                while tps_hit < n and ((hi[j] >= tp_lvl[tps_hit]) if d == 1 else (lo[j] <= tp_lvl[tps_hit])):
                    tps_hit += 1
                    cur_sl = (entry + d * (BE_LOCK_PIPS / 10.0)) if tps_hit == 1 else tp_lvl[tps_hit - 2]
                if tps_hit == n:
                    exit_px = tp_lvl[n - 1]; break
            if exit_px is None:
                exit_px = cl[min(end - 1, len(cl) - 1)]
            pips = d * (exit_px - entry) * 10 - (SL_SLIP_PIPS if via_stop else 0.0)
        elif target == "hybrid":                             # bank 1/n per TP + trail the remainder
            cur_sl = sl_lvl; tps_hit = 0; banked = 0.0; via_stop = False; rem_exit = None
            for j in range(i0, end):
                if (n - tps_hit) > 0 and ((lo[j] <= cur_sl) if d == 1 else (hi[j] >= cur_sl)):
                    rem_exit = cur_sl; via_stop = True; break
                while tps_hit < n and ((hi[j] >= tp_lvl[tps_hit]) if d == 1 else (lo[j] <= tp_lvl[tps_hit])):
                    banked += tp_pips[tps_hit] / n            # this 1/n portion booked at its TP
                    tps_hit += 1
                    cur_sl = (entry + d * (BE_LOCK_PIPS / 10.0)) if tps_hit == 1 else tp_lvl[tps_hit - 2]
                if tps_hit == n:
                    break
            if rem_exit is None:
                rem_exit = cl[min(end - 1, len(cl) - 1)]
            rem = (n - tps_hit) / n
            rem_pips = d * (rem_exit - entry) * 10 - (SL_SLIP_PIPS if via_stop else 0.0)
            pips = banked + rem * rem_pips
        else:
            tps_hit = 0; sl_hit = False
            for j in range(i0, end):
                if (lo[j] <= sl_lvl) if d == 1 else (hi[j] >= sl_lvl):
                    sl_hit = True; break
                while tps_hit < n and ((hi[j] >= tp_lvl[tps_hit]) if d == 1 else (lo[j] <= tp_lvl[tps_hit])):
                    tps_hit += 1
                if tps_hit == n:
                    break
            if target is None:                               # scale-out
                if tps_hit == 0:
                    pips = -sl_loss if sl_hit else 0.0
                else:
                    pips = sum(tp_pips[k] / n for k in range(tps_hit)) \
                           + (n - tps_hit) / n * (0.0 if SL_TO_BE else (-sl_loss if sl_hit else 0.0))
            else:                                            # single target TPk
                pips = tp_pips[target - 1] if tps_hit >= target else (-sl_loss if sl_hit else 0.0)

        for k in range(7):
            if tps_hit >= k:
                reach[k] += 1
        pips -= (SPREAD_PIPS + COMMISSION_PIPS)
        pnl = pips * PIP_VALUE_PER_LOT * lots
        equity += pnl
        peak = max(peak, equity); max_dd = min(max_dd, equity / peak - 1)
        (wins := wins + 1) if pnl > 0 else (losses := losses + 1)
        trades.append({"date": when, "equity": equity})
    n = wins + losses
    return {"final": equity, "max_dd": max_dd, "wins": wins, "n": n,
            "reach": reach, "trades": trades, "skipped": skipped}


def main():
    sigs = sorted((json.loads(l) for l in open("signals.jsonl")), key=lambda s: s["date"])
    ts, op, hi, lo, cl = load_prices()

    target = {"scaleout": None, "single": TARGET_TP, "trail": "trail"}[EXIT_MODE]
    label = {"scaleout": "SCALE-OUT (1/5 per TP)", "single": f"TARGET TP{TARGET_TP} only",
             "trail": "STEP-TRAIL SL behind each TP"}[EXIT_MODE]
    r = simulate(sigs, ts, op, hi, lo, cl, target)
    print("═" * 66)
    print(f"REALISTIC BACKTEST vs REAL XAUUSD — @Raahimbfxproo")
    print("═" * 66)
    print(f"  ${START_BALANCE:.0f} start · {RISK_PCT*100:.0f}% risk · 1:{LEVERAGE} · compounding · exit: {label}")
    print(f"  costs (pips): spread {SPREAD_PIPS} + commission {COMMISSION_PIPS} + "
          f"entry-slip {ENTRY_SLIP_PIPS} + SL-slip {SL_SLIP_PIPS}")
    print(f"  win rate {100*r['wins']/r['n']:.0f}%  ·  maxDD {100*r['max_dd']:.0f}%")
    print(f"  BALANCE: ${START_BALANCE:,.0f} → ${r['final']:,.2f}  ({100*(r['final']/START_BALANCE-1):+,.0f}%)")
    print("\n  month-by-month:")
    cur = None
    for t in r["trades"]:
        ym = t["date"].strftime("%Y-%m")
        if ym != cur:
            if cur is not None: print(f"    {cur}:  ${last_eq:,.2f}")
            cur = ym
        last_eq = t["equity"]
    print(f"    {cur}:  ${last_eq:,.2f}")

    if SWEEP:
        import statistics as st
        print("\n" + "─" * 66)
        print("  SWEEP — all exit mechanisms (same $/risk/costs):")
        print(f"  {'exit':<24}{'win%':>7}{'maxDD':>8}{'final $':>16}")
        for k in range(1, 6):
            rk = simulate(sigs, ts, op, hi, lo, cl, k)
            rr = st.median([abs(s['tps'][k-1]-s['entry'])/abs(s['sl']-s['entry'])
                            for s in sigs if len(s['tps']) >= k and s['sl'] != s['entry']])
            print(f"  {f'TP{k} only ({rr:.2f}:1)':<24}{100*rk['wins']/rk['n']:>6.0f}%"
                  f"{100*rk['max_dd']:>7.0f}%{rk['final']:>16,.0f}")
        rt = simulate(sigs, ts, op, hi, lo, cl, "trail")
        print(f"  {'STEP-TRAIL (1 posn)':<24}{100*rt['wins']/rt['n']:>6.0f}%"
              f"{100*rt['max_dd']:>7.0f}%{rt['final']:>16,.0f}")
        rs = simulate(sigs, ts, op, hi, lo, cl, None)
        print(f"  {'scale-out':<24}{100*rs['wins']/rs['n']:>6.0f}%"
              f"{100*rs['max_dd']:>7.0f}%{rs['final']:>16,.0f}")
        rh = simulate(sigs, ts, op, hi, lo, cl, "hybrid")
        print(f"  {'HYBRID (bank+trail)':<24}{100*rh['wins']/rh['n']:>6.0f}%"
              f"{100*rh['max_dd']:>7.0f}%{rh['final']:>16,.0f}")


if __name__ == "__main__":
    main()
