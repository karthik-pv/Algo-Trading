import logging
from datetime import datetime
import calendar
import csv
import threading
import time
from pprint import pprint
from loguru import logger

from core.utils import get_trading_symbols_from_json , find_matching_row_in_csv , fetch_from_json , compute_limit_margin , round_to_tick , get_exchange_for_underlying , resolve_data_path
from core.kite_connector import KiteSingleton
from interface.broker_interface import BrokerInterface
from core.trade_logic import Trader_Singleton
from adapter.kite_utils import fund_summary_attribute_mgmt , position_attribute_mgmt , orders_attribute_mgmt, instr_det_attrib_mgmt

import pandas as pd
import json

from core.utils import is_market_open

from core import shared_state

import os




class KiteAdapter(BrokerInterface):

    def __init__(self):
        self._trader = Trader_Singleton()
        self.kite_instance = KiteSingleton()
        self.kite = self.kite_instance.get_kite()
        self._underlying = fetch_from_json("appconfig.json" , "UNDERLYING")

        self._instrument_cache = {}       # stores processed instruments keyed by tradingsymbol
        self._instrument_data_loaded = {}  # raw data loaded from CSV once
        self._csv_loaded = False           # flag to check if CSV has been read
        self._csv_load_lock = threading.Lock()  # guards CSV load (background preload + lazy load)

        # Instrument-file-derived meta (replaces the old derived keys in
        # appconfig.json). Computed in memory at startup from the CSV by
        # download_instrument_list / compute_instrument_meta.
        self._instrument_meta = None

        # Kite websocket feed sampler state (KITE FEED RATE / FEED
        # STALL log lines - mirrors the MStock connector sampler so the
        # two feeds can be compared from the logs).
        self._feed_packets = 0
        self._feed_stats_window_start = 0.0
        self._feed_last_tick_ts = 0.0
        self._feed_max_gap = 0.0
        self._feed_lock = threading.Lock()

    def _get_order_freeze_limit(self):
        # Fetched per order so ORDER_FREEZE_LIMIT edits apply without a restart.
        key = "NIFTY_ORDER_FREEZE_LIMIT" if str(self._underlying) == "NIFTY" else "SENSEX_ORDER_FREEZE_LIMIT"
        value = int(fetch_from_json("appconfig.json", key) or 0)
        if value <= 0:
            default = 1800 if str(self._underlying) == "NIFTY" else 1000
            logger.warning(f"{key} missing or invalid in appconfig.json; using exchange default {default}")
            return default
        return value

    def _fallback_ltp(self, tradingsymbol):
        # Fetched per call so *_FALLBACK_LTP edits apply without a restart.
        # (The old code referenced non-existent Trader attributes here and
        # crashed instead of returning the configured fallback.)
        # CRUDEOILM must be matched before CRUDEOIL - the shorter prefix
        # also matches mini-contract symbols.
        symbol = str(tradingsymbol).upper()
        if "CRUDEOILM" in symbol:
            key = "CRUDEOILM_FALLBACK_LTP"
        elif "CRUDEOIL" in symbol:
            key = "CRUDEOIL_FALLBACK_LTP"
        elif "SENSEX" in symbol:
            key = "SENSEX_FALLBACK_LTP"
        else:
            key = "NIFTY_FALLBACK_LTP"
        return float(fetch_from_json("appconfig.json", key) or 0)

    def get_instrument_meta(self):
        if self._instrument_meta is None and os.path.exists(resolve_data_path("kite_instruments.csv")):
            # Lazily derive from the CSV (e.g. a call before the startup
            # download ran). The CSV is small; parsing it here is cheap.
            try:
                self.compute_instrument_meta("kite_instruments.csv", self._underlying)
            except Exception as e:
                logger.warning(f"Lazy instrument meta computation failed: {e}")
        if self._instrument_meta:
            return self._instrument_meta
        return super().get_instrument_meta()


    def _load_instruments_from_csv(self, csv_path="kite_instruments.csv"):
        """Loads all instrument data from CSV into memory once."""
        logger.info("Loading instrument data from CSV into memory...")
        csv_path = resolve_data_path(csv_path)
        try:
            with open(csv_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts = row.get("tradingsymbol")
                    if ts:
                        self._instrument_data_loaded[ts] = row
            self._csv_loaded = True
            logger.info(f"Loaded {len(self._instrument_data_loaded)} instruments into memory.")
        except Exception as e:
            logger.error(f"Failed to load instrument CSV: {e}")


    def preload_instrument_data(self):
        """
        Warm the in-memory instrument CSV cache ahead of first use.

        Safe to call from a background thread at startup: concurrent
        lazy loads from get_instrument_details block on the same lock
        and reuse the result instead of re-reading the CSV.
        """
        with self._csv_load_lock:
            if not self._csv_loaded:
                self._load_instruments_from_csv()

    def get_instrument_details(self, tradingsymbol):
        logger.info(f"Attempting to fetch instrument details for {tradingsymbol}")

        # ✅ 1. Check processed cache first
        if tradingsymbol in self._instrument_cache:
            logger.info(f"Cache HIT for instrument: {tradingsymbol}")
            return self._instrument_cache[tradingsymbol]

        # ✅ 2. Ensure CSV is loaded into memory
        # (double-checked lock: if a background preload is already
        # loading the CSV, wait for it instead of loading again)
        if not self._csv_loaded:
            with self._csv_load_lock:
                if not self._csv_loaded:
                    self._load_instruments_from_csv()

        # ✅ 3. Lookup instrument in preloaded memory
        data = self._instrument_data_loaded.get(tradingsymbol)
        if not data:
            logger.error(f"No instrument details found for {tradingsymbol} in in-memory CSV data.")
            return None

        logger.info(f"Cache MISS for instrument: {tradingsymbol}. Using in-memory CSV data.")
        processed_data = instr_det_attrib_mgmt(data)
        logger.log(logging.INFO, f"Instrument Details (processed): {processed_data}")

        # ✅ 4. Cache processed result for next time
        self._instrument_cache[tradingsymbol] = processed_data
        return processed_data


    def fetch_expiries(self , underlying):
        logger.info(f"fetch_expiries {underlying} from M.Stock...")
    
    def fetch_all_orders(self):
        try:
            orders = orders_attribute_mgmt(self.kite.orders())
            #logger.debug(f"Fetched Orders: {orders}")
            return orders
        except Exception as e:
            logger.error(f"Kite fetch_all_orders error: {e}")
            return []
        
    def fetch_all_orders_for_export(self):
        """
        Fetch orders from Kite and normalize fields
        to match mStock structure.
        """
        try:
            orders = self.kite.orders()

            normalized = []

            for o in orders:

                normalized.append({
                    "timestamp": o.get("exchange_timestamp") or o.get("order_timestamp"),
                    "tradingsymbol": o.get("tradingsymbol"),
                    "transaction_type": o.get("transaction_type"),
                    "quantity": o.get("filled_quantity"),
                    "average_price": o.get("average_price"),
                    "order_status": o.get("status"),
                    "order_id": o.get("order_id")
                })

            return normalized

        except Exception:
            logger.exception("Kite order fetch failed")
            return []        

    def fetch_all_instruments(self):
        try:
            return self.kite.instruments()
        except Exception as e:
            logger.error(f"Kite fetch_all_instruments error: {e}")

            return None

    def fetch_all_positions(self):
        try:
            raw_positions = self.kite.positions()
            if isinstance(raw_positions, dict):
                positions = raw_positions.get("net", [])
            else:
                positions = raw_positions or []

            if not positions:
                logger.info("Kite returned no open positions on startup refresh.")
                return []

            positions = position_attribute_mgmt(positions)
            positions = [
                p for p in positions
                if float(p.get("quantity", 0) or 0) > 0
            ]

            for position in positions:
                tradingsymbol = position.get("tradingsymbol") or position.get("symbol")
                if not tradingsymbol:
                    logger.warning(f"Skipping Kite position entry with no tradingsymbol: {position}")
                    continue

                position["tradingsymbol"] = tradingsymbol
                instrument_details = self.get_instrument_details(tradingsymbol)
                if instrument_details and instrument_details.get("lot_size"):
                    position["lotsize"] = int(instrument_details["lot_size"])
                else:
                    logger.warning(
                        f"Missing instrument details for {tradingsymbol}. Using lot_size=1 so refresh can continue."
                    )
                    position["lotsize"] = 1

                position["quantity"] = float(position.get("quantity", 0) or 0)
                position["average_price"] = float(position.get("average_price", 0) or 0)
                position["instrument_token"] = str(position.get("instrument_token", position.get("token", 0)))
                position["exchange"] = position.get("exchange", "NFO")

                if position["exchange"] != "MCX":
                    try:
                        position["quantity"] = position["quantity"] / position["lotsize"]
                    except ZeroDivisionError:
                        position["quantity"] = float(position.get("quantity", 0) or 0)

            pprint(positions)
            return positions
        except Exception as e:
            logger.error(f"Kite fetch_all_positions error: {e}")
            return []

    def fetch_all_trades(self):
        try:
            return self.kite.trades()
        except Exception as e:
            logger.error(f"Kite fetch_all_trades error: {e}")
            return None
        
    def fetch_fund_summary(self):
        logger.info("Calling fetch_fund_summary of Kite Connect")
        try:
            fund_summary = fund_summary_attribute_mgmt(self.kite.margins())
            return fund_summary
        except Exception as e:
            logger.error(f"Kite fund summary error : {e}")
        return

    def fetch_day_start_cash(self):
        logger.info("Fetching start-of-day cash (opening_balance) from Kite...")
        try:
            margins = self.kite.margins() or {}
            opening_balance = (
                margins.get("equity", {}).get("available", {}) or {}
            ).get("opening_balance")
            if opening_balance is None:
                logger.warning("opening_balance not present in Kite margins response")
                return None
            return float(opening_balance)
        except Exception as e:
            logger.error(f"Kite day-start cash error : {e}")
        return

    def fetch_instrument_quote(self , exchange , instrument):
        try:
            formatted_instrument = f"{exchange}:{instrument}"
            logger.info(f"Fetching quote for {formatted_instrument} from Kite API...")
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
    
    # def get_instrument_details(self, tradingsymbol):
    #     logger.info(f"Attempting to fetch instrument details for {tradingsymbol}")
        
    #     if tradingsymbol in self._instrument_cache:
    #         logger.info(f"Cache HIT for instrument: {tradingsymbol}")
    #         return self._instrument_cache[tradingsymbol]
        
    #     logger.info(f"Cache MISS for instrument: {tradingsymbol}. Fetching from source (CSV).")
        
    #     data = find_matching_row_in_csv("kite_instruments.csv", "tradingsymbol", tradingsymbol)
    #     if not data:
    #         logger.error(f"No instrument details found for {tradingsymbol} in kite_instruments.csv")
    #         return None
    #     logger.info(f"Instrument details fetched from CSV: {data}")
    #     processed_data = instr_det_attrib_mgmt(data)
    #     logger.log("DATA", f"Instrument Details (processed): {processed_data}")
    #     self._instrument_cache[tradingsymbol] = processed_data
    #     return processed_data

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

            #FUTURE AND OPTIONS EXPIRING TOGETHER
            # Get the first letter of the month name, e.g., 'October' -> 'O'
            # if the weekly expiry doesnt coincide with monthly expiry of the near month future token ..then it would be first letter of the month followed by 2 digits of expiry date
            # if the weekly expiry coincides with monthly expiry of the near month future token ..then it would be first three letters of the month and no digits of expiry date
            
            if self.get_instrument_meta().get("expire_together"):
                month_char = calendar.month_name[expiry.month][:3].upper()
                dd_str=""
            else:
                #month_char = calendar.month_name[expiry.month][0].upper()
                month_char = str(expiry.month)
                dd_str = f"{expiry.day:02d}"        

            logger.info(f"{underlying}{yy}{month_char}{dd_str}{strike}{call_or_put}")
            return f"{underlying}{yy}{month_char}{dd_str}{strike}{call_or_put}"
    
    #self._broker.buy_units(order_details["tradingsymbol"] , order_details["token"] , order_details["lots"] , self._exchange , ltp,self._mode,sell_mode,target_profit)
    def buy_units(self, trading_symbol, instrument_token, quantity , exchange , ltp,mode="",sell_mode="",target_profit=0,strategy=""):
        logger.info(f"Buying units: {quantity} of {trading_symbol} ({instrument_token}) via KiteAPI...Current LTP: {ltp}...Mode is {mode}. Sell Mode is {sell_mode}, target_profit is {target_profit}")
        #logger.debug(f"Inside Buy Units trading symbol: {trading_symbol} instrument token: {instrument_token}, quantity: {quantity} lots, exchange: {exchange}, ltp: {ltp}" )
        # data = shared_state.latest_tradingview_data
        # if not data:
        #     logger.warning("No TradingView data available for validation.")
        #     return
        
        # pvt_30m = data['PVT']['30m']
        # logger.debug(f"pvt_30m is : {pvt_30m}")
        try:
            instrument_details = self.get_instrument_details(trading_symbol)
            lotsize = instrument_details["lot_size"]
            tick = instrument_details.get("tick_size") or 0.05
            if exchange == "MCX":
                order_type = self.kite.ORDER_TYPE_LIMIT
                price = round_to_tick(float(ltp) + compute_limit_margin(ltp), tick, up=True)
                # Kite MCX quantity is in LOTS (the margin engine charges
                # limit price x lot size x lots), so pass the lot count
                # through unchanged. Multiplying by lot size here makes
                # every order lot_size times too big.
                order_id = self.kite.place_order(
                    tradingsymbol=trading_symbol,
                    exchange = exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                    quantity= str(quantity),
                    order_type = order_type,
                    product = self.kite.PRODUCT_MIS,
                    variety=self.kite.VARIETY_REGULAR, 
                    price = price
                )
            else:
                # order_type = self.kite.ORDER_TYPE_MARKET
                # quantity = int(quantity) * int(lotsize)
                # price = 0

                # Changed from Market to Limit to avoid KiteConnection library upgrade and to mimic the market protection 
                order_type = self.kite.ORDER_TYPE_LIMIT
                quantity = int(quantity) * int(lotsize)
                #price = 0
                price = round_to_tick(float(ltp) + compute_limit_margin(ltp), tick, up=True) 



                #This checks if the quantity is greater the Order Freeze Limit..
                #If so then it check if it can be converted into 2 legs or more
                #if Not reset the quantity to max order freeze limit so that quantity could be executed as normal order
                #If it can be converted intio more than 2 legs that it takes the iceberg route

                if quantity > int(self._get_order_freeze_limit()):
                    if quantity // int(self._get_order_freeze_limit()) == 1:
                        logger.info(f"Quantity {quantity} is more thae than Order Freeze Limit {self._get_order_freeze_limit()} but not big enough for 2 iceberg legs. So restricting it Order freeze limit (normal order)")
                        quantity = self._get_order_freeze_limit()

                if int(quantity) <= int(self._get_order_freeze_limit()):
                    logger.debug(f"Quantity - {int(quantity)}. Executing for Normal Order")
                    order_id = self.kite.place_order(
                        tradingsymbol=trading_symbol, 
                        exchange = exchange,
                        transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                        quantity= str(quantity),
                        order_type = order_type,
                        product = self.kite.PRODUCT_MIS,
                        variety=self.kite.VARIETY_REGULAR, 
                        price = price
                    )
                else:
                    logger.debug(f"Quantity - {int(quantity)}. Executing as an Iceberg Order")
                    order_type = self.kite.ORDER_TYPE_LIMIT # Iceberg order cannot be of type Market
                    
                    iceberg_legs = quantity // int(self._get_order_freeze_limit()) # This will be 2 or more
                    
                    price = round_to_tick(float(ltp) + compute_limit_margin(ltp), tick, up=True) 
                    logger.debug(f"Arrived iceberg legs - {iceberg_legs} with iceberg quantity {self._get_order_freeze_limit()}")
                    order_id = self.kite.place_order(
                        tradingsymbol=trading_symbol, 
                        exchange = exchange,
                        transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                        quantity= str(quantity),
                        order_type = order_type,
                        product = self.kite.PRODUCT_MIS,
                        variety="iceberg", 
                        price = price,
                        iceberg_legs=iceberg_legs,
                        iceberg_quantity= self._get_order_freeze_limit()
                    )

            logger.info("Kite buy order placed successfully")

            # =========================================================
            # FAST POSITION GRID UPDATE (normal, iceberg and MCX paths)
            # =========================================================
            # BUY has been successfully accepted by Kite.
            # Immediately show the current LTP as the temporary BUY price.
            # The authoritative BUY price will be calculated later by
            # refresh_open_pos_buy_price().
            grid_qty = int(quantity) if exchange == "MCX" else int(quantity) // int(lotsize)
            self._trader.fast_update_position_after_buy(
                trading_symbol=trading_symbol,
                instrument_token=instrument_token,
                quantity=grid_qty,
                lotsize=lotsize,
                executed_buy_price=ltp,
                exchange=exchange,
                strategy=strategy,
                sell_mode=sell_mode or "T"
            )

            # =========================================================
            # EXIT ORDER FOR SELL TYPES U / D (mirrors the MStock flow)
            #   U: SELL LIMIT at LTP + target immediately (no fill wait)
            #   D: SELL LIMIT at executed buy price + target (fill-accurate)
            # Sell Type T places nothing - the TP/SL watcher manages it.
            # =========================================================
            if mode == "LIVE" and sell_mode in ("U", "D"):
                if target_profit <= 0:
                    logger.warning(
                        f"Sell Mode {sell_mode} with target_profit "
                        f"{target_profit}; skipping the immediate exit "
                        f"order (watcher will manage the position)."
                    )
                else:
                    if sell_mode == "D":
                        buy_price = self.fetch_order_executed_price(order_id)
                        if buy_price is None:
                            buy_price = float(ltp)
                            logger.warning(
                                f"Executed price unavailable for order "
                                f"{order_id}; using buy LTP {buy_price} "
                                f"for the immediate sell"
                            )
                        exit_price = buy_price + target_profit
                    else:  # U
                        exit_price = float(ltp) + target_profit

                    self.place_exit_limit(
                        trading_symbol,
                        instrument_token,
                        grid_qty,
                        exchange,
                        exit_price
                    )
            elif mode == "LIVE" and sell_mode == "T":
                logger.debug("Waiting for Trigger to raise the Sell order")

            self._trader.frontend_data_socket.emit(
                'status_message',
                {"success": True, "message": "Order placed successfully"}
            )

            return order_id
        except Exception as e:
            logger.error(f"Error in kite buy order {e}")
            self._trader.frontend_data_socket.emit('status_message', {"success": False, "message": "Error placing order..."})

    def buy_sell_units(self, trading_symbol, instrument_token, quantity, exchange, ltp, mode="", sell_mode="", target_profit=0, strategy=""):
        """Sell Type U entry point: BUY + immediate SELL LIMIT at LTP + target.

        Mirrors MStock's buy_sell_units - the exit is placed aggressively
        before the fill price is known, so the order sits in the sell
        queue while the premium is still spiking.
        """
        return self.buy_units(
            trading_symbol,
            instrument_token,
            quantity,
            exchange,
            ltp,
            mode=mode,
            sell_mode="U",
            target_profit=target_profit,
            strategy=strategy
        )

    def place_exit_limit(self, trading_symbol, instrument_token, quantity, exchange, price):
        """Place a SELL LIMIT exit order. quantity is in LOTS.

        Used by the U/D immediate exit at buy time and to re-place a
        partial leg's exit after a manual partial sell.
        """
        try:
            instrument_details = self.get_instrument_details(trading_symbol)
            lotsize = instrument_details["lot_size"]
            tick = instrument_details.get("tick_size") or 0.05

            # Mirror the buy-side quantity semantics: units for NFO/BFO,
            # lots for MCX.
            order_qty = int(quantity) if exchange == "MCX" else int(quantity) * int(lotsize)
            limit_price = round_to_tick(float(price), tick, up=True)

            order_id = self.kite.place_order(
                tradingsymbol=trading_symbol,
                exchange = exchange,
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity=str(order_qty),
                order_type = self.kite.ORDER_TYPE_LIMIT,
                product = self.kite.PRODUCT_MIS,
                variety=self.kite.VARIETY_REGULAR,
                price = limit_price
            )
            logger.info(
                f"Kite SELL LIMIT exit placed at {limit_price} for "
                f"{trading_symbol} ({quantity} lots)"
            )
            self._trader.frontend_data_socket.emit(
                'sell_order_result',
                {"success": True, "tradingsymbol": trading_symbol, "lots": int(quantity), "Sell Price": limit_price}
            )
            return order_id
        except Exception as e:
            logger.error(f"Kite place_exit_limit failed for {trading_symbol}: {e}")
            return None

    def fetch_order_executed_price(self, order_id):
        """Fetch the executed average price of a completed Kite order."""
        try:
            history = self.kite.order_history(str(order_id))
            status = str(history.get("status", "")).upper()
            avg_price = history.get("average_price")
            if status == "COMPLETE" and avg_price:
                return float(avg_price)
            logger.warning(
                f"Kite order {order_id} not fully traded (status {status}); "
                f"no executed price yet"
            )
            return None
        except Exception as e:
            logger.error(f"Kite fetch_order_executed_price error for {order_id}: {e}")
            return None

    

    def sell_units(self, trading_symbol, instrument_token , quantity, exchange , ltp, position_key=""):
        logger.debug(f"Inside sell units trading symbol: {trading_symbol} instrument token: {instrument_token} quantity: {quantity} exchange: {exchange} ltp: {ltp}" )
        original_quantity = quantity
        try:
            instrument_details = self.get_instrument_details(trading_symbol)
            lotsize = instrument_details["lot_size"]
            tick = instrument_details.get("tick_size") or 0.05
            if exchange == "MCX":
                order_type = self.kite.ORDER_TYPE_LIMIT
                # Rounded down to the tick and floored at one tick so
                # low-priced options can never produce an invalid
                # (zero/negative) sell limit price.
                price = round_to_tick(float(ltp) - compute_limit_margin(ltp), tick, up=False)
                # Kite MCX quantity is in LOTS (same as the BUY path);
                # pass the lot count through unchanged.
                order_id = self.kite.place_order(
                    tradingsymbol=trading_symbol,
                    exchange = exchange,
                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                    quantity= str(quantity),
                    order_type = order_type,
                    product = self.kite.PRODUCT_MIS,
                    variety=self.kite.VARIETY_REGULAR, 
                    price = price
                )                
            else:
                # Changed from Market to Limit to avoid KiteConnection library upgrade and to mimic the market protection 
                order_type = self.kite.ORDER_TYPE_LIMIT
                quantity = int(quantity) * int(lotsize)
                #price = 0
                price = round_to_tick(float(ltp) - compute_limit_margin(ltp), tick, up=False) 

                #This checks if the quantity is greater the Order Freeze Limit..
                #If so then it check if it can be converted into 2 legs or more
                #if Not reset the quantity to max order freeze limit so that quantity could be executed as normal order
                #If it can be converted intio more than 2 legs that it takes the iceberg route

                if quantity > int(self._get_order_freeze_limit()):
                    if quantity // int(self._get_order_freeze_limit()) == 1:
                        logger.info(f"Quantity {quantity} is more than than Order Freeze Limit {self._get_order_freeze_limit()} but not big enough for 2 iceberg legs. So restricting it Order freeze limit (normal order)")
                        quantity = self._get_order_freeze_limit()

                if int(quantity) <= int(self._get_order_freeze_limit()):
                    logger.debug(f"Quantity - {int(quantity)}. ")
                    logger.debug(
                        "Placing SELL Normal order",
                        extra={
                            "tradingsymbol": trading_symbol,
                            "exchange": exchange,
                            "transaction_type": self.kite.TRANSACTION_TYPE_SELL,
                            "quantity": quantity,
                            "order_type": order_type,
                            "product": self.kite.PRODUCT_MIS,
                            "variety": self.kite.VARIETY_REGULAR,
                            "price": price,
                            "ltp": ltp
                        }
                    )

                    order_id = self.kite.place_order(
                        tradingsymbol=trading_symbol, 
                        exchange = exchange,
                        transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                        quantity= str(quantity),
                        order_type = order_type,
                        product = self.kite.PRODUCT_MIS,
                        variety=self.kite.VARIETY_REGULAR, 
                        price = price
                    )
                else:
                    logger.debug(f"Quantity - {int(quantity)}. Executing as an Iceberg Order")
                    order_type = self.kite.ORDER_TYPE_LIMIT # Iceberg order cannot be of type Market
                    
                    iceberg_legs = quantity // int(self._get_order_freeze_limit()) # This will be 2 or more
                    
                    price = round_to_tick(float(ltp) - compute_limit_margin(ltp), tick, up=False) 
                    logger.debug(f"Arrived iceberg legs - {iceberg_legs} with iceberg quantity {self._get_order_freeze_limit()}")
                    logger.debug(
                        "Placing SELL ICEBERG order",
                        extra={
                            "tradingsymbol": trading_symbol,
                            "exchange": exchange,
                            "transaction_type": self.kite.TRANSACTION_TYPE_SELL,
                            "quantity": quantity,
                            "order_type": order_type,
                            "product": self.kite.PRODUCT_MIS,
                            "variety": "iceberg",
                            "price": price,
                            "iceberg_legs": iceberg_legs,
                            "iceberg_quantity": self._get_order_freeze_limit(),
                            "ltp": ltp
                        }
                    )                    
                    order_id = self.kite.place_order(
                        tradingsymbol=trading_symbol, 
                        exchange = exchange,
                        transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                        quantity= str(quantity),
                        order_type = order_type,
                        product = self.kite.PRODUCT_MIS,
                        variety="iceberg", 
                        price = price,
                        iceberg_legs=iceberg_legs,
                        iceberg_quantity= self._get_order_freeze_limit()
                    )

            logger.info(f"Kite Sell order placed successfully. Order ID: {order_id}")
            self._trader.frontend_data_socket.emit('status_message',{"success": True, "message": "Order executed successfully"})
            self._trader.frontend_data_socket.emit('sell_order_result',{"success": True, "tradingsymbol": trading_symbol , "lots": original_quantity, "position_key": position_key})
            return order_id
        except Exception as e:
            logger.error(f"Kite sell_units error: {e}")
            self._trader.frontend_data_socket.emit('status_message', {"success": False, "message": "Error placing order..."})
            self._trader.frontend_data_socket.emit('sell_order_result',{"success": False, "tradingsymbol": trading_symbol , "position_key": position_key, "error": str(e)})
            return None

    def subscribe_to_all(self, instruments):
        try:
            socket = self.kite_instance.get_kite_socket_connection()
            instrument_tokens = [int(i) for i in instruments]
            if socket and socket.is_connected():
                socket.subscribe(instrument_tokens)
                socket.set_mode(socket.MODE_FULL, instrument_tokens)
            logger.debug(f"Subscribed to {instrument_tokens}")
        except Exception as e:
            logger.error("Error in subscribing to instruments {e}")
        
    def unsubscribe_from_all(self, instruments):
        return super().unsubscribe_from_all(instruments)

    def _record_feed_ticks(self, count):
        """
        Feed-rate sampler for the Kite websocket - mirrors the MStock
        sampler (KITE FEED RATE / KITE FEED STALL lines). Kite batches
        several instrument ticks per on_ticks call; counting per tick
        (not per batch) keeps the numbers comparable with MStock, which
        counts per 379-byte quote packet. Runs on the ticker dispatch
        thread; _feed_lock guards against the stall watchdog reading
        concurrently.
        """
        if count <= 0:
            return
        now_ts = time.time()
        with self._feed_lock:
            if not self._feed_stats_window_start:
                self._feed_stats_window_start = now_ts
                self._feed_last_tick_ts = now_ts
            self._feed_packets += count
            gap = now_ts - self._feed_last_tick_ts
            self._feed_last_tick_ts = now_ts
            if gap > self._feed_max_gap:
                self._feed_max_gap = gap
            if gap > 10:
                logger.warning(
                    f"KITE FEED STALL | no tick for {gap:.1f}s - grid "
                    f"prices were frozen during this window"
                )
            if now_ts - self._feed_stats_window_start >= 60:
                elapsed = now_ts - self._feed_stats_window_start
                rate = self._feed_packets / elapsed * 60
                logger.info(
                    f"KITE FEED RATE | {self._feed_packets} ticks in "
                    f"{elapsed:.0f}s ({rate:.1f}/min) | longest quiet "
                    f"gap {self._feed_max_gap:.1f}s"
                )
                self._feed_stats_window_start = now_ts
                self._feed_packets = 0
                self._feed_max_gap = 0.0

    def _start_feed_stall_watchdog(self, shutdown_event):
        """
        Detect ONGOING feed silence. The sampler inside on_ticks can
        only flag a stall after the feed resumes (the gap is measured
        when the next tick lands) - if the websocket dies completely,
        nothing would ever be logged. This watchdog wakes every 10s and
        warns while the silence continues. Market-closed periods are
        skipped so off-hours sessions do not raise false alarms.
        """

        def _watch():
            while not shutdown_event.is_set():
                shutdown_event.wait(timeout=10)
                if shutdown_event.is_set():
                    break
                if not is_market_open():
                    continue
                with self._feed_lock:
                    last_ts = self._feed_last_tick_ts
                if not last_ts:
                    continue
                silent_for = time.time() - last_ts
                if silent_for > 10:
                    logger.warning(
                        f"KITE FEED STALL | no tick for {silent_for:.0f}s "
                        f"(ongoing) - prices on the grid are frozen"
                    )

        threading.Thread(
            target=_watch, daemon=True, name="kite-feed-watchdog"
        ).start()

    def start_socket_connection(self, shutdown_event, trader_instance):
        socket = self.kite_instance.get_kite_socket_connection()

        def on_ticks(ws, ticks):
            self._record_feed_ticks(len(ticks))
            for tick in ticks:
                instrument_token = tick["instrument_token"]
                price = tick["last_price"]
                ohlc = tick.get("ohlc") or {}
                self._trader.set_latest_price(
                    str(instrument_token),
                    None,
                    price,
                    ohlc.get("close")
                )

        def on_connect(ws, response):
            logger.info("Connected to Kite WebSocket")
            # Setup must NOT run on the websocket dispatch thread:
            # setup_woc waits for the first ticks, which can only be
            # delivered by this very thread. Running it on a separate
            # thread keeps on_ticks flowing while setup waits, so the
            # websocket (not REST) seeds the option prices.
            threading.Thread(
                target=self._trader.setup_woc_subscriptions,
                daemon=True,
                name="woc-setup",
            ).start()

        def on_close(ws, code, reason):
            logger.warning(f"Kite WebSocket closed: {code}, {reason}")

        # def on_order_update(ws, data):
        #     logger.info(f"Kite Order update: {data}")
        #     self._trader.refresh_open_pos_buy_price()
        #     subscribe_instruments = self._trader.get_relevant_instruments_to_track()
        #     if subscribe_instruments:
        #         try:
        #             subscribe_instruments_list = [int(i) for i in subscribe_instruments]
        #             ws.subscribe(subscribe_instruments_list)
        #             ws.set_mode(ws.MODE_FULL, subscribe_instruments_list)
        #         except Exception as e:
        #             logger.error(f"Error casting elements of subscribe_instruments to int: {e}. Instruments: {subscribe_instruments}")


        def on_order_update(ws, data):
            logger.info(f"Kite Order update: {data}")
            try:
                self._trader.refresh_open_pos_buy_price()
            except Exception as e:
                logger.error(f"Failed to refresh positions after Kite order update: {e}", exc_info=True)
                return

            subscribe_instruments = self._trader.get_relevant_instruments_to_track()
            if subscribe_instruments:
                try:
                    subscribe_instruments_list = [int(i) for i in subscribe_instruments]
                    ws.subscribe(subscribe_instruments_list)
                    ws.set_mode(ws.MODE_FULL, subscribe_instruments_list)
                except Exception as e:
                    logger.error(f"Error casting elements of subscribe_instruments to int: {e}. Instruments: {subscribe_instruments}")

        socket.on_ticks = on_ticks
        socket.on_connect = on_connect
        socket.on_close = on_close
        socket.on_order_update = on_order_update

        socket.connect(threaded=True)

        # Ongoing-stall watchdog: the in-on_ticks sampler only reports a
        # stall once the feed has resumed; this one warns while the
        # silence continues.
        self._start_feed_stall_watchdog(shutdown_event or threading.Event())
        # if shutdown_event:
        #     shutdown_event.wait()
        #socket.close()

    

    def dev_start(self):
        self.kite_instance.initialise_kite_for_dev()

    def prod_start(self , request_token=None):
        if request_token:
            self.kite_instance.start_session(request_token)
        else:
            self.kite_instance.initialise_kite_for_prod()

    def download_instrument_list(self,exchange,underlying):
        
        filename = resolve_data_path("kite_instruments.csv")
        token_file = resolve_data_path("access_token.json")

        # ✅ Check if instruments already downloaded today
        try:
            if os.path.exists(filename) and os.path.exists(token_file):

                with open(token_file, "r") as f:
                    token_data = json.load(f)

                last_run = token_data.get("kite_last_downloaded_instruments_timestamp")

                if last_run:
                    last_run_date = datetime.fromisoformat(last_run).date()
                    today = datetime.now().date()
                    # Forcing to download everyday as for some reason it is not getting downloaded at all as the date all the time remains the same
                    # Look into this issue later
                    if last_run_date == today:
                        logger.info(f"{filename} already downloaded today. Using existing file.")

                        # still compute meta from existing file
                        self.compute_instrument_meta(filename, underlying)
                        return

        except Exception as e:
            logger.warning(f"Could not validate last run timestamp: {e}")

        logger.info(f"Downloading Instrument List for {exchange} and {underlying}")

        
        logger.info(f"Downloading Instrument List for {exchange} and {underlying}")
        try:
            mcx_instruments = self.kite.instruments(exchange)
            if not mcx_instruments:
                logger.error("No instruments fetched. Cannot proceed to save.")
            else:
                filename = resolve_data_path('kite_instruments.csv')
                headers = mcx_instruments[0].keys()
                with open(filename, 'w', newline='') as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=headers)
                    writer.writeheader()
                    writer.writerows(mcx_instruments)
                logger.info(f"Successfully saved instruments to {filename}")

                from core.utils import write_to_json
                write_to_json({"kite_last_downloaded_instruments_timestamp": datetime.now().isoformat()}, "access_token.json")

                self.compute_instrument_meta("kite_instruments.csv", underlying)

        except NameError:
            logger.error("The 'kite' object is not defined. Please ensure you have properly initialized and authenticated your KiteConnect object before running this code.")
        except Exception as e:
            logger.error(f"An error occurred during fetching or saving: {e}")

    def compute_instrument_meta(self, csv_file, underlying):
        """
        Derive the instrument meta (exchange, near-month future token,
        near option expiry, expire-together flag, strike interval) from
        the instrument CSV. Nothing is written to appconfig.json anymore;
        the meta lives in memory and is exposed via get_instrument_meta().
        """
        csv_file = resolve_data_path(csv_file)
        logger.debug(f"Instrument meta to be computed for {csv_file} and for underlying {underlying}")
        if not underlying:
            logger.debug("No underlying supplied; skipping instrument meta computation.")
            return None

        df = pd.read_csv(csv_file)
        underlying = underlying.upper().strip()

        # Mapping for index family
        index_mapping = {
            'NIFTY': ['NIFTY'],
            'SENSEX': ['SENSEX'],
            'CRUDEOIL': ['CRUDEOIL'],
            'CRUDEOILM': ['CRUDEOILM']
        }
        family = index_mapping.get(underlying, [underlying])
        today = pd.Timestamp.now().normalize()

        # --- 1️⃣ Find nearest future ---
        fut_df = df[
            (df['instrument_type'] == 'FUT') &
            (df['name'].isin(family))
        ].copy()

        if fut_df.empty:
            raise ValueError(f"No futures found for {underlying}")

        fut_df['expiry'] = pd.to_datetime(fut_df['expiry'])
        nearest_future = fut_df.sort_values('expiry').iloc[0]

        exchange = str(nearest_future['exchange'])
        trading_symbol = str(nearest_future['tradingsymbol'])
        expiry_date_future = nearest_future['expiry']

        # --- 2️⃣ Find nearest options (weekly for NIFTY/SENSEX, monthly
        # for the CRUDEOIL family; replaces the old manual
        # CRUDEOIL_EXPIRY_DATE constant) ---
        opt_df = df[
            (df['instrument_type'].isin(['CE', 'PE'])) &
            (df['name'].isin(family))
        ].copy()

        near_option_expiry = None
        # Fixed 100-point strike grid - deliberate parity with the
        # MStock adapter (which also enforces 100 so a stale instrument
        # file cannot bring back 50). Do NOT derive this from the CSV:
        # NIFTY's real 50-point spacing was explicitly rejected by the
        # user ("Call and PUT strikes interval ... should be 100").
        strike_interval = 100
        expire_together = False  # default

        if not opt_df.empty:
            opt_df['expiry'] = pd.to_datetime(opt_df['expiry'])
            future_opts = opt_df[opt_df['expiry'] >= today]

            if not future_opts.empty:
                near_option_expiry_ts = future_opts.sort_values('expiry')['expiry'].iloc[0]
                near_option_expiry = near_option_expiry_ts.date()

                # --- 4️⃣ Determine if near future and near option expire together ---
                if near_option_expiry is not None:
                    expire_together = near_option_expiry == expiry_date_future.date()

        if not exchange:
            exchange = get_exchange_for_underlying(underlying) or ""

        self._instrument_meta = {
            "underlying": underlying,
            "exchange": exchange,
            "near_month_future_token": trading_symbol,
            "near_option_expiry": near_option_expiry.isoformat() if near_option_expiry else None,
            "expire_together": bool(expire_together),
            "strike_interval": int(strike_interval),
            "generated_on": datetime.now().date().isoformat(),
        }

        logger.info(f"Computed instrument meta: {json.dumps(self._instrument_meta, indent=4)}")
        return self._instrument_meta

    def get_quote(self, tradingsymbol):

        logger.info(f"Getting Quote for {tradingsymbol} from Kite API...")

        try:

            data = self.get_instrument_details(tradingsymbol)

            # 🟢 Skip REST calls when market closed
            if not is_market_open():

                logger.info("Market closed — using instrument last_price")

                price = float(data.get("last_price", 0))

                if price > 0:
                    return [price, price, price]

            quote = self.fetch_instrument_quote(data["exchange"], tradingsymbol)

            logger.debug(f"Raw quote data received: {quote}")

            # Kite response key format
            key = f"{data['exchange']}:{tradingsymbol}"

            if not quote or key not in quote:
                raise Exception(f"Quote missing for {key}")

            q = quote[key]

            ltp = q["last_price"]
            open_price = q["ohlc"]["open"]
            close_price = q["ohlc"].get("close")

            return [ltp, open_price, close_price]

        except Exception as e:

            logger.error(f"Fetch Instrument Quote failed for {tradingsymbol}: {e}")

            if tradingsymbol.endswith(("CE", "PE")):
                logger.warning(f"Returning fallback option LTP 100 for {tradingsymbol}.")
                return [100, 100]

            fallback = self._fallback_ltp(tradingsymbol)
            logger.warning(f"Returning fallback LTP {fallback} for {tradingsymbol}.")
            return [fallback, fallback]

    # def get_quotes_batch(self, symbols):

    #     try:

    #         instruments = [f"NFO:{s}" for s in symbols]

    #         logger.info(f"Fetching batch quotes for {len(instruments)} instruments")

    #         data = self.kite.quote(instruments)

    #         result = {}

    #         for sym in symbols:

    #             key = f"NFO:{sym}"

    #             if key in data:

    #                 q = data[key]

    #                 result[sym] = [
    #                     q["last_price"],
    #                     q["ohlc"]["open"]
    #                 ]

    #         return result

    #     except Exception as e:

    #         logger.error(f"Batch quote failed: {e}")
    #         return {}            

    # def get_quotes_batch(self, symbols):
    #     try:
    #         instruments = []

    #         # Determine exchange from the actual instrument
    #         for sym in symbols:

    #             if sym.upper().startswith(("CRUDEOIL", "CRUDEOILM")):
    #                 exchange = "MCX"
    #             else:
    #                 exchange = "NFO"

    #             instruments.append(f"{exchange}:{sym}")

    #         logger.info(
    #             f"Fetching batch quotes for {len(instruments)} instruments: "
    #             f"{instruments}"
    #         )

    #         data = self.kite.quote(instruments)

    #         result = {}

    #         for sym in symbols:

    #             if sym.upper().startswith(("CRUDEOIL", "CRUDEOILM")):
    #                 exchange = "MCX"
    #             else:
    #                 exchange = "NFO"

    #             key = f"{exchange}:{sym}"

    #             if key in data:
    #                 q = data[key]

    #                 result[sym] = [
    #                     q["last_price"],
    #                     q["ohlc"]["open"]
    #                 ]
    #         return result
    #     except Exception as e:
    #         logger.error(f"Batch quote failed: {e}")
    #         return {}    

    def _resolve_quote_exchange(self, sym):
        """
        Exchange for a quote request, resolved from the instrument
        master (correct for every exchange incl. BFO/SENSEX) with the
        old CRUDEOIL-prefix heuristic as the fallback for symbols that
        are not in the master.
        """
        self.preload_instrument_data()
        row = self._instrument_data_loaded.get(sym)
        exchange = str((row or {}).get("exchange") or "").strip().upper()
        if exchange:
            return exchange
        if sym.upper().startswith(("CRUDEOIL", "CRUDEOILM")):
            return "MCX"
        return "NFO"

    def get_quotes_batch(self, symbols):
        try:
            instruments = []

            for sym in symbols:
                exchange = self._resolve_quote_exchange(sym)
                instruments.append(f"{exchange}:{sym}")

            logger.info(
                f"Fetching batch quotes for {len(instruments)} instruments: "
                f"{instruments}"
            )

            data = self.kite.quote(instruments)

            result = {}

            for sym in symbols:
                key = f"{self._resolve_quote_exchange(sym)}:{sym}"

                if key in data:
                    q = data[key]

                    result[sym] = [
                        q["last_price"],
                        q["ohlc"]["open"],
                        q["ohlc"].get("close")
                    ]

            return result

        except Exception as e:
            logger.error(f"Batch quote failed: {e}")
            return {}

        
    def cancel_order(self, order_id):
        logger.info(f"Cancelling order: {order_id} via Kite...")
        try:
            self.kite.cancel_order(order_id)
            logger.info(f"Order {order_id} cancelled successfully.")
            return True
        except Exception as e:
            logger.error(f"Cancel order error for {order_id}: {e}")
            return False

    def cancel_all_pending_orders(self):
        logger.info("Cancelling ALL pending orders via Kite...")
        try:
            pending_orders = self.fetch_all_pending_orders()
            logger.info(f"Pending orders found: {len(pending_orders)}")

            cancelled = 0
            for order in pending_orders:
                order_id = order.get("order_id")
                if not order_id:
                    logger.error(f"Skipping—order has no order_id: {order}")
                    continue

                if self.cancel_order(order_id):
                    cancelled += 1

                # Kite rate-limits order requests; pace the burst.
                time.sleep(0.3)

            logger.info(f"Cancelled {cancelled}/{len(pending_orders)} pending orders.")
            return cancelled
        except Exception as e:
            logger.error(f"Cancel all pending orders error: {e}")
            return 0

    def fetch_all_pending_orders(self):
        logger.info("Fetching pending orders from Kite API...")
        try:
            orders = self.kite.orders() or []

            terminal_statuses = ("COMPLETE", "REJECTED", "CANCELLED")
            pending_orders = [
                {
                    "tradingsymbol": o.get("tradingsymbol"),
                    "timestamp": o.get("order_timestamp"),
                    "transaction_type": o.get("transaction_type"),
                    "quantity": o.get("quantity"),
                    "average_price": o.get("average_price"),
                    # Resting LIMIT price (the U/D exit target); stays
                    # the order's limit while pending, unlike
                    # average_price which only fills in as the order
                    # executes.
                    "price": o.get("price"),
                    "order_type": o.get("order_type"),
                    "order_status": o.get("status"),
                    "order_id": o.get("order_id"),
                    # For the pending-grid joins (position leg + lot size).
                    "token": str(o.get("instrument_token") or ""),
                    "exchange": o.get("exchange"),
                }
                for o in orders
                if str(o.get("status", "") or "").upper() not in terminal_statuses
            ]

            logger.info(f"Total orders: {len(orders)}, Pending: {len(pending_orders)}")
            return pending_orders
        except Exception as e:
            logger.error(f"Error fetching pending orders: {e}")
            return []


