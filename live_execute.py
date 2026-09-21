#!/usr/bin/env python3
"""
StreamTrade — Stage 5b: LIVE ORDER EXECUTION on cTrader (single-TP4 strategy).

Streams the channel, and on each signal places a REAL cTrader market order with the
signal's SL and its TP4 as the take-profit, sized by risk off the live account balance.

⚠️  SAFETY MODEL — read before running
  * EXECUTE_ENABLED = False  → ARMED DRY-RUN: connects, reads real balance, computes the
      EXACT order (volume, SL, TP prices) and logs "WOULD SEND …" but sends NOTHING.
      Run it like this FIRST and eyeball the orders. Flip to True only when they look right.
  * HOST_TYPE=demo + a demo account is the default. Live requires a token re-authorized
      for the live account AND HOST_TYPE=live — do that only after demo looks correct.
  * Hard rails always on: MAX_LOT, MAX_TRADES_PER_DAY, MAX_OPEN_POSITIONS.
  * Dedup by signal id; per-message try/except; supervisor restart. Same 24/5 hardening.

Run:  caffeinate -i ./venv/bin/python live_execute.py     (stop other bots — shared session)
"""

import os
import re
import sys
import json
import time
import threading
import datetime
import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient, events

from parse import parse_signal

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
from twisted.internet import reactor, defer

load_dotenv()

# ══════════════════════════ SAFETY / CONFIG — EDIT THESE ══════════════════════════
EXECUTE_ENABLED   = True        # False = armed dry-run (compute + log, send NOTHING). True = place real orders.
CONFIRM_LIVE      = False        # extra gate: must be True to run against a LIVE (real-money) account

EXIT_MODE         = "dynamic"    # "single" = one order at TP{TARGET_TP}; "dynamic" = split + trail-behind-TP
TARGET_TP         = 4            # single mode: exit whole position at this TP
SPLIT2            = 0.02         # dynamic: calc lot >= this -> 2 sub-positions (aim TP3, TP4)
SPLIT4            = 0.08         # dynamic: calc lot >= this -> 4 sub-positions (aim TP1..TP4)
TARGET_MAP        = {1: [1], 2: [1, 2], 4: [0, 1, 2, 3]}  # 1->TP2 · 2->TP2+TP3 · 4->TP1..TP4
BUFFER_PIPS       = 5           # dynamic: trail the stop this far behind a hit TP
MAX_CHASE_PIPS    = 20          # skip a signal if price already ran this far past its entry (late-chase guard)
# early-alert entry: enter at the bare "BUY/SELL NOW" (no levels), set real SL/TP when the details arrive
EARLY_ALERT_ENTRY = True
ALERT_WINDOW_MIN  = 5           # match a details post to a preceding NOW-alert within this many minutes
PROVISIONAL_SL_PIPS = 250       # safety stop placed with the early order (replaced by the channel's real SL)
ALERT_LABEL       = "streamtrade-alert"   # distinct label for early-alert positions
RISK_PCT          = 0.30         # risk per trade off account balance
MAX_LOT           = 10.00         # hard ceiling on lots per order (start SMALL on demo)
MAX_TRADES_PER_DAY = 6           # stop opening new trades after this many in a UTC day
MAX_OPEN_POSITIONS = 3           # don't exceed this many of OUR open positions at once
ORDER_LABEL       = "streamtrade-tp4"
# ═══════════════════════════════════════════════════════════════════════════════════

# ── Telegram ──
API_ID   = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
CHANNEL  = os.getenv("TG_CHANNEL", "@Raahimbfxproo")
SESSION  = os.getenv("TG_SESSION", "streamtrade")

# ── cTrader ──
CLIENT_ID     = os.getenv("CTRADER_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET", "")
ACCESS_TOKEN  = os.getenv("CTRADER_ACCESS_TOKEN", "")
HOST_TYPE     = os.getenv("CTRADER_HOST_TYPE", "demo")
ACCOUNT_ID    = int(os.getenv("CTRADER_ACCOUNT_ID", "0"))
SYMBOL_NAME   = os.getenv("CTRADER_SYMBOL", "XAUUSD")
PRICE_SCALE   = 100000.0
PIP_VALUE_PER_LOT = 10.0
CONTRACT_OZ   = 100

LOG_FILE = "execute_log.jsonl"
HEARTBEAT_MIN = 30

