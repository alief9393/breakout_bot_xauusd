#!/usr/bin/env python3
"""Parse CryptoNinjas-style signals into structured dicts, and map $COIN -> Binance symbol."""
import re


def to_binance(symbol):
    """$WIF -> WIFUSDT ; $ICNTUSDT -> ICNTUSDT (already suffixed)."""
    s = symbol.upper().lstrip("$")
    if s.endswith("USDT"):
        return s
    return s + "USDT"


def parse_crypto(text):
    """Return {dir, symbol, binance, entry_market, entry_limit, sl, tps} or None if not a signal."""
    t = (text or "").replace("⏎", "\n")
    tu = t.upper()
    if "LONG" in tu:
        d = "BUY"
    elif "SHORT" in tu:
        d = "SELL"
    else:
        return None
    sym = re.search(r"\$([A-Z0-9]+)", t)
    em = re.search(r"Entry market[^:]*:\s*([0-9.]+)", t, re.I)
    el = re.search(r"Entry limit[^:]*:\s*([0-9.]+)", t, re.I)
    sl = re.search(r"\bSL[^:]*:\s*([0-9.]+)", t, re.I)
    tps = re.findall(r"TP\d[^:]*:\s*([0-9.]+)", t, re.I)
    if not (sym and em and sl and tps):
        return None
    return {
        "dir": d,
        "symbol": sym.group(1).upper(),
        "binance": to_binance(sym.group(1)),
        "entry_market": float(em.group(1)),
        "entry_limit": float(el.group(1)) if el else None,
        "sl": float(sl.group(1)),
        "tps": [float(x) for x in tps],
    }


if __name__ == "__main__":
    samples = [
        "🟢 LONG  - $WIF  - Entry market (now): 0.2287 - Entry limit: 0.1866 - SL: 0.1584  🎯 TP1: 0.2655 🎯 TP2: 0.3115 🎯 TP3: 0.5878",
        "🔴 SHORT - $XRPUSDT - Entry market: 1.3321 - Entry limit: 1.4024 - SL: 1.4471 🎯 TP1: 1.2575 🎯 TP2: 1.1987 🎯 TP3: 0.9951",
        "ARB hit TP2 + 1.9R",
    ]
    for s in samples:
        print(parse_crypto(s))
