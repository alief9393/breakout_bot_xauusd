#!/usr/bin/env python3
"""
StreamTrade — LIVE FORWARD-VALIDATION (paper), hardened for 24/5.

Why this exists
───────────────
The backtest can be gamed by a channel that DELETES its losing signals — we only
ever see the survivors. This bot defeats that: the moment a signal posts, it grabs
the REAL market price from cTrader, opens a PAPER trade (no order sent), and follows
the live price tick-by-tick until every scale-out part closes at a TP or the stop.
Because we capture and resolve each trade in real time, the channel can delete the
loser afterwards all it wants — we already recorded it and tracked it to the stop.

What it logs per signal
  * stated entry vs the actual market price at signal time (does the entry make sense?)
  * the real spread at that moment (validates our cost assumption)
  * every scale-out part as it banks a TP, and the final paper P&L (pips + $)

It does NOT place orders. It is a truth-meter for the channel, run forward in time.

Architecture
  * cTrader price feed runs on Twisted in a BACKGROUND THREAD, publishing the latest
    bid/ask to a shared, lock-guarded dict.
  * Telethon streams the channel on asyncio in the MAIN thread.
  * An asyncio ticker (1s) advances every open paper trade against the shared price.

Run (macOS, stays awake):
    caffeinate -i ./venv/bin/python live_validate.py
  Stop live.py first — they share the same Telegram session.
"""

import os
import re
import sys
import json
import math
import time
import threading
import datetime
import asyncio
import subprocess

from dotenv import load_dotenv
from telethon import TelegramClient, events

from parse import parse_signal
import vstate

# cTrader (Twisted) — imported for the price-feed thread
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from twisted.internet import reactor, defer

load_dotenv()

# ── Telegram ──
API_ID   = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
CHANNEL  = os.getenv("TG_CHANNEL", "@Raahimbfxproo")   # the channel we're validating
SESSION  = os.getenv("TG_SESSION", "streamtrade")

# ── cTrader ──
CLIENT_ID     = os.getenv("CTRADER_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET", "")
ACCESS_TOKEN  = os.getenv("CTRADER_ACCESS_TOKEN", "")
HOST_TYPE     = os.getenv("CTRADER_HOST_TYPE", "demo")
ACCOUNT_ID    = int(os.getenv("CTRADER_ACCOUNT_ID", "0"))
SYMBOL_NAME   = os.getenv("CTRADER_SYMBOL", "XAUUSD")
PRICE_SCALE   = 100000.0   # cTrader Open API prices are integers × 1e5

# ── strategy (paper) — mirrors the backtests ──
EXIT_MODE   = "dynamic"    # "single" · "scale" · "dynamic" (size-based split + trail-behind-TP)
TARGET_TP   = 4            # single mode: exit the whole position at this TP
SCALE_TO    = 5            # scale mode: scale across TP1..TP{SCALE_TO}
SL_TO_BE    = True         # scale mode: move stop to breakeven once enough parts have banked
BE_AFTER_TP = 2            # scale mode: arm breakeven only AFTER this many TPs bank
# dynamic mode (bt_dynamic_split): split by size, trail behind each hit TP
SPLIT2      = 0.02         # calc lot >= this -> 2 positions (aim TP3, TP4)
SPLIT4      = 0.08         # calc lot >= this -> 4 positions (aim TP1..TP4)
TARGET_MAP  = {1: [1], 2: [1, 2], 4: [0, 1, 2, 3]}  # 1->TP2 · 2->TP2+TP3 · 4->TP1..TP4
BUFFER_PIPS = 5            # trail the stop this far behind a hit TP
# early-alert entry + chase guard (mirrors bt_dynamic_split's EARLY_ALERT_ENTRY)
EARLY_ALERT_ENTRY = True   # enter at the bare "BUY/SELL NOW" alert price when one precedes the details
ALERT_WINDOW_MIN  = 5      # match a details post to a NOW-alert within this many minutes before it
MAX_CHASE_PIPS    = 20     # (no-alert path) skip a signal if price already ran this far past its entry
MAX_LOT     = 100.0
BALANCE0    = 100.0       # paper account for the $ equity curve
RISK_PCT    = 0.30         # risk per trade (sane, comparable to the backtest)
CONTRACT_OZ = 100
PIP_VALUE_PER_LOT = 10.0
MIN_LOT     = 0.01

