"""
Offline Trade Execution Report reconstruction from the datewise session
logs (data/logs/MM_DD/app_*.log).

Log lines look like:
2026-09-23 10:38:03:166 | DEBUG | trade_logic.py | handle_buy_order | Received buy order from client: {...} |

The click -> execution lifecycle of every trade is recoverable from the
event lines the app already writes; fill PRICES are only recoverable
where the logs carry them (Current LTP, auto-sell LTP, resting exit
limit, paper prices, avg-price refresh dumps). The API merges broker
order-book data on top for the current day when available.
"""

import ast
import json
import os
import re
from datetime import datetime

from loguru import logger

from core.utils import DATA_DIR

# --- loguru line format: {ts} | {LEVEL:<8} | {file:<25} | {func:<25} | {msg} ---
LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):(\d{3})\s*\|\s*"
    r"([A-Z]+)\s*\|\s*"
    r"([\w.]+)\s*\|\s*"
    r"([\w.]+)\s*\|\s*"
    r"(.*)$"
)

# --- event extractors (verified against real session logs) ---
BUY_CLICK_RE = re.compile(r"Received buy order from client: (\{.*\})")
SELL_CLICK_RE = re.compile(r"Received sell order from client: (\{.*\})")
BUY_UNITS_RE = re.compile(
    r"Buying(?:/Selling)? units: (\d+) of (\S+) \((\d+)\) via \S+"
    r".*?Current LTP: (\d+(?:\.\d+)?)"
)
SELL_UNITS_TEMP_RE = re.compile(
    r"Selling units: (\d+) of (\S+) \((\d+) (?:LIMIT|MARKET) ([\d.]+)\s*\)"
)
HTTP_TIMING_RE = re.compile(r"M\.Stock BUY HTTP TIMING")
BUY_RESPONSE_RE = re.compile(r"Buy order\s+response raw:\s*(.+)$")
BUY_ERR_RE = re.compile(
    r"(?:Error placing buy order|Retry buy order failed):\s*(.+)"
)
KITE_BUY_OK_RE = re.compile(r"Kite buy order placed successfully")
# U-mode exit placed inline by buy_sell_units (never routed through
# sell_units_temp): "LTP: 28.45 ; Target Profit : 2.5 ; So Selling Price is 30.95"
UD_EXIT_RE = re.compile(
    r"LTP: \d+(?:\.\d+)? ; Target Profit : \d+(?:\.\d+)? ; "
    r"So Selling Price is (\d+(?:\.\d+)?)"
)
AUTO_SELL_RE = re.compile(
    r"Placing SELL order for (.+?) (\d+) : (\d+) lots at LTP ([\d.]+)"
)
SELL_OK_RE = re.compile(r"Sell order successful for (.+?) \((\d+) lots\)")
BROKER_FILL_RE = re.compile(r"Broker-side SELL fill detected")
AVG_PRICE_RE = re.compile(
    r"positions after calculating average buy price is (\[.*\])", re.DOTALL
)
TEMP_BUY_RE = re.compile(
    r"TEMP BUY GRID UPDATE: (\S+) \[([^\]]+)\] \| Qty=(\d+) \| Temporary Buy=([\d.]+)"
)
RETRY_RE = re.compile(r"retrying with (\d+) lot")
PAPER_BUY_RE = re.compile(
    r"Paper BUY recorded:\s*(\S+)\s*Token=(\S+)\s*Qty=(\d+)\s*"
    r"Price=([\d.]+)\s*Strategy=(\S+)\s*SellMode=(\S+)"
)
PAPER_SELL_RE = re.compile(
    r"Paper SELL recorded for\s*(\S+)\s*\[([^\]]+)\]\s*\((\d+) lots\) @ ([\d.]+)"
)
KITE_EXIT_RE = re.compile(
    r"Kite SELL LIMIT exit placed at ([\d.]+) for (\S+) \((\d+) lots\)"
)

# Max seconds between a click and its execution markers / a trigger and
# its fill before events are considered unrelated.
EXEC_WINDOW = 120
AVG_WINDOW = 120

# Per-file parsed-event cache: path -> ((mtime_ns, size), events).
# Makes repeated /api/audit loads near-instant; only the growing live
# session log is ever re-read.
_EVENT_CACHE = {}


