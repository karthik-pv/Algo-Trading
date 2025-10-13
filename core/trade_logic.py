import threading
import logging
import queue
import datetime
from typing import Optional , Dict , Any
from flask_socketio import SocketIO
from interface.broker_interface import BrokerInterface

from core.trade_utils import get_expiry_date , get_instrument_tokens_from_symbol , get_instrument_details_from_json , calculate_accurate_average_buy_price_and_update_positions 

from utils import fetch_from_json , find_matching_object
import time

PNT_STOP_LOSS = 1
PNT_BOOK_PROFIT = 1

PCTG_BOOK_PROFIT = 1
PCTG_STOP_LOSS = 1


class Trader_Singleton:

    frontend_data_socket = SocketIO(cors_allowed_origins="*")

    _instance = None
    _broker = BrokerInterface
    
    _position_data = {}
    _five_weekly_option_contracts = {}
    _fund_summary = {}

    _trading_watcher_thread_running = False
    _stop_event = threading.Event()
    _tick_counter = 0
    _comparison_function = "PNT"
    _use_max_margin = True
    _sell_mode = "ALERT"

    _positions_to_subscribe = []


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
    
    def weekly_options_initialize(self , token , data , call_or_put , price , level):
        if token not in self._five_weekly_option_contracts:
            self._five_weekly_option_contracts[token] = {}
        self._five_weekly_option_contracts[token]["name"] = data["tradingsymbol"]
        self._five_weekly_option_contracts[token]["expiry"] = data["expiry"]
        self._five_weekly_option_contracts[token]["ltp"] = price
        self._five_weekly_option_contracts[token]["lotsize"] = data["lot_size"]
        self._five_weekly_option_contracts[token]["lots"] =  int(((self._fund_summary["cash_balance"] - self._fund_summary["utilized"]) / price) / int(data["lot_size"]))
        self._five_weekly_option_contracts[token]["call_or_put"] = call_or_put
        self._five_weekly_option_contracts[token]["level"] = level

    
    def update_weekly_positions(self, token: str, attribute_name: str, updated_value):
        if token not in self._five_weekly_option_contracts:
            logging.warning(
                f"Token {token} not found in weekly option contracts. "
                f"Cannot update '{attribute_name}'."
            )
            return
        self._five_weekly_option_contracts[token][attribute_name] = updated_value
        self._five_weekly_option_contracts[token]["lots"] =  int(((self._fund_summary["cash_balance"]) / self._five_weekly_option_contracts[token]["ltp"]) / int(self._five_weekly_option_contracts[token]["lotsize"]))
        logging.debug(
            f"Updated weekly option {token}: Set '{attribute_name}' to {updated_value}"
        )
    
    def position_data_update(self, token_or_symbol, attribute_name, updated_value, is_token=True):
        key_token = self._get_position_key(token_or_symbol, is_token)
        
        if key_token is None:
            logging.warning(f"Could not find position for {token_or_symbol}. Skipping update.")
            return
            
        if key_token not in self._position_data:
            self._position_data[key_token] = {}
            
        self._position_data[key_token][attribute_name] = updated_value
        
        # --- ADDED: P/L Recalculation Block ---
        # If the price was updated, recalculate all P/L fields
        if attribute_name == 'latest_price':
            position = self._position_data[key_token]
            buy_price = position.get('average_price', 0)
            net_qty = position.get('net_quantity', 0)
            
            if buy_price > 0:
                # Recalculate and store the new P/L values
                pts_pl = self.calculate_point_difference(buy_price, updated_value)
                pct_pl = self.calculate_pctg_difference(buy_price, updated_value)
                total_pl = pts_pl * net_qty
                
                position['pts_pl'] = pts_pl
                position['pct_pl'] = pct_pl
                position['total_pl'] = total_pl
        # --- END ADDED BLOCK ---
        
        logging.debug(f"Updated {key_token}:{attribute_name} to {updated_value}")

    def refresh_open_positions_and_buy_price(self):
        self._broker.unsubscribe_from_all(self.get_relevant_instruments_to_track())
        fund_summary = self._broker.fetch_fund_summary()
        positions_from_broker = self._broker.fetch_all_positions()
        orders_from_broker = self._broker.fetch_all_orders()
        positions = calculate_accurate_average_buy_price_and_update_positions(positions_from_broker , orders_from_broker)

        new_position_tokens = {pos["instrument_token"] for pos in positions}
        
        tokens_to_remove = [
            token for token in self._position_data 
            if token not in new_position_tokens
        ]
        for token in tokens_to_remove:
            del self._position_data[token]
            
        for position in positions: # Assuming this is the accurately calculated 'positions' list
            token = position["instrument_token"]
            
            if token not in self._position_data:
                self._position_data[token] = {}

            # --- MODIFIED BLOCK ---
            avg_price = position["average_price"]
            net_qty = int(position["quantity"])
            lotsize = int(position["lotsize"])

            # Calculate initial P/L values (they will be 0)
            pts_pl = self.calculate_point_difference(avg_price, avg_price)
            pct_pl = self.calculate_pctg_difference(avg_price, avg_price)
            total_pl = pts_pl * net_qty

            self._position_data[token].update({
                "tradingsymbol": position["tradingsymbol"],
                "average_price": avg_price,
                "net_quantity": net_qty, 
                "latest_price": avg_price, # Initial latest_price is the buy price
                "instrument_token": position["instrument_token"],
                "exchange" : position["exchange"],
                "lotsize" : lotsize,
                "lots": int(net_qty / lotsize),
                # ADDED: Initial P/L fields
                "pts_pl": pts_pl,
                "pct_pl": pct_pl,
                "total_pl": total_pl
            })
            # --- END MODIFIED BLOCK ---

        for key , value in fund_summary.items():
            self._fund_summary[key] = float(value)
            
        self._broker.subscribe_to_all(self.get_relevant_instruments_to_track())
        try:
            if not self.frontend_data_socket:
                raise ConnectionRefusedError
            self.frontend_data_socket.emit(
                    'update_open_positions',
                    self._position_data
                )
            self.frontend_data_socket.emit(
                    'update_weekly_options',
                    self._five_weekly_option_contracts
                )
        except Exception as e:
            print("Frontend socket yet to be instantiated")
        logging.info(f"Updated positions: {len(self._position_data)}")
        logging.debug(self._position_data)

    def get_relevant_instruments_to_track(self):
        """Returns a list of instrument tokens currently in the _position_data."""
        instruments = list(self._position_data.keys())
        instruments.extend(self._five_weekly_option_contracts.keys())
        print(instruments)
        return instruments

    # ---------------------- TRADING LOGIC (Refactored) ----------------------

    def stop_loss_book_profit_core(self, broker: BrokerInterface):
        logging.info("Started trading watcher thread")
        while not self._stop_event.is_set():
            print(self._position_data)
            for token , data in list(self._position_data.items()):
                print("---------------")
                print(data)
                print("---------------")
                qty = data.get("lots")
                buy_price = data.get("average_price")
                ltp = data.get("latest_price")
                exchange = data.get("exchange")
                tradingsymbol = data.get("tradingsymbol")
                instrument_token = data.get("instrument_token")

                sell = self.to_sell_or_not_to_sell(buy_price , ltp)
                print(f"DECISION TO SELL IS {sell}")
                if sell:
                    if self._sell_mode == "SELL":
                        print(tradingsymbol)
                        print(instrument_token)
                        print(qty)
                        broker.sell_units(None,instrument_token,qty)
                    print("###################################")
                    logging.critical(f"SELL UNITS - {tradingsymbol}")
                    print("###################################")
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
        underlying: str = "NIFTY",
        exchange: str = "NFO"
    ) -> Dict[str, any]:
        if strike_interval <= 0:
            raise ValueError("strike_interval must be positive integer")
        
        atm_strike = int((nifty_price // strike_interval) * strike_interval)

        expiry = get_expiry_date(underlying)

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
                "ITM2": atm_strike + 2 * strike_interval,
                "ITM1": atm_strike + 1 * strike_interval,
                "ATM":  atm_strike,
                "OTM1": atm_strike - 1 * strike_interval,
                "OTM2": atm_strike - 2 * strike_interval,
            }
        else:
            raise ValueError("call_or_put must be 'CE' or 'PE'")

        for k, v in strikes.items():
            if v is None or v < 0:
                strikes[k] = None

        symbols = {
            k: (self._broker.format_option_symbol(underlying, expiry, v, call_or_put) if v is not None else None)
            for k, v in strikes.items()
        }

        return {"expiry": expiry, "strikes": strikes, "symbols": symbols}

        

    def setup_weekly_option_contract_subscriptions(self):
        try:
            near_month_token = fetch_from_json("constants.json" , "NEAR_MONTH_FUTURE_TOKEN")
            print(near_month_token)
            ltp_nifty_near_month = self._broker.get_ltp(near_month_token)
            print(ltp_nifty_near_month)
            contracts = self.get_5_weekly_option_contracts(float(ltp_nifty_near_month) , "CE" , underlying="CRUDEOIL")["symbols"]
            print(contracts)
            relevant_tokens_to_subscribe = []
            for key , value in contracts.items(): 
                data = self._broker.get_instrument_details(value)
                print(data)
                price = self._broker.get_ltp(data["tradingsymbol"])
                print(price)
                token = data["instrument_token"]
                relevant_tokens_to_subscribe.append(token)
                self.weekly_options_initialize(token , data , "CE" , price , level=key)
            contracts = self.get_5_weekly_option_contracts(ltp_nifty_near_month , "PE" , underlying="CRUDEOIL")["symbols"]
            for key , value in contracts.items(): 
                data = self._broker.get_instrument_details(value)
                price = self._broker.get_ltp(data["tradingsymbol"])
                token = data["instrument_token"]
                relevant_tokens_to_subscribe.append(token)
                self.weekly_options_initialize(token , data , "PE" , price , level=key)
            print("++++++++++++++++++++++++++++++++++++++++")
            print(self._five_weekly_option_contracts)
            print("Subscribing here ++++++++++++++++++++++")
            print(relevant_tokens_to_subscribe)
            self._broker.subscribe_to_all(relevant_tokens_to_subscribe)
            return
        except Exception as e:
            print(f"Exception in setting up weekly options - {e}")



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
        try:
            token = instrument_token
            if token in self._position_data:
                self.position_data_update(
                    token, 
                    "latest_price", 
                    price, 
                    is_token=True
                )
                position_info = self._position_data[token]
                payload = {
                    'token': token,
                    'name': tradingsymbol,
                    'ltp': price,
                    'pts_pl': position_info.get('pts_pl'),
                    'pct_pl': position_info.get('pct_pl'),
                    'total_pl': position_info.get('total_pl')
                }
                print("SENDING THE DATA ++++++++++++++++++++++++++++++++++++")
                print(payload)
                self.frontend_data_socket.emit('price-updated-position', payload)
            
            
            if token in self._five_weekly_option_contracts:
                self.update_weekly_positions(token , "ltp" , price)
                payload = {
                    'token': token,             
                    'name': tradingsymbol,      
                    'ltp': price,
                    'lots' : self._five_weekly_option_contracts[token]["lots"]
                }
                self.frontend_data_socket.emit('price-updated-order', payload)
        except Exception as e:
            print(e)

        

    # ---------------------- THREAD CONTROL ----------------------

    def start_frontend_socket_server(self, app):
        self.frontend_data_socket.init_app(app)
        self.register_socket_handlers()

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
        self.refresh_open_positions_and_buy_price()
        tokens_to_subscribe = self.get_relevant_instruments_to_track()
        try:
            self._broker.subscribe_to_all(tokens_to_subscribe)
        except Exception as e:
            print(f"Couldnt subscribe to instruments - {e}")

    def on_start(self):
        # Calls the centralized function
        self.refresh_open_positions_and_buy_price()


    # ---------------------- SOCKET EVENT HANDLERS ----------------------

    # In your Trader_Singleton class

    def register_socket_handlers(self):
        @self.frontend_data_socket.on('connect')
        def handle_connect():
            logging.info("Client connected to socket server.")

        @self.frontend_data_socket.on('disconnect')
        def handle_disconnect():
            logging.info("Client disconnected.")

        @self.frontend_data_socket.on('request_weekly_options')
        def handle_request_weekly_options():
            """Handles request from the 'Options Widget' (widget_two)."""
            logging.info(
                "Received 'request_weekly_options' from client. Sending weekly contracts..."
            )
            self.frontend_data_socket.emit(
                'update_weekly_options',
                self._five_weekly_option_contracts
            )

        @self.frontend_data_socket.on('request_open_positions')
        def handle_request_open_positions():
            """Handles request from the 'Sell Positions Widget' (widget_four)."""
            logging.info(
                "Received 'request_open_positions' from client. Sending position data..."
            )
            # Emits an event that the sell widget is listening for
            self.frontend_data_socket.emit(
                'update_open_positions',
                self._position_data
            )

        @self.frontend_data_socket.on('place_sell_order')
        def handle_sell_order(order_details):
            """Handles a sell order request from the frontend."""
            logging.info(f"Received sell order from client: {order_details}")
            print(order_details)
            self._broker.sell_units(None , order_details["token"] , order_details["lots"])

        @self.frontend_data_socket.on('place_buy_order')
        def handle_buy_order(order_details):
            logging.info(f"Received buy order from client: {order_details}")
            print(order_details)
            self._broker.buy_units(None , order_details["token"] , order_details["lots"])