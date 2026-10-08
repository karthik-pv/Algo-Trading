"""
End-of-day technical session report ("Log Report" on the Audit tab).

Reconstructs how the app ran for a trade date from three sources:
  - data/logs/MM_DD/app_*.log   (session log: errors, feed, timings)
  - data/audit/audit_YYYY-MM-DD.json (click -> exec trade audit)
  - tick_stats.json             (per-hour tick counts)

Returns a structured dict the frontend renders section by section:
{"date": ..., "sections": [{"title": str, "lines": [str],
  "table": {"headers": [...], "rows": [[...]]}}, ...]}

Line severity is carried by a leading marker the UI colours:
  "✔" ok/green, "⚠" warn/amber, "✖" error/red, "•" plain,
  "   " (leading spaces) = indented detail line.
"""

import ast
import bisect
import csv
import glob
import json
import os
import re
from collections import Counter
from datetime import datetime, time as dtime, timedelta

from loguru import logger

from core.audit_log_parser import LOG_LINE_RE, parse_date_logs, retag_untagged
from core.utils import DATA_DIR, resolve_data_path, fetch_from_json

LOGS_ROOT = os.path.join(DATA_DIR, "logs")
AUDIT_DIR = os.path.join(DATA_DIR, "audit")

# Health is scoped to the traded instrument's session - pre-open app
# starts and post-close idling must not pollute the error / feed /
# connection stats.
TRADING_WINDOWS = {
    "NIFTY": (dtime(9, 15), dtime(15, 45)),
    "SENSEX": (dtime(9, 15), dtime(15, 45)),
    "CRUDEOIL": (dtime(9, 0), dtime(11, 30)),
    "MCX": (dtime(9, 0), dtime(11, 30)),
}
_DEFAULT_WINDOW = TRADING_WINDOWS["NIFTY"]


def _trading_hours(underlying):
    """(start, end) time-of-day for the instrument's session."""
    return TRADING_WINDOWS.get(underlying, _DEFAULT_WINDOW)


def _detect_underlying(records):
    """Underlying for the date: from the day's traded symbols first
    (a config change mid-week must not mislabel an old date), falling
    back to the current appconfig."""
    text = " ".join(str(r.get("symbol") or "") for r in records).upper()
    if "CRUDE" in text:
        return "CRUDEOIL"
    if "SENSEX" in text:
        return "SENSEX"
    if "NIFTY" in text:
        return "NIFTY"
    cfg = str(fetch_from_json("appconfig.json", "UNDERLYING") or "").upper()
    if "CRUDE" in cfg:
        return "CRUDEOIL"
    if "SENSEX" in cfg:
        return "SENSEX"
    if cfg in TRADING_WINDOWS:
        return cfg
    return "NIFTY"


def _trading_window(records):
    underlying = _detect_underlying(records)
    return underlying, *_trading_hours(underlying)

FEED_RATE_RE = re.compile(
    r"MSTOCK FEED RATE \| (\d+) quote packets in [\d.]+s "
    r"\(([\d.]+)/min\) \| longest quiet gap ([\d.]+)s"
)
FEED_STALL_RE = re.compile(r"MSTOCK FEED STALL \| no quote packet for ([\d.]+)s")
HTTP_TIMING_RE = re.compile(
    r"M\.Stock BUY HTTP TIMING \| request_to_send=([\d.]+)s "
    r"\| response_wait=([\d.]+)s \| total=([\d.]+)s"
)
BUY_CLICK_LINE_RE = re.compile(r"Received buy order from client:")
TEMP_BUY_LINE_RE = re.compile(r"TEMP BUY GRID UPDATE")
SELL_TRIGGER_LINE_RE = re.compile(r"Placing SELL order for")
PENDING_LINE_RE = re.compile(r"Pending orders fetch: (\d+) order\(s\) by status: (\{.*\})")
PENDING_TOTAL_RE = re.compile(r"Total orders: (\d+), Pending: (\d+)")
HEARTBEAT_RE = re.compile(r"SL watcher heartbeat \| monitoring (\d+) position leg")
POLL_GAP_RE = re.compile(r"poll gap was (\d+)s")
SHUTDOWN_LINE = "_shutdown_if_no_browser"
WS_DROP_RE = re.compile(
    r"Connection closed|rejected WebSocket|Network is unreachable.*Retrying|"
    r"WebSocket error"
)

_MSG_TRUNC = 140


def _fmt_ts(dt):
    return dt.strftime("%H:%M:%S") if dt else "-"


