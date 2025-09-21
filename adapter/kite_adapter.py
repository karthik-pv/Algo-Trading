import logging

from utils import get_trading_symbols_from_json
from core.kite_connector import KiteSingleton
from interface.broker_interface import BrokerInterface
from core.trade_logic import Trader_Singleton


class KiteAdapter(BrokerInterface):

    def __init__(self):
        self._trader = Trader_Singleton()
        self.kite_instance = KiteSingleton()
        self.kite = self.kite_instance.get_kite()
        self.supported_exchanges = ["MCX", "NIFTY"]

    def fetch_all_orders(self):
        try:
            return self.kite.orders()
        except Exception as e:
            logging.error(f"Kite fetch_all_orders error: {e}")
            return None

    def fetch_all_instruments(self):
        try:
            return self.kite.instruments()
        except Exception as e:
            logging.error(f"Kite fetch_all_instruments error: {e}")
            return None

    def fetch_all_positions(self):
        try:
            positions = self.kite.positions()
            return [p for p in positions["net"] if p["quantity"] > 0]
        except Exception as e:
            logging.error(f"Kite fetch_all_positions error: {e}")
            return None

    def fetch_all_trades(self):
        try:
            return self.kite.trades()
        except Exception as e:
            logging.error(f"Kite fetch_all_trades error: {e}")
            return None

    def sell_units(self, trading_symbol, quantity, exchange):
        try:
            order_id = self.kite.place_order(
                tradingsymbol=trading_symbol,
                exchange=exchange,
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity=quantity,
                order_type=self.kite.ORDER_TYPE_MARKET,
                product=self.kite.PRODUCT_MIS,
                variety=self.kite.VARIETY_REGULAR,
            )
            logging.info(f"Kite Sell order placed successfully. Order ID: {order_id}")
            return order_id
        except Exception as e:
            logging.error(f"Kite sell_units error: {e}")
            return None

    # def fetch_instruments_from_json(self):
    #     try:
    #         instruments = self.kite.instruments()
    #         trading_symbols = get_trading_symbols_from_json()
    #         # trading_symbols = ["CRUDEOILM25SEP5400PE"]
    #         relevant_instruments = [
    #             inst["instrument_token"]
    #             for inst in instruments
    #             if inst["tradingsymbol"] in trading_symbols
    #             and inst["exchange"] in self.supported_exchanges
    #         ]
    #         return relevant_instruments if relevant_instruments else []
    #     except Exception as e:
    #         logging.error(f"Kite fetch_instruments_from_json error: {e}")
    #         return None

    def start_socket_connection(self, shutdown_event, trader_instance):
        socket = self.kite_instance.get_kite_socket_connection()

        def on_ticks(ws, ticks):
            for tick in ticks:
                instrument_token = tick["instrument_token"]
                price = tick["ohlc"]["close"]
                self._trader.set_latest_price(instrument_token, None, price)

        def on_connect(ws, response):
            logging.info("Connected to Kite WebSocket")
            instruments = self.fetch_instruments_from_json()
            ws.set_mode(ws.MODE_FULL, instruments)

        def on_close(ws, code, reason):
            logging.warning(f"Kite WebSocket closed: {code}, {reason}")

        def on_order_update(ws, data):
            logging.info(f"Kite Order update: {data}")
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
        self.kite_instance.initialise_kite_for_dev()

    def prod_start(self):
        self.kite_instance.initialise_kite_for_prod()
