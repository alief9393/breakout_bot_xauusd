#!/usr/bin/env python3
"""
cTrader Open API - Phase 2 prep: paged historical OHLC downloader.

A single ProtoOAGetTrendbarsReq can only return a bounded number of bars, so to
pull months/years of history we page BACKWARD through time, window by window, and
stitch the results together.

Design notes (why it's built this way):
  * Truncation-safe paging: each iteration we step `to` back to just before the
    EARLIEST bar we actually received (not to the window's `from`). If the server
    caps the response, the earliest bar comes back later than `from` and we simply
    re-cover that gap next iteration. Window size therefore affects speed only,
    never correctness — we can never silently skip bars.
  * Shrink-on-error: if a window is rejected (e.g. span too large), we halve the
    window and retry the same `to` instead of losing that slice of history.
  * Error responses arrive on the deferred's CALLBACK (same clientMsgId), not the
    errback — so we inspect payloadType and raise ourselves.
  * Every run self-validates (OHLC integrity, monotonic timestamps, dupes, gaps)
    before you trust the CSV. Garbage data => garbage backtest.

Config lives in .env (see .env.example). Extra keys used here:
    CTRADER_HISTORY_YEARS   how far back to pull      (default 3)
    CTRADER_WINDOW_DAYS     per-request window size   (default 14)
    CTRADER_REQUEST_DELAY   seconds between requests  (default 1.0)
    CTRADER_HISTORY_CSV     output file               (default xauusd_m5_history.csv)
"""

import os
import sys
import csv
import time
import datetime

from dotenv import load_dotenv
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
from twisted.internet import reactor, defer, task

# ─────────────────────────────────────────────────────────────
# CONFIG — loaded from .env (see .env.example)
# ─────────────────────────────────────────────────────────────
load_dotenv()

CLIENT_ID     = os.getenv("CTRADER_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET", "")
ACCESS_TOKEN  = os.getenv("CTRADER_ACCESS_TOKEN", "")

HOST_TYPE     = os.getenv("CTRADER_HOST_TYPE", "demo")
ACCOUNT_ID    = int(os.getenv("CTRADER_ACCOUNT_ID", "0"))
SYMBOL_NAME   = os.getenv("CTRADER_SYMBOL", "XAUUSD")
PERIOD        = os.getenv("CTRADER_PERIOD", "M5")

HISTORY_YEARS = float(os.getenv("CTRADER_HISTORY_YEARS", "3"))
WINDOW_DAYS   = int(os.getenv("CTRADER_WINDOW_DAYS", "14"))
REQUEST_DELAY = float(os.getenv("CTRADER_REQUEST_DELAY", "1.0"))
_ROOT         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_CSV    = os.getenv("CTRADER_HISTORY_CSV", os.path.join(_ROOT, "data", "xauusd_m1.csv"))
os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_CSV)), exist_ok=True)

_missing = [n for n, v in [
    ("CTRADER_CLIENT_ID", CLIENT_ID),
    ("CTRADER_CLIENT_SECRET", CLIENT_SECRET),
    ("CTRADER_ACCESS_TOKEN", ACCESS_TOKEN),
] if not v]
if _missing:
    sys.exit("Missing required credentials in .env: " + ", ".join(_missing) +
             "\nCopy .env.example to .env and fill them in.")
# ─────────────────────────────────────────────────────────────

# minutes-per-bar for each period (used to size backward steps)
PERIOD_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
PERIOD_ENUM = {
    "M1":  ProtoOATrendbarPeriod.M1,  "M5":  ProtoOATrendbarPeriod.M5,
    "M15": ProtoOATrendbarPeriod.M15, "M30": ProtoOATrendbarPeriod.M30,
    "H1":  ProtoOATrendbarPeriod.H1,  "H4":  ProtoOATrendbarPeriod.H4,
    "D1":  ProtoOATrendbarPeriod.D1,
}
if PERIOD not in PERIOD_ENUM:
    sys.exit(f"Unsupported CTRADER_PERIOD={PERIOD}. Use one of {list(PERIOD_ENUM)}")

