#!/usr/bin/env python3
"""
Shared breakout strategy core — platform-agnostic. Both the MT5 and cTrader engines
import this so the logic is IDENTICAL; only order plumbing differs per platform.

Strategy (matches research/breakout.py):
  * enter LONG when price breaks the prior N-day HIGH, SHORT when it breaks the N-day LOW
  * hard stop 1R (SL_PIPS) below/above entry — losers capped at -1R
  * NO take-profit; trail the stop TRAIL_R behind the favorable extreme (let winners run)
  * one position at a time; COOLDOWN after a close before re-arming
  * max hold HORIZON_DAYS then force-close

The engine is a pure state machine: feed it daily bars + price ticks + account state,
it emits intents ('enter' / 'amend_sl' / 'close'). The adapter executes them on the broker.
"""
import datetime

GOLD_PIP = 0.1                 # XAUUSD: 1 pip = 0.1 price
PIP_VALUE_PER_LOT = 10.0       # $/pip per 1.0 lot (1 lot = 100oz)


class Config:
    def __init__(self, **kw):
        self.symbol       = kw.get("symbol", "XAUUSD")
        self.donchian_n   = kw.get("donchian_n", 3)
        self.sl_pips      = kw.get("sl_pips", 150)
        self.trail_r      = kw.get("trail_r", 2.0)
        self.risk_pct     = kw.get("risk_pct", 0.07)
        self.horizon_days = kw.get("horizon_days", 7)
        self.cooldown_min = kw.get("cooldown_min", 240)
        self.min_lot      = kw.get("min_lot", 0.01)
        self.max_lot      = kw.get("max_lot", 50.0)
        self.pip          = kw.get("pip", GOLD_PIP)


def size_lots(cfg, balance):
    """Risk cfg.risk_pct of balance over a sl_pips stop; round to 0.01, clamp [min,max]."""
    raw = (cfg.risk_pct * balance) / (cfg.sl_pips * PIP_VALUE_PER_LOT)
    lots = round(raw, 2)
    return min(cfg.max_lot, max(cfg.min_lot, lots)), (lots < cfg.min_lot), (lots > cfg.max_lot)


class BreakoutEngine:
    """One-position breakout state machine. Times are UTC datetimes; prices are floats."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.nh = None; self.nl = None            # current N-day donchian high/low
        self.pos = None                           # None or dict(dir, entry, stop, extreme, opened, lots)
        self.cooldown_until = None

    # ---- daily levels ----
    def update_donchian(self, daily):
        """daily: ordered list of (date, high, low), oldest->newest, EXCLUDING today.
           Sets the breakout levels from the last donchian_n completed days."""
        n = self.cfg.donchian_n
        if len(daily) < n:
            self.nh = self.nl = None
            return
        window = daily[-n:]
        self.nh = max(h for _, h, _ in window)
        self.nl = min(l for _, _, l in window)

    # ---- called by adapter when its own order actually fills ----
    def on_filled(self, direction, fill_price, lots, now):
        R = self.cfg.sl_pips * self.cfg.pip
        d = 1 if direction == "BUY" else -1
        self.pos = {"dir": direction, "d": d, "entry": fill_price, "lots": lots,
                    "stop": fill_price - d * R, "extreme": fill_price, "opened": now}

    def on_closed(self, now):
        self.pos = None
        self.cooldown_until = now + datetime.timedelta(minutes=self.cfg.cooldown_min)

    # ---- main tick ----
    def on_tick(self, now, bid, ask, balance):
        """Return an intent dict or None.
           intents: {'action':'enter', dir, ref_price, sl, lots}
                    {'action':'amend_sl', sl}
                    {'action':'close', reason}"""
        cfg = self.cfg
        R = cfg.sl_pips * cfg.pip
        mid = (bid + ask) / 2.0

        if self.pos is None:
            if self.cooldown_until and now < self.cooldown_until:
                return None
            if self.nh is None:
                return None
            # breakout detection on the mid
            if mid >= self.nh:
                lots, mn, mx = size_lots(cfg, balance)
                return {"action": "enter", "dir": "BUY", "ref_price": mid,
                        "sl": round(ask - R, 2), "lots": lots, "min_clamp": mn, "max_clamp": mx}
            if mid <= self.nl:
                lots, mn, mx = size_lots(cfg, balance)
                return {"action": "enter", "dir": "SELL", "ref_price": mid,
                        "sl": round(bid + R, 2), "lots": lots, "min_clamp": mn, "max_clamp": mx}
            return None

        # in a position: max-hold timeout?
        if now - self.pos["opened"] > datetime.timedelta(days=cfg.horizon_days):
            return {"action": "close", "reason": "max_hold"}

        # trail the stop behind the favorable extreme
        p = self.pos; d = p["d"]
        if d == 1:
            p["extreme"] = max(p["extreme"], bid)
            new_stop = p["extreme"] - cfg.trail_r * R
            if new_stop > p["stop"] + 1e-9:
                p["stop"] = new_stop
                return {"action": "amend_sl", "sl": round(new_stop, 2)}
        else:
            p["extreme"] = min(p["extreme"], ask)
            new_stop = p["extreme"] + cfg.trail_r * R
            if new_stop < p["stop"] - 1e-9:
                p["stop"] = new_stop
                return {"action": "amend_sl", "sl": round(new_stop, 2)}
        return None


if __name__ == "__main__":
    # tiny self-test: breakout -> trail -> stop
    cfg = Config(donchian_n=3, sl_pips=150, trail_r=2.0, risk_pct=0.07)
    eng = BreakoutEngine(cfg)
    eng.update_donchian([(1, 4000, 3980), (2, 4010, 3990), (3, 4005, 3985)])   # NH=4010 NL=3980
    t0 = datetime.datetime(2026, 1, 1, 0, 0)
    print("NH/NL:", eng.nh, eng.nl)
    intent = eng.on_tick(t0, 4011, 4011.2, 1000)      # breaks 4010 -> BUY
    print("tick1:", intent)
    eng.on_filled("BUY", 4011.2, intent["lots"], t0)
    print("filled entry, stop:", eng.pos["stop"], "(should be ~4011.2 - 15 = 3996.2)")
    intent = eng.on_tick(t0 + datetime.timedelta(minutes=5), 4040, 4040.2, 1000)  # runs up
    print("tick2 (price 4040):", intent, "(should amend_sl to ~4040-30=4010)")
    lots, mn, mx = size_lots(cfg, 1000)
    print("size @ $1000, 7% risk:", lots, "lots  (7%*1000/1500 = 0.047 -> 0.05)")
