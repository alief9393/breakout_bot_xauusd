# CLAUDE.md — Breakout Bot (XAUUSD) — Project Brief & Operator Guide

> This file auto-loads into a Claude Code session opened in this folder. It is the complete
> handoff: what this project is, what was learned, the final strategy, how to run it live on
> MT5/cTrader, the config, and the honest caveats. Read it fully before acting.

---

## 1. What this project is (the short version)

It started as a tool to **validate a gold (XAUUSD) Telegram signal channel** against real price, and
evolved into **building and validating our own trading strategy** with rigorous backtests. The
end product is a **Donchian breakout trend-following bot for XAUUSD** with disciplined money
management, plus two live execution engines (**MT5** and **cTrader**).

**The honest headline:** the strategy is a real trend-following edge **on gold 2023–2026 (a strong
bull/trending period)**. It has **NOT yet been validated out-of-sample** on gold's choppy/bear
years. Treat all backtest returns as the optimistic ceiling until that validation is done.

---

## 2. The final strategy (what the bot trades)

**Donchian breakout + let-winners-run money management.** Rules:

1. **Entry (the regime detector):** enter **LONG** when price breaks the prior **N-day HIGH**;
   **SHORT** when it breaks the prior **N-day LOW**. `N = DONCHIAN_N` (3–5 days). The breakout
   *is* the trend-start signal — the bot is flat (no trade) when price stays inside the range (chop).
2. **Stop (cut losers):** hard stop **1R** away, where `1R = SL_PIPS` (150 pips = 15.0 price on gold).
   Every loser is capped at −1R.
3. **Exit (let winners run):** **no fixed take-profit.** Trail the stop **`TRAIL_R` (2R)** behind
   the favorable extreme — only moves in your favor. You ride a trend until it pulls back 2R.
4. **One position at a time.** After a close, **cooldown 240 min** before re-arming.
5. **Max hold 7 days** then force-close.
6. **Sizing:** risk `RISK_PCT` of balance over the 1R stop; lots rounded to 0.01, clamped
   **[0.01, 50]** (broker min/max).

**Why this shape:** win rate is only ~36–43%, but each **winner runs far** (avg **+0.4 to +0.7 R**)
while losers are capped at −1R. The asymmetry (small capped losses, uncapped trend winners) is the
entire edge. This is textbook trend-following (how CTAs trade).

---

## 3. Recommended config (settled values)

| Field | Value | Note |
|---|---|---|
| `DONCHIAN_N` | **3–5** | 3–5 all similar; 3 scored highest but that's likely overfit. 5 is safer. Below 3 = too noisy. |
| `SL_PIPS` | 150 | 1R |
| `TRAIL_R` | 2.0 | let-winners-run leash |
| `RISK_PCT` | **0.02–0.07** | 2% ≈ −25% DD; 5% ≈ −51% DD; 7% ≈ −63% DD. **Never 10%+ (blows up).** |
| `HORIZON_DAYS` | 7 | longer holds captured more trend in tests (5–7 good) |
| `COOLDOWN_MIN` | 240 | |
| `MIN_LOT / MAX_LOT` | 0.01 / 50 | broker limits — see §7 |

**Backtest reference (gold 2023–2026, lot limits applied):**
- 5d, 7% risk, $300 start → ~$2.5M (−63% DD, 39% win, +0.46R, 67% months positive), ~385 trades.
- 20d, 2% risk → ~191 trades, −25% DD (smoothest).
- These are **one 3-year bull** — not a forward promise.

