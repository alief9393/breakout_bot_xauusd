# CLAUDE.md — breakout_bot_xauusd

Brief for an AI assistant (or a new operator) picking up this project. Read this first.

## What this is
A trend-following trading bot for **gold (XAUUSD)**. One strategy — a **Donchian breakout with a
let-winners-run exit** — expressed three ways that share the same logic:
- `core/breakout_core.py` — the strategy as a pure state machine (the single source of truth)
- `backtest/backtest.py` — historical simulation (stdlib only)
- `live/live_ctrader.py` and `live/live_mt5.py` — execution engines that import the core

If you change the strategy, change it in `core/breakout_core.py`; the live engines inherit it.
`backtest/backtest.py` re-implements the same rules standalone — keep the two in sync.

## The strategy (and WHY it works)
- **Entry:** LONG when price breaks the prior **N-day high**, SHORT when it breaks the N-day low.
  The breakout itself is the regime detector → the bot is **flat in chop, long/short only in trends.**
- **Stop (1R):** hard stop **SL_PIPS = 150** (gold pip = 0.1 price, so 150 pips = $15) from entry.
  Losers are capped at **−1R**.
- **Exit:** **no take-profit.** Trail the stop **TRAIL_R = 2R** behind the favorable extreme; force-close
  after **HORIZON_DAYS = 7**.
- **Sizing:** risk **RISK_PCT** of balance per trade, compounding, lots rounded to 0.01 and clamped
  to [MIN_LOT 0.01, MAX_LOT 50].

**The edge is money management, not prediction.** Win rate is only ~39% — most trades lose small at
−1R — but the few winners run far, giving **+0.41R average per trade**. *Win rate is a lying metric;
expectancy (avg R × frequency) is what pays.* Do not "improve" it by adding a take-profit or tightening
the trail — that caps the winners and kills the edge (tested; it does).

## Repo layout
```
core/breakout_core.py     # Config + BreakoutEngine state machine; run it for a self-test
backtest/backtest.py      # edit CONFIG at top, run from repo root
backtest/fetch_prices.py  # cTrader M1 OHLC downloader -> data/xauusd_m1.csv
live/live_ctrader.py      # cTrader engine (Twisted/protobuf; runs on any OS incl. a cheap Linux VPS)
live/live_mt5.py          # MT5 engine (needs the MetaTrader5 package + terminal; WINDOWS ONLY)
data/                     # price CSVs (gitignored — regenerate with fetch_prices.py)
.env.example              # copy to .env, fill cTrader/MT5 creds
```

## How to run
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in cTrader credentials

python backtest/fetch_prices.py # downloads gold M1 -> data/xauusd_m1.csv (needs .env)
python backtest/backtest.py     # backtest
python core/breakout_core.py    # engine self-test (no data/creds needed)
```
**Live, in order — never skip a step:**
1. `EXECUTE_ENABLED=False` (default) → run `live/live_ctrader.py`: it logs the intents it *would* send, trades nothing. Watch it for a day.
2. Set `EXECUTE_ENABLED=True` on a **DEMO** account (`CTRADER_HOST_TYPE=demo`). Watch it place/trail/close real orders on fake money.
3. Only then consider a live account with **small** risk. On the MT5 side use `live/live_mt5.py` on a Windows VPS with the terminal logged in.

## Config knobs (same names in backtest & core)
`DONCHIAN_N` breakout lookback in days (robust 3–20; shorter = more trades) ·
`SL_PIPS` 1R stop · `TRAIL_R` trail distance in R · `RISK_PCT` risk per trade
(0.02 ≈ −25% DD, 0.07 ≈ profit-optimal-but-brutal) · `HORIZON_DAYS` max hold ·
`COOLDOWN_MIN` re-arm delay · `MIN_LOT`/`MAX_LOT` broker clamps.

## Lot-sizing reality (important)
`lots = RISK_PCT × balance ÷ (SL_PIPS × $10/lot)`, rounded to 0.01, clamped [0.01, 50].
- On a tiny balance the 0.01 minimum **forces over-risk** (you can't risk less than one micro-lot).
- On a big balance the 50-lot cap **throttles compounding**.
Both distort results at the extremes — the backtest reports how many trades hit each clamp.

**Leverage note:** a broker's "1:500 / 1:1000" is just the *margin* limit (how little you post to open
a position). It is NOT your risk. Risk is set by **lot size via RISK_PCT**. Keep effective leverage low;
the strategy's −70% drawdown is already punishing — do not amplify it.

## What's been validated (don't re-litigate)
- **Walk-forward (the honest test): ~1,439× out-of-sample**, beat buy-and-hold in **7 of 8** unseen
  blocks. Parameter N≈3 kept being re-selected on past data and kept working forward → a *real*, robust
  edge, not curve-fit. Per-trade +0.41R, stable across N=3…20.
- Tested against ~20 other instruments and several strategy families (mean-reversion, momentum,
  grid, breakout variants) — **gold trend-following was the clear winner.** Silver/oil/indices/FX/crypto
  were all weaker or lost. Don't expect another instrument to beat gold here.

## Honest caveats / the one open gate
- The entire sample is **2023–2026 gold — a bull market.** The strategy has **never faced a sustained
  gold bear.** That is the single go/no-go before real money: **re-run the backtest + walk-forward on
  older gold data that includes bear years (≈2013 crash, 2015–2018 grind).** If it survives that, trust it.
- **−70% drawdown is real.** Expect to watch the account fall by most of its peak and not flinch.
- Headline multiples ride M1 fill optimism + trade frequency; the *edge* (+0.41R) is the durable part,
  the giant end number is idealized.

## Security / hygiene
- **Never commit** `.env` or `*.session` (gitignored). Credentials live only in `.env`.
- Price CSVs are gitignored — regenerate with `fetch_prices.py`, don't commit data.
- Always start live work on **demo** with `EXECUTE_ENABLED=False`.

## Working style
Be evidence-driven: pull the actual data before making a claim; don't assume. When something looks
too good, suspect look-ahead / curve-fit and test it walk-forward. Keep changes small and in the core.
Prefer honest, survivable returns over fantasy numbers.