SPOT_EVENT_PT = ProtoOASpotEvent().payloadType
EXEC_EVENT_PT = ProtoOAExecutionEvent().payloadType

# ── shared state (Twisted thread <-> asyncio) ──
_lock = threading.Lock()
STATE = {"bid": None, "ask": None, "t": None,
         "balance": None, "symbol_id": None, "lot_size": None, "digits": 2,
         "min_vol": None, "step_vol": None, "max_vol": None,
         "acc": ACCOUNT_ID, "ready": False,
         "opened_today": 0, "day": None, "open_positions": 0, "seen": set()}
_client = None
_alerts = {}   # early-alert positions awaiting details: aid -> {dir, mono, pos_id, entry, finalized, digits}
_lat = {}      # latency tracking per message id: mid -> {rx_mono, post_ts, send_mono}


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


def get_price():
    with _lock:
        return STATE["bid"], STATE["ask"], STATE["t"]


def lots_to_volume(lots):
    """Snap desired lots to the symbol's step, clamp to [min,max]. Verified: XAUUSD lotSize=10000."""
    ls = STATE["lot_size"] or 10000
    step = STATE["step_vol"] or 100
    vol = round(lots * ls / step) * step
    vol = max(STATE["min_vol"] or 100, min(vol, STATE["max_vol"] or 1000000))
    return int(vol)


def size_lots(balance, sl_pips):
    """Risk-based lots, clamped by MAX_LOT (and the min-lot floor)."""
    if sl_pips <= 0:
        return 0.0
    lots = (balance * RISK_PCT) / (sl_pips * PIP_VALUE_PER_LOT)
    return max(0.01, min(round(lots, 2), MAX_LOT))


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


# open dynamic-split groups: gid -> {d, sl, entry, digits, trail_stop, subs:[{target,lots,volume,label,pos_id,closed}]}
_groups = {}