def canonical_symbol(symbol):
    """Reduce the broker's symbol spellings to one pairing key:
    NIFTY-22Sep2026-23600-CE / NIFTY2692223600CE / NIFTY26SEP23600CE all
    become NIFTY23600CE."""
    s = str(symbol or "").upper().replace(" ", "")
    m = re.search(
        r"(NIFTY|SENSEX)-\d{1,2}[A-Z]{3}\d{4}-(\d+(?:\.\d+)?)-(CE|PE)", s
    )
    if m:
        return f"{m.group(1)}{int(float(m.group(2)))}{m.group(3)}"
    m = re.search(r"(NIFTY|SENSEX)\d{2}[A-Z]{3}(\d+(?:\.\d+)?)(CE|PE)$", s)
    if m:
        return f"{m.group(1)}{int(float(m.group(2)))}{m.group(3)}"
    # Weekly numeric-expiry form: NIFTY2692223600CE (26|9|22 expiry)
    m = re.search(r"(NIFTY|SENSEX)\d{5}(\d+(?:\.\d+)?)(CE|PE)$", s)
    if m:
        return f"{m.group(1)}{int(float(m.group(2)))}{m.group(3)}"
    return re.sub(r"[^A-Z0-9]", "", s)


def _parse_ts(date_part, millis):
    try:
        base = datetime.strptime(date_part, "%Y-%m-%d %H:%M:%S")
        return base.timestamp() + int(millis) / 1000.0
    except (ValueError, OSError):
        return None


