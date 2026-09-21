#!/usr/bin/env python3
"""
StreamTrade — STARTUP BACKFILL for the live validator.

When live_validate.py was down (laptop asleep, restart), signals posted that it
never tracked. This fills the gap: it pulls the channel's recent history, fetches
real cTrader M1 prices for that window, and resolves each missed signal with the
SAME scale-out rules as the live bot — appending the results to validate_log.jsonl
and updating the shared portfolio (validate_state.json).

HONEST LIMITATION: backfill can only see signals that still EXIST in the channel.
Any loser the channel deleted during the downtime is invisible here, so backfilled
rows carry "backfilled": true and "source":"history" — keep them separate from the
deletion-proof live rows when you judge the channel.

Run standalone:  ./venv/bin/python backfill.py
(live_validate.py runs it automatically on startup.)
"""

import os
import sys
import json
import time
import datetime
import asyncio

import live_validate as lv          # shared config/constants (import has no side effects)
import vstate
from parse import parse_signal

from telethon import TelegramClient
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *   # ProtoOATrendbarPeriod
from twisted.internet import reactor, defer

BACKFILL_SINCE = os.getenv("BACKFILL_SINCE", "2026-09-07")   # fixed anchor: resolve EVERY signal from this date
MAX_LOOK_MIN   = 60 * 48
PRICE_SCALE    = 100000.0


def backfill_cutoff():
    return datetime.datetime.fromisoformat(BACKFILL_SINCE).replace(tzinfo=datetime.timezone.utc)


