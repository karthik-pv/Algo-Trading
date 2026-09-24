import bisect
import calendar
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
from loguru import logger

from interface.broker_interface import BrokerInterface
from core.utils import (
    fetch_from_json,
    find_matching_object,
    find_matching_row_in_csv,
    load_json_with_retry,
    read_instrument_meta,
    get_exchange_for_underlying,
    resolve_data_path,
    INSTRUMENTS_SECTION_PREFIX,
)

_MSTOCK_REDUCED_FILE = resolve_data_path("mstock_instrument_list_reduced.json")
_MSTOCK_FULL_FILE = resolve_data_path("mstock_instrument_list.json")
_KITE_CSV_FILE = resolve_data_path("kite_instruments.csv")

# Sub-ticks emitted per CSV row (one second of recorded data).
_OHLC_FIELDS = ("open", "high", "low", "close")


class PlaybackAdapter(BrokerInterface):
    """Replays a recorded second-by-second OHLC series for a single instrument.

    Orders are executed internally against the replayed price, cash is
    tracked from Playback.json, and order timestamps follow the playback
    clock so the Orders tab shows authentic durations and PnL statistics.
    """

    def __init__(self):
        self._trader = None
        self._stop_event = None
        self._thread = None
        self._subscribed_tokens = set()
        self._prices = {}
        self._instruments = {}
        self._symbol_index = {}
        self._positions = {}
        self._orders = []
        self._order_counter = 0
        self._lookup_misses = set()
        self._lock = threading.Lock()

        self._config = self._load_config()
        self._tradingsymbol = str(self._config.get("INSTRUMENT") or "").strip().upper()
        if not self._tradingsymbol:
            raise RuntimeError("Playback.json must define INSTRUMENT (option tradingsymbol)")

        self._initial_cash = float(self._config.get("CASH_BALANCE", 100000) or 100000)
        self._cash = self._initial_cash
        self._speed = min(50.0, max(0.1, float(self._config.get("SPEED", 1) or 1)))
        # Gaps longer than this many market-seconds (e.g. the overnight
        # break) auto-pause playback instead of dwelling in real time.
        self._session_gap_seconds = float(self._config.get("SESSION_GAP_SECONDS", 300) or 300)

        self._rows, self._playback_day_open = self._load_price_file(
            self._config.get("PLAYBACK_PRICE_FILE", "PlaybackPrice.csv")
        )
        if not self._rows:
            raise RuntimeError(
                f"No usable rows found in the playback price file for {self._tradingsymbol}"
            )

        self._playback_instrument = self._resolve_playback_instrument()
        self._playback_token = str(self._playback_instrument["instrument_token"])
        self._register_instrument(self._playback_instrument)
        self._prices[self._playback_token] = self._playback_day_open

        # Playback engine state. The replay clock is anchored to the
        # wall clock so one second of market time takes one second of
        # real time at 1x (TradingView-style pacing).
        self._row_index = 0
        self._running = False
        self._paused = False
        self._engine_stop = None
        self._engine_lock = threading.Lock()
        self._current_playback_time = self._rows[0][0]
        self._anchor_ts = self._rows[0][0]
        self._anchor_wall = 0.0
        self._last_target = self._rows[0][0]
        self._last_index = None
        self._last_stage = None
        self._session_gap_ack = False

        logger.info(
            f"Playback adapter initialized for {self._tradingsymbol} "
            f"({len(self._rows)} seconds of data, cash {self._initial_cash:,.2f})"
        )

    # ------------------------------------------------------------------
    # Configuration / data loading
    # ------------------------------------------------------------------
    @staticmethod
    def _load_config():
        playback_json_path = resolve_data_path("Playback.json")
        try:
            return load_json_with_retry(playback_json_path)
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Failed to load Playback.json: {error}")

    @staticmethod
    def _load_price_file(file_name):
        path = resolve_data_path(str(file_name))
        if not os.path.exists(path):
            raise RuntimeError(f"Playback price file not found: {path}")

        try:
            frame = pd.read_csv(path)
        except Exception as error:
            raise RuntimeError(f"Failed to read playback price file '{path}': {error}")

        missing = {"time", "open", "high", "low", "close"} - set(frame.columns)
        if missing:
            raise RuntimeError(
                f"Playback price file missing columns: {', '.join(sorted(missing))}"
            )

        frame["time"] = pd.to_datetime(frame["time"], errors="raise", utc=True)
        frame["time"] = frame["time"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        frame = frame.sort_values("time").reset_index(drop=True)

        records = frame.to_dict(orient="records")
        rows = [
            (
                record["time"].to_pydatetime(),
                float(record["open"]),
                float(record["high"]),
                float(record["low"]),
                float(record["close"]),
            )
            for record in records
        ]
        day_open = rows[0][1] if rows else 0.0
        return rows, day_open

    # ------------------------------------------------------------------
    # Instrument resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _stable_token(symbol):
        digest = hashlib.sha1(str(symbol).encode("utf-8")).hexdigest()
        return str(int(digest[:12], 16))

    def _lookup_instrument(self, tradingsymbol):
        """Resolve real instrument details from the broker instrument lists."""
        symbol = str(tradingsymbol)
        if symbol in self._lookup_misses:
            return None
        broker = str(fetch_from_json("appconfig.json", "BROKER") or "MSTOCK").upper()
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_exchange = get_exchange_for_underlying(self._underlying()) or "NFO"

        try:
            if broker == "KITE":
                row = find_matching_row_in_csv(
                    _KITE_CSV_FILE, "tradingsymbol", symbol
                )
                if row:
                    return {
                        "tradingsymbol": row.get("tradingsymbol") or symbol,
                        "instrument_token": str(row.get("instrument_token") or self._stable_token(symbol)),
                        "lot_size": row.get("lot_size") or 1,
                        "expiry": row.get("expiry"),
                        "strike": row.get("strike"),
                        "exchange": row.get("exchange") or default_exchange,
                    }

            else:
                # The reduced file carries {"meta": ..., "instruments": [...]};
                # the full master file is a plain top-level array.
                reduced = find_matching_object(
                    _MSTOCK_REDUCED_FILE,
                    "name",
                    symbol,
                    prefix=INSTRUMENTS_SECTION_PREFIX,
                )
                if reduced:
                    return {
                        "tradingsymbol": reduced.get("name") or symbol,
                        "instrument_token": str(reduced.get("token") or self._stable_token(symbol)),
                        "lot_size": reduced.get("lotsize") or 1,
                        "expiry": reduced.get("expiry"),
                        "strike": reduced.get("strike"),
                        "exchange": reduced.get("exch_seg") or default_exchange,
                    }
                item = find_matching_object(
                    _MSTOCK_FULL_FILE, "name", symbol
                )
                if item:
                    return {
                        "tradingsymbol": item.get("name") or symbol,
                        "instrument_token": str(item.get("token") or self._stable_token(symbol)),
                        "lot_size": item.get("lotsize") or 1,
                        "expiry": item.get("expiry"),
                        "strike": item.get("strike"),
                        "exchange": item.get("exch_seg") or default_exchange,
                    }
        except Exception as error:
            logger.warning(f"Instrument lookup failed for {symbol}: {error}")

        # Remember misses so repeated lookups (e.g. table refreshes) do not
        # rescan the full multi-MB instrument list every time.
        self._lookup_misses.add(symbol)
        return None

    def _default_price_for_symbol(self, symbol):
        text = str(symbol).upper()
        if text.endswith(("CE", "PE")):
            return 100.0
        if text == self._tradingsymbol:
            return self._playback_day_open
        if "FUT" in text:
            fallback_key = "SENSEX_FALLBACK_LTP" if self._underlying() == "SENSEX" else "NIFTY_FALLBACK_LTP"
            try:
                return float(fetch_from_json("appconfig.json", fallback_key))
            except (TypeError, ValueError):
                return 25000.0
        return 100.0

    @staticmethod
    def _underlying():
        return str(fetch_from_json("appconfig.json", "UNDERLYING") or "NIFTY").upper()

    @staticmethod
    def _parse_strike_from_symbol(symbol):
        """Best-effort strike parse for symbols not found in the lists.

        The trailing digit run before CE/PE is yy + month + day + strike,
        so the strike is the last 5 digits for NIFTY, 6 for SENSEX and
        4 for other underlyings.
        """
        text = str(symbol).upper()
        match = re.search(r"(\d+)(CE|PE)$", text)
        if not match:
            return None
        digits = match.group(1)
        underlying = str(fetch_from_json("appconfig.json", "UNDERLYING") or "NIFTY").upper()
        width = 6 if underlying == "SENSEX" else (5 if underlying == "NIFTY" else 4)
        if len(digits) <= width:
            return None
        try:
            return int(digits[-width:])
        except ValueError:
            return None

    def _resolve_playback_instrument(self):
        details = self._lookup_instrument(self._tradingsymbol)
        if details is None:
            configured_lot = self._config.get("LOT_SIZE") or 1
            logger.warning(
                f"Playback instrument {self._tradingsymbol} not found in the instrument "
                f"lists; synthesizing with lot size {configured_lot}"
            )
            details = {
                "tradingsymbol": self._tradingsymbol,
                "instrument_token": self._stable_token(self._tradingsymbol),
                "lot_size": configured_lot,
                "expiry": None,
                "strike": self._parse_strike_from_symbol(self._tradingsymbol),
                "exchange": get_exchange_for_underlying(self._underlying()) or "NFO",
            }
        return details

    def _register_instrument(self, instrument):
        token = str(instrument["instrument_token"])
        instrument.setdefault("last_price", self._default_price_for_symbol(instrument["tradingsymbol"]))
        instrument.setdefault("underlying", self._underlying())
        self._instruments[token] = instrument
        self._symbol_index[str(instrument["tradingsymbol"]).upper()] = token
        self._prices.setdefault(token, instrument["last_price"])
        return instrument

    def _ensure_instrument(self, tradingsymbol, instrument_token=None):
        symbol = str(tradingsymbol)
        if symbol.upper() in self._symbol_index:
            return self._instruments[self._symbol_index[symbol.upper()]]

        token = str(instrument_token or self._stable_token(symbol))
        if token in self._instruments:
            return self._instruments[token]

        details = self._lookup_instrument(symbol)
        if details is not None and str(details["instrument_token"]) != token:
            token = str(details["instrument_token"])
        if details is None:
            details = {
                "tradingsymbol": symbol,
                "instrument_token": token,
                "lot_size": self._config.get("LOT_SIZE") or 1,
                "expiry": None,
                "exchange": get_exchange_for_underlying(self._underlying()) or "NFO",
            }
        return self._register_instrument(details)

    # ------------------------------------------------------------------
    # Playback engine
    # ------------------------------------------------------------------
    def _emit_stage_price(self, row_index, stage):
        row_time, open_price, high_price, low_price, close_price = self._rows[row_index]
        price = (open_price, high_price, low_price, close_price)[stage]
        self._current_playback_time = row_time

        instrument = self._instruments.get(self._playback_token)
        if instrument:
            instrument["last_price"] = price
        self._prices[self._playback_token] = price

        if self._trader:
            self._trader.set_latest_price(
                self._playback_token,
                self._tradingsymbol,
                price,
                self._playback_day_open,
            )

    def _reanchor(self):
        """Re-base the replay clock on the current position.

        Called on resume and on speed changes so the replayed time does
        not jump ahead by the time spent paused or the speed delta.
        """
        if self._last_target is not None:
            self._anchor_ts = self._last_target
        elif self._row_index > 0:
            self._anchor_ts = self._rows[self._row_index - 1][0]
        else:
            self._anchor_ts = self._rows[0][0]
        self._anchor_wall = time.monotonic()

    def _emit_status(self, success, message):
        if self._trader and getattr(self._trader, "frontend_data_socket", None):
            try:
                self._trader.frontend_data_socket.emit(
                    "status_message", {"success": success, "message": message}
                )
            except Exception as error:
                logger.debug(f"Could not emit playback status message: {error}")

    def _emit_playback_status_event(self):
        if self._trader and getattr(self._trader, "frontend_data_socket", None):
            try:
                self._trader.frontend_data_socket.emit(
                    "playback_status",
                    self.get_playback_status()
                )
            except Exception as error:
                logger.debug(f"Could not emit playback status: {error}")

    def _emit_playback_time(self):
        if self._trader and getattr(self._trader, "frontend_data_socket", None):
            try:
                self._trader.frontend_data_socket.emit(
                    "playback_time",
                    {"time": self._current_playback_time.strftime("%Y-%m-%d %H:%M:%S")},
                )
            except Exception as error:
                logger.debug(f"Could not emit playback time: {error}")

    def _playback_loop(self, engine_stop):
        """Timestamp-anchored replay.

        The replayed market time advances at (real elapsed time x speed),
        exactly like TradingView's 1x: one second of market time takes
        one second of real time at 1x, and gaps in the recorded data
        consume real time instead of being skipped instantly. Each
        recorded second plays its O/H/L/C sub-ticks in order as the
        replay clock crosses the row's second.
        """
        logger.info("Playback engine started (timestamp-anchored pacing)")
        rows = self._rows
        try:
            while not engine_stop.is_set() and self._running:
                if self._paused:
                    engine_stop.wait(0.2)
                    continue

                target = self._anchor_ts + timedelta(
                    seconds=(time.monotonic() - self._anchor_wall) * self._speed
                )
                self._last_target = target

                # Advance to the last row whose timestamp is due.
                while self._row_index < len(rows) and rows[self._row_index][0] <= target:
                    self._row_index += 1

                if self._row_index >= len(rows):
                    logger.info("Playback reached the end of the price file")
                    self._running = False
                    self._emit_status(True, "Playback finished - no more data in the price file")
                    self._emit_playback_status_event()
                    break

                current = self._row_index - 1
                row_time = rows[current][0]
                fraction = max(0.0, (target - row_time).total_seconds())
                stage = min(len(_OHLC_FIELDS) - 1, int(fraction * len(_OHLC_FIELDS)))

                if (current, stage) != (self._last_index, self._last_stage):
                    row_changed = current != self._last_index
                    self._last_index, self._last_stage = current, stage
                    try:
                        self._emit_stage_price(current, stage)
                    except Exception as error:
                        logger.exception(f"Playback tick failed: {error}")
                    if row_changed:
                        # One clock announcement per replayed second.
                        self._emit_playback_time()

                # Session-boundary gap (e.g. overnight): pause instead of
                # dwelling for hours. The user can jump the time picker or
                # resume to wait the gap out in real time. Trigger only
                # after the current row's second has fully played out.
                next_time = rows[self._row_index][0]
                gap_seconds = (next_time - row_time).total_seconds()
                if (
                    gap_seconds > self._session_gap_seconds
                    and fraction >= 1.0
                    and not self._session_gap_ack
                ):
                    self._session_gap_ack = True
                    self._paused = True
                    logger.info(
                        f"Playback paused at a session gap - next data at {next_time:%Y-%m-%d %H:%M:%S}"
                    )
                    self._emit_status(
                        True,
                        f"Playback reached a session gap - next data at "
                        f"{next_time:%Y-%m-%d %H:%M:%S}. Jump the time picker to skip ahead, "
                        f"or press resume to wait it out."
                    )
                    self._emit_playback_status_event()
                    continue

                engine_stop.wait(0.02)
        finally:
            logger.info("Playback engine stopped")

    def start_playback(self, start_datetime):
        """Seek the CSV to the requested time and (re)start the engine.

        Existing positions, orders and cash are preserved; only the
        replay position jumps to the requested time.
        """
        if isinstance(start_datetime, str):
            target = datetime.fromisoformat(start_datetime.strip())
        elif isinstance(start_datetime, datetime):
            target = start_datetime
        else:
            raise ValueError("start_datetime must be an ISO string or datetime")

        first_time = self._rows[0][0]
        last_time = self._rows[-1][0]
        if target < first_time or target > last_time:
            raise ValueError(
                f"Playback time must be between {first_time:%Y-%m-%d %H:%M:%S} "
                f"and {last_time:%Y-%m-%d %H:%M:%S}"
            )

        with self._engine_lock:
            row_times = [row[0] for row in self._rows]
            index = bisect.bisect_left(row_times, target)
            if index >= len(self._rows):
                raise ValueError(f"No playback data available after {target:%Y-%m-%d %H:%M:%S}")

            if self._engine_stop is not None:
                self._engine_stop.set()

            self._row_index = index
            self._paused = False
            self._running = True
            self._session_gap_ack = False
            self._last_index = None
            self._last_stage = None
            self._anchor_ts = self._rows[index][0]
            self._anchor_wall = time.monotonic()
            self._last_target = self._anchor_ts
            self._current_playback_time = self._anchor_ts

            engine_stop = threading.Event()
            self._engine_stop = engine_stop
            self._thread = threading.Thread(
                target=self._playback_loop,
                args=(engine_stop,),
                daemon=True,
                name="playback-engine",
            )
            self._thread.start()

        actual_start = self._rows[index][0]
        logger.info(f"Playback started from {actual_start:%Y-%m-%d %H:%M:%S} at {self._speed}x speed")
        return actual_start

    def pause_playback(self):
        if self._running:
            self._paused = True
            logger.info("Playback paused")

    def resume_playback(self):
        if self._running and self._paused:
            self._reanchor()
            self._paused = False
            logger.info("Playback resumed")

    def stop_playback(self):
        self._running = False
        self._paused = False
        if self._engine_stop is not None:
            self._engine_stop.set()
        logger.info("Playback stopped by user")

    def set_playback_speed(self, speed):
        try:
            speed_value = float(speed)
        except (TypeError, ValueError):
            raise ValueError("Playback speed must be numeric")
        self._speed = min(50.0, max(0.1, speed_value))
        if self._running and not self._paused:
            self._reanchor()
        logger.info(f"Playback speed set to {self._speed}x")

    def get_playback_status(self):
        return {
            "instrument": self._tradingsymbol,
            "running": self._running,
            "paused": self._paused,
            "speed": self._speed,
            "current_time": self._current_playback_time.strftime("%Y-%m-%d %H:%M:%S"),
            "start_bound": self._rows[0][0].strftime("%Y-%m-%d %H:%M:%S"),
            "end_bound": self._rows[-1][0].strftime("%Y-%m-%d %H:%M:%S"),
        }

    def get_playback_date(self):
        return self._rows[0][0].strftime("%Y-%m-%d")

    def get_playback_instrument_name(self):
        return self._tradingsymbol

    def get_playback_strike(self):
        """Return the playback instrument's strike (from the instrument list)."""
        try:
            return int(self._playback_instrument.get("strike"))
        except (TypeError, ValueError):
            return None

    def get_current_playback_price(self):
        return self._prices.get(self._playback_token, self._playback_day_open)

    # ------------------------------------------------------------------
    # Broker interface: funds / orders / positions
    # ------------------------------------------------------------------
    def fetch_all_orders(self):
        with self._lock:
            return [dict(order) for order in self._orders]

    def fetch_all_pending_orders(self):
        logger.debug("Playback executes orders instantly; no pending orders exist")
        return []

    def cancel_order(self, order_id):
        logger.debug(f"Playback has no pending orders; nothing to cancel for {order_id}")
        return False

    def cancel_all_pending_orders(self):
        logger.debug("Playback executes orders instantly; no pending orders to cancel")
        return 0

    def manual_refresh_positions(self):
        logger.info("Refreshing open positions in playback...")
        if self._trader:
            self._trader.refresh_open_pos_buy_price()

    def fetch_all_positions(self):
        with self._lock:
            return [dict(position) for position in self._positions.values() if position["quantity"] > 0]

    def fetch_all_trades(self):
        return [order for order in self.fetch_all_orders() if order["order_status"] == "COMPLETE"]

    def fetch_fund_summary(self):
        with self._lock:
            return {
                "cash_balance": self._cash,
                "utilized": max(0.0, self._initial_cash - self._cash),
            }

    def fetch_day_start_cash(self):
        return self._initial_cash

    def fetch_instruments_from_json(self):
        return list(self._instruments.values())

    def fetch_all_instruments(self):
        return list(self._instruments.values())

    def fetch_instrument_quote(self, exchange, instrument):
        instrument_data = self._ensure_instrument(instrument)
        token = str(instrument_data["instrument_token"])
        price = self._prices.get(token, instrument_data["last_price"])
        return {f"{exchange}:{instrument}": {"last_price": price, "ohlc": {"open": price}}}

    def get_ltp(self, symbol_name):
        return self.get_quote(symbol_name)[0]

    def get_quote(self, tradingsymbol):
        instrument = self._ensure_instrument(tradingsymbol)
        token = str(instrument["instrument_token"])
        price = self._prices.get(token, instrument.get("last_price", 0))
        return [price, price, price]

    def get_quotes_batch(self, symbols):
        return {str(symbol): self.get_quote(symbol) for symbol in symbols}

    def fetch_expiries(self, underlying):
        return []

    def get_instrument_details(self, tradingsymbol):
        return self._ensure_instrument(tradingsymbol)

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------
    def _record_order(self, side, symbol, token, quantity, price, exchange, lotsize):
        self._order_counter += 1
        order = {
            "order_id": f"PB-{self._order_counter:06d}",
            "timestamp": self._current_playback_time,
            "tradingsymbol": symbol,
            "transaction_type": side,
            "quantity": int(quantity),
            "filled_quantity": int(quantity),
            "average_price": float(price),
            "order_status": "COMPLETE",
            "instrument_token": str(token),
            "exchange": exchange,
            "lot_size": int(lotsize),
        }
        self._orders.append(order)
        return order

    def _execute(self, side, trading_symbol, instrument_token, quantity, exchange, ltp):
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError("Playback order quantity must be positive")

        instrument = self.get_instrument_details(trading_symbol)
        token = str(instrument_token or instrument["instrument_token"])
        price = float(ltp or self._prices.get(token) or instrument.get("last_price") or 0)
        if price <= 0:
            raise ValueError("No playback price available yet; start playback before trading")

        lotsize = int(instrument.get("lot_size", 1) or 1)
        order_cost = price * quantity * lotsize

        with self._lock:
            position = self._positions.get(token, {
                "tradingsymbol": trading_symbol,
                "instrument_token": token,
                "exchange": exchange,
                "quantity": 0,
                "average_price": 0.0,
                "lotsize": lotsize,
            })
            current_quantity = int(position["quantity"])

            if side == "BUY":
                if order_cost > self._cash:
                    raise ValueError(
                        f"FUND LIMIT INSUFFICIENT: need {order_cost:,.2f} for "
                        f"{quantity} lot(s) of {trading_symbol} at {price:,.2f}, "
                        f"available cash {self._cash:,.2f}"
                    )
                new_quantity = current_quantity + quantity
                position["average_price"] = (
                    (position["average_price"] * current_quantity) + (price * quantity)
                ) / new_quantity
                self._cash -= order_cost
            else:
                if quantity > current_quantity:
                    raise ValueError("Playback sell quantity exceeds open position")
                new_quantity = current_quantity - quantity
                self._cash += order_cost

            position["quantity"] = new_quantity
            position["lotsize"] = lotsize
            self._positions[token] = position
            order = self._record_order(side, trading_symbol, token, quantity, price, exchange, lotsize)
            if new_quantity == 0:
                del self._positions[token]

        if self._trader:
            self._trader.refresh_open_pos_buy_price()

        self._emit_status(True, f"Playback {side} executed: {quantity} lot(s) of {trading_symbol} at {price:,.2f}")

        if self._trader and getattr(self._trader, "frontend_data_socket", None):
            try:
                self._trader.frontend_data_socket.emit(
                    f"{side.lower()}_order_result",
                    {"success": True, "tradingsymbol": trading_symbol, "lots": quantity},
                )
            except Exception as error:
                logger.debug(f"Could not emit {side.lower()} order result: {error}")

        return order["order_id"]

    def buy_units(
        self,
        trading_symbol,
        instrument_token,
        quantity,
        exchange,
        ltp,
        mode="",
        sell_mode="",
        target_profit=0,
        strategy="",
        target_profit_pct=None,
    ):
        return self._execute("BUY", trading_symbol, instrument_token, quantity, exchange, ltp)

    def buy_sell_units(
        self,
        trading_symbol,
        instrument_token,
        quantity,
        exchange,
        ltp,
        mode="",
        sell_mode="",
        target_profit=0,
        strategy="",
        target_profit_pct=None,
    ):
        return self.buy_units(
            trading_symbol,
            instrument_token,
            quantity,
            exchange,
            ltp,
            mode,
            sell_mode,
            target_profit,
            strategy,
        )

    def sell_units(self, trading_symbol, instrument_token, quantity, exchange, ltp, position_key=""):
        return self._execute("SELL", trading_symbol, instrument_token, quantity, exchange, ltp)

    # ------------------------------------------------------------------
    # Subscriptions / lifecycle
    # ------------------------------------------------------------------
    def unsubscribe_from_all(self, instruments):
        self._subscribed_tokens.difference_update(str(token) for token in instruments)

    def subscribe_to_all(self, instruments):
        for token in instruments:
            token = str(token)
            self._subscribed_tokens.add(token)
            if token not in self._instruments:
                self._ensure_instrument(token, token)

    def start_socket_connection(self, shutdown_event, trader_instance):
        self._trader = trader_instance
        self._stop_event = shutdown_event

        # Seed the trader's fund summary so option lots are sized from the
        # Playback.json cash balance right from the first table build.
        try:
            trader_instance.update_fund_summary(self.fetch_fund_summary())
        except Exception as error:
            logger.warning(f"Could not seed fund summary for playback: {error}")

        trader_instance.setup_woc_subscriptions()
        logger.info("Playback adapter ready - waiting for a playback start request")

    def get_instrument_meta(self):
        """
        Playback derives meta from the real instrument lists when
        available: the mstock reduced file's persisted meta block is
        used as-is; otherwise values are synthesized from the replayed
        instrument / underlying (same shape as the live brokers).
        """
        underlying = self._underlying()

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        persisted_meta = read_instrument_meta(_MSTOCK_REDUCED_FILE)
        if persisted_meta.get("underlying") == underlying and persisted_meta.get("near_month_future_token"):
            return persisted_meta

        details = self._lookup_instrument(self._tradingsymbol) or {}
        expiry_raw = details.get("expiry")
        near_option_expiry = None
        if expiry_raw:
            try:
                near_option_expiry = pd.to_datetime(str(expiry_raw)).date()
            except Exception:
                near_option_expiry = None

        if near_option_expiry is None:
            # No real expiry available; keep the replay self-consistent
            # by anchoring the option expiry to the playback clock's day.
            near_option_expiry = self._current_playback_time.date()

        near_future_month = near_option_expiry.replace(day=1)
        return {
            "underlying": underlying,
            "exchange": get_exchange_for_underlying(underlying) or str(details.get("exchange") or "NFO"),
            "near_month_future_token": (
                f"{underlying}{near_future_month.year % 100:02d}"
                f"{near_future_month.strftime('%b').upper()}FUT"
            ),
            "near_option_expiry": near_option_expiry.isoformat(),
            "expire_together": False,
            "strike_interval": 100,
            "generated_on": self._current_playback_time.date().isoformat(),
        }

    def format_option_symbol(self, underlying, expiry, strike, call_or_put):
        try:
            yy = expiry.year % 100
            expire_together = self.get_instrument_meta().get("expire_together")
            if expire_together:
                month_char = calendar.month_name[expiry.month][:3].upper()
                dd_str = ""
            else:
                month_char = str(expiry.month)
                dd_str = f"{expiry.day:02d}"
            return f"{underlying}{yy}{month_char}{dd_str}{int(strike)}{call_or_put.upper()}"
        except Exception as error:
            logger.error(f"Error in playback format_option_symbol: {error}")
            return False

    def dev_start(self):
        return None

    def prod_start(self, request_token=None):
        return None

    def download_instrument_list(self, exchange=None, underlying=None):
        return None
