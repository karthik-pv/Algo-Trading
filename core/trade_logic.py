import threading
import logging
import queue
from interface.broker_interface import BrokerInterface

PNT_STOP_LOSS = 0.5
PNT_BOOK_PROFIT = 0.5

PCTG_BOOK_PROFIT = 0.5
PCTG_STOP_LOSS = 0.5


class Trader_Singleton:
    _instance = None
    _broker = None
    
    _position_data = {}

    _trading_watcher_thread_running = False
    _stop_event = threading.Event()
    _tick_counter = 0
    _comparison_function = "PNT"
    _use_max_margin = True

    alert_queue = queue.Queue()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Trader_Singleton, cls).__new__(cls)
        return cls._instance

    def set_broker(self, broker):
        self._broker = broker

    def _get_position_key(self, token_or_symbol, is_token=True):
        """Helper to find the instrument token (the master key) from either token or trading symbol."""
        if is_token:
            return token_or_symbol
        
        for token, data in self._position_data.items():
            if data.get("tradingsymbol") == token_or_symbol:
                return token
        return None
    
    def position_data_update(self, token_or_symbol, attribute_name, updated_value, is_token=True):
        key_token = self._get_position_key(token_or_symbol, is_token)
        
        if key_token is None:
            logging.warning(f"Could not find position for {token_or_symbol}. Skipping update for {attribute_name}.")
            return
            
        if key_token not in self._position_data:
            self._position_data[key_token] = {}
            
        self._position_data[key_token][attribute_name] = updated_value
        
        logging.debug(f"Updated {key_token}:{attribute_name} to {updated_value}")

    def refresh_open_positions_and_buy_price(self):
        # Clear existing data to reflect only currently open positions
        # NOTE: We iterate over a copy to safely remove/update entries
        positions_from_broker = self._broker.fetch_all_positions()
        
        new_position_tokens = {pos["instrument_token"] for pos in positions_from_broker}
        
        tokens_to_remove = [
            token for token in self._position_data 
            if token not in new_position_tokens
        ]
        for token in tokens_to_remove:
            del self._position_data[token]
            
        for position in positions_from_broker:
            token = position["instrument_token"]
            
            if token not in self._position_data:
                self._position_data[token] = {}

            self._position_data[token].update({
                "tradingsymbol": position["tradingsymbol"],
                "average_price": position["average_price"],
                "quantity": position["quantity"], 
                "latest_price": self._position_data[token].get("latest_price")
            })
            
        logging.info(f"Updated positions: {len(self._position_data)}")
        logging.debug(self._position_data)

    def get_relevant_instruments_to_track(self):
        """Returns a list of instrument tokens currently in the _position_data."""
        return list(self._position_data.keys())

    def update_trading_symbol_and_quantity(self):
        self.refresh_open_open_positions_and_buy_price()

    # ---------------------- TRADING LOGIC (Refactored) ----------------------

    def stop_loss_book_profit_core(self, broker: BrokerInterface):
        logging.info("Started trading watcher thread")
        while not self._stop_event.is_set():
            # Iterate over positions still in the dictionary
            for token, data in list(self._position_data.items()):
                qty = data.get("quantity")
                buy_price = data.get("average_price")
                ltp = data.get("latest_price")
                tradingsymbol = data.get("tradingsymbol")

                sell = self.to_sell_or_not_to_sell(buy_price , ltp)

                if sell:
                    #sell()
                    pass

                

            self._stop_event.wait(timeout=5)

    # ---------------------- DECISION MAKER ----------------------------

    def to_sell_or_not_to_sell(self,buy_price , current_price):
        # that is the question 
        if self._comparison_function == "PCT":
            diff = self.calculate_pctg_difference(buy_price,current_price)
            if diff >= PCTG_BOOK_PROFIT or abs(diff) >= PCTG_STOP_LOSS:
                return True
        if self._comparison_function == "PNT":
            diff = self.calculate_point_difference(buy_price,current_price)
            if diff >= PNT_BOOK_PROFIT or abs(diff) >= PNT_STOP_LOSS:
                return True
        return False
        
        


    # ---------------------- DIFFERENCE CALCULATOR ----------------------



    def calculate_pctg_difference(buy_price: float, current_price: float) -> float:
        if buy_price <= 0:
            raise ValueError("Buy price must be greater than zero")
        
        pct_change = ((current_price - buy_price) / buy_price) * 100
        return pct_change
    
    def calculate_point_difference(buy_price: float, current_price: float) -> float:
        return current_price - buy_price

            
    # ---------------------- TICK HANDLES ----------------------

    def set_latest_price(self, instrument_token, tradingsymbol, price):
        token = instrument_token
        
        if token not in self._position_data:
            self._position_data[token] = {
                "tradingsymbol": tradingsymbol,
            }
            
        self.position_data_update(
            token, 
            "latest_price", 
            price, 
            is_token=True
        )

    # ---------------------- THREAD CONTROL ----------------------

    def start_trading_watcher_thread(self):
        if not self._trading_watcher_thread_running:
            self._trading_watcher_thread_running = True
            self._stop_event.clear()
            thread = threading.Thread(
                target=self.stop_loss_book_profit_core,
                args=(self._broker,),
                daemon=True,
            )
            logging.info("Launching trading watcher thread 🚀")
            thread.start()

    def stop_trading_watcher_thread(self):
        if self._trading_watcher_thread_running:
            logging.info("Stopping trading watcher thread ⏹")
            self._stop_event.set()
            self._trading_watcher_thread_running = False

    def refresh_subscriptions(self):
        # Refactored to use the new centralized data structure
        self.refresh_open_positions_and_buy_price()
        
        # Get the list of tokens to subscribe to
        tokens_to_subscribe = self.get_relevant_instruments_to_track()
        
        print(self._position_data)
        
        # Call the broker's subscription method
        self._broker.refresh_subscriptions(tokens_to_subscribe)
        
    def on_start(self):
        # Calls the centralized function
        self.refresh_open_positions_and_buy_price()