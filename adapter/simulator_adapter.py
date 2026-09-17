import hashlib
import json
import os
import random
import threading
import time
from datetime import date, datetime, timedelta

import holidays
from loguru import logger

from interface.broker_interface import BrokerInterface
from utils import fetch_from_json, load_json_with_retry, get_exchange_for_underlying

india_holidays = holidays.India()


class SimulatorAdapter(BrokerInterface):
    """Configuration-driven broker used for non-market-hours testing."""

    def __init__(self):
        self._trader = None
        self._stop_event = None
        self._thread = None
        self._subscribed_tokens = set()
        self._prices = {}
        self._instruments = {}
        self._positions = {}
        self._orders = []
        self._order_counter = 0
        self._lock = threading.Lock()

        settings = self._load_settings()
        self._settings = settings
        self._underlying, self._underlying_price_config = self._underlying_config()
        self._random = random.Random(settings.get("RANDOM_SEED", 42))
        self._tick_interval = max(0.05, float(settings.get("TICK_INTERVAL_SECONDS", 1)))
        self._slippage = float(settings.get("SLIPPAGE_POINTS", 0))
        self._cash = float(settings.get("INITIAL_CASH", 100000))

    @staticmethod
    def _load_settings():
        try:
            simulation_json_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "simulation.json",
            )
            return load_json_with_retry(simulation_json_path)
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"Failed to load simulation.json, using simulator defaults: {error}")
            return {}

    @staticmethod
    def _stable_token(symbol):
        digest = hashlib.sha1(symbol.encode("utf-8")).hexdigest()
        return str(int(digest[:12], 16))

    def _underlying_config(self):
        underlying = str(fetch_from_json("constants.json", "UNDERLYING") or "NIFTY").upper()
        defaults = {
            "NIFTY": {"INITIAL_PRICE": 25000, "MIN_PRICE": 24800, "MAX_PRICE": 25200, "VOLATILITY_POINTS": 12},
            "SENSEX": {"INITIAL_PRICE": 82000, "MIN_PRICE": 81500, "MAX_PRICE": 82500, "VOLATILITY_POINTS": 30},
        }
        return underlying, {**defaults.get(underlying, defaults["NIFTY"]), **self._settings.get(underlying, {})}

    def _next_weekly_expiry(self):
        """
        Algorithmic stand-in for the instrument-file expiry: the next
        weekly expiry day for the underlying (NIFTY=Tuesday,
        SENSEX=Thursday; CRUDEOIL family approximated with the monthly
        19th), skipping Indian holidays.
        """
        underlying = self._underlying
        today = date.today()

        if underlying in ("CRUDEOIL", "CRUDEOILM"):
            candidate = date(today.year, today.month, 19)
            if candidate < today:
                month, year = (today.month + 1, today.year) if today.month < 12 else (1, today.year + 1)
                candidate = date(year, month, 19)
            while candidate in india_holidays:
                candidate -= timedelta(days=1)
            return candidate

        target_weekday = 3 if underlying == "SENSEX" else 1  # Thursday / Tuesday
        candidate = today + timedelta(days=(target_weekday - today.weekday()) % 7)
        while candidate in india_holidays or candidate < today:
            candidate += timedelta(days=7)
        return candidate

    def get_instrument_meta(self):
        """
        Synthetic instrument meta: no instrument file exists in
        simulation, so every value is derived from the configured
        underlying (algorithmic expiry, exchange via the shared map).
        """
        underlying = self._underlying
        near_option_expiry = self._next_weekly_expiry()
        near_future_month = near_option_expiry.replace(day=1)
        near_month_future_token = (
            f"{underlying}{near_future_month.year % 100:02d}"
            f"{near_future_month.strftime('%b').upper()}FUT"
        )
        return {
            "underlying": underlying,
            "exchange": get_exchange_for_underlying(underlying) or "NFO",
            "near_month_future_token": near_month_future_token,
            "near_option_expiry": near_option_expiry.isoformat(),
            "expire_together": False,
            "strike_interval": 100 if underlying == "SENSEX" else 50,
            "generated_on": date.today().isoformat(),
        }

    def _ensure_instrument(self, tradingsymbol, instrument_token=None):
        symbol = str(tradingsymbol)
        token = str(instrument_token or self._stable_token(symbol))
        if token in self._instruments:
            return self._instruments[token]

        underlying = self._underlying
        underlying_config = self._underlying_price_config
        is_option = symbol.upper().endswith(("CE", "PE"))
        configured = self._settings.get("OPTION_DEFAULT", {}) if is_option else underlying_config
        default_price = float(configured.get("INITIAL_PRICE", 100 if is_option else underlying_config["INITIAL_PRICE"]))
        expiry = self._configured_expiry() if is_option else None
        self._instruments[token] = {
            "tradingsymbol": symbol,
            "instrument_token": token,
            "exchange": get_exchange_for_underlying(underlying) or "NFO",
            "lot_size": int(self._settings.get("LOT_SIZE", 1)),
            "expiry": expiry,
            "last_price": default_price,
            "underlying": underlying,
        }
        self._prices[token] = default_price
        return self._instruments[token]

    def _configured_expiry(self):
        meta = self.get_instrument_meta()
        try:
            expiry = date.fromisoformat(str(meta["near_option_expiry"]))
            return expiry.strftime("%d%b%Y").upper()
        except (TypeError, ValueError):
            return None

    def fetch_all_orders(self):
        with self._lock:
            return list(self._orders)

    def fetch_all_pending_orders(self):
        logger.debug("Simulation executes orders instantly; no pending orders exist")
        return []

    def cancel_order(self, order_id):
        logger.debug(f"Simulation has no pending orders; nothing to cancel for {order_id}")
        return False

    def manual_refresh_positions(self):
        logger.info("Refreshing open positions in simulator...")
        self._trader.refresh_open_pos_buy_price()

    def fetch_instruments_from_json(self):
        return list(self._instruments.values())

    def fetch_all_instruments(self):
        return list(self._instruments.values())

    def fetch_all_positions(self):
        with self._lock:
            return [dict(position) for position in self._positions.values() if position["quantity"] > 0]

    def fetch_all_trades(self):
        return [order for order in self.fetch_all_orders() if order["order_status"] == "COMPLETE"]

    def fetch_fund_summary(self):
        with self._lock:
            return {"cash_balance": self._cash}

    def fetch_instrument_quote(self, exchange, instrument):
        instrument_data = self._ensure_instrument(instrument)
        price = self._prices[instrument_data["instrument_token"]]
        return {f"{exchange}:{instrument}": {"last_price": price, "ohlc": {"open": price}}}

    def get_ltp(self, symbol_name):
        return self.get_quote(symbol_name)[0]

    def get_quote(self, tradingsymbol):
        instrument = self._ensure_instrument(tradingsymbol)
        price = self._prices[instrument["instrument_token"]]
        return [price, price, price]

    def get_quotes_batch(self, symbols):
        return {symbol: self.get_quote(symbol) for symbol in symbols}

    def fetch_expiries(self, underlying):
        return []

    def get_instrument_details(self, tradingsymbol):
        configured_instruments = self._settings.get("INSTRUMENTS", {})
        configured = configured_instruments.get(str(tradingsymbol), {})
        instrument = self._ensure_instrument(
            tradingsymbol,
            configured.get("instrument_token")
        )
        instrument.update({key: value for key, value in configured.items() if key != "instrument_token"})
        return instrument

    def _record_order(self, side, symbol, token, quantity, price, exchange):
        self._order_counter += 1
        order = {
            "order_id": f"SIM-{self._order_counter:06d}",
            "timestamp": time.strftime("%Y-%b-%d %H:%M:%S"),
            "tradingsymbol": symbol,
            "transaction_type": side,
            "quantity": int(quantity),
            "filled_quantity": int(quantity),
            "average_price": float(price),
            "order_status": "COMPLETE",
            "instrument_token": str(token),
            "exchange": exchange,
        }
        self._orders.append(order)
        return order

    def _execute(self, side, trading_symbol, instrument_token, quantity, exchange, ltp):
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError("Simulation order quantity must be positive")

        instrument = self.get_instrument_details(trading_symbol)
        token = str(instrument_token or instrument["instrument_token"])
        price = float(ltp or self._prices.get(token) or instrument["last_price"])
        price += self._slippage if side == "BUY" else -self._slippage
        lotsize = int(instrument.get("lot_size", 1) or 1)

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
                new_quantity = current_quantity + quantity
                position["average_price"] = (
                    (position["average_price"] * current_quantity) + (price * quantity)
                ) / new_quantity
                self._cash -= price * quantity * lotsize
            else:
                if quantity > current_quantity:
                    raise ValueError("Simulation sell quantity exceeds open position")
                new_quantity = current_quantity - quantity
                self._cash += price * quantity * lotsize

            position["quantity"] = new_quantity
            self._positions[token] = position
            order = self._record_order(side, trading_symbol, token, quantity, price, exchange)
            if new_quantity == 0:
                del self._positions[token]

        self._trader.refresh_open_pos_buy_price()
        self._trader.frontend_data_socket.emit("status_message", {
            "success": True,
            "message": f"Simulation {side} executed at {price:.2f}",
        })
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
        )

    def sell_units(self, trading_symbol, instrument_token, quantity, exchange, ltp):
        return self._execute("SELL", trading_symbol, instrument_token, quantity, exchange, ltp)

    def unsubscribe_from_all(self, instruments):
        self._subscribed_tokens.difference_update(str(token) for token in instruments)

    def subscribe_to_all(self, instruments):
        for token in instruments:
            token = str(token)
            self._subscribed_tokens.add(token)
            if token not in self._instruments:
                self._ensure_instrument(token, token)

    def _tick_loop(self):
        while not self._stop_event.is_set():
            with self._lock:
                for token in self._subscribed_tokens:
                    instrument = self._instruments.get(token)
                    if not instrument:
                        continue
                    config = self._settings.get("OPTION_DEFAULT", {}) if instrument["tradingsymbol"].upper().endswith(("CE", "PE")) else self._underlying_price_config
                    current = self._prices[token]
                    volatility = float(config.get("VOLATILITY_POINTS", 1))
                    minimum = float(config.get("MIN_PRICE", current - volatility * 10))
                    maximum = float(config.get("MAX_PRICE", current + volatility * 10))
                    next_price = current + self._random.uniform(-volatility, volatility)
                    self._prices[token] = max(minimum, min(maximum, next_price))
                    instrument["last_price"] = self._prices[token]
                    self._trader.set_latest_price(token, instrument["tradingsymbol"], self._prices[token])
            self._stop_event.wait(self._tick_interval)

    def start_socket_connection(self, shutdown_event, trader_instance):
        self._trader = trader_instance
        self._stop_event = shutdown_event
        self._thread = threading.Thread(target=self._tick_loop, daemon=True, name="simulation-ticks")
        self._thread.start()
        trader_instance.setup_woc_subscriptions()
        logger.info("Simulation tick engine started")

    def format_option_symbol(self, underlying, expiry, strike, call_or_put):
        return f"{underlying.upper()}{expiry.year % 100}{expiry.month}{expiry.day:02d}{int(strike)}{call_or_put.upper()}"

    def dev_start(self):
        return None

    def prod_start(self, request_token=None):
        return None

    def download_instrument_list(self, exchange=None, underlying=None):
        return None