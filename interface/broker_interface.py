from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)


class BrokerInterface(ABC):

    @abstractmethod
    def fetch_all_orders(self):
        pass

    @abstractmethod
    def fetch_all_instruments(self):
        pass

    @abstractmethod
    def fetch_all_positions(self):
        pass

    @abstractmethod
    def fetch_all_trades(self):
        pass

    @abstractmethod
    def fetch_fund_summary(self):
        pass

    def fetch_day_start_cash(self):
        return None

    @abstractmethod
    def fetch_instrument_quote(self):
        pass

    @abstractmethod
    def fetch_expiries(self):
        pass

    @abstractmethod
    def get_ltp(self):
        pass

    @abstractmethod
    def get_instrument_details(self , tradingsymbol):
        pass

    @abstractmethod
    def sell_units(self, trading_symbol, instrument_token ,quantity ,exchange ,ltp, position_key=""):
        pass

    def place_exit_limit(self, trading_symbol, instrument_token, quantity, exchange, price):
        """
        Place a SELL LIMIT exit order (Sell Types U/D).

        quantity is in LOTS. Used at buy time (U/D immediate exits) and
        to re-place a partial leg's exit after a manual partial sell.
        Default no-op for brokers/adapters that do not support resting
        exit orders (simulator, playback).
        """
        logger.warning(
            f"place_exit_limit not supported by {type(self).__name__}; "
            f"skipping exit for {trading_symbol}"
        )
        return None

    def cancel_all_pending_orders(self):
        """
        Cancel every pending/open order at the broker. Returns the
        number of orders cancelled. Default no-op (simulator, playback).
        """
        logger.warning(
            f"cancel_all_pending_orders not supported by "
            f"{type(self).__name__}; nothing cancelled."
        )
        return 0

    @abstractmethod
    def buy_units(self , trading_symbol , instrument_token , quantity , exchange ,ltp, mode="", sell_mode="", target_profit=0, strategy=""):
        pass

    @abstractmethod
    def unsubscribe_from_all(self , instruments):
        pass

    @abstractmethod
    def subscribe_to_all(self , instruments):
        pass

    @abstractmethod
    def format_option_symbol(self ,  underlying , expiry , strike, call_or_put):
        pass

    @abstractmethod
    def start_socket_connection(self, shutdown_event, trader_instance):
        pass

    @abstractmethod
    def dev_start():
        pass

    @abstractmethod
    def prod_start():
        pass

    @abstractmethod
    def download_instrument_list():
        pass

    def get_instrument_meta(self):
        """
        Instrument-file-derived constants (replaces the old derived
        keys in appconfig.json). Brokers compute/persist this from
        their instrument master; shape:
        {
            "underlying": "SENSEX",
            "exchange": "BFO",
            "near_month_future_token": "SENSEX26SEPFUT",
            "near_option_expiry": "2026-09-17",
            "expire_together": false,
            "strike_interval": 100,
            "generated_on": "2026-09-16",
        }
        Default implementation derives what it can from appconfig.json
        (UNDERLYING) with safe fallbacks.
        """
        from core.utils import fetch_from_json, get_exchange_for_underlying

        underlying = str(fetch_from_json("appconfig.json", "UNDERLYING") or "").upper()
        return {
            "underlying": underlying,
            "exchange": get_exchange_for_underlying(underlying) or "",
            "near_month_future_token": None,
            "near_option_expiry": None,
            "expire_together": False,
            "strike_interval": 100,
            "generated_on": None,
        }

    def preload_instrument_data(self):
        """Optional hook: warm broker-side instrument caches in a
        background thread at startup. Default no-op; brokers with a
        heavy instrument master (e.g. Kite CSV) override this."""
        return None