def log(obj):
    line = {"t": datetime.datetime.utcnow().isoformat(timespec="seconds"), **obj}
    print(line, flush=True)
    try:
        with open(lv.LOG_FILE, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# Phase 3 (pure): resolve one signal against OHLC bars with scale-out
# ══════════════════════════════════════════════════════════════════════════
def find_start(ts, when):
    a, b = 0, len(ts)
    while a < b:
        m = (a + b) // 2
        if ts[m] < when: a = m + 1
        else: b = m
    return a if a < len(ts) else None


def resolve_one(sig, ts, op, hi, lo, i0, balance):
    """
    Market entry at the signal bar (mirrors live_validate: BUY/SELL NOW), absolute TP/SL.
    EXIT_MODE single/scale/dynamic — same exits as the live bot, bar-based.
    Returns (reason, gross_pips, net_pips, parts_hit, parts_n, sl_dist, entry, total_lots).
    """
    d = 1 if sig["dir"] == "BUY" else -1
    fill = i0
    entry = op[fill] + d * (lv.ENTRY_SLIP_PIPS / 10.0)
    ahead = [t for t in sig["tps"] if (t > entry if d == 1 else t < entry)]
    sl = sig["sl"]; sl_dist = abs(entry - sl)
    end = min(fill + MAX_LOOK_MIN, len(ts))
    comm = lv.COMMISSION_USD_PER_100K * (entry * lv.CONTRACT_OZ / 100000.0) / lv.PIP_VALUE_PER_LOT
    cost = lv.SPREAD_PIPS_FALLBACK + comm

    if lv.EXIT_MODE == "dynamic":
        if not ahead or sl_dist <= 0:
            return "untradeable", 0.0, 0.0, 0, 0, sl_dist, entry, None
        total = max(lv.MIN_LOT, min(round((balance * lv.RISK_PCT) / (sl_dist * lv.CONTRACT_OZ), 2), lv.MAX_LOT))
        idx, sizes = lv.plan_split(total, len(ahead))
        subs = [{"tgt": ahead[i], "lots": sizes[k], "open": True} for k, i in enumerate(idx)]
        stop = sl; banked = 0.0; hit = 0; reason = "timeout"
        for j in range(fill, end):
            if (lo[j] <= stop) if d == 1 else (hi[j] >= stop):
                for s in [x for x in subs if x["open"]]:
                    banked += ((stop - entry) * d * 10 - lv.SL_SLIP_PIPS) * (s["lots"] / total); s["open"] = False
                reason = "SL" if abs(stop - sl) < 1e-9 else "trail"; break
            for s in [x for x in subs if x["open"]]:
                if (hi[j] >= s["tgt"]) if d == 1 else (lo[j] <= s["tgt"]):
                    banked += (s["tgt"] - entry) * d * 10 * (s["lots"] / total); s["open"] = False; hit += 1
                    trail = s["tgt"] - d * (lv.BUFFER_PIPS / 10.0)
                    if (trail > stop) if d == 1 else (trail < stop): stop = trail
            if not [x for x in subs if x["open"]]:
                reason = "all_tps"; break
        return reason, round(banked, 1), round(banked - cost, 1), hit, len(subs), sl_dist, entry, total

    # single / scale (even parts)
    if lv.EXIT_MODE == "single":
        tps = [ahead[lv.TARGET_TP - 1]] if len(ahead) >= lv.TARGET_TP else []
    else:
        tps = ahead[:lv.SCALE_TO]
    n = len(tps)
    if n == 0:
        return "untradeable", 0.0, 0.0, 0, 0, sl_dist, entry, None
    stop = sl; hit = 0; banked = 0.0; reason = "timeout"
    for j in range(fill, end):
        if (lo[j] <= stop) if d == 1 else (hi[j] >= stop):
            rem = n - hit; at_be = abs(stop - entry) < 1e-9
            banked += rem / n * (0.0 if at_be else -(sl_dist * 10 + lv.SL_SLIP_PIPS))
            reason = "BE" if at_be else "SL"; break
        while hit < n and ((hi[j] >= tps[hit]) if d == 1 else (lo[j] <= tps[hit])):
            banked += abs(tps[hit] - entry) * 10 / n; hit += 1
            if lv.EXIT_MODE == "scale" and lv.SL_TO_BE and hit >= lv.BE_AFTER_TP:
                stop = entry
        if hit == n:
            reason = "all_tps"; break
    return reason, round(banked, 1), round(banked - cost, 1), hit, n, sl_dist, entry, None


def resolve_all(signals, ts, op, hi, lo, state):
    """Resolve every not-yet-processed signal chronologically, updating the portfolio."""
    closed = 0
    for sig in sorted(signals, key=lambda s: s["date"]):
        if sig["id"] in state["processed_ids"]:
            continue
        when = sig["date"] if isinstance(sig["date"], datetime.datetime) \
            else datetime.datetime.fromisoformat(sig["date"]).replace(tzinfo=None)
        i0 = find_start(ts, when)
        if i0 is None:
            continue                                  # no price data covering this signal
        reason, gross, net, hit, n, sl_dist, entry, total_lots = resolve_one(sig, ts, op, hi, lo, i0, state["equity"])
        if reason == "untradeable":
            state["processed_ids"].add(sig["id"])
            log({"event": "signal_untradeable", "backfilled": True, "source": "history",
                 "mid": sig["id"], "signal_time": when.isoformat(timespec="minutes"),
                 "dir": sig["dir"], "entry": round(entry, 2),
                 "note": "market ran past all TPs before entry"})
            continue
        if sl_dist <= 0:
            continue
        lots = total_lots if total_lots is not None else \
            max(lv.MIN_LOT * n, round((state["equity"] * lv.RISK_PCT) / (sl_dist * lv.CONTRACT_OZ), 2))
        pnl = net * lv.PIP_VALUE_PER_LOT * lots
        vstate.apply_pnl(state, pnl)
        state["processed_ids"].add(sig["id"])
        rec = f"{state['wins']}W/{state['losses']}L"
        log({"event": "PAPER_CLOSE", "backfilled": True, "source": "history",
             "mid": sig["id"], "signal_time": when.isoformat(timespec="minutes"),
             "dir": sig["dir"], "entry": round(entry, 2), "reason": reason,
             "parts_banked": hit, "parts_total": n,
             "gross_pips": gross, "net_pips": net, "lots": lots, "pnl_usd": round(pnl, 2),
             "equity": round(state["equity"], 2), "record": rec})
        closed += 1
    return closed


# ══════════════════════════════════════════════════════════════════════════
# Phase 1: recent channel signals (Telethon / asyncio)
# ══════════════════════════════════════════════════════════════════════════
async def pull_recent_signals():
    # same hardening as the live stream: ride out transient Telegram RPC hiccups
    client = TelegramClient(lv.SESSION, lv.API_ID, lv.API_HASH,
                            connection_retries=None, auto_reconnect=True,
                            retry_delay=5, request_retries=5)
    # get_me()/get_entity can throw RpcCallFailError ("Telegram is having internal issues");
    # retry a few times before giving up (backfill is optional — live still starts either way)
    entity = None
    for attempt in range(1, 6):
        try:
            await client.start()
            entity = await client.get_entity(lv.CHANNEL)
            break
        except Exception as e:
            log({"event": "backfill_retry", "attempt": attempt, "error": repr(e)[:120]})
            await asyncio.sleep(5 * attempt)
    if entity is None:
        await client.disconnect()
        raise RuntimeError("Telegram unreachable after retries")
    cutoff = backfill_cutoff()
    out = []
    async for m in client.iter_messages(entity):          # newest-first
        if m.date and m.date < cutoff:
            break
        sig = parse_signal(m.message or "")
        if sig and len(sig["tps"]) >= 1:
            out.append({"id": m.id, "date": m.date.replace(tzinfo=None), **sig})
    await client.disconnect()
    return out


# ══════════════════════════════════════════════════════════════════════════
# Phase 2: recent M1 bars (cTrader / Twisted, runs once then stops)
# ══════════════════════════════════════════════════════════════════════════
def fetch_recent_bars(days):
    host = EndPoints.PROTOBUF_DEMO_HOST if lv.HOST_TYPE == "demo" else EndPoints.PROTOBUF_LIVE_HOST
    client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
    result = {"bars": {}, "error": None}
    st = {"acc": lv.ACCOUNT_ID, "sym": None}

    def send(r, t=20):
        return client.send(r, responseTimeoutInSeconds=t)

    def reconstruct(b):
        low = b.low
        o = (low + b.deltaOpen) / PRICE_SCALE
        h = (low + b.deltaHigh) / PRICE_SCALE
        c = (low + b.deltaClose) / PRICE_SCALE
        l = low / PRICE_SCALE
        ts = datetime.datetime.utcfromtimestamp(b.utcTimestampInMinutes * 60)
        return (ts, o, h, l, c)

    @defer.inlineCallbacks
    def go():
        try:
            r = ProtoOAApplicationAuthReq(); r.clientId = lv.CLIENT_ID; r.clientSecret = lv.CLIENT_SECRET
            yield send(r)
            r = ProtoOAGetAccountListByAccessTokenReq(); r.accessToken = lv.ACCESS_TOKEN
            accs = Protobuf.extract((yield send(r))).ctidTraderAccount
            if st["acc"] == 0:
                st["acc"] = ([a for a in accs if not a.isLive] or accs)[0].ctidTraderAccountId
            r = ProtoOAAccountAuthReq(); r.ctidTraderAccountId = st["acc"]; r.accessToken = lv.ACCESS_TOKEN
            yield send(r)
            r = ProtoOASymbolsListReq(); r.ctidTraderAccountId = st["acc"]
            syms = Protobuf.extract((yield send(r))).symbol
            st["sym"] = [s for s in syms if s.symbolName.upper() == lv.SYMBOL_NAME.upper()][0].symbolId

            now_ms = int(time.time() * 1000)
            start_ms = now_ms - int(days * 24 * 3600 * 1000)
            to_ts = now_ms
            window_ms = 24 * 3600 * 1000                    # 1 day per request
            while to_ts > start_ms:
                from_ts = max(start_ms, to_ts - window_ms)
                req = ProtoOAGetTrendbarsReq()
                req.ctidTraderAccountId = st["acc"]; req.symbolId = st["sym"]
                req.period = ProtoOATrendbarPeriod.M1
                req.fromTimestamp = from_ts; req.toTimestamp = to_ts
                res = Protobuf.extract((yield send(req)))
                tbs = res.trendbar
                if not tbs:
                    break
                earliest = min(b.utcTimestampInMinutes for b in tbs)
                for b in tbs:
                    result["bars"][b.utcTimestampInMinutes] = reconstruct(b)
                new_to = earliest * 60 * 1000 - 60 * 1000
                if new_to >= to_ts:
                    break
                to_ts = new_to
        except Exception as e:
            result["error"] = repr(e)
        finally:
            if reactor.running:
                reactor.stop()

    def on_connected(_):
        go()

    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(lambda *_: None)
    client.startService()
    reactor.run(installSignalHandlers=False)

    rows = [result["bars"][k] for k in sorted(result["bars"])]
    ts = [r[0] for r in rows]; op = [r[1] for r in rows]
    hi = [r[2] for r in rows]; lo = [r[3] for r in rows]
    return ts, op, hi, lo, result["error"]


def main():
    log({"event": "backfill_start", "since": BACKFILL_SINCE, "channel": lv.CHANNEL})
    state = vstate.load(lv.BALANCE0)
    try:
        signals = asyncio.run(pull_recent_signals())
    except Exception as e:
        log({"event": "backfill_error", "phase": "telegram", "error": repr(e)})
        return
    fresh = [s for s in signals if s["id"] not in state["processed_ids"]]
    log({"event": "backfill_signals", "found": len(signals), "new": len(fresh)})
    if not fresh:
        log({"event": "backfill_done", "closed": 0, "note": "nothing to backfill"})
        vstate.save(state)
        return
    span_days = (datetime.datetime.now(datetime.timezone.utc) - backfill_cutoff()).days + 2  # +margin to resolve
    ts, op, hi, lo, err = fetch_recent_bars(span_days)
    if err or not ts:
        log({"event": "backfill_error", "phase": "prices", "error": err or "no bars"})
        return
    closed = resolve_all(fresh, ts, op, hi, lo, state)
    vstate.save(state)
    log({"event": "backfill_done", "closed": closed,
         "equity": round(state["equity"], 2), "record": f"{state['wins']}W/{state['losses']}L"})


if __name__ == "__main__":
    main()
