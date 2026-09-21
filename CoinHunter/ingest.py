#!/usr/bin/env python3
"""Pull the crypto channel, parse signals, save to signals.jsonl.
Uses its OWN 'coinhunter' Telegram session (first run asks phone + code — a distinct login,
so it never collides with the StreamTrade bots' sessions)."""
import os, json, asyncio, datetime
from telethon import TelegramClient
from parse_crypto import parse_crypto

HERE = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(HERE, ".env")):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
CHANNEL = os.environ.get("TG_CHANNEL", "@cryptoninjas_trading_ann")
SESSION = os.environ.get("TG_SESSION", "coinhunter")
LIMIT = int(os.environ.get("INGEST_LIMIT", "500"))
OUT = os.path.join(HERE, "signals.jsonl")


async def main():
    # reuse an already-authenticated session if present (avoids another login):
    # prefer CoinHunter/<SESSION>, else the StreamTrade/explore session that's already logged in
    cand = [os.path.join(HERE, SESSION), os.path.join(HERE, "..", "explore")]
    sess = next((c for c in cand if os.path.exists(c + ".session")), cand[0])
    client = TelegramClient(sess, API_ID, API_HASH)
    await client.start()
    entity = await client.get_entity(CHANNEL)
    msgs = []
    async for m in client.iter_messages(entity, limit=LIMIT):
        if m.date and (m.message or "").strip():
            msgs.append(m)
    await client.disconnect()
    msgs.sort(key=lambda m: m.id)

    sigs = []
    for m in msgs:
        s = parse_crypto(m.message)
        if not s:
            continue
        s["id"] = m.id
        s["date"] = m.date.astimezone(datetime.timezone.utc).isoformat()
        sigs.append(s)

    with open(OUT, "w") as f:
        for s in sigs:
            f.write(json.dumps(s) + "\n")

    longs = sum(1 for s in sigs if s["dir"] == "BUY")
    coins = sorted({s["symbol"] for s in sigs})
    print(f"pulled {len(msgs)} text messages · parsed {len(sigs)} signals -> {OUT}")
    print(f"  {longs} LONG / {len(sigs)-longs} SHORT · {len(coins)} distinct coins")
    if sigs:
        print(f"  range {sigs[0]['date'][:10]} -> {sigs[-1]['date'][:10]}")
        print(f"  coins: {', '.join(coins)}")


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
