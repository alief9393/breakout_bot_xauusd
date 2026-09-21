#!/usr/bin/env python3
"""
StreamTrade — DYNAMIC-SPLIT backtest (your design).

Splits the risk-sized position into sub-positions based on how big it is, targets a
different set of TPs per split count, and trails the stop behind each hit TP (with a
buffer, to avoid the race condition at the TP level).

  calc lot  <  SPLIT2      -> 1 position  -> aim TP3
  SPLIT2 <= calc < SPLIT4  -> 2 positions -> aim TP3, TP4
  calc lot >= SPLIT4       -> 4 positions -> aim TP1, TP2, TP3, TP4

Lots are distributed to valid 0.01 steps (remainder to the NEARER TPs), so total size
is preserved even when it doesn't divide evenly (0.10/4 -> 0.03,0.03,0.02,0.02).

After any sub-position's TP fills, the remaining subs' stop trails to (that TP − buffer),
locking progressive profit. Enter at MARKET. Edit CONFIG, then run.
"""

import json
import re
import datetime

# ══════════════════════════ CONFIG — EDIT THESE ══════════════════════════
SIGNAL_SOURCE = "channel"    # "channel" · "momentum" · "hybrid" (channel + momentum in MOM_HOURS)
CHANNEL       = "raahim"
PRESETS = {"raahim": {"file": "signals.jsonl", "entry": "market"},
           "gold_pro": {"file": "signals_gold_pro.jsonl", "entry": "limit"}}
SIGNALS_FILE  = PRESETS[CHANNEL]["file"]

# channel-only: enter at the bare "GOLD BUY/SELL NOW" alert (earlier) instead of the details post
EARLY_ALERT_ENTRY = True             # True = enter at the NOW-alert time when one preceded the signal
ALERT_WINDOW_MIN  = 5                # match an alert within this many minutes before the details
MESSAGES_FILE     = "messages.jsonl"

# momentum generator params (used only when SIGNAL_SOURCE="momentum")
MOM_HOURS     = {0, 1, 2, 3}          # morning session (UTC)
MOM_MOVE_MIN  = 15                    # follow a move >= this (pips) over MOM_LOOKBACK
MOM_LOOKBACK  = 30
MOM_COOLDOWN  = 120                   # min minutes between signals
MOM_LADDER    = [30, 60, 90, 120, 150, 180]   # TP ladder (pips) — mirrors the channel's structure
MOM_SL        = 130                   # SL (pips)

SPLIT2        = 0.02          # calc lot >= this -> split into 2
SPLIT4        = 0.08          # calc lot >= this -> split into 4
BUFFER_PIPS   = 5            # trail the stop this far BEHIND a hit TP (race-condition guard)
# which ahead-TP indices (0-based: 0=TP1, 1=TP2, ...) each split count targets:
TARGET_MAP    = {1: [1], 2: [1, 2], 4: [0, 1, 2, 3]}   # 1->TP2 · 2->TP2+TP3 · 4->TP1..TP4
START_DATE    = "2026-03-01"

START_BALANCE = 100.0
RISK_PCT      = 0.10
MIN_LOT       = 0.01
MAX_LOT       = 100.0
PRICE_CSV     = "xauusd_m1.csv"
MAX_LOOK_MIN  = 60 * 48

SPREAD_PIPS      = 1.0
ENTRY_SLIP_PIPS  = 1.0
SL_SLIP_PIPS     = 2.0
COMMISSION_USD_PER_100K = 6.0
# ══════════════════════════════════════════════════════════════════════════

CONTRACT_OZ = 100
PIP_VALUE_PER_LOT = 10.0


def load_prices():
    ts, op, hi, lo = [], [], [], []
    with open(PRICE_CSV) as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip("\n").split(",")
            ts.append(datetime.datetime.fromisoformat(t)); op.append(float(o)); hi.append(float(h)); lo.append(float(l))
    return ts, op, hi, lo


def find_start(ts, when):
    a, b = 0, len(ts)
    while a < b:
        m = (a + b) // 2
        if ts[m] < when: a = m + 1
        else: b = m
    return a if a < len(ts) else None


def comm_pips(entry):
    return COMMISSION_USD_PER_100K * (entry * CONTRACT_OZ / 100000.0) / PIP_VALUE_PER_LOT


