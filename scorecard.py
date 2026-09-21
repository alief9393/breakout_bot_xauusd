#!/usr/bin/env python3
"""
Live scorecard — reads the bots' logs and reports the ONLY numbers that decide
whether following the channel makes money: expectancy per trade and the SIZE of
wins vs losses (not just win rate). Run anytime: ./venv/bin/python scorecard.py
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
VAL_LOG = os.path.join(HERE, "validate_log.jsonl")
EXE_LOG = os.path.join(HERE, "execute_log.jsonl")


def load_closes(path):
    trades = []
    if not os.path.exists(path):
        return trades
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("event") == "PAPER_CLOSE":
            trades.append(r)
    return trades


def bar(x, width=24, lo=-30, hi=30):
    # simple $ bar around zero
    mid = width // 2
    n = int(round(max(lo, min(hi, x)) / hi * mid))
    if n >= 0:
        return " " * mid + "█" * n + " " * (mid - n)
    return " " * (mid + n) + "█" * (-n) + " " * mid


def report():
    tr = load_closes(VAL_LOG)
    print("=" * 74)
    print("STREAMTRADE SCORECARD — paper forward record (live_validate)")
    print("=" * 74)
    if not tr:
        print("no closed trades yet.")
        return
    wins = [t for t in tr if t.get("pnl_usd", 0) > 0]
    losses = [t for t in tr if t.get("pnl_usd", 0) <= 0]
    n = len(tr)
    tot = sum(t.get("pnl_usd", 0) for t in tr)
    aw = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
    al = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0
    wr = len(wins) / n
    exp = tot / n
    ratio = (aw / abs(al)) if al else float("inf")
    be_ratio = (1 - wr) / wr if wr else float("inf")     # win/loss size needed to break even

    print(f"\n  per-trade log ({n} trades):")
    print(f"  {'#':>3} {'date':>16} {'dir':>4} {'reason':>9} {'lots':>5} {'pnl$':>8}   {'-30      0      +30':>22}")
    eq = None
    peak = -1e9; mdd = 0
    for i, t in enumerate(tr, 1):
        p = t.get("pnl_usd", 0)
        eq = t.get("equity", eq)
        if eq is not None:
            peak = max(peak, eq); mdd = min(mdd, eq / peak - 1) if peak > 0 else mdd
        st = t.get("signal_time", t.get("t", ""))[:16]
        print(f"  {i:>3} {st:>16} {t.get('dir',''):>4} {t.get('reason',''):>9} "
              f"{t.get('lots',0):>5} {p:>+8.2f}   |{bar(p)}|")

    print("\n" + "-" * 74)
    print(f"  record:            {len(wins)}W / {len(losses)}L   ({100*wr:.0f}% win)")
    print(f"  avg WIN:           ${aw:+.2f}")
    print(f"  avg LOSS:          ${al:+.2f}")
    print(f"  win/loss size:     {ratio:.2f}   (need > {be_ratio:.2f} to break even at {100*wr:.0f}% win)")
    print(f"  EXPECTANCY/trade:  ${exp:+.3f}   <-- the number that decides it")
    print(f"  total P&L:         ${tot:+.2f}")
    if eq is not None:
        print(f"  equity:            ${eq:.2f}   (max drawdown {100*mdd:.0f}%)")

    print("\n  verdict:")
    if len(losses) == 0:
        print("    ⏳ NO LOSSES YET — expectancy not yet meaningful. The real test is the")
        print("       first loss: is it small (edge holds) or big (R/R bites)? Keep running.")
    elif exp > 0 and ratio >= be_ratio:
        print(f"    ✅ POSITIVE expectancy so far. Wins are large enough vs losses at this win rate.")
        print(f"       Still small sample — keep running to confirm it's not a streak.")
    else:
        print(f"    ⚠️  expectancy ~zero/negative — wins too small vs losses. Watch it, don't act yet.")

    # execute balance snapshot
    if os.path.exists(EXE_LOG):
        bal = None
        for line in open(EXE_LOG):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "balance" in r and r.get("balance") is not None:
                bal = r["balance"]
        if bal is not None:
            print(f"\n  live_execute demo balance (real orders): ${bal:.2f}")
    print("=" * 74)


if __name__ == "__main__":
    report()
