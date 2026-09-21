#!/usr/bin/env python3
"""
StreamTrade — Backtest Engine (@Raahimbfxproo XAUUSD signals).

Follows the channel's signals using their claimed outcomes (TP hits read from the
follow-up messages). Optionally counts DELETED trades as hidden losses, after
excluding his daily recruitment posts ("send HELP / account management").

HOW TO USE:  edit the CONFIG block below, then run:  ./venv/bin/python engine.py
"""

import re
import csv
import json
from types import SimpleNamespace

# ══════════════════════════════ CONFIG — EDIT THESE ══════════════════════════════
START_BALANCE = 100         # starting account, $
RISK_PCT      = 0.05        # risk per trade   (0.05 = 5%) — sized off CURRENT equity = compounding
LEVERAGE      = 500         # 1:500 — caps the max lots the margin allows
COST_PIPS     = 5.0         # round-trip spread + commission + slippage, in pips
SCALE_OUT     = True        # True = split position across the 5 TPs; False = exit all at TP1
SL_TO_BE      = True        # after TP1, the remainder exits at breakeven (0)
MIN_LOT       = 0.01

# --- how to treat DELETED posts ---
COUNT_DELETED_AS_LOSS = True   # False = ignore deletions entirely (signals-only view)
RECRUIT_PER_DAY       = 3.0    # recruitment deletions/day to EXCLUDE (NOT losses).
                               # Total deletion rate is ~1.6/day, so any value >= 1.6
                               # means "all deletions are recruitment" -> 0 hidden losses.
HIDDEN_LOSSES         = None   # set an int to FORCE the hidden-loss count; None = auto from RECRUIT_PER_DAY
# ═════════════════════════════════════════════════════════════════════════════════

CONTRACT_OZ = 100
PIP_VALUE_PER_LOT = 10.0    # $ per pip per lot (1pt = $1 = 10 pips; 1 pip = $0.10 -> $10/lot)


def load():
    msgs = sorted((json.loads(l) for l in open("messages.jsonl")), key=lambda m: m["id"])
    sigs = sorted((json.loads(l) for l in open("signals.jsonl")), key=lambda s: s["id"])
    sig_ids = [s["id"] for s in sigs]
    for k, s in enumerate(sigs):
        nxt = sig_ids[k + 1] if k + 1 < len(sig_ids) else 10**12
        mtp = 0
        for m in msgs:
            if s["id"] <= m["id"] < nxt:
                for n in re.findall(r'TP\s*(\d)\s*HIT', m["text"].upper()):
                    mtp = max(mtp, int(n))
        s["max_tp"] = min(mtp, len(s["tps"]))
    sigs.sort(key=lambda x: x["date"])
    return msgs, sigs


def deletion_stats(msgs):
    ids = sorted(m["id"] for m in msgs); present = set(ids)
    missing = [i for i in range(ids[0], ids[-1] + 1) if i not in present]
    runs, run = [], [missing[0]]
    for x in missing[1:]:
        if x == run[-1] + 1:
            run.append(x)
        else:
            runs.append(run); run = [x]
    runs.append(run)
    sizes = [len(r) for r in runs]
    return {"blocks": len(runs), "singletons": sum(1 for z in sizes if z == 1),
            "multi": sum(1 for z in sizes if z >= 2),
            "active_days": len({m["date"][:10] for m in msgs})}


def signal_pips(s, cfg):
    tp_pips = [abs(t - s["entry"]) * 10 for t in s["tps"]]
    sl_pips = abs(s["sl"] - s["entry"]) * 10
    k, n = s["max_tp"], len(s["tps"])
    if not cfg.scale_out:
        gross = tp_pips[0] if k >= 1 else -sl_pips
    elif k == 0:
        gross = -sl_pips
    else:
        gross = sum(tp_pips[j] / n for j in range(k)) + (n - k) / n * (0.0 if cfg.sl_to_be else -sl_pips)
    return gross - cfg.cost_pips


