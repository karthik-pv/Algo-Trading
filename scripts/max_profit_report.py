#!/usr/bin/env python3
"""Max-profit report: for every trade executed on a date, what was the
highest unrealized profit reached between the buy fill and the sell
fill, per the DUAL-BROKER tick capture (data/feed_compare/<date>/)?

Usage:
    ./venv/bin/python scripts/max_profit_report.py [YYYY-MM-DD]

    date defaults to today. Reads the audit file for the trade windows
    and both brokers' tick CSVs (chain + manual + position captures)
    for the peak LTP inside each [buy_exec_ts, sell_exec_ts] window.

Peak profit is reported in points and % of the buy average price -
multiply points by the contract's lot size x lots for rupees.
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.session_report import (  # noqa: E402
    _audit_records, _position_capture_csvs, _load_broker_tick_rows,
    _position_norm_symbol,
)


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now().date().isoformat()

    records = [
        r for r in (_audit_records(date_str) or [])
        if isinstance(r, dict) and r.get("buy_exec_ts")
        and str(r.get("trade_mode") or "LIVE").upper() == "LIVE"
    ]
    if not records:
        print(f"No LIVE trades found for {date_str}")
        return 1

    csvs = _position_capture_csvs(date_str)
    broker_rows = {b: _load_broker_tick_rows(csvs[b]) for b in ("MSTOCK", "KITE")}
    have_rows = {b for b, rows in broker_rows.items() if rows}
    if not have_rows:
        print(f"No tick-capture CSVs found for {date_str} "
              f"(data/feed_compare/{date_str}/) - capture was not running.")
        return 1

    # Last captured tick per symbol across brokers: an OPEN position's
    # analysis window ends there (a stale audit row whose exit happened
    # outside the app's hooks must not swallow the rest of the day's
    # ticks as "profit while holding").
    last_tick_ns = {}
    for rows in broker_rows.values():
        for ns, sym, _ in rows:
            if sym:
                last_tick_ns[sym] = max(last_tick_ns.get(sym, 0), ns)

    print(f"Max-profit report for {date_str} "
          f"(tick sources: {' + '.join(sorted(have_rows))})\n")

    for r in records:
        symbol = _position_norm_symbol(r.get("symbol"))
        buy_exec = float(r["buy_exec_ts"])
        sell_exec = r.get("sell_exec_ts")
        open_pos = not sell_exec
        end_ts = float(sell_exec or datetime.now().timestamp())
        if end_ts < buy_exec:
            continue

        buy_avg = r.get("buy_avg_price") or r.get("ltp_exec")
        try:
            buy_avg = float(buy_avg) if buy_avg is not None else None
        except (TypeError, ValueError):
            buy_avg = None

        # Open positions: cap the peak window at the symbol's last
        # captured tick.
        if open_pos and symbol in last_tick_ns:
            end_ts = min(end_ts, last_tick_ns[symbol] / 1e9)

        # Peak LTP from each broker inside the holding window.
        peaks = {}
        for b in have_rows:
            lo = int(buy_exec * 1e9)
            hi = int(end_ts * 1e9)
            window_ltps = [
                (ns, ltp) for ns, sym, ltp in broker_rows[b]
                if lo <= ns <= hi and ltp is not None
                and sym == symbol
            ]
            if window_ltps:
                peaks[b] = max(window_ltps, key=lambda t: t[1])

        window_txt = (f"{_fmt_ts(buy_exec)} -> "
                      + ("OPEN" if open_pos else _fmt_ts(end_ts)))
        print(f"{symbol}  [{window_txt}]  buy avg {buy_avg}")
        if not peaks:
            print("  no ticks captured in the holding window\n")
            continue

        broker, (peak_ns, peak_ltp) = max(peaks.items(), key=lambda kv: kv[1][1])
        peak_at = datetime.fromtimestamp(peak_ns / 1e9).strftime("%H:%M:%S")
        if buy_avg:
            pts = peak_ltp - buy_avg
            print(f"  peak LTP {peak_ltp:g} at {peak_at} ({broker}) -> "
                  f"max unrealized profit {pts:+.2f} pts "
                  f"({pts / buy_avg * 100:+.2f}%)")
        else:
            print(f"  peak LTP {peak_ltp:g} at {peak_at} ({broker})")
        for b, (ns, ltp) in sorted(peaks.items()):
            print(f"    {b}: peak {ltp:g} at "
                  f"{datetime.fromtimestamp(ns / 1e9).strftime('%H:%M:%S')}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
