#!/usr/bin/env python3
"""
StreamTrade — claim-based backtest (no price data needed).

Rule (per user): EDITED = fine (trust the surviving signals' claimed outcomes),
DELETED = loss (the missing posts are hidden losers).

We:
  1. Read each surviving signal's CLAIMED outcome from its follow-up messages
     (max "TPn HIT", or a close/"wait for new signal" = scratch/loss).
  2. Estimate how many trades were DELETED (hidden losses) from the missing-id
     gaps — reported as a range, since we can't see deleted content.
  3. Combine into an honest win rate + P&L (pips), scale-out 1/5 per TP.
"""

import re
import json
import statistics as st

COST_PIPS = 5.0            # round-trip spread+commission+slippage
BE_AFTER_TP1 = True        # winners: remainder after last claimed TP exits ~breakeven


def load():
    msgs = [json.loads(l) for l in open("messages.jsonl")]
    sigs = [json.loads(l) for l in open("signals.jsonl")]
    return sorted(msgs, key=lambda m: m["id"]), sorted(sigs, key=lambda s: s["id"])


def claimed_outcomes(msgs, sigs):
    sig_ids = sorted(s["id"] for s in sigs)
    by_id = {s["id"]: s for s in sigs}
    # window of messages after each signal, up to the next signal
    out = {}
    for k, sid in enumerate(sig_ids):
        nxt = sig_ids[k + 1] if k + 1 < len(sig_ids) else 10**12
        window = [m for m in msgs if sid <= m["id"] < nxt]
        max_tp = 0
        closed = False
        for m in window:
            tu = m["text"].upper()
            for n in re.findall(r'TP\s*(\d)\s*HIT', tu):
                max_tp = max(max_tp, int(n))
            if "WAIT FOR NEW SIGNAL" in tu or re.search(r'\bCLOSE\b', tu) or "SL HIT" in tu:
                closed = True
        out[sid] = {"max_tp": max_tp, "closed": closed}
    return out


def deleted_analysis(msgs):
    ids = sorted(m["id"] for m in msgs)
    lo, hi = ids[0], ids[-1]
    present = set(ids)
    missing = [i for i in range(lo, hi + 1) if i not in present]
    # contiguous runs
    runs = []
    run = [missing[0]]
    for x in missing[1:]:
        if x == run[-1] + 1:
            run.append(x)
        else:
            runs.append(run); run = [x]
    runs.append(run)
    # footprint of a visible trade = median surviving-message span between signals
    return {"total_deleted": len(missing), "n_blocks": len(runs),
            "run_sizes": [len(r) for r in runs]}


def main():
    msgs, sigs = load()
    oc = claimed_outcomes(msgs, sigs)
    dl = deleted_analysis(msgs)

    # per-signal claimed pnl (pips), scale-out 1/5 per TP hit
    def sig_pips(s, o):
        tp_pips = [abs(t - s["entry"]) * 10 for t in s["tps"]]
        sl_pips = abs(s["sl"] - s["entry"]) * 10
        n = len(s["tps"])
        if o["max_tp"] >= 1:
            got = sum((1.0 / n) * tp_pips[k] for k in range(min(o["max_tp"], n)))
            # remainder rides to breakeven (they claim SL->BE after TP1)
            return got - COST_PIPS, True
        # no TP claimed -> a scratch/loss; if they said close/wait treat as SL loss
        return (-sl_pips - COST_PIPS), False

    claimed_win, claimed_loss = [], []
    for s in sigs:
        pips, won = sig_pips(s, oc[s["id"]])
        (claimed_win if won else claimed_loss).append(pips)

    n_sig = len(sigs)
    n_win = len(claimed_win)
    med_sl = st.median(abs(s["sl"] - s["entry"]) * 10 for s in sigs)

    # deleted-trade estimates (hidden losses):
    #   low  = count only multi-message blocks (>=2) as trades
    #   high = every deleted block is a hidden trade
    blocks = dl["run_sizes"]
    del_low = sum(1 for r in blocks if r >= 2)
    del_high = dl["n_blocks"]
    # volume-based: total deleted / typical winning-trade footprint
    footprint = max(2, round(dl["total_deleted"] / max(1, del_low)))  # rough
    del_mid = round(dl["total_deleted"] / 6)   # assume ~6 msgs per hidden trade

    print("═" * 64)
    print("CLAIM-BASED BACKTEST  (edited = fine, deleted = loss)")
    print("═" * 64)
    print(f"  surviving signals:            {n_sig}")
    print(f"    claimed wins (TP1+):        {n_win}")
    print(f"    visible losses/closes:      {n_sig - n_win}")
    print(f"  deleted messages:             {dl['total_deleted']}  in {dl['n_blocks']} blocks")
    print(f"  median SL: {med_sl:.0f} pips\n")

    surviving_pips = sum(claimed_win) + sum(claimed_loss)
    print("  ── Scenario: hidden (deleted) trades counted as SL losses ──")
    print(f"  {'hidden losses':>14} | {'total trades':>12} | {'win rate':>9} | {'net pips':>10} | {'avg/trade':>10}")
    for label, dtr in [("low  (blocks>=2)", del_low), ("mid  (~6 msg/trade)", del_mid),
                       ("high (all blocks)", del_high)]:
        total = n_sig + dtr
        wr = 100 * n_win / total
        net = surviving_pips + dtr * (-(med_sl + COST_PIPS))
        print(f"  {label:>18} | {total:>12} | {wr:>7.0f}% | {net:>+10.0f} | {net/total:>+10.1f}")

    print("\n  Their CLAIMED win rate (surviving only): "
          f"{100*n_win/n_sig:.0f}%  ->  honest win rate drops to "
          f"{100*n_win/(n_sig+del_mid):.0f}% once deletions count as losses.")
    print("\n  NOTE: this trusts their claimed TP hits. Stage 3 (price verification)")
    print("        will check whether those claimed wins were even real.")


if __name__ == "__main__":
    main()
