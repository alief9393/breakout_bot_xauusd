#!/usr/bin/env python3
"""
StreamTrade — Stage 1+2: pull a Telegram channel's full history and detect
deleted / edited messages (the legitimacy check).

How deletion detection works: Telegram assigns SEQUENTIAL ids to a channel's
posts. If we pull every message and the ids jump (…104, 105, 108…), then 106 and
107 were deleted. Gaps = hidden messages. For a signals channel, that's where
losing trades go to disappear.

FIRST RUN logs you in — it asks for your phone number and the code Telegram sends
you (one time). Run it in YOUR terminal (it needs interactive input):

    ./venv/bin/python ingest.py

After that a .session file is saved and future runs are automatic.
"""

import os
import csv
import json
import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()
API_ID = os.getenv("TG_API_ID", "")
API_HASH = os.getenv("TG_API_HASH", "")
CHANNEL = os.getenv("TG_CHANNEL", "")
SESSION = os.getenv("TG_SESSION", "streamtrade")

for name, val in [("TG_API_ID", API_ID), ("TG_API_HASH", API_HASH), ("TG_CHANNEL", CHANNEL)]:
    if not val:
        raise SystemExit(f"Missing {name} in .env — copy .env.example to .env and fill it in.")

OUT_JSONL = "messages.jsonl"
OUT_CSV = "messages.csv"


async def main():
    client = TelegramClient(SESSION, int(API_ID), API_HASH)
    await client.start()  # interactive phone-code login on first run
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username}).")

    entity = await client.get_entity(CHANNEL)
    title = getattr(entity, "title", CHANNEL)
    print(f"Reading channel: {title}\nPulling full history (this can take a while) ...")

    msgs = []
    async for m in client.iter_messages(entity, limit=None):
        msgs.append({
            "id": m.id,
            "date": m.date.isoformat() if m.date else None,
            "text": (m.message or "").replace("\r", " "),
            "edited": m.edit_date.isoformat() if m.edit_date else "",
            "fwd": bool(m.fwd_from),
            "reply_to": m.reply_to_msg_id or "",
            "has_media": bool(m.media),
            "views": getattr(m, "views", "") or "",
        })
        if len(msgs) % 500 == 0:
            print(f"   ... {len(msgs)} messages")

    msgs.sort(key=lambda x: x["id"])
    with open(OUT_JSONL, "w") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(msgs[0].keys()))
        w.writeheader(); w.writerows(msgs)

    analyze(msgs, title)
    await client.disconnect()


def analyze(msgs, title):
    ids = [m["id"] for m in msgs]
    lo, hi = ids[0], ids[-1]
    present = set(ids)
    missing = [i for i in range(lo, hi + 1) if i not in present]
    edited = [m for m in msgs if m["edited"]]
    expected = hi - lo + 1

    print("\n" + "═" * 60)
    print(f"LEGITIMACY REPORT — {title}")
    print("═" * 60)
    print(f"  messages pulled:      {len(msgs):,}")
    print(f"  id range:             {lo} … {hi}  (expected {expected:,} if none deleted)")
    print(f"  span:                 {msgs[0]['date']}  →  {msgs[-1]['date']}")
    print(f"  DELETED (missing ids): {len(missing):,}  "
          f"({100*len(missing)/expected:.1f}% of the sequence)")
    print(f"  EDITED after posting:  {len(edited):,}")
    if missing:
        # show where the deletions cluster (first 30)
        print(f"  first deleted ids:    {missing[:30]}{' …' if len(missing) > 30 else ''}")
    print("\n  READ: a signals channel with MANY deleted/edited posts is hiding")
    print("        something — likely losing trades. We cross-check against real")
    print("        gold prices next, so their claimed wins get verified regardless.")
    print(f"\nSaved {len(msgs):,} messages to {OUT_JSONL} and {OUT_CSV}")


if __name__ == "__main__":
    asyncio.run(main())