# ══════════════════════════════════════════════════════════════════════════
# cTrader thread: connect, auth, balance, symbol details, spots, order handling
# ══════════════════════════════════════════════════════════════════════════
def start_ctrader():
    global _client
    host = EndPoints.PROTOBUF_LIVE_HOST if HOST_TYPE == "live" else EndPoints.PROTOBUF_DEMO_HOST
    _client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

    def send(r, t=20):
        return _client.send(r, responseTimeoutInSeconds=t)

    @defer.inlineCallbacks
    def setup():
        try:
            r = ProtoOAApplicationAuthReq(); r.clientId = CLIENT_ID; r.clientSecret = CLIENT_SECRET
            yield send(r)
            r = ProtoOAGetAccountListByAccessTokenReq(); r.accessToken = ACCESS_TOKEN
            accs = Protobuf.extract((yield send(r))).ctidTraderAccount
            if STATE["acc"] == 0:
                pick = [a for a in accs if (a.isLive == (HOST_TYPE == "live"))] or accs
                STATE["acc"] = pick[0].ctidTraderAccountId
            chosen = next((a for a in accs if a.ctidTraderAccountId == STATE["acc"]), None)
            is_live = bool(chosen and chosen.isLive)
            if is_live and not (EXECUTE_ENABLED and CONFIRM_LIVE):
                log({"event": "refuse_live", "note": "account is LIVE but EXECUTE_ENABLED+CONFIRM_LIVE not both set — halting"})
                reactor.stop(); return
            r = ProtoOAAccountAuthReq(); r.ctidTraderAccountId = STATE["acc"]; r.accessToken = ACCESS_TOKEN
            yield send(r)
            # balance
            r = ProtoOATraderReq(); r.ctidTraderAccountId = STATE["acc"]
            tr = Protobuf.extract((yield send(r))).trader
            md = getattr(tr, "moneyDigits", 2)               # scale for balance (USD accounts = 2)
            with _lock:
                STATE["balance"] = tr.balance / (10 ** md)
            # reconcile OUR existing open positions so the rail survives restarts
            r = ProtoOAReconcileReq(); r.ctidTraderAccountId = STATE["acc"]
            rec = Protobuf.extract((yield send(r)))
            gids = set(); singles = 0                          # a dynamic group = many positions but 1 slot
            for p in rec.position:
                if int(p.positionStatus) != 1:
                    continue
                lbl = getattr(getattr(p, "tradeData", None), "label", "")
                if lbl.startswith(ORDER_LABEL + "::"):
                    gids.add(lbl.split("::")[1] if "::" in lbl else lbl)
                elif lbl == ORDER_LABEL or lbl.startswith(ALERT_LABEL + "::"):
                    singles += 1
            ours = len(gids) + singles
            with _lock:
                STATE["open_positions"] = ours
            # symbol details
            r = ProtoOASymbolsListReq(); r.ctidTraderAccountId = STATE["acc"]
            syms = Protobuf.extract((yield send(r))).symbol
            sid = [s for s in syms if s.symbolName.upper() == SYMBOL_NAME.upper()][0].symbolId
            r = ProtoOASymbolByIdReq(); r.ctidTraderAccountId = STATE["acc"]; r.symbolId.append(sid)
            det = Protobuf.extract((yield send(r))).symbol[0]
            with _lock:
                STATE["symbol_id"] = sid; STATE["lot_size"] = det.lotSize; STATE["digits"] = det.digits
                STATE["min_vol"] = det.minVolume; STATE["step_vol"] = det.stepVolume; STATE["max_vol"] = det.maxVolume
            # subscribe spots (for entry-sanity logging)
            r = ProtoOASubscribeSpotsReq(); r.ctidTraderAccountId = STATE["acc"]; r.symbolId.append(sid)
            yield send(r)
            with _lock:
                STATE["ready"] = True
            log({"event": "ctrader_ready", "account": STATE["acc"], "is_live": is_live,
                 "balance": STATE["balance"], "open_positions": ours, "symbol_id": sid,
                 "lot_size": det.lotSize, "execute_enabled": EXECUTE_ENABLED, "host": HOST_TYPE})
        except Exception as e:
            log({"event": "ctrader_setup_error", "error": repr(e)})

    def on_message(_, message):
        pt = message.payloadType
        if pt == SPOT_EVENT_PT:
            ev = Protobuf.extract(message)
            with _lock:
                if ev.HasField("bid"): STATE["bid"] = ev.bid / PRICE_SCALE
                if ev.HasField("ask"): STATE["ask"] = ev.ask / PRICE_SCALE
                STATE["t"] = time.time()
        elif pt == EXEC_EVENT_PT:
            ev = Protobuf.extract(message)
            et = ev.executionType
            info = {"event": "EXECUTION", "exec_type": int(et)}
            if ev.HasField("errorCode") and ev.errorCode:
                info["error"] = ev.errorCode
            if ev.HasField("order"):
                info["order_id"] = ev.order.orderId
            close_px = getattr(ev.deal, "executionPrice", None) if ev.HasField("deal") else None
            if ev.HasField("position"):
                info["position_id"] = ev.position.positionId
                info["pos_status"] = int(ev.position.positionStatus)
                status = int(ev.position.positionStatus)
                lbl = getattr(getattr(ev.position, "tradeData", None), "label", "")
                if lbl.startswith(ORDER_LABEL + "::"):       # dynamic-split sub-position
                    handle_group_event(lbl, status, ev.position.positionId, close_px)
                elif lbl.startswith(ALERT_LABEL + "::"):      # early-alert position
                    handle_alert_event(lbl, status, ev.position.positionId, close_px)
                elif status == 2 and lbl == ORDER_LABEL:     # single-mode position closed
                    with _lock:
                        STATE["open_positions"] = max(0, STATE["open_positions"] - 1)
                    refresh_balance()
            if ev.HasField("deal"):
                info["fill_price"] = close_px
                info["deal_volume"] = getattr(ev.deal, "volume", None)
                # fill lag: order-send -> fill, and total channel-post -> fill
                lbl2 = getattr(getattr(ev.position, "tradeData", None), "label", "") if ev.HasField("position") else ""
                key = None
                if "::" in lbl2:
                    try: key = int(lbl2.split("::")[1])
                    except Exception: key = None
                lt = _lat.get(key) if key is not None else None
                if lt and lt.get("send_mono"):
                    info["send_to_fill_sec"] = round(time.time() - lt["send_mono"], 3)
                if lt and lt.get("post_ts"):
                    info["post_to_fill_sec"] = round(time.time() - lt["post_ts"], 2)
            log(info)

    def on_connected(_):
        log({"event": "ctrader_connected", "note": "authenticating"})
        setup()

    def on_disconnected(_, reason):
        with _lock:
            STATE["ready"] = False
        log({"event": "ctrader_disconnected", "reason": str(reason), "note": "reconnecting in 5s"})
        reactor.callLater(5, _client.startService)

    _client.setConnectedCallback(on_connected)
    _client.setDisconnectedCallback(on_disconnected)
    _client.setMessageReceivedCallback(on_message)
    _client.startService()
    reactor.run(installSignalHandlers=False)


