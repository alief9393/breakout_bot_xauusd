#!/usr/bin/env python3
"""
Channel backtest with a REALISTIC entry delay.
Same engine/logic as bt_dynamic_split (dynamic-split, trail-behind-TP, real M1
gating, same costs) — but the fill price is modeled honestly:

  entry = the REAL M1 market price at (signal post time + DELAY), not the
          channel's stated entry (which is already stale when posted).

Edit the CONFIG block below, save, and run:
  ./venv/bin/python bt_channel_delay.py
Changing the exit knobs here overrides them for THIS backtest only — it does
NOT edit bt_dynamic_split.py, so the live bots are untouched.
"""
import sys, collections, datetime
sys.path.insert(0, "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade")
import bt_dynamic_split as b

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG — edit here, then run. (No command-line needed.)
# ═══════════════════════════════════════════════════════════════════════════
RISK        = 0.40            # risk per trade (fraction of balance)
BAL0        = 100.0          # starting balance
DELAYS      = [0, 1]   # minutes after post we fill; each is one row
SHOW_STATED = True            # also show the channel's fantasy (fill at posted entry)

# --- exit engine (overrides bt_dynamic_split for THIS backtest only) ---
TARGET_MAP  = {1: [1], 2: [1, 2], 4: [0, 1, 2, 3]}   # 1->TP2 · 2->TP2+TP3 · 4->TP1..TP4
BUFFER_PIPS = 5              # trail the stop this far behind each hit TP
SPLIT2      = 0.02           # calc lot >= this -> 2-way split
SPLIT4      = 0.08           # calc lot >= this -> 4-way split
MAX_LOT     = 100.0          # lot ceiling

# --- your withdrawal plan ---
RECOVER_AT   = 200.0         # when balance first hits this, pull your initial stake out
RECOVER_AMT  = 100.0         # the initial stake to withdraw (capital safe -> house money)
HARVEST_AT   = 2000.0        # once balance reaches this, start monthly withdrawals
MONTHLY_PCT  = 0.50          # withdraw this fraction of the balance each month while harvesting

# --- money tree: reinvest cash-out into more instances ---
TREE_TRIGGER     = 1000.0    # when instance-1 cash-out reaches this, spin up new instances
NEW_INSTANCES    = 2         # how many new instances to launch
NEW_INSTANCE_BAL = 500.0     # starting balance each (funded from the cash-out)
# ═══════════════════════════════════════════════════════════════════════════

# apply the config to the shared engine (runtime only — file on disk unchanged)
b.TARGET_MAP = TARGET_MAP
b.BUFFER_PIPS = BUFFER_PIPS
b.SPLIT2 = SPLIT2
b.SPLIT4 = SPLIT4
b.MAX_LOT = MAX_LOT
b.SIGNAL_SOURCE = "channel"
b.EARLY_ALERT_ENTRY = False          # test the details-post timing itself

ts, op, hi, lo = b.load_prices()
sigs = b.load_signals(ts, op)
SLIP = b.ENTRY_SLIP_PIPS / 10.0      # market slippage on the fill (price units)


def run(delay_min, use_stated=False):
    """One equity run. delay_min = minutes after post we actually fill.
       use_stated=True -> fill at the channel's posted entry (their fantasy)."""
    yr = {}
    skipped = 0
    for s in sigs:
        when = datetime.datetime.fromisoformat(s["date"]).replace(tzinfo=None)
        i0 = b.find_start(ts, when)
        if i0 is None:
            continue
        i_enter = min(i0 + delay_min, len(ts) - 1)
        d = 1 if s["dir"] == "BUY" else -1
        entry = (s["entry"] if use_stated else op[i_enter]) + d * SLIP
        sl = s["sl"]
        sl_dist = abs(entry - sl)
        ahead = [t for t in s["tps"] if (t > entry if d == 1 else t < entry)]
        if not ahead or sl_dist <= 0:
            skipped += 1                              # market ran past every TP -> untradeable
            continue
        y = ts[i_enter].year
        st = yr.setdefault(y, [BAL0, BAL0, 0.0, 0, 0])
        total = max(b.MIN_LOT, min(round((st[0] * RISK) / (sl_dist * b.CONTRACT_OZ), 2), MAX_LOT))
        idx, sizes = b.plan_split(total, len(ahead))
        gross = b.simulate(entry, d, sl, [ahead[k] for k in idx], sizes, i_enter, ts, hi, lo)
        pnl = gross - (b.SPREAD_PIPS + b.comm_pips(entry)) * 10 * sum(sizes)
        st[0] += pnl
        st[1] = max(st[1], st[0]); st[2] = min(st[2], st[0] / st[1] - 1)
        st[3] += pnl > 0; st[4] += pnl <= 0
    years = sorted(yr)
    rets = {y: yr[y][0] / BAL0 - 1 for y in years}
    n = sum(yr[y][3] + yr[y][4] for y in years)
    w = sum(yr[y][3] for y in years)
    dd = min((yr[y][2] for y in years), default=0)
    return rets, (w / n if n else 0), n, skipped, dd


def result(delay=0, stated=False):
    """Return (win%, maxDD%, final_balance) — final balance compounds year over year."""
    rets, win, n, skipped, dd = run(delay, use_stated=stated)
    final = BAL0
    for y in sorted(rets):
        final *= (1 + rets[y])
    return 100 * win, 100 * dd, final, skipped


