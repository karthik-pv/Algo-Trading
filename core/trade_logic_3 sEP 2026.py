import re
import threading
import logging
import queue
import datetime
import os
from typing import Optional , Dict , Any
from flask_socketio import SocketIO
from interface.broker_interface import BrokerInterface
from loguru import logger
import json

import time
import pythoncom
import win32com.client

from core.trade_utils import get_expiry_date , get_instrument_tokens_from_symbol , get_instrument_details_from_json , calculate_accurate_average_buy_price_and_update_positions
from core.shared_state import latest_tradingview_data


from utils import fetch_from_json , find_matching_object
import time

PNT_STOP_LOSS = float(fetch_from_json("settings.json", "PTS_LOSS"))
PNT_BOOK_PROFIT = float(fetch_from_json("settings.json", "PTS_PROFIT"))
PCT_BOOK_PROFIT = float(fetch_from_json("settings.json", "PCT_PROFIT"))
PCT_STOP_LOSS = float(fetch_from_json("settings.json", "PCT_LOSS"))
PTS_PROFIT_INTRA_FACTOR = float(fetch_from_json("settings.json", "PTS_PROFIT_INTRA_FACTOR"))
PTS_PROFIT_SCALPING_FACTOR = float(fetch_from_json("settings.json", "PTS_PROFIT_SCALPING_FACTOR"))
PTS_PROFIT_ULTRA_SCALPING_FACTOR = float(fetch_from_json("settings.json", "PTS_PROFIT_ULTRA_SCALPING_FACTOR"))

STOP_LOSS_PCT = float(fetch_from_json("settings.json", "STOP_LOSS_PCT"))
STOP_LOSS_INTRA = fetch_from_json("settings.json", "STOP_LOSS_INTRA")
STOP_LOSS_SCALPING = fetch_from_json("settings.json", "STOP_LOSS_SCALPING")
STOP_LOSS_ULTRA_SCALPING = fetch_from_json("settings.json", "STOP_LOSS_ULTRA_SCALPING")
POSITION_PNL_LOG_FREQ = fetch_from_json("settings.json", "POSITION_PNL_LOG_FREQ")