# costs (pips) — same as the backtests
SPREAD_PIPS_FALLBACK = 1.0     # used only if we can't read a live spread
COMMISSION_USD_PER_100K = 6.0  # cTrader gold: $3/side = $6 round-turn per $100k notional (price-based)
ENTRY_SLIP_PIPS      = 1.0
SL_SLIP_PIPS         = 2.0

TRADE_TIMEOUT_MIN = 60 * 48    # abandon a paper trade unresolved after this long
STALE_PRICE_SEC   = 30         # don't open trades if the feed hasn't ticked this recently
HEARTBEAT_MIN     = 30
TICK_SEC          = 1.0
LOG_FILE          = "validate_log.jsonl"
ARCHIVE_FILE      = "channel_archive.jsonl"   # append-only copy of EVERY channel msg (survives their deletes)

RUN_BACKFILL_ON_START = os.getenv("VALIDATE_BACKFILL", "1") == "1"   # set VALIDATE_BACKFILL=0 to skip (avoids cTrader connect clash with live_execute)
BACKFILL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill.py")

# ══════════════════════════════════════════════════════════════════════════
# Shared price state (written by the Twisted thread, read by asyncio)
# ══════════════════════════════════════════════════════════════════════════
_price_lock = threading.Lock()
_price = {"bid": None, "ask": None, "t": None}     # t = time.time() of last tick


def publish_price(bid=None, ask=None):
    with _price_lock:
        if bid is not None:
            _price["bid"] = bid
        if ask is not None:
            _price["ask"] = ask
        _price["t"] = time.time()


def read_price():
    with _price_lock:
        return dict(_price)


def utcnow():
    return datetime.datetime.utcnow()


def log(obj):
    line = {"t": utcnow().isoformat(timespec="seconds"), **obj}
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


