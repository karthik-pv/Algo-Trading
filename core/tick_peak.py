"""Holding-window peak finder over the Feed Lab tick capture.

One implementation of "max LTP seen between a trade's buy fill and its
sell fill", shared by the three views that need it - the Audit grid's
peak columns (core/audit_log.py), the session report's position-tick
section (core/session_report.py), and scripts/max_profit_report.py -
so the numbers can never disagree between views.

Data source: every tick CSV the Feed Lab captures for the date
(data/feed_compare/<date>/{mstock,kite}_*.csv - the always-on chain
capture, the position-scoped capture, and any manual Feed Lab run all
write there, and all are merged).

Rows are cached per file stamp, so repeated Audit-grid polls during a
live session pay the CSV read only when new ticks actually landed.
"""

import bisect
import csv
import glob
import os
import threading
from datetime import datetime

from core.utils import DATA_DIR
from core.session_report import _position_norm_symbol

_csv_lock = threading.Lock()
_row_cache = {}   # (paths, stamps) -> [(recv_ns, symbol, ltp)] merged+sorted


def capture_csvs(date_str):
    """All tick-capture CSVs for the date, per broker (chain + manual
    + position files; instrument dumps excluded)."""
    out = {"MSTOCK": [], "KITE": []}
    for broker in out:
        out[broker] = sorted(glob.glob(os.path.join(
            DATA_DIR, "feed_compare", date_str,
            f"{broker.lower()}_*.csv")))
    return out


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_rows(paths):
    """[(recv_ns, symbol, ltp)] merged + sorted across the given CSVs,
    cached until any file's mtime/size changes."""
    paths = list(paths or [])
    stamps = tuple((p, *_stamp(p)) for p in paths)
    with _csv_lock:
        cached = _row_cache.get(stamps)
        if cached is not None:
            return cached
        # Drop stale cache entries so the dict cannot grow unbounded
        # across a day of live flushes.
        if len(_row_cache) > 8:
            for key in [k for k in _row_cache if k != stamps][: len(_row_cache) - 8]:
                _row_cache.pop(key, None)
    rows = []
    for path in paths:
        try:
            with open(path, newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    continue
                cols = [h.strip() for h in header]
                if "recv_ns" not in cols:
                    continue
                ns_i = cols.index("recv_ns")
                sym_i = cols.index("symbol") if "symbol" in cols else None
                ltp_i = cols.index("ltp") if "ltp" in cols else None
                for line in reader:
                    if len(line) <= ns_i:
                        continue
                    try:
                        ns = int(line[ns_i])
                    except (TypeError, ValueError):
                        continue
                    raw_sym = (line[sym_i] or "").upper() if sym_i is not None else ""
                    # Normalize dashed broker formats to the exchange
                    # weekly format so trades match their ticks.
                    sym = _position_norm_symbol(raw_sym) or raw_sym
                    ltp = None
                    if ltp_i is not None and len(line) > ltp_i:
                        try:
                            ltp = float(line[ltp_i])
                        except (TypeError, ValueError):
                            ltp = None
                    rows.append((ns, sym, ltp))
        except OSError:
            continue
    rows.sort()
    with _csv_lock:
        _row_cache[stamps] = rows
    return rows


def window_slice(rows, symbol, start_ts, end_ts):
    """Rows for one symbol inside [start_ts, end_ts] (epoch seconds)."""
    ns_list = [t[0] for t in rows]
    lo = bisect.bisect_left(ns_list, int(start_ts * 1e9))
    hi = bisect.bisect_right(ns_list, int(end_ts * 1e9))
    # Exact symbol match - the capture writes many symbols a day, and
    # counting another contract's ticks toward this trade would
    # fabricate peaks.
    return [t for t in rows[lo:hi] if t[1] == symbol]


def peak_from_rows(rows, symbol, start_ts, end_ts, buy_avg=None):
    """Peak dict for one trade from pre-loaded rows (see
    peak_for_trade). Returns None when no ticks exist in the window."""
    sl = window_slice(rows, symbol, start_ts, end_ts)
    candidates = [(ns, ltp) for ns, _, ltp in sl if ltp is not None]
    if not candidates:
        return None
    peak_ns, peak_ltp = max(candidates, key=lambda t: t[1])
    out = {
        "peak_ltp": peak_ltp,
        "peak_at": datetime.fromtimestamp(peak_ns / 1e9).strftime("%H:%M:%S"),
        "peak_pts": None,
        "peak_pct": None,
    }
    if buy_avg:
        try:
            pts = peak_ltp - float(buy_avg)
            out["peak_pts"] = pts
            out["peak_pct"] = pts / float(buy_avg) * 100
        except (TypeError, ValueError):
            pass
    return out


def last_tick_ts(rows, symbol):
    """Epoch seconds of the symbol's last captured tick (0 when none)."""
    ts = [t[0] for t in rows if t[1] == symbol]
    return (ts[-1] / 1e9) if ts else 0.0


def peak_for_trade(symbol, start_ts, end_ts, buy_avg=None,
                   date_str=None, csvs=None):
    """Peak LTP seen by EITHER broker between a trade's buy and sell
    fills, with profit in points / % of the buy average and the time
    it was reached.

    `csvs` may pass a pre-resolved per-broker path map (from
    capture_csvs); otherwise it is globbed for `date_str` (default
    today). Open trades should pass end_ts = now - the window is
    capped at the symbol's last captured tick, so a stale audit row
    whose exit was never recorded cannot swallow the rest of the
    day's ticks.
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    csvs = csvs or capture_csvs(date_str)
    sym = _position_norm_symbol(symbol) or str(symbol or "").upper()
    per_broker = {b: load_rows(p) for b, p in csvs.items()}

    # Cap at the last captured tick across brokers (open-trade guard).
    last = max(
        (last_tick_ts(rows, sym) for rows in per_broker.values()),
        default=0.0,
    )
    if last and end_ts > last:
        end_ts = last
    if end_ts <= start_ts:
        return None

    best = None
    for rows in per_broker.values():
        cand = peak_from_rows(rows, sym, start_ts, end_ts, buy_avg)
        if cand and (best is None or cand["peak_ltp"] > best["peak_ltp"]):
            best = cand
    return best