def _fmt_dur(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _short(msg):
    msg = " ".join(str(msg).split())
    return msg[:_MSG_TRUNC] + ("…" if len(msg) > _MSG_TRUNC else "")


def _error_category(msg):
    if "Offline orders are not allowed" in msg:
        return "Broker rejected order (offline-not-allowed)"
    if "gaierror" in msg or "Network is unreachable" in msg:
        return "Network / DNS failure"
    if "WebSocket" in msg or "Connection closed" in msg:
        return "WebSocket drop"
    if "fetch_all_pending_orders" in msg or "pending orders" in msg.lower():
        return "Pending-orders fetch failed"
    return "Other"


def _log_files_for(date_str):
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return []
    # Live month folder first, then the weekly backup folder.
    folders = [
        os.path.join(LOGS_ROOT, day.strftime("%m_%d")),
        os.path.join(LOGS_ROOT, "0_backup", day.strftime("%m_%d")),
    ]
    paths = []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        paths.extend(
            os.path.join(folder, f)
            for f in sorted(os.listdir(folder))
            if f.startswith("app_") and f.endswith(".log")
        )
        if paths:
            break
    return paths


def _scan_logs(paths, win_start=None, win_end=None):
    """Single pass over the day's log lines collecting every stat the
    report needs. Lines outside the trading-hours window (win_start /
    win_end time-of-day) are excluded from the health stats; app
    lifecycle events (starts, clean shutdown) stay global."""
    stats = {
        "first_ts": None,
        "last_ts": None,
        "error_lines": [],          # (dt, file, func, msg)
        "warn_samples": [],         # non-feed-stall warnings worth showing
        "feed_rates": [],           # (packets, per_min, gap_s)
        "feed_stalls": [],          # gap_s
        "http_timings": [],         # (side, total_s)
        "ws_drops": 0,
        "poll_gaps": [],            # gap_s (frontend poll throttling)
        "buy_clicks": 0,
        "buys_ok": 0,
        "sell_triggers": 0,
        "last_pending": None,       # (total, pending)
        "last_heartbeat_legs": None,
        "clean_shutdown": False,
        "socket_starts": 0,
        "last_status_counts": {},
    }
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    m = LOG_LINE_RE.match(raw)
                    if not m:
                        continue
                    date_part, millis, level, fname, func, msg = m.groups()
                    try:
                        ts = datetime.strptime(date_part, "%Y-%m-%d %H:%M:%S")
                        ts = ts.replace(microsecond=int(millis) * 1000)
                    except ValueError:
                        continue
                    in_window = not (win_start and win_end) or (
                        win_start <= ts.time() <= win_end
                    )

                    if func == "start_frontend_socket_server":
                        stats["socket_starts"] += 1
                    elif func == SHUTDOWN_LINE:
                        stats["clean_shutdown"] = True

                    if not in_window:
                        continue

                    if stats["first_ts"] is None:
                        stats["first_ts"] = ts
                    stats["last_ts"] = ts

                    if level == "ERROR":
                        stats["error_lines"].append((ts, fname, func, msg))
                        continue
                    if level != "WARNING":
                        if TEMP_BUY_LINE_RE.search(msg):
                            stats["buys_ok"] += 1
                        elif BUY_CLICK_LINE_RE.search(msg):
                            stats["buy_clicks"] += 1
                        elif SELL_TRIGGER_LINE_RE.search(msg):
                            stats["sell_triggers"] += 1
                        elif func == "stop_loss_book_profit_core":
                            hb = HEARTBEAT_RE.search(msg)
                            if hb:
                                stats["last_heartbeat_legs"] = int(hb.group(1))
                        elif func == "fetch_all_pending_orders":
                            pt = PENDING_TOTAL_RE.search(msg)
                            if pt and int(pt.group(2)) == 0:
                                stats["last_pending"] = (
                                    int(pt.group(1)), int(pt.group(2))
                                )
                        continue

                    # WARNING level from here down.
                    fr = FEED_RATE_RE.search(msg)
                    if fr:
                        stats["feed_rates"].append(
                            (int(fr.group(1)), float(fr.group(2)), float(fr.group(3)))
                        )
                        continue
                    fs = FEED_STALL_RE.search(msg)
                    if fs:
                        stats["feed_stalls"].append(float(fs.group(1)))
                        continue
                    if "poll gap" in msg:
                        pg = POLL_GAP_RE.search(msg)
                        if pg:
                            stats["poll_gaps"].append(int(pg.group(1)))
                        continue
                    if WS_DROP_RE.search(msg):
                        stats["ws_drops"] += 1
                        continue
                    pd = PENDING_LINE_RE.search(msg)
                    if pd:
                        try:
                            counts = ast.literal_eval(pd.group(2))
                            stats["last_status_counts"] = counts
                        except (ValueError, SyntaxError):
                            pass
                        continue
                    if len(stats["warn_samples"]) < 8:
                        stats["warn_samples"].append((ts, func, msg))
        except OSError:
            continue
    return stats


def _audit_records(date_str):
    """Audit records for the date: the persisted audit file first (has
    click-time capture), falling back to log reconstruction. Untagged
    records (persisted before mode tagging existed) are retagged from
    their log-reconstructed twins so PAPER / LIVE never mix."""
    path = os.path.join(AUDIT_DIR, f"audit_{date_str}.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                records = json.load(fh)
            if isinstance(records, list):
                records = [r for r in records if isinstance(r, dict)]
                try:
                    retag_untagged(records, parse_date_logs(date_str))
                except Exception as e:
                    logger.error(f"Session report: mode retag failed: {e}")
                return records
        except (OSError, ValueError):
            pass
    return parse_date_logs(date_str)


def _trade_section(records):
    """Aggregate trade-flow stats ONLY - the per-trade rows already
    live in the Audit tab's main grid, so no table is emitted here."""
    latencies = []
    manual_sell_lat = []
    sources = Counter()
    strategies = Counter()
    modes = Counter()
    buys_ok = buys_failed = imported = 0

    for r in records:
        if r.get("imported"):
            imported += 1
            continue
        strat = str(r.get("strategy") or "-") or "-"
        mode = str(r.get("sell_mode") or "-") or "-"
        strategies[strat] += 1
        if mode != "-":
            modes[mode] += 1
        click = r.get("buy_click_ts") or r.get("first_attempt_ts")
        exec_ts = r.get("buy_exec_ts")
        ok = bool(r.get("buy_success") and exec_ts)
        if ok:
            buys_ok += 1
        elif click:
            buys_failed += 1
        lat = (exec_ts - click) if (ok and click) else None
        if lat is not None and lat >= 0:
            latencies.append(lat)

        src = r.get("sell_trigger_source")
        st = r.get("sell_trigger_ts")
        se = r.get("sell_exec_ts")
        if src:
            sources[src] += 1
            if src == "MANUAL" and st and se and se >= st:
                manual_sell_lat.append(se - st)

    lines = [
        f"• Buys: {buys_ok} executed, {buys_failed} failed "
        f"({buys_ok + buys_failed} clicks)"
        + (f", {imported} broker-imported rows" if imported else ""),
    ]
    if latencies:
        lines.append(
            f"• Click → buy-executed latency: avg {sum(latencies)/len(latencies):.2f}s"
            f", min {min(latencies):.2f}s, max {max(latencies):.2f}s (n={len(latencies)})"
        )
        slow = [l for l in latencies if l > 2]
        if slow:
            lines.append(f"⚠ {len(slow)} buy(s) took >2s to execute")
        else:
            lines.append("✔ All executed buys under 2s click-to-exec")
    retried = sum(1 for r in records if not r.get("imported") and r.get("retry_count"))
    lines.append(
        f"• Retries: {retried} leg(s) needed lot-reduction retries"
        if retried else "✔ No retries needed (first attempt filled every time)"
    )
    if sources:
        src_txt = ", ".join(f"{k} {v}" for k, v in sources.most_common())
        lines.append(f"• Sell triggers: {src_txt}")
        if manual_sell_lat:
            lines.append(
                f"• Manual sell trigger → executed: avg "
                f"{sum(manual_sell_lat)/len(manual_sell_lat):.2f}s"
            )
    if strategies:
        lines.append(
            f"• Strategies: "
            + ", ".join(f"{k} {v}" for k, v in strategies.most_common())
            + (" | Sell modes: " + ", ".join(f"{k} {v}" for k, v in modes.most_common())
               if modes else "")
        )
    if buys_failed:
        lines.append(
            f"✖ {buys_failed} buy click(s) never reached the broker — see Errors section"
        )
    return {"title": "Trade flow", "lines": lines}


def _error_section(stats):
    lines = []
    n = len(stats["error_lines"])
    if not n:
        lines.append("✔ Zero ERROR lines in the session log")
        return {"title": "Errors", "lines": lines}

    cats = Counter(_error_category(msg) for _, _, _, msg in stats["error_lines"])
    lines.append(
        f"✖ {n} ERROR line(s) across {len(cats)} "
        f"categor{'y' if len(cats) == 1 else 'ies'}"
    )
    for cat, cnt in cats.most_common():
        lines.append(f"  ✖ {cat}: {cnt} line(s)")
    by_cat_time = {}
    for ts, fname, func, msg in stats["error_lines"]:
        by_cat_time.setdefault(_error_category(msg), []).append((ts, func, msg))
    for cat, items in by_cat_time.items():
        first, last = items[0][0], items[-1][0]
        sample = next((m for _, _, m in reversed(items) if m.strip()), "")
        lines.append(
            f"     {cat}: {first.strftime('%H:%M:%S')}–{last.strftime('%H:%M:%S')}"
            f" — sample: {_short(sample)}"
        )
    return {"title": "Errors", "lines": lines}


def _feed_section(stats, date_str, win=None):
    lines = []
    rates = [r[1] for r in stats["feed_rates"]]
    gaps = [r[2] for r in stats["feed_rates"]] + stats["feed_stalls"]
    if rates:
        lines.append(
            f"• Feed rate: avg {sum(rates)/len(rates):.0f} packets/min"
            f", min {min(rates):.0f}, max {max(rates):.0f} "
            f"({len(rates)} samples)"
        )
    if stats["feed_stalls"]:
        lines.append(
            f"⚠ {len(stats['feed_stalls'])} feed stall warning(s)"
            + (f", worst quiet gap {max(gaps):.1f}s" if gaps else "")
        )
    elif gaps:
        lines.append(f"✔ Worst quiet gap {max(gaps):.1f}s — no stall warnings")
    else:
        lines.append("• No feed-rate telemetry in this session's log")

    tick_rows = []
    stats_path = resolve_data_path("tick_stats.json")
    if os.path.isfile(stats_path):
        try:
            with open(stats_path, "r", encoding="utf-8", errors="replace") as fh:
                tick_stats = json.load(fh)
            day = (tick_stats.get(date_str) or {})
            # Broker -> token -> {hour: count}
            per_hour = {}
            for broker_tokens in day.values():
                for hourly in broker_tokens.values():
                    for hour, cnt in hourly.items():
                        per_hour.setdefault(hour, []).append(int(cnt))
            for hour in sorted(per_hour):
                # Keep only hours that intersect the trading window
                # (the pre-open 08:00 bucket is not session health).
                try:
                    h = int(hour)
                    h_start = dtime(h, 0)
                    h_end = dtime(23, 59) if h == 23 else dtime(h + 1, 0)
                except ValueError:
                    continue
                if win and (h_end <= win[0] or h_start >= win[1]):
                    continue
                counts = per_hour[hour]
                tick_rows.append([
                    f"{hour}:00", str(sum(counts)), str(len(counts)),
                    f"{min(counts)}–{max(counts)}",
                ])
        except (OSError, ValueError):
            pass
    if tick_rows:
        lines.append("• Ticks per hour (all watched tokens):")
    table = {
        "headers": ["Hour", "Total ticks", "Tokens", "Per-token range"],
        "rows": tick_rows,
    } if tick_rows else None
    return {"title": "Tick feed & speed", "lines": lines, "table": table}


def _session_section(stats, paths, underlying, win):
    lines = []
    lines.append(
        f"• Health scoped to {underlying} trading hours "
        f"{win[0].strftime('%H:%M')}–{win[1].strftime('%H:%M')} "
        "(pre-open / post-close log lines excluded)"
    )
    if stats["first_ts"] and stats["last_ts"]:
        dur = (stats["last_ts"] - stats["first_ts"]).total_seconds()
        lines.append(
            f"• In-session {stats['first_ts'].strftime('%H:%M:%S')} → "
            f"{stats['last_ts'].strftime('%H:%M:%S')} ({_fmt_dur(dur)})"
        )
    else:
        lines.append("⚠ No in-trading-hours log lines found")
    restarts = len(paths)
    if restarts > 1:
        lines.append(f"⚠ {restarts} app start(s) this day (log files)")
    if stats["clean_shutdown"]:
        lines.append("✔ Clean shutdown (window closed → app exited)")
    else:
        lines.append("⚠ No clean-shutdown marker — app may have been killed")
    if stats["last_heartbeat_legs"] is not None:
        if stats["last_heartbeat_legs"] == 0:
            lines.append("✔ SL watcher flat at last heartbeat (0 open legs)")
        else:
            lines.append(
                f"⚠ SL watcher last saw {stats['last_heartbeat_legs']} open leg(s)"
            )
    if stats["last_pending"]:
        total, pending = stats["last_pending"]
        if pending == 0:
            lines.append(
                f"✔ Order book settled: {total} order(s) for the day, 0 pending"
            )
            if stats["last_status_counts"]:
                counts = ", ".join(
                    f"{k} {v}" for k, v in stats["last_status_counts"].items()
                )
                lines.append(f"     statuses: {counts}")
        else:
            lines.append(f"⚠ {pending} order(s) still pending at last poll")
    return {"title": "Session overview", "lines": lines}


def _health_section(stats):
    lines = []
    if stats["ws_drops"]:
        lines.append(
            f"⚠ {stats['ws_drops']} WebSocket drop/retry event(s) — "
            "check Errors for windows"
        )
    else:
        lines.append("✔ WebSocket stayed connected all session")
    if stats["poll_gaps"]:
        lines.append(
            f"• {len(stats['poll_gaps'])} frontend poll-gap warning(s) "
            f"(browser throttling when window minimised — benign)"
        )
    if stats["warn_samples"]:
        lines.append(f"• Other warnings ({len(stats['warn_samples'])} shown):")
        for ts, func, msg in stats["warn_samples"]:
            lines.append(f"     {ts.strftime('%H:%M:%S')} [{func}] {_short(msg)}")
    return {"title": "Connection & health", "lines": lines}


def _broker_comparison_section(date_str):
    """
    Daily KITE vs MSTOCK feed verdict for the Audit tab, built from the
    Feed Lab's capture files (data/feed_compare/<date>/). Present only
    on days both feeds were captured - skipped otherwise so it never
    duplicates the single-broker sampler stats in 'Tick feed & speed'.

    Answers, with numbers: which feed delivered more, which reported
    price changes FIRST (the latency a trade decision feels), and how
    closely the two agree.
    """
    base = os.path.join(DATA_DIR, "feed_compare", date_str)
    if not os.path.isdir(base):
        return None

    import pandas as pd

    feeds = {}
    for broker in ("kite", "mstock"):
        paths = [
            p for p in sorted(glob.glob(os.path.join(base, f"{broker}_*.csv")))
            if "instruments" not in os.path.basename(p)
        ]
        if not paths:
            continue
        try:
            # Concatenate EVERY capture file of the day (chain, manual,
            # position) - the per-symbol stats below need full coverage.
            df = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
        except Exception as e:
            logger.warning(f"Broker verdict: could not read {broker} CSVs: {e}")
            continue
        if "recv_iso" not in df.columns or "ltp" not in df.columns:
            continue
        df["ts"] = pd.to_datetime(df["recv_iso"], errors="coerce")
        df = df.dropna(subset=["ts", "ltp"]).sort_values("ts")
        if len(df) < 2:
            continue
        feeds[broker.upper()] = df

    if not feeds:
        return None

    lines = []
    table_rows = []

    for broker in ("KITE", "MSTOCK"):
        df = feeds.get(broker)
        if df is None:
            table_rows.append([broker, "not captured", "-", "-", "-", "-"])
            continue
        span_min = (df["ts"].max() - df["ts"].min()).total_seconds() / 60
        rate = len(df) / span_min if span_min > 0 else 0
        gaps = df["ts"].diff().dt.total_seconds().dropna()
        table_rows.append([
            broker,
            f"{len(df):,}",
            f"{rate:.0f}/min" if span_min > 0 else "-",
            f"{gaps.quantile(0.5):.2f}s" if len(gaps) else "-",
            f"{gaps.quantile(0.95):.2f}s" if len(gaps) else "-",
            f"{gaps.max():.0f}s" if len(gaps) else "-",
        ])

    table = {
        "headers": ["Feed", "Ticks", "Rate", "Gap p50", "Gap p95", "Max gap"],
        "rows": table_rows,
    }

    verdict_lines = []

    # --- LTP agreement (per shared symbol, 2s pairing) ---------------
    agreements = []
    if "KITE" in feeds and "MSTOCK" in feeds:
        for sym in set(feeds["KITE"]["symbol"].unique()) & set(feeds["MSTOCK"]["symbol"].unique()):
            kf = feeds["KITE"][feeds["KITE"]["symbol"] == sym][["ts", "ltp"]]
            mf = feeds["MSTOCK"][feeds["MSTOCK"]["symbol"] == sym][["ts", "ltp"]]
            mf_t = mf["ts"].tolist()
            mf_p = mf["ltp"].tolist()
            matched = agree = 0
            for ts, px in zip(kf["ts"], kf["ltp"]):
                j = bisect.bisect_left(mf_t, ts - timedelta(seconds=2))
                while j < len(mf_t) and mf_t[j] <= ts + timedelta(seconds=2):
                    if mf_p[j] == px:
                        matched += 1
                        agree += 1
                        break
                    j += 1
                else:
                    continue
            if matched:
                agreements.append(agree / matched * 100)

    # --- Feed latency: cross-correlation of the price series --------
    # Unbiased method: resample both series to a 100ms grid (ffill),
    # roll MSTOCK by offsets in +-1s and find the offset that
    # maximises agreement with KITE. Sign convention (verified with a
    # synthetic jump): a POSITIVE best offset means MSTOCK's OLDER
    # data matches KITE's current data - i.e. MSTOCK printed the
    # price first - so MSTOCK leads. Event-matching ("same price seen
    # earlier") is biased by dense feeds' quote churn and is not used.
    if "KITE" in feeds and "MSTOCK" in feeds:
        import numpy as np
        offsets = []
        shared = sorted(
            set(feeds["KITE"]["symbol"].unique())
            & set(feeds["MSTOCK"]["symbol"].unique())
        )
        for sym in shared:
            kf = feeds["KITE"][feeds["KITE"]["symbol"] == sym].set_index("ts")["ltp"]
            mf = feeds["MSTOCK"][feeds["MSTOCK"]["symbol"] == sym].set_index("ts")["ltp"]
            start = max(kf.index.min(), mf.index.min()).round("100ms")
            end = min(kf.index.max(), mf.index.max()).round("100ms")
            grid = pd.date_range(start, end, freq="100ms")
            if len(grid) < 50:
                continue
            ka = kf.reindex(grid, method="ffill").to_numpy()
            ma = mf.reindex(grid, method="ffill").to_numpy()
            best_off, best_agree = None, -1.0
            for off in range(-10, 11):        # +-1.0s in 100ms steps
                ms = np.roll(ma, off)
                v = slice(abs(off), len(grid) - abs(off)) if off else slice(None)
                agree = float(np.mean(np.abs(ka[v] - ms[v]) <= 0.05))
                if agree > best_agree:
                    best_agree, best_off = agree, off
            if best_off is not None:
                offsets.append((best_off * 0.1, best_agree))

        if offsets:
            offsets.sort()
            median_off = offsets[len(offsets) // 2][0]
            lines.append(
                f"• Feed latency (cross-correlation of LTP series, "
                f"100ms grid, {len(offsets)} instrument(s)):"
            )
            for off, agree in offsets:
                if off > 0.05:
                    lines.append(
                        f"   MSTOCK leads by ~{off:.1f}s "
                        f"(agreement {agree * 100:.1f}%)"
                    )
                elif off < -0.05:
                    lines.append(
                        f"   KITE leads by ~{-off:.1f}s "
                        f"(agreement {agree * 100:.1f}%)"
                    )
                else:
                    lines.append(
                        f"   effectively simultaneous ({agree * 100:.1f}%)"
                    )
            med = median_off
            if med > 0.05:
                verdict_lines.append(
                    f"✔ MSTOCK is the lower-latency feed "
                    f"(price info leads KITE by ~{med:.1f}s)"
                )
            elif med < -0.05:
                verdict_lines.append(
                    f"✔ KITE is the lower-latency feed "
                    f"(price info leads MSTOCK by ~{-med:.1f}s)"
                )
            else:
                verdict_lines.append("• Feed latencies are effectively equal")
        else:
            lines.append("• Not enough aligned data for latency comparison")
    else:
        lines.append(
            "• Only one feed captured - latency comparison needs both "
            "brokers' Feed Lab captures"
        )

    if agreements:
        mean_agree = sum(agreements) / len(agreements)
        lines.append(
            f"• LTP agreement across feeds: {mean_agree:.1f}% "
            f"({len(agreements)} instrument(s), 2s pairing)"
        )
        verdict_lines.append(
            f"✔ Feeds agree on price ({mean_agree:.1f}% of paired LTPs identical)"
        )

    k, m_ = feeds.get("KITE"), feeds.get("MSTOCK")
    if k is not None and m_ is not None:
        ratio = len(m_) / max(len(k), 1)
        if ratio > 1.15:
            verdict_lines.append(
                f"• MSTOCK streamed {ratio:.1f}x the tick volume "
                f"(denser quote updates)"
            )
        elif ratio < 0.85:
            verdict_lines.append(
                f"• KITE streamed {1 / ratio:.1f}x the tick volume this session"
            )

    lines.extend(verdict_lines)
    return {"title": "Broker verdict (KITE vs MSTOCK)", "lines": lines, "table": table}


def _verdict_section(stats, records):
    lines = []
    issues = []
    if any("Offline orders" in m for _, _, _, m in stats["error_lines"]):
        issues.append("broker offline-order rejections hit some buys")
    if stats["ws_drops"]:
        issues.append("WebSocket drops (auto-recovered)")
    if stats["feed_stalls"]:
        issues.append(f"{len(stats['feed_stalls'])} feed stalls")
    failed = sum(
        1 for r in records
        if not r.get("imported") and (r.get("buy_click_ts") or r.get("first_attempt_ts"))
        and not r.get("buy_success")
    )
    if failed:
        issues.append(f"{failed} buy click(s) failed")
    flat = stats["last_heartbeat_legs"] == 0
    if issues:
        if flat and not failed:
            lines.append("✔ Flat at EOD, positions all closed")
        lines.append("⚠ Watch items: " + "; ".join(issues))
    elif flat:
        lines.append("✔ Clean session: all orders filled, flat at EOD, no errors")
    else:
        lines.append("✔ No errors; check open legs above")
    return {"title": "Verdict", "lines": lines}


def _summary(stats, records, paths):
    """One-line health strip for the Audit tab: the numbers a trader
    glances at, in order. ok=False turns the strip amber."""
    parts = []
    if stats["first_ts"] and stats["last_ts"]:
        dur = (stats["last_ts"] - stats["first_ts"]).total_seconds()
        parts.append(
            f"Session {stats['first_ts'].strftime('%H:%M:%S')}–"
            f"{stats['last_ts'].strftime('%H:%M:%S')} ({_fmt_dur(dur)})"
        )
    if len(paths) > 1:
        parts.append(f"{len(paths)} app starts")

    clicks = failed = 0
    latencies = []
    for r in records:
        if r.get("imported"):
            continue
        click = r.get("buy_click_ts") or r.get("first_attempt_ts")
        if r.get("buy_success") and r.get("buy_exec_ts") and click:
            clicks += 1
            lat = r["buy_exec_ts"] - click
            if lat >= 0:
                latencies.append(lat)
        elif click:
            failed += 1
    trade_txt = f"{clicks} buy(s) filled"
    if failed:
        trade_txt += f", {failed} failed"
    parts.append(trade_txt)
    if latencies:
        parts.append(f"avg latency {sum(latencies)/len(latencies):.2f}s")

    n_err = len(stats["error_lines"])
    if n_err:
        cats = Counter(_error_category(m) for _, _, _, m in stats["error_lines"])
        top = ", ".join(f"{c} x{n}" for c, n in cats.most_common(2))
        parts.append(f"{n_err} errors ({top})")
    else:
        parts.append("0 errors")
    if stats["feed_stalls"]:
        parts.append(f"{len(stats['feed_stalls'])} feed stalls")
    if stats["ws_drops"]:
        parts.append(f"{stats['ws_drops']} ws drops")
    if stats["last_heartbeat_legs"] == 0:
        parts.append("flat at EOD")
    return {"ok": n_err == 0 and failed == 0, "parts": parts}


_REPORT_CACHE = {}


def _position_norm_symbol(symbol):
    """Same normalization the capture supervisor applies to leg symbols
    (broker dashed / Excel compact -> exchange weekly format)."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    m = re.match(r"^([A-Z]+)(\d{2})([A-Z]{3})(\d{4})(\d+)(CE|PE)$", sym)
    if not m:
        m = re.match(r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{4})-(\d+)-(CE|PE)$", sym)
    if not m:
        return sym
    u, dd, mon, yyyy, strike, typ = m.groups()
    month_code = {"JAN": "1", "FEB": "2", "MAR": "3", "APR": "4", "MAY": "5",
                  "JUN": "6", "JUL": "7", "AUG": "8", "SEP": "9", "OCT": "O",
                  "NOV": "N", "DEC": "D"}.get(mon)
    if not month_code:
        return sym
    return f"{u}{yyyy[-2:]}{month_code}{dd.zfill(2)}{strike}{typ}"


def _position_capture_csvs(date_str):
    """All position/Feed-Lab tick CSVs for the date, per broker."""
    out = {"MSTOCK": [], "KITE": []}
    for broker in out:
        out[broker] = sorted(glob.glob(os.path.join(
            DATA_DIR, "feed_compare", date_str,
            f"{broker.lower()}_*.csv")))
    return out


def _load_broker_tick_rows(paths):
    """[(recv_ns, symbol, ltp)] merged + sorted from a broker's CSVs.

    Delegates to core.tick_peak's loader - the ONE implementation
    shared with the Audit grid's peak columns (stamp-cached, so
    repeated polls during live flushes re-read only changed files)."""
    from core.tick_peak import load_rows
    return load_rows(paths)


def _position_tick_section(records, date_str):
    """Per-position tick counts AND holding-window peak from BOTH
    brokers while holding.

    Reads rows in the day's capture CSVs
    (data/feed_compare/YYYY-MM-DD/{mstock,kite}_*.csv - the always-on
    chain capture, the position-scoped capture, and any manual Feed
    Lab run all write here) whose recv_ns timestamp falls inside the
    trade's [buy_exec_ts, sell_exec_ts] window and whose symbol matches
    the trade's (normalized) symbol. Ticks/min = ticks / holding
    seconds. Peak = max LTP seen from either broker inside the window
    -> unrealized profit at that instant in points and % of the buy
    average price, plus the wall-clock time it was seen.
    """
    now_ts = datetime.now().timestamp()
    trades = []
    for r in records:
        buy_exec = r.get("buy_exec_ts")
        if not buy_exec:
            continue
        symbol = _position_norm_symbol(r.get("symbol"))
        if not symbol:
            continue
        sell_exec = r.get("sell_exec_ts")
        open_pos = not sell_exec
        end_ts = float(sell_exec or now_ts)
        if end_ts < float(buy_exec):
            continue
        trades.append((symbol, float(buy_exec), end_ts, open_pos, r))

    if not trades:
        return None

    broker_rows = {b: _load_broker_tick_rows(_position_capture_csvs(date_str)[b])
                   for b in ("MSTOCK", "KITE")}
    broker_ns = {b: [t[0] for t in rows] for b, rows in broker_rows.items()}

    # Last captured tick per symbol across both brokers: an OPEN
    # position's analysis window ends there (a stale audit row whose
    # exit happened outside the app's hooks must not swallow the rest
    # of the day's ticks as "profit while holding").
    last_tick_ns = {}
    for rows in broker_rows.values():
        for ns, sym, _ in rows:
            if sym:
                last_tick_ns[sym] = max(last_tick_ns.get(sym, 0), ns)

    def _window_slice(broker, symbol, start, end):
        rows = broker_rows[broker]
        ns_list = broker_ns[broker]
        lo = bisect.bisect_left(ns_list, int(start * 1e9))
        hi = bisect.bisect_right(ns_list, int(end * 1e9))
        # Exact symbol match - with chain + position captures writing
        # many symbols a day, empty-symbol rows no longer exist and
        # counting them toward every trade would inflate the numbers.
        return [t for t in rows[lo:hi] if t[1] == symbol]

    rows_out = []
    for symbol, start, end, open_pos, rec in trades:
        dur = max(end - start, 0.001)
        # Peak/tick analysis window: for OPEN positions cap at the
        # symbol's last captured tick (display duration stays real).
        peak_end = end
        if open_pos and symbol in last_tick_ns:
            peak_end = min(end, last_tick_ns[symbol] / 1e9)
        m_rows = _window_slice("MSTOCK", symbol, start, peak_end)
        k_rows = _window_slice("KITE", symbol, start, peak_end)
        m_ticks, k_ticks = len(m_rows), len(k_rows)

        # Holding-window peak from both feeds (same NSE/MFO/BFO prints;
        # whichever broker's socket delivered the extreme first wins)
        # via the shared peak helper - the Audit grid's peak columns
        # use the same implementation.
        from core.tick_peak import peak_from_rows
        peak = peak_from_rows(
            m_rows + k_rows, symbol, start, peak_end,
            buy_avg=(rec.get("buy_avg_price") or rec.get("ltp_exec")),
        )
        if peak:
            peak_txt = f"{peak['peak_ltp']:g}"
            pts_txt = (f"{peak['peak_pts']:+.2f}"
                       if peak["peak_pts"] is not None else "-")
            pct_txt = (f"{peak['peak_pct']:+.2f}%"
                       if peak["peak_pct"] is not None else "-")
            peak_at = peak["peak_at"]
        else:
            peak_txt = pts_txt = pct_txt = peak_at = "-"

        rows_out.append([
            symbol,
            f"{datetime.fromtimestamp(start).strftime('%H:%M:%S')} -> "
            + ("OPEN" if open_pos else datetime.fromtimestamp(end).strftime('%H:%M:%S')),
            f"{dur:.0f}s",
            str(m_ticks) if broker_rows["MSTOCK"] else "-",
            f"{m_ticks / dur * 60:.0f}" if m_ticks else "-",
            str(k_ticks) if broker_rows["KITE"] else "-",
            f"{k_ticks / dur * 60:.0f}" if k_ticks else "-",
            peak_txt, pts_txt, pct_txt, peak_at,
        ])

    lines = []
    if not broker_rows["MSTOCK"] and not broker_rows["KITE"]:
        lines.append(
            "⚠ No tick-capture CSVs found for this date - the chain/position "
            "capture writes only while the app runs (restart the app once "
            "after this feature lands)")
    if not broker_rows["KITE"]:
        lines.append(
            "⚠ KITE ticks not captured - no Kite session/token today; "
            "MSTOCK counts are complete")

    return {
        "title": "Position tick capture + holding-window peak (MSTOCK vs KITE)",
        "lines": lines,
        "table": {
            "headers": ["Symbol", "Holding window", "Duration",
                        "MSTOCK ticks", "MSTOCK /min",
                        "KITE ticks", "KITE /min",
                        "Peak LTP", "Peak pts", "Peak %", "Peak at"],
            "rows": rows_out,
        },
    }


def build_session_report(date_str, mode="LIVE"):
    """Build the Log Report dict for YYYY-MM-DD. Cached per (date,
    file stamps, mode) - the Audit tab refetches on every grid reload,
    and rescanning a multi-MB session log each time would be wasteful.
    `mode` buckets the trade records (LIVE / PAPER) so the session
    report never mixes the two."""
    paths = _log_files_for(date_str)
    if not paths:
        return {
            "date": date_str,
            "summary": {
                "ok": False,
                "parts": [f"No session logs found for {date_str}"],
            },
            "sections": [],
        }
    stamps = []
    for p in paths + [resolve_data_path("tick_stats.json")]:
        try:
            st = os.stat(p)
            stamps.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    # Position tick-capture CSVs change while positions are open - a
    # live report must pick up new ticks, so they partake in the
    # cache key too.
    for p in sum(_position_capture_csvs(date_str).values(), []):
        try:
            st = os.stat(p)
            stamps.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    key = (date_str, mode, tuple(stamps))
    cached = _REPORT_CACHE.get(key)
    if cached is not None:
        return cached

    records = _audit_records(date_str)
    # PAPER / LIVE bucket filter - untagged rows (older files) count as
    # LIVE, matching the grid's convention.
    want_mode = str(mode or "LIVE").strip().upper()
    if want_mode not in ("LIVE", "PAPER"):
        want_mode = "LIVE"
    records = [
        r for r in records
        if str(r.get("trade_mode") or "LIVE").strip().upper() == want_mode
    ]
    underlying, win_start, win_end = _trading_window(records)
    win = (win_start, win_end)
    stats = _scan_logs(paths, win_start, win_end)

    sections = [
        _session_section(stats, paths, underlying, win),
        _trade_section(records),
        _position_tick_section(records, date_str),
        _error_section(stats),
        _feed_section(stats, date_str, win),
        _broker_comparison_section(date_str),
        _health_section(stats),
        _verdict_section(stats, records),
    ]
    sections = [s for s in sections if s]
    report = {
        "date": date_str,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": _summary(stats, records, paths),
        "sections": sections,
    }
    _REPORT_CACHE[key] = report
    # Keep the cache bounded: a day's live log grows -> new key each
    # poll, old keys become garbage.
    while len(_REPORT_CACHE) > 8:
        _REPORT_CACHE.pop(next(iter(_REPORT_CACHE)))
    return report


# ------------------------------------------------------------------
# ERRORS & WARNINGS GRID
#
# Every ERROR / WARNING line from the date's session logs, in
# chronological order, with a running index - the Audit tab's dedicated
# error grid. Separate from the session report's Errors section (which
# only summarizes categories): this returns each individual line so a
# failed order or a feed drop can be read in place, with its time.
# ------------------------------------------------------------------

_ERROR_GRID_CACHE = {}
_ERROR_GRID_LIMIT = 5000     # rows served per request (oldest first)

# Market-hours window for the grid (the NIFTY/SENSEX equity session,
# matching feed_capture.MARKET_WINDOWS - deliberately NOT the CRUDEOIL
# analysis window, so MCX evening prints are bucketed as non-market
# hours, not lost). Rows outside it are hidden unless the Audit tab's
# "non-market hours" checkbox is ticked.
_ERROR_GRID_MH = ((9, 15), (15, 45))

# Human-friendly "what happened" labels for the grid's Type column and
# its occurrence summary. Ordered most-specific first; first match wins.
# Patterns are drawn from the day's real log content (RMS rejections,
# session-expired polls, feed stalls, ...).
_FRIENDLY_PATTERNS = [
    ("Offline orders are not allowed", "Broker rejected order (offline-not-allowed)"),
    ("RMS", "Order rejected by broker (RMS margin/rules)"),
    ("Insufficient funds", "Insufficient funds - lots reduced on retry"),
    ("insufficient funds even for 1 lot", "Pre-margin: can't afford even 1 lot"),
    ("Invalid App Code", "Kite 2FA code rejected (lock risk)"),
    ("Automated Kite login failed", "Kite auto-login failed"),
    ("Pre-margin check: right-sizing", "Pre-margin right-sizing of lots"),
    ("Resting exit", "Resting exit CANCELLED - position unprotected"),
    ("Zero/invalid cash balance", "Cash balance unavailable - fallback lots"),
    ("positions_from_broker is None or empty", "Broker positions list empty (transient)"),
    ("Frontend pending-orders poll gap", "Browser tab throttled - poll gap"),
    ("MSTOCK FEED STALL", "M.Stock feed stall - grid prices frozen"),
    ("Feed Lab: Kite websocket closed", "Feed Lab: Kite capture socket closed"),
    ("Closed-market sanitise", "Closed-market quotes normalised to close"),
    ("Fund summary API error", "Fund summary failed (invalid session)"),
    ("Error fetching pending orders", "Pending-orders fetch failed"),
    ("fetch_all_pending_orders", "Pending-orders fetch failed"),
    ("pending orders", "Pending-orders fetch failed"),
    ("server rejected WebSocket connection", "M.Stock feed socket rejected (HTTP 403)"),
    ("Connection closed: no close frame", "Socket closed uncleanly - reconnecting"),
    ("WebSocket", "WebSocket drop/retry"),
    ("M.Stock API GET /openapi/typeb/orders attempt", "M.Stock orders poll retry failing"),
    ("M.Stock API GET /openapi/typeb/orders failed", "M.Stock orders poll failed (session?)"),
    ("instruments/quote attempt", "M.Stock quote API retry failing"),
    ("instruments/quote failed", "M.Stock quote API failed"),
    ("batch quote failed", "M.Stock batch quote failed"),
    ("no valid response from M.Stock", "M.Stock API returned no valid response"),
    ("gaierror", "Network / DNS failure"),
    ("Network is unreachable", "Network / DNS failure"),
]


def _friendly_what(msg):
    """Short human-readable label for one log line's message (first
    pattern match wins; 'Other' when nothing matches)."""
    for pat, label in _FRIENDLY_PATTERNS:
        if pat in msg:
            return label
    return "Other"


def build_error_grid(date_str, limit=_ERROR_GRID_LIMIT):
    """{'ok', 'rows': [{i, time, level, source, message, mh}],
    'errors', 'warnings' (market-hours counts), 'nmh_errors',
    'nmh_warnings', 'total', 'truncated'} for the date's logs.

    Every row carries `mh` (in market hours); the grid defaults to
    market-hours-only and the checkbox flips to the rest."""
    paths = _log_files_for(date_str)
    if not paths:
        return {"ok": False, "rows": [], "errors": 0, "warnings": 0,
                "nmh_errors": 0, "nmh_warnings": 0,
                "total": 0, "truncated": False,
                "error": f"No session logs found for {date_str}"}

    stamps = []
    for p in paths:
        try:
            st = os.stat(p)
            stamps.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    key = (date_str, tuple(stamps))
    cached = _ERROR_GRID_CACHE.get(key)
    if cached is not None:
        return cached

    mh_start = _ERROR_GRID_MH[0][0] * 60 + _ERROR_GRID_MH[0][1]
    mh_end = _ERROR_GRID_MH[1][0] * 60 + _ERROR_GRID_MH[1][1]

    entries = []
    lines_seen = 0
    merged = 0
    # One broker RMS rejection is logged as 2-4 ERROR lines (API error,
    # order error, parse error, handler error) that all cite the same
    # RMS:<orderid>. Collapse them into ONE grid row - the event count,
    # not the log-line count, is what the summary should show. The row
    # keeps the first line's time and the LONGEST message (the handler
    # line carries the full AVAILABLE/REQUIRED FUND detail).
    seen_rms = {}
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    m = LOG_LINE_RE.match(raw)
                    if not m:
                        continue
                    date_part, millis, level, fname, func, msg = m.groups()
                    lvl = str(level or "").strip().upper()
                    if lvl not in ("ERROR", "WARNING"):
                        continue
                    try:
                        ts = datetime.strptime(
                            date_part, "%Y-%m-%d %H:%M:%S")
                        ts = ts.replace(microsecond=int(millis) * 1000)
                    except ValueError:
                        continue
                    minute_of_day = ts.hour * 60 + ts.minute
                    mh = mh_start <= minute_of_day <= mh_end
                    lines_seen += 1
                    text = str(msg or "").strip()
                    rid = re.search(r"RMS:(\d+):", text)
                    if rid and rid.group(1) in seen_rms:
                        # Same rejection, another line - merge into the
                        # canonical row instead of adding one.
                        canonical = entries[seen_rms[rid.group(1)]]
                        if len(text) > len(canonical[4]):
                            canonical[4] = text
                        merged += 1
                        continue
                    if rid:
                        seen_rms[rid.group(1)] = len(entries)
                    entries.append([ts, lvl, str(fname or ""), str(func or ""),
                                    text, mh])
        except OSError as read_error:
            logger.warning(f"Error grid: could not read {path}: {read_error}")

    # Multiple log files per day (one per app run) - chronological
    # across restarts; Python's sort is stable so same-timestamp lines
    # keep file order.
    entries.sort(key=lambda e: e[0])

    n_err = sum(1 for e in entries if e[1] == "ERROR" and e[5])
    n_warn = sum(1 for e in entries if e[1] == "WARNING" and e[5])
    n_nmh_err = sum(1 for e in entries if e[1] == "ERROR" and not e[5])
    n_nmh_warn = sum(1 for e in entries if e[1] == "WARNING" and not e[5])
    rows = [
        {
            "i": i + 1,
            "time": e[0].strftime("%H:%M:%S.%f")[:-3],
            "level": e[1],
            "what": _friendly_what(e[4]),
            "source": f"{e[2]}:{e[3]}" if e[3] else e[2],
            "message": e[4][:2000],
            "mh": e[5],
        }
        for i, e in enumerate(entries[:limit])
    ]
    result = {
        "ok": True,
        "rows": rows,
        "errors": n_err,
        "warnings": n_warn,
        "nmh_errors": n_nmh_err,
        "nmh_warnings": n_nmh_warn,
        "total": len(entries),
        "lines_seen": lines_seen,
        "merged": merged,
        "truncated": len(entries) > limit,
    }
    _ERROR_GRID_CACHE[key] = result
    while len(_ERROR_GRID_CACHE) > 8:
        _ERROR_GRID_CACHE.pop(next(iter(_ERROR_GRID_CACHE)))
    return result