def place_order_on_reactor(side, volume, sl_price, tp_price, digits, tag):
    """Runs on the Twisted reactor thread (via callFromThread). Sends the market order."""
    try:
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = STATE["acc"]
        req.symbolId = STATE["symbol_id"]
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = ProtoOATradeSide.BUY if side == "BUY" else ProtoOATradeSide.SELL
        req.volume = volume
        req.stopLoss = round(sl_price, digits)
        req.takeProfit = round(tp_price, digits)
        req.label = ORDER_LABEL
        req.comment = tag
        _client.send(req)
        log({"event": "ORDER_SENT", "side": side, "volume": volume,
             "sl": round(sl_price, digits), "tp": round(tp_price, digits), "tag": tag})
    except Exception as e:
        log({"event": "order_send_error", "error": repr(e)})


_ALERT_BAD = re.compile(r'PROFIT|RUNNING|PIPS|EXPECT|GAP|HIT|DONE|✅', re.I)


def alert_dir(text):
    """A bare 'GOLD BUY/SELL NOW' alert (no levels) -> its direction, else None."""
    tu = (text or "").upper()
    if _ALERT_BAD.search(text or "") or "TP" in tu:
        return None
    if re.search(r'\bNOW\b', tu) and re.search(r'\b(BUY|SELL)\b', tu):
        return "BUY" if "BUY" in tu else "SELL"
    return None


def place_early_on_reactor(aid, side, volume, sl_prov, digits):
    """Place the early-alert market order with a provisional safety stop, no TP yet (reactor thread)."""
    try:
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = STATE["acc"]
        req.symbolId = STATE["symbol_id"]
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = ProtoOATradeSide.BUY if side == "BUY" else ProtoOATradeSide.SELL
        req.volume = volume
        req.stopLoss = round(sl_prov, digits)
        req.label = f"{ALERT_LABEL}::{aid}"
        req.comment = f"early-{aid}"
        _client.send(req)
        log({"event": "ALERT_ORDER_SENT", "aid": aid, "side": side, "volume": volume,
             "provisional_sl": round(sl_prov, digits), "note": "early entry — real SL/TP set when details arrive"})
    except Exception as e:
        log({"event": "order_send_error", "aid": aid, "error": repr(e)})


def finalize_alert_on_reactor(pos_id, sl_price, tp_price, digits):
    """Replace an early-alert position's provisional stop with the channel's real SL + set its TP."""
    try:
        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = STATE["acc"]
        req.positionId = pos_id
        req.stopLoss = round(sl_price, digits)
        req.takeProfit = round(tp_price, digits)
        _client.send(req)
        log({"event": "ALERT_FINALIZED", "position_id": pos_id,
             "real_sl": round(sl_price, digits), "tp": round(tp_price, digits)})
    except Exception as e:
        log({"event": "amend_error", "position_id": pos_id, "error": repr(e)})


def close_position_on_reactor(pos_id, volume):
    """Market-close a position (used to drop an early-alert entry whose details never came)."""
    try:
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = STATE["acc"]
        req.positionId = pos_id
        req.volume = volume
        _client.send(req)
        log({"event": "ALERT_ABANDONED_CLOSE", "position_id": pos_id,
             "note": "no details arrived within window — closing early entry"})
    except Exception as e:
        log({"event": "close_error", "position_id": pos_id, "error": repr(e)})


