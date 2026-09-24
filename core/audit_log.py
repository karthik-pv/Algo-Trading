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
import threading
import time
from datetime import datetime

from loguru import logger

from core.utils import DATA_DIR


AUDIT_DIR = os.path.join(DATA_DIR, "audit")


def _fmt_ts(ts):
    """Render an epoch timestamp as HH:MM:SS.mmm (or '' when missing)."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


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
    # Excel download so the two can never drift apart.
    COLUMNS = [
        "Symbol", "Lots", "SS",
        "Buy Click Time", "Buy Exec Time", "Buy Avg Price", "Buy Time Cost (s)",
        "LTP @ Click", "LTP @ Exec", "Buy Slippage", "Buy Slippage %",
        "Buy Lot Cost Slippage",
        "Sell Trigger Time", "Trigger Source", "Sell Exec Time", "Sell Time Cost (s)",
        "LTP @ Trigger", "LTP @ Sell Exec", "Sell Slippage", "Sell Slippage %",
        "Sell Profit Slippage",
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
        ("Trade", 3), ("Buy", 9), ("Sell", 9), ("Retry", 5), ("Comment", 1),
    ]

    # Display captions for the SECOND header row: the group band already
    # says BUY / SELL, so the per-column "Buy"/"Sell" prefixes are
    # stripped there. Data keys (COLUMNS) stay unique for the API.
    DISPLAY_NAMES = {
        "Buy Click Time": "Click Time",
        "Buy Exec Time": "Exec Time",
        "Buy Avg Price": "Avg Price",
        "Buy Time Cost (s)": "Time Cost (s)",
        "Buy Slippage": "Slippage",
        "Buy Slippage %": "Slippage %",
        "Buy Lot Cost Slippage": "Lot Cost Slippage",
        "Sell Trigger Time": "Trigger Time",
        "Sell Exec Time": "Exec Time",
        "Sell Time Cost (s)": "Time Cost (s)",
        "LTP @ Sell Exec": "LTP @ Exec",
        "Sell Slippage": "Slippage",
        "Sell Slippage %": "Slippage %",
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

    @staticmethod
    def _write_snapshot(snapshot):
        try:
            os.makedirs(AUDIT_DIR, exist_ok=True)
            path = TradeAudit._file_path()
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
        no sell execution yet. Falls back to a symbol-prefix match."""
        best = None
        for rec in reversed(self._records):
            if rec.get("sell_exec_ts"):
                continue
            if token and str(rec.get("token")) == str(token):
                return rec
            if not token and symbol:
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
        """True when a record already captured this order (same symbol,
        matching side, within a few seconds - distinct rebuys of the
        same contract sit minutes apart)."""
        for rec in records:
            if str(rec.get("symbol") or "") != str(symbol):
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
        IMPORTED rows (trades placed before the audit hooks existed)."""
        if not orders:
            return
        try:
            import pandas as pd
        except ImportError:
            return

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
            candidates = [
                rec for rec in records
                if str(rec.get("symbol") or "") == str(symbol)
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
                    # Broker average_price is the authoritative per-order
                    # fill - it overwrites the provisional grid price.
                    if best_rec.get("buy_avg_price") != order["price"]:
                        best_rec["buy_avg_price"] = order["price"]
                        best_rec["buy_avg_ts"] = ts_epoch
                else:
                    best_rec["sell_exec_ltp"] = order["price"]
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
                    open_buy["sell_success"] = True
                else:
                    records.append({
                        "imported": True,
                        "symbol": symbol,
                        "token": "",
                        "lots": _lots_for(symbol, order),
                        "strategy": "",
                        "sell_mode": "",
                        "sell_trigger_ts": ts_epoch,
                        "sell_trigger_ltp": order["price"],
                        "sell_trigger_source": "IMPORTED",
                        "sell_exec_ts": ts_epoch,
                        "sell_exec_ltp": order["price"],
                        "sell_success": True,
                    })

        for rec in records:
            if rec.get("imported") and not rec.get("imported_note"):
                rec["imported_note"] = (
                    "Imported from broker order book - no click-time capture "
                    "for this trade (placed before the audit feature/restart)"
                )

    # -------------------------------------------------------------
    # Report builder
    # -------------------------------------------------------------
    @staticmethod
    def _comment_for(out):
        """One-line execution commentary built from the computed metrics."""
        parts = []
        btc = out.get("Buy Time Cost (s)")
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
        bs_pct = out.get("Buy Slippage %")
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
        stc = out.get("Sell Time Cost (s)")
        if stc != "":
            parts.append(
                f"sell filled in {float(stc):.2f}s "
                f"({str(out.get('Trigger Source') or '').lower()})"
            )
        ss_pct = out.get("Sell Slippage %")
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

    def _computed(self, rec):
        """Return a display-ready copy with derived metrics + formatted
        times."""
        out = {
            "Symbol": rec.get("symbol", ""),
            "Lots": rec.get("lots", ""),
            "SS": self._ss_code(rec),
            "Buy Click Time": _fmt_ts(rec.get("buy_click_ts")),
            "Buy Exec Time": _fmt_ts(rec.get("buy_exec_ts")),
            "Buy Avg Price": _fmt_num(rec.get("buy_avg_price")),
            "Buy Time Cost (s)": "",
            "LTP @ Click": _fmt_num(rec.get("ltp_click")),
            "LTP @ Exec": _fmt_num(rec.get("ltp_exec")),
            "Buy Slippage": "",
            "Buy Slippage %": "",
            "Buy Lot Cost Slippage": "",
            "Sell Trigger Time": _fmt_ts(
                rec.get("sell_trigger_ts")
                or rec.get("resting_exit_placed_ts")
            ),
            "Trigger Source": rec.get("sell_trigger_source", ""),
            "Sell Exec Time": _fmt_ts(rec.get("sell_exec_ts")),
            "Sell Time Cost (s)": "",
            "LTP @ Trigger": _fmt_num(
                rec.get("sell_trigger_ltp")
                or rec.get("resting_exit_price")
            ),
            "LTP @ Sell Exec": _fmt_num(rec.get("sell_exec_ltp")),
            "Sell Slippage": "",
            "Sell Slippage %": "",
            "Sell Profit Slippage": "",
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
            btc = rec["buy_exec_ts"] - rec["buy_click_ts"]
            out["Buy Time Cost (s)"] = _fmt_num(btc)
        ltp_click = rec.get("ltp_click")
        ltp_exec = rec.get("ltp_exec")
        if ltp_exec is None:
            # No post-fill tick logged for this row - the ACTUAL fill
            # price (avg) is the truest LTP@Exec for slippage purposes.
            ltp_exec = rec.get("buy_avg_price")
            if ltp_exec is not None:
                out["LTP @ Exec"] = _fmt_num(ltp_exec)
        if ltp_click and ltp_exec is not None:
            slip = float(ltp_exec) - float(ltp_click)
            out["Buy Slippage"] = _fmt_num(slip)
            out["Buy Slippage %"] = _fmt_num(slip / float(ltp_click) * 100)
            if lots_f is not None:
                out["Buy Lot Cost Slippage"] = _fmt_num(slip * lots_f)

        # ---- SELL metrics ----
        trig_ts = rec.get("sell_trigger_ts") or rec.get("resting_exit_placed_ts")
        if trig_ts and rec.get("sell_exec_ts") and not rec.get("imported"):
            stc = rec["sell_exec_ts"] - trig_ts
            out["Sell Time Cost (s)"] = _fmt_num(stc)
        trig_ltp = rec.get("sell_trigger_ltp") or rec.get("resting_exit_price")
        sell_ltp = rec.get("sell_exec_ltp")
        if trig_ltp and sell_ltp is not None:
            sslip = float(sell_ltp) - float(trig_ltp)
            out["Sell Slippage"] = _fmt_num(sslip)
            out["Sell Slippage %"] = _fmt_num(sslip / float(trig_ltp) * 100)
            if lots_f is not None:
                out["Sell Profit Slippage"] = _fmt_num(sslip * lots_f)

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
                                lot_size_lookup=None):
        """
        Report rows for a calendar date, merged from (best fidelity
        first, later sources deduplicated against earlier ones):
        1. today's live-hook records (or the day's persisted audit file
           for past dates),
        2. trades reconstructed from that date's session logs,
        3. today's broker order book (fills missing prices + imports
           trades the other sources never saw).
        """
        from core.audit_log_parser import parse_date_logs, canonical_symbol

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
        # already captured (same symbol, buy execution within seconds -
        # distinct rebuys of a contract sit far apart).
        try:
            for log_rec in parse_date_logs(date_str):
                covered = any(
                    canonical_symbol(r.get("symbol"))
                    == canonical_symbol(log_rec.get("symbol"))
                    and r.get("buy_exec_ts") and log_rec.get("buy_exec_ts")
                    and abs(r["buy_exec_ts"] - log_rec["buy_exec_ts"]) < 10
                    for r in base
                )
                if not covered:
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

        base.sort(
            key=lambda r: r.get("buy_click_ts") or r.get("buy_exec_ts")
            or r.get("sell_exec_ts") or 0
        )
        return [self._computed(r) for r in base]


# Module-level singleton shared by trade_logic and the adapters.
trade_audit = TradeAudit()
