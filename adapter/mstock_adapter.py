import http.client
import json
import logging

from utils import get_trading_symbols_from_json
from core.mstock_connector import MStockSingleton
from interface.broker_interface import BrokerInterface
from core.trade_logic import Trader_Singleton
from adapter.mstock_utils import position_attribute_mgmt


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
        try:
            conn = http.client.HTTPSConnection("api.mstock.trade")
            headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
            }
            conn.request("GET", "/openapi/typeb/portfolio/positions", headers=headers)
            response = json.loads(conn.getresponse().read().decode("utf-8"))
            positions = position_attribute_mgmt(response["data"])
            open_positions = [
                open_position
                for open_position in positions
                if int(open_position["quantity"]) > 0
            ]
            return open_positions
        except Exception as e:
            logging.error(f"Error fetching positions: {e}")

    def fetch_all_trades(self):
        return

    def sell_units(self, trading_symbol, quantity, exchange):
        return

    def refresh_subscriptions(self, instruments):
        self.mstock_instance._subscribe_to_instruments(instruments)

    async def start_socket_connection(self, shutdown_event, trader_instance):
        await self.mstock_instance.start_socket_connection(
            shutdown_event, trader_instance
        )
        print("Socket started")

    def dev_start(self):
        self.mstock_instance.initialise_for_dev()

    def prod_start(self):
        self.mstock_instance.initialise_for_prod()
