from abc import ABC, abstractmethod


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
    def sell_units(self, trading_symbol, instrument_token ,quantity ,exchange ,ltp):
        pass

    @abstractmethod
    def buy_units(self , trading_symbol , instrument_token , quantity , exchange ,ltp):
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
        keys in constants.json). Brokers compute/persist this from
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
        Default implementation derives what it can from constants.json
        (UNDERLYING) with safe fallbacks.
        """
        from utils import fetch_from_json, get_exchange_for_underlying

        underlying = str(fetch_from_json("constants.json", "UNDERLYING") or "").upper()
        return {
            "underlying": underlying,
            "exchange": get_exchange_for_underlying(underlying) or "",
            "near_month_future_token": None,
            "near_option_expiry": None,
            "expire_together": False,
            "strike_interval": 100 if underlying == "SENSEX" else 50,
            "generated_on": None,
        }

    def preload_instrument_data(self):
        """Optional hook: warm broker-side instrument caches in a
        background thread at startup. Default no-op; brokers with a
        heavy instrument master (e.g. Kite CSV) override this."""
        return None