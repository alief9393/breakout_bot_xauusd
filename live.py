#!/usr/bin/env python3
"""
RETIRED — do not use.

This used to be Stage 5a: signal DETECTION ONLY. It had no price feed, so it could
never tell whether a signal hit TP or SL — it just logged the order it *would* place.

It has been replaced by live_validate.py, which:
  * grabs the REAL cTrader price the moment a signal posts,
  * paper-trades it with scale-out (BE after TP2),
  * follows the live price to TP/SL and records the real outcome,
  * backfills anything missed while down, into one merged record.

The old detection-only code is preserved in live_detect_only.py for reference.
"""

import sys

BANNER = """
────────────────────────────────────────────────────────────────
  live.py is RETIRED — it only DETECTED signals (no price, no outcomes).

  Run the forward-validation bot instead:

      caffeinate -i ./venv/bin/python live_validate.py

  (old detection-only code archived as live_detect_only.py)
────────────────────────────────────────────────────────────────
"""

if __name__ == "__main__":
    print(BANNER)
    sys.exit(1)
