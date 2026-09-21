#!/usr/bin/env python3
"""
StreamTrade — LIVE bot, Stage 5a: real-time signal detection (dry-run), hardened for 24/5.

Streams @Raahimbfxproo and, the moment a new signal posts, parses it and logs the
EXACT trade it would place. No orders yet (Stage 5b adds cTrader demo execution).

Stability for 24/5:
  * auto-reconnect on dropped connections (Telethon), infinite retries
  * supervisor loop restarts the whole bot on any crash (15s backoff)
  * every message handled in its own try/except — one bad post can't kill the bot
  * dedup by message id (reconnects/edits won't double-log)
  * heartbeat every 30 min so you can see it's still alive

Run (macOS, keeps laptop awake while running):
    caffeinate -i ./venv/bin/python live.py
"""

import os
import json
import datetime
import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient, events

from parse import parse_signal   # exact same parser as the backtest

load_dotenv()
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
CHANNEL = os.getenv("TG_CHANNEL", "@Raahimbfxproo")
SESSION = os.getenv("TG_SESSION", "streamtrade")

# ── strategy config (matches the backtests) ──
BALANCE   = 100.0
RISK_PCT  = 0.10
TARGET_TP = 3
CONTRACT_OZ = 100
HEARTBEAT_MIN = 30
LOG_FILE  = "live_log.jsonl"

STATE = {"signals": 0, "messages": 0, "started": None}


def size_lots(entry, sl):
    d = abs(entry - sl)
    return max(0.01, round((BALANCE * RISK_PCT) / (d * CONTRACT_OZ), 2)) if d > 0 else 0.01


def log(obj):
    line = {"t": datetime.datetime.utcnow().isoformat(timespec="seconds"), **obj}
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass  # never let logging kill the bot


async def heartbeat():
    while True:
        await asyncio.sleep(HEARTBEAT_MIN * 60)
        log({"event": "heartbeat", "alive": True,
             "messages_seen": STATE["messages"], "signals_detected": STATE["signals"],
             "uptime_min": round((datetime.datetime.utcnow() - STATE["started"]).total_seconds() / 60)})


async def run_bot():
    # infinite reconnects + auto-reconnect keep the socket alive through drops
    client = TelegramClient(SESSION, API_ID, API_HASH,
                            connection_retries=None, auto_reconnect=True,
                            retry_delay=5, request_retries=5)
    await client.start()
    me = await client.get_me()
    entity = await client.get_entity(CHANNEL)
    STATE["started"] = STATE["started"] or datetime.datetime.utcnow()
    seen = set()

    log({"event": "online", "user": me.username, "channel": getattr(entity, "title", CHANNEL),
         "mode": "DRY-RUN (no orders)", "sizing": f"${BALANCE:.0f}@{RISK_PCT*100:.0f}% TP{TARGET_TP}"})

    @client.on(events.NewMessage(chats=entity))
    async def on_new(event):
        try:
            mid = event.message.id
            if mid in seen:
                return
            seen.add(mid)
            STATE["messages"] += 1
            text = event.message.message or ""
            sig = parse_signal(text)
            if not sig:
                return
            if len(sig["tps"]) < TARGET_TP:
                log({"event": "signal_skipped", "reason": f"<{TARGET_TP} TPs", "text": text[:100]})
                return
            STATE["signals"] += 1
            log({"event": "WOULD_PLACE_ORDER", "dir": sig["dir"], "entry": sig["entry"],
                 "sl": sig["sl"], "tp_target": sig["tps"][TARGET_TP - 1], "all_tps": sig["tps"],
                 "lots": size_lots(sig["entry"], sig["sl"]),
                 "sl_pips": round(abs(sig["entry"] - sig["sl"]) * 10, 1), "note": "DRY-RUN"})
        except Exception as e:
            log({"event": "handler_error", "error": repr(e)})   # isolate: never kill the bot

    hb = asyncio.create_task(heartbeat())
    try:
        await client.run_until_disconnected()
    finally:
        hb.cancel()
        try:
            await client.disconnect()
        except Exception:
            pass


async def main():
    log({"event": "boot", "note": "starting supervisor"})
    while True:                       # supervisor: restart on any crash
        try:
            await run_bot()
            log({"event": "disconnected", "note": "clean disconnect; restarting in 15s"})
        except Exception as e:
            log({"event": "crash", "error": repr(e), "note": "restarting in 15s"})
        await asyncio.sleep(15)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped by user")
