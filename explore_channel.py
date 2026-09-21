#!/usr/bin/env python3
"""
Throwaway channel explorer — pulls recent messages from any channel for analysis.
Uses its OWN separate session ('explore') so it never collides with the live bots'
sessions. First run asks for phone + code (a distinct login, like another device).

Edit CHANNEL / LIMIT below, then:  ./venv/bin/python explore_channel.py
"""
import os, datetime
from telethon import TelegramClient

CHANNEL = "@cryptoninjas_trading_ann"   # <- change to explore any channel
LIMIT   = 120                            # how many recent messages to pull

# load Telegram creds from .env
for line in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]


async def main():
    client = TelegramClient("explore", API_ID, API_HASH)   # separate session file
    await client.start()
    entity = await client.get_entity(CHANNEL)
    print(f"=== {CHANNEL} · {getattr(entity, 'title', '')} · last {LIMIT} messages (UTC) ===\n")
    rows = []
    async for msg in client.iter_messages(entity, limit=LIMIT):
        if not msg.date:
            continue
        d = msg.date.astimezone(datetime.timezone.utc)
        txt = (msg.message or "").replace("\n", " ⏎ ").strip()
        rows.append((msg.id, d.strftime("%m-%d %H:%M"), txt))
    rows.sort()
    prev = None
    for mid, hm, txt in rows:
        if prev is not None and mid != prev + 1:
            miss = list(range(prev + 1, mid))
            print(f"   ⚠️ GAP ids {miss[0]}–{miss[-1]} ({len(miss)} deleted/non-text)")
        print(f"[{hm}] id={mid}  {txt[:220]}")
        prev = mid
    await client.disconnect()
    print(f"\n(pulled {len(rows)} messages)")


import asyncio
asyncio.get_event_loop().run_until_complete(main())
