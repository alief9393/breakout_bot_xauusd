#!/usr/bin/env python3
"""
cTrader live engine for the Donchian breakout strategy (shared breakout_core).
Runs anywhere (Twisted + protobuf). Reuses the StreamTrade cTrader credentials.

Setup: fill live_breakout/.env with CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET /
CTRADER_ACCESS_TOKEN / CTRADER_HOST_TYPE (demo|live). Then: python live_ctrader_breakout.py
Safety: EXECUTE_ENABLED=False = dry-run. Start on DEMO.
"""
import os, sys, json, time, datetime, threading, calendar
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from breakout_core import Config, BreakoutEngine
from dotenv import load_dotenv
from twisted.internet import reactor, defer
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
# fall back to StreamTrade root .env for cTrader creds if not set here
load_dotenv(os.path.join(HERE, "..", ".env"))

CLIENT_ID     = os.getenv("CTRADER_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET", "")
ACCESS_TOKEN  = os.getenv("CTRADER_ACCESS_TOKEN", "")
HOST_TYPE     = os.getenv("CTRADER_HOST_TYPE", "demo")
SYMBOL_NAME   = os.getenv("CTRADER_SYMBOL", "XAUUSD")
EXECUTE_ENABLED = os.getenv("EXECUTE_ENABLED", "False") == "True"
ACCOUNT_ID    = int(os.getenv("CTRADER_ACCOUNT_ID", "0"))

PRICE_SCALE = 100000.0
LABEL = "breakout"
POLL_SEC = 15
CFG = Config(symbol=SYMBOL_NAME, donchian_n=3, sl_pips=150, trail_r=2.0,
             risk_pct=0.07, horizon_days=7, cooldown_min=240, min_lot=0.01, max_lot=50.0)
LOG = os.path.join(HERE, "ctrader_breakout_log.jsonl")

ST = {"bid": None, "ask": None, "acc": ACCOUNT_ID, "sym": None, "lot_size": None,
      "digits": 2, "min_vol": 100, "step_vol": 100, "balance": None, "pos": None, "last_day": None}
eng = BreakoutEngine(CFG)
SPOT_PT = ProtoOASpotEvent().payloadType
EXEC_PT = ProtoOAExecutionEvent().payloadType


def log(o):
    line = {"t": datetime.datetime.utcnow().isoformat(timespec="seconds"), **o}
    print(line, flush=True)
    try:
        open(LOG, "a").write(json.dumps(line) + "\n")
    except Exception:
        pass


def lots_to_volume(lots):
    ls = ST["lot_size"] or 10000; step = ST["step_vol"] or 100
    return int(max(ST["min_vol"] or 100, round(lots * ls / step) * step))


client = Client(EndPoints.PROTOBUF_DEMO_HOST if HOST_TYPE == "demo" else EndPoints.PROTOBUF_LIVE_HOST,
                EndPoints.PROTOBUF_PORT, TcpProtocol)


def send(req, t=25):
    return client.send(req, responseTimeoutInSeconds=t)


@defer.inlineCallbacks
def setup():
    try:
        r = ProtoOAApplicationAuthReq(); r.clientId = CLIENT_ID; r.clientSecret = CLIENT_SECRET; yield send(r)
        r = ProtoOAGetAccountListByAccessTokenReq(); r.accessToken = ACCESS_TOKEN
        accts = Protobuf.extract((yield send(r))).ctidTraderAccount
        if ST["acc"] == 0:
            ST["acc"] = ([a for a in accts if not a.isLive] or accts)[0].ctidTraderAccountId
        r = ProtoOAAccountAuthReq(); r.ctidTraderAccountId = ST["acc"]; r.accessToken = ACCESS_TOKEN; yield send(r)
        r = ProtoOASymbolsListReq(); r.ctidTraderAccountId = ST["acc"]
        sym = [s for s in Protobuf.extract((yield send(r))).symbol if s.symbolName.upper() == SYMBOL_NAME.upper()][0]
        ST["sym"] = sym.symbolId
        r = ProtoOASymbolByIdReq(); r.ctidTraderAccountId = ST["acc"]; r.symbolId.append(sym.symbolId)
        det = Protobuf.extract((yield send(r))).symbol[0]
        ST["lot_size"] = det.lotSize; ST["digits"] = det.digits
        ST["min_vol"] = det.minVolume; ST["step_vol"] = det.stepVolume
        yield refresh_balance()
        yield reconcile()
        yield update_donchian()
        r = ProtoOASubscribeSpotsReq(); r.ctidTraderAccountId = ST["acc"]; r.symbolId.append(sym.symbolId); yield send(r)
        log({"event": "online", "symbol": SYMBOL_NAME, "account": ST["acc"], "host": HOST_TYPE,
             "balance": ST["balance"], "execute_enabled": EXECUTE_ENABLED, "NH": eng.nh, "NL": eng.nl,
             "cfg": {"donchian_n": CFG.donchian_n, "sl_pips": CFG.sl_pips, "trail_r": CFG.trail_r, "risk_pct": CFG.risk_pct}})
        reactor.callLater(POLL_SEC, tick_loop)
    except Exception as e:
        log({"event": "setup_error", "error": repr(e)})


@defer.inlineCallbacks
def refresh_balance():
    try:
        r = ProtoOATraderReq(); r.ctidTraderAccountId = ST["acc"]
        tr = Protobuf.extract((yield send(r))).trader
        ST["balance"] = tr.balance / (10 ** getattr(tr, "moneyDigits", 2))
    except Exception:
        pass