def _collect_events(path):
    """Extract ordered (ts, kind, payload) events from one log file."""
    events = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = LOG_LINE_RE.match(line)
                if not m:
                    continue
                ts = _parse_ts(m.group(1), m.group(2))
                if ts is None:
                    continue
                level = m.group(3)
                fn = m.group(5)
                msg = m.group(6)

                pm = BUY_CLICK_RE.search(msg)
                if pm:
                    try:
                        payload = ast.literal_eval(pm.group(1))
                    except (ValueError, SyntaxError):
                        payload = {}
                    events.append((ts, "BUY_CLICK", payload))
                    continue

                pm = SELL_CLICK_RE.search(msg)
                if pm:
                    try:
                        payload = ast.literal_eval(pm.group(1))
                    except (ValueError, SyntaxError):
                        payload = {}
                    events.append((ts, "SELL_CLICK", payload))
                    continue

                pm = BUY_UNITS_RE.search(msg)
                if pm and fn in ("buy_units", "buy_sell_units"):
                    events.append((ts, "BUY_LTP", {
                        "lots": int(pm.group(1)),
                        "symbol": pm.group(2),
                        "token": pm.group(3),
                        "ltp": float(pm.group(4)),
                    }))
                    continue

                if HTTP_TIMING_RE.search(msg) and fn == "sell_units":
                    events.append((ts, "SELL_EXEC", {}))
                    continue

                pm = BUY_RESPONSE_RE.search(msg)
                if pm and fn in ("buy_units", "buy_sell_units"):
                    # Parse the broker response JSON: status + script +
                    # rejection message.
                    ok, script, rmsg = False, None, None
                    raw = pm.group(1).rstrip().rstrip("|").rstrip()
                    try:
                        # Broker responses are JSON (true/false/null) -
                        # json.loads, with a literal_eval fallback.
                        try:
                            data = json.loads(raw)
                        except (ValueError, TypeError):
                            data = ast.literal_eval(raw)
                        if isinstance(data, list):
                            data = data[0] if data else {}
                        ok = bool(data.get("status"))
                        rmsg = data.get("message")
                        script = (data.get("data") or {}).get("script")
                    except (ValueError, SyntaxError, AttributeError, IndexError, TypeError):
                        pass
                    if script or not ok:
                        events.append((ts, "BUY_RESPONSE", {
                            "symbol": script, "ok": ok, "msg": rmsg,
                        }))
                    continue

                if KITE_BUY_OK_RE.search(msg) and fn == "buy_units":
                    events.append((ts, "BUY_RESPONSE", {
                        "symbol": None, "ok": True, "msg": None,
                    }))
                    continue

                pm = BUY_ERR_RE.search(msg)
                if (fn == "handle_buy_order" and level == "ERROR" and pm):
                    events.append((ts, "BUY_FAIL", {
                        "token": None,
                        "msg": pm.group(1).strip("| ").strip(),
                    }))
                    continue

                pm = AUTO_SELL_RE.search(msg)
                if pm and fn == "stop_loss_book_profit_core":
                    events.append((ts, "SELL_TRIGGER_AUTO", {
                        "symbol": pm.group(1),
                        "token": pm.group(2),
                        "lots": int(pm.group(3)),
                        "ltp": float(pm.group(4)),
                    }))
                    continue

                pm = SELL_OK_RE.search(msg)
                if pm:
                    events.append((ts, "SELL_OK", {
                        "symbol": pm.group(1), "lots": int(pm.group(2)),
                    }))
                    continue

                if BROKER_FILL_RE.search(msg):
                    events.append((ts, "BROKER_FILL", {}))
                    continue

                pm = AVG_PRICE_RE.search(msg)
                if pm:
                    try:
                        positions = ast.literal_eval(pm.group(1))
                    except (ValueError, SyntaxError):
                        positions = []
                    if isinstance(positions, list):
                        events.append((ts, "BUY_AVG", positions))
                    continue

                pm = TEMP_BUY_RE.search(msg)
                if pm:
                    events.append((ts, "TEMP_BUY", {
                        "symbol": pm.group(1),
                        "key": pm.group(2),
                        # Leg key "token:STRATEGY:MODE" -> token
                        "token": pm.group(2).split(":")[0],
                        "qty": int(pm.group(3)),
                        "price": float(pm.group(4)),
                    }))
                    continue

                pm = RETRY_RE.search(msg)
                if pm:
                    events.append((ts, "RETRY", {"lots": int(pm.group(1))}))
                    continue

                pm = PAPER_BUY_RE.search(msg)
                if pm:
                    events.append((ts, "PAPER_BUY", {
                        "symbol": pm.group(1), "token": pm.group(2),
                        "lots": int(pm.group(3)), "price": float(pm.group(4)),
                        "strategy": pm.group(5), "sell_mode": pm.group(6),
                    }))
                    continue

                pm = PAPER_SELL_RE.search(msg)
                if pm:
                    events.append((ts, "PAPER_SELL", {
                        "symbol": pm.group(1), "key": pm.group(2),
                        "lots": int(pm.group(3)), "price": float(pm.group(4)),
                    }))
                    continue

                pm = UD_EXIT_RE.search(msg)
                if pm and fn == "buy_sell_units":
                    events.append((ts, "RESTING_EXIT", {
                        "price": float(pm.group(1)),
                    }))
                    continue

                pm = SELL_UNITS_TEMP_RE.search(msg)
                if pm and fn == "sell_units_temp":
                    events.append((ts, "RESTING_EXIT", {
                        "lots": int(pm.group(1)),
                        "symbol": pm.group(2),
                        "token": pm.group(3),
                        "price": float(pm.group(4)),
                    }))
                    continue

                pm = KITE_EXIT_RE.search(msg)
                if pm:
                    events.append((ts, "RESTING_EXIT", {
                        "price": float(pm.group(1)),
                        "symbol": pm.group(2),
                        "lots": int(pm.group(3)),
                    }))
    except OSError as e:
        logger.error(f"Audit log parser: could not read {path}: {e}")
    return events


def _find(records, match, require_open=False):
    """Most recent record satisfying match(rec); open = has a buy and
    no sell execution yet."""
    for rec in reversed(records):
        if require_open:
            if not rec.get("buy_exec_ts") or rec.get("sell_exec_ts"):
                continue
        if match(rec):
            return rec
    return None


def _mark_buy_failed(rec, msg):
    """Mark a click as a failed order. If execution was only inferred
    from a position dump (which belonged to an earlier trade of the
    same contract), roll that inference back first."""
    if rec.get("exec_from_dump"):
        rec["buy_exec_ts"] = None
        rec["buy_avg_ts"] = None
        rec["buy_avg_price"] = None
        rec["ltp_exec"] = None
        rec["exec_from_dump"] = False
    if not rec.get("buy_exec_ts"):
        rec["buy_success"] = False
        rec["buy_note"] = msg


