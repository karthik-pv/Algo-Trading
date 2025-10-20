import logging
from datetime import datetime
import calendar
import csv
from pprint import pprint
from loguru import logger

from utils import get_trading_symbols_from_json , find_matching_row_in_csv , fetch_from_json
from core.kite_connector import KiteSingleton
from interface.broker_interface import BrokerInterface
from core.trade_logic import Trader_Singleton
from adapter.kite_utils import fund_summary_attribute_mgmt , position_attribute_mgmt , orders_attribute_mgmt, instrument_details_attribute_mgmt


class KiteAdapter(BrokerInterface):

    def __init__(self):
        self._trader = Trader_Singleton()
        self.kite_instance = KiteSingleton()
        self.kite = self.kite_instance.get_kite()
        self.supported_exchanges = ["MCX", "NIFTY"]
        self._limit_margin = fetch_from_json("constants.json" , "LIMIT_MARGIN")
        self._instrument_cache = {}

    def fetch_all_orders(self):
        try:
            orders = orders_attribute_mgmt(self.kite.orders())
            return orders
        except Exception as e:
            logger.error(f"Kite fetch_all_orders error: {e}")
            return None

    def fetch_all_instruments(self):
        try:
            return self.kite.instruments()
        except Exception as e:
            logger.error(f"Kite fetch_all_instruments error: {e}")

            return None

    def fetch_all_positions(self):
        try:
            positions = self.kite.positions()
            positions = position_attribute_mgmt(positions["net"])
            positions = [p for p in positions if p["quantity"] > 0]
            for position in positions:
                position["lotsize"] = int(self.get_instrument_details(position["tradingsymbol"])["lot_size"])
                position["quantity"] = position["quantity"]/position["lotsize"]
            pprint(positions)
            return positions
        except Exception as e:
            logger.error(f"Kite fetch_all_positions error: {e}")
            return None

    def fetch_all_trades(self):
        try:
            return self.kite.trades()
        except Exception as e:
            logger.error(f"Kite fetch_all_trades error: {e}")
            return None
        
    def fetch_fund_summary(self):
        try:
            fund_summary = fund_summary_attribute_mgmt(self.kite.margins())
            return fund_summary
        except Exception as e:
            logger.error(f"Kite fund summary error : {e}")
        return
    
    def fetch_instrument_quote(self , exchange , instrument):
        try:
            formatted_instrument = f"{exchange}:{instrument}"
            instrument_quote = self.kite.quote(formatted_instrument)
            return instrument_quote
        except Exception as e:
            logger.error(f"Kite fetch instrument quote error : {e}")
        
    def get_ltp(self , symbol_name):
        try:
            data = find_matching_row_in_csv("kite_instruments.csv" , "tradingsymbol" , symbol_name)
            quote = self.fetch_instrument_quote(data["exchange"] , data["tradingsymbol"])
            formatted_instrument = f"{data["exchange"]}:{data["tradingsymbol"]}" 
            return quote[formatted_instrument]["last_price"]
        except Exception as e:
            logger.error(f"Error getting LTP {e}")
    
    def get_instrument_details(self, tradingsymbol):
        logger.info(f"Attempting to fetch instrument details for {tradingsymbol}")
        if tradingsymbol in self._instrument_cache:
            logger.info(f"Cache HIT for instrument: {tradingsymbol}")
            return self._instrument_cache[tradingsymbol]
        logger.info(f"Cache MISS for instrument: {tradingsymbol}. Fetching from source (CSV).")
        data = find_matching_row_in_csv("kite_instruments.csv", "tradingsymbol", tradingsymbol)
        if not data:
            logger.error(f"No instrument details found for {tradingsymbol} in kite_instruments.csv")
            return None
        logger.info(f"Instrument details fetched from CSV: {data}")
        processed_data = instrument_details_attribute_mgmt(data)
        logger.log("DATA", f"Instrument Details (processed): {processed_data}")
        self._instrument_cache[tradingsymbol] = processed_data
        return processed_data

    def format_option_symbol(self ,  underlying: str,
        expiry: datetime.date,
        strike: int,
        call_or_put: str) -> str:
        underlying = underlying.upper()
        call_or_put = call_or_put.upper()
        yy = expiry.year % 100

        logger.info(f"Formatting option symbol for {underlying}, expiry: {expiry}, strike: {strike}, type: {call_or_put}")

        if underlying == "CRUDEOIL" or underlying == "CRUDEOILM":
            month_abbr = expiry.strftime('%b').upper()
            
            return f"{underlying}{yy}{month_abbr}{strike}{call_or_put}"
        else:
            month_char = calendar.month_name[expiry.month][0].upper()
            dd_str = f"{expiry.day:02d}"
            
            return f"{underlying}{yy}{month_char}{dd_str}{strike}{call_or_put}"
    
    def buy_units(self, trading_symbol, instrument_token, quantity , exchange , ltp):
        try:
            if exchange == "MCX":
                order_type = self.kite.ORDER_TYPE_LIMIT
                price = ltp+self._limit_margin
            else:
                order_type = self.kite.ORDER_TYPE_MARKET
                price = 0
            lotsize = self.get_instrument_details(trading_symbol)["lot_size"]
            logger.debug(f"Quantity - {int(quantity) * int(lotsize)}")
            order_id = self.kite.place_order(
                tradingsymbol=trading_symbol, 
                exchange = exchange,
                transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                quantity= str(int(quantity) * int(lotsize)),
                order_type = order_type,
                product = self.kite.PRODUCT_MIS,
                variety=self.kite.VARIETY_REGULAR, 
                price = price
            )
            logger.info("Kite buy order placed successfully")
            return order_id
        except Exception as e:
            logger.error(f"Error in kite buy order {e}")
    

    def sell_units(self, trading_symbol, instrument_token , quantity, exchange , ltp):
        try:
            if exchange == "MCX":
                order_type = self.kite.ORDER_TYPE_LIMIT
                price = ltp-self._limit_margin
            else:
                order_type = self.kite.ORDER_TYPE_MARKET
                price = 0
            lotsize = self.get_instrument_details(trading_symbol)["lot_size"]
            logger.debug(f"Quantity - {int(quantity) * int(lotsize)}")
            order_id = self.kite.place_order(
                tradingsymbol=trading_symbol,
                exchange=exchange,
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity= int(quantity) * int(lotsize),
                order_type=order_type,
                product=self.kite.PRODUCT_MIS,
                variety=self.kite.VARIETY_REGULAR,
                price = price
            )
            logger.info(f"Kite Sell order placed successfully. Order ID: {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"Kite sell_units error: {e}")
            return None

    def subscribe_to_all(self, instruments):
        try:
            socket = self.kite_instance.get_kite_socket_connection()
            instrument_tokens = [int(i) for i in instruments]
            if socket and socket.is_connected():
                socket.subscribe(instrument_tokens)
            logger.debug(f"Subscribed to {instrument_tokens}")
        except Exception as e:
            logger.error("Error in subscribing to instruments {e}")
        
    def unsubscribe_from_all(self, instruments):
        return super().unsubscribe_from_all(instruments)

    def start_socket_connection(self, shutdown_event, trader_instance):
        socket = self.kite_instance.get_kite_socket_connection()

        def on_ticks(ws, ticks):
            for tick in ticks:
                instrument_token = tick["instrument_token"]
                price = tick["last_price"]
                self._trader.set_latest_price(str(instrument_token), None, price)

        def on_connect(ws, response):
            logger.info("Connected to Kite WebSocket")

        def on_close(ws, code, reason):
            logger.warning(f"Kite WebSocket closed: {code}, {reason}")

        def on_order_update(ws, data):
            logger.info(f"Kite Order update: {data}")
            self._trader.refresh_open_positions_and_buy_price()
            subscribe_instruments = self._trader.get_relevant_instruments_to_track()
            if subscribe_instruments:
                ws.subscribe(subscribe_instruments)

        socket.on_ticks = on_ticks
        socket.on_connect = on_connect
        socket.on_close = on_close
        socket.on_order_update = on_order_update

        socket.connect(threaded=True)
        # if shutdown_event:
        #     shutdown_event.wait()
        socket.close()

    

    def dev_start(self):
        self.kite_instance.initialise_kite_for_dev()

    def prod_start(self , request_token=None):
        if request_token:
            self.kite_instance.start_session(request_token)
        else:
            self.kite_instance.initialise_kite_for_prod()

    def download_instrument_list(self,exchange):
        try:
            mcx_instruments = self.kite.instruments(exchange)
            if not mcx_instruments:
                logger.error("No instruments fetched. Cannot proceed to save.")
            else:
                filename = 'kite_instruments.csv'
                headers = mcx_instruments[0].keys()
                with open(filename, 'w', newline='') as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=headers)
                    writer.writeheader()
                    writer.writerows(mcx_instruments)
                logger.info(f"Successfully saved instruments to {filename}")
        except NameError:
            logger.error("The 'kite' object is not defined. Please ensure you have properly initialized and authenticated your KiteConnect object before running this code.")
        except Exception as e:
            logger.error(f"An error occurred during fetching or saving: {e}")