class Trader_Singleton:

    frontend_data_socket = SocketIO(cors_allowed_origins="*")

    _instance = None
    _broker = BrokerInterface
    
    _position_data = {}
    _five_weekly_option_contracts = {}
    _fund_summary = {}

    _near_month_data = {}
    _trading_watcher_thread_running = False
    _stop_event = threading.Event()
    _tick_counter = 0
    _comparison_function = fetch_from_json("settings.json" , "COMPARISON_FUNCTION")
    _use_max_margin = True
    _mode = fetch_from_json("settings.json" , "MODE")
    _broker_string = fetch_from_json("constants.json" , "BROKER")
    _exchange = fetch_from_json("constants.json" , "EXCHANGE")
    _underlying = fetch_from_json("constants.json" , "UNDERLYING")
    _near_month_future_symbol = fetch_from_json("constants.json" , "NEAR_MONTH_FUTURE_TOKEN")
    _buy_sell_together = bool(fetch_from_json("constants.json" , "BUY_SELL_TOGETHER"))

    _CASH_BALANCE_PAPER_TRADING = float(fetch_from_json("settings.json" , "CASH_BALANCE_PAPER_TRADING"))

    _NIFTY_FALLBACK_LTP = float(fetch_from_json("constants.json" , "NIFTY_FALLBACK_LTP"))
    _SENSEX_FALLBACK_LTP = float(fetch_from_json("constants.json" , "SENSEX_FALLBACK_LTP"))

    _near_month_future_ltp = 0.0
    open_near_month_future = 0.0
    _order_strategy_mapping = {}

    _cash_balance_margin_pct = fetch_from_json("settings.json" , "MARGIN_USAGE_PCT")

    _positions_to_subscribe = []
    
    _tradingView_Data = {}

    _latest_prices = {}
    _position_pnl_log_freq = POSITION_PNL_LOG_FREQ
    _last_position_pnl_log_ts = 0.0
    _last_freq_log_ts = {}
    _voice_announcement_enabled = True

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Trader_Singleton, cls).__new__(cls)
        return cls._instance

    def set_broker(self, broker):
        self._broker = broker


    def get_price_with_fallback(self, token, symbol):

        price = self.get_latest_price(token)

        if price:
            return price

        try:
            [price, _] = self._broker.get_quote(symbol)
            return price
        except:
            return 0        

    def wait_for_prices(self, tokens, timeout=8.0):
        """
        Wait briefly for websocket ticks to arrive before falling back to REST.
        Returns dictionary {token: price}
        """

        start = time.time()
        prices = {}
        remaining = set(str(token) for token in tokens)

        while time.time() - start < timeout:
            for token in list(remaining):
                price = self.get_latest_price(token)
                if price:
                    prices[token] = price
                    remaining.discard(token)

            if not remaining:
                break

            time.sleep(0.5)

        return prices
    
    def _get_position_key(self, token_or_symbol, is_token=True):
        """Helper to find the instrument token (the master key) from either token or trading symbol."""
        if is_token:
            return str(token_or_symbol)
        
        for token, data in self._position_data.items():
            if data.get("tradingsymbol") == token_or_symbol:
                return token
        return None
    
    def _calculate_option_lots(self, price: float, lot_size: Any) -> int:
        """Return a no-zero lot count for option rows, even when broker cash balance is unavailable."""
        try:
            safe_lot_size = int(lot_size)
        except (TypeError, ValueError):
            safe_lot_size = 1

        if price <= 0 or safe_lot_size <= 0:
            return 1

        cash_balance = float(self._fund_summary.get("cash_balance", 0) or 0)
        margin_pct = float(self._cash_balance_margin_pct or 1)

        if cash_balance <= 0:
            logger.warning(
                "Zero/invalid cash balance while calculating option lots; falling back to 1 lot. "
                f"Price={price}, lot_size={safe_lot_size}, margin_pct={margin_pct}"
            )
            return 1

        computed_lots = int(((cash_balance * margin_pct) / price) / safe_lot_size)

        if self._mode == "ALERT": 
            computed_lots = int(((self._CASH_BALANCE_PAPER_TRADING * margin_pct) / price) / safe_lot_size)

        return computed_lots
        # We dont want to return 1 lot if the computed_lots is 0. This is because we want to avoid placing orders when the cash balance is low and the computed lots is 0. Hence we will return 0 in such cases.
        #return max(1, computed_lots) 


    def _should_log_with_frequency(self, channel: str = "default") -> bool:
        freq_raw = str(self._position_pnl_log_freq or "TICK").strip().upper()

        if freq_raw == "TICK":
            return True

        try:
            interval_seconds = int(float(freq_raw))
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid POSITION_PNL_LOG_FREQ '{self._position_pnl_log_freq}'. Falling back to TICK logging."
            )
            self._position_pnl_log_freq = "TICK"
            return True

        if interval_seconds <= 0:
            return True

        now_ts = time.time()
        last_ts = self._last_freq_log_ts.get(channel, 0.0)
        if now_ts - last_ts >= interval_seconds:
            self._last_freq_log_ts[channel] = now_ts
            return True

        return False

    def _should_log_position_pnl(self) -> bool:
        return self._should_log_with_frequency("position_pnl")

    def weekly_options_initialize(self , token , data , call_or_put , price , level,open):
        
        if price <= 0:
            logger.warning(f"Invalid price {price} for {data['tradingsymbol']}. Skipping.")
            return
        
        token = str(token)
        logger.info(f"Initializing weekly option {data['tradingsymbol']} at LTP {price}")
        if token not in self._five_weekly_option_contracts:
            self._five_weekly_option_contracts[token] = {}
        
        self._five_weekly_option_contracts[token]["name"] = data["tradingsymbol"]
        self._five_weekly_option_contracts[token]["expiry"] = data["expiry"]
        self._five_weekly_option_contracts[token]["ltp"] = price
        self._five_weekly_option_contracts[token]["lotsize"] = data["lot_size"]
        logger.debug(f" Cash Balance {self._fund_summary.get('cash_balance', 0)} and Cash Balance Margin Pct {self._cash_balance_margin_pct} and Price {price} and Lot Size {data['lot_size']}")
        self._five_weekly_option_contracts[token]["lots"] = self._calculate_option_lots(price, data["lot_size"])
        logger.debug(f"Calculated lots is   {self._five_weekly_option_contracts[token]['lots']}")
        logger.info(f" Determining Lot size {int(data["lot_size"])}")
        self._five_weekly_option_contracts[token]["call_or_put"] = call_or_put
        self._five_weekly_option_contracts[token]["level"] = level
        self._five_weekly_option_contracts[token]["open"] = open

        payload = {
                    'token': token,
                    'name': data["tradingsymbol"],
                    'ltp': price,
                    'lots': self._five_weekly_option_contracts[token]["lots"]
                }

        self.frontend_data_socket.emit('price-updated-order', payload)

    
    def update_weekly_options(self, token: str, attribute_name: str, updated_value):
        #logger.info(f"Updating weekly option {token}: Setting '{attribute_name}' to {updated_value}")
        if token not in self._five_weekly_option_contracts:
            logger.warning(
                f"Token {token} not found in weekly option contracts. "
                f"Cannot update '{attribute_name}'."
            )
            return
        self._five_weekly_option_contracts[token][attribute_name] = updated_value
        lot_size = self._five_weekly_option_contracts[token].get("lotsize") or self._five_weekly_option_contracts[token].get("lot_size") or 1
        current_ltp = self._five_weekly_option_contracts[token].get("ltp") or self._five_weekly_option_contracts[token].get("price") or 0
        self._five_weekly_option_contracts[token]["lots"] = self._calculate_option_lots(current_ltp, lot_size)

        
        # logger.debug(
        #     f"Updated weekly option {token}: Set '{attribute_name}' to {updated_value}"
        # )
        #logger.debug(f"Updated weekly option {token}: Set '{attribute_name}' to {updated_value}")

       

    
    def position_data_update(self, token_or_symbol, attribute_name, updated_value, is_token=True):
        #logger.info(f"Updating position {token_or_symbol}: Setting '{attribute_name}' to {updated_value}")
        key_token = self._get_position_key(token_or_symbol, is_token)
        
        if key_token is None:
            logger.warning(f"Could not find position for {token_or_symbol}. Skipping update.")
            return
            
        if key_token not in self._position_data:
            self._position_data[key_token] = {}
            
        self._position_data[key_token][attribute_name] = updated_value
        
        if attribute_name == 'latest_price':
            position = self._position_data[key_token]
            buy_price = position.get('average_price', 0)
            net_qty = position.get('net_quantity', 0)
            lotsize = position.get('lotsize', 0)
            
            
            if buy_price > 0:
                # Recalculate and store the new P/L values
                pts_pl = self.calculate_point_difference(buy_price, updated_value)
                pct_pl = self.calculate_pctg_difference(buy_price, updated_value)
                total_pl = pts_pl * net_qty * lotsize
                if self._should_log_position_pnl():
                    logger.debug(f" For {token_or_symbol} : Buy Price {buy_price} , LTP {updated_value} , PTS : {pts_pl}, PCT {pct_pl:.2f}% and Total P/L {total_pl}. Position Grid Updated.")
                
                position['pts_pl'] = pts_pl
                position['pct_pl'] = pct_pl
                position['total_pl'] = total_pl
        
        #logger.debug(f"Updated {key_token}:{attribute_name} to {updated_value}")

    def update_fund_summary(self, fund_summary: Dict[str, Any]):
        logger.info("Updating fund summary...")
        for key , value in fund_summary.items():
            self._fund_summary[key] = float(value)
        logger.debug(f"Fund Summary updated to {self._fund_summary}")

    def refresh_open_pos_buy_price(self):
        logger.info("Refreshing open positions and buy prices...")
        # self._broker.unsubscribe_from_all(self.get_relevant_instruments_to_track())
        
        #How could we avoid this call here..as this results in multiple fund summary calls
        logger.info("Fetching latest fund summary from broker...")
        try:
            fund_summary = self._broker.fetch_fund_summary()

            if not fund_summary or not isinstance(fund_summary, dict):
                raise ValueError("Fund summary returned None or invalid data")
            
            for key, value in fund_summary.items():
                self._fund_summary[key] = float(value)

            logger.debug(f"Fund Summary fetched is {self._fund_summary}")
        except Exception as e:
            logger.error(f"Fund Summary fetch failed. Using fallback cash balance. Error: {e}", exc_info=True)
            # ✅ Fallback values
            self._fund_summary = {}
            self._fund_summary["cash_balance"] = 10000.0

            logger.warning("Fallback cash balance of 10000 assigned.")

        positions_from_broker = self._broker.fetch_all_positions()

        if isinstance(positions_from_broker, dict):
            positions_from_broker = positions_from_broker.get("net", [])

        if not positions_from_broker:
            logger.warning("positions_from_broker is None or empty — skipping avg price calculation")
            self._position_data.clear()
            try:
                if not self.frontend_data_socket:
                    raise ConnectionRefusedError
                self.frontend_data_socket.emit('update_open_positions', self._position_data)
                self.frontend_data_socket.emit('update_weekly_options', self._five_weekly_option_contracts)
                self.frontend_data_socket.emit('status_message', {"success": True, "message": "Refreshed position ..."})
            except Exception:
                logger.error("Frontend socket yet to be instantiated")
            logger.info(f"Updated positions: {len(self._position_data)}")
            return

        orders_from_broker = self._broker.fetch_all_orders()
        if not orders_from_broker:
            logger.info("Broker returned None for orders. Using empty list.")
            orders_from_broker = []
        logger.debug(f"positions_from_broker is {positions_from_broker}")
        positions = calculate_accurate_average_buy_price_and_update_positions(positions_from_broker , orders_from_broker)
        logger.debug(f"positions after calculating average buy price is  {positions}")

        if not positions_from_broker:
            logger.info("Broker returned None for positions. Using empty list.")
            positions_from_broker = []

        new_position_tokens = {str(pos["instrument_token"]) for pos in positions}
        
        tokens_to_remove = [
            token for token in self._position_data 
            if token not in new_position_tokens
        ]
        for token in tokens_to_remove:
            del self._position_data[token]
            
        for position in positions: # Assuming this is the accurately calculated 'positions' list
            token = str(position["instrument_token"])

            logger.debug(f"POS CHECK {position}")
            
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
                # "lots": int(net_qty / lotsize), MSTOCK COUPLED CODE 
                "lots" : net_qty,
                # ADDED: Initial P/L fields
                "pts_pl": pts_pl,
                "pct_pl": pct_pl,
                "total_pl": total_pl,
                "order_strategy" : self._order_strategy_mapping.get(position["instrument_token"], "ULTRA_SCALPING")
            })
            # --- END MODIFIED BLOCK ---

            
        # self._broker.subscribe_to_all(self.get_relevant_instruments_to_track())
        try:
            if not self.frontend_data_socket:
                raise ConnectionRefusedError
            self.frontend_data_socket.emit(
                    'update_open_positions',
                    self._position_data
                )
            #PUTTA, why are we emitting socket even though there was no change done to _five_weekly_option_contracts?
            #How should we recalculate the lot size post the buy transaction with the the latest fund summary data?
            #How was it (recalculated lot size for five_weekly_options post the buy/sell transactions) working early? 
            #Can we compare this version of the file with the your latest git version
            self.frontend_data_socket.emit(
                    'update_weekly_options',
                    self._five_weekly_option_contracts
                )
            self.frontend_data_socket.emit('status_message',{"success": True, "message": "Refreshed position ..."}
)
        except Exception as e:
            logger.error("Frontend socket yet to be instantiated")
        logger.info(f"Updated positions: {len(self._position_data)}")
        logger.debug(self._position_data)
        

        

    def get_relevant_instruments_to_track(self):
        logger.info("Compiling list of relevant instruments to track: 1) Positions, 2) 5 Weekly Options and 3) Near Month Future")
        """Returns a list of instrument tokens currently in the _position_data."""
        instruments = list(self._position_data.keys())
        instruments.extend(self._five_weekly_option_contracts.keys())
        instruments.extend(self._near_month_data.keys())
        logger.info(f"Subscribing to {len(instruments)} instruments.")
        return instruments

    # ---------------------- TRADING LOGIC (Refactored) ----------------------

    def voice_readout_core(self):
        logger.info("Started voice readout thread")
        pythoncom.CoInitialize()
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        SVSFlagsAsync = 1 
        SVSFPurgeBeforeSpeak = 2

        def format_pct_pl_for_voice(pct_pl):
            if pct_pl is None:
                return None

            if abs(pct_pl) < 5:
                formatted = f"{pct_pl:.1f}"
            else:
                formatted = str(int(round(pct_pl)))

            if formatted.startswith("-"):
                formatted = formatted.replace("-", "minus ")

            return formatted

        while not self._stop_event.is_set():
            if not self._voice_announcement_enabled:
                if speaker.Status.RunningState == 2:
                    try:
                        speaker.Speak("", SVSFPurgeBeforeSpeak)
                    except Exception:
                        pass
                self._stop_event.wait(timeout=0.25)
                continue

            if self._position_data:
                # Check if the speaker is currently busy (RunningState 2 means it is speaking)
                # If it's done (RunningState 1), give it the newest value.
                if speaker.Status.RunningState != 2:
                    for token, data in list(self._position_data.items()):
                        pct_pl = data.get("pct_pl")
                        rounded_pl = format_pct_pl_for_voice(pct_pl)
                        if rounded_pl is not None:
                            speaker.Speak(rounded_pl, SVSFlagsAsync)
            
            # Wait for 1 second before evaluating again
            self._stop_event.wait(timeout=1.0)
    def stop_loss_book_profit_core(self, broker: BrokerInterface):
        logger.info("Started trading watcher thread")
        
        

        while not self._stop_event.is_set():
            # logger.debug("Evaluating open positions for stop-loss/book-profit...")
            # logger.debug(self._position_data)
            #logger.debug(f"Total Open Positions to Evaluate: {len(self._position_data)}")
            for token , data in list(self._position_data.items()):
                should_log_eval = self._should_log_with_frequency(f"stop_loss_eval:{token}")
                if should_log_eval:
                    logger.debug(f"Evaluating position for token {token}")
                    logger.debug(f"Iterating Position records: {data}")
                
                qty = data.get("lots")
                buy_price = data.get("average_price")
                ltp = data.get("latest_price")
                exchange = self._exchange
                tradingsymbol = data.get("tradingsymbol")
                instrument_token = data.get("instrument_token")
                order_strategy_type = data.get("order_strategy" , "DEFAULT")
                #sell_type = 
                

                match = re.search(r'(CE|PE)$', tradingsymbol.upper())
                contract_type = match.group(1) if match else None

                if should_log_eval:
                    logger.debug(f"Order Strategy Type set is {order_strategy_type}")
                
                # For now just running the check for the Stop Loss..As we would be resorting to book profit undeterministically
                # where in As soon as Buy Order is raised Sell Order is also raised with Sell Price = LTP + x (Limit Order)
                # Refactor this change where this could be driven by the settings.json parameters
                # Ultra Scalping is determined by 15s MACD2 Swing Set Up where in we are capturing 1s MACD2  + 5s Stochastic Swing

                # This trigger check is only applicable when Sell Type = T. For other cases U and D system raises Sell Order immediately after Buy Order is executed

                #sell = self.to_sell_or_not_to_sell(buy_price , ltp , order_strategy_type,contract_type, should_log_eval)
                sell = self.to_sell_or_not_to_sell_SL(buy_price , ltp , order_strategy_type,contract_type, should_log_eval)
                
                if should_log_eval:
                    logger.debug(f"Decision To Sell {tradingsymbol} - {sell}")
                # Execute Sell Order if these conditions are met
                # 1. If sell is True and Sell Mode is TRIGGER_SELL and LTP > Buy Price (Book Profit Scenario)
                # 2. If sell is True and LTP < Buy Price (Stop Loss Scenario). In this case, Sell Mode can be anything. We dont expect TRIGGER_SELL to be set here.
                
                
                if self._mode == "EXECUTION":
                    if sell:
                        logger.info(f"Placing SELL order for {tradingsymbol} {instrument_token} : {qty} lots at LTP {ltp}")
                        broker.sell_units(tradingsymbol,instrument_token,qty,exchange,ltp)
                    else:
                        if should_log_eval:
                            logger.debug(f"No SELL action taken for {tradingsymbol} at LTP {ltp}")
                else:
                    if should_log_eval:
                        logger.debug(f"ALERT: Sell conditions met for {tradingsymbol} at LTP {ltp} - Decision To Sell: {sell}")

                    filename = "PaperTrading.txt"

                    # Create header if file doesn't exist
                    if not os.path.exists(filename):
                        with open(filename, "w", encoding="utf-8") as f:
                            f.write(
                                "Time\tType\tInstrument\tToken\tProduct\tQty.\tAvg. price\tStatus\n"
                            )

                    timestamp = datetime.now().strftime("%-m/%-d/%Y %H:%M")
                    # On Windows use:
                    # timestamp = datetime.now().strftime("%#m/%#d/%Y %H:%M")

                    with open(filename, "a", encoding="utf-8") as f:
                        f.write(
                            f"{timestamp}\t"
                            f"SELL\t"
                            f"{tradingsymbol}\t"
                            f"{instrument_token}\t"
                            f"MIS\t"
                            f"{qty}/{qty}\t"
                            f"{ltp}\t"
                            f"COMPLETE\n"
                        )                    

                self._stop_event.wait(timeout=2)
    
    def stop_loss_core(self, broker: BrokerInterface):
        logger.info("Started trading watcher thread - For Just Stop Loss")
        while not self._stop_event.is_set():
            # logger.debug("Evaluating open positions for stop-loss/book-profit...")
            # logger.debug(self._position_data)
            #logger.debug(f"Total Open Positions to Evaluate: {len(self._position_data)}")

            for token , data in list(self._position_data.items()):
                should_log_eval = self._should_log_with_frequency(f"stop_loss_eval:{token}")
                if should_log_eval:
                    logger.debug(f"Evaluating position for token {token}")
                    logger.debug(f"Iterating Position records: {data}")
                
                qty = data.get("lots")
                buy_price = data.get("average_price")
                ltp = data.get("latest_price")
                exchange = self._exchange
                tradingsymbol = data.get("tradingsymbol")
                instrument_token = data.get("instrument_token")
                order_strategy_type = data.get("order_strategy" , "DEFAULT")
                if should_log_eval:
                    logger.debug(f"Order Strategy Type set is {order_strategy_type}")
                
                sell = self.to_sell_or_not_to_sell(buy_price , ltp , order_strategy_type, should_log_eval)
                if should_log_eval:
                    logger.debug(f"Decision To Sell {tradingsymbol} - {sell}")
                # Execute Sell Order if these conditions are met
                # 1. If sell is True and Sell Mode is TRIGGER_SELL and LTP > Buy Price (Book Profit Scenario)
                # 2. If sell is True and LTP < Buy Price (Stop Loss Scenario). In this case, Sell Mode can be anything. We dont expect TRIGGER_SELL to be set here.
                
                if self._mode == "EXECUTION":
                    if should_log_eval:
                        logger.debug("In EXECUTION Mode")
                    if ((sell and (ltp > buy_price)) or (sell and ltp < buy_price)):
                        logger.info(f"Placing SELL order for {tradingsymbol} {instrument_token} : {qty} lots at LTP {ltp}")
                        broker.sell_units(tradingsymbol,instrument_token,qty,exchange,ltp)
                    else:
                        if should_log_eval:
                            logger.debug(f"No SELL action taken for {tradingsymbol} at LTP {ltp}")

                else:
                    if should_log_eval:
                        logger.debug("In ALERT Mode")
                        logger.debug(f"ALERT: Sell conditions met for {tradingsymbol} at LTP {ltp} - Decision To Sell: {sell}")

                    write_paper_trade(transaction_type="SELL",tradingsymbol=tradingsymbol,token=instrument_token,qty=qty["lots"],ltp=ltp,product="MIS")

                    filename = "PaperTrading.txt"

                    # Create header if file doesn't exist
                    if not os.path.exists(filename):
                        with open(filename, "w", encoding="utf-8") as f:
                            f.write(
                                "Time\tType\tInstrument\tToken\tProduct\tQty.\tAvg. price\tStatus\n"
                            )

                    timestamp = datetime.datetime.now().strftime("%-m/%-d/%Y %H:%M")
                    # On Windows use:
                    # timestamp = datetime.now().strftime("%#m/%#d/%Y %H:%M")

                    with open(filename, "a", encoding="utf-8") as f:
                        f.write(
                            f"{timestamp}\t"
                            f"SELL\t"
                            f"{tradingsymbol}\t"
                            f"MIS\t"
                            f"{qty}/{qty}\t"
                            f"{ltp}\t"
                            f"COMPLETE\n"
                        )

                self._stop_event.wait(timeout=5)


    # ---------------------- DECISION MAKER ----------------------------

    def to_sell_or_not_to_sell(self,buy_price , current_price , order_strategy_type, log_enabled=True):
        # that is the question 
        if log_enabled:
            logger.debug(f"Deciding to sell or not: Buy Price = {buy_price}, Current Price = {current_price} and Order Strategy Type is {order_strategy_type}")
        PROFIT_FACTOR = 1

        if order_strategy_type == "INTRA":
            PROFIT_FACTOR = PTS_PROFIT_INTRA_FACTOR
        elif order_strategy_type == "SCALPING":
            PROFIT_FACTOR = PTS_PROFIT_SCALPING_FACTOR
        elif order_strategy_type == "ULTRA_SCALPING":
            PROFIT_FACTOR = PTS_PROFIT_ULTRA_SCALPING_FACTOR
        
        if log_enabled:
            logger.debug(f"Profit factor is {PROFIT_FACTOR} and Compare Function is {self._comparison_function}")
        
        if "PCT" in self._comparison_function:
            diff = self.calculate_pctg_difference(buy_price,current_price)
            if log_enabled:
                logger.debug(f"In PCT Mode and Diff is {diff}")
            if current_price > buy_price and PCT_BOOK_PROFIT >0 :
                if diff >= PCT_BOOK_PROFIT:
                    if log_enabled:
                        logger.debug("Returning True for Booking Profit")
                    return True
            if current_price < buy_price and PCT_STOP_LOSS >0 :
                if abs(diff) >= PCT_STOP_LOSS:
                    if log_enabled:
                        logger.debug(f"Returning True for Booking Loss {abs(diff)} is greater than Stop Loss Pct {PCT_STOP_LOSS}")
                    return True
            return False
        if "PNT" in self._comparison_function:
            diff = self.calculate_point_difference(buy_price,current_price)
            if log_enabled:
                logger.debug(f"In PNT Mode and Diff is {diff}")
            #if PNT_BOOK_PROFIT is 0, then SELL TRIGGER never triggered for PROFIT. User has to book the profit manually. Sell Trigger is only then for STOP LOSS
            if current_price > buy_price and PNT_BOOK_PROFIT > 0 :
                if diff >= PNT_BOOK_PROFIT*PROFIT_FACTOR:
                    if log_enabled:
                        logger.debug("Returning True for Booking Profit")
                    return True
            if current_price < buy_price and PNT_STOP_LOSS > 0 :
                if abs(diff) >= PNT_STOP_LOSS:
                    if log_enabled:
                        logger.debug("Returning True for Booking Loss")
                    return True
            return False
        
    #BADD
    def to_sell_or_not_to_sell_SL(self,buy_price , current_price , order_strategy_type,contract_type, log_enabled=True):
        # that is the question 
        if log_enabled:
            logger.debug(f"Deciding to sell or not for Stop Loss: Buy Price = {buy_price}, Current Price = {current_price} and Order Strategy Type is {order_strategy_type}")


        #logger.debug(f"STOP_LOSS settings : STOP_LOSS_ULTRA_SCALPING = {STOP_LOSS_ULTRA_SCALPING} , STOP_LOSS_SCALPING = {STOP_LOSS_SCALPING} , STOP_LOSS_INTRA = {STOP_LOSS_INTRA} and STOP_LOSS_PCT = {STOP_LOSS_PCT} ")
        #logger.debug(f"TradingView Data used for Stop Loss Decision : {self._tradingView_Data}")

        # if self._tradingView_Data:
        #     logger.info("TradingView Data is NOT empty. Can make Stop Loss decision based on indicators. So proceeding with checks")
        #     logger.debug(f"Order Strategy Type is {order_strategy_type} and Contract Type is {contract_type}" )


        #     if order_strategy_type == "INTRA":
        #         if contract_type == "CE":
        #             if STOP_LOSS_INTRA == "1M_MACD":
        #                 if self._tradingView_Data["INDICATORS"]["1M"]["Trend_2452"] == "-1":
        #                     logger.debug("Returning True for Booking Loss as 1m MACD has crossed down.")
        #                     return True
        #                 else:
        #                     logger.debug(f"1m MACD condition for Stop Loss not met. Trend_2452:{ self._tradingView_Data['INDICATORS']['1M']['Trend_2452']}") 
        #                     return False
        #         elif contract_type == "PE":
        #             if STOP_LOSS_INTRA == "1M_MACD":
        #                 if self._tradingView_Data["INDICATORS"]["1M"]["Trend_2452"] == "1":
        #                     logger.debug("Returning True for Booking Loss as 1m MACD has crossed up.")
        #                     return True
        #                 else:
        #                     logger.debug(f"1m MACD condition for Stop Loss not met. Trend_2452:{ self._tradingView_Data['INDICATORS']['1M']['Trend_2452']}") 
        #                     return False
        #     elif order_strategy_type == "SCALPING":
        #         if contract_type == "CE":
        #             if STOP_LOSS_SCALPING == "15S_MACD":
        #                 if self._tradingView_Data["INDICATORS"]["15S"]["Trend_2452"] == "-1":
        #                     logger.debug("Returning True for Booking Loss as 15s MACD has crossed down.")
        #                     return True
        #                 else:
        #                     logger.debug(f"15s MACD condition for Stop Loss not met. Trend_2452:{ self._tradingView_Data['INDICATORS']['15S']['Trend_2452']}") 
        #                     return False
        #         elif contract_type == "PE":
        #             if STOP_LOSS_SCALPING == "15S_MACD":
        #                 if self._tradingView_Data["INDICATORS"]["15S"]["Trend_2452"] == "1":
        #                     logger.debug("Returning True for Booking Loss as 15s MACD has crossed up.")
        #                     return True
        #                 else:
        #                     logger.debug(f"15s MACD condition for Stop Loss not met. Trend_2452:{ self._tradingView_Data['INDICATORS']['15S']['Trend_2452']}") 
        #                     return False
        #     elif order_strategy_type == "ULTRA_SCALPING":
        #         if contract_type == "CE":
        #             if STOP_LOSS_ULTRA_SCALPING == "5S_MACD":
        #                 if float(self._tradingView_Data["INDICATORS"]["5S"]["Trend_2452"]) == -1:
        #                     logger.debug("Returning True for Booking Loss as 5s MACD has crossed down.")
        #                     return True
        #                 else:
        #                     logger.debug(f"5s MACD condition for Stop Loss not met. Trend_2452:{ self._tradingView_Data['INDICATORS']['5S']['Trend_2452']}") 
        #                     return False
        #             elif STOP_LOSS_ULTRA_SCALPING == "15S_MACD":
        #                 if float(self._tradingView_Data["INDICATORS"]["15S"]["Trend_2452"]) == -1:
        #                     logger.debug("Returning True for Booking Loss as 15s MACD has crossed down.")
        #                     return True
        #                 else:
        #                     logger.debug(f"15s MACD condition for Stop Loss not met. Trend_2452:{ self._tradingView_Data['INDICATORS']['15S']['Trend_2452']}") 
        #                     return False
        #             elif STOP_LOSS_ULTRA_SCALPING == "1M_STOCH":
        #                 if float(self._tradingView_Data["INDICATORS"]["1M"]["KTrendFlag"]) == -1:
        #                     logger.debug("Returning True for Booking Loss as 1m Stoch has crossed down.")
        #                     return True
        #                 else:
        #                     logger.debug(f"1m Stoch condition for Stop Loss not met. KTrendFlag:{ self._tradingView_Data['INDICATORS']['1M']['KTrendFlag']}") 
        #                     return False
        #             elif STOP_LOSS_ULTRA_SCALPING == "1M_EMA50":
        #                 if float(current_price) < float(self._tradingView_Data["INDICATORS"]["1M"]["EMA50"]):
        #                     logger.debug(f"Returning True for Booking Loss as Current price: {current_price} has gone below 1m EMA50 {self._tradingView_Data['INDICATORS']['1M']['EMA50']}.")
        #                     return True
        #                 else:
        #                     logger.debug(f"1m EMA50 condition for Stop Loss not met. Current Price :{current_price } is still above 1m EMA50 is {self._tradingView_Data['INDICATORS']['1M']['EMA50']} ") 
        #                     return False
        #             elif STOP_LOSS_ULTRA_SCALPING == "1M_MACD2":
        #                 if float(self._tradingView_Data["INDICATORS"]["1M"]["Trend_2452"]) == -1:
        #                     logger.debug("Returning True for Booking Loss as 1M MACD2 has crossed down.")
        #                     return True
        #                 else:
        #                     logger.debug(f"1M MACD2 condition for Stop Loss not met. Trend_2452:{ self._tradingView_Data['INDICATORS']['1M']['Trend_2452']}") 
        #                     return False
        #             elif STOP_LOSS_ULTRA_SCALPING == "5M_PVT":
        #                 if float(self._tradingView_Data["INDICATORS"]["5M"]["PVT"]) < 0:
        #                     logger.debug("Returning True for Booking Loss as 5M PVT is negative.")
        #                     return True
        #                 else:
        #                     logger.debug(f"5M PVT condition for Stop Loss not met. PVT:{ self._tradingView_Data['INDICATORS']['5M']['PVT']}") 
        #                     return False
        #         elif contract_type == "PE":
        #             if STOP_LOSS_ULTRA_SCALPING == "5S_MACD":
        #                 if float(self._tradingView_Data["INDICATORS"]["5S"]["Trend_2452"]) == 1:
        #                     logger.debug("Returning True for Booking Loss as 5s MACD has crossed up.")
        #                     return True
        #                 else:
        #                     logger.debug(f"5s MACD condition for Stop Loss not met. Trend_2452:{ self._tradingView_Data['INDICATORS']['5S']['Trend_2452']}") 
        #                     return False
        #             elif STOP_LOSS_ULTRA_SCALPING == "15S_MACD":
        #                 if int(self._tradingView_Data["INDICATORS"]["15S"]["Trend_2452"]) == 1:
        #                     logger.debug("Returning True for Booking Loss as 15s MACD has crossed up.")
        #                     return True
        #                 else:
        #                     logger.debug(f"15s MACD condition for Stop Loss not met. Trend_2452:{ self._tradingView_Data["INDICATORS"]["15S"]["Trend_2452"]}") 
        #                     return False
        #             elif STOP_LOSS_ULTRA_SCALPING == "1M_STOCH":
        #                 if float(self._tradingView_Data["INDICATORS"]["1M"]["KTrendFlag"]) == 1:
        #                     logger.debug("Returning True for Booking Loss as 1m Stoch has crossed Up.")
        #                     return True
        #                 else:
        #                     logger.debug(f"1m Stoch condition for Stop Loss not met. KTrendFlag:{ self._tradingView_Data['INDICATORS']['1M']['KTrendFlag']}") 
        #                     return False
        #             elif STOP_LOSS_ULTRA_SCALPING == "1M_EMA50":
        #                 if float(current_price) > float(self._tradingView_Data["INDICATORS"]["1M"]["EMA50"]):
        #                     logger.debug(f"Returning True for Booking Loss as Current price: {current_price} has gone above 1m EMA50 {self._tradingView_Data['INDICATORS']['1M']['EMA50']}.")
        #                     return True
        #                 else:
        #                     logger.debug(f"1m EMA50 condition for Stop Loss not met. Current Price :{current_price } is still below 1m EMA50 is {self._tradingView_Data['INDICATORS']['1M']['EMA50']} ") 
        #                     return False
        #             elif STOP_LOSS_ULTRA_SCALPING == "1M_MACD2":
        #                 if float(self._tradingView_Data["INDICATORS"]["1M"]["Trend_2452"]) == 1:
        #                     logger.debug("Returning True for Booking Loss as 1M MACD2 has crossed up.")
        #                     return True
        #                 else:
        #                     logger.debug(f"1M MACD2 condition for Stop Loss not met. Trend_2452:{ self._tradingView_Data['INDICATORS']['1M']['Trend_2452']}") 
        #                     return False                        
        #             elif STOP_LOSS_ULTRA_SCALPING == "5M_PVT":
        #                 if float(self._tradingView_Data["INDICATORS"]["5M"]["PVT"]) > 0:
        #                     logger.debug("Returning True for Booking Loss as 5M PVT is positive.")
        #                     return True
        #                 else:
        #                     logger.debug(f"5M PVT condition for Stop Loss not met. PVT:{ self._tradingView_Data['INDICATORS']['5M']['PVT']}") 
        #                     return False
        # else:
        #     logger.warning("TradingView Data is empty. Cannot make Stop Loss decision based on indicators. So proceeding with STOP_LOSS_PCT check")
        
        # This will be the fall back Stop Loss based on PCT difference
        # Orders will be one of the above types : Intra , Scalping , Ultra Scalping...Even if 1m/15s/5s MACD Swing dont trigger the Stop Loss we rely on the STOP_LOSS_PCT parameter
        # logger.debug("Falling back to STOP_LOSS_PCT based Stop Loss Check")
        if log_enabled:
            logger.debug(f"STOP_LOSS_PCT based Stop Loss Check. Buy Price is {buy_price} and Current Price is {current_price} and STOP_LOSS_PCT is {STOP_LOSS_PCT}")
        if float(STOP_LOSS_PCT) > 0 and current_price < buy_price :
            diff = abs(self.calculate_pctg_difference(buy_price,current_price))
            if log_enabled:
                logger.debug(f"PCT Diff between BUY and Current Market Price is {diff} and STOP_LOSS_PCT is {STOP_LOSS_PCT}")
            if diff >= float(STOP_LOSS_PCT):
                if log_enabled:
                    logger.debug("Returning True for Booking Loss.STOP_LOSS_PCT triggered.")
                return True
        if log_enabled:
            logger.debug("Returning False for Booking Loss")
        return False

    # ---------------------- GENERATE OTMS/ITMS ------------------------------    
        
    def get_5_woc(
        self,
        nifty_price: float,
        call_or_put: str,
        strike_interval: int = 100,
        underlying: str = "NIFTY"
    ) -> Dict[str, any]:
        logger.info(f"Generating 5 weekly option contracts for {underlying} at price {nifty_price} ({call_or_put})")
        if strike_interval <= 0:
            raise ValueError("strike_interval must be positive integer")
        
        atm_strike = int((nifty_price // strike_interval) * strike_interval)
        remainder = nifty_price%100
        if remainder >= 50:
            atm_strike+=100
        

        expiry = get_expiry_date(underlying)

        logger.info(f"Calculated ATM Strike: {atm_strike}, Expiry Date: {expiry}")

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
        
        logger.debug(f"Initial strikes calculated: {strikes}")

        for k, v in strikes.items():
            if v is None or v < 0:
                strikes[k] = None

        symbols = {
            k: (self._broker.format_option_symbol(underlying, expiry, v, call_or_put) if v is not None else None)
            for k, v in strikes.items()
        }

        return {"expiry": expiry, "strikes": strikes, "symbols": symbols}



    def setup_woc_subscriptions(self):

        logger.info("Setting up weekly option contract subscriptions...")

        try:

            near_month_symbol = fetch_from_json("constants.json", "NEAR_MONTH_FUTURE_TOKEN")

            near_month_data = self._broker.get_instrument_details(near_month_symbol)

            near_month_token = str(near_month_data["instrument_token"])


            self._broker.subscribe_to_all([near_month_token])
            price = self.get_latest_price(str(near_month_token))
            if not price:
                [price, open] = self._broker.get_quote(near_month_data["tradingsymbol"])
            if not price:
                logger.info("Near Month Future REST quote failed. Using fallback price " + self._NIFTY_FALLBACK_LTP)
                price = self._NIFTY_FALLBACK_LTP



            # ------------------------------------
            # Step 1: Generate all contracts first
            # ------------------------------------
            #float(self._NIFTY_FALLBACK_LTP),
            contracts_ce = self.get_5_woc(
                price,   
                "CE",
                underlying=self._underlying
            )["symbols"]

            contracts_pe = self.get_5_woc(
                price,   
                "PE",
                underlying=self._underlying
            )["symbols"]

            all_contracts = {}

            tokens_to_subscribe = [near_month_token]


            for key, value in contracts_ce.items():

                data = self._broker.get_instrument_details(value)

                token = str(data["instrument_token"])

                tokens_to_subscribe.append(token)

                all_contracts[token] = ("CE", key, data)


            for key, value in contracts_pe.items():

                data = self._broker.get_instrument_details(value)

                token = str(data["instrument_token"])

                tokens_to_subscribe.append(token)

                all_contracts[token] = ("PE", key, data)


            logger.info(f"Subscribing to {len(tokens_to_subscribe)} instruments")

            # ------------------------------------
            # Step 2: Subscribe once
            # ------------------------------------

            self._broker.subscribe_to_all(tokens_to_subscribe)


            # ------------------------------------
            # Step 3: Wait once for websocket
            # ------------------------------------

            prices = self.wait_for_prices(tokens_to_subscribe)

            # ------------------------------------
            # Step 3.5: Batch REST for missing prices
            # ------------------------------------

            missing_symbols = []

            for token in tokens_to_subscribe:

                if not prices.get(token):

                    if token == near_month_token:

                        missing_symbols.append(near_month_data["tradingsymbol"])

                    else:

                        missing_symbols.append(all_contracts[token][2]["tradingsymbol"])


            if missing_symbols:

                logger.info(f"{len(missing_symbols)} prices missing from websocket. Fetching batch REST.")

                batch_quotes = self._broker.get_quotes_batch(missing_symbols)

            else:

                batch_quotes = {}


            # ------------------------------------
            # Step 4: Handle Near Month Future
            # ------------------------------------

            price = prices.get(near_month_token)

            if not price:

                q = batch_quotes.get(near_month_data["tradingsymbol"])

                if q:
                    price, open_price = q

            if not price:

                logger.warning("Future REST quote failed. Using fallback")

                price = self._NIFTY_FALLBACK_LTP

                open_price = price


            self.ltp_near_month_future = price

            self.open_near_month_future = open_price

            chgO = (price - open_price) * 100 / open_price


            self.frontend_data_socket.emit(
                "near-month-ltp-updated",
                {"ltp": price, "chgO": chgO}
            )

            self._near_month_data[near_month_token] = price


            # ------------------------------------
            # Step 5: Initialize options
            # ------------------------------------

            

            for token, (cp, level, data) in all_contracts.items():

                price = prices.get(token)

                if not price:

                    logger.debug(f"Websocket price missing for {data['tradingsymbol']}; checking REST fallback")

                q = batch_quotes.get(data["tradingsymbol"])

                if q:
                    price = q[0]

                if not price:

                    logger.info(f"REST quote unavailable for {data['tradingsymbol']}; using fallback price 100")

                    price = 100


                self.weekly_options_initialize(
                    token,
                    data,
                    cp,
                    price,
                    level=level,
                    open=price
                )

                self._order_strategy_mapping[token] = "ULTRA_SCALPING"


            logger.info("Weekly options setup completed")


            self.frontend_data_socket.emit(
                "update_weekly_options",
                self._five_weekly_option_contracts
            )


        except Exception as e:

            logger.error(f"Exception in setting up weekly options - {e}")


    # def setup_woc_subscriptions(self):
    #     logger.info("Setting up weekly option contract subscriptions...")
    #     try:

    #         near_month_symbol = fetch_from_json("constants.json" , "NEAR_MONTH_FUTURE_TOKEN")

    #         near_month_data = self._broker.get_instrument_details(near_month_symbol)
    #         near_month_token = str(near_month_data["instrument_token"])

    #         logger.debug(f"Near Month Future Symbol: {near_month_symbol}, Token: {near_month_token}")

    #         # ------------------------------------------------
    #         # Subscribe to near month future first
    #         # ------------------------------------------------

    #         self._broker.subscribe_to_all([near_month_token])

    #         price = self.get_latest_price(str(near_month_token))

    #         if not price:
    #             [price, open] = self._broker.get_quote(near_month_data["tradingsymbol"])
            
    #         if not price:
    #             price = self._NIFTY_FALLBACK_LTP

    #         self.ltp_near_month_future = price
    #         self.open_near_month_future = price

    #         logger.info(
    #             f"Fetched Near Month Future LTP: {self.ltp_near_month_future}"
    #         )

    #         chgO = (self.ltp_near_month_future - self.open_near_month_future)*100 / self.open_near_month_future

    #         self.frontend_data_socket.emit(
    #             "near-month-ltp-updated",
    #             {"ltp": self.ltp_near_month_future,"chgO":chgO}
    #         )

    #         self._near_month_data[near_month_token] = self.ltp_near_month_future

    #         # ------------------------------------------------
    #         # Generate CE contracts
    #         # ------------------------------------------------

    #         contracts = self.get_5_woc(
    #             float(self.ltp_near_month_future),
    #             "CE",
    #             underlying=self._underlying
    #         )["symbols"]

    #         logger.log("DATA",f" Weekly Option Contracts - {contracts}")

    #         tokens_to_subscribe = []

    #         contract_details = {}

    #         for key, value in contracts.items():

    #             data = self._broker.get_instrument_details(value)

    #             token = str(data["instrument_token"])

    #             tokens_to_subscribe.append(token)

    #             contract_details[key] = (token, data)

    #         # subscribe all CE tokens
    #         self._broker.subscribe_to_all(tokens_to_subscribe)

    #         for key, (token, data) in contract_details.items():

    #             price = self.get_latest_price(token)

    #             if not price:
    #                 price, _ = self._broker.get_quote(data["tradingsymbol"])

    #             if not price:
    #                 price = 100   # fallback option price

    #             open_price = price

    #             self.weekly_options_initialize(
    #                 token,
    #                 data,
    #                 "CE",
    #                 price,
    #                 level=key,
    #                 open=open_price
    #             )

    #             self._order_strategy_mapping[token] = "ULTRA_SCALPING"

    #         logger.info("Completed CALL weekly options setup.")

    #         # ------------------------------------------------
    #         # Generate PE contracts
    #         # ------------------------------------------------

    #         contracts = self.get_5_woc(
    #             self.ltp_near_month_future,
    #             "PE",
    #             underlying=self._underlying
    #         )["symbols"]

    #         tokens_to_subscribe = []

    #         contract_details = {}

    #         for key, value in contracts.items():

    #             data = self._broker.get_instrument_details(value)

    #             token = str(data["instrument_token"])

    #             tokens_to_subscribe.append(token)

    #             contract_details[key] = (token, data)

    #         self._broker.subscribe_to_all(tokens_to_subscribe)

    #         for key, (token, data) in contract_details.items():

    #             price = self.get_latest_price(token)

    #             if not price:
    #                 price, _ = self._broker.get_quote(data["tradingsymbol"])

    #             if not price:
    #                 price = 100

    #             open_price = price

    #             self.weekly_options_initialize(
    #                 token,
    #                 data,
    #                 "PE",
    #                 price,
    #                 level=key,
    #                 open=open_price
    #             )

    #             self._order_strategy_mapping[token] = "ULTRA_SCALPING"

    #         logger.info("Completed PUT weekly options setup.")
    #         # Send the full weekly options table to frontend grid
    #         self.frontend_data_socket.emit(
    #             "update_weekly_options",
    #             self._five_weekly_option_contracts
    #         )            

    #     except Exception as e:
    #         logger.error(f"Exception in setting up weekly options - {e}")


    # def setup_woc_subscriptions(self):
    #     logger.info("Setting up weekly option contract subscriptions...")
    #     try:    
    #         near_month_symbol = fetch_from_json("constants.json" , "NEAR_MONTH_FUTURE_TOKEN")
    #         near_month_token = self._broker.get_instrument_details(near_month_symbol)["instrument_token"]

    #         self._broker.subscribe_to_all([near_month_token])

    #         logger.debug(f"Near Month Future Symbol: {near_month_symbol}, Token: {near_month_token}")
    #         logger.debug(f"Broker object: {self._broker}")
    #         logger.debug(f"Broker class: {self._broker.__class__}")
    #         logger.debug(f"Broker methods: {dir(self._broker)}")
            
    #         #Avoiding Rest Call
    #         #[self.ltp_near_month_future,self.open_near_month_future] = self._broker.get_quote(near_month_symbol)

    #         while not self.get_latest_price(str(near_month_token)):
    #             logger.info(f"Waiting for LTP of Near Month Future ({near_month_symbol}) to be available...")
    #             time.sleep(0.05)
    #         self.ltp_near_month_future = self.get_latest_price(str(near_month_token))
    #         self.open_near_month_future = self.ltp_near_month_future
            
    #         logger.info(f"Fetched Near Month Future LTP: {self.ltp_near_month_future} and Open Price: {self.open_near_month_future} for symbol {near_month_symbol}")
            
    #         #Avoiding Rest Call




    #         logger.debug(f"Near Month Future LTP: {self.ltp_near_month_future}, Open: {self.open_near_month_future}")
    #         chgO = (self.ltp_near_month_future - self.open_near_month_future)*100 / self.open_near_month_future
    #         self.frontend_data_socket.emit("near-month-ltp-updated",{"ltp": self.ltp_near_month_future,"chgO":chgO})
            
    #         self._near_month_data[near_month_token] = self.ltp_near_month_future
    #         contracts = self.get_5_woc(float(self.ltp_near_month_future) , "CE" , underlying=self._underlying)["symbols"]
    #         logger.log("DATA",f" Weekly Option Contracts - {contracts}")
    #         #relevant_tokens_to_subscribe = []
    #         for key , value in contracts.items(): 
    #             data = self._broker.get_instrument_details(value)
    #             #Avoiding Rest Call
    #             #[price,open] = self._broker.get_quote(data["tradingsymbol"])

    #             token = str(data["instrument_token"])
    #             while not self.get_latest_price(token):
    #                 time.sleep(0.05)

    #             price = self.get_latest_price(token)
    #             open = price
    #             #Avoiding Rest Call



    #             token = data["instrument_token"]
    #             #relevant_tokens_to_subscribe.append(token)
    #             self.weekly_options_initialize(token , data , "CE" , price , level=key,open=open)
    #             self._order_strategy_mapping[token] = "ULTRA_SCALPING"
    #         logger.info("Completed CALL weekly options setup.")
    #         logger.info("Setting up PUT weekly options...")
    #         contracts = self.get_5_woc(self.ltp_near_month_future , "PE" , underlying=self._underlying)["symbols"]
    #         for key , value in contracts.items(): 
    #             data = self._broker.get_instrument_details(value)
    #             #Avoiding Rest Call
    #             #[price,open] = self._broker.get_quote(data["tradingsymbol"])
    #             #token = data["instrument_token"]
    #             token = str(data["instrument_token"])
    #             while not self.get_latest_price(token):
    #                 time.sleep(0.05)

    #             price = self.get_latest_price(token)
    #             open = price

                
    #             #relevant_tokens_to_subscribe.append(token)
    #             self.weekly_options_initialize(token , data , "PE" , price , level=key,open=open)
    #             self._order_strategy_mapping[token] = "ULTRA_SCALPING"
    #         #self._broker.subscribe_to_all(relevant_tokens_to_subscribe)
    #         logger.info("Completed PUT weekly options setup.")
    #         return
    #     except Exception as e:
    #         logger.error(f"Exception in setting up weekly options - {e}")



    # ---------------------- DIFFERENCE CALCULATOR ----------------------



    def calculate_pctg_difference(self , buy_price: float, current_price: float) -> float:
        if buy_price <= 0:
            raise ValueError("Buy price must be greater than zero")
        
        pct_change = ((current_price - buy_price) / buy_price) * 100
        return pct_change
    
    def calculate_point_difference(self , buy_price: float, current_price: float) -> float:
        #logger.debug(f"Calculating point difference: Buy Price = {buy_price}, Current Price = {current_price}")
        return current_price - buy_price

            
    # ---------------------- TICK HANDLER ----------------------

    def set_latest_price(self, instrument_token, tradingsymbol, price):
        try:
            #logger.debug(f"Received tick for {tradingsymbol} (Token: {instrument_token}) with Latest price {price}")
            #token = instrument_token
            token = str(instrument_token)

            # ⭐ ADD THIS LINE (store price for strategy use)
            self._latest_prices[token] = price


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
                self.frontend_data_socket.emit('price-updated-position', payload)
            
            
            if token in self._five_weekly_option_contracts:
                self.update_weekly_options(token , "ltp" , price)
                payload = {
                    'token': token,             
                    'name': tradingsymbol,      
                    'ltp': price,
                    'lots' : self._five_weekly_option_contracts[token]["lots"]
                }
                # logger.debug("UPDATING ORDER PRICES")
                self.frontend_data_socket.emit('price-updated-order', payload)
            
            if token in self._near_month_data:
                self._near_month_data[token] = price

                chgO = round((price - self.open_near_month_future )*100 / self.open_near_month_future,2) 
                payload = {"ltp" : price, "chgO": chgO}

                self._near_month_future_ltp = price
                self.frontend_data_socket.emit('near-month-ltp-updated', payload)




            # # When Near Month Future's LTP gets updated, let us also update the TradingViewData in the front end for reference
            # data = latest_tradingview_data
            # if not data:
            #     logger.warning("No TradingView data available for validation.")
            #     return
            # payload = {"TradingView_Data" : data}
            # #self.frontend_data_socket.emit('TradingView_Data', payload)


            
        except Exception as e:
            logger.error(f"Error setting latest price {e}")


    def get_latest_price(self, token):
        return self._latest_prices.get(str(token))        

    # ---------------------- THREAD CONTROL ----------------------

    def start_frontend_socket_server(self, app):
        logger.info("Starting frontend socket server...")
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
        logger.info("Frontend socket server thread has been launched. 🚀")


    def start_trading_watcher_thread(self):
        logger.info("Starting trading watcher thread...")
        if not self._trading_watcher_thread_running:
            self._trading_watcher_thread_running = True
            self._stop_event.clear()
            thread = threading.Thread(
                target=self.stop_loss_book_profit_core,
                args=(self._broker,),
                daemon=True,
            )
            logger.info("Launching trading watcher thread 🚀")
            thread.start()

            # Start the new voice readout thread
            voice_thread = threading.Thread(
                target=self.voice_readout_core,
                daemon=True,
            )
            logger.info("Launching voice readout thread 🔊")
            voice_thread.start()            

    def stop_trading_watcher_thread(self):
        logger.info("Stopping trading watcher thread...")
        if self._trading_watcher_thread_running:
            logger.info("Stopping trading watcher thread ⏹")
            self._stop_event.set()
            self._trading_watcher_thread_running = False

    def refresh_subscriptions(self):
        logger.info("Refreshing subscriptions to instruments...")
        #self.refresh_open_pos_buy_price()
        tokens_to_subscribe = self.get_relevant_instruments_to_track()
        try:
            self._broker.subscribe_to_all(tokens_to_subscribe)
        except Exception as e:
            logger.error(f"Couldnt subscribe to instruments - {e}")

    def on_start(self):
        logger.info("Trader on_start initialization...")
        self.refresh_open_pos_buy_price()

    

   #{'INDICATORS': {'15S': {'PVT': 0.97, 'PVTPoiseFlag': -1, 'PVTTrendFlag': 1, 'PVTCrossOverIndex': 4, 'PVTCrossUnderIndex': 5, 'Trend_1226': 1, 'X_1226': 15, 'Y_1226': 32, 
   #                'MACD_1226': 39.15, 'Signal_1226': 29.19, 'Hist_1226': 9.95, 'Trend_2452': 1, 'X_2452': 10, 'Y_2452': 29, 'MACD_2452': 50.72, 'Signal_2452': 43.56, 'Hist_2452': 7.16, 
   #                'ZCross': 0, 'K': 71.65, 'KTrendFlag': -1, 'StochPoiseFlag': -1, 'KCrossoverIndex': 21, 'KCrossUnderIndex': 5, 'EMA9': 103905, 'EMA21': 103864, 'EMA50': 103810, 
   #                'EMA100': 103728, 'EMA200': 103598, 'VWAP': 102244.27, 'VOC': 29.14, 'VOCTrendFlag': -1, 'VOCCrossOverIndex': 6, 'VOCCrossUnderIndex': 14}, 
   #
   #                '1M': {'PVT': 0.61, 'PVTPoiseFlag': 1, 'PVTTrendFlag': 1, 'PVTCrossOverIndex': 5, 'PVTCrossUnderIndex': 7, 'Trend_1226': 1, 'X_1226': 1, 'Y_1226': 6, 'MACD_1226': 87.8, 'Signal_1226': 87.36, 'Hist_1226': 0.44, 'Trend_2452': 1, 'X_2452': 44, 'Y_2452': 74, 'MACD_2452': 144.72, 'Signal_2452': 138.74, 'Hist_2452': 5.98, 'ZCross': 0, 'K': 36.27, 'KTrendFlag': 1, 'StochPoiseFlag': 1, 'KCrossoverIndex': 1, 'KCrossUnderIndex': 13, 'EMA9': 103828, 'EMA21': 103752, 'EMA50': 103598, 'EMA100': 103391, 'EMA200': 103061, 'VWAP': 102242.58, 'VOC': 21.48, 'VOCTrendFlag': 1, 'VOCCrossOverIndex': 1, 'VOCCrossUnderIndex': 3}, '5M': {'PVT': 0.24, 'PVTPoiseFlag': 1, 'PVTTrendFlag': 1, 'PVTCrossOverIndex': 34, 'PVTCrossUnderIndex': 40, 'Trend_1226': 1, 'X_1226': 4, 'Y_1226': 12, 'MACD_1226': 274.3, 'Signal_1226': 253.67, 'Hist_1226': 20.63, 'Trend_2452': 1, 'X_2452': 33, 'Y_2452': 36, 'MACD_2452': 405.67, 'Signal_2452': 374.73, 'Hist_2452': 30.95, 'ZCross': 0, 'K': 69.72, 'KTrendFlag': 1, 'StochPoiseFlag': 1, 'KCrossoverIndex': 7, 'KCrossUnderIndex': 16, 'EMA9': 103643, 'EMA21': 103392, 'EMA50': 102953, 'EMA100': 102571, 'EMA200': 102302, 'VWAP': 102240.71, 'VOC': -7.05, 'VOCTrendFlag': 1, 'VOCCrossOverIndex': 45, 'VOCCrossUnderIndex': 10}, '30M': {'PVT': 1, 'PVTPoiseFlag': 1, 'PVTTrendFlag': 1, 'PVTCrossOverIndex': 9, 'PVTCrossUnderIndex': 13, 'Trend_1226': 1, 'X_1226': 20, 'Y_1226': 30, 'MACD_1226': 382.46, 'Signal_1226': 191.07, 'Hist_1226': 191.38, 'Trend_2452': 1, 'X_2452': 10, 'Y_2452': 12, 'MACD_2452': 213.28, 'Signal_2452': 72.33, 'Hist_2452': 140.94, 'ZCross': 0, 'K': 97.07, 'KTrendFlag': 1, 'StochPoiseFlag': 1, 'KCrossoverIndex': 7, 'KCrossUnderIndex': 11, 'EMA9': 102959, 'EMA21': 102497, 'EMA50': 102232, 'EMA100': 102199, 'EMA200': 102691, 'VWAP': 102239.26, 'VOC': 16.28, 'VOCTrendFlag': -1, 'VOCCrossOverIndex': 8, 'VOCCrossUnderIndex': 10}, '2H': {'PVT': 0.25, 'PVTPoiseFlag': 1, 'PVTTrendFlag': 1, 'PVTCrossOverIndex': 2, 'PVTCrossUnderIndex': 6, 'Trend_1226': 1, 'X_1226': 2, 'Y_1226': 6, 'MACD_1226': 39.07, 'Signal_1226': -87.12, 'Hist_1226': 126.19, 'Trend_2452': 1, 'X_2452': 24, 'Y_2452': 26, 'MACD_2452': -554.26, 'Signal_2452': -697.93, 'Hist_2452': 143.68, 'ZCross': 0, 'K': 51.58, 'KTrendFlag': 1, 'StochPoiseFlag': 1, 'KCrossoverIndex': 2, 'KCrossUnderIndex': 16, 'EMA9': 102320, 'EMA21': 102195, 'EMA50': 102697, 'EMA100': 104320, 'EMA200': 106715, 'VWAP': 102236.68, 'VOC': -6.56, 'VOCTrendFlag': 1, 'VOCCrossOverIndex': 25, 'VOCCrossUnderIndex': 20}, 'D': {'PVT': -0.57, 'PVTPoiseFlag': 1, 'PVTTrendFlag': -1, 'PVTCrossOverIndex': 40, 'PVTCrossUnderIndex': 31, 'Trend_1226': -1, 'X_1226': 15, 'Y_1226': 7, 'MACD_1226': -2783.24, 'Signal_1226': -2130.73, 'Hist_1226': -652.51, 'Trend_2452': -1, 'X_2452': 39, 'Y_2452': 29, 'MACD_2452': -2627.45, 'Signal_2452': -1943.24, 'Hist_2452': -684.21, 'ZCross': 0, 'K': 4.31, 'KTrendFlag': -1, 'StochPoiseFlag': -1, 'KCrossoverIndex': 20, 'KCrossUnderIndex': 10, 'EMA9': 104564, 'EMA21': 107442, 'EMA50': 110461, 'EMA100': 111285, 'EMA200': 108040, 'VWAP': 102771.11, 'VOC': -3.22, 'VOCTrendFlag': -1, 'VOCCrossOverIndex': 6, 'VOCCrossUnderIndex': 1}, 'W': {'PVT': -0.5, 'PVTPoiseFlag': -1, 'PVTTrendFlag': -1, 'PVTCrossOverIndex': 28, 'PVTCrossUnderIndex': 4, 'Trend_1226': -1, 'X_1226': 26, 'Y_1226': 11, 'MACD_1226': 2937.8, 'Signal_1226': 4649.85, 'Hist_1226': -1712.05, 'Trend_2452': -1, 'X_2452': 25, 'Y_2452': 9, 'MACD_2452': 9634.24, 'Signal_2452': 10667.07, 'Hist_2452': -1032.83, 'ZCross': 0, 'K': 13.96, 'KTrendFlag': -1, 'StochPoiseFlag': -1, 'KCrossoverIndex': 4, 'KCrossUnderIndex': 3, 'EMA9': 111835, 'EMA21': 110478, 'EMA50': 100727, 'EMA100': 84918, 'EMA200': 65364, 'VWAP': 105734.63, 'VOC': 2.56, 'VOCTrendFlag': 1, 'VOCCrossOverIndex': 1, 'VOCCrossUnderIndex': 2}}}

    def process_TradingView_Data(self , data):
        try:
            recommendation = ""
            tradeRecomDescription = ""
            #logger.info(f"TradingView Data received: {data}")
            self._tradingView_Data = data
            logger.debug(f"TradingView Data is assigned to internal variable {self._tradingView_Data}")
            #logger.debug(f"1m EMA 50:{data['EMA']['1m']['EMA50']}") 

            indicators = data["INDICATORS"]
            trade_setups = []



            if int(data["INDICATORS"]["2H"]["PVTTrendFlag"]) == -1 and int(data["INDICATORS"]["2H"]["Trend_2452"]) == -1 and int(data["INDICATORS"]["30M"]["PVTTrendFlag"]) == -1 and int(data["INDICATORS"]["30M"]["Trend_2452"]) == -1:
                trade_setups.append({
                "Trend": "Downtrend",
                "SetUp": "2PM DT",
                "Description": "Expect price falls from 1E12, 5E12.",
                "TradeCase": "5PXD",
                "Aggressive": True
            })
            elif int(data["INDICATORS"]["2H"]["PVTTrendFlag"]) == 1 and int(data["INDICATORS"]["2H"]["Trend_2452"]) == 1 and int(data["INDICATORS"]["30M"]["PVTTrendFlag"]) == 1 and int(data["INDICATORS"]["30M"]["Trend_2452"]) == 1:
                trade_setups.append({
                "Trend": "Uptrend",
                "SetUp": "2PM UT",
                "Description": "Expect price rises from 1E12, 5E12.",
                "TradeCase": "5PXU",
                "Aggressive": True
            })
            elif int(data["INDICATORS"]["2H"]["PVTTrendFlag"]) == -1 and int(data["INDICATORS"]["2H"]["Trend_2452"]) == -1:
                trade_setups.append({
                "Trend": "Downtrend",
                "SetUp": "2H Weak Downtrend",
                "Description": "2H timeframe showing early signs of weakness.",
                "TradeCase": "Cautiously short on pullbacks. Confirm with lower timeframes.",
                "Aggressive": False
            })

            # ✅ Save or emit for frontend
            self.trade_setups = trade_setups
        
            # ✅ Prepare data payload for frontend
            json_data = {
                "TradingView_Data": data,
                "TradeSetups": trade_setups
            }

            # ✅ Emit event to frontend (Socket.IO)
            self.frontend_data_socket.emit("update_trading_view_data", json_data)

            logger.debug(f"Emitted TradeSetups: {trade_setups}")

            # Optional: also return for backend usage/testing
            return trade_setups

        except Exception as e:
            logger.error(f"TradingView Data processing failed: {e}")    
            return []
        

    def get_open_position_buy_time(self, token):

        trades = self.fetch_paper_trades()

        token = str(token)

        # ---------------------------------------------------------
        # Find the latest BUY for this token
        # ---------------------------------------------------------

        for trade in reversed(trades):

            if str(trade.get("Token")) != token:
                continue

            if trade.get("Type", "").upper() != "BUY":
                continue

            buy_time = trade.get("Time")

            if buy_time:
                logger.debug(
                    f"Latest BUY time for token {token}: {buy_time}"
                )
                return buy_time

        logger.warning(
            f"No BUY time found for token {token}"
        )

        return None


    def fetch_paper_trades(self):

        filename = "PaperTrading.txt"

        if not os.path.exists(filename):
            return []

        trades = []

        try:

            with open(filename, "r", encoding="utf-8") as f:
                lines = f.readlines()

            if not lines:
                return []

            # ---------------------------------------------------------
            # Read header
            # ---------------------------------------------------------

            headers = [
                h.strip()
                for h in lines[0].rstrip("\n").split("\t")
            ]

            logger.debug(f"PaperTrading headers: {headers}")

            # ---------------------------------------------------------
            # Read transactions
            # ---------------------------------------------------------

            for line_number, line in enumerate(lines[1:], start=2):

                line = line.rstrip("\n")

                if not line.strip():
                    continue

                values = line.split("\t")

                # -----------------------------------------------------
                # Make sure the row has the same number of columns
                # as the header
                # -----------------------------------------------------

                if len(values) != len(headers):

                    logger.warning(
                        f"Skipping PaperTrading line {line_number}: "
                        f"Expected {len(headers)} columns, "
                        f"found {len(values)}"
                    )

                    continue

                # -----------------------------------------------------
                # Create dictionary
                # -----------------------------------------------------

                trade = {}

                for header, value in zip(headers, values):
                    trade[header] = value.strip()

                # -----------------------------------------------------
                # Only COMPLETE transactions
                # -----------------------------------------------------

                if trade.get("Status", "").upper() != "COMPLETE":
                    continue

                # -----------------------------------------------------
                # Validate required fields
                # -----------------------------------------------------

                required_fields = [
                    "Time",
                    "Type",
                    "Instrument",
                    "Token",
                    "Product",
                    "Qty.",
                    "Avg. price",
                    "Status"
                ]

                missing_fields = [
                    field
                    for field in required_fields
                    if field not in trade
                ]

                if missing_fields:

                    logger.warning(
                        f"Skipping PaperTrading line {line_number}: "
                        f"Missing fields {missing_fields}"
                    )

                    continue

                # -----------------------------------------------------
                # Add trade
                # -----------------------------------------------------

                trades.append(trade)

            logger.info(
                f"Paper trades successfully read: {len(trades)}"
            )

            logger.debug(
                f"Paper trades: {trades}"
            )

            return trades

        except Exception as e:

            logger.error(
                f"Error reading PaperTrading.txt: {e}",
                exc_info=True
            )

            return []

    def calculate_paper_positions(self):

        trades = self.fetch_paper_trades()

        positions = {}

        for trade in trades:

            try:
                token = str(trade["Token"])
                symbol = trade["Instrument"]

                qty = int(str(trade["Qty."]).split("/")[0])
                price = float(trade["Avg. price"])
                transaction_type = trade["Type"].upper()

            except Exception as e:
                logger.warning(
                    f"Skipping invalid paper trade {trade}: {e}"
                )
                continue

            if token not in positions:
                positions[token] = {
                    "tradingsymbol": symbol,
                    "instrument_token": token,
                    "buy_quantity": 0,
                    "sell_quantity": 0,
                    "buy_value": 0.0,
                    "sell_value": 0.0,
                }

            position = positions[token]

            if transaction_type == "BUY":

                position["buy_quantity"] += qty
                position["buy_value"] += qty * price

            elif transaction_type == "SELL":

                position["sell_quantity"] += qty
                position["sell_value"] += qty * price

        return positions

    def refresh_paper_positions(self):

        logger.info("========================================")
        logger.info("Refreshing PAPER positions")
        logger.info("========================================")

        trades = self.fetch_paper_trades()

        logger.info(f"Paper trades read: {len(trades)}")
        logger.debug(f"Paper trades: {trades}")

        paper_positions = self.calculate_paper_positions()

        logger.info(f"Calculated paper positions: {paper_positions}")

        new_position_data = {}

        for token, position in paper_positions.items():

            net_quantity = (
                position["buy_quantity"]
                - position["sell_quantity"]
            )

            logger.debug(
                f"Token={token} "
                f"BUY={position['buy_quantity']} "
                f"SELL={position['sell_quantity']} "
                f"NET={net_quantity}"
            )

            if net_quantity <= 0:
                continue

            average_buy_price = (
                position["buy_value"]
                / position["buy_quantity"]
            )

            try:
                instrument_data = self._broker.get_instrument_details(
                    position["tradingsymbol"]
                )

                lotsize = int(
                    instrument_data.get("lot_size", 1)
                )

                exchange = instrument_data.get(
                    "exchange",
                    self._exchange
                )

            except Exception as e:

                logger.warning(
                    f"Could not get instrument details for "
                    f"{position['tradingsymbol']}: {e}"
                )

                lotsize = 1
                exchange = self._exchange

            latest_price = self.get_latest_price(token)

            if not latest_price:
                latest_price = average_buy_price

            pts_pl = self.calculate_point_difference(
                average_buy_price,
                latest_price
            )

            pct_pl = self.calculate_pctg_difference(
                average_buy_price,
                latest_price
            )

            total_pl = pts_pl * net_quantity

            new_position_data[token] = {
                "tradingsymbol": position["tradingsymbol"],
                "average_price": average_buy_price,
                "net_quantity": net_quantity,
                "latest_price": latest_price,
                "instrument_token": token,
                "exchange": exchange,
                "lotsize": lotsize,
                "lots": net_quantity,
                "pts_pl": pts_pl,
                "pct_pl": pct_pl,
                "total_pl": total_pl,
                "order_strategy":
                    self._order_strategy_mapping.get(
                        token,
                        "ULTRA_SCALPING"
                    )
            }

        self._position_data = new_position_data

        logger.info(
            f"PAPER POSITION DATA READY: {self._position_data}"
        )

        self.frontend_data_socket.emit(
            "update_open_positions",
            self._position_data
        )

        logger.info(
            f"update_open_positions emitted. "
            f"Positions={len(self._position_data)}"
        )  

    # ---------------------- SOCKET EVENT HANDLERS ----------------------

    # In your Trader_Singleton class

    def register_socket_handlers(self):
        logger.info("Registering socket event handlers...")

        @self.frontend_data_socket.on('connect')
        def handle_connect():
            logger.debug(self._near_month_data)
            payload = {
                    "broker" : self._broker_string , 
                    "exchange" : self._exchange ,
                    "underlying" : self._near_month_future_symbol, 
                    "positions_data" : self._position_data,
                    "order_strategy_mapping" : self._order_strategy_mapping
                }
            self.frontend_data_socket.emit(
                'setup_data',
                {
                    "broker" : self._broker_string , 
                    "exchange" : self._exchange ,
                    "underlying" : self._near_month_future_symbol, 
                    "positions_data" : self._position_data,
                    "order_strategy_mapping" : self._order_strategy_mapping
                }
            )
              # ⭐ ADD THIS
            if self._five_weekly_option_contracts:
                logger.info("Sending weekly options to newly connected frontend")
                self.frontend_data_socket.emit(
                    "update_weekly_options",
                    self._five_weekly_option_contracts
                )
            logger.debug(payload)
            logger.info("Client connected to socket server.")

        @self.frontend_data_socket.on('disconnect')
        def handle_disconnect():
            logger.info("Client disconnected.")

        @self.frontend_data_socket.on('voice_announcement_toggle')
        def handle_voice_announcement_toggle(payload):
            enabled = True
            if isinstance(payload, dict):
                enabled = bool(payload.get("enabled", True))
            self._voice_announcement_enabled = enabled
            logger.info(f"Voice announcement {'enabled' if enabled else 'disabled'} from UI")

        @self.frontend_data_socket.on('refresh-options-table')
        def refresh_table():
            logger.info("Refreshing frontend options table")
            self.refresh_open_pos_buy_price()
            #self.setup_woc_subscriptions()


        @self.frontend_data_socket.on('request_weekly_options')
        def handle_request_weekly_options():
            """Handles request from the 'Options Widget' (widget_two)."""
            logger.info(
                "Received 'request_weekly_options' from client. Sending weekly contracts..."
            )
            self.frontend_data_socket.emit(
                'update_weekly_options',
                self._five_weekly_option_contracts
            )

        @self.frontend_data_socket.on('request_open_positions')
        def handle_request_open_positions():
            """Handles request from the 'Sell Positions Widget' (widget_four)."""
            logger.info("Received 'request_open_positions' from client. Sending position data...")
            # Emits an event that the sell widget is listening for
            self.frontend_data_socket.emit('update_open_positions',self._position_data)

        @self.frontend_data_socket.on("order_strategy_data_updated_positions")
        def handle_order_strategy_type_positions(order_strategy_details):
            logger.debug(f"Received updated order strategy type for positions {order_strategy_details}")
            self.position_data_update(order_strategy_details["instrument_token"] , "order_strategy" , order_strategy_details["strategy_type"])
            logger.debug(f"Updated positions data: {self._position_data}")

        @self.frontend_data_socket.on("order_strategy_data_updated_orders")
        def handle_order_strategy_type_orders(order_strategy_details):
            logger.debug(f"Received updated order strategy type for orders {order_strategy_details}")
            self._order_strategy_mapping[order_strategy_details["instrument_token"]] = order_strategy_details["strategy_type"]
            logger.debug(f"Updated Order Strategy Mapping: {self._order_strategy_mapping}")

        @self.frontend_data_socket.on('place_sell_order')
        def handle_sell_order(order_details):
            """Handles a sell order request from the frontend."""
            logger.debug(f"Available positions: {self._position_data}")
            logger.debug(f"Received sell order from client: {order_details}")
            try:
                #ltp = self._five_weekly_option_contracts[order_details["token"]]["ltp"]
                ltp = self._position_data[order_details["token"]]["latest_price"]
                logger.debug(f"LTP fetched from the position : {ltp}")

                # Get current mode
                self._mode = fetch_from_json("settings.json", "MODE")
                logger.debug(f"Current trading mode: {self._mode}")

                # =========================================================
                # PAPER / ALERT MODE
                # =========================================================
                if self._mode != "EXECUTION":

                    buy_price = self._position_data[order_details["token"]]["average_price"]
                    lot_size = self._position_data[order_details["token"]]["lotsize"]
                    buy_time = self.get_open_position_buy_time(order_details["token"])

                    write_paper_trade(
                        transaction_type="SELL",
                        tradingsymbol=order_details["tradingsymbol"],
                        token=order_details["token"],
                        qty=order_details["lots"],
                        ltp=ltp,
                        product="MIS",
                        buy_price=buy_price,
                        lot_size=lot_size,
                        buy_time=buy_time
                    )                
                    logger.info(
                    f"Paper SELL recorded for "
                    f"{order_details['tradingsymbol']} "
                    f"({order_details['lots']} lots) @ {ltp}"
                )

                    # Recalculate paper positions
                    self.refresh_paper_positions()

                    self.frontend_data_socket.emit(
                        'sell_order_result',
                        {
                            "success": True,
                            "tradingsymbol": order_details["tradingsymbol"],
                            "lots": order_details["lots"],
                            "paper": True
                        }
                    )
                # =========================================================
                # REAL EXECUTION MODE
                # =========================================================
                else:                    

                    result = self._broker.sell_units(order_details["tradingsymbol"] , order_details["token"] , order_details["lots"] , self._exchange , ltp)

                    logger.info(f"Sell order successful for {order_details['tradingsymbol']} ({order_details['lots']} lots)")
                    #self.frontend_data_socket.emit('sell_order_result',{"success": True, "tradingsymbol": order_details["tradingsymbol"], "lots": order_details["lots"]})
            except Exception as e:
                logger.error(f"Error placing sell order: {e}", exc_info=False)
                self.frontend_data_socket.emit(
                    'sell_order_result',
                    {"success": False, "tradingsymbol": order_details.get("tradingsymbol", "?"), "error": str(e)}
                )
                                               
        @self.frontend_data_socket.on('request_pending_orders')
        def send_pending():
            self.frontend_data_socket.emit('update_pending_orders', self._broker.fetch_all_pending_orders())                     

        @self.frontend_data_socket.on('cancel_pending_order')
        def cancel_pending_order_handler(data):
            try:
                order_id = data.get("order_id")
                if not order_id:
                    self.frontend_data_socket.emit('status_message', {"success": False,"message": "Missing order ID"})
                    return

                logger.info(f"Received request to cancel pending order: {order_id}")

                # Call your adapter function
                success = self._broker.cancel_order(order_id)

                if success:
                    self.frontend_data_socket.emit('status_message', {"success": True,"message": f"Order {order_id} cancelled"})
                else:
                    self.frontend_data_socket.emit('status_message', {"success": False,"message": f"Failed to cancel order {order_id}"})
                    return

                # Fetch latest pending orders again
                pending_orders = self._broker.fetch_all_pending_orders()

                # Send updated pending orders to UI
                self.frontend_data_socket.emit('update_pending_orders', pending_orders)

            except Exception as e:
                logger.error(f"Error cancelling pending order: {e}")
                self.frontend_data_socket.emit('status_message', {"success": False,"message": f"Error cancelling order: {str(e)}"})


        def write_paper_trade(
            transaction_type,
            tradingsymbol,
            token,
            qty,
            ltp,
            product="MIS",
            buy_price=None,
            lot_size=1,
            buy_time=None
        ):

            filename = "PaperTrading.txt"

            # ---------------------------------------------------------
            # Fixed column widths
            # ---------------------------------------------------------

            WIDTH_TIME       = 20
            WIDTH_TYPE       = 8
            WIDTH_INSTRUMENT = 24
            WIDTH_TOKEN      = 12
            WIDTH_PRODUCT    = 10
            WIDTH_QTY        = 10
            WIDTH_PRICE      = 12
            WIDTH_STATUS     = 12
            WIDTH_DURATION   = 12
            WIDTH_PNL_RATE   = 12
            WIDTH_PNL        = 16
            WIDTH_PNL_PCT    = 12

            # ---------------------------------------------------------
            # Header
            # ---------------------------------------------------------

            headers = [
                "Time".ljust(WIDTH_TIME),
                "Type".ljust(WIDTH_TYPE),
                "Instrument".ljust(WIDTH_INSTRUMENT),
                "Token".ljust(WIDTH_TOKEN),
                "Product".ljust(WIDTH_PRODUCT),
                "Qty.".ljust(WIDTH_QTY),
                "Avg. price".ljust(WIDTH_PRICE),
                "Status".ljust(WIDTH_STATUS),
                "Duration".rjust(WIDTH_DURATION),
                "PnL Rate".rjust(WIDTH_PNL_RATE),
                "PnL %".rjust(WIDTH_PNL_PCT),
                "PnL".rjust(WIDTH_PNL)
            ]

            # Create header if file doesn't exist
            if not os.path.exists(filename):

                with open(filename, "w", encoding="utf-8") as f:
                    f.write("\t".join(headers) + "\n")

            # ---------------------------------------------------------
            # Default PnL values
            # BUY records will have blank PnL columns
            # ---------------------------------------------------------

            pnl_rate = ""
            pnl = ""
            pnl_pct = ""
            duration = ""

            # ---------------------------------------------------------
            # Calculate PnL ONLY for SELL
            # ---------------------------------------------------------

            if transaction_type.upper() == "SELL" and buy_price is not None:

                sell_time = datetime.datetime.now()

                if isinstance(buy_time, str):
                    buy_time = datetime.datetime.strptime(
                        buy_time,
                        "%m/%d/%Y %H:%M:%S"
                    )

                elapsed = sell_time - buy_time
                total_seconds = int(elapsed.total_seconds())

                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                seconds = total_seconds % 60

                if hours > 0:
                    duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                else:
                    duration = f"{minutes:02d}:{seconds:02d}"

                buy_price = float(buy_price)
                sell_price = float(ltp)
                #quantity = int(qty)
                quantity = int(qty) * int(lot_size)

                # PnL Rate = Sell Price - Buy Price
                pnl_rate_value = sell_price - buy_price

                # PnL = (Sell Price - Buy Price) * Qty
                pnl_value = pnl_rate_value * quantity

                # PnL % = (Sell Price - Buy Price) * 100 / Buy Price
                pnl_pct_value = (
                    (pnl_rate_value * 100) / buy_price
                    if buy_price != 0
                    else 0
                )

                pnl_rate = f"{pnl_rate_value:.2f}"

                # No decimal places + comma separated
                pnl = f"{pnl_value:,.0f}"

                pnl_pct = f"{pnl_pct_value:.2f}"

            # ---------------------------------------------------------
            # Timestamp
            # ---------------------------------------------------------

            timestamp = datetime.datetime.now().strftime(
                "%#m/%#d/%Y %H:%M:%S"
            )

            # ---------------------------------------------------------
            # Prepare row
            # ---------------------------------------------------------

            values = [
                timestamp.ljust(WIDTH_TIME),
                transaction_type.ljust(WIDTH_TYPE),
                tradingsymbol.ljust(WIDTH_INSTRUMENT),
                str(token).ljust(WIDTH_TOKEN),
                product.ljust(WIDTH_PRODUCT),
                f"{qty}/{qty}".ljust(WIDTH_QTY),
                f"{float(ltp):.2f}".rjust(WIDTH_PRICE),
                "COMPLETE".ljust(WIDTH_STATUS),
                duration.rjust(WIDTH_DURATION),
                # PnL columns
                pnl_rate.rjust(WIDTH_PNL_RATE),
                pnl_pct.rjust(WIDTH_PNL_PCT),
                pnl.rjust(WIDTH_PNL)
            ]

            # ---------------------------------------------------------
            # Write transaction
            # ---------------------------------------------------------

            with open(filename, "a", encoding="utf-8") as f:

                f.write(
                    "\t".join(values) + "\n"
                )


        @self.frontend_data_socket.on('place_buy_order')
        def handle_buy_order(order_details):
            logger.debug(f"Received buy order from client: {order_details}")
            try:
                target_profit = 0
                if order_details["strategy"].upper() == "INTRA":
                    target_profit = PTS_PROFIT_INTRA_FACTOR* PNT_BOOK_PROFIT         
                elif order_details["strategy"].upper() == "SCALPING":
                    target_profit = PTS_PROFIT_SCALPING_FACTOR* PNT_BOOK_PROFIT
                elif order_details["strategy"].upper() == "ULTRASCALPING":
                    target_profit = PTS_PROFIT_ULTRA_SCALPING_FACTOR* PNT_BOOK_PROFIT

                # Sell Mode could be ALERT or EXECUTION based on user preference
                self._mode = fetch_from_json("settings.json" , "MODE")
                sell_mode = order_details["SELL_MODE"]
                logger.debug(f"Mode set is {self._mode} ; Strategy set is {order_details['strategy']}  Sell Mode is {sell_mode} ")

                ltp = self._five_weekly_option_contracts[order_details["token"]]["ltp"]

                if self._mode != "EXECUTION":
                    write_paper_trade(transaction_type="BUY",tradingsymbol=order_details["tradingsymbol"],token=order_details["token"],qty=order_details["lots"],ltp=ltp,product="MIS")
                    logger.info(
                            f"Paper BUY recorded: "
                            f"{order_details['tradingsymbol']} "
                            f"Token={order_details['token']} "
                            f"Qty={order_details['lots']} "
                            f"Price={ltp}"
                        )

                    logger.info("Calling refresh_paper_positions() after BUY...")                    
                    self.refresh_paper_positions()
                    logger.info(f"After BUY refresh, position_data = {self._position_data}")
                else:
                    if sell_mode == "U":
                        logger.info(f"Placing Buy-Sell order as Sell Mode is set to {sell_mode} ")
                        self._broker.buy_sell_units(order_details["tradingsymbol"] , order_details["token"] , order_details["lots"] , self._exchange , ltp,self._mode,sell_mode,target_profit)
                    else: # Sell Mode is 'T' or 'D'
                        logger.info("Placing Buy order")
                        self._broker.buy_units(order_details["tradingsymbol"] , order_details["token"] , order_details["lots"] , self._exchange , ltp,self._mode,sell_mode,target_profit)
                    # ✅ Once order completes, tell frontend to refresh fund summary
                    #self.frontend_data_socket.emit('refresh_fund_summary')
                    # ✅ Notify frontend of success
                    #self.frontend_data_socket.emit('buy_order_result', { "success": True,"tradingsymbol": order_details["tradingsymbol"],"lots": order_details["lots"]})
            except Exception as e:
                    logger.error(f"|Error placing buy order: {e}|", exc_info=True)
                    #Check if the error is because of insufficient fund..If so, reduce the lots by 1 and reattempt
                    error_message = str(e).upper()
                    if "FUND LIMIT INSUFFICIENT" in error_message:
                        reduced_lots = max(1, int(order_details["lots"]) - 1)
                        logger.warning(f"Insufficient funds for {order_details['lots']} lots — retrying with {reduced_lots} lot(s)...")
                        try:
                            ltp = self._five_weekly_option_contracts[order_details["token"]]["ltp"]
                            self._broker.buy_units(order_details["tradingsymbol"] , order_details["token"] , reduced_lots , self._exchange , ltp)
                            self.frontend_data_socket.emit('buy_order_result', { 
                                "success": True,
                                "tradingsymbol": order_details["tradingsymbol"],
                                "lots": reduced_lots,
                                "note": "Retried with reduced lots due to insufficient funds"
                            })
                            return
                        except Exception as retry_error:
                            logger.error(f"Retry buy order failed: {retry_error}", exc_info=True)
                            self.frontend_data_socket.emit('buy_order_result', {
                                "success": False,
                                "tradingsymbol": order_details["tradingsymbol"],
                                "lots": reduced_lots,
                                "error": str(retry_error)
                            })
                            return
                    #logger.exception(f"Error placing buy order: {e}")
                    # ❌ Notify frontend of failure
                    self.frontend_data_socket.emit('buy_order_result', {
                        "success": False,
                        "tradingsymbol": order_details["tradingsymbol"],
                        "lots": order_details["lots"],
                        "error": str(e)
                    })

      

        