PERIOD_MS = PERIOD_MINUTES[PERIOD] * 60 * 1000
MAX_RETRIES = 3

ERROR_TYPES = (ProtoOAErrorRes().payloadType, ProtoErrorRes().payloadType)

host = EndPoints.PROTOBUF_DEMO_HOST if HOST_TYPE == "demo" else EndPoints.PROTOBUF_LIVE_HOST
client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

state = {"account_id": ACCOUNT_ID, "symbol_id": None}


class ApiError(Exception):
    """A cTrader error response delivered on the deferred callback."""


def sleep(seconds):
    return task.deferLater(reactor, seconds, lambda: None)


@defer.inlineCallbacks
def send_checked(req, timeout=20):
    """Send a request, return the extracted response, raise ApiError on error responses."""
    msg = yield client.send(req, responseTimeoutInSeconds=timeout)
    if msg.payloadType in ERROR_TYPES:
        res = Protobuf.extract(msg)
        raise ApiError(getattr(res, "description", None) or getattr(res, "errorCode", str(res)))
    defer.returnValue(Protobuf.extract(msg))


@defer.inlineCallbacks
def request_trendbars(from_ts, to_ts):
    """One trendbar request with retry/backoff on transient failures (timeouts, drops)."""
    req = ProtoOAGetTrendbarsReq()
    req.ctidTraderAccountId = state["account_id"]
    req.symbolId = state["symbol_id"]
    req.period = PERIOD_ENUM[PERIOD]
    req.fromTimestamp = from_ts
    req.toTimestamp = to_ts
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            res = yield send_checked(req)
            defer.returnValue(res)
        except ApiError:
            raise  # let the caller decide (e.g. shrink window)
        except Exception as e:
            last = e
            print(f"   ! request failed (attempt {attempt}/{MAX_RETRIES}): {e!r} — backing off")
            yield sleep(2.0 * attempt)
    raise last


def reconstruct(b):
    """cTrader delta-encoded bar -> [iso_time, o, h, l, c, volume]."""
    low = b.low
    o = (low + b.deltaOpen) / 100000.0
    h = (low + b.deltaHigh) / 100000.0
    c = (low + b.deltaClose) / 100000.0
    l = low / 100000.0
    ts = datetime.datetime.utcfromtimestamp(b.utcTimestampInMinutes * 60)
    return [ts.isoformat(), o, h, l, c, b.volume]