@defer.inlineCallbacks
def reconcile():
    try:
        r = ProtoOAReconcileReq(); r.ctidTraderAccountId = ST["acc"]
        rec = Protobuf.extract((yield send(r)))
        ST["pos"] = None
        for p in rec.position:
            if int(p.positionStatus) != 1:
                continue
            lbl = getattr(getattr(p, "tradeData", None), "label", "")
            if lbl == LABEL:
                d = "BUY" if p.tradeData.tradeSide == ProtoOATradeSide.BUY else "SELL"
                entry = p.price / PRICE_SCALE if hasattr(p, "price") else ST["bid"]
                ST["pos"] = {"id": p.positionId, "dir": d}
                if eng.pos is None and entry:
                    eng.on_filled(d, entry, p.tradeData.volume / (ST["lot_size"] or 10000), datetime.datetime.utcnow())
    except Exception as e:
        log({"event": "reconcile_error", "error": repr(e)})


@defer.inlineCallbacks
def update_donchian():
    """Pull last donchian_n+2 completed daily bars and set NH/NL."""
    try:
        now_ms = int(time.time() * 1000)
        span = (CFG.donchian_n + 3) * 24 * 3600 * 1000
        r = ProtoOAGetTrendbarsReq(); r.ctidTraderAccountId = ST["acc"]; r.symbolId = ST["sym"]
        r.period = ProtoOATrendbarPeriod.D1; r.fromTimestamp = now_ms - span; r.toTimestamp = now_ms
        bars = Protobuf.extract((yield send(r))).trendbar
        daily = []
        today = datetime.datetime.utcnow().date()
        for btb in bars:
            low = btb.low / PRICE_SCALE
            h = low + btb.deltaHigh / PRICE_SCALE
            dt = datetime.datetime.utcfromtimestamp(btb.utcTimestampInMinutes * 60).date()
            if dt < today:                      # only completed days
                daily.append((dt, h, low))
        eng.update_donchian(daily)
    except Exception as e:
        log({"event": "donchian_error", "error": repr(e)})


def place_order(direction, lots, sl):
    req = ProtoOANewOrderReq()
    req.ctidTraderAccountId = ST["acc"]; req.symbolId = ST["sym"]
    req.orderType = ProtoOAOrderType.MARKET
    req.tradeSide = ProtoOATradeSide.BUY if direction == "BUY" else ProtoOATradeSide.SELL
    req.volume = lots_to_volume(lots); req.stopLoss = round(sl, ST["digits"]); req.label = LABEL
    client.send(req)
    log({"event": "ORDER_SENT", "dir": direction, "lots": lots, "volume": req.volume, "sl": round(sl, ST["digits"])})


def amend_sl(sl):
    if not ST["pos"]:
        return
    req = ProtoOAAmendPositionSLTPReq()
    req.ctidTraderAccountId = ST["acc"]; req.positionId = ST["pos"]["id"]; req.stopLoss = round(sl, ST["digits"])
    client.send(req)
    log({"event": "AMEND_SL", "sl": round(sl, ST["digits"])})


def close_pos():
    if not ST["pos"]:
        return
    req = ProtoOAClosePositionReq()
    req.ctidTraderAccountId = ST["acc"]; req.positionId = ST["pos"]["id"]
    client.send(req); log({"event": "CLOSE_SENT", "id": ST["pos"]["id"]})


def tick_loop():
    try:
        today = datetime.datetime.utcnow().date()
        if today != ST["last_day"]:
            update_donchian(); ST["last_day"] = today
        if ST["bid"] and ST["ask"] and ST["balance"]:
            intent = eng.on_tick(datetime.datetime.utcnow(), ST["bid"], ST["ask"], ST["balance"])
            if intent:
                if not EXECUTE_ENABLED:
                    log({"event": "DRY_RUN_intent", **intent})
                elif intent["action"] == "enter" and ST["pos"] is None:
                    place_order(intent["dir"], intent["lots"], intent["sl"])
                elif intent["action"] == "amend_sl" and ST["pos"] is not None:
                    amend_sl(intent["sl"])
                elif intent["action"] == "close" and ST["pos"] is not None:
                    close_pos()
    except Exception as e:
        log({"event": "loop_error", "error": repr(e)})
    reactor.callLater(POLL_SEC, tick_loop)


def on_message(_, message):
    pt = message.payloadType
    if pt == SPOT_PT:
        ev = Protobuf.extract(message)
        if ev.HasField("bid"): ST["bid"] = ev.bid / PRICE_SCALE
        if ev.HasField("ask"): ST["ask"] = ev.ask / PRICE_SCALE
    elif pt == EXEC_PT:
        ev = Protobuf.extract(message)
        if ev.HasField("position"):
            lbl = getattr(getattr(ev.position, "tradeData", None), "label", "")
            if lbl == LABEL:
                status = int(ev.position.positionStatus)
                if status == 1:
                    ST["pos"] = {"id": ev.position.positionId, "dir": eng.pos["dir"] if eng.pos else "?"}
                    if ev.HasField("deal") and eng.pos is None:
                        d = "BUY" if ev.position.tradeData.tradeSide == ProtoOATradeSide.BUY else "SELL"
                        eng.on_filled(d, ev.deal.executionPrice, ev.deal.volume / (ST["lot_size"] or 10000), datetime.datetime.utcnow())
                elif status == 2:                         # closed (SL hit / manual)
                    ST["pos"] = None; eng.on_closed(datetime.datetime.utcnow())
                    log({"event": "position_closed"})
                    reactor.callFromThread(lambda: refresh_balance())


def on_connected(_):
    log({"event": "ctrader_connected"}); setup()


def on_disconnected(_, reason):
    log({"event": "ctrader_disconnected", "reason": str(reason)[:80]})
    reactor.callLater(5, client.startService)


if __name__ == "__main__":
    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(on_disconnected)
    client.setMessageReceivedCallback(on_message)
    client.startService()
    reactor.run()