**Withdrawal plan (operator's real risk control):** start small, **withdraw your initial stake as
soon as the account has grown enough to pull it out** ("recover first"), then harvest profits
routinely (e.g. 50%/month). Once your capital is out, deep drawdowns are on house money. The single
danger is a big drawdown *before* you've recovered your initial — so recover early.

---

## 4. Repo layout

```
StreamTrade/
├─ CLAUDE.md                     ← this file
├─ README.md, requirements.txt
├─ .env.example                  ← template; real .env is gitignored (recreate it)
│
├─ live_breakout/                ← THE LIVE BOT (what you deploy)
│  ├─ breakout_core.py           ← shared strategy state machine (platform-agnostic, self-tested)
│  ├─ live_mt5_breakout.py       ← MT5 engine (WINDOWS ONLY)
│  ├─ live_ctrader_breakout.py   ← cTrader engine (runs anywhere; verified connects)
│  └─ .env.example               ← template
│
├─ research/                     ← strategy research & backtests
│  ├─ breakout.py                ← MAIN backtest for the live strategy (Donchian + trail)
│  ├─ mm_trend.py                ← trend-following baseline (daily entry) — for comparison
│  ├─ mm_backtest.py             ← money-management engine (let-winners-run), per-month + range
│  ├─ money_mgmt.py              ← exit-scheme comparison (proved let-winners-run wins)
│  ├─ dca_grid.py                ← DCA/grid test (proved it's a trap — high win, blows up)
│  ├─ regime_switch.py           ← regime-switch test (proved switching-to-mean-reversion fails)
│  └─ (momentum_*, edge_filters, morning_edge, ...) ← earlier research, mostly negative results
│
├─ CoinHunter/                   ← crypto channel validator (Binance spot + OKX price gating)
│  ├─ ingest.py, parse_crypto.py, binance.py, prices.py, backtest.py
│
├─ bt_dynamic_split.py           ← the shared backtest engine (load_prices, simulate, plan_split)
├─ fetch_prices.py               ← pulls XAUUSD M1 history from cTrader -> xauusd_m1_ext.csv
├─ live_validate.py              ← gold-channel PAPER validator (truth-meter, deletion detection)
├─ live_execute.py               ← gold-channel real-demo executor (dynamic-split, early-alert)
└─ xauusd_m1_ext.csv             ← price data (GITIGNORED, 57MB — regenerate, see §6)
```

**Not in the repo (gitignored, recreate locally):** `.env` files, `*.session` (Telegram auth),
`venv/`, `*.csv`, runtime `*_log.jsonl` / `*_state.json`.

---

## 5. Windows VPS setup (the deployment target)

```powershell
# 1. Install Python 3.9+ and Git. Clone:
git clone git@github.com:alief9393/breakout_bot_xauusd.git
cd breakout_bot_xauusd

# 2. Virtual env + deps
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install MetaTrader5 python-dotenv        # MetaTrader5 is Windows-only

# 3. Install & log into the MT5 terminal (e.g. Exness MT5), symbol XAUUSD visible in Market Watch.

# 4. Recreate the env for the live engine:
copy live_breakout\.env.example live_breakout\.env
#   edit live_breakout\.env  -> set EXECUTE_ENABLED=False first (dry-run),
#   MT5_LOGIN / MT5_PASSWORD / MT5_SERVER (or leave blank if terminal already logged in)
#   for cTrader: CTRADER_CLIENT_ID / SECRET / ACCESS_TOKEN / HOST_TYPE
```

---

## 6. Running the live engines

**Always: dry-run → demo → live. Never skip.**

### MT5 (Windows)
```powershell
cd live_breakout
python live_mt5_breakout.py
```
- `EXECUTE_ENABLED=False` → logs `DRY_RUN_intent` lines, **sends no orders**. Watch these for a day;
  confirm entries/SLs/trailing look sane.
- Then `EXECUTE_ENABLED=True` on a **DEMO** account. Confirm real fills + the trailing-stop amends.
- Only then, live, small. **The MT5 engine has never been run live — verify its first trades by hand.**

### cTrader (runs anywhere; already verified it connects)
```bash
python live_ctrader_breakout.py
```
Same `EXECUTE_ENABLED` flow. Uses `CTRADER_*` creds. Auto-reconnects on drops.

### Both engines
- Poll every 15s, refresh the N-day Donchian levels daily, act on breakouts, trail the stop, cooldown.
- Log to `mt5_breakout_log.jsonl` / `ctrader_breakout_log.jsonl` (gitignored).
- **One engine per broker account** — running both on the same account double-sizes. Use separate accounts.

### Keep it alive on the VPS
- **Windows:** Task Scheduler (start on boot, restart on failure) or NSSM as a service. Enable MT5 auto-login.
- **Linux (cTrader only):** `tmux` or a `systemd` service.

---

## 7. Lot sizing reality (important)

`lots = risk% × balance ÷ (SL_PIPS × $10/lot)`, rounded to 0.01, clamped [0.01, 50]:
- **Small balance → forced over-risk.** At $100 with 150-pip SL, the 0.01 min lot risks ~$15 = 15%,
  not your intended %. Need **~$300+** for a 5% setting to be honest. The engines log `min_clamp`.
- **Large balance → capped.** You hit the 50-lot ceiling around ~$1.5M (at 5%); growth flattens
  past that. Backtest "millions×" numbers ignore real 50-lot slippage — treat them skeptically.

---

## 8. Running the backtests / research

The backtests need the price CSV, which is gitignored. Regenerate it first:
```bash
python fetch_prices.py        # pulls XAUUSD M1 from cTrader -> xauusd_m1_ext.csv (needs CTRADER_* creds in root .env)
```
Then:
```bash
python research/breakout.py   # the main strategy backtest (edit CONFIG at top)
python research/mm_trend.py   # trend baseline for comparison
```
Both have a CONFIG block up top: balance, risk, Donchian length, hold, `START_YM`/`END_YM` range,
costs, min/max lot. Per-month tables + drawdown + clamp counts print out.

---

## 9. Key lessons learned (do NOT relitigate these)

- **Win rate ≠ profit.** A 94%-win DCA grid lost −176%; a 40%-win breakout made money. Only
  **expectancy (avg R × frequency)** matters. Chasing win rate is the classic trap.
- **DCA / grid / averaging-down is dangerous** (not "fake" — real math, dangerous). High win rate,
  rare catastrophic tail, negative expectancy on a driftless series. Tested and rejected.
- **Regime detection lags.** Switching to mean-reversion in "chop" broke more (big trend months) than
  it fixed. The fix is the **breakout entry itself** (self-selecting regime detector, fail-safe: flat in chop).
- **The gold signal channel is not a real edge.** `@Raahimbfxproo` (later renamed `@XAUUSDSINGLE0`)
  posts stale/fabricated-timing TP claims, deletes losing signals (survivorship), and ran a
  **recovery scam** (deleted "I'll recover your losses, DM @HadiFX4" bait — caught by our deletion
  detector). Real-price gating showed following it is ~breakeven-to-negative. **Never trust a signal
  channel's self-reported record; gate against real price.** The CoinHunter crypto channel was
  similar (honest format, but ~breakeven when price-gated).
- **20%/day is arithmetically impossible** (compounds to more than all money on Earth in a year).
  It's the signature of a scam, not a target.
- **The breakout edge is regime-dependent** — it needs a trending market. Proven on gold 2023–26;
  **unproven on gold's choppy/bear years.** This is the #1 open validation.

---

## 10. Immediate next steps (priority order)

1. **OUT-OF-SAMPLE VALIDATION (do before real money).** Fetch older gold data (≈2013–2023, incl.
   choppy/down years) and run `research/breakout.py` on it. If it stays positive → real edge. If it
   collapses → it was riding the 2023–26 trend. This is the go/no-go test.
2. **Forward paper-test** the live engine (dry-run → demo) for a few weeks on real incoming data.
3. **Deploy** on the VPS with the withdrawal plan; recover initial early, harvest routinely, size at
   a drawdown you can stomach (1–2% risk = −25%; 5% = −51%; 7% = −63%).

---

## 11. Security

- **Never commit** `.env` or `*.session` (Telegram auth keys = full account access). `.gitignore`
  already blocks them. If you add secrets, keep them out of git.
- The repo is **public** on `github.com/alief9393/breakout_bot_xauusd`. Only code + `.env.example`
  templates are in it. Recreate `.env` files locally from the templates.
- Git identity for this repo is the **personal** account (`aliefchandra10@gmail.com` / `alief9393`),
  set locally; the machine's global git is the work account — don't let commits here use it.

---

## 12. Working style (for the Claude Code session)

- **Be evidence-driven, verify before concluding** — don't declare things fail/succeed without
  checking the data (this was a repeated correction during development).
- **Be honest and calibrated** — no hype, state drawdowns and caveats plainly, but don't be a doomer.
- **Real-order code is sensitive** — default to dry-run, confirm on demo, never ship untested live logic.
- The operator prefers **concise, direct answers** with the numbers up front.