def place_split_on_reactor(gid):
    """Place all sub-orders of a dynamic-split group (reactor thread). Each sub: its own TP, shared SL."""
    g = _groups.get(gid)
    if not g:
        return
    for k, sub in enumerate(g["subs"]):
        try:
            req = ProtoOANewOrderReq()
            req.ctidTraderAccountId = STATE["acc"]
            req.symbolId = STATE["symbol_id"]
            req.orderType = ProtoOAOrderType.MARKET
            req.tradeSide = ProtoOATradeSide.BUY if g["d"] == 1 else ProtoOATradeSide.SELL
            req.volume = sub["volume"]
            req.stopLoss = round(g["sl"], g["digits"])
            req.takeProfit = round(sub["target"], g["digits"])
            req.label = sub["label"]
            req.comment = f"grp{gid}s{k}"
            _client.send(req)
        except Exception as e:
            log({"event": "order_send_error", "gid": gid, "sub": k, "error": repr(e)})
    log({"event": "GROUP_SENT", "gid": gid, "subs": len(g["subs"]),
         "side": "BUY" if g["d"] == 1 else "SELL",
         "targets": [round(s["target"], g["digits"]) for s in g["subs"]],
         "lots": [s["lots"] for s in g["subs"]], "sl": round(g["sl"], g["digits"])})


def amend_position_on_reactor(pos_id, sl_price, digits):
    """Trail one open sub-position's stop to sl_price (reactor thread)."""
    try:
        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = STATE["acc"]
        req.positionId = pos_id
        req.stopLoss = round(sl_price, digits)
        _client.send(req)
        log({"event": "TRAIL_AMEND", "position_id": pos_id, "new_sl": round(sl_price, digits)})
    except Exception as e:
        log({"event": "amend_error", "position_id": pos_id, "error": repr(e)})


def handle_group_event(lbl, status, pos_id, close_px):
    """Reactor thread. sub label 'ORDER_LABEL::gid::k': record pos on open; on close, trail the
    remaining subs behind the hit TP, and free the group's slot when all subs are closed."""
    try:
        _, gid_s, k_s = lbl.split("::"); gid = int(gid_s); k = int(k_s)
    except Exception:
        return
    amends = []; trail_val = None; digits = 2; group_done = False
    with _lock:
        g = _groups.get(gid)
        if not g or k >= len(g["subs"]):
            return
        sub = g["subs"][k]; digits = g["digits"]; d = g["d"]
        if status == 1:                                   # opened -> record position id
            sub["pos_id"] = pos_id
            return
        if status != 2:                                   # only OPEN/CLOSED matter
            return
        sub["closed"] = True                              # closed
        if close_px is not None:                          # did it close AT its TP? then trail the rest
            tp_hit = (close_px >= sub["target"] - 0.05) if d == 1 else (close_px <= sub["target"] + 0.05)
            if tp_hit:
                trail = sub["target"] - d * (BUFFER_PIPS / 10.0)
                if (trail > g["trail_stop"]) if d == 1 else (trail < g["trail_stop"]):
                    g["trail_stop"] = trail; trail_val = trail
                    amends = [o["pos_id"] for o in g["subs"] if not o["closed"] and o.get("pos_id")]
        if all(s["closed"] for s in g["subs"]):
            STATE["open_positions"] = max(0, STATE["open_positions"] - 1)
            _groups.pop(gid, None); group_done = True
    for pid in amends:
        amend_position_on_reactor(pid, trail_val, digits)
    if group_done:
        refresh_balance()
        log({"event": "GROUP_CLOSED", "gid": gid})


def handle_alert_event(lbl, status, pos_id, close_px):
    """Reactor thread. Early-alert position 'ALERT_LABEL::aid': record fill on open (and apply a
    deferred finalize if the details already arrived); free the slot on close."""
    try:
        aid = int(lbl.split("::")[1])
    except Exception:
        return
    do_finalize = None
    with _lock:
        a = _alerts.get(aid)
        if not a:
            return
        if status == 1:                                   # opened -> record id + actual fill price
            a["pos_id"] = pos_id
            if close_px is not None:
                a["entry"] = close_px
            if a["want"] is not None:                     # details already came -> finalize now
                do_finalize = (pos_id, a["want"]["sl"], a["want"]["tp"], a["digits"])
        elif status == 2:                                 # closed (TP/SL/abandon) -> free slot
            STATE["open_positions"] = max(0, STATE["open_positions"] - 1)
            _alerts.pop(aid, None)
    if do_finalize:
        finalize_alert_on_reactor(*do_finalize)
    if status == 2:
        refresh_balance()
        log({"event": "ALERT_CLOSED", "aid": aid})


