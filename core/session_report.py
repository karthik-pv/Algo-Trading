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
import json
import os
import re
from collections import Counter
from datetime import datetime, time as dtime

from loguru import logger

from core.audit_log_parser import LOG_LINE_RE, parse_date_logs
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
                    from core.audit_log_parser import (
                        parse_date_logs, retag_untagged,
                    )
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
        _error_section(stats),
        _feed_section(stats, date_str, win),
        _health_section(stats),
        _verdict_section(stats, records),
    ]
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
