#!/usr/bin/env python3
"""
StreamTrade RESEARCH — optimize the honest morning fade for PROFIT (Jan29–Aug31 2026).

Grid over trigger size / cooldown / exit-TP (morning session fixed), ranked by total
pips. Reports win%, trades/day, drawdown, profit factor. Then shows the winner's
month-by-month and a TRAIN/TEST split so we can see it's not a single-period fluke.
"""

import json
import datetime
import collections

BASE = "/Users/aliefchandrawijaya/experiment/ai-llm-bot/StreamTrade/"
MORNING_HOURS = {0, 1, 2, 3}
SL_PIPS   = 130
SLSLIP    = 2.0; SLIP = 1.0; SPREAD = 1.0; COMM_PER_100K = 6.0
LOOK      = 60 * 48
START     = "2026-01-01"; END = "2026-08-31"     # data actually starts 2026-01-29
SPLIT     = "2026-06-15"


def load():
    ts = []; op = []; hi = []; lo = []
    with open(BASE + "xauusd_m1.csv") as f:
        next(f)
        for line in f:
            t, o, h, l, c, v = line.rstrip().split(",")
            dt = datetime.datetime.fromisoformat(t)
            ts.append(dt); op.append(float(o)); hi.append(float(h)); lo.append(float(l))
    return ts, op, hi, lo


def comm_pips(entry):
    return COMM_PER_100K * (entry * 100 / 100000.0) / 10.0


def signals(ts, op, hi, lo, move_min, cooldown, mode="fade"):
    out = []; last = -10**9
    for i in range(30, len(ts) - LOOK):
        if ts[i].hour not in MORNING_HOURS or i - last < cooldown: continue
        ds = ts[i].date().isoformat()
        if ds < START or ds > END: continue
        mv = (op[i] - op[i - 30]) * 10
        if abs(mv) < move_min: continue
        sgn = 1 if mv > 0 else -1
        d = -sgn if mode == "fade" else sgn          # fade = against the move; momo = with it
        out.append({"i": i, "t": ts[i], "d": d, "entry": op[i] + d * SLIP / 10})
        last = i
    return out


def resolve(sig, op, hi, lo, ts, tp_pips):
    d = sig["d"]; entry = sig["entry"]; i = sig["i"]
    tp = entry + d * tp_pips / 10; sl = entry - d * SL_PIPS / 10
    res = None
    for j in range(i, i + LOOK):
        if (lo[j] <= sl) if d == 1 else (hi[j] >= sl): res = "SL"; break
        if (hi[j] >= tp) if d == 1 else (lo[j] <= tp): res = "TP"; break
    p = (tp_pips if res == "TP" else -(SL_PIPS + SLSLIP) if res == "SL" else 0.0) - (SPREAD + comm_pips(entry))
    return res, p


def metrics(sigs, op, hi, lo, ts, tp_pips):
    pips = []; wins = 0; gw = gl = 0.0; cum = 0.0; peak = 0.0; dd = 0.0
    for s in sigs:
        res, p = resolve(s, op, hi, lo, ts, tp_pips)
        pips.append(p); wins += res == "TP"
        if p > 0: gw += p
        else: gl += -p
        cum += p; peak = max(peak, cum); dd = min(dd, cum - peak)
    n = len(pips)
    days = len(set(s["t"].date() for s in sigs)) or 1
    return {"n": n, "win": 100 * wins / n if n else 0, "perday": n / days,
            "total": sum(pips), "avg": sum(pips) / n if n else 0,
            "dd": dd, "pf": (gw / gl if gl else 99)}


def main():
    ts, op, hi, lo = load()
    grid = []
    for mode in ("fade", "momo"):
        for mv in (15, 25, 35, 50):
            for cd in (120, 240):
                sg = signals(ts, op, hi, lo, mv, cd, mode)
                if not sg: continue
                for tpn, tpp in ((1, 30), (2, 60), (3, 90), (4, 120)):
                    m = metrics(sg, op, hi, lo, ts, tpp)
                    grid.append((m["total"], mode, mv, cd, tpn, m))
    grid.sort(reverse=True, key=lambda x: x[0])
    print("═" * 78)
    print("MORNING OPTIMIZER (fade vs momentum) — Jan29–Aug31 2026, ranked by total pips")
    print("═" * 78)
    print(f"  {'mode':>5s} {'move':>4s} {'cd':>4s} {'TP':>3s} {'win%':>5s} {'/day':>5s} {'avg':>6s} {'total':>7s} {'maxDD':>7s} {'PF':>5s}")
    for tot, mode, mv, cd, tpn, m in grid[:14]:
        print(f"  {mode:>5s} {mv:4d} {cd:4d} TP{tpn} {m['win']:4.0f}% {m['perday']:5.2f} {m['avg']:+6.1f} {m['total']:+7.0f} {m['dd']:7.0f} {m['pf']:5.2f}")

    # winner detail
    tot, mode, mv, cd, tpn, m = grid[0]
    tpp = {1: 30, 2: 60, 3: 90, 4: 120}[tpn]
    sg = signals(ts, op, hi, lo, mv, cd, mode)
    print(f"\n  ── BEST: {mode}, move>={mv}p, cooldown {cd}m, exit TP{tpn} ──")
    mon = collections.defaultdict(float)
    for s in sg:
        _, p = resolve(s, op, hi, lo, ts, tpp); mon[s["t"].strftime("%Y-%m")] += p
    print("  month-by-month (pips):")
    for k in sorted(mon): print(f"    {k}: {mon[k]:+.0f}")
    tr = [s for s in sg if s["t"].date().isoformat() < SPLIT]
    te = [s for s in sg if s["t"].date().isoformat() >= SPLIT]
    mt = metrics(tr, op, hi, lo, ts, tpp); me = metrics(te, op, hi, lo, ts, tpp)
    print(f"\n  train (<{SPLIT}): {mt['win']:.0f}% win, {mt['perday']:.2f}/day, {mt['total']:+.0f} pips, PF {mt['pf']:.2f}")
    print(f"  test  (>={SPLIT}): {me['win']:.0f}% win, {me['perday']:.2f}/day, {me['total']:+.0f} pips, PF {me['pf']:.2f}")


if __name__ == "__main__":
    main()