def refresh_balance():
    """Re-fetch account balance (runs on the reactor thread). Keeps sizing accurate as
    the account draws down / grows — call after every close and periodically."""
    try:
        r = ProtoOATraderReq(); r.ctidTraderAccountId = STATE["acc"]
        d = _client.send(r)
    except Exception:
        return

    def _got(msg):
        try:
            tr = Protobuf.extract(msg).trader
            md = getattr(tr, "moneyDigits", 2)
            with _lock:
                STATE["balance"] = tr.balance / (10 ** md)
            log({"event": "balance_refresh", "balance": STATE["balance"]})
        except Exception:
            pass
    d.addCallback(_got); d.addErrback(lambda e: None)


# ══════════════════════════════════════════════════════════════════════════
# asyncio: Telegram signal stream
# ══════════════════════════════════════════════════════════════════════════
def day_key():
    return utcnow().strftime("%Y-%m-%d")


def gate_ok(sig):
    """Enforce the hard rails. Returns (ok, reason)."""
    with _lock:
        if not STATE["ready"]:
            return False, "cTrader not ready / price stale"
        if STATE["day"] != day_key():
            STATE["day"] = day_key(); STATE["opened_today"] = 0
        if STATE["opened_today"] >= MAX_TRADES_PER_DAY:
            return False, f"daily cap {MAX_TRADES_PER_DAY} reached"
        if STATE["open_positions"] >= MAX_OPEN_POSITIONS:
            return False, f"max open positions {MAX_OPEN_POSITIONS}"
    return True, ""


async def open_early_alert(side, aid):
    """Enter at the bare NOW-alert with a provisional safety stop; details will set the real SL/TP."""
    bid, ask, t = get_price()
    if bid is None or ask is None or not t or (time.time() - t > 30):
        log({"event": "skip", "reason": "no/stale price", "dir": side, "aid": aid}); return
    ok, why = gate_ok({"dir": side})
    if not ok:
        log({"event": "skip", "reason": why, "dir": side, "aid": aid}); return
    d = 1 if side == "BUY" else -1
    entry = ask if d == 1 else bid
    sl_prov = entry - d * (PROVISIONAL_SL_PIPS / 10.0)
    with _lock:
        bal = STATE["balance"] or 0.0
        digits = STATE["digits"]
        if any(not a["finalized"] for a in _alerts.values()):     # one early entry at a time
            log({"event": "skip", "reason": "early-alert already pending", "dir": side, "aid": aid}); return
    volume = lots_to_volume(size_lots(bal, PROVISIONAL_SL_PIPS))
    with _lock:
        _alerts[aid] = {"dir": side, "mono": time.time(), "pos_id": None, "entry": None,
                        "finalized": False, "digits": digits, "volume": volume, "want": None}
        STATE["opened_today"] += 1; STATE["open_positions"] += 1
    log({"event": "ALERT_ENTRY", "aid": aid, "dir": side, "market_entry": round(entry, 2),
         "provisional_sl": round(sl_prov, 2), "provisional_lots": round(volume / (STATE["lot_size"] or 10000), 2),
         "note": "early entry placed — awaiting details for real SL/TP"})
    reactor.callFromThread(place_early_on_reactor, aid, side, volume, sl_prov, digits)


def try_finalize_alert(sig):
    """If a pending early-alert matches this details post, set its real SL + TP. Returns True if handled."""
    d = 1 if sig["dir"] == "BUY" else -1
    now = time.time()
    with _lock:
        cands = [(aid, a) for aid, a in _alerts.items()
                 if not a["finalized"] and a["dir"] == sig["dir"]
                 and (now - a["mono"]) <= ALERT_WINDOW_MIN * 60]
        if not cands:
            return False
        aid, a = max(cands, key=lambda kv: kv[1]["mono"])
        ref = a["entry"] if a["entry"] is not None else (sig["entry"])
        ahead = [x for x in sig["tps"] if (x > ref if d == 1 else x < ref)]
        if not ahead:
            a["finalized"] = True                     # nothing ahead -> leave provisional SL, let it run/stop
            log({"event": "ALERT_FINALIZE_SKIP", "aid": aid, "note": "no TP ahead of early entry"})
            return True
        tp = ahead[min(1, len(ahead) - 1)]            # TARGET_MAP single-target = 2nd ahead (TP2)
        a["want"] = {"sl": sig["sl"], "tp": tp}
        a["finalized"] = True
        pos_id = a["pos_id"]; digits = a["digits"]
    if pos_id is not None:
        reactor.callFromThread(finalize_alert_on_reactor, pos_id, sig["sl"], tp, digits)
    else:
        log({"event": "ALERT_FINALIZE_DEFERRED", "aid": aid,
             "note": "details arrived before fill confirmation — will finalize on open"})
    return True


