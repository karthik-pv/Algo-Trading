import logging

from utils import get_trading_symbols_from_json
from core.mstock_connector import MStockSingleton
from interface.broker_interface import BrokerInterface
from core.trade_logic import Trader_Singleton


class MStockAdapter(BrokerInterface):

    def __init__(self):
        self._trader = Trader_Singleton()
        self.mstock_instance = MStockSingleton()
        self.client = self.mstock_instance.get_client()
        self.supported_exchanges = ["NSE", "BSE"]  # Adjust as per MStock API

    def fetch_all_orders(self):
        try:
            return self.client.get_orders()
        except Exception as e:
            logging.error(f"MStock fetch_all_orders error: {e}")
            return None

    def fetch_all_instruments(self):
        try:
            return self.client.get_instruments()
        except Exception as e:
            logging.error(f"MStock fetch_all_instruments error: {e}")
            return None

    def fetch_all_positions(self):
        try:
            positions = self.client.get_positions()
            return [p for p in positions if p.get("quantity", 0) > 0]
        except Exception as e:
            logging.error(f"MStock fetch_all_positions error: {e}")
            return None

    def fetch_all_trades(self):
        try:
            return self.client.get_trades()
        except Exception as e:
            logging.error(f"MStock fetch_all_trades error: {e}")
            return None

    def sell_units(self, trading_symbol, quantity, exchange):
        try:
            order_id = self.client.place_order(
                tradingsymbol=trading_symbol,
                exchange=exchange,
                transaction_type="SELL",
                quantity=quantity,
                order_type="MARKET",
                product="MIS",
                variety="REGULAR",
            )
            logging.info(f"MStock Sell order placed successfully. Order ID: {order_id}")
            return order_id
        except Exception as e:
            logging.error(f"MStock sell_units error: {e}")
            return None

    def fetch_instruments_from_json(self):
        try:
            instruments = self.client.get_instruments()
            trading_symbols = get_trading_symbols_from_json()
            relevant_instruments = [
                inst["token"]
                for inst in instruments
                if inst["symbol"] in trading_symbols
                and inst["exchange"] in self.supported_exchanges
            ]
            return relevant_instruments if relevant_instruments else []
        except Exception as e:
            logging.error(f"MStock fetch_instruments_from_json error: {e}")
            return None

    def start_socket_connection(self, shutdown_event, trader_instance):
        socket = self.mstock_instance.get_socket_connection()

        def on_ticks(ws, ticks):
            self._trader.set_latest_price(ticks)

        def on_connect(ws, response):
            logging.info("Connected to MStock WebSocket")
            instruments = self.fetch_instruments_from_json()
            if instruments:
                ws.subscribe(instruments)

        def on_close(ws, code, reason):
            logging.warning(f"MStock WebSocket closed: {code}, {reason}")

        def on_order_update(ws, data):
            logging.info(f"MStock Order update: {data}")
            self._trader.refresh_open_positions_and_buy_price()
            subscribe_instruments = self._trader.get_relevant_instruments_to_track()
            if subscribe_instruments:
                ws.subscribe(subscribe_instruments)

        socket.on_ticks = on_ticks
        socket.on_connect = on_connect
        socket.on_close = on_close
        socket.on_order_update = on_order_update

        socket.connect(threaded=True)
        if shutdown_event:
            shutdown_event.wait()
        socket.close()

    def dev_start(self):
        self.mstock_instance.initialise_for_dev()

    def prod_start(self):
        self.mstock_instance.initialise_for_prod()