def generate_momentum(ts, op):
    """Our OWN signals (no survivorship): morning session, follow a >= MOM_MOVE_MIN move."""
    out = []; last = -10 ** 9
    for i in range(MOM_LOOKBACK, len(ts) - MAX_LOOK_MIN):
        if ts[i].hour not in MOM_HOURS or i - last < MOM_COOLDOWN:
            continue
        mv = (op[i] - op[i - MOM_LOOKBACK]) * 10
        if abs(mv) < MOM_MOVE_MIN:
            continue
        d = 1 if mv > 0 else -1
        entry = op[i]
        tps = [entry + d * p / 10.0 for p in MOM_LADDER]
        sl = entry - d * MOM_SL / 10.0
        out.append({"date": ts[i].isoformat(), "dir": "BUY" if d == 1 else "SELL",
                    "entry": entry, "tps": tps, "sl": sl})
        last = i
    return out


_ALERT_BAD = re.compile(r'PROFIT|RUNNING|PIPS|EXPECT|GAP|HIT|DONE|✅', re.I)


def _alert_dir(t):
    """A bare 'GOLD BUY/SELL NOW' alert (no levels) -> its direction, else None."""
    tu = (t or "").upper()
    if _ALERT_BAD.search(t or "") or "TP" in tu:
        return None
    if re.search(r'\bNOW\b', tu) and re.search(r'\b(BUY|SELL)\b', tu):
        return "BUY" if "BUY" in tu else "SELL"
    return None


def attach_alert_entries(sigs):
    """For channel signals preceded by a NOW-alert, set entry_date = the alert time (earlier)."""
    msgs = [json.loads(l) for l in open(MESSAGES_FILE)]
    alerts = []
    for m in msgs:
        d = _alert_dir(m["text"])
        if d and m.get("date"):
            alerts.append((datetime.datetime.fromisoformat(m["date"]).replace(tzinfo=None), d))
    for s in sigs:
        sd = datetime.datetime.fromisoformat(s["date"]).replace(tzinfo=None)
        best = None
        for at, ad in alerts:
            gap = (sd - at).total_seconds()
            if 0 < gap <= ALERT_WINDOW_MIN * 60 and ad == s["dir"]:
                if best is None or at > best:
                    best = at
        s["entry_date"] = best.isoformat() if best else s["date"]
    return sigs


def load_signals(ts, op):
    if SIGNAL_SOURCE == "momentum":
        return [dict(s, src="momentum") for s in generate_momentum(ts, op)]
    ch = [json.loads(l) for l in open(SIGNALS_FILE)]
    if EARLY_ALERT_ENTRY:
        ch = attach_alert_entries(ch)
    for s in ch:
        s["src"] = "channel"
    if SIGNAL_SOURCE == "hybrid":
        # channel signals (their times) + momentum signals in MOM_HOURS (set these to the 15-21 UTC gap)
        mom = [dict(s, src="momentum") for s in generate_momentum(ts, op)]
        return ch + mom
    return ch


def plan_split(total, avail):
    """Return (target_indices_into_ahead, sub_sizes) for a total lot size and #ahead TPs."""
    units = max(1, round(total / 0.01))                 # number of 0.01-lots
    ndes = 4 if total >= SPLIT4 else 2 if total >= SPLIT2 else 1
    n = min(ndes, avail, units)                          # can't exceed available TPs or lots
    base_idx = TARGET_MAP.get(n, list(range(n)))         # which TPs this split count aims at
    idx = sorted(set(min(i, avail - 1) for i in base_idx))   # clamp to what's available, dedup
    n = len(idx)
    base = units // n; rem = units - base * n
    sizes = [(base + (1 if k < rem else 0)) * 0.01 for k in range(n)]   # remainder -> nearer TPs
    return idx, sizes


def simulate(entry, d, sl, targets, sizes, i0, ts, hi, lo):
    """Trail-behind-TP sim. Returns gross $ P&L (before spread/commission)."""
    open_subs = list(zip(targets, sizes))
    stop = sl
    pnl = 0.0
    end = min(i0 + MAX_LOOK_MIN, len(ts))
    for j in range(i0, end):
        if (lo[j] <= stop) if d == 1 else (hi[j] >= stop):          # stop hit -> close all remaining
            for tgt, sz in open_subs:
                pips = (stop - entry) * d * 10 - SL_SLIP_PIPS
                pnl += pips * PIP_VALUE_PER_LOT * sz
            open_subs = []
            break
        still = []
        for tgt, sz in open_subs:
            if (hi[j] >= tgt) if d == 1 else (lo[j] <= tgt):        # this sub's TP filled
                pips = (tgt - entry) * d * 10
                pnl += pips * PIP_VALUE_PER_LOT * sz
                trail = tgt - d * (BUFFER_PIPS / 10.0)              # trail stop behind the hit TP
                if (trail > stop) if d == 1 else (trail < stop):
                    stop = trail
            else:
                still.append((tgt, sz))
        open_subs = still
        if not open_subs:
            break
    return pnl


