"""
Trade Execution Audit log.

Records the click -> execution -> avg-price lifecycle of every BUY and the
trigger -> execution lifecycle of every SELL, computes the slippage / time
cost metrics of the Trade Execution Technical Report, and persists each
session's events to data/audit/audit_YYYY-MM-DD.json so the Audit tab can
also report on trades placed before an app restart.

All timestamps are captured with time.time() (epoch seconds, millisecond
resolution when formatted) and rendered as HH:MM:SS.mmm local time.
"""

import json
import os
import re
import threading
import time
from datetime import datetime

from loguru import logger

from core.utils import DATA_DIR, fetch_from_json


AUDIT_DIR = os.path.join(DATA_DIR, "audit")


def _fmt_ts(ts):
    """Render an epoch timestamp as HH:MM:SS.mmm (or '' when missing)."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


# Broker display spelling NIFTY-29Sep2026-22600-PE -> the app's
# trading-symbol code NIFTY26SEP22600PE, so imported rows render the
# Symbol column in one uniform format. (The weekly day-of-month is not
# part of the app code; within a session that is never ambiguous.)
_BROKER_SYMBOL_RE = re.compile(
    r"^([A-Z]+)-\d{1,2}([A-Za-z]{3})(\d{4})-(\d+(?:\.\d+)?)-(CE|PE)$"
)


def _display_symbol(symbol):
    s = str(symbol or "")
    m = _BROKER_SYMBOL_RE.match(s)
    if not m:
        return s
    name, mon, year, strike, opt = m.groups()
    return f"{name}{year[2:]}{mon.upper()}{int(float(strike))}{opt.upper()}"


def _is_option_symbol(symbol, exchange=None):
    """OPTIONS-ONLY guard for the report - the same logic the Buy/Sell
    grids apply to positions (core.utils.is_option_instrument, which
    keeps stocks, ETFs and futures out). One twist: the broker's dashed
    spelling (NIFTY-29Sep2026-22600-PE) defeats the strike+CE/PE suffix
    regex, so the canonical app spelling is checked as well."""
    s = str(symbol or "").strip()
    if not s:
        return False
    from core.utils import is_option_instrument
    if is_option_instrument(symbol=s, exchange=exchange):
        return True
    from core.audit_log_parser import canonical_symbol
    return is_option_instrument(symbol=canonical_symbol(s), exchange=exchange)


def _fmt_num(value, decimals=2):
    if value is None or value == "":
        return ""
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return ""


class TradeAudit:
    """Thread-safe recorder + report builder for execution auditing."""

    # Grid / Excel column order. Shared by snapshot() (API) and the
    # Excel download so the two can never drift apart. Within BUY and
    # SELL, the time columns come first (contiguous - the sub-header
    # band splits each group into Time / Price), then Trigger Source,
    # then the price block.
    COLUMNS = [
        "Symbol", "Lots", "SS",
        "Buy Click Time", "Buy Exec Time", "Buy Time Slippage (s)",
        "LTP @ Click", "LTP @ Exec", "Buy Avg Price",
        "Buy Price Slippage", "Buy Price Slippage %", "Buy Lot Cost Slippage",
        "Sell Trigger Time", "Sell Exec Time", "Sell Time Slippage (s)",
        "Trigger Source",
        "LTP @ Trigger", "LTP @ Sell Exec", "Sell Avg Price",
        "Sell Price Slippage", "Sell Price Slippage %", "Sell Profit Slippage",
        "Peak LTP (hold)", "Peak Profit (pts)", "Peak Profit %", "Peak At",
        "Retries", "Retry Elapsed (s)", "First Attempt LTP",
        "Exec Slippage %", "Buy Cost Increase",
        "Comment",
    ]

    # Strategy + sell mode -> the compact "SS" code shown in the grid
    # (matches the Trade tab's SS column): UU = Ultra scalping + U, SD =
    # Scalping + D, IT = Intra + T, etc.
    STRATEGY_CODES = {
        "ULTRA_SCALPING": "U",
        "SCALPING": "S",
        "INTRA": "I",
    }

    # Column-group band: (caption, colspan) over COLUMNS - used by the
    # grid's group row and the Excel export's merged header band.
    COLUMN_GROUPS = [
        ("Trade", 3), ("Buy", 9), ("Sell", 10), ("Peak", 4),
        ("Retry", 5), ("Comment", 1),
    ]

    # Sub-header band under the BUY / SELL group cells: a second
    # grouping layer splitting each side into its Time block (click,
    # exec, latency - grayed in the UI) and Price block. Spans are
    # contiguous runs of that group's columns; groups not listed here
    # (Trade / Retry / Comment) rowspan across the sub-header row.
    SUB_GROUPS = {
        "Buy": [("Time", 3), ("Price", 6)],
        "Sell": [("Time", 3), ("", 1), ("Price", 6)],
    }

    # Columns rendered gray: the latency bookkeeping block (Time
    # sub-groups of Buy and Sell).
    TIME_COLUMNS = {
        "Buy Click Time", "Buy Exec Time", "Buy Time Slippage (s)",
        "Sell Trigger Time", "Sell Exec Time", "Sell Time Slippage (s)",
    }

    # Display captions for the THIRD row (short names - the group and
    # sub-header bands above already say BUY / SELL and TIME / PRICE).
    # Data keys (COLUMNS) stay unique for the API.
    DISPLAY_NAMES = {
        "Buy Click Time": "Click",
        "Buy Exec Time": "Exec",
        "Buy Time Slippage (s)": "Slip",
        "LTP @ Click": "Click",
        "LTP @ Exec": "Exec",
        "Buy Avg Price": "Avg",
        "Buy Price Slippage": "Slippage",
        "Buy Price Slippage %": "%",
        "Buy Lot Cost Slippage": "Cost Slippage",
        "Sell Trigger Time": "Click",
        "Sell Exec Time": "Exec",
        "Sell Time Slippage (s)": "Slip",
        "LTP @ Trigger": "Click",
        "LTP @ Sell Exec": "Exec",
        "Sell Avg Price": "Avg",
        "Sell Price Slippage": "Slippage",
        "Sell Price Slippage %": "%",
        "Sell Profit Slippage": "Profit Slippage",
        "Buy Cost Increase": "Cost Increase",
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._records = []
        self._persist_lock = threading.Lock()
        self._persist_pending = threading.Event()
        self._load_today()

    # -------------------------------------------------------------
    # Trading mode
    # -------------------------------------------------------------
    @staticmethod
    def _current_mode():
        """The app's trading mode at record time, bucketed to the two
        report views: PAPER vs everything that places real orders
        (LIVE / SIMULATION / PLAYBACK - the same branch the trade
        watcher uses). Paper and live executions must never be mixed
        in the report, so every record is tagged as it is created."""
        try:
            mode = str(fetch_from_json("appconfig.json", "MODE") or "")
        except Exception:
            mode = ""
        return "PAPER" if mode.strip().upper() == "PAPER" else "LIVE"

    # -------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------
    def _today_key(self):
        return datetime.now().strftime("%Y-%m-%d")

    def _file_path(self, day_key=None):
        return os.path.join(
            AUDIT_DIR, f"audit_{day_key or self._today_key()}.json"
        )

    def _load_today(self):
        """Load today's (already persisted) events so a restart keeps
        reporting the full session."""
        path = self._file_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    self._records = json.load(f) or []
        except Exception as e:
            logger.error(f"Audit log: could not load {path}: {e}")
            self._records = []

    def today_records(self):
        """Read-only copy of today's audit records. Used by the WOC
        rebuild to restore feed coverage for contracts traded earlier
        today (an app restart re-resolves the window from the current
        ATM, orphaning morning trades' Orders rows)."""
        with self._lock:
            return [dict(r) for r in self._records]

    def _persist(self, snapshot=None):
        """Write the records to disk in a BACKGROUND thread - order
        events stay purely in-memory (the write is a few hundred KB of
        JSON that must never sit inside the buy/sell path). A pending
        flag + single-writer loop guarantees the latest state always
        lands on disk even when events burst."""
        if snapshot is None:
            self._persist_pending.set()
        else:
            # Explicit snapshot (session end) - write exactly this.
            with self._persist_lock:
                self._write_snapshot(snapshot)
            return

        def _write():
            self._persist_pending.set()
            if not self._persist_lock.acquire(blocking=False):
                return  # another writer will pick up the pending flag
            try:
                while self._persist_pending.is_set():
                    self._persist_pending.clear()
                    with self._lock:
                        snap = [dict(r) for r in self._records]
                    self._write_snapshot(snap)
            finally:
                self._persist_lock.release()

        threading.Thread(target=_write, daemon=True,
                         name="audit-persist").start()

    def _write_snapshot(self, snapshot):
        try:
            os.makedirs(AUDIT_DIR, exist_ok=True)
            path = self._file_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=1, default=str)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"Audit log: persist failed: {e}")

    # -------------------------------------------------------------
    # Record matching
    # -------------------------------------------------------------
    def _match_open(self, token=None, symbol=None):
        """Most recent record for this token/symbol that has a buy but
        no sell execution yet. Falls back to a symbol-prefix match.
        token also accepts a composite position key
        (TOKEN:STRATEGY:MODE) - matched on its token prefix."""
        best = None
        want = str(token) if token else None
        for rec in reversed(self._records):
            if rec.get("sell_exec_ts"):
                continue
            if want:
                rec_token = str(rec.get("token"))
                if (rec_token == want
                        or want.startswith(rec_token + ":")):
                    return rec
            if not want and symbol:
                rec_sym = str(rec.get("symbol") or "")
                if rec_sym == symbol or rec_sym.startswith(str(symbol)):
                    best = best or rec
        return best

    def _match_latest_for_symbol(self, symbol):
        """Latest record for a symbol regardless of open/closed state
        (used by broker-side resting-exit fills)."""
        for rec in reversed(self._records):
            rec_sym = str(rec.get("symbol") or "")
            if rec_sym == symbol or rec_sym.startswith(str(symbol)):
                return rec
        return None

    # -------------------------------------------------------------
    # BUY events
    # -------------------------------------------------------------
    def record_buy_click(self, tradingsymbol, token, lots, ltp,
                         strategy="", sell_mode=""):
        now = time.time()
        with self._lock:
            rec = {
                "date": self._today_key(),
                "symbol": tradingsymbol,
                "token": str(token),
                "lots": lots,
                "strategy": strategy,
                "sell_mode": sell_mode,
                "trade_mode": self._current_mode(),
                "buy_click_ts": now,
                "ltp_click": ltp,
                # First-attempt baseline for the retry-slippage metrics.
                "first_attempt_ts": now,
                "first_attempt_ltp": ltp,
                "retry_count": 0,
                "retries": [],
            }
            self._records.append(rec)
            self._persist()
        return rec

    def record_buy_executed(self, token, lots, ltp, retried=False,
                            note="", success=True):
        with self._lock:
            rec = self._match_open(token=token)
            if rec is None:
                # Executed without a click on record (e.g. process
                # restart mid-flight) - synthesize a minimal row.
                rec = {
                    "date": self._today_key(),
                    "symbol": "?",
                    "token": str(token),
                    "lots": lots,
                    "strategy": "",
                    "sell_mode": "",
                    "trade_mode": self._current_mode(),
                    "retry_count": 1 if retried else 0,
                    "retries": [],
                }
                self._records.append(rec)
            if not rec.get("buy_click_ts"):
                rec["first_attempt_ltp"] = rec.get("first_attempt_ltp") or ltp
            rec["buy_exec_ts"] = time.time()
            rec["ltp_exec"] = ltp
            rec["buy_success"] = bool(success)
            if note:
                rec["buy_note"] = note
            if retried:
                rec["retried_success"] = True
            self._persist()

    def record_buy_avg_price(self, token, avg_price):
        """First post-execution average-price receipt (adapters call
        this right after the buy is confirmed)."""
        with self._lock:
            rec = self._match_open(token=token)
            if rec is None:
                return
            rec["buy_avg_ts"] = time.time()
            rec["buy_avg_price"] = avg_price
            self._persist()

    def record_retry_attempt(self, token, lots, ltp):
        with self._lock:
            rec = self._match_open(token=token)
            if rec is None:
                return
            rec["retry_count"] = int(rec.get("retry_count") or 0) + 1
            rec.setdefault("retries", []).append(
                {"ts": time.time(), "lots": lots, "ltp": ltp}
            )
            self._persist()

    # -------------------------------------------------------------
    # SELL events
    # -------------------------------------------------------------
    def record_sell_trigger(self, token, lots, ltp, source="MANUAL"):
        with self._lock:
            rec = self._match_open(token=token)
            if rec is None:
                return
            # First trigger wins; retries of a failed auto-sell must
            # not reset the clock.
            if not rec.get("sell_trigger_ts"):
                rec["sell_trigger_ts"] = time.time()
                rec["sell_trigger_ltp"] = ltp
                rec["sell_trigger_source"] = source
                self._persist()

    def record_sell_executed(self, token, lots, ltp, success=True):
        with self._lock:
            rec = self._match_open(token=token)
            if rec is None:
                return
            if rec.get("sell_exec_ts"):
                return  # keep the first fill
            rec["sell_exec_ts"] = time.time()
            rec["sell_exec_ltp"] = ltp
            rec["sell_success"] = bool(success)
            self._persist()

    def record_resting_exit_placed(self, symbol, lots, price):
        """A U/D resting exit limit was placed at the broker - its
        placement time is the Sell Trigger for those legs."""
        with self._lock:
            rec = self._match_open(symbol=symbol) or self._match_latest_for_symbol(symbol)
            if rec is None:
                return
            if not rec.get("resting_exit_placed_ts"):
                rec["resting_exit_placed_ts"] = time.time()
                rec["resting_exit_price"] = price
                # The resting exit IS the exit order for U/D legs.
                if not rec.get("sell_trigger_ts"):
                    rec["sell_trigger_ts"] = rec["resting_exit_placed_ts"]
                    rec["sell_trigger_ltp"] = price
                    rec["sell_trigger_source"] = "RESTING_EXIT"
            self._persist()

    def update_resting_exit_price(self, symbol, price):
        """A provisional (click-anchored) resting exit was re-anchored
        to the executed buy average - refresh the resting price so the
        audit reports the limit that actually governs the exit."""
        with self._lock:
            rec = self._match_open(symbol=symbol) or self._match_latest_for_symbol(symbol)
            if rec is None:
                return
            if not rec.get("resting_exit_placed_ts"):
                return
            rec["resting_exit_price"] = price
            if rec.get("sell_trigger_source") == "RESTING_EXIT":
                rec["sell_trigger_ltp"] = price
            self._persist()

    def record_broker_side_sell_fill(self, symbol, fill_price, filled_qty):
        """A resting exit filled at the broker (no app-placed sell)."""
        with self._lock:
            rec = self._match_latest_for_symbol(symbol)
            if rec is None:
                return
            if rec.get("sell_exec_ts"):
                return
            rec["sell_exec_ts"] = time.time()
            rec["sell_exec_ltp"] = fill_price
            # The resting exit fill IS the actual sell fill price.
            rec["sell_avg_price"] = fill_price
            rec["sell_success"] = True
            rec["sell_trigger_source"] = rec.get(
                "sell_trigger_source") or "RESTING_EXIT"
            if filled_qty:
                rec["sell_filled_qty"] = filled_qty
            self._persist()

    # -------------------------------------------------------------
    # Broker order-book backfill (trades that predate the live hooks)
    # -------------------------------------------------------------
    _EXECUTED_STATUSES = {"traded", "complete", "executed", "filled"}

    @staticmethod
    def _covered_by(records, symbol, side, ts):
        """True when a record already captured this order (same symbol
        canonically - broker and app spellings differ -, matching side,
        within a few seconds - distinct rebuys of the same contract sit
        minutes apart)."""
        from core.audit_log_parser import canonical_symbol
        sym_key = canonical_symbol(symbol)
        for rec in records:
            if canonical_symbol(rec.get("symbol")) != sym_key:
                continue
            live = (
                rec.get("buy_exec_ts") if side == "BUY"
                else rec.get("sell_exec_ts")
            )
            if live and abs(live - ts) < 15:
                return True
        return False

    def _merge_broker_orders(self, records, orders, lot_size_lookup=None):
        """Fill execution prices from today's broker orders into
        records missing them, and import still-uncovered orders as
        IMPORTED rows (trades placed outside the app's live audit
        hooks - broker terminal trades or broker-side exit fills)."""
        if not orders:
            return
        try:
            import pandas as pd
        except ImportError:
            return
        # The broker spells symbols NIFTY-29Sep2026-22600-PE while the
        # live hooks store NIFTY26SEP22600PE - compare canonically or
        # every broker order looks uncovered and gets duplicated.
        from core.audit_log_parser import canonical_symbol

        today = datetime.now().date()
        # Deduplicate partial-fill rows of the same order id: keep the
        # row with the largest fill.
        best_by_id = {}
        for order in orders:
            if not isinstance(order, dict):
                continue
            side = str(order.get("transaction_type") or "").upper()
            status = str(order.get("order_status")
                         or order.get("status") or "").lower()
            symbol = order.get("tradingsymbol")
            if side not in ("BUY", "SELL") or status not in self._EXECUTED_STATUSES:
                continue
            try:
                qty = float(order.get("quantity") or 0)
                price = float(order.get("average_price") or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0 or price <= 0 or not symbol:
                continue
            # OPTIONS-ONLY GUARD: the report tracks options exclusively -
            # never import (or enrich from) futures, cash-segment stocks
            # or ETFs traded outside the app. Same rule the Buy/Sell
            # grids apply to positions.
            if not _is_option_symbol(symbol, order.get("exchange")):
                continue
            ts = pd.to_datetime(
                order.get("timestamp"), errors="coerce",
                dayfirst=True, format="mixed"
            )
            if pd.isna(ts) or ts.date() != today:
                continue
            # pandas .timestamp() treats naive stamps as UTC (a +5:30
            # shift on IST machines); Python datetime assumes local -
            # exchange order times ARE local wall-clock.
            ts_epoch = ts.to_pydatetime().timestamp()
            oid = order.get("order_id") or f"{symbol}:{side}:{ts}"
            prev = best_by_id.get(oid)
            if prev is None or qty > prev["_qty"]:
                best_by_id[oid] = {
                    "_qty": qty, "side": side, "symbol": symbol,
                    "ts_epoch": ts_epoch,
                    "qty": qty, "price": price,
                }

        # Lots via the broker's instrument details (cached per symbol).
        lot_cache = {}

        def _lots_for(symbol, order):
            if symbol in lot_cache:
                return lot_cache[symbol]
            lots = None
            try:
                lot_size = None
                if lot_size_lookup:
                    lot_size = lot_size_lookup(symbol)
                if lot_size and float(lot_size) > 0:
                    lots = int(round(float(order["qty"]) / float(lot_size)))
            except Exception:
                lots = None
            lot_cache[symbol] = lots
            return lots

        buys = []
        for oid, order in best_by_id.items():
            symbol = order["symbol"]
            ts_epoch = order["ts_epoch"]

            # Price enrichment: the nearest same-symbol record to this
            # order's timestamp gets the fill price the logs could not
            # capture (exchange ts vs receipt ts differ by ~1-2s).
            # Broker-book prices are live-only - paper records must
            # never receive them.
            sym_key = canonical_symbol(symbol)
            candidates = [
                rec for rec in records
                if str(rec.get("trade_mode") or "LIVE").upper() != "PAPER"
                and canonical_symbol(rec.get("symbol")) == sym_key
            ]
            best_rec, best_gap = None, None
            for rec in candidates:
                anchor = (
                    rec.get("buy_exec_ts") if order["side"] == "BUY"
                    else rec.get("sell_exec_ts")
                )
                if anchor is None:
                    continue
                gap = abs(anchor - ts_epoch)
                if gap < 15 and (best_gap is None or gap < best_gap):
                    best_rec, best_gap = rec, gap
            if best_rec is not None:
                if order["side"] == "BUY":
                    if not best_rec.get("ltp_exec"):
                        best_rec["ltp_exec"] = order["price"]
                    # Broker average_price is authoritative ONLY for
                    # log-reconstructed rows (no live fill capture).
                    # Live-hook rows keep their own fill - the broker
                    # book may hold an external same-strike order in
                    # the match window, and overwriting would misprice
                    # the live trade.
                    if (best_rec.get("buy_avg_price") != order["price"]
                            and best_rec.get("source") == "logs"):
                        best_rec["buy_avg_price"] = order["price"]
                        best_rec["buy_avg_ts"] = ts_epoch
                    elif not best_rec.get("buy_avg_price"):
                        best_rec["buy_avg_price"] = order["price"]
                        best_rec["buy_avg_ts"] = ts_epoch
                else:
                    if not best_rec.get("sell_avg_price") or (
                            best_rec.get("source") == "logs"):
                        # Broker average_price is the actual SELL fill -
                        # kept separate from the LTP @ Exec tick.
                        best_rec["sell_avg_price"] = order["price"]
                if not best_rec.get("lots") and lot_size_lookup:
                    best_rec["lots"] = _lots_for(symbol, order)

            if self._covered_by(records, symbol, order["side"], ts_epoch):
                continue

            if order["side"] == "BUY":
                rec = {
                    "imported": True,
                    "symbol": symbol,
                    "token": "",
                    "lots": _lots_for(symbol, order),
                    "strategy": "",
                    "sell_mode": "",
                    "trade_mode": "LIVE",
                    "buy_exec_ts": ts_epoch,
                    "ltp_exec": order["price"],
                    "buy_avg_ts": ts_epoch,
                    "buy_avg_price": order["price"],
                    "buy_success": True,
                }
                records.append(rec)
                buys.append(rec)
            else:
                # Pair with the earliest still-open imported BUY of the
                # same symbol, else record a sell-only row.
                open_buy = next(
                    (b for b in buys
                     if b["symbol"] == symbol and not b.get("sell_exec_ts")),
                    None
                )
                if open_buy is not None:
                    open_buy["sell_trigger_ts"] = ts_epoch
                    open_buy["sell_trigger_ltp"] = order["price"]
                    open_buy["sell_trigger_source"] = "IMPORTED"
                    open_buy["sell_exec_ts"] = ts_epoch
                    open_buy["sell_exec_ltp"] = order["price"]
                    open_buy["sell_avg_price"] = order["price"]
                    open_buy["sell_success"] = True
                else:
                    records.append({
                        "imported": True,
                        "symbol": symbol,
                        "token": "",
                        "lots": _lots_for(symbol, order),
                        "strategy": "",
                        "sell_mode": "",
                        "trade_mode": "LIVE",
                        "sell_trigger_ts": ts_epoch,
                        "sell_trigger_ltp": order["price"],
                        "sell_trigger_source": "IMPORTED",
                        "sell_exec_ts": ts_epoch,
                        "sell_exec_ltp": order["price"],
                        "sell_avg_price": order["price"],
                        "sell_success": True,
                    })

        for rec in records:
            if rec.get("imported") and not rec.get("imported_note"):
                rec["imported_note"] = (
                    "Imported from broker order book - placed outside the "
                    "app's audit hooks (broker terminal trade, or an exit "
                    "order filled broker-side)"
                )

    # -------------------------------------------------------------
    # Report builder
    # -------------------------------------------------------------
    @staticmethod
    def _comment_for(out):
        """One-line execution commentary built from the computed metrics."""
        parts = []
        btc = out.get("Buy Time Slippage (s)")
        if btc != "":
            btc = float(btc)
            if btc < 0.5:
                parts.append("Fast buy fill")
            elif btc < 1.5:
                parts.append("Normal buy fill")
            elif btc < 3:
                parts.append("Slow buy fill")
            else:
                parts.append(f"Very slow buy fill ({btc:.1f}s)")
        bs_pct = out.get("Buy Price Slippage %")
        if bs_pct != "" and bs_pct is not None:
            bs_pct = float(bs_pct)
            if bs_pct > 0.75:
                parts.append("heavy buy slippage")
            elif bs_pct > 0.25:
                parts.append("mild buy slippage")
            else:
                parts.append("tight fill vs LTP")
        if int(out.get("Retries") or 0) > 0:
            parts.append(
                f"lots reduced on retry x{out.get('Retries')} "
                f"({out.get('Exec Slippage %') or 0:+.2f}% entry cost)"
            )
        stc = out.get("Sell Time Slippage (s)")
        if stc != "":
            parts.append(
                f"sell filled in {float(stc):.2f}s "
                f"({str(out.get('Trigger Source') or '').lower()})"
            )
        ss_pct = out.get("Sell Price Slippage %")
        if ss_pct != "" and ss_pct is not None:
            ss_pct = float(ss_pct)
            if ss_pct < -0.75:
                parts.append("heavy sell slippage")
            elif ss_pct < -0.25:
                parts.append("mild sell slippage")
            else:
                parts.append("clean sell exit")
        if out.get("Buy Exec Time") == "" and out.get("Sell Exec Time") == "":
            return "Awaiting execution events"
        return "; ".join(p for p in parts if p) or "Buy pending"

    @classmethod
    def _ss_code(cls, rec):
        """Compact Strategy+SellMode code: UU / SD / IT / UT ..."""
        strategy = str(rec.get("strategy") or "").strip().upper()
        mode = str(rec.get("sell_mode") or "").strip().upper()[:1]
        if not strategy and not mode:
            return ""
        strat_code = cls.STRATEGY_CODES.get(strategy)
        if strat_code is None:
            strat_code = strategy[:1].upper() if strategy else "?"
        mode_code = mode if mode in ("U", "D", "T") else (mode or "?")
        return f"{strat_code}{mode_code}"

    def _computed(self, rec, date_str=None):
        """Return a display-ready copy with derived metrics + formatted
        times. Price Slippage = Avg Price - LTP @ Click (sell: Sell Avg
        - LTP @ Trigger): how far the actual fill landed from the price
        on screen when the trade was clicked - the real cost of the
        click -> fill delay. Time Slippage = that latency.
        `date_str` selects the day's tick-capture CSVs for the peak
        columns (defaults to today)."""
        out = {
            "Symbol": _display_symbol(rec.get("symbol", "")),
            "Lots": rec.get("lots", ""),
            "SS": self._ss_code(rec),
            "Buy Click Time": _fmt_ts(rec.get("buy_click_ts")),
            "Buy Exec Time": _fmt_ts(rec.get("buy_exec_ts")),
            "Buy Time Slippage (s)": "",
            "LTP @ Click": _fmt_num(rec.get("ltp_click")),
            "LTP @ Exec": _fmt_num(rec.get("ltp_exec")),
            "Buy Avg Price": _fmt_num(rec.get("buy_avg_price")),
            "Buy Price Slippage": "",
            "Buy Price Slippage %": "",
            "Buy Lot Cost Slippage": "",
            "Sell Trigger Time": _fmt_ts(
                rec.get("sell_trigger_ts")
                or rec.get("resting_exit_placed_ts")
            ),
            "Trigger Source": rec.get("sell_trigger_source", ""),
            "Sell Exec Time": _fmt_ts(rec.get("sell_exec_ts")),
            "Sell Time Slippage (s)": "",
            "LTP @ Trigger": _fmt_num(
                rec.get("sell_trigger_ltp")
                or rec.get("resting_exit_price")
            ),
            "LTP @ Sell Exec": _fmt_num(rec.get("sell_exec_ltp")),
            "Sell Avg Price": "",
            "Sell Price Slippage": "",
            "Sell Price Slippage %": "",
            "Sell Profit Slippage": "",
            "Peak LTP (hold)": "",
            "Peak Profit (pts)": "",
            "Peak Profit %": "",
            "Peak At": "",
            "Retries": rec.get("retry_count") or 0,
            "Retry Elapsed (s)": "",
            "First Attempt LTP": _fmt_num(rec.get("first_attempt_ltp")),
            "Exec Slippage %": "",
            "Buy Cost Increase": "",
            "Comment": "",
        }

        lots = rec.get("lots")
        try:
            lots_f = float(lots)
        except (TypeError, ValueError):
            lots_f = None

        # ---- BUY metrics ----
        if rec.get("buy_click_ts") and rec.get("buy_exec_ts"):
            bts = rec["buy_exec_ts"] - rec["buy_click_ts"]
            out["Buy Time Slippage (s)"] = _fmt_num(bts)
        ltp_click = rec.get("ltp_click")
        ltp_exec = rec.get("ltp_exec")
        if ltp_exec is None and rec.get("buy_avg_price") is not None:
            # No post-fill tick logged for this row - show the fill
            # price as LTP @ Exec, but slippage still keys off the
            # click LTP below.
            out["LTP @ Exec"] = _fmt_num(rec["buy_avg_price"])
        buy_avg = rec.get("buy_avg_price")
        if buy_avg is not None and ltp_click:
            pslip = float(buy_avg) - float(ltp_click)
            out["Buy Price Slippage"] = _fmt_num(pslip)
            out["Buy Price Slippage %"] = _fmt_num(
                pslip / float(ltp_click) * 100
            )
            if lots_f is not None:
                out["Buy Lot Cost Slippage"] = _fmt_num(pslip * lots_f)

        # ---- SELL metrics ----
        trig_ts = rec.get("sell_trigger_ts") or rec.get("resting_exit_placed_ts")
        if trig_ts and rec.get("sell_exec_ts") and not rec.get("imported"):
            sts = rec["sell_exec_ts"] - trig_ts
            out["Sell Time Slippage (s)"] = _fmt_num(sts)
        trig_ltp = rec.get("sell_trigger_ltp") or rec.get("resting_exit_price")
        sell_ltp = rec.get("sell_exec_ltp")
        # The actual sell fill: captured from the broker order book /
        # broker-side fill hooks. Imported rows only ever knew one
        # price - the broker's - so it doubles as the average.
        sell_avg = rec.get("sell_avg_price")
        if sell_avg is None and rec.get("imported"):
            sell_avg = sell_ltp
        if sell_avg is not None:
            out["Sell Avg Price"] = _fmt_num(sell_avg)
        if sell_avg is not None and trig_ltp:
            pslip = float(sell_avg) - float(trig_ltp)
            out["Sell Price Slippage"] = _fmt_num(pslip)
            out["Sell Price Slippage %"] = _fmt_num(
                pslip / float(trig_ltp) * 100
            )
            if lots_f is not None:
                out["Sell Profit Slippage"] = _fmt_num(pslip * lots_f)

        # ---- PEAK metrics (holding-window max from the dual-broker
        # tick capture: chain + manual + position files merged). The
        # highest LTP seen by either broker between the buy fill and
        # the sell fill - what the trade WAS worth at its best, vs
        # what the exit actually realized. Points x lots = rupees at
        # the peak; % keys off the buy average.
        if rec.get("buy_exec_ts"):
            try:
                from core.tick_peak import peak_for_trade
                end_ts = rec.get("sell_exec_ts") or datetime.now().timestamp()
                peak = peak_for_trade(
                    rec.get("symbol"),
                    float(rec["buy_exec_ts"]),
                    float(end_ts),
                    buy_avg=rec.get("buy_avg_price") or rec.get("ltp_exec"),
                    date_str=date_str,
                )
            except Exception as peak_error:
                logger.warning(f"Audit: peak lookup failed: {peak_error}")
                peak = None
            if peak:
                out["Peak LTP (hold)"] = _fmt_num(peak["peak_ltp"])
                if peak["peak_pts"] is not None:
                    out["Peak Profit (pts)"] = _fmt_num(peak["peak_pts"])
                    out["Peak Profit %"] = _fmt_num(peak["peak_pct"])
                out["Peak At"] = peak["peak_at"]

        # ---- RETRY metrics ----
        retries = rec.get("retries") or []
        if retries:
            first_retry_ts = retries[0].get("ts")
            end_ts = rec.get("buy_exec_ts") or retries[-1].get("ts")
            if first_retry_ts and end_ts:
                out["Retry Elapsed (s)"] = _fmt_num(end_ts - first_retry_ts)
        first_ltp = rec.get("first_attempt_ltp")
        if rec.get("retry_count") and first_ltp and ltp_exec is not None:
            diff = float(ltp_exec) - float(first_ltp)
            out["Exec Slippage %"] = _fmt_num(diff / float(first_ltp) * 100)
            if lots_f is not None:
                out["Buy Cost Increase"] = _fmt_num(diff * lots_f)

        if rec.get("imported"):
            out["Comment"] = rec.get("imported_note") or "Imported from broker order book"
        else:
            comment = self._comment_for(out)
            if rec.get("buy_success") is False:
                comment = (
                    f"BUY FAILED: {rec.get('buy_note') or 'order rejected'}"
                    + ("; " + comment if comment and comment != "Awaiting execution events" else "")
                )
            if rec.get("source") == "logs":
                if not comment or comment == "Awaiting execution events":
                    comment = "Reconstructed from session logs - no further data captured"
                out["Comment"] = "[From logs] " + comment
            else:
                out["Comment"] = comment or "Awaiting execution events"
        return out

    def snapshot(self):
        """All records for today as display-ready dicts (API + Excel)."""
        return self.build_snapshot_for_date(
            datetime.now().strftime("%Y-%m-%d")
        )

    # -------------------------------------------------------------
    # Per-date report builder
    # -------------------------------------------------------------
    def _load_date_file(self, date_str):
        """Raw records persisted on the given date (feature-era data)."""
        path = self._file_path(date_str)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"Audit log: could not load {path}: {e}")
        return []

    def enrich_today(self, orders, lot_size_lookup=None):
        """Apply today's broker order book to the CANONICAL live records
        (fills missing sell/buy fill prices, imports orders no hook
        captured) and persist - so the data survives restarts even if
        the Audit tab is never opened that day."""
        if not orders:
            return
        with self._lock:
            self._merge_broker_orders(self._records, orders, lot_size_lookup)
            self._persist()

    def build_snapshot_for_date(self, date_str, broker_orders=None,
                                lot_size_lookup=None, mode="LIVE"):
        """
        Report rows for a calendar date, merged from (best fidelity
        first, later sources deduplicated against earlier ones):
        1. today's live-hook records (or the day's persisted audit file
           for past dates),
        2. trades reconstructed from that date's session logs,
        3. today's broker order book (fills missing prices + imports
           trades the other sources never saw).

        `mode` picks the trade bucket: "LIVE" (default - real order
        paths: LIVE / SIMULATION / PLAYBACK) or "PAPER" - paper and
        live executions are never mixed in one report.
        """
        from core.audit_log_parser import parse_date_logs, canonical_symbol

        want_mode = str(mode or "LIVE").strip().upper()
        if want_mode not in ("LIVE", "PAPER"):
            want_mode = "LIVE"

        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            target = datetime.now().date()
        today = datetime.now().date()

        if target == today and broker_orders:
            # Merge into the canonical store (and persist) so broker
            # fill prices survive restarts.
            self.enrich_today(broker_orders, lot_size_lookup)

        with self._lock:
            if target == today:
                # Live records are identical to today's audit file.
                base = [dict(r) for r in self._records]
            else:
                base = [dict(r) for r in self._load_date_file(date_str)]

        # Reconstructed rows: skip trades the higher-fidelity sources
        # already captured. Matching is on the record's FIRST event
        # (click when present - so failed clicks dedupe too, not just
        # executed ones) and distinct rebuys of a contract sit far
        # apart.
        def _first_ts(r):
            return (r.get("buy_click_ts") or r.get("buy_exec_ts")
                    or r.get("sell_trigger_ts") or r.get("sell_exec_ts"))

        try:
            from core.audit_log_parser import retag_untagged
            log_recs = parse_date_logs(date_str)
            # Untagged persisted rows (app versions before mode
            # tagging) inherit the mode of their log-reconstructed
            # twin - otherwise yesterday's paper trades would default
            # into the LIVE view forever.
            retag_untagged(base, log_recs)
            for log_rec in log_recs:
                lr_ts = _first_ts(log_rec)
                match = next(
                    (
                        r for r in base
                        if lr_ts and _first_ts(r)
                        and abs(_first_ts(r) - lr_ts) < 10
                        and (
                            (str(r.get("token") or "")
                             and str(r.get("token"))
                             == str(log_rec.get("token") or ""))
                            or canonical_symbol(r.get("symbol"))
                            == canonical_symbol(log_rec.get("symbol"))
                        )
                    ),
                    None,
                )
                if match is not None:
                    # The live hooks don't flag broker rejections - fold
                    # the log-reconstructed failure into the live row.
                    if (not match.get("buy_exec_ts")
                            and log_rec.get("buy_success") is False):
                        match["buy_success"] = False
                        match["buy_note"] = (
                            log_rec.get("buy_note") or match.get("buy_note")
                        )
                    continue
                base.append(log_rec)
        except Exception as e:
            logger.error(f"Audit: log reconstruction failed for {date_str}: {e}")

        # Broker order book only serves the current day; enrich prices
        # and import orders no other source captured.
        if broker_orders and target == today:
            try:
                self._merge_broker_orders(base, broker_orders, lot_size_lookup)
            except Exception as e:
                logger.error(f"Audit: broker merge failed: {e}")

        # OPTIONS-ONLY GUARD: the Trade Execution Technical Report must
        # never pick up instruments other than options - the same rule
        # the Buy/Sell grids apply to positions. Catches rows already
        # persisted in older audit files (imported before the merge
        # guard existed) and anything the other sources ever slip in.
        # Unknown-symbol rows ("?" - app trade whose symbol was lost in
        # a restart) stay: they are the app's own trades, never foreign
        # instruments.
        def _foreign(r):
            s = str(r.get("symbol") or "").strip()
            return bool(s) and s != "?" and not _is_option_symbol(s)

        base = [r for r in base if not _foreign(r)]

        # Imported rows that an app record already covers (same event,
        # symbol + side + <15s) are duplicates persisted by earlier
        # builds whose canonical pairing missed the weekly format -
        # drop them so the grid shows one row per real trade.
        live_rows = [r for r in base if not r.get("imported")]
        base = [
            r for r in base
            if not r.get("imported")
            or not self._covered_by(
                live_rows,
                r.get("symbol"),
                "BUY" if r.get("buy_exec_ts") else "SELL",
                r.get("buy_exec_ts") or r.get("sell_exec_ts") or 0,
            )
        ]

        # PAPER / LIVE bucket filter: untagged rows (older persisted
        # files) count as LIVE - only real-order paths existed before
        # the tagging existed.
        base = [
            r for r in base
            if str(r.get("trade_mode") or "LIVE").strip().upper() == want_mode
        ]

        base.sort(
            key=lambda r: r.get("buy_click_ts") or r.get("buy_exec_ts")
            or r.get("sell_exec_ts") or 0
        )
        return [self._computed(r, date_str) for r in base]


# Module-level singleton shared by trade_logic and the adapters.
trade_audit = TradeAudit()