def simulate(sigs, cfg, hidden_losses=0):
    events = [("sig", s) for s in sigs]
    if hidden_losses > 0:
        step = len(sigs) / hidden_losses
        inject = {int(i * step) for i in range(hidden_losses)}
        merged = []
        for idx, ev in enumerate(events):
            if idx in inject:
                merged.append(("hidden", None))
            merged.append(ev)
        events = merged

    equity = cfg.start_balance; peak = equity; max_dd = 0.0
    wins = losses = 0; gw = gl = 0.0; log = []
    for kind, s in events:
        if kind == "hidden":
            pnl = -(equity * cfg.risk_pct)
            losses += 1; gl += pnl
            log.append({"date": "", "dir": "DELETED", "max_tp": "", "pips": "", "pnl": round(pnl, 2)})
        else:
            pips = signal_pips(s, cfg)
            sl_price = abs(s["sl"] - s["entry"])
            lots = max(cfg.min_lot, round((equity * cfg.risk_pct) / (sl_price * CONTRACT_OZ), 2)) if sl_price else cfg.min_lot
            pnl = pips * PIP_VALUE_PER_LOT * lots
            (wins, gw) = (wins + 1, gw + pnl) if pnl > 0 else (wins, gw)
            (losses, gl) = (losses + 1, gl + pnl) if pnl <= 0 else (losses, gl)
            log.append({"date": s["date"][:10], "dir": s["dir"], "max_tp": s["max_tp"], "pips": round(pips, 1), "pnl": round(pnl, 2)})
        equity += pnl
        peak = max(peak, equity); max_dd = min(max_dd, equity / peak - 1)
        log[-1]["equity"] = round(equity, 2)
    n = wins + losses
    return {"end": equity, "n": n, "wins": wins, "losses": losses, "gw": gw, "gl": gl, "max_dd": max_dd, "log": log}


def show(title, r, cfg):
    wr = 100 * r["wins"] / r["n"] if r["n"] else 0
    pf = r["gw"] / abs(r["gl"]) if r["gl"] else float("inf")
    print(f"\n  ── {title} ──")
    print(f"     trades {r['n']}  ·  win rate {wr:.0f}%  ·  PF {pf:.2f}  ·  maxDD {100*r['max_dd']:.0f}%")
    print(f"     balance ${cfg.start_balance:,.0f} → ${r['end']:,.0f}   ({100*(r['end']/cfg.start_balance-1):+.0f}%)")


def main():
    cfg = SimpleNamespace(start_balance=START_BALANCE, risk_pct=RISK_PCT, cost_pips=COST_PIPS,
                          scale_out=SCALE_OUT, sl_to_be=SL_TO_BE, min_lot=MIN_LOT)
    msgs, sigs = load()
    ds = deletion_stats(msgs)

    if HIDDEN_LOSSES is not None:
        hidden = HIDDEN_LOSSES; how = f"forced to {hidden}"
    else:
        recruit = round(RECRUIT_PER_DAY * ds["active_days"])
        hidden = max(0, ds["blocks"] - recruit)
        how = f"{ds['blocks']} blocks − {recruit} recruitment ({RECRUIT_PER_DAY}/day × {ds['active_days']}d) = {hidden}"

    print("═" * 66)
    print("STREAMTRADE BACKTEST ENGINE — @Raahimbfxproo")
    print("═" * 66)
    print(f"  config: ${START_BALANCE:.0f} · risk {RISK_PCT*100:.1f}% · scale-out={SCALE_OUT} · SL→BE={SL_TO_BE} · cost {COST_PIPS}p")
    print(f"  deletions: {ds['blocks']} blocks ({ds['singletons']} singletons / {ds['multi']} multi), {ds['active_days']} active days")
    print(f"  hidden losses: {how}")

    show("SIGNALS ONLY (their claimed record)", simulate(sigs, cfg, 0), cfg)
    if COUNT_DELETED_AS_LOSS:
        r1 = simulate(sigs, cfg, hidden)
        show(f"WITH {hidden} HIDDEN LOSSES (recruitment excluded)", r1, cfg)
        with open("trade_log.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "dir", "max_tp", "pips", "pnl", "equity"])
            w.writeheader(); w.writerows(r1["log"])
        print("\n  (per-trade log saved to trade_log.csv)")


if __name__ == "__main__":
    main()