def run():
    ts, op, hi, lo = load_prices()
    sigs = sorted(load_signals(ts, op), key=lambda s: s["date"])
    equity = START_BALANCE; peak = equity; max_dd = 0.0
    wins = losses = skipped = 0
    split_count = {}
    trades = []

    early_used = 0
    src_n = {}; src_pnl = {}
    for s in sigs:
        if START_DATE and s["date"][:10] < START_DATE:
            continue
        entry_date = s.get("entry_date", s["date"])
        early_used += entry_date != s["date"]
        when = datetime.datetime.fromisoformat(entry_date).replace(tzinfo=None)
        i0 = find_start(ts, when)
        if i0 is None:
            skipped += 1; continue
        d = 1 if s["dir"] == "BUY" else -1
        entry = op[i0] + d * (ENTRY_SLIP_PIPS / 10.0)
        ahead = [t for t in s["tps"] if (t > entry if d == 1 else t < entry)]
        sl = s["sl"]; sl_dist = abs(entry - sl)
        if not ahead or sl_dist <= 0:
            skipped += 1; continue

        calc = (equity * RISK_PCT) / (sl_dist * CONTRACT_OZ)
        total = max(MIN_LOT, min(round(calc, 2), MAX_LOT))
        idx, sizes = plan_split(total, len(ahead))
        targets = [ahead[i] for i in idx]
        split_count[len(sizes)] = split_count.get(len(sizes), 0) + 1

        gross = simulate(entry, d, sl, targets, sizes, i0, ts, hi, lo)
        total_lots = sum(sizes)
        cost = (SPREAD_PIPS + comm_pips(entry)) * PIP_VALUE_PER_LOT * total_lots
        pnl = gross - cost
        equity += pnl
        peak = max(peak, equity); max_dd = min(max_dd, equity / peak - 1)
        wins += pnl > 0; losses += pnl <= 0
        sc = s.get("src", "channel")
        src_n[sc] = src_n.get(sc, 0) + 1
        src_pnl[sc] = src_pnl.get(sc, 0.0) + pnl
        trades.append({"date": when, "equity": equity})

    n = wins + losses
    print("═" * 62)
    print("DYNAMIC-SPLIT BACKTEST — size-based split, trail-behind-TP")
    print("═" * 62)
    src = "MOMENTUM (our own — honest)" if SIGNAL_SOURCE == "momentum" else f"CHANNEL {SIGNALS_FILE} (survivorship!)"
    print(f"  signals: {src} · entry=market · ${START_BALANCE:.0f} · {RISK_PCT*100:.0f}% risk · compounding")
    print(f"  split thresholds: 1pos<{SPLIT2} <=2pos<{SPLIT4} <=4pos   ·   trail buffer {BUFFER_PIPS}p")
    print(f"  signals: {n} (skipped {skipped})")
    if SIGNAL_SOURCE == "channel" and EARLY_ALERT_ENTRY:
        print(f"  early-alert entries used: {early_used} (entered at the 'NOW' alert instead of the details)")
    print(f"  split usage: 1-pos {split_count.get(1,0)} · 2-pos {split_count.get(2,0)} · 3-pos {split_count.get(3,0)} · 4-pos {split_count.get(4,0)}")
    if SIGNAL_SOURCE == "hybrid":
        print(f"  by source:  channel {src_n.get('channel',0)} sig ({src_pnl.get('channel',0):+,.0f} $) · momentum {src_n.get('momentum',0)} sig ({src_pnl.get('momentum',0):+,.0f} $)")
    print(f"  win rate {100*wins/n:.0f}%   ·   maxDD {100*max_dd:.0f}%")
    print(f"  BALANCE: ${START_BALANCE:,.0f} -> ${equity:,.2f}  ({100*(equity/START_BALANCE-1):+,.0f}%)")
    print("\n  month-by-month:")
    cur = None
    for t in trades:
        ym = t["date"].strftime("%Y-%m")
        if ym != cur:
            if cur is not None: print(f"    {cur}:  ${last:,.2f}")
            cur = ym
        last = t["equity"]
    if cur: print(f"    {cur}:  ${last:,.2f}")


if __name__ == "__main__":
    run()
