#!/usr/bin/env python3
"""
MT5 live engine for the Donchian breakout strategy (shared breakout_core).
WINDOWS ONLY — needs the MetaTrader5 package + a running MT5 terminal (won't run on Mac).
Typical use: a Windows PC or Windows VPS with your Exness MT5 terminal logged in.

Setup:
  pip install MetaTrader5
  fill live_breakout/.env  (MT5_LOGIN / MT5_PASSWORD / MT5_SERVER, or leave blank if the
  terminal is already logged in), then:  python live_mt5_breakout.py

Safety: EXECUTE_ENABLED=False = dry-run (logs intents, sends NOTHING). Set True to trade.
Start on a DEMO account.
"""
import os, sys, json, time, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from breakout_core import Config, BreakoutEngine

try:
    import MetaTrader5 as mt5
except Exception as e:
    print("MetaTrader5 import failed — this engine only runs on Windows with MT5 installed.")
    print("  ", e)
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(HERE, ".env")) if os.path.exists(os.path.join(HERE, ".env")) else []:
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

# ── CONFIG ──
SYMBOL          = os.environ.get("MT5_SYMBOL", "XAUUSD")
EXECUTE_ENABLED = os.environ.get("EXECUTE_ENABLED", "False") == "True"
MAGIC           = 770001
POLL_SEC        = 15
CFG = Config(symbol=SYMBOL, donchian_n=3, sl_pips=150, trail_r=2.0,
             risk_pct=0.07, horizon_days=7, cooldown_min=240, min_lot=0.01, max_lot=50.0)
LOGIN    = os.environ.get("MT5_LOGIN", "")
PASSWORD = os.environ.get("MT5_PASSWORD", "")
SERVER   = os.environ.get("MT5_SERVER", "")
LOG = os.path.join(HERE, "mt5_breakout_log.jsonl")


def log(o):
    line = {"t": datetime.datetime.utcnow().isoformat(timespec="seconds"), **o}
    print(line, flush=True)
    try:
        open(LOG, "a").write(json.dumps(line) + "\n")
    except Exception:
        pass


def connect():
    ok = mt5.initialize(login=int(LOGIN), password=PASSWORD, server=SERVER) if LOGIN else mt5.initialize()
    if not ok:
        log({"event": "mt5_init_failed", "error": str(mt5.last_error())}); return False
    if not mt5.symbol_select(SYMBOL, True):
        log({"event": "symbol_select_failed", "symbol": SYMBOL}); return False
    return True


def daily_bars(n):
    """Last n COMPLETED daily bars as [(date, high, low)] (skip today's forming bar)."""
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_D1, 1, n)   # pos 1 skips current day
    if rates is None:
        return []
    return [(datetime.datetime.utcfromtimestamp(r["time"]).date(), float(r["high"]), float(r["low"])) for r in rates]


def our_position():
    ps = mt5.positions_get(symbol=SYMBOL)
    for p in (ps or []):
        if p.magic == MAGIC:
            return p
    return None


def send_market(direction, lots, sl):
    tick = mt5.symbol_info_tick(SYMBOL)
    price = tick.ask if direction == "BUY" else tick.bid
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": float(lots),
           "type": mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL,
           "price": price, "sl": float(sl), "deviation": 30, "magic": MAGIC,
           "comment": "breakout", "type_filling": mt5.ORDER_FILLING_IOC}
    r = mt5.order_send(req)
    log({"event": "ORDER_SENT", "dir": direction, "lots": lots, "sl": sl,
         "retcode": r.retcode, "price": getattr(r, "price", None)})
    return r


def amend_sl(ticket, sl):
    r = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": SYMBOL,
                        "position": ticket, "sl": float(sl), "magic": MAGIC})
    log({"event": "AMEND_SL", "ticket": ticket, "sl": sl, "retcode": r.retcode})


def close_position(pos):
    tick = mt5.symbol_info_tick(SYMBOL)
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": pos.volume,
           "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
           "position": pos.ticket, "price": tick.bid if is_buy else tick.ask,
           "deviation": 30, "magic": MAGIC, "type_filling": mt5.ORDER_FILLING_IOC}
    r = mt5.order_send(req)
    log({"event": "CLOSE_SENT", "ticket": pos.ticket, "retcode": r.retcode})


def main():
    if not connect():
        return
    acc = mt5.account_info()
    log({"event": "online", "symbol": SYMBOL, "server": SERVER or "terminal",
         "balance": acc.balance, "execute_enabled": EXECUTE_ENABLED,
         "cfg": {"donchian_n": CFG.donchian_n, "sl_pips": CFG.sl_pips, "trail_r": CFG.trail_r,
                 "risk_pct": CFG.risk_pct}})
    eng = BreakoutEngine(CFG)
    had_pos = False
    last_day = None
    while True:
        try:
            # refresh donchian on a new day
            today = datetime.datetime.utcnow().date()
            if today != last_day:
                eng.update_donchian(daily_bars(CFG.donchian_n))
                log({"event": "donchian", "NH": eng.nh, "NL": eng.nl}); last_day = today
            tick = mt5.symbol_info_tick(SYMBOL)
            acc = mt5.account_info()
            pos = our_position()
            # detect a close that happened server-side (SL hit)
            if had_pos and pos is None:
                eng.on_closed(datetime.datetime.utcnow())
                log({"event": "position_closed", "balance": acc.balance})
            had_pos = pos is not None
            # keep engine's position state in sync with the broker
            if pos is not None and eng.pos is None:
                eng.on_filled("BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
                              pos.price_open, pos.volume, datetime.datetime.utcnow())
            intent = eng.on_tick(datetime.datetime.utcnow(), tick.bid, tick.ask, acc.balance)
            if intent:
                if not EXECUTE_ENABLED:
                    log({"event": "DRY_RUN_intent", **intent})
                elif intent["action"] == "enter" and pos is None:
                    r = send_market(intent["dir"], intent["lots"], intent["sl"])
                    if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                        eng.on_filled(intent["dir"], r.price, intent["lots"], datetime.datetime.utcnow())
                elif intent["action"] == "amend_sl" and pos is not None:
                    amend_sl(pos.ticket, intent["sl"])
                elif intent["action"] == "close" and pos is not None:
                    close_position(pos)
        except Exception as e:
            log({"event": "loop_error", "error": repr(e)})
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