async def on_signal(text, gid):
    sig = parse_signal(text)
    if not sig:
        return
    if EARLY_ALERT_ENTRY and try_finalize_alert(sig):     # details completing an early-alert entry
        return
    bid, ask, t = get_price()
    if bid is None or ask is None or not t or (time.time() - t > 30):
        log({"event": "skip", "reason": "no/stale price", "dir": sig["dir"]}); return
    d = 1 if sig["dir"] == "BUY" else -1
    mid = (bid + ask) / 2
    entry = ask if d == 1 else bid                    # market fill side
    ahead = [x for x in sig["tps"] if (x > entry if d == 1 else x < entry)]
    sl = sig["sl"]; sl_pips = abs(entry - sl) * 10
    need = TARGET_TP if EXIT_MODE == "single" else 1
    if len(ahead) < need:
        log({"event": "skip", "reason": f"<{need} TPs ahead of market", "dir": sig["dir"],
             "entry": round(entry, 2), "tps": sig["tps"]}); return
    chase = (mid - sig["entry"]) * d * 10        # + = market already ran past the stated entry, in our direction
    if chase > MAX_CHASE_PIPS:
        log({"event": "skip", "reason": "late-chase", "dir": sig["dir"],
             "stated_entry": sig["entry"], "market_mid": round(mid, 2),
             "chase_pips": round(chase, 1), "limit": MAX_CHASE_PIPS,
             "note": "price already ran past entry — not chasing a late fill"}); return
    ok, why = gate_ok(sig)
    if not ok:
        log({"event": "skip", "reason": why, "dir": sig["dir"]}); return
    with _lock:
        bal = STATE["balance"] or 0.0
        digits = STATE["digits"]
    total = size_lots(bal, sl_pips)
    lt = _lat.get(gid)
    decision_lag = round(time.time() - lt["rx_mono"], 3) if lt else None      # receive -> order-send (our code)
    total_lag = round(time.time() - lt["post_ts"], 2) if lt and lt["post_ts"] else None  # channel post -> our send
    if lt:
        lt["send_mono"] = time.time()
    common = {"event": "SIGNAL_ORDER", "dir": sig["dir"], "market_mid": round(mid, 2),
              "entry_side": round(entry, 2), "sl": sl, "sl_pips": round(sl_pips, 1),
              "balance": bal, "spread_pips": round((ask - bid) * 10, 2),
              "decision_lag_sec": decision_lag, "total_lag_sec": total_lag}

    if EXIT_MODE == "dynamic":
        idx, sizes = plan_split(total, len(ahead))
        subs = [{"target": ahead[i], "lots": sizes[k], "volume": lots_to_volume(sizes[k]),
                 "label": f"{ORDER_LABEL}::{gid}::{k}", "pos_id": None, "closed": False}
                for k, i in enumerate(idx)]
        order = {**common, "total_lots": total,
                 "split": [{"tp": round(s["target"], 2), "lots": s["lots"]} for s in subs]}
        if not EXECUTE_ENABLED:
            log({**order, "MODE": "DRY-RUN — would place this split (EXECUTE_ENABLED=False)"}); return
        with _lock:
            _groups[gid] = {"d": d, "sl": sl, "entry": entry, "digits": digits, "trail_stop": sl, "subs": subs}
            STATE["opened_today"] += 1; STATE["open_positions"] += 1
        log({**order, "MODE": "SENDING"})
        reactor.callFromThread(place_split_on_reactor, gid)
        return

    # single mode — one order at TP{TARGET_TP}
    tp = ahead[TARGET_TP - 1]
    volume = lots_to_volume(total)
    order = {**common, "tp4": tp, "lots": total, "volume": volume}
    if not EXECUTE_ENABLED:
        log({**order, "MODE": "DRY-RUN — would send this exact order (EXECUTE_ENABLED=False)"}); return
    with _lock:
        STATE["opened_today"] += 1; STATE["open_positions"] += 1
    log({**order, "MODE": "SENDING"})
    reactor.callFromThread(place_order_on_reactor, sig["dir"], volume, sl, tp, digits,
                           f"{sig['dir']}@{round(entry,2)}tp{TARGET_TP}")


