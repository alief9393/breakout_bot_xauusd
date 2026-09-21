#!/usr/bin/env python3
"""
StreamTrade — ingest ANY channel's history + legitimacy report (generalized).

Usage:  ./venv/bin/python ingest_channel.py @Btcusd_Forex_Xauusd

Pulls the channel's full history to messages_<name>.jsonl, prints the legitimacy
report (deleted / edited counts), and previews sample signals per instrument so we
can build the parser. Reuses your saved Telegram session (no re-login) — but STOP
live.py first (Ctrl+C) so the two don't fight over the session file.
"""

import os
import sys
import re
import json
import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION = os.getenv("TG_SESSION", "streamtrade")
CHANNEL = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TG_CHANNEL", "")

if not CHANNEL:
    raise SystemExit("Usage: ingest_channel.py @channelname")

SAFE = re.sub(r'[^A-Za-z0-9]', '_', CHANNEL).strip("_")
OUT = f"messages_{SAFE}.jsonl"


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"Logged in as @{me.username}. Reading {CHANNEL} ...")
    try:
        entity = await client.get_entity(CHANNEL)
    except Exception as e:
        raise SystemExit(f"Cannot access {CHANNEL}: {e}\n"
                         f"Make sure the account @{me.username} is a MEMBER of that channel.")
    title = getattr(entity, "title", CHANNEL)

    msgs = []
    async for m in client.iter_messages(entity, limit=None):
        msgs.append({"id": m.id, "date": m.date.isoformat() if m.date else None,
                     "text": (m.message or "").replace("\r", " "),
                     "edited": m.edit_date.isoformat() if m.edit_date else ""})
        if len(msgs) % 500 == 0:
            print(f"   ... {len(msgs)} messages")
    msgs.sort(key=lambda x: x["id"])
    with open(OUT, "w") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

    # legitimacy report
    ids = [m["id"] for m in msgs]
    lo, hi = ids[0], ids[-1]
    missing = (hi - lo + 1) - len(ids)
    edited = sum(1 for m in msgs if m["edited"])
    print("\n" + "═" * 60)
    print(f"LEGITIMACY REPORT — {title}")
    print("═" * 60)
    print(f"  messages:  {len(msgs):,}   span {msgs[0]['date'][:10]} → {msgs[-1]['date'][:10]}")
    print(f"  DELETED (missing ids): {missing:,} ({100*missing/(hi-lo+1):.0f}%)")
    print(f"  EDITED after posting:  {edited:,} ({100*edited/len(msgs):.0f}%)")

    # instrument breakdown + BTC signal previews
    def has(t, *w): return any(x in t.upper() for x in w)
    sigmsgs = [m for m in msgs if has(m["text"], "BUY", "SELL") and re.search(r'\bSL\b', m["text"].upper())]
    btc = [m for m in sigmsgs if has(m["text"], "BTC")]
    xau = [m for m in sigmsgs if has(m["text"], "XAU", "GOLD")]
    fx = [m for m in sigmsgs if not has(m["text"], "BTC", "XAU", "GOLD")]
    print(f"\n  full signals (BUY/SELL + SL): {len(sigmsgs)}")
    print(f"    BTC: {len(btc)}   XAU/GOLD: {len(xau)}   other/forex: {len(fx)}")
    print("\n  ── SAMPLE BTC SIGNALS (first 5) ──")
    for m in btc[:5]:
        print(f"\n  [id {m['id']} {m['date'][:16]} edited={'Y' if m['edited'] else 'N'}]")
        print("   " + m["text"][:300].replace("\n", "\n   "))
    print(f"\nSaved {len(msgs):,} messages to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
