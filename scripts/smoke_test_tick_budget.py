#!/usr/bin/env python3
"""Tick-budget smoke test: forecasting must stay O(1)-per-tick inside the
trading app (the cheap-tier contract).

Checks:
  1. cold start (lazy store + D-CSV load) does NOT trip the fuse
  2. 1000 warm on_snapshot calls each finish < 5 ms (the budget is 50 ms)
  3. an injected slow call (80 ms) still RETURNS its result but TRIPS the fuse
  4. while tripped (same day), on_snapshot returns None and skips the work
  5. a new day auto-re-arms the fuse
  6. reset_fuse() re-arms manually

Exit 0 = all checks pass.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecasting.engine import ForecastEngine, TICK_BUDGET_MS
from forecasting.smoke_test import build_snapshot

_checks = [0, 0]


def check(cond, label):
    _checks[0] += 1
    print("  [%s] %s" % ("OK" if cond else "FAIL", label))
    if cond:
        _checks[1] += 1


def main():
    snap = build_snapshot()
    eng = ForecastEngine()

    # 1. cold start: lazy store + 1.9 MB D-CSV load - exempt from the budget
    t0 = time.perf_counter()
    out = eng.on_snapshot(snap, log=False)
    cold_ms = (time.perf_counter() - t0) * 1000.0
    check(eng._tripped is None,
          "cold start (%.0f ms incl. lazy D-CSV load) does not trip the fuse" % cold_ms)
    check(out is not None, "cold call returns a forecast dict")

    # 2. 1000 warm calls, every one well under budget
    worst = 0.0
    for _ in range(1000):
        t0 = time.perf_counter()
        eng.on_snapshot(snap, log=False)
        worst = max(worst, (time.perf_counter() - t0) * 1000.0)
    check(worst < 5.0,
          "1000 warm calls: worst %.2f ms (< 5 ms; budget %.0f ms)" % (worst, TICK_BUDGET_MS))
    check(eng._tripped is None, "fuse still armed after 1000 calls")

    # 3. injected slow call: returns its result, trips the fuse in the finally
    orig = eng._on_snapshot

    def slow(data, log=True):
        time.sleep(0.08)
        return orig(data, log=False)

    eng._on_snapshot = slow
    out = eng.on_snapshot(snap, log=False)
    check(out is not None, "slow call (80 ms) still returns its result")
    check(eng._tripped is not None, "fuse TRIPPED: %s" % eng._tripped)
    eng._on_snapshot = orig

    # 4. tripped = skipped for the rest of the day
    check(eng.on_snapshot(snap, log=False) is None,
          "tripped: subsequent same-day calls skip (None)")

    # 5. a new day auto-re-arms
    eng._tripped_day = "2000-01-01"
    out = eng.on_snapshot(snap, log=False)
    check(eng._tripped is None and out is not None, "new day: fuse auto-re-arms")

    # 6. manual reset
    eng._tripped = "manual trip"
    eng.reset_fuse()
    check(eng._tripped is None, "reset_fuse() re-arms")

    print()
    print("%d/%d checks passed" % (_checks[1], _checks[0]))
    return 0 if _checks[1] == _checks[0] else 1


if __name__ == "__main__":
    sys.exit(main())