async def heartbeat():
    while True:
        await asyncio.sleep(HEARTBEAT_MIN * 60)
        if STATE["ready"]:
            reactor.callFromThread(refresh_balance)         # periodic re-read (reactor thread)
        bid, ask, t = get_price()
        with _lock:
            log({"event": "heartbeat", "ready": STATE["ready"], "balance": STATE["balance"],
                 "opened_today": STATE["opened_today"], "open_positions": STATE["open_positions"],
                 "last_mid": round((bid + ask) / 2, 2) if bid and ask else None,
                 "execute_enabled": EXECUTE_ENABLED})


async def alert_watchdog():
    """Abandon an early-alert entry whose details never arrived within the window (close it out)."""
    while True:
        await asyncio.sleep(30)
        now = time.time()
        to_close = []
        with _lock:
            for aid, a in list(_alerts.items()):
                if not a["finalized"] and (now - a["mono"]) > ALERT_WINDOW_MIN * 60:
                    a["finalized"] = True                 # stop re-triggering; close if it actually filled
                    if a["pos_id"] is not None:
                        to_close.append((a["pos_id"], a["volume"]))
                    else:
                        _alerts.pop(aid, None)
                        STATE["open_positions"] = max(0, STATE["open_positions"] - 1)
        for pos_id, vol in to_close:
            reactor.callFromThread(close_position_on_reactor, pos_id, vol)


async def run_stream():
    client = TelegramClient(SESSION, API_ID, API_HASH,
                            connection_retries=None, auto_reconnect=True, retry_delay=5, request_retries=5)
    await client.start()
    me = await client.get_me()
    entity = await client.get_entity(CHANNEL)
    log({"event": "online", "channel": getattr(entity, "title", CHANNEL),
         "strategy": (f"dynamic-split (buffer {BUFFER_PIPS}p)" if EXIT_MODE == "dynamic" else f"single TP{TARGET_TP}"),
         "execute_enabled": EXECUTE_ENABLED,
         "rails": {"risk": RISK_PCT, "max_lot": MAX_LOT,
                   "max_per_day": MAX_TRADES_PER_DAY, "max_open": MAX_OPEN_POSITIONS}})

    @client.on(events.NewMessage(chats=entity))
    async def handler(event):
        try:
            mid = event.message.id
            with _lock:
                if mid in STATE["seen"]:
                    return
                STATE["seen"].add(mid)
            # latency: when did the channel post vs when did we receive it?
            post_ts = event.message.date.timestamp() if event.message.date else None
            _lat[mid] = {"rx_mono": time.time(), "post_ts": post_ts, "send_mono": None}
            if post_ts is not None:
                log({"event": "SIGNAL_RX", "mid": mid,
                     "delivery_lag_sec": round(time.time() - post_ts, 2),
                     "note": "channel posted -> we received (Telegram delivery lag)"})
            text = event.message.message or ""
            # bare "BUY/SELL NOW" (no levels) -> enter early; the details post finalizes SL/TP
            if EARLY_ALERT_ENTRY and alert_dir(text) and not parse_signal(text):
                await open_early_alert(alert_dir(text), mid)
                return
            await on_signal(text, mid)
        except Exception as e:
            log({"event": "handler_error", "error": repr(e)})

    asyncio.create_task(heartbeat())
    if EARLY_ALERT_ENTRY:
        asyncio.create_task(alert_watchdog())
    await client.run_until_disconnected()


async def supervisor():
    log({"event": "boot", "execute_enabled": EXECUTE_ENABLED, "host": HOST_TYPE,
         "note": "ARMED DRY-RUN (no orders)" if not EXECUTE_ENABLED else "LIVE ORDER MODE"})
    threading.Thread(target=start_ctrader, daemon=True).start()
    await asyncio.sleep(6)
    while True:
        try:
            await run_stream()
            log({"event": "stream_disconnected", "note": "restart in 15s"})
        except Exception as e:
            log({"event": "stream_crash", "error": repr(e), "note": "restart in 15s"})
        await asyncio.sleep(15)


if __name__ == "__main__":
    try:
        asyncio.run(supervisor())
    except KeyboardInterrupt:
        print("\nstopped by user")