def archive(mid_id, posted_iso, text):
    """Keep our own copy of every channel message so a later deletion can't hide it."""
    try:
        with open(ARCHIVE_FILE, "a") as f:
            f.write(json.dumps({"id": mid_id, "posted": posted_iso,
                                "seen": utcnow().isoformat(timespec="seconds"),
                                "text": text}) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# Paper trade with scale-out (tick-driven)
# ══════════════════════════════════════════════════════════════════════════
def plan_split(total, avail):
    """Size-based split -> (target indices into ahead, sub_lots). Mirrors bt_dynamic_split."""
    units = max(1, round(total / 0.01))
    ndes = 4 if total >= SPLIT4 else 2 if total >= SPLIT2 else 1
    n = min(ndes, avail, units)
    base_idx = TARGET_MAP.get(n, list(range(n)))
    idx = sorted(set(min(i, avail - 1) for i in base_idx))
    n = len(idx); base = units // n; rem = units - base * n
    sizes = [(base + (1 if k < rem else 0)) * 0.01 for k in range(n)]
    return idx, sizes


_ALERT_BAD = re.compile(r'PROFIT|RUNNING|PIPS|EXPECT|GAP|HIT|DONE|✅', re.I)


def alert_dir(text):
    """A bare 'GOLD BUY/SELL NOW' alert (no levels) -> its direction, else None."""
    tu = (text or "").upper()
    if _ALERT_BAD.search(text or "") or "TP" in tu:
        return None
    if re.search(r'\bNOW\b', tu) and re.search(r'\b(BUY|SELL)\b', tu):
        return "BUY" if "BUY" in tu else "SELL"
    return None


class PaperTrade:
    """
    Enters at the live mid when the signal posts (market, 'BUY/SELL NOW'), absolute TP/SL.
    EXIT_MODE: 'single' (whole at TP{TARGET_TP}), 'scale' (even parts + BE), or 'dynamic'
    (size-based split into 1/2/4 sub-positions, uneven lots, stop trails behind each hit TP).
    Net pips are position-weighted; book_result multiplies by the total lots.
    """
    def __init__(self, mid, spread_pips, sig, opened_at, balance, entry_override=None):
        self.d = 1 if sig["dir"] == "BUY" else -1
        self.stated_entry = sig["entry"]
        # entry_override = price captured at an earlier NOW-alert; else the current live mid
        base = entry_override if entry_override is not None else mid
        self.early = entry_override is not None
        self.entry = base + self.d * (ENTRY_SLIP_PIPS / 10.0)
        self.sl = sig["sl"]; self.sl_dist = abs(self.entry - self.sl)
        self.spread_pips = spread_pips; self.opened_at = opened_at; self.opened_mono = time.time()
        self.stop = self.sl; self.banked_pips = 0.0; self.closed = False; self.result = None
        self.hit = 0; self.total_lots = None; self.tps = None; self.subs = None
        ahead = [t for t in sig["tps"] if (t > self.entry if self.d == 1 else t < self.entry)]
        if EXIT_MODE == "single":
            self.tps = [ahead[TARGET_TP - 1]] if len(ahead) >= TARGET_TP else []
            self.n = len(self.tps)
        elif EXIT_MODE == "dynamic":
            if not ahead or self.sl_dist <= 0:
                self.subs = []; self.n = 0
            else:
                total = max(MIN_LOT, min(round((balance * RISK_PCT) / (self.sl_dist * CONTRACT_OZ), 2), MAX_LOT))
                self.total_lots = total
                idx, sizes = plan_split(total, len(ahead))
                self.subs = [{"tgt": ahead[i], "lots": sizes[k], "open": True} for k, i in enumerate(idx)]
                self.n = len(self.subs)
        else:  # scale
            self.tps = ahead[:SCALE_TO]; self.n = len(self.tps)

    def _adverse(self, px):
        return (px <= self.stop) if self.d == 1 else (px >= self.stop)

    def update(self, mid):
        """Advance one tick. Returns a close-dict when fully resolved, else None."""
        if self.closed:
            return None
        if (time.time() - self.opened_mono) > TRADE_TIMEOUT_MIN * 60:
            return self._close("timeout", mid)     # any still-open parts left flat
        if EXIT_MODE == "dynamic":
            return self._update_dynamic(mid)
        # single / scale (even parts)
        if self._adverse(mid):
            rem = self.n - self.hit
            at_be = abs(self.stop - self.entry) < 1e-9
            loss = 0.0 if at_be else -(self.sl_dist * 10 + SL_SLIP_PIPS)
            self.banked_pips += (rem / self.n * loss) if self.n else 0.0
            return self._close("BE" if at_be else "SL", mid)
        while self.hit < self.n and ((mid >= self.tps[self.hit]) if self.d == 1 else (mid <= self.tps[self.hit])):
            self.banked_pips += abs(self.tps[self.hit] - self.entry) * 10 / self.n
            self.hit += 1
            if EXIT_MODE == "scale" and SL_TO_BE and self.hit >= BE_AFTER_TP:
                self.stop = self.entry
            if self.hit == self.n:
                return self._close("all_tps", mid)
        return None

    def _update_dynamic(self, mid):
        openn = [s for s in self.subs if s["open"]]
        if not openn:
            return self._close("done", mid)
        if self._adverse(mid):                     # stop hit -> close all remaining at the stop
            for s in openn:
                frac = s["lots"] / self.total_lots
                pips = (self.stop - self.entry) * self.d * 10 - SL_SLIP_PIPS
                self.banked_pips += pips * frac; s["open"] = False
            return self._close("SL" if abs(self.stop - self.sl) < 1e-9 else "trail", mid)
        for s in openn:                            # bank any sub whose TP the price reached
            if (mid >= s["tgt"]) if self.d == 1 else (mid <= s["tgt"]):
                frac = s["lots"] / self.total_lots
                self.banked_pips += (s["tgt"] - self.entry) * self.d * 10 * frac
                s["open"] = False; self.hit += 1
                trail = s["tgt"] - self.d * (BUFFER_PIPS / 10.0)      # trail stop behind this TP
                if (trail > self.stop) if self.d == 1 else (trail < self.stop):
                    self.stop = trail
        if not [s for s in self.subs if s["open"]]:
            return self._close("all_tps", mid)
        return None

    def _close(self, reason, mid):
        self.closed = True
        gross = self.banked_pips
        comm = COMMISSION_USD_PER_100K * (self.entry * CONTRACT_OZ / 100000.0) / PIP_VALUE_PER_LOT
        net = gross - (self.spread_pips + comm)
        self.result = {"reason": reason, "gross_pips": round(gross, 1),
                       "net_pips": round(net, 1), "parts_banked": self.hit,
                       "parts_total": self.n, "exit_mid": round(mid, 2)}
        return self.result


# ══════════════════════════════════════════════════════════════════════════
# cTrader price feed (Twisted, background thread)
# ══════════════════════════════════════════════════════════════════════════
SPOT_EVENT_PT = ProtoOASpotEvent().payloadType
_ct_state = {"account_id": ACCOUNT_ID, "symbol_id": None}


def start_price_feed():
    host = EndPoints.PROTOBUF_DEMO_HOST if HOST_TYPE == "demo" else EndPoints.PROTOBUF_LIVE_HOST
    client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

    def send(req, timeout=20):
        return client.send(req, responseTimeoutInSeconds=timeout)

    @defer.inlineCallbacks
    def authenticate_and_subscribe():
        try:
            r = ProtoOAApplicationAuthReq(); r.clientId = CLIENT_ID; r.clientSecret = CLIENT_SECRET
            yield send(r)
            r = ProtoOAGetAccountListByAccessTokenReq(); r.accessToken = ACCESS_TOKEN
            accts = Protobuf.extract((yield send(r))).ctidTraderAccount
            if _ct_state["account_id"] == 0:
                chosen = ([a for a in accts if not a.isLive] or accts)[0]
                _ct_state["account_id"] = chosen.ctidTraderAccountId
            r = ProtoOAAccountAuthReq(); r.ctidTraderAccountId = _ct_state["account_id"]; r.accessToken = ACCESS_TOKEN
            yield send(r)
            r = ProtoOASymbolsListReq(); r.ctidTraderAccountId = _ct_state["account_id"]
            symbols = Protobuf.extract((yield send(r))).symbol
            match = [s for s in symbols if s.symbolName.upper() == SYMBOL_NAME.upper()]
            if not match:
                log({"event": "feed_error", "error": f"symbol {SYMBOL_NAME} not found"}); return
            _ct_state["symbol_id"] = match[0].symbolId
            r = ProtoOASubscribeSpotsReq()
            r.ctidTraderAccountId = _ct_state["account_id"]
            r.symbolId.append(_ct_state["symbol_id"])
            yield send(r)
            log({"event": "feed_online", "symbol": SYMBOL_NAME, "symbol_id": _ct_state["symbol_id"],
                 "account": _ct_state["account_id"], "host": HOST_TYPE})
        except Exception as e:
            log({"event": "feed_error", "error": repr(e), "note": "will retry on reconnect"})

    def on_message(_, message):
        if message.payloadType != SPOT_EVENT_PT:
            return
        ev = Protobuf.extract(message)
        bid = ev.bid / PRICE_SCALE if ev.HasField("bid") else None
        ask = ev.ask / PRICE_SCALE if ev.HasField("ask") else None
        if bid is not None or ask is not None:
            publish_price(bid, ask)

    def on_connected(_):
        log({"event": "feed_connected", "note": "authenticating + subscribing"})
        authenticate_and_subscribe()

    def on_disconnected(_, reason):
        log({"event": "feed_disconnected", "reason": str(reason), "note": "reconnecting in 5s"})
        reactor.callLater(5, client.startService)

    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(on_disconnected)
    client.setMessageReceivedCallback(on_message)
    client.startService()
    reactor.run(installSignalHandlers=False)     # required: reactor in a non-main thread


# ══════════════════════════════════════════════════════════════════════════
# Paper portfolio + reporting
# ══════════════════════════════════════════════════════════════════════════
STATE = vstate.load(BALANCE0)   # persistent portfolio (equity, W/L, processed ids) — shared with backfill
OPEN = []                       # active PaperTrades this session (not persisted)


def book_result(pt):
    lots = getattr(pt, "total_lots", None) or (
        max(MIN_LOT * pt.n, round((STATE["equity"] * RISK_PCT) / (pt.sl_dist * CONTRACT_OZ), 2))
        if pt.sl_dist > 0 else MIN_LOT * max(pt.n, 1))
    pnl = pt.result["net_pips"] * PIP_VALUE_PER_LOT * lots
    vstate.apply_pnl(STATE, pnl)
    if getattr(pt, "mid", None) is not None:
        STATE["processed_ids"].add(pt.mid)
    vstate.save(STATE)          # persist immediately so a crash can't lose the record
    n = STATE["wins"] + STATE["losses"]
    log({"event": "PAPER_CLOSE", "source": "live", "mid": getattr(pt, "mid", None), **pt.result,
         "lots": lots, "pnl_usd": round(pnl, 2),
         "equity": round(STATE["equity"], 2),
         "record": f"{STATE['wins']}W/{STATE['losses']}L",
         "win_rate": f"{100*STATE['wins']/n:.0f}%" if n else "n/a",
         "max_dd": f"{100*STATE['max_dd']:.0f}%"})


# ══════════════════════════════════════════════════════════════════════════
# Async: signal stream + tick loop
# ══════════════════════════════════════════════════════════════════════════
async def ticker():
    while True:
        await asyncio.sleep(TICK_SEC)
        p = read_price()
        if p["bid"] is None or p["ask"] is None:
            continue
        mid = (p["bid"] + p["ask"]) / 2
        still = []
        for pt in OPEN:
            res = pt.update(mid)
            if res is None:
                still.append(pt)
            else:
                book_result(pt)
        OPEN[:] = still


async def heartbeat():
    while True:
        await asyncio.sleep(HEARTBEAT_MIN * 60)
        p = read_price()
        fresh = p["t"] and (time.time() - p["t"] < STALE_PRICE_SEC)
        log({"event": "heartbeat", "open_trades": len(OPEN),
             "equity": round(STATE["equity"], 2),
             "record": f"{STATE['wins']}W/{STATE['losses']}L",
             "price_fresh": bool(fresh),
             "last_mid": round((p["bid"] + p["ask"]) / 2, 2) if p["bid"] and p["ask"] else None})


async def run_stream():
    client = TelegramClient(SESSION, API_ID, API_HASH,
                            connection_retries=None, auto_reconnect=True,
                            retry_delay=5, request_retries=5)
    await client.start()
    me = await client.get_me()
    entity = await client.get_entity(CHANNEL)
    seen = set(STATE["processed_ids"])        # don't re-trade anything backfill already resolved
    gapwatch = {"max": max(seen) if seen else 0}   # highest channel msg-id seen (for delete/gap detection)
    pending_alerts = []      # bare "NOW" alerts awaiting their details post: {dir, mid, mono, id}
    strat = {"single": f"single TP{TARGET_TP}", "scale": f"scale→TP{SCALE_TO} (BE@{BE_AFTER_TP})",
             "dynamic": f"dynamic-split (buffer {BUFFER_PIPS}p)"}.get(EXIT_MODE, EXIT_MODE)
    log({"event": "online", "user": me.username, "channel": getattr(entity, "title", CHANNEL),
         "mode": "PAPER forward-validation", "strategy": strat,
         "sizing": f"${BALANCE0:.0f}@{RISK_PCT*100:.0f}%",
         "resumed_equity": round(STATE["equity"], 2),
         "resumed_record": f"{STATE['wins']}W/{STATE['losses']}L"})

    @client.on(events.NewMessage(chats=entity))
    async def on_new(event):
        try:
            mid_id = event.message.id
            if mid_id in seen:
                return
            seen.add(mid_id)
            text = event.message.message or ""
            posted = (event.message.date.isoformat(timespec="seconds")
                      if event.message.date else utcnow().isoformat(timespec="seconds"))
            archive(mid_id, posted, text)                      # keep our own copy before they can delete it
            if gapwatch["max"] and mid_id > gapwatch["max"] + 1:   # channel skipped ids -> deleted msg(s)
                missing = list(range(gapwatch["max"] + 1, mid_id))
                log({"event": "channel_gap", "missing_ids": missing, "count": len(missing),
                     "note": "message id(s) skipped — deleted or edited-away by channel"})
            gapwatch["max"] = max(gapwatch["max"], mid_id)
            sig = parse_signal(text)

            # --- early-alert entry: a bare "BUY/SELL NOW" (no levels) captures the early price ---
            adir = alert_dir(text)
            if EARLY_ALERT_ENTRY and adir and (not sig or len(sig.get("tps", [])) < 1):
                p = read_price()
                if p["bid"] is not None and p["ask"] is not None and p["t"] and (time.time() - p["t"] <= STALE_PRICE_SEC):
                    amid = (p["bid"] + p["ask"]) / 2
                    pending_alerts.append({"dir": adir, "mid": amid, "mono": time.time(), "id": mid_id})
                    log({"event": "ALERT_ENTRY", "source": "live", "mid": mid_id, "dir": adir,
                         "captured_price": round(amid, 2),
                         "note": "bare NOW-alert — holding early entry price for the upcoming details post"})
                else:
                    log({"event": "ALERT_NO_PRICE", "dir": adir, "mid": mid_id,
                         "note": "alert seen but price feed stale — cannot capture early entry"})
                return

            if not sig:
                return
            if len(sig["tps"]) < 1:
                return
            p = read_price()
            if p["bid"] is None or p["ask"] is None or not p["t"] or (time.time() - p["t"] > STALE_PRICE_SEC):
                log({"event": "signal_no_price", "dir": sig["dir"], "entry": sig["entry"],
                     "note": "price feed stale/unavailable — logged but NOT paper-traded", "text": text[:90]})
                return
            mid = (p["bid"] + p["ask"]) / 2
            spread_pips = round((p["ask"] - p["bid"]) * 10, 2)
            gap_pips = round(abs(mid - sig["entry"]) * 10, 1)     # stated entry vs real market

            # match a preceding NOW-alert (same dir, within window) -> enter at its captured price
            entry_override = None
            if EARLY_ALERT_ENTRY:
                now_mono = time.time()
                pending_alerts[:] = [a for a in pending_alerts if (now_mono - a["mono"]) <= ALERT_WINDOW_MIN * 60]
                cands = [a for a in pending_alerts if a["dir"] == sig["dir"]]
                if cands:
                    match = max(cands, key=lambda a: a["mono"])
                    entry_override = match["mid"]
                    pending_alerts.remove(match)

            # chase guard — only when we did NOT get an early-alert entry
            if entry_override is None:
                d = 1 if sig["dir"] == "BUY" else -1
                chase = (mid - sig["entry"]) * d * 10      # + = price already ran past entry in our favor
                if chase > MAX_CHASE_PIPS:
                    STATE["processed_ids"].add(mid_id); vstate.save(STATE)
                    log({"event": "signal_rejected_chase", "source": "live", "mid": mid_id, "dir": sig["dir"],
                         "stated_entry": sig["entry"], "market_mid": round(mid, 2),
                         "chase_pips": round(chase, 1), "limit": MAX_CHASE_PIPS,
                         "note": "price already ran past entry — not chasing a late fill"})
                    return

            pt = PaperTrade(mid, spread_pips or SPREAD_PIPS_FALLBACK, sig, utcnow(), STATE["equity"],
                            entry_override=entry_override)
            pt.mid = mid_id
            if pt.n == 0:                         # entry already past every TP — untradeable, log & skip
                STATE["processed_ids"].add(mid_id); vstate.save(STATE)
                log({"event": "signal_untradeable", "source": "live", "mid": mid_id, "dir": sig["dir"],
                     "stated_entry": sig["entry"], "market_mid": round(mid, 2),
                     "entry_gap_pips": gap_pips, "note": "market ran past all TPs before entry"})
                return
            OPEN.append(pt)
            log({"event": "PAPER_OPEN", "source": "live", "mid": mid_id, "dir": sig["dir"],
                 "entry_mode": "early-alert" if pt.early else "details",
                 "effective_entry": round(pt.entry, 2),
                 "stated_entry": sig["entry"], "market_mid": round(mid, 2),
                 "entry_gap_pips": gap_pips, "live_spread_pips": spread_pips,
                 "sl": sig["sl"], "tps_used": pt.tps, "parts": pt.n,
                 "sane": gap_pips <= 30})       # >30 pips off = entry doesn't match market
        except Exception as e:
            log({"event": "handler_error", "error": repr(e)})

    @client.on(events.MessageDeleted(chats=entity))
    async def on_deleted(event):
        # Fires the moment the channel deletes messages (while we're connected).
        # We look up our archive so the deleted TEXT is preserved, not just the id.
        try:
            ids = list(event.deleted_ids or [])
            if not ids:
                return
            recovered = {}
            try:
                with open(ARCHIVE_FILE) as f:
                    for ln in f:
                        r = json.loads(ln)
                        if r["id"] in ids:
                            recovered[r["id"]] = r.get("text", "")
            except Exception:
                pass
            log({"event": "channel_deletion", "deleted_ids": ids, "count": len(ids),
                 "recovered": [{"id": i, "text": (recovered.get(i, "") or "")[:200],
                                "had_copy": i in recovered} for i in ids],
                 "note": "channel deleted message(s) — likely burying a loser"})
        except Exception as e:
            log({"event": "deleted_handler_error", "error": repr(e)})

    asyncio.create_task(ticker())
    asyncio.create_task(heartbeat())
    await client.run_until_disconnected()


async def supervisor():
    global STATE
    log({"event": "boot", "note": "starting price feed + signal stream"})

    # 1) backfill any signals missed while we were down (separate process → own reactor)
    if RUN_BACKFILL_ON_START:
        log({"event": "backfill_launch"})
        try:
            subprocess.run([sys.executable, BACKFILL_PATH], timeout=300)
        except Exception as e:
            log({"event": "backfill_launch_error", "error": repr(e), "note": "continuing to live"})
        STATE = vstate.load(BALANCE0)      # reload whatever backfill committed

    # 2) start the live price feed thread
    threading.Thread(target=start_price_feed, daemon=True).start()
    await asyncio.sleep(5)                  # let the feed connect before the first signals
    while True:
        try:
            await run_stream()
            log({"event": "stream_disconnected", "note": "restarting in 15s"})
        except Exception as e:
            log({"event": "stream_crash", "error": repr(e), "note": "restarting in 15s"})
        await asyncio.sleep(15)


if __name__ == "__main__":
    try:
        asyncio.run(supervisor())
    except KeyboardInterrupt:
        n = STATE["wins"] + STATE["losses"]
        print(f"\nstopped. paper record: {STATE['wins']}W/{STATE['losses']}L"
              + (f" ({100*STATE['wins']/n:.0f}% win), equity ${STATE['equity']:.2f}" if n else "")
              + "  (persisted to validate_state.json)")
