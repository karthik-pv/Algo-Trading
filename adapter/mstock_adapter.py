import http.client
import json
import logging
import datetime
import calendar

from utils import get_trading_symbols_from_json , find_matching_object
from core.mstock_connector import MStockSingleton
from interface.broker_interface import BrokerInterface
from core.trade_logic import Trader_Singleton
from adapter.mstock_utils import position_attribute_mgmt , fund_summary_attribute_mgmt , order_attribute_mgmt


class MStockAdapter(BrokerInterface):

    def __init__(self):
        self._trader = Trader_Singleton()
        self.mstock_instance = MStockSingleton()
        self.supported_exchanges = ["NSE", "BSE"]

    def fetch_all_orders(self):
        try:
            conn = http.client.HTTPSConnection('api.mstock.trade')
            headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
            }
            conn.request('GET', '/openapi/typeb/orders', headers=headers)
            response = order_attribute_mgmt(json.loads(conn.getresponse().read().decode("utf-8"))["data"])
            print("\n")
            print("ORDERS PROCESSED DATA =================== \n")
            print(response)
            return response
        except Exception as e:
            logging.error(
                f"Error fetching orders: {e} \n\n CONSIDER LOGGING IN AGAIN \n\n"
            )

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
            print("POSITIONS RAW DATA =================== \n")
            print(response["data"])
            print("\n")
            print("POSITIONS PROCESSED DATA =================== \n")
            positions = position_attribute_mgmt(response["data"])
            open_positions = [
                open_position
                for open_position in positions
                if int(open_position["quantity"]) > 0
            ]
            print(open_positions)
            print("\n")
            return open_positions
        except Exception as e:
            logging.error(
                f"Error fetching positions: {e} \n\n CONSIDER LOGGING IN AGAIN \n\n"
            )

    def fetch_all_trades(self):
        return
    
    def fetch_fund_summary(self):
        conn = http.client.HTTPSConnection('api.mstock.trade')
        headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
            }
        conn.request('GET', '/openapi/typeb/user/fundsummary', headers=headers)
        response = json.loads(conn.getresponse().read().decode("utf-8"))
        fund_summary = fund_summary_attribute_mgmt(response)
        return fund_summary
    
    def fetch_instrument_quote(self , exchange , instrument_token):
        conn = http.client.HTTPSConnection('api.mstock.trade')
        headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
                "Content-Type": "application/json"
            }
        json_data = {
            'mode': 'OHLC',
            'exchangeTokens': {
               exchange : [instrument_token]
            },
        }
        conn.request(
            'POST',
            '/openapi/typeb/instruments/quote',
            json.dumps(json_data),
            headers
        )
        response = json.loads(conn.getresponse().read().decode("utf-8"))
        return response


    def sell_units(self, trading_symbol, instrument_token , quantity, exchange , ltp):
        print("here")
        conn = http.client.HTTPSConnection('api.mstock.trade')
        headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
                "Content-Type": "application/json"
            }
        trading_symbol_for_transaction = find_matching_object("instrument_list.json" , "token" , instrument_token)["name"]
        json_data = { 
            'variety': 'NORMAL',
            'tradingsymbol': trading_symbol_for_transaction,
            'symboltoken': instrument_token,
            'exchange': "NFO",
            'transactiontype': 'SELL',
            'ordertype': 'MARKET',
            'quantity': str(quantity),
            'producttype': 'INTRADAY',
            'price': "0.00",
            'triggerprice': '0.00',
            'squareoff': '0.00',
            'stoploss': '0.00',
            'trailingStopLoss': '',
            'disclosedquantity': '0',
            'duration': 'DAY',
            'ordertag': 'my_algo',
        }
        print(json_data)
        conn.request(
            'POST',
            '/openapi/typeb/orders/regular',
            json.dumps(json_data),
            headers
        )
        response = conn.getresponse().read().decode("utf-8")
        print(response)

    def buy_units(self, trading_symbol, instrument_token, quantity, exchange, ltp):
        return super().buy_units(trading_symbol, instrument_token, quantity, exchange, ltp)
    
    def format_option_symbol(self ,  underlying: str,
        expiry: datetime.date,
        strike: int,
        call_or_put: str) -> str:
        yy = expiry.year % 100
        # Get the first letter of the month name, e.g., 'October' -> 'O'
        month_char = calendar.month_name[expiry.month][0].upper()
        dd_str = f"{expiry.day:02d}"
        return f"{underlying}{yy}{month_char}{dd_str}{strike}{call_or_put.upper()}"

    def unsubscribe_from_all(self,instruments):
        self.mstock_instance._unsubscribe_from_all(instruments)

    def subscribe_to_all(self, instruments):
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
