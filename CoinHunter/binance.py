#!/usr/bin/env python3
"""Binance public 1-minute klines fetcher (data-api.binance.vision — no API key needed).
Used to gate channel crypto signals against real price, the way cTrader M1 gated the gold ones."""
import urllib.request, ssl, json, time

BASE = "https://data-api.binance.vision"
# the sandbox proxy intercepts TLS; this is public read-only price data (no secrets), so relax verify
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_symbols = None


def _get(path, tries=4):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(BASE + path, headers={"User-Agent": "coinhunter"})
            return json.load(urllib.request.urlopen(req, timeout=25, context=_CTX))
        except Exception as e:
            last = e
            time.sleep(0.5 * (a + 1))
    raise last


def symbol_exists(sym):
    """True if `sym` (e.g. WIFUSDT) trades on Binance spot."""
    global _symbols
    if _symbols is None:
        info = _get("/api/v3/exchangeInfo")
        _symbols = {s["symbol"] for s in info.get("symbols", [])}
    return sym in _symbols


def klines(symbol, start_ms, end_ms, interval="1m"):
    """Return [(open_time_ms, open, high, low, close), ...] between start and end (paginated)."""
    out = []
    cur = int(start_ms)
    end_ms = int(end_ms)
    while cur < end_ms:
        batch = _get(f"/api/v3/klines?symbol={symbol}&interval={interval}"
                     f"&startTime={cur}&endTime={end_ms}&limit=1000")
        if not batch:
            break
        out.extend(batch)
        last = batch[-1][0]
        if last <= cur:
            break
        cur = last + 60_000
        if len(batch) < 1000:
            break
        time.sleep(0.12)
    return [(b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4])) for b in out]


if __name__ == "__main__":
    import datetime
    print("exists WIFUSDT:", symbol_exists("WIFUSDT"), "| exists FAKECOINUSDT:", symbol_exists("FAKECOINUSDT"))
    now = int(time.time() * 1000)
    bars = klines("WIFUSDT", now - 10 * 60_000, now)
    print(f"pulled {len(bars)} recent WIFUSDT 1m bars")
    for b in bars[-3:]:
        print(" ", datetime.datetime.utcfromtimestamp(b[0] / 1000).strftime("%H:%M"), "o", b[1], "h", b[2], "l", b[3], "c", b[4])
