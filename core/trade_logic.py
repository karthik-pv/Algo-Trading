import threading
import logging
import queue
import datetime
from typing import Optional , Dict
from flask_socketio import SocketIO
from interface.broker_interface import BrokerInterface

from core.trade_utils import get_weekly_expiry_date , get_instrument_tokens_from_symbol , get_instrument_details_from_json

from utils import fetch_from_json , find_matching_object

PNT_STOP_LOSS = 0.5
PNT_BOOK_PROFIT = 0.5

PCTG_BOOK_PROFIT = 0.5
PCTG_STOP_LOSS = 0.5


class Trader_Singleton:

    frontend_data_socket = SocketIO(cors_allowed_origins="*")

    _instance = None
    _broker = BrokerInterface
    
    _position_data = {}
    _five_weekly_option_contracts = {}

    _trading_watcher_thread_running = False
    _stop_event = threading.Event()
    _tick_counter = 0
    _comparison_function = "PNT"
    _use_max_margin = True


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
    
    def weekly_options_initialize(self , token , data , call_or_put):
        if token not in self._five_weekly_option_contracts:
            self._five_weekly_option_contracts[token] = {}
        self._five_weekly_option_contracts[token]["name"] = data["name"]
        self._five_weekly_option_contracts[token]["expiry"] = data["expiry"]
        self._five_weekly_option_contracts[token]["ltp"] = 0
        self._five_weekly_option_contracts[token]["lots"] =  data["lotsize"]
        self._five_weekly_option_contracts[token]["call_or_put"] = call_or_put

    
    def update_weekly_positions(self, token: str, attribute_name: str, updated_value):
        if token not in self._five_weekly_option_contracts:
            logging.warning(
                f"Token {token} not found in weekly option contracts. "
                f"Cannot update '{attribute_name}'."
            )
            return
        self._five_weekly_option_contracts[token][attribute_name] = updated_value
        logging.debug(
            f"Updated weekly option {token}: Set '{attribute_name}' to {updated_value}"
        )
    
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
        self._broker.unsubscribe_from_all(self.get_relevant_instruments_to_track())

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
                "latest_price": position["average_price"],
                "instrument_token": position["instrument_token"],
                "instrument": position["instrument_token"],
                "exchange" : position["exchange"]
            })
        
        self._broker.subscribe_to_all(self.get_relevant_instruments_to_track())
        logging.info(f"Updated positions: {len(self._position_data)}")
        logging.debug(self._position_data)

    def get_relevant_instruments_to_track(self):
        """Returns a list of instrument tokens currently in the _position_data."""
        return list(self._position_data.keys())

    # ---------------------- TRADING LOGIC (Refactored) ----------------------

    def stop_loss_book_profit_core(self, broker: BrokerInterface):
        logging.info("Started trading watcher thread")
        while not self._stop_event.is_set():
            print(self._position_data)
            for token , data in list(self._position_data.items()):
                print("---------------")
                print(data)
                print("---------------")
                qty = data.get("quantity")
                buy_price = data.get("average_price")
                ltp = data.get("latest_price")
                exchange = data.get("exchange")
                tradingsymbol = data.get("tradingsymbol")
                instrument_token = data.get("instrument_token")

                sell = self.to_sell_or_not_to_sell(buy_price , ltp)
                print(f"DECISION TO SELL IS {sell}")
                if sell:
                    broker.sell_units(tradingsymbol,instrument_token,qty , exchange , ltp)
                    print("SELLING")
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
        

    # ---------------------- GENERATE OTMS/ITMS ------------------------------    
        
    def get_5_weekly_option_contracts(
        self,
        nifty_price: float,
        call_or_put: str,
        strike_interval: int = 100,
        underlying: str = "NIFTY"
    ) -> Dict[str, any]:
        if strike_interval <= 0:
            raise ValueError("strike_interval must be positive integer")

        # Floor to the nearest lower strike -> ATM (ensures ATM <= current price)
        atm_strike = int((nifty_price // strike_interval) * strike_interval)

        # Pass the 'today' argument down to the expiry calculation function
        expiry = get_weekly_expiry_date()

        if call_or_put.upper() == "CE":
            strikes = {
                "ITM2": atm_strike - 2 * strike_interval,
                "ITM1": atm_strike - 1 * strike_interval,
                "ATM":  atm_strike,
                "OTM1": atm_strike + 1 * strike_interval,
                "OTM2": atm_strike + 2 * strike_interval,
            }
        elif call_or_put.upper() == "PE":
            strikes = {
                # for puts, ITM = strikes above spot
                "ITM2": atm_strike + 2 * strike_interval,
                "ITM1": atm_strike + 1 * strike_interval,
                "ATM":  atm_strike,
                "OTM1": atm_strike - 1 * strike_interval,
                "OTM2": atm_strike - 2 * strike_interval,
            }
        else:
            raise ValueError("call_or_put must be 'CE' or 'PE'")

        # Normalize: if any strike < 0, set to None
        for k, v in strikes.items():
            if v is None or v < 0:
                strikes[k] = None

        symbols = {
            k: (self._broker.format_option_symbol(underlying, expiry, v, call_or_put) if v is not None else None)
            for k, v in strikes.items()
        }

        return {"expiry": expiry, "strikes": strikes, "symbols": symbols}

        

    def setup_weekly_option_contract_subscriptions(self):
        nifty_near_month_token = fetch_from_json("constants.json" , "NIFTY_NEAR_MONTH_FUTURE_TOKEN")
        nifty_near_month_data = find_matching_object("instrument_list.json" , "name" , nifty_near_month_token)
        nifty_near_month_quote = self._broker.fetch_instrument_quote(nifty_near_month_data["exch_seg"] , nifty_near_month_data["token"])
        print(nifty_near_month_quote)
        ltp_nifty_near_month = nifty_near_month_quote["data"]["fetched"][0]["close"]
        contracts = self.get_5_weekly_option_contracts(ltp_nifty_near_month , "CE")["symbols"]
        relevant_tokens_to_subscribe = []
        for key , value in contracts.items(): 
            data = get_instrument_details_from_json(value)
            token = data["token"]
            relevant_tokens_to_subscribe.append(token)
            self.weekly_options_initialize(token , data , "CE")
        print(self._five_weekly_option_contracts)
        self._broker.subscribe_to_all(relevant_tokens_to_subscribe)
        return



    # ---------------------- DIFFERENCE CALCULATOR ----------------------



    def calculate_pctg_difference(self , buy_price: float, current_price: float) -> float:
        if buy_price <= 0:
            raise ValueError("Buy price must be greater than zero")
        
        pct_change = ((current_price - buy_price) / buy_price) * 100
        return pct_change
    
    def calculate_point_difference(self , buy_price: float, current_price: float) -> float:
        return current_price - buy_price

            
    # ---------------------- TICK HANDLER ----------------------

    def set_latest_price(self, instrument_token, tradingsymbol, price):
        token = instrument_token
        
        if token in self._position_data:
            self.position_data_update(
                token, 
                "latest_price", 
                price, 
                is_token=True
            )
        
        if token in self._five_weekly_option_contracts:
            self.update_weekly_positions(token , "ltp" , price)


        

    # ---------------------- THREAD CONTROL ----------------------

    def start_frontend_socket_server(self, app):
        self.frontend_data_socket.init_app(app)

        def run_socketio():
            self.frontend_data_socket.run(
                app,
                host='0.0.0.0',
                port=5001,
                allow_unsafe_werkzeug=True
            )
            
        socket_thread = threading.Thread(target=run_socketio, daemon=True)
        socket_thread.start()
        logging.info("Frontend socket server thread has been launched. 🚀")


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
        self._broker.subscribe_to_all(tokens_to_subscribe)
        
    def on_start(self):
        # Calls the centralized function
        self.refresh_open_positions_and_buy_price()