def instance_sim(delay_min, start_bal, start_after=None, recover_amt=0.0, recover_at=None, trigger=None):
    """Simulate one instance. Trades signals posted after `start_after` (or all).
       Recovers `recover_amt` when balance first hits `recover_at`, then withdraws
       MONTHLY_PCT/month above HARVEST_AT. If `trigger` is set, records the date the
       cumulative cash-out first reaches it. Drawdown on total wealth (bal + withdrawn)."""
    ordered = sorted(sigs, key=lambda s: s["date"])
    bal = start_bal; withdrawn = 0.0
    recovered = (recover_amt <= 0); harvesting = False; blew_up = False
    peak = start_bal; maxdd = 0.0; last_month = None; trigger_date = None
    wins = losses = 0
    for s in ordered:
        when = datetime.datetime.fromisoformat(s["date"]).replace(tzinfo=None)
        if start_after is not None and when <= start_after:
            continue
        ym = (when.year, when.month)
        if harvesting and last_month is not None and ym != last_month:   # month rolled over
            cut = bal * MONTHLY_PCT; withdrawn += cut; bal -= cut
        last_month = ym
        if bal <= 0:
            blew_up = True; break
        i0 = b.find_start(ts, when)
        if i0 is None:
            continue
        i_enter = min(i0 + delay_min, len(ts) - 1)
        d = 1 if s["dir"] == "BUY" else -1
        entry = op[i_enter] + d * SLIP
        sl = s["sl"]; sl_dist = abs(entry - sl)
        ahead = [t for t in s["tps"] if (t > entry if d == 1 else t < entry)]
        if not ahead or sl_dist <= 0:
            continue
        total = max(b.MIN_LOT, min(round((bal * RISK) / (sl_dist * b.CONTRACT_OZ), 2), MAX_LOT))
        idx, sizes = b.plan_split(total, len(ahead))
        gross = b.simulate(entry, d, sl, [ahead[k] for k in idx], sizes, i_enter, ts, hi, lo)
        pnl = gross - (b.SPREAD_PIPS + b.comm_pips(entry)) * 10 * sum(sizes)
        bal += pnl; wins += pnl > 0; losses += pnl <= 0
        wealth = bal + withdrawn
        peak = max(peak, wealth); maxdd = min(maxdd, wealth / peak - 1)
        if bal <= 0:
            blew_up = True; break
        if not recovered and recover_at and bal >= recover_at:
            withdrawn += recover_amt; bal -= recover_amt; recovered = True
        if bal >= HARVEST_AT:
            harvesting = True
        if trigger is not None and trigger_date is None and withdrawn >= trigger:
            trigger_date = when
    n = wins + losses
    return dict(withdrawn=withdrawn, final=bal, total=withdrawn + bal, maxdd=100 * maxdd,
                recovered=recovered, harvested=harvesting, blew_up=blew_up,
                trigger_date=trigger_date, win=100 * wins / n if n else 0)


def tree_sim(delay_min):
    """Instance 1 starts at $BAL0. When its cash-out hits TREE_TRIGGER, spend
       NEW_INSTANCES x NEW_INSTANCE_BAL of that cash-out to launch new instances,
       which then trade forward from that date. Report the whole tree's pocketed cash."""
    i1 = instance_sim(delay_min, BAL0, recover_amt=RECOVER_AMT, recover_at=RECOVER_AT, trigger=TREE_TRIGGER)
    fund = NEW_INSTANCES * NEW_INSTANCE_BAL
    if i1["trigger_date"] is None:
        return dict(triggered=False, pocket=i1["withdrawn"], left=i1["final"],
                    total=i1["total"], when=None, i1=i1, subs=[])
    subs = [instance_sim(delay_min, NEW_INSTANCE_BAL, start_after=i1["trigger_date"],
                         recover_amt=NEW_INSTANCE_BAL, recover_at=2 * NEW_INSTANCE_BAL)
            for _ in range(NEW_INSTANCES)]
    pocket = i1["withdrawn"] - fund + sum(s["withdrawn"] for s in subs)   # fund is reinvested, not pocketed
    left = i1["final"] + sum(s["final"] for s in subs)
    return dict(triggered=True, pocket=pocket, left=left, total=pocket + left,
                when=i1["trigger_date"], i1=i1, subs=subs, fund=fund)


if __name__ == "__main__":
    print(f"\nCHANNEL backtest · realistic delay · WITHDRAWAL PLAN + MONEY TREE · {RISK*100:.0f}% risk")
    print(f"inst-1 ${BAL0:.0f} start · recover ${RECOVER_AMT:.0f} at ${RECOVER_AT:.0f} · harvest {MONTHLY_PCT*100:.0f}%/mo above ${HARVEST_AT:.0f}")
    print(f"tree: when cash-out hits ${TREE_TRIGGER:,.0f} -> launch {NEW_INSTANCES} x ${NEW_INSTANCE_BAL:.0f} instances")
    print(f"{len(sigs)} signals · {ts[0].date()}→{ts[-1].date()} · real cTrader M1\n")

    hdr = f"  {'Delay':>6} | {'single-instance':>16} | {'MONEY TREE (3 inst)':>19} | tree launched"
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for dmin in DELAYS:
        one = instance_sim(dmin, BAL0, recover_amt=RECOVER_AMT, recover_at=RECOVER_AT)
        tr = tree_sim(dmin)
        launched = tr["when"].strftime("%Y-%m") if tr["triggered"] else "—"
        print(f"  {str(dmin)+'m':>6} | ${one['withdrawn']:>14,.0f} | ${tr['pocket']:>17,.0f} | {launched}")
    print("\n  single-instance = cash pocketed by instance 1 alone.")
    print(f"  MONEY TREE = instance 1 + {NEW_INSTANCES} more launched from its cash-out (the ${NEW_INSTANCES*NEW_INSTANCE_BAL:,.0f} funding is reinvested, not counted as pocket).")
