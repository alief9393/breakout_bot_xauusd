#!/usr/bin/env python3
"""Multi-source SPOT 1m klines: try Binance spot first, fall back to OKX.
Returns [(open_time_ms, o, h, l, c)] and tells you which source answered."""
import urllib.request, ssl, json, time
import binance   # existing Binance-spot fetcher (data-api.binance.vision)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _okx_get(url, tries=3):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.load(urllib.request.urlopen(req, timeout=25, context=_CTX))
        except Exception as e:
            last = e
            time.sleep(0.4 * (a + 1))
    raise last


def _okx_klines(base, start_ms, end_ms):
    inst = f"{base}-USDT"
    got = {}
    after = int(end_ms)
    for _ in range(80):                      # up to ~8000 candles
        url = (f"https://www.okx.com/api/v5/market/history-candles"
               f"?instId={inst}&bar=1m&after={after}&limit=100")
        r = _okx_get(url)
        if r.get("code") != "0":
            break
        data = r.get("data", [])
        if not data:
            break
        for c in data:                       # [ts, o, h, l, c, vol, ...]
            ts = int(c[0])
            got[ts] = (ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]))
        oldest = min(int(c[0]) for c in data)
        if oldest <= start_ms:
            break
        after = oldest
        time.sleep(0.15)
    return [got[t] for t in sorted(got) if start_ms <= t <= end_ms]


def get_klines(base, start_ms, end_ms):
    """base = coin symbol without USDT (e.g. 'WIF'). Returns (bars, source)."""
    bsym = base if base.endswith("USDT") else base + "USDT"
    try:
        if binance.symbol_exists(bsym):
            bars = binance.klines(bsym, start_ms, end_ms)
            if bars:
                return bars, "binance"
    except Exception:
        pass
    try:
        okx_base = base[:-4] if base.endswith("USDT") else base
        bars = _okx_klines(okx_base, start_ms, end_ms)
        if bars:
            return bars, "okx"
    except Exception:
        pass
    return [], None


if __name__ == "__main__":
    now = int(time.time() * 1000)
    for base in ["WIF", "ZORA", "MOODENG", "SKR", "NOTACOIN"]:
        bars, src = get_klines(base, now - 6 * 60_000, now)
        print(f"  {base:>10}: {len(bars)} bars via {src}")