def _group(events):
    """Fold chronological events into trade records.

    Buy-side markers are attributed by CLICK INTERVAL: the events
    between click N and click N+1 (per token, or per symbol for the
    broker response which carries the script name) belong to trade N.
    This survives interleaved/concurrent buys of the same contract,
    double-clicks and multi-leg iceberg orders.
    """
    records = []
    rec_by_key = {}
    clicks_by_token = {}

    for ts, kind, payload in events:
        if kind == "BUY_CLICK":
            token = str(payload.get("token", ""))
            clicks_by_token.setdefault(token, []).append(ts)

    def own_by_token(token, ts):
        """Record owning this timestamp: same token, latest click
        at or before ts."""
        clicks = clicks_by_token.get(str(token), [])
        owner = None
        for c in clicks:
            if c <= ts:
                owner = c
            else:
                break
        if owner is None:
            return None
        return rec_by_key.get((str(token), owner))

    def own_by_symbol(symbol, ts):
        """Same as own_by_token but matched through the canonical
        symbol (broker responses spell the symbol differently)."""
        want = canonical_symbol(symbol)
        best = None
        for rec in records:
            if canonical_symbol(rec.get("symbol")) != want:
                continue
            click = rec.get("buy_click_ts")
            if click is None or click > ts:
                continue
            if best is None or click > best.get("buy_click_ts"):
                best = rec
        return best

    def same_token(rec, payload):
        return str(rec.get("token")) == str(payload.get("token", ""))

    def same_symbol(rec, payload):
        return canonical_symbol(rec.get("symbol")) == canonical_symbol(
            payload.get("symbol")
        )

    for ts, kind, payload in events:
        if kind == "BUY_CLICK":
            # Collapse double-fires: a repeat click for the SAME
            # contract/strategy/mode within 2s is one trade intent
            # (the grid button re-emitted). Keep the first click -
            # that is the true button-press time.
            token = str(payload.get("token", ""))
            dup = next(
                (
                    r for r in reversed(records)
                    if str(r.get("token")) == token
                    and r.get("strategy") == payload.get("strategy", "")
                    and r.get("sell_mode") == payload.get("SELL_MODE", "")
                    and r.get("buy_click_ts")
                    and ts - r["buy_click_ts"] < 2.0
                ),
                None,
            )
            if dup is not None:
                continue
            rec = {
                "source": "logs",
                "symbol": payload.get("tradingsymbol", ""),
                "token": token,
                "lots": payload.get("lots", ""),
                "strategy": payload.get("strategy", ""),
                "sell_mode": payload.get("SELL_MODE", ""),
                "buy_click_ts": ts,
                "first_attempt_ts": ts,
                "retry_count": 0,
                "retries": [],
            }
            records.append(rec)
            rec_by_key[(token, ts)] = rec
            continue

        if kind == "BUY_LTP":
            rec = own_by_token(payload.get("token"), ts)
            if rec is not None and rec.get("ltp_click") is None:
                rec["ltp_click"] = payload["ltp"]
            continue

        if kind == "BUY_RESPONSE":
            # Broker success-response receipt (all buy flows). One per
            # iceberg leg - the LAST successful response in the click
            # interval is the full execution. A failed response marks
            # the trade as failed (carries the rejection reason) and
            # ROLLS BACK dump-derived execution evidence (the position
            # dump belonged to an earlier trade of the same contract).
            if payload.get("symbol"):
                rec = own_by_symbol(payload.get("symbol"), ts)
            else:
                # Kite's success line names no symbol - the buy in
                # flight is the most recent unexecuted click.
                rec = _find(
                    records,
                    lambda r: r.get("buy_click_ts")
                    and not r.get("buy_exec_ts")
                    and ts - r["buy_click_ts"] < EXEC_WINDOW,
                )
            if rec is None:
                continue
            if payload.get("ok"):
                # The retry loop means a failure response may PRECEDE a
                # success for the same click - the success wins.
                rec["buy_success"] = True
                rec["buy_note"] = None
                rec["exec_from_dump"] = False
                rec["buy_exec_ts"] = ts
                if rec.get("first_attempt_ltp") is None:
                    rec["first_attempt_ltp"] = rec.get("ltp_click")
            elif not rec.get("buy_exec_ts") or rec.get("exec_from_dump"):
                _mark_buy_failed(rec, payload.get("msg") or "order rejected")
            continue

        if kind == "BUY_FAIL":
            # Connection-level failures (no response raw line at all).
            rec = own_by_token(payload.get("token"), ts) if payload.get("token") else None
            if rec is None:
                rec = _find(
                    records,
                    lambda r: not r.get("buy_exec_ts")
                    and r.get("buy_click_ts")
                    and ts - r["buy_click_ts"] < EXEC_WINDOW,
                )
            if rec is not None:
                _mark_buy_failed(rec, payload.get("msg") or "order failed")
            continue

        if kind == "RETRY":
            rec = _find(records, lambda r: not r.get("buy_exec_ts"))
            if rec is not None:
                rec["retry_count"] = int(rec.get("retry_count") or 0) + 1
                rec.setdefault("retries", []).append(
                    {"ts": ts, "lots": payload.get("lots")}
                )
            continue

        if kind == "BUY_AVG":
            # The refresh dump lists every open position. Two uses:
            # 1. authoritative average for a trade whose exec is known;
            # 2. execution EVIDENCE for clicks that left no response
            #    line (old simulator flows): the position appearing in
            #    a dump right after the click proves the fill.
            for rec in records:
                if rec.get("buy_success") is False and not rec.get("buy_exec_ts"):
                    # A failed order with no execution - a position
                    # dump can only belong to an earlier trade.
                    continue
                executed = bool(rec.get("buy_exec_ts"))
                if executed and rec.get("buy_avg_price"):
                    continue
                anchor = rec.get("buy_exec_ts") or rec.get("buy_click_ts")
                if anchor is None or ts - anchor > AVG_WINDOW:
                    continue
                for pos in payload:
                    if not isinstance(pos, dict):
                        continue
                    if canonical_symbol(pos.get("tradingsymbol")) == canonical_symbol(rec.get("symbol")):
                        try:
                            qty = float(pos.get("quantity") or 0)
                            raw_avg = pos.get("average_price")
                            avg = (
                                float(raw_avg)
                                if raw_avg not in (None, "", 0)
                                else None
                            )
                        except (TypeError, ValueError):
                            qty, avg = 0.0, None
                        if qty > 0:
                            if not executed:
                                rec["buy_exec_ts"] = ts
                                rec["exec_from_dump"] = True
                                if rec.get("first_attempt_ltp") is None:
                                    rec["first_attempt_ltp"] = rec.get("ltp_click")
                            if avg and not rec.get("buy_avg_price"):
                                rec["buy_avg_ts"] = ts
                                rec["buy_avg_price"] = avg
                        break
            continue

        if kind == "TEMP_BUY":
            rec = own_by_token(payload.get("token"), ts)
            if (
                rec is not None
                and rec.get("buy_exec_ts")
                and ts - rec["buy_exec_ts"] < AVG_WINDOW
            ):
                # The grid's first post-execution price receipt - the
                # latest tick at fill time doubles as LTP @ Exec. The
                # AUTHORITATIVE average arrives via the AVG_PRICE dump.
                if rec.get("ltp_exec") is None:
                    rec["ltp_exec"] = payload["price"]
            continue

        if kind in ("SELL_CLICK", "SELL_TRIGGER_AUTO", "RESTING_EXIT"):
            source = {
                "SELL_CLICK": "MANUAL",
                "SELL_TRIGGER_AUTO": "SL_WATCHER",
                "RESTING_EXIT": "RESTING_EXIT",
            }[kind]
            if kind == "SELL_CLICK":
                rec = _find(
                    records,
                    lambda r, p=payload: same_token(r, p),
                    require_open=True,
                )
            elif payload.get("symbol"):
                rec = _find(
                    records,
                    lambda r, p=payload: same_symbol(r, p),
                    require_open=True,
                )
            else:
                # Symbol-less event (the inline U/D exit inside the buy
                # flow): the trade just bought is the open leg.
                rec = _find(records, lambda r: True, require_open=True)
            if rec is not None:
                supersede = (
                    rec.get("sell_trigger_source") == "RESTING_EXIT"
                    and kind in ("SELL_CLICK", "SELL_TRIGGER_AUTO")
                )
                if not rec.get("sell_trigger_ts") or supersede:
                    # A manual/watcher sell CANCELS the resting exit
                    # and fills itself - it is the real trigger.
                    rec["sell_trigger_ts"] = ts
                    rec["sell_trigger_source"] = source
                    if kind == "SELL_TRIGGER_AUTO":
                        rec["sell_trigger_ltp"] = payload.get("ltp")
                        if payload.get("lots"):
                            rec["lots"] = payload["lots"]
                    elif kind == "RESTING_EXIT":
                        rec["sell_trigger_ltp"] = payload.get("price")
                if kind == "RESTING_EXIT" and not rec.get("resting_exit_placed_ts"):
                    rec["resting_exit_placed_ts"] = ts
                    rec["resting_exit_price"] = payload.get("price")
            continue

        if kind in ("SELL_EXEC", "SELL_OK"):
            rec = _find(
                records,
                lambda r: r.get("sell_trigger_ts")
                and not r.get("sell_exec_ts")
                and ts - r["sell_trigger_ts"] < EXEC_WINDOW * 3,
                require_open=True,
            )
            if rec is not None and not rec.get("sell_exec_ts"):
                rec["sell_exec_ts"] = ts
            continue

        if kind == "BROKER_FILL":
            # The fill line carries only order ids; attribute by
            # elimination: the oldest open leg whose exit was a resting
            # limit, else the oldest open leg.
            open_recs = [
                r for r in records
                if r.get("buy_exec_ts") and not r.get("sell_exec_ts")
            ]
            resting = [r for r in open_recs if r.get("resting_exit_placed_ts")]
            rec = (resting or open_recs)
            if rec:
                rec = rec[0]
                rec["sell_exec_ts"] = ts
                rec["sell_trigger_source"] = rec.get(
                    "sell_trigger_source") or "RESTING_EXIT"
            continue

        if kind == "PAPER_BUY":
            records.append({
                "source": "logs",
                "symbol": payload.get("symbol", ""),
                "token": str(payload.get("token", "")),
                "lots": payload.get("lots", ""),
                "strategy": payload.get("strategy", ""),
                "sell_mode": payload.get("sell_mode", ""),
                "buy_click_ts": ts,
                "buy_exec_ts": ts,
                "ltp_click": payload.get("price"),
                "ltp_exec": payload.get("price"),
                "buy_avg_ts": ts,
                "buy_avg_price": payload.get("price"),
                "buy_success": True,
                "first_attempt_ts": ts,
                "first_attempt_ltp": payload.get("price"),
                "retry_count": 0,
                "retries": [],
            })
            continue

        if kind == "PAPER_SELL":
            rec = _find(
                records,
                lambda r, p=payload: same_token(r, p) or same_symbol(r, p),
                require_open=True,
            )
            if rec is None:
                rec = {
                    "source": "logs",
                    "symbol": payload.get("symbol", ""),
                    "token": "",
                    "lots": payload.get("lots", ""),
                    "strategy": "",
                    "sell_mode": "",
                    "retry_count": 0,
                    "retries": [],
                }
                records.append(rec)
            rec["sell_trigger_ts"] = ts
            rec["sell_trigger_ltp"] = payload.get("price")
            rec["sell_trigger_source"] = "MANUAL"
            rec["sell_exec_ts"] = ts
            rec["sell_exec_ltp"] = payload.get("price")
            rec["sell_success"] = True
            continue

    return [r for r in records if r.get("buy_click_ts") or r.get("buy_exec_ts") or r.get("sell_exec_ts")]


def parse_date_logs(date_str, logs_root=None):
    """Parse every session log for the given date (YYYY-MM-DD) into
    trade execution records, chronologically ordered. Per-file event
    caches keyed by (mtime, size) - only NEW or GROWN files (the live
    session log) are re-read on subsequent calls."""
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return []
    folder = os.path.join(
        logs_root or os.path.join(DATA_DIR, "logs"), day.strftime("%m_%d")
    )
    if not os.path.isdir(folder):
        return []

    events = []
    for fname in sorted(os.listdir(folder)):
        if not (fname.startswith("app_") and fname.endswith(".log")):
            continue
        path = os.path.join(folder, fname)
        try:
            stat = os.stat(path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
        cached = _EVENT_CACHE.get(path)
        if cached and cached[0] == stamp:
            events.extend(cached[1])
            continue
        file_events = _collect_events(path)
        _EVENT_CACHE[path] = (stamp, file_events)
        events.extend(file_events)

    events.sort(key=lambda e: e[0])
    try:
        return _group(events)
    except Exception as e:
        logger.error(f"Audit log parser: grouping failed for {date_str}: {e}")
        return []