@defer.inlineCallbacks
def download_history():
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(HISTORY_YEARS * 365 * 24 * 3600 * 1000)
    window_days = WINDOW_DAYS
    to_ts = now_ms

    bars = {}  # utcTimestampInMinutes -> row  (dict = automatic dedupe across overlapping windows)
    req_count = 0
    print(f"Pulling ~{HISTORY_YEARS}y of {PERIOD} {SYMBOL_NAME} back to "
          f"{datetime.datetime.utcfromtimestamp(start_ms/1000).date()} "
          f"(window={window_days}d, delay={REQUEST_DELAY}s) ...")

    while to_ts > start_ms:
        from_ts = max(start_ms, to_ts - window_days * 24 * 3600 * 1000)
        try:
            res = yield request_trendbars(from_ts, to_ts)
        except ApiError as e:
            if window_days > 1:
                window_days = max(1, window_days // 2)
                print(f"   ! window rejected ({e}); shrinking to {window_days}d and retrying")
                continue
            print(f"   ! window rejected at minimum size: {e}")
            break

        req_count += 1
        tbs = res.trendbar
        if not tbs:
            print("   reached start of available history (empty window)")
            break

        earliest_min = min(b.utcTimestampInMinutes for b in tbs)
        for b in tbs:
            bars[b.utcTimestampInMinutes] = reconstruct(b)

        new_to = earliest_min * 60 * 1000 - PERIOD_MS
        if new_to >= to_ts:  # no backward progress -> stop rather than loop forever
            print("   no backward progress; stopping")
            break
        to_ts = new_to

        edge = datetime.datetime.utcfromtimestamp(earliest_min * 60).date()
        print(f"   [{req_count:>4}] +{len(tbs):>4} bars | total {len(bars):>7} | back to {edge}")
        yield sleep(REQUEST_DELAY)

    rows = sorted(bars.values(), key=lambda r: r[0])
    write_and_validate(rows)


def write_and_validate(rows):
    if not rows:
        print("No bars downloaded — nothing written.")
        reactor.stop()
        return

    with open(OUTPUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    print(f"\nSaved {len(rows)} bars to {OUTPUT_CSV}")

    # ── self-validation: never trust bars we haven't checked ──
    bad_ohlc = bad_hl = dups = 0
    ts = []
    for r in rows:
        _, o, h, l, c, _v = r
        o, h, l, c = float(o), float(h), float(l), float(c)
        if not (h >= o and h >= c and l <= o and l <= c):
            bad_ohlc += 1
        if h < l:
            bad_hl += 1
        ts.append(datetime.datetime.fromisoformat(r[0]))
    mono = all(ts[i] < ts[i + 1] for i in range(len(ts) - 1))
    dups = len(ts) - len(set(ts))

    step = PERIOD_MINUTES[PERIOD]
    gaps = {}
    for i in range(len(ts) - 1):
        d = int((ts[i + 1] - ts[i]).total_seconds() / 60)
        if d != step:
            gaps[d] = gaps.get(d, 0) + 1

    print("── validation ──────────────────────────────")
    print(f"  span:                 {ts[0]}  ->  {ts[-1]}")
    print(f"  OHLC integrity fails: {bad_ohlc}")
    print(f"  high<low fails:       {bad_hl}")
    print(f"  strictly increasing:  {mono}")
    print(f"  duplicate timestamps: {dups}")
    print(f"  off-step intervals:   {len(gaps)} distinct "
          f"(expected: weekend/session-break gaps only)")
    ok = (bad_ohlc == 0 and bad_hl == 0 and mono and dups == 0)
    print(f"  RESULT: {'PASS ✅' if ok else 'FAIL ❌ — inspect before backtesting'}")
    reactor.stop()


@defer.inlineCallbacks
def run():
    try:
        yield send_checked(_app_auth_req())
        print("Application authenticated.")

        accounts = (yield send_checked(_account_list_req())).ctidTraderAccount
        print(f"Found {len(accounts)} account(s).")
        if state["account_id"] == 0:
            demo = [a for a in accounts if not a.isLive]
            chosen = (demo or accounts)[0]
            state["account_id"] = chosen.ctidTraderAccountId
            print(f"Auto-selected account {state['account_id']} "
                  f"({'live' if chosen.isLive else 'demo'})")

        yield send_checked(_account_auth_req(state["account_id"]))
        print(f"Account {state['account_id']} authenticated.")

        symbols = (yield send_checked(_symbols_req())).symbol
        match = [s for s in symbols if s.symbolName.upper() == SYMBOL_NAME.upper()]
        if not match:
            print(f"Symbol '{SYMBOL_NAME}' not found.")
            reactor.stop()
            return
        state["symbol_id"] = match[0].symbolId
        print(f"{SYMBOL_NAME} -> symbolId {state['symbol_id']}")

        yield download_history()
    except Exception as e:
        print(f"FATAL: {e!r}")
        if reactor.running:
            reactor.stop()


def _app_auth_req():
    r = ProtoOAApplicationAuthReq(); r.clientId = CLIENT_ID; r.clientSecret = CLIENT_SECRET; return r

def _account_list_req():
    r = ProtoOAGetAccountListByAccessTokenReq(); r.accessToken = ACCESS_TOKEN; return r

def _account_auth_req(account_id):
    r = ProtoOAAccountAuthReq(); r.ctidTraderAccountId = account_id; r.accessToken = ACCESS_TOKEN; return r

def _symbols_req():
    r = ProtoOASymbolsListReq(); r.ctidTraderAccountId = state["account_id"]; return r


def on_connected(_):
    print("Connected. Authenticating ...")
    run()

def on_disconnected(_, reason):
    print("Disconnected:", reason)


client.setConnectedCallback(on_connected)
client.setDisconnectedCallback(on_disconnected)

print(f"Connecting to {HOST_TYPE} host ({host}) ...")
client.startService()
reactor.run()
