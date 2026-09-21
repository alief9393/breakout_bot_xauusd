# StreamTrade

Verify, backtest, and (if legit) auto-trade a Telegram trading-signals channel.

**Target channel:** `@Raahimbfxproo`  ·  **Execution:** IC Markets cTrader (gold/XAUUSD)

## Pipeline
1. **Ingest** (`ingest.py`) — pull the channel's full history + detect **deleted/edited** messages (hidden losses).
2. **Parse** — extract BUY/SELL signals with entry, TP1..N, SL, and outcome updates.
3. **Verify** — replay each signal against **real XAUUSD price data** to check if TP or SL hit *first* — their true win rate, not their claimed one.
4. **Backtest** — honest P&L with spread + commission + slippage + post→fill latency.
5. **Live** — stream new posts → parse → execute on cTrader with sizing + TP/SL.

## Setup
```bash
cp .env.example .env          # then fill TG_API_ID + TG_API_HASH (from my.telegram.org)
./venv/bin/python ingest.py   # FIRST run: enter your phone + the code Telegram texts you (one time)
```
The first run logs in and saves a `.session` file (gitignored — it's a full login to your account, keep it secret). After that, runs are automatic.

Output: `messages.jsonl` / `messages.csv` + a **legitimacy report** (how many posts were deleted/edited).
