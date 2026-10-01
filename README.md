# breakout_bot_xauusd

A trend-following bot for **gold (XAUUSD)**: Donchian breakout entry + let-winners-run exit.
One tested strategy core, a self-contained backtest, and two live execution engines (cTrader + MT5).

> **The edge is money management, not prediction.** ~39% of trades win, but winners run far
> (avg **+0.41R**/trade). Cut losers at −1R, trail winners at 2R, stay flat in chop.

## The strategy
- **Entry:** go LONG when price breaks the prior **N-day high**, SHORT when it breaks the N-day low.
  The breakout *is* the regime filter — no trade in chop.
- **Stop:** hard **−1R** (150 pips = $15 on gold). Losers capped.
- **Exit:** no take-profit — trail the stop **2R** behind the best price; force-close after 7 days.
- **Sizing:** risk a fixed **%** of balance per trade, compounding, lots clamped [0.01, 50].

## Layout
```
core/breakout_core.py     # shared strategy state machine (both live engines import this)
backtest/backtest.py      # self-contained backtest (stdlib only)
backtest/fetch_prices.py  # downloads M1 OHLC from cTrader -> data/xauusd_m1.csv
live/live_ctrader.py      # cTrader execution engine (runs anywhere)
live/live_mt5.py          # MT5 execution engine (Windows only)
data/                     # price CSVs land here (gitignored)
CLAUDE.md                 # full brief for an AI assistant / new operator
```

## Quickstart
```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill in your cTrader credentials

python backtest/fetch_prices.py # pull gold M1 history -> data/xauusd_m1.csv
python backtest/backtest.py     # run the backtest
```
To go live, start on a **demo** account with `EXECUTE_ENABLED=False` (dry-run), watch the logs,
then flip to `True`. Details in [CLAUDE.md](CLAUDE.md).

## Honest caveats
- Validated on **2023–2026 gold — a bull market.** It survived walk-forward (~1,439× out-of-sample,
  beat buy-and-hold in 7/8 unseen blocks) but has **never faced a sustained gold bear.**
- The **−70% drawdown is real.** Size risk small, **never leverage.**
- This is research code for education, not financial advice. Trade at your own risk.
