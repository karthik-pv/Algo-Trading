import logging

from utils import get_trading_symbols_from_json
from core.mstock_connector import MStockSingleton
from interface.broker_interface import BrokerInterface
from core.trade_logic import Trader_Singleton


class MStockAdapter(BrokerInterface):

    def __init__(self):
        self._trader = Trader_Singleton()
        self.mstock_instance = MStockSingleton()
        self.supported_exchanges = ["NSE", "BSE"]  # Adjust as per MStock API

    def fetch_all_orders(self):
        return

    def fetch_all_instruments(self):
        return

    def fetch_all_positions(self):
        return

    def fetch_all_trades(self):
        return

    def sell_units(self, trading_symbol, quantity, exchange):
        return

    def fetch_instruments_from_json(self):
        return

    def start_socket_connection(self, shutdown_event, trader_instance):
        return

    def dev_start(self):
        self.mstock_instance.initialise_for_dev()

    def prod_start(self):
        self.mstock_instance.initialise_for_prod()
