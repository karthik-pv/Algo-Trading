import asyncio
import csv
import http.client
import json
import logging
import datetime
import calendar
import time
import requests
import os
import tempfile
import ssl
import shutil


import pandas as pd
from datetime import timedelta  

import camelot
import re
from collections import defaultdict, deque




from utils import get_trading_symbols_from_json , find_matching_object , fetch_from_json , write_to_json
from core.mstock_connector import MStockSingleton
from interface.broker_interface import BrokerInterface
from core.trade_logic import Trader_Singleton
from adapter.mstock_utils import (
    position_attribute_mgmt, fund_summary_attribute_mgmt, order_attribute_mgmt,
    instr_det_attrib_mgmt, normalize_contract_symbol, write_orders_workbook,
)
from loguru import logger


_SSL_CONTEXT = ssl.create_default_context()


class MStockAdapter(BrokerInterface):

    def __init__(self):
        logger.info("Initializing MStockAdapter...")
        self._trader = Trader_Singleton()
        self.mstock_instance = MStockSingleton()

        self._instrument_cache = {}       # stores processed instruments keyed by tradingsymbol
        self._instrument_data_loaded = {}  # raw data loaded from CSV once
        self._csv_loaded = False           # flag to check if CSV has been read


        self._underlying = fetch_from_json("constants.json" , "UNDERLYING")
        self._ORDER_FREEZE_LIMIT = fetch_from_json("constants.json", f"{'NIFTY' if self._underlying == 'NIFTY' else 'SENSEX'}_ORDER_FREEZE_LIMIT")

        self._MSTOCK_INSTRUMENT_FILE = "mstock_instrument_list_reduced.json"

        self.constants = {
            "EXCHANGE": "",
            "NEAR_MONTH_FUTURE_TOKEN": "",
            "EXPIRY_MONTH": "",
            "EXPIRY_YEAR": "",
            "NIFTY_EXPIRY_DATE": "",
            "SENSEX_EXPIRY_DATE": "",
            "NEAR_FUTURE_NEAR_OPTION_EXPIRE_TOGETHER": False
        }

    def update_constants(self, new_values: dict):
        """Update dictionary with new values"""
        self.constants.update(new_values)

    def get_constant(self, key):
        """Retrieve a specific value"""
        return self.constants.get(key)

    def show_constants(self):
        """Print or return the full dictionary"""
        logger.debug(f"Updated Constants are : {self.constants}")


    def cancel_order(self, order_id):
        logger.info(f"Cancelling order: {order_id}")

        try:
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)

            headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
                "Content-Type": "application/json"
            }
            # payload = {
            #     "data": {
            #         "cancelall": "true"
            #     }
            # }
            endpoint = f"/openapi/typeb/orders/regular/{order_id}"
            # logger.debug(f"Cancel payload: {payload}")

            #conn.request("DELETE", endpoint, json.dumps(payload), headers)

            # endpoint = "/openapi/typea/orders/cancelall"
            # EMPTY JSON BODY (required)
            # payload = "{}"
            # conn.request("POST", endpoint,payload, headers)
            conn.request("POST", endpoint,body = "{}", headers=headers)

            response = conn.getresponse()
            raw = response.read().decode("utf-8")
            conn.close()

            logger.debug(f"Cancel HTTP status: {response.status}")
            logger.debug(f"Cancel raw response: {raw}")

            # Treat empty response as success
            if response.status in (200, 202, 204):
                logger.info(f"Order {order_id} cancelled successfully.")
                return True

            # Parse response JSON
            if raw.strip():
                try:
                    res = json.loads(raw)
                    status = str(res.get("status", "")).lower()
                    if status in ["success", "true", "ok"]:
                        logger.info(f"Order {order_id} cancelled successfully.")
                        return True
                    else:
                        logger.error(f"Cancel failed: {res}")
                        return False
                except Exception:
                    # Unexpected body - treat as success per m.Stock inconsistency
                    logger.warning(f"Non-JSON cancel response, treating as success.")
                    return True

            return False

        except Exception as e:
            logger.error(f"Cancel order error for {order_id}: {e}")
            return False


    def fetch_all_pending_orders(self):
        logger.info("Fetching pending orders from M.Stock...")

        try:
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
            headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
            }

            conn.request('GET', '/openapi/typeb/orders', headers=headers)
            raw = conn.getresponse().read().decode("utf-8")
            conn.close()

            data = json.loads(raw).get("data", [])

            logger.debug(f"Raw Pending orders data: {data}")

            # Apply your existing transformation logic
            orders = order_attribute_mgmt(data)

            # Filter using your rule: status O-Pending
            pending_orders = [
                o for o in orders
                if str(o.get("order_status", 0)) == "O-Pending" 
            ]

            logger.info(f"Total orders: {len(orders)}, Pending: {len(pending_orders)}")
            self._trader.frontend_data_socket.emit('update_pending_orders', pending_orders)
            return pending_orders

        except Exception as e:
            logger.error(f"Error fetching pending orders: {e} \n\n CONSIDER LOGGING IN AGAIN \n\n")
            return []

    def cancel_all_pending_orders(self):
        logger.info("Cancelling ALL pending orders...")

        pending_orders = self.fetch_all_pending_orders()
        logger.info(f"Pending orders found: {len(pending_orders)}")

        cancelled = 0

        for order in pending_orders:
            order_id = order["order_id"]

            if not order_id:
                logger.error(f"Skipping—order has no order_id: {order}")
                continue

            success = self.cancel_order(order_id)

            if success:
                cancelled += 1

        logger.info(f"Cancelled {cancelled}/{len(pending_orders)} pending orders.")

        return cancelled



    def fetch_all_orders(self):
        logger.info("Fetching all orders from M.Stock...")
        try:
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
            headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
            }
            conn.request('GET', '/openapi/typeb/orders', headers=headers)
            # raw_response = conn.getresponse().read().decode("utf-8")
            # logger.info(f"Raw orders response: {raw_response}")
            #response = order_attribute_mgmt(json.loads(conn.getresponse().read().decode("utf-8"))["data"])

            raw = json.loads(conn.getresponse().read().decode("utf-8"))
            #logger.info(f"API raw response: {raw}")
            
            orders = raw["data"]
            #logger.info(f"Orders Raw Data : {orders}")

            response = order_attribute_mgmt(orders)

            #logger.info(f"Orders data post attributes management: {response}")

            logger.info(f"Total No. of orders without any filter: {len(response)}")
            
            return response
        except Exception as e:
            logger.error(
                f"Error fetching orders: {e} \n\n CONSIDER LOGGING IN AGAIN \n\n"
            )
    
    def fetch_order_executed_price(self,orderid):
        logger.info(f"Fetching executed price for order id {orderid} from M.Stock...")
        try:
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
            headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
            }
            conn.request('GET', '/openapi/typeb/orders', headers=headers)
            # raw_response = conn.getresponse().read().decode("utf-8")
            # logger.info(f"Raw orders response: {raw_response}")
            #response = order_attribute_mgmt(json.loads(conn.getresponse().read().decode("utf-8"))["data"])

            raw = json.loads(conn.getresponse().read().decode("utf-8"))
            #logger.info(f"API raw response: {raw}")
            
            orders = raw["data"]
            #logger.debug(f"Orders Raw Data : {orders}")
            logger.debug(f"Total No. of orders fetched: {len(orders)}")
            
            logger.debug(f"Searching for order id {orderid} in fetched orders")
            
            order = next(
                (
                    o for o in orders
                    if str(o.get("orderid")) == str(orderid)
                    and str(o.get("orderstatus") or o.get("status") or "").lower()
                    in {"traded", "complete", "o-pending", "pending"}
                ),
                None,
            )
            
            if not order:
                logger.warning(f"Order {orderid} not yet traded")
                return None

            logger.debug(f"Order found for the order id {orderid}: {order}")

            # response = order_attribute_mgmt([order])
            # logger.info(f"Orders data post attributes management: {response}")

            average_price = order.get("averageprice")
            if average_price in (None, "", "0", 0, 0.0):
                logger.warning(f"Order {orderid} has no executed average price yet")
                return None

            avg_price = float(average_price)

            logger.debug(f"Executed average price for order {orderid}: {avg_price}")
            return avg_price
        
        except Exception as e:
            logger.error(f"Error fetching orders: {e} \n\n CONSIDER LOGGING IN AGAIN \n\n")
    
    def fetch_all_instruments(self):
        logger.info("Fetching all instruments from M.Stock...")
        conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
        headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
            }
        conn.request('GET', '/openapi/typeb/instruments/OpenAPIScripMaster', headers=headers)
        instrument_data = json.loads(conn.getresponse().read().decode("utf-8"))
        filename = "../"+ self._MSTOCK_INSTRUMENT_FILE
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(instrument_data, f, ensure_ascii=False, indent=4)
        return
    

    def fetch_all_positions(self):
        logger.info("Fetching all positions from M.Stock...")
        try:
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
            headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
            }
            conn.request("GET", "/openapi/typeb/portfolio/positions", headers=headers)
            response = json.loads(conn.getresponse().read().decode("utf-8"))
            #logger.info(f"Raw positions response: {response}")
            positions = position_attribute_mgmt(response["data"])
            #logger.info(f"Processed positions: {positions}")
            logger.debug(f"Total positions fetched: {len(positions)}")
            open_positions = [
                open_position
                for open_position in positions
                if int(open_position["quantity"]) > 0
            ]
            logger.debug(f"Open positions: {open_positions}")
            
            return open_positions
        except Exception as e:
            logger.error(
                f"Error fetching positions: {e} \n\n CONSIDER LOGGING IN AGAIN \n\n"
            )

    def get_ltp(self , tradingsymbol):
        logger.info(f"Getting LTP for {tradingsymbol} from M.Stock...")
        try:
            data = self.get_instrument_details(tradingsymbol)
            #hading during non marketing hours
            try:
                
                ltp = self.fetch_instrument_quote(data["exch_seg"] , data["token"])["data"]["fetched"][0]["ltp"]
                return ltp
            except Exception as e:
                logger.error(f"Fetch Instrument Quote failed for {tradingsymbol}: {e}")
                # Handle fallback for Nifty / Sensex
                if "NIFTY" in tradingsymbol.upper():
                    logger.warning("Returning fallback LTP = 25000 for NIFTY.")
                    return 25900
                elif "SENSEX" in tradingsymbol.upper():
                    logger.warning("Returning fallback LTP = 84000 for SENSEX.")
                    return 84000
                else:
                    logger.warning("Returning default fallback LTP = 25000.")
                    return 25900
            
        except Exception as e:
            logger.error(f"Error getting LTP for {tradingsymbol}: {e}")
            # Same fallback logic for outer exception too
            if "NIFTY" in tradingsymbol.upper():
                return 25900
            elif "SENSEX" in tradingsymbol.upper():
                return 84000
            else:
                return 25900
    def get_quote(self , tradingsymbol):
        logger.info(f"Getting Quote for {tradingsymbol} from M.Stock...")
        try:
            data = self.get_instrument_details(tradingsymbol)
            #handing during non marketing hours
            # mstock has exg_seg as NSE..For Kite it is segment NFO-FUT
            #quote = self.fetch_instrument_quote(data["segment"] , data["token"])
            quote = self.fetch_instrument_quote(data["exch_seg"], data["token"])

            # ✅ HARD VALIDATION BEFORE ACCESSING
            if (
                not quote or
                not isinstance(quote, dict) or
                not quote.get("data") or
                not quote["data"].get("fetched") or
                len(quote["data"]["fetched"]) == 0
            ):
                raise Exception

            ltp = quote["data"]["fetched"][0]["ltp"]
            open = quote["data"]["fetched"][0]["open"]  
            return [ltp,open]
        except Exception as e:
            logger.error(f"Fetch Instrument Quote failed for {tradingsymbol}: {e}")
            # Handle fallback for Nifty / Sensex
            if "NIFTY" in tradingsymbol.upper():
                logger.warning(f"Returning fallback LTP {self._trader._NIFTY_FALLBACK_LTP} for NIFTY.")
                return [self._trader._NIFTY_FALLBACK_LTP,self._trader._NIFTY_FALLBACK_LTP]
            elif "SENSEX" in tradingsymbol.upper():
                logger.warning(f"Returning fallback LTP {self._trader._SENSEX_FALLBACK_LTP} for SENSEX.")
                return [self._trader._SENSEX_FALLBACK_LTP,self._trader._SENSEX_FALLBACK_LTP]
            else:
                logger.warning("Returning fallback LTP {self._trader._NIFTY_FALLBACK_LTP} for NIFTY.")
                return [self._trader._NIFTY_FALLBACK_LTP,self._trader._NIFTY_FALLBACK_LTP]


    def get_quotes_batch(self, symbols):

        try:
            logger.info(
                f"Fetching batch quotes for {len(symbols)} instruments from M.Stock"
            )

            # ---------------------------------------------------------
            # Get instrument details for all trading symbols
            # ---------------------------------------------------------
            instruments = []

            for symbol in symbols:
                data = self.get_instrument_details(symbol)

                if not data:
                    logger.warning(
                        f"Instrument details not found for {symbol}"
                    )
                    continue

                instruments.append({
                    "symbol": symbol,
                    "exchange": data["exch_seg"],
                    "token": str(data["token"])
                })

            if not instruments:
                logger.warning("No instruments available for batch quote")
                return {}

            # ---------------------------------------------------------
            # Group tokens by exchange
            # ---------------------------------------------------------
            exchange_tokens = {}

            for item in instruments:
                exchange = item["exchange"]
                token = item["token"]

                if exchange not in exchange_tokens:
                    exchange_tokens[exchange] = []

                exchange_tokens[exchange].append(token)

            # ---------------------------------------------------------
            # M.Stock batch quote request
            # ---------------------------------------------------------
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)

            headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
                "Content-Type": "application/json"
            }

            json_data = {
                "mode": "OHLC",
                "exchangeTokens": exchange_tokens
            }

            logger.debug(
                f"M.Stock batch quote request: {json_data}"
            )

            conn.request(
                "POST",
                "/openapi/typeb/instruments/quote",
                json.dumps(json_data),
                headers
            )

            response = conn.getresponse()
            raw_response = response.read().decode("utf-8")
            conn.close()

            logger.debug(
                f"M.Stock batch quote response: {raw_response}"
            )

            if not raw_response:
                logger.error("Empty response from M.Stock batch quote API")
                return {}

            response_data = json.loads(raw_response)

            if not response_data.get("status", False):
                logger.error(
                    f"M.Stock batch quote failed: {response_data}"
                )
                return {}

            fetched = response_data.get("data", {}).get("fetched", [])

            if not fetched:
                logger.warning("No quote data returned by M.Stock")
                return {}

            # ---------------------------------------------------------
            # Build token -> symbol mapping
            # ---------------------------------------------------------
            token_to_symbol = {
                item["token"]: item["symbol"]
                for item in instruments
            }

            result = {}

            # ---------------------------------------------------------
            # Process returned quotes
            # ---------------------------------------------------------
            for quote in fetched:

                # token = str(
                #     quote.get("token")
                #     or quote.get("symboltoken")
                #     or quote.get("instrument_token")
                #     or ""
                # )

                # MSTOCK_SENSEX 
                token = str(
                    quote.get("token")
                    or quote.get("symbolToken")
                    or quote.get("symboltoken")
                    or quote.get("instrument_token")
                    or ""
                )


                if not token:
                    logger.warning(
                        f"Unable to identify token in quote: {quote}"
                    )
                    continue

                symbol = token_to_symbol.get(token)

                if not symbol:
                    logger.warning(
                        f"Returned token {token} not found in requested instruments"
                    )
                    continue

                ltp = quote.get("ltp")
                open_price = quote.get("open")

                if ltp is None:
                    logger.warning(
                        f"LTP missing for {symbol}: {quote}"
                    )
                    continue

                result[symbol] = [
                    ltp,
                    open_price if open_price is not None else ltp
                ]

            logger.info(
                f"M.Stock batch quotes received: "
                f"{len(result)}/{len(symbols)}"
            )

            return result

        except Exception as e:

            logger.error(
                f"M.Stock batch quote failed: {e}"
            )

            return {}


    def fetch_all_trades(self):
        logger.info("Fetching all trades from M.Stock...")
        return
    
    def fetch_fund_summary(self):
        logger.info("Fetching fund summary from M.Stock...")
        try:
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
            headers = {
                    "X-Mirae-Version": "1",
                    "X-PrivateKey": self.mstock_instance._api_key,
                    "Authorization": f"Bearer {self.mstock_instance._access_token}",
                }
            conn.request('GET', '/openapi/typeb/user/fundsummary', headers=headers)
            raw_response = conn.getresponse().read().decode("utf-8")

            if not raw_response:
                raise ValueError("Empty response from server")
            
            response = json.loads(raw_response)

            logger.debug("Fund summary response: {response}", response=response)


            # ✅ 1. HANDLE INVALID SESSION CLEANLY
            if not response.get("status"):
                error_msg = response.get("message", "Unknown API error")
                logger.error(f"Fund summary API error: {error_msg}")

                # Optional: trigger logout or UI popup
                if "invalid session" in error_msg.lower():
                    logger.critical("Session expired. User must log in again.")
                    self._trader.on_session_expired()   # <-- your logout / popup hook

                return None  # ✅ graceful exit
        
            # ✅ 2. HANDLE MISSING DATA
            if response.get("data") is None:
                logger.error("Fund summary data is None")
                return None

            # ✅ 3. SAFE PROCESSING
            fund_summary = fund_summary_attribute_mgmt(response)
            self._trader.update_fund_summary(fund_summary)

            return fund_summary

        except json.JSONDecodeError:
            logger.error("Invalid JSON received from fund summary API")
            return None
        
        except Exception as e:
            logger.error(f"Error fetching fund summary: {e}")
            return 
    
    def fetch_instrument_quote(self , exchange , instrument_token):
        logger.info(f"Fetching instrument quote for {exchange} {instrument_token} from M.Stock...")
        try:
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
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
            logger.info(f"Instrument quote fetched for {exchange} {instrument_token} is : {response}")
            if not response.get("status", True):
                logger.error("Instrument quote failed: {}", response)
            else:
                logger.debug("Instrument quote response: {}", response)
            return response
        except Exception as e:
            logger.error(f"Error fetching quotes {e}")
    
    #Fetch all available option expiries for a given underlying (e.g. 'NIFTY')
    def fetch_expiries(self , underlying):
        logger.info(f"fetch_expiries {underlying} from M.Stock...")
        try:
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
            headers = {
                    "X-Mirae-Version": "1",
                    "X-PrivateKey": self.mstock_instance._api_key,
                    "Authorization": f"Bearer {self.mstock_instance._access_token}",
                    "Content-Type": "application/json"
                }
             # 🔹 The endpoint for instruments list
            endpoint = f"/openapi/instruments/{underlying}"

            conn.request("GET", endpoint, headers=headers)
            raw_response = conn.getresponse().read().decode("utf-8")
            response = json.loads(raw_response)            

            if not response.get("status", True):
                logger.error(f"Failed to fetch expiries: {response}")
                return []
            
            instruments = response.get("data", [])
            expiries = sorted({inst.get("expiryDate") for inst in instruments if inst.get("expiryDate")})
            logger.info(f"Found {len(expiries)} expiries for {underlying}: {expiries}")

            return expiries
            
        except Exception as e:
            logger.error(f"Error fetch_expiries {e}")


    def sell_units(self, trading_symbol, instrument_token , quantity , exchange , ltp):
        try:
            logger.info(f"Selling units: {quantity} of {trading_symbol} ({instrument_token}) via M.Stock...")
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
            headers = {
                    "X-Mirae-Version": "1",
                    "X-PrivateKey": self.mstock_instance._api_key,
                    "Authorization": f"Bearer {self.mstock_instance._access_token}",
                    "Content-Type": "application/json"
                }
            
            # logger.info(f"Instrument {instrument_token} details: performing file lookup...")
            # data = find_matching_object("mstock_instrument_list.json", "token", str(instrument_token))
            # logger.info(f"Found instrument {instrument_token} in file mstock_instrument_list.json")


            # --- Fast path: check in-memory cache first ---
            if instrument_token in self._trader._five_weekly_option_contracts:
                logger.info(f"Check for instrument {instrument_token} in cache (_five_weekly_option_contracts)")
                data = self._trader._five_weekly_option_contracts[instrument_token]
                logger.info(f"Found instrument {instrument_token} in cache (_five_weekly_option_contracts)")
            else:
            # --- Fallback: slow file lookup ---
                logger.info(f"Instrument {instrument_token} not found in cache, performing file lookup...")
                data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE, "token", str(instrument_token))
                logger.info(f"Found instrument {instrument_token} in file "+ self._MSTOCK_INSTRUMENT_FILE)

            logger.debug(f"Instrument data for selling: {data}")
            trading_symbol_for_transaction = data["name"] or data.get("tradingsymbol", trading_symbol)
            #instrument_token = ["instrument_token"]
            lotsize = data["lotsize"]
            json_data = { 
                'variety': 'NORMAL',
                'tradingsymbol': trading_symbol_for_transaction,
                'symboltoken': instrument_token,
                'exchange': exchange,
                'transactiontype': 'SELL',
                'ordertype': 'MARKET',
                'quantity': str(int(quantity) * int(lotsize)),
                'producttype': 'CARRYFORWARD',
                'price': "0.00",
                'triggerprice': '0.00',
                'squareoff': '0.00',
                'stoploss': '0.00',
                'trailingStopLoss': '',
                'disclosedquantity': '0',
                'duration': 'DAY',
                'ordertag': 'my_algo',
            }
            logger.debug(f"Sell order payload: {json_data}")
            order_request_start = time.perf_counter()
            conn.request(
                'POST',
                '/openapi/typeb/orders/regular',
                json.dumps(json_data),
                headers
            )
            order_request_sent = time.perf_counter()
            response = conn.getresponse().read().decode("utf-8")
            order_response_received = time.perf_counter()
            logger.debug(
                f"M.Stock BUY HTTP TIMING | "
                f"request_to_send={order_request_sent - order_request_start:.3f}s | "
                f"response_wait={order_response_received - order_request_sent:.3f}s | "
                f"total={order_response_received - order_request_start:.3f}s"
            )
            logger.debug(f"Buy order response: {response}")
            conn.close()
            logger.debug(f"Sell order executed. Response: {response}")
            
            # Parse and check success
            try:
                res_json = json.loads(response)

                # Some M.Stock responses are lists, others are single dicts
                if isinstance(res_json, list):
                    res_json = res_json[0]

                # If status is missing or false → error
                if not res_json.get("status", False):
                    msg = res_json.get("message", "Unknown error")
                    logger.error(f"M.Stock SELL error for {trading_symbol}: {msg} | Raw: {res_json}")
                    raise Exception(msg)

            except json.JSONDecodeError:
                logger.error(f"Invalid JSON in SELL response: {response}")
                raise Exception("Invalid JSON response from M.Stock")

            except Exception as parse_err:
                logger.error(f"Error parsing SELL response: {parse_err}")
                raise

            self._trader.frontend_data_socket.emit('sell_order_result',{"success": True, "tradingsymbol": trading_symbol , "Quantity": quantity})
            #self._trader.frontend_data_socket.emit('status_message', {"message": "Refreshing position now..."})
            logger.info("Refreshing open positions and buy prices after SELL...This would update the fund summar / cash balance as well")
            self._trader.refresh_open_pos_buy_price()

            return True
            
        except Exception as e:
            logger.error(f"Error selling units {e}")
            raise
    def sell_units_temp(self, trading_symbol, instrument_token , quantity , exchange , ltp,limit_market="MARKET",price=0):
        try:
            logger.info(f"Selling units: {quantity} of {trading_symbol} ({instrument_token} {limit_market} {price} ) via M.Stock...")
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
            headers = {
                    "X-Mirae-Version": "1",
                    "X-PrivateKey": self.mstock_instance._api_key,
                    "Authorization": f"Bearer {self.mstock_instance._access_token}",
                    "Content-Type": "application/json"
                }
            
            # logger.info(f"Instrument {instrument_token} details: performing file lookup...")
            # data = find_matching_object("mstock_instrument_list.json", "token", str(instrument_token))
            # logger.info(f"Found instrument {instrument_token} in file mstock_instrument_list.json")


            # --- Fast path: check in-memory cache first ---
            if instrument_token in self._trader._five_weekly_option_contracts:
                logger.info(f"Check for instrument {instrument_token} in cache (_five_weekly_option_contracts)")
                data = self._trader._five_weekly_option_contracts[instrument_token]
                logger.info(f"Found instrument {instrument_token} in cache (_five_weekly_option_contracts)")
            else:
            # --- Fallback: slow file lookup ---
                logger.info(f"Instrument {instrument_token} not found in cache, performing file lookup...")
                data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE, "token", str(instrument_token))
                logger.info(f"Found instrument {instrument_token} in file "+ self._MSTOCK_INSTRUMENT_FILE)

            logger.debug(f"Instrument data for selling: {data}")
            trading_symbol_for_transaction = data["name"] or data.get("tradingsymbol", trading_symbol)
            #instrument_token = ["instrument_token"]
            lotsize = data["lotsize"]
            json_data = { 
                'variety': 'NORMAL',
                'tradingsymbol': trading_symbol_for_transaction,
                'symboltoken': instrument_token,
                'exchange': exchange,
                'transactiontype': 'SELL',
                'ordertype': limit_market,
                'quantity': str(int(quantity) * int(lotsize)),
                'producttype': 'CARRYFORWARD',
                'price': str(price),
                'triggerprice': '0.00',
                'squareoff': '0.00',
                'stoploss': '0.00',
                'trailingStopLoss': '',
                'disclosedquantity': '0',
                'duration': 'DAY',
                'ordertag': 'my_algo',
            }
            logger.debug(f"Sell order payload: {json_data}")
            conn.request(
                'POST',
                '/openapi/typeb/orders/regular',
                json.dumps(json_data),
                headers
            )
            response = conn.getresponse().read().decode("utf-8")
            conn.close()
            logger.debug(f"Sell order executed. Response: {response}")
            
            # Parse and check success
            try:
                res_json = json.loads(response)

                # Some M.Stock responses are lists, others are single dicts
                if isinstance(res_json, list):
                    res_json = res_json[0]

                # If status is missing or false → error
                if not res_json.get("status", False):
                    msg = res_json.get("message", "Unknown error")
                    logger.error(f"M.Stock SELL error for {trading_symbol}: {msg} | Raw: {res_json}")
                    raise Exception(msg)

            except json.JSONDecodeError:
                logger.error(f"Invalid JSON in SELL response: {response}")
                raise Exception("Invalid JSON response from M.Stock")

            except Exception as parse_err:
                logger.error(f"Error parsing SELL response: {parse_err}")
                raise

            self._trader.frontend_data_socket.emit('sell_order_result',{"success": True, "tradingsymbol": trading_symbol , "Quantity": quantity})
            self._trader.frontend_data_socket.emit('status_message',{"success": True, "message": "Refreshing position now..."})
            logger.info("Refreshing open positions and buy prices after SELL...This would update the fund summar / cash balance as well")
            self._trader.refresh_open_pos_buy_price()

            return True
            
        except Exception as e:
            logger.error(f"Error selling units {e}")
            raise

    def buy_units(self, trading_symbol = None, instrument_token = None, quantity = 0, exchange="",ltp=0,mode="",sell_mode="",target_profit=0):
        try:
            logger.info(f"Buying units: {quantity} of {trading_symbol} ({instrument_token}) via M.Stock...Current LTP: {ltp}...Mode is {mode}. Sell Mode is {sell_mode}, target_profit is {target_profit}")
            buy_quantity = quantity
            
             # --- Fast path: check in-memory cache first ---
            if instrument_token in self._trader._five_weekly_option_contracts:
                logger.info(f"Check for instrument {instrument_token} in cache (_five_weekly_option_contracts)")
                data = self._trader._five_weekly_option_contracts[instrument_token]
                logger.info(f"Found instrument {instrument_token} in cache (_five_weekly_option_contracts)")
            else:
            # --- Fallback: slow file lookup ---
                logger.info(f"Instrument {instrument_token or trading_symbol} not found in cache, performing file lookup...")               
                if trading_symbol:
                    data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE , "name" , trading_symbol)
                elif instrument_token:
                    data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE , "token" , instrument_token)
            
            if not data:
                raise Exception(f"Instrument not found in {self._MSTOCK_INSTRUMENT_FILE} for {trading_symbol or instrument_token}")

            trading_symbol_for_transaction = data["name"]
            #instrument_token = data["token"]
            lotsize = data["lotsize"]
            
            logger.debug(f"quantity : {quantity} and lotsize {lotsize} ")

            quantity = int(quantity) * int(lotsize)

            full_legs = quantity // int(self._ORDER_FREEZE_LIMIT)
            remainder = quantity % int(self._ORDER_FREEZE_LIMIT)
            legs = [int(self._ORDER_FREEZE_LIMIT)] * full_legs
            
            logger.info("Mimicking Iceberg Order if the Order Freeze Limit is execeeded")
            logger.debug(f"Quantity: {quantity} full_legs:{full_legs} remainder{remainder} and legs {legs} ")

            if remainder:
                legs.append(remainder)
            
            response = None
            for i, qty in enumerate(legs, start=1):
                logger.debug(f"Placing leg {i}: {qty} qty")
                json_data = { 
                    'variety': 'NORMAL',
                    'tradingsymbol': trading_symbol_for_transaction,
                    'symboltoken': instrument_token,
                    'exchange': exchange,
                    'transactiontype': 'BUY',
                    'ordertype': 'MARKET',
                    'quantity': str(qty),
                    'producttype': 'CARRYFORWARD',
                    'price': "0.00",
                    'triggerprice': '0.00',
                    'squareoff': '0.00',
                    'stoploss': '0.00',
                    'trailingStopLoss': '',
                    'disclosedquantity': '0',
                    'duration': 'DAY',
                    'ordertag': 'my_algo',
                    'data':''
                }
                logger.debug(f"Buy order payload: {json_data}")

                conn = http.client.HTTPSConnection('api.mstock.trade', timeout=10, context=_SSL_CONTEXT)
                headers = {
                        "X-Mirae-Version": "1",
                        "X-PrivateKey": self.mstock_instance._api_key,
                        "Authorization": f"Bearer {self.mstock_instance._access_token}",
                        "Content-Type": "application/json"
                }

                order_request_start = time.perf_counter()

                conn.request(
                    'POST',
                    '/openapi/typeb/orders/regular',
                    json.dumps(json_data),
                    headers
                )

                order_request_sent = time.perf_counter()
                
                raw_response = conn.getresponse().read().decode("utf-8")

                order_response_received = time.perf_counter()

                logger.debug(
                    f"M.Stock BUY HTTP TIMING | "
                    f"request_to_send={order_request_sent - order_request_start:.3f}s | "
                    f"response_wait={order_response_received - order_request_sent:.3f}s | "
                    f"total={order_response_received - order_request_start:.3f}s"
                )                

                logger.debug(f"Buy order response raw: {raw_response}")
                conn.close()
                

                try:
                    response = json.loads(raw_response)
                except Exception:
                    raise Exception(f"Invalid JSON response from M.Stock: {raw_response}")

                # Handle both dict and list responses
                parsed_response = response[0] if isinstance(response, list) and len(response) > 0 else response
                logger.debug(f"Buy order  parsed response: {parsed_response}")
                if isinstance(parsed_response, dict):
                    status_val = parsed_response.get("status")
                    # status can be bool (False) or string ("success", "ok")
                    status_ok = (status_val is True) or (str(status_val).lower() in ["success", "ok"])

                    if not status_ok:
                        api_error = parsed_response.get("message") or parsed_response.get("error") or "Unknown API error"
                        logger.error(f"M.Stock API error for {trading_symbol}: {api_error} | Raw: {raw_response}")
                        raise Exception(api_error)
                    else:
                        self._trader.frontend_data_socket.emit('buy_order_result',{"success": True, "tradingsymbol": trading_symbol , "Quantity": quantity})
                        # FAST POSITION GRID UPDATE
                        # Do this immediately after successful BUY,
                        # before any SELL handling or REST position refresh.
                        self._trader.fast_update_position_after_buy(
                            trading_symbol=trading_symbol,
                            instrument_token=instrument_token,
                            quantity=int(qty) // int(lotsize),
                            lotsize=lotsize,
                            executed_buy_price=ltp,
                            exchange=exchange
                        )                        
                        # Checking what time advantage we get by placing sell order immediately after buy order even before position / order refresh happens

                        # self._trader.frontend_data_socket.emit('buy_order_result',{"success": True, "tradingsymbol": trading_symbol , "Quantity": quantity})
                        # self._trader.frontend_data_socket.emit('status_message', {"success": True,"message": "Refreshing position now..."})            
                        # self._trader.refresh_open_pos_buy_price()                        
                        # logger.debug(f"Buy order response (leg {i}): {response}")
                        
                        
                        
                        
                        # MODE CAN BE "ALERT", "EXECUTION"
                        # SELL_MODE CAN BE "U" OR "D" OR "T"

                        if mode == "LIVE":
                            logger.debug(f"Mode is {mode} and Sell_Mode is {sell_mode}")
                            # if sell_mode == "U": # Undetermined Profit at LTP + target_profit
                            #     logger.info("System placing Sell Order at LTP + {target_profit} points immediately after buy order is placed")
                            #     self.sell_units_temp(trading_symbol, instrument_token , buy_quantity , exchange,ltp=ltp,limit_market="LIMIT",price=ltp+target_profit) 
                            if sell_mode == "D": # Determined Profit at Buy Price + target_profit
                                logger.debug(f"System placing Sell Order at Buy Price + {target_profit} points immediately after buy order is placed")
                                logger.debug(f"Fetching Buy Price for the order id {parsed_response.get('data').get('orderid')}")
                                buy_price = self.fetch_order_executed_price(parsed_response.get('data').get("orderid"))
                                logger.info(f"Buy Price fetched for the order id {parsed_response.get('data').get('orderid')} is {buy_price}")
                                if buy_price is None:
                                    buy_price = float(ltp)
                                    logger.warning(
                                        f"Executed price unavailable for order "
                                        f"{parsed_response.get('data').get('orderid')}; "
                                        f"using buy LTP {buy_price} for the immediate sell"
                                    )
                                self.sell_units_temp(trading_symbol, instrument_token , buy_quantity , exchange,ltp=ltp,limit_market="LIMIT",price=buy_price+target_profit) 
                            elif sell_mode == "T": # Trigger Based..So Sell Order is not explicity raised..
                                logger.debug("Waiting for Trigger to raise the Sell order")

                        
                        # Moved position refresh and frontend emit after the Sell Order Placement to gain timwe advantage..Check logs and decide if this should be the way or should this be moved above as earlier
                        self._trader.frontend_data_socket.emit('status_message', {"success": True,"message": "Refreshing position now..."})            
                        self._trader.refresh_open_pos_buy_price()                        
                        logger.debug(f"Buy order response (leg {i}): {response}")
                        
                            
            return response
            
        except Exception as e:
            logger.error(f"Error buying units {e}")
            raise

    def buy_sell_units(self, trading_symbol = None, instrument_token = None, quantity = 0, exchange="",ltp=0,mode="",sell_mode="",target_profit=0):
        try:
            logger.info(f"Buying/Selling units: {quantity} of {trading_symbol} ({instrument_token}) via M.Stock...Current LTP: {ltp}...Mode is {mode}. Sell Mode is {sell_mode}, target_profit is {target_profit}")
            
            if mode == "LIVE":
                buy_quantity = quantity

                # --- Fast path: check in-memory cache first ---
                if instrument_token in self._trader._five_weekly_option_contracts:
                    logger.info(f"Check for instrument {instrument_token} in cache (_five_weekly_option_contracts)")
                    data = self._trader._five_weekly_option_contracts[instrument_token]
                    logger.info(f"Found instrument {instrument_token} in cache (_five_weekly_option_contracts)")
                else:
                # --- Fallback: slow file lookup ---
                    logger.info(f"Instrument {instrument_token or trading_symbol} not found in cache, performing file lookup...")               
                    if trading_symbol:
                        data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE , "name" , trading_symbol)
                    elif instrument_token:
                        data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE , "token" , instrument_token)
                
                if not data:
                    raise Exception(f"Instrument not found in {self._MSTOCK_INSTRUMENT_FILE} for {trading_symbol or instrument_token}")

                trading_symbol_for_transaction = data["name"]
                #instrument_token = data["token"]
                lotsize = data["lotsize"]
                
                logger.debug(f"quantity : {quantity} and lotsize {lotsize} ")

                quantity = int(quantity) * int(lotsize)

                full_legs = quantity // int(self._ORDER_FREEZE_LIMIT)
                remainder = quantity % int(self._ORDER_FREEZE_LIMIT)
                legs = [int(self._ORDER_FREEZE_LIMIT)] * full_legs
                
                logger.info("Mimicking Iceberg Order if the Order Freeze Limit is execeeded")
                logger.debug(f"Quantity: {quantity} full_legs:{full_legs} remainder{remainder} and legs {legs} ")

                if remainder:
                    legs.append(remainder)
                
                response = None
                for i, qty in enumerate(legs, start=1):
                    logger.debug(f"Placing leg {i}: {qty} qty")
                    json_data = { 
                        'variety': 'NORMAL',
                        'tradingsymbol': trading_symbol_for_transaction,
                        'symboltoken': instrument_token,
                        'exchange': exchange,
                        'transactiontype': 'BUY',
                        'ordertype': 'MARKET',
                        'quantity': str(qty),
                        'producttype': 'CARRYFORWARD',
                        'price': "0.00",
                        'triggerprice': '0.00',
                        'squareoff': '0.00',
                        'stoploss': '0.00',
                        'trailingStopLoss': '',
                        'disclosedquantity': '0',
                        'duration': 'DAY',
                        'ordertag': 'my_algo',
                        'data':''
                    }
                    logger.debug(f"Buy order payload: {json_data}")

                    conn = http.client.HTTPSConnection('api.mstock.trade', timeout=10, context=_SSL_CONTEXT)
                    headers = {
                            "X-Mirae-Version": "1",
                            "X-PrivateKey": self.mstock_instance._api_key,
                            "Authorization": f"Bearer {self.mstock_instance._access_token}",
                            "Content-Type": "application/json"
                    }

                    conn.request(
                        'POST',
                        '/openapi/typeb/orders/regular',
                        json.dumps(json_data),
                        headers
                    )

                    raw_response = conn.getresponse().read().decode("utf-8")
                    logger.debug(f"Buy order response raw: {raw_response}")


                    #conn.close()
                    

                    try:
                        response = json.loads(raw_response)
                    except Exception:
                        raise Exception(f"Invalid JSON response from M.Stock: {raw_response}")

                    # Handle both dict and list responses
                    parsed_response = response[0] if isinstance(response, list) and len(response) > 0 else response
                    logger.debug(f"Buy order  parsed response: {parsed_response}")
                    if isinstance(parsed_response, dict):
                        status_val = parsed_response.get("status")
                        # status can be bool (False) or string ("success", "ok")
                        status_ok = (status_val is True) or (str(status_val).lower() in ["success", "ok"])

                        if not status_ok:
                            api_error = parsed_response.get("message") or parsed_response.get("error") or "Unknown API error"
                            logger.error(f"M.Stock API error for {trading_symbol}: {api_error} | Raw: {raw_response}")
                            raise Exception(api_error)
                        else:
                            self._trader.frontend_data_socket.emit('buy_order_result',{"success": True, "tradingsymbol": trading_symbol , "Quantity": quantity})
                            logger.debug("Selling immediately after buying as per settings...")
                            logger.debug(f"LTP: {ltp} ; Target Profit : {target_profit} ; So Selling Price is {ltp+target_profit}")

                            if mode != "LIVE":
                                logger.debug(f"Mode is {mode} and Sell_Mode is {sell_mode}")
                                logger.debug("Since Mode is not EXECUTION, placing Sell Order immediately after buy order is not done")
                                return


                            json_data = { 
                            'variety': 'NORMAL',
                            'tradingsymbol': trading_symbol_for_transaction,
                            'symboltoken': instrument_token,
                            'exchange': exchange,
                            'transactiontype': 'SELL',
                            'ordertype': "LIMIT",
                            'quantity': str(int(qty)),
                            'producttype': 'CARRYFORWARD',
                            'price': str(ltp+target_profit),
                            'triggerprice': '0.00',
                            'squareoff': '0.00',
                            'stoploss': '0.00',
                            'trailingStopLoss': '',
                            'disclosedquantity': '0',
                            'duration': 'DAY',
                            'ordertag': 'my_algo',
                        }
                        logger.debug(f"Sell order payload: {json_data}")
                        conn.request(
                            'POST',
                            '/openapi/typeb/orders/regular',
                            json.dumps(json_data),
                            headers
                        )
                        raw_response = conn.getresponse().read().decode("utf-8")
                        logger.debug(f"Sell order response raw: {raw_response}")
                        conn.close()

                        self._trader.frontend_data_socket.emit('sell_order_result',{"success": True, "tradingsymbol": trading_symbol , "Quantity": quantity,"Sell Price": ltp+target_profit})
                        self._trader.frontend_data_socket.emit('status_message', {"success": True,"message": "Refreshing position now..."})            
                        self._trader.refresh_open_pos_buy_price()                        
                                
                return response
        except Exception as e:
            logger.error(f"Error buying units {e}")
            raise
        

    def manual_refresh_positions(self):
        logger.info("Manually refreshing open positions and buy prices in M.StockAdapter...")
        self._trader.refresh_open_pos_buy_price()

    @staticmethod
    def _select_contract_note_time(order_time, trade_time, price):
        """Select OrderTime, falling back to TradeTime or price-based seconds."""
        time_pattern = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
        parsed_times = []
        for value in (order_time, trade_time):
            match = time_pattern.match(str(value).strip())
            if match:
                hour, minute, second = match.groups()
                parsed_times.append((int(hour), int(minute), int(second or 0)))
            else:
                parsed_times.append(None)

        selected = parsed_times[0] if parsed_times[0] and parsed_times[0][2] else None
        selected = selected or (parsed_times[1] if parsed_times[1] and parsed_times[1][2] else None)
        selected = selected or parsed_times[0] or parsed_times[1]
        if not selected:
            raise ValueError(f"Invalid contract-note times: {order_time}, {trade_time}")

        hour, minute, second = selected
        if second == 0:
            second = max(0, min(59, int(round(float(price)))))
        return f"{hour:02d}:{minute:02d}:{second:02d}"

    def extract_trades(self,tables):
        """
        Extract BUY/SELL trades from Camelot tables
        Returns list of normalized trade dicts
        """
        trades = []

        for table in tables:
            df = table.df

            for _, row in df.iterrows():
                row_text = " ".join(row.astype(str))

                # Must contain explicit BUY or SELL
                if not re.search(r"\b(BUY|SELL)\b", row_text):
                    continue

                try:
                    values = [str(value).strip() for value in row.tolist()]
                    side_index = next(
                        index for index, value in enumerate(values)
                        if value.upper() in {"BUY", "SELL"}
                    )
                    if side_index >= 6:
                        order_id = values[0]
                        order_time = values[1]
                        trade_time = values[2]
                        symbol = values[side_index - 1]
                        quantity = int(float(values[side_index + 1]))
                        price = float(values[side_index + 3])
                    else:
                        order_id = values[0]
                        order_time = values[3]
                        trade_time = ""
                        symbol = values[4]
                        quantity = int(float(values[6]))
                        price = float(values[7])

                    selected_time = self._select_contract_note_time(
                        order_time, trade_time, price
                    )

                    trades.append({
                        "order_id": order_id,
                        "trade_time": selected_time,
                        "symbol": symbol,
                        "side": values[side_index].upper(),
                        "qty": quantity,
                        "price": price
                    })
                except Exception:
                    continue

        return trades

    def ocr_pdf(self,input_pdf: str) -> str:
        import pytesseract
        from pytesseract import TesseractNotFoundError
        from PIL import Image
        import tempfile
        from reportlab.pdfgen import canvas

        try:
            import pymupdf
        except ImportError as error:
            raise RuntimeError(
                "Contract-note OCR requires PyMuPDF. Install dependencies from requirements.txt."
            ) from error

        tesseract_cmd = (
            os.environ.get("TESSERACT_CMD")
            or shutil.which("tesseract")
            or next(
                (
                    path
                    for path in (
                        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                    )
                    if os.path.isfile(path)
                ),
                None,
            )
        )
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

        images = []
        document = pymupdf.open(input_pdf)
        try:
            scale = 300 / 72
            matrix = pymupdf.Matrix(scale, scale)
            for page in document:
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                images.append(
                    Image.frombytes(
                        "RGB",
                        [pixmap.width, pixmap.height],
                        pixmap.samples,
                    )
                )
        finally:
            document.close()

        tmp_fd, tmp_pdf_path = tempfile.mkstemp(suffix=".pdf")
        os.close(tmp_fd)
        c = canvas.Canvas(tmp_pdf_path)

        for img in images:
            try:
                text = pytesseract.image_to_string(img)
            except TesseractNotFoundError as error:
                raise RuntimeError(
                    "Contract-note OCR requires Tesseract OCR. Install Tesseract and either "
                    "add it to PATH or set TESSERACT_CMD to tesseract.exe."
                ) from error
            c.drawString(20, 800, text[:1000])  # minimal text injection
            c.showPage()

        c.save()
        return tmp_pdf_path

    def convert_mstock_contract_note(self,pdf_path: str) -> str:
        """
        Converts mStock contract note PDF into paired trades CSV
        Returns path to generated CSV
        """

        # ---------- Pass 1: Normal Camelot ----------
        try:
            tables = camelot.read_pdf(
                pdf_path,
                pages="3",
                flavor="stream",
                strip_text="\n"
            )
            raw_trades = self.extract_trades(tables)
        except Exception as error:
            logger.warning(
                f"Camelot could not parse the contract note; using OCR fallback: {error}"
            )
            raw_trades = []

        # ---------- Pass 2: OCR fallback ----------
        if not raw_trades:
            ocr_pdf_path = self.ocr_pdf(pdf_path)
            try:
                try:
                    tables = camelot.read_pdf(
                        ocr_pdf_path,
                        pages="all",
                        flavor="stream",
                        strip_text="\n"
                    )
                    raw_trades = self.extract_trades(tables)
                except Exception as error:
                    logger.warning(
                        f"Camelot could not parse the OCR output: {error}"
                    )
                    raw_trades = []
            finally:
                try:
                    os.unlink(ocr_pdf_path)
                except PermissionError:
                    pass

        if not raw_trades:
            raise ValueError("No BUY/SELL trades found (even after OCR)")

        trade_date = datetime.datetime.now().date()
        try:
            import pymupdf

            document = pymupdf.open(pdf_path)
            text = "\n".join(page.get_text() for page in document[:2])
            document.close()
            date_match = re.search(
                r"TRADE DATE\s+([A-Z][a-z]{2}\s+\d{1,2}\s+\d{4})",
                text,
                re.IGNORECASE,
            )
            if date_match:
                trade_date = datetime.datetime.strptime(
                    date_match.group(1), "%b %d %Y"
                ).date()
        except (ImportError, ValueError):
            pass

        trade_records = []
        for trade in raw_trades:
            if not re.match(r"^\d{2}:\d{2}:\d{2}$", trade["trade_time"]):
                continue

            instrument = normalize_contract_symbol(trade["symbol"])

            trade_records.append({
                "datetime": datetime.datetime.strptime(
                    f"{trade_date.isoformat()} {trade['trade_time']}",
                    "%Y-%m-%d %H:%M:%S",
                ),
                "type": trade["side"].upper(),
                "instrument": instrument,
                "quantity": float(trade["qty"]),
                "price": float(trade["price"]),
                "order_id": trade["order_id"],
            })

        if not trade_records:
            raise ValueError("No timed BUY/SELL trades found in contract note")

        trade_records.sort(key=lambda trade: trade["datetime"])

        # Match the VBA clubbing step before FIFO pairing. Identical orders
        # at the same timestamp become one quantity-weighted order.
        clubbed_records = []
        for trade in trade_records:
            if clubbed_records:
                previous = clubbed_records[-1]
                same_order = (
                    previous["datetime"] == trade["datetime"]
                    and previous["type"] == trade["type"]
                    and previous["instrument"] == trade["instrument"]
                )
                if same_order:
                    total_quantity = previous["quantity"] + trade["quantity"]
                    previous["price"] = (
                        (previous["price"] * previous["quantity"])
                        + (trade["price"] * trade["quantity"])
                    ) / total_quantity
                    previous["quantity"] = total_quantity
                    continue
            clubbed_records.append(trade.copy())

        trade_records = clubbed_records
        open_buys = defaultdict(deque)
        cumulative_pnl = 0.0
        output_rows = []
        timeline_script = []
        order_zone_script = []
        pnl_zone_script = []
        order_label_script = []
        pnl_label_script = []
        sequence = 1

        for trade in trade_records:
            duration = ""
            pnl_rate = ""
            pnl = ""
            pnl_pct = ""
            pnl_rt = ""

            sequence_id = f"{trade['datetime']:%y%m%d}_{sequence}"
            time_var = f"t{sequence_id}"
            timeline_script.append(
                f"{time_var}=timestamp('Asia/Kolkata',{trade['datetime']:%Y,%m,%d,%H,%M,%S})"
            )

            if trade["type"] == "BUY":
                open_buys[trade["instrument"]].append({
                    "quantity": trade["quantity"],
                    "price": trade["price"],
                    "datetime": trade["datetime"],
                    "time_var": time_var,
                    "option_type": "CE" if trade["instrument"].endswith("CE") else "PE",
                })
            elif trade["type"] == "SELL":
                remaining = trade["quantity"]
                matched_quantity = 0
                total_buy_cost = 0.0
                first_buy_time = None
                matched_buys = []

                while remaining > 0 and open_buys[trade["instrument"]]:
                    buy = open_buys[trade["instrument"]][0]
                    matched = min(remaining, buy["quantity"])
                    first_buy_time = first_buy_time or buy["datetime"]
                    matched_quantity += matched
                    total_buy_cost += matched * buy["price"]
                    matched_buys.append((buy, matched))
                    buy["quantity"] -= matched
                    remaining -= matched
                    if buy["quantity"] <= 0.0001:
                        open_buys[trade["instrument"]].popleft()

                if matched_quantity:
                    average_buy = total_buy_cost / matched_quantity
                    pnl_rate_value = trade["price"] - average_buy
                    pnl_value = pnl_rate_value * matched_quantity
                    cumulative_pnl += pnl_value
                    elapsed_seconds = int(
                        (trade["datetime"] - first_buy_time).total_seconds()
                    )
                    duration = f"{elapsed_seconds // 60:02d}:{elapsed_seconds % 60:02d}"
                    pnl_rate = round(pnl_rate_value, 2)
                    pnl = round(pnl_value)
                    purchase_value = average_buy * matched_quantity
                    pnl_pct = round((pnl_value / purchase_value) * 100, 2) if purchase_value else 0
                    pnl_rt = round(cumulative_pnl)

                    sell_time_var = time_var
                    for match_number, (buy, matched) in enumerate(matched_buys, start=1):
                        pine_id = f"{sequence_id}_{match_number}"
                        top_color = "color.new(color.green,0)" if buy["option_type"] == "CE" else "color.new(color.red,0)"
                        bottom_color = "color.new(color.green,0)" if pnl_value >= 0 else "color.new(color.red,0)"
                        mid_time = f"({buy['time_var']}+{sell_time_var})/2"
                        mid_y = "(high+low)/2"
                        order_zone_script.append(
                            f"if barstate.islast\n    var bz_{pine_id} = box.new({buy['time_var']},high,{sell_time_var},{mid_y},xloc=xloc.bar_time,bgcolor={top_color},border_width=0)"
                        )
                        pnl_zone_script.append(
                            f"if barstate.islast\n    var pbz_{pine_id} = box.new({buy['time_var']},{mid_y},{sell_time_var},low,xloc=xloc.bar_time,bgcolor={bottom_color},border_width=0)"
                        )
                        order_label_script.append(
                            f"if barstate.islast\n    var l_{pine_id} = label.new({mid_time},(high+{mid_y})/2,text='{matched:g}  {duration}  {pnl_rate:.1f}',xloc=xloc.bar_time,style=label.style_label_center,color=color.new(color.black,15),textcolor=color.white,size=size.normal,textalign=text.align_center,text_font_family=font.family_monospace)"
                        )
                        pnl_label_script.append(
                            f"if barstate.islast\n    var pl_{pine_id} = label.new({mid_time},(low+{mid_y})/2,text='{purchase_value / 100000:.1f}L  {pnl:.0f}  {pnl_pct:.2f}%',xloc=xloc.bar_time,style=label.style_label_center,color=color.new(color.black,15),textcolor=color.white,size=size.normal,textalign=text.align_center,text_font_family=font.family_monospace)"
                        )

            output_rows.append({
                "ORDERDATE": trade["datetime"].strftime("%m/%d/%Y"),
                "ORDERTIME": trade["datetime"].strftime("%H:%M:%S"),
                "TRAN": trade["type"],
                "CONT": trade["instrument"],
                "Product": "MIS",
                "Qty.": f"{trade['quantity']:g}/{trade['quantity']:g}",
                "RATE": round(trade["price"], 1),
                "STATUS": "COMPLETE",
                "Duration": duration,
                "PurValue": f"{(average_buy * matched_quantity) / 100000:.1f}L" if trade["type"] == "SELL" and matched_quantity else "",
                "PnL_Rate": pnl_rate,
                "PnL": pnl,
                "PnL%": pnl_pct,
                "PnL_RT": pnl_rt,
            })
            sequence += 1

        pine_script = "\n".join(
            [
                "// === TIMELINE ===",
                *timeline_script,
                "// === ORDER ZONES ===",
                *order_zone_script,
                "// === P&L ZONES ===",
                *pnl_zone_script,
                "// === ORDER LABELS ===",
                *order_label_script,
                "// === P&L LABELS ===",
                *pnl_label_script,
            ]
        )

        return write_orders_workbook(output_rows, pine_script)



    def get_instrument_details(self , tradingsymbol):
        logger.info(f"Getting instrument details for {tradingsymbol} from M.Stock...")
        data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE , "name" , tradingsymbol)
        updated_data = instr_det_attrib_mgmt(data)
        return updated_data
    
    def format_option_symbol(self ,  underlying: str,
        expiry: datetime.date,
        strike: int,
        call_or_put: str) -> str:
        try:
            logger.info(f"Formatting option symbol for {underlying} expiring on {expiry}, strike {strike}, type {call_or_put}...")
            yy = expiry.year % 100

            #FUTURE AND OPTIONS EXPIRING TOGETHER
            # Get the first letter of the month name, e.g., 'October' -> 'O'
            # if the weekly expiry doesnt coincide with monthly expiry of the near month future token ..then it would be first letter of the month followed by 2 digits of expiry date
            # if the weekly expiry coincides with monthly expiry of the near month future token ..then it would be first three letters of the month and no digits of expiry date
            if self.get_constant("NEAR_FUTURE_NEAR_OPTION_EXPIRE_TOGETHER"): 
                month_char = calendar.month_name[expiry.month][:3].upper()
                dd_str=""
            else:
                #month_char = calendar.month_name[expiry.month][0].upper() 
                #Since Jan 2026 first weekly expiry the above code stopped working and the below code is put in place
                month_char = str(expiry.month)
                dd_str = f"{expiry.day:02d}"  

            logger.info(f"{underlying}{yy}{month_char}{dd_str}{strike}{call_or_put.upper()}")
            return f"{underlying}{yy}{month_char}{dd_str}{strike}{call_or_put.upper()}"
        except Exception as e:
            logger.error(f"❌ Error @ format_option_symbol: {e}")
            return False

    def unsubscribe_from_all(self,instruments):
        logger.info("Unsubscribing from all instruments in M.Stock...")
        if not instruments:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.mstock_instance._unsubscribe_from_all(instruments))
            return
        except RuntimeError:
            pass

        logger.info("No running loop detected; running MStock unsubscribe synchronously.")
        try:
            asyncio.run(self.mstock_instance._unsubscribe_from_all(instruments))
        except Exception as e:
            logger.error(f"MStock unsubscribe failed: {e}")

    # def subscribe_to_all(self, instruments):
    #     logger.info("Subscribing to all instruments in M.Stock...")
    #     if not instruments:
    #         return
    #     try:
    #         loop = asyncio.get_running_loop()
    #         loop.create_task(self.mstock_instance._subscribe_to_instruments(instruments))
    #         return
    #     except RuntimeError:
    #         pass

    #     logger.info("No running loop detected; running MStock subscribe synchronously.")
    #     try:
    #         asyncio.run(self.mstock_instance._subscribe_to_instruments(instruments))
    #     except Exception as e:
    #         logger.error(f"MStock subscribe failed: {e}")

    # MSTOCK_SENSEX

    def subscribe_to_all(self, instruments):
        logger.info("Subscribing to all instruments in M.Stock...")

        if not instruments:
            logger.info("No instruments to subscribe.")
            return

        try:
            # Group tokens by M.Stock exchange type.
            #
            # M.Stock WebSocket expects:
            #   exchangeType 2 -> NSE
            #   exchangeType 3 -> BSE
            #
            # The instrument master gives us the exchange segment
            # (exch_seg), so do not assume every token belongs to NSE.

            exchange_tokens = {}

            for instrument_token in instruments:

                token = str(instrument_token)

                try:
                    data = find_matching_object(
                        self._MSTOCK_INSTRUMENT_FILE,
                        "token",
                        token
                    )

                    if not data:
                        logger.warning(
                            f"Instrument details not found for token {token}. "
                            f"Skipping WebSocket subscription."
                        )
                        continue

                    exchange = str(data.get("exch_seg", "")).upper()

                    if exchange in ("NSE", "NFO"):
                        exchange_type = 2

                    elif exchange in ("BSE", "BFO"):
                        exchange_type = 3 if exchange == "BSE" else 4

                    else:
                        logger.warning(
                            f"Unknown M.Stock exchange '{exchange}' "
                            f"for token {token}. Skipping subscription."
                        )
                        continue

                    exchange_tokens.setdefault(exchange_type, []).append(token)

                    logger.debug(
                        f"M.Stock subscription mapping: "
                        f"token={token}, exchange={exchange}, "
                        f"exchangeType={exchange_type}"
                    )

                except Exception as e:
                    logger.error(
                        f"Error resolving exchange for token {token}: {e}"
                    )

            if not exchange_tokens:
                logger.warning(
                    "No valid instruments available for M.Stock subscription."
                )
                return

            logger.info(
                f"M.Stock exchange-grouped subscriptions: {exchange_tokens}"
            )

            try:
                loop = asyncio.get_running_loop()

                loop.create_task(
                    self.mstock_instance._subscribe_to_instruments(
                        exchange_tokens
                    )
                )
                return

            except RuntimeError:
                pass

            logger.info("No running loop detected; scheduling M.Stock subscription on WebSocket loop.")

            socket_loop = getattr(self.mstock_instance, "_socket_loop", None)

            if socket_loop is None or socket_loop.is_closed():
                logger.error(
                    "M.Stock WebSocket loop is not available; subscription not scheduled."
                )
                return

            asyncio.run_coroutine_threadsafe(
                self.mstock_instance._subscribe_to_instruments(
                    exchange_tokens
                ),
                socket_loop
        )

        except Exception as e:
            logger.error(f"M.Stock subscribe failed: {e}")

    async def start_socket_connection(self, shutdown_event, trader_instance):
        logger.info("Starting socket connection in M.StockAdapter...")
        await self.mstock_instance.start_socket_connection(
            shutdown_event, trader_instance
        )
        logger.info("Socket started")
        

    def dev_start(self):
        logger.info("Starting M.StockAdapter in development mode...")
        self.mstock_instance.initialise_for_dev()

    def prod_start(self):
        logger.info("Starting M.StockAdapter in production mode...")
        if self.mstock_instance.initialise_for_prod():
            self.download_instrument_list(None,None)

    def download_instrument_list(self, exchange: str,underlying:str) -> bool:
        last_downloaded_date_string = fetch_from_json("access_token.json" , "mstock_last_downloaded_instruments_timestamp")
        last_downloaded_date = datetime.datetime.fromisoformat(last_downloaded_date_string)


        underlying_exists = False
        reduced_file_path = "mstock_instrument_list_reduced.json"

        if os.path.exists(reduced_file_path):
            try:
                with open(reduced_file_path, "r") as f:
                    reduced_list = json.load(f)

                for item in reduced_list:
                    if "symbol" in item and item["symbol"] == underlying:
                        underlying_exists = True
                        break

            except Exception as e:
                logger.error(f"❌ Error reading reduced instrument file: {e}")
                underlying_exists = False  # Force download

        #UnderlyingExist = true check helps to check if the underlying exists int the reduced file ..if not then we need to download the latest instrument list again and then reduce it again
        #This scenario can happen when user changes the underlying from NIFTY to SENSEX or vice versa post download of instrument list and reduction for the day
        if last_downloaded_date.date() == datetime.datetime.now().date() and underlying_exists == True:
            self.update_instrument_constants("mstock_instrument_list.json", underlying, "constants.json")
            with open("constants.json") as f:
                constants = json.load(f)
                self.reduce_instrument_list(constants)
            logger.info("✅ Instrument list already downloaded today; skipping download.")
            return
        try:
            logger.info(f"Downloading instrument list for exchange: {exchange} from M.Stock...")
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
            headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
                "Content-Type": "application/json"
            }
            conn.request('GET', '/openapi/typeb/instruments/OpenAPIScripMaster', headers=headers)
            response = conn.getresponse()
            response_body = response.read().decode("utf-8")
            response_json = json.loads(response_body)
            
            file_path = "mstock_instrument_list.json"
            with open(file_path, 'w') as f:
                json.dump(response_json, f, indent=4)
            
            logger.info(f"✅ Successfully downloaded and saved instrument list data to {file_path}.")
            write_to_json({"mstock_last_downloaded_instruments_timestamp" : datetime.datetime.now().isoformat()} , "access_token.json")

            self.update_instrument_constants("mstock_instrument_list.json", underlying, "constants.json")
            
            with open("constants.json") as f:
                constants = json.load(f)
                self.reduce_instrument_list(constants)

            return True

        except json.JSONDecodeError:
            logger.error(f"❌ Error decoding JSON response. Response was: {response_body[:200]}...")
            return False
        except Exception as e:
            logger.error(f"❌ Error downloading instrument list: {e}")
            return False

    def reduce_instrument_list(self, constants: dict):
        """
        Reduce mstock_instrument_list.json based on constants.json:
        - Keep only the near month future token
        - Keep only NIFTY options expiring on the configured expiry (exact match)
        - Keep only strikes within +/-1000 of FUT LTP and multiples of 100
        """

        logger.info("Reducing instrument list based on constants...")

        try:
            with open("mstock_instrument_list.json", "r") as f:
                data = json.load(f)

            filtered = []

            # Load constants
            FUT_TOKEN   = constants["NEAR_MONTH_FUTURE_TOKEN"]
            UNDERLYING  = constants["UNDERLYING"]
            EXP_MONTH   = constants["EXPIRY_MONTH"]       # e.g., "NOV"
            EXP_YEAR    = str(constants["EXPIRY_YEAR"])   # "2025"
            if UNDERLYING == "NIFTY":
                EXP_DAY     = str(constants["NIFTY_EXPIRY_DATE"])  # "18"
            elif UNDERLYING == "SENSEX":
                EXP_DAY     = str(constants["SENSEX_EXPIRY_DATE"])  # "18"

            # Build final expiry string for exact match
            TARGET_EXPIRY = f"{EXP_DAY}{EXP_MONTH.capitalize()}{EXP_YEAR}"

            self._trader._NEAR_MONTH_FUTURE_LTP = self.get_ltp(FUT_TOKEN)

            strike_lower = int(self._trader._NEAR_MONTH_FUTURE_LTP - 3000)
            strike_upper = int(self._trader._NEAR_MONTH_FUTURE_LTP + 3000)

            logger.info(
                f"Filtering: FUT={FUT_TOKEN}, expiry={TARGET_EXPIRY}, "
                f"strike range={strike_lower} to {strike_upper}"
            )

            for inst in data:

                token = inst.get("name") or inst.get("tradingsymbol") or inst.get("symbol")

                # 1️⃣ Keep exact future symbol
                if token == FUT_TOKEN:
                    filtered.append(inst)
                    continue

                symbol    = inst.get("symbol")
                inst_type = inst.get("instrumenttype")
                expiry    = inst.get("expiry")

                # Only options will have expiry
                if not expiry:
                    continue

                # Simplify: direct expiry match
                if (
                    symbol == UNDERLYING
                    and inst_type.upper() == "OPTIDX"
                    and str(expiry).upper() == TARGET_EXPIRY.upper()
                ):
                    # Strike logic
                    strike_raw = inst.get("strike", 0)
                    try:
                        strike = int(float(strike_raw))
                    except:
                        continue

                    if (
                        strike_lower <= strike <= strike_upper
                        and strike % 100 == 0
                    ):
                        filtered.append(inst)

            # Write reduced output
            with open("mstock_instrument_list_reduced.json", "w") as f:
                json.dump(filtered, f, indent=2)

            logger.info(
                f"Done! {len(filtered)} records saved to mstock_instrument_list_reduced.json"
            )
            return True

        except Exception as e:
            logger.error(f"❌ Error reducing instrument list: {e}")
            return False


    def update_constants_file(self,data, json_file="constants.json"):
        #logger.info("Constants file going to be updated")
        """Update or create constants.json with the given data."""
        try:
            # Load existing constants
            with open(json_file, "r") as f:
                constants = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            constants = {}

        # Update keys
        constants.update(data)

        # Write back to file (pretty format)
        with open(json_file, "w") as f:
            json.dump(constants, f, indent=4)

        logger.info("Constants file updated successfully.")


    def update_instrument_constants(self, json_instrument_file, underlying, json_file="constants.json"):
        logger.debug(f"Instrument constants to be updated for {json_instrument_file} and underlying {underlying}")

        # --- 1️⃣ Load instrument data from JSON ---
        try:
            with open(json_instrument_file, "r") as f:
                instruments = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load {json_instrument_file}: {e}")
            return

        # Convert list of dicts → DataFrame for easier filtering
        df = pd.DataFrame(instruments)
        logger.debug(f"Loaded {len(df)} instruments from {json_instrument_file}")
        underlying = underlying.upper().strip()

        # --- 2️⃣ Filter for futures of the given underlying ---
        fut_df = df[
            (df["instrumenttype"].str.upper() == "FUTIDX") &
            (df["symbol"].str.upper() == underlying)
        ].copy()

        if fut_df.empty:
            logger.warning(f"No futures found for {underlying}")
            exchange = ""
            trading_symbol = ""
            expiry_month = ""
            expiry_year = ""
            expiry_date_future = None
        else:
            fut_df["expiry"] = pd.to_datetime(fut_df["expiry"], errors="coerce")
            nearest_future = fut_df.sort_values("expiry").iloc[0]

            exchange = nearest_future.get("exch_seg", "")
            trading_symbol = nearest_future.get("name", "")
            expiry_date_future = nearest_future.get("expiry", "")

            if pd.notna(expiry_date_future) and expiry_date_future != "":
                expiry_month = pd.to_datetime(expiry_date_future).strftime("%b").upper()
                expiry_year = pd.to_datetime(expiry_date_future).strftime("%Y")
            else:
                expiry_month = ""
                expiry_year = ""

        # --- 3️⃣ Find nearest weekly options (if available) ---
        opt_df = df[
            (df["instrumenttype"].str.upper() == "OPTIDX") &
            (df["name"].str.upper().str.endswith(("CE", "PE")))
        ].copy()

        expiry_nifty = ""
        expiry_sensex = ""
        expire_together = False

        if not opt_df.empty:
            opt_df["expiry"] = pd.to_datetime(opt_df["expiry"], errors="coerce")
            nifty_opts = opt_df[opt_df["symbol"].str.upper().str.contains("NIFTY")]
            sensex_opts = opt_df[opt_df["symbol"].str.upper().str.contains("SENSEX")]

            # expiry_nifty_date = nifty_opts.sort_values("expiry")["expiry"].dropna().iloc[0] if not nifty_opts.empty else None
            # expiry_sensex_date = sensex_opts.sort_values("expiry")["expiry"].dropna().iloc[0] if not sensex_opts.empty else None
            
            today = pd.Timestamp.now().normalize()

            future_nifty_opts = nifty_opts[nifty_opts["expiry"] >= today]
            future_sensex_opts = sensex_opts[sensex_opts["expiry"] >= today]

            expiry_nifty_date = (
                future_nifty_opts.sort_values("expiry")["expiry"].dropna().iloc[0]
                if not future_nifty_opts.empty
                else None
            )

            expiry_sensex_date = (
                future_sensex_opts.sort_values("expiry")["expiry"].dropna().iloc[0]
                if not future_sensex_opts.empty
                else None
            )            

            expiry_nifty = expiry_nifty_date.strftime("%d") if expiry_nifty_date is not None else ""
            expiry_sensex = expiry_sensex_date.strftime("%d") if expiry_sensex_date is not None else ""

            # --- 4️⃣ Determine if future and option expire together ---
            if expiry_date_future is not None and pd.notna(expiry_date_future):
                if underlying == "NIFTY" and expiry_nifty_date is not None:
                    expire_together = expiry_nifty_date.date() == expiry_date_future.date()
                elif underlying == "SENSEX" and expiry_sensex_date is not None:
                    expire_together = expiry_sensex_date.date() == expiry_date_future.date()

        # --- 5️⃣ Prepare constants update ---
        update_data = {
            "EXCHANGE": exchange,
            "NEAR_MONTH_FUTURE_TOKEN": trading_symbol,
            "EXPIRY_MONTH": expiry_month,
            "EXPIRY_YEAR": expiry_year,
            "NIFTY_EXPIRY_DATE": expiry_nifty,
            "SENSEX_EXPIRY_DATE": expiry_sensex,
            "NEAR_FUTURE_NEAR_OPTION_EXPIRE_TOGETHER": expire_together,
        }

        # --- 6️⃣ Write and sync constants ---
        self.update_constants_file(update_data, json_file)
        self.update_constants(update_data)

        logger.debug("Updated constants:\n" + json.dumps(self.constants, indent=4))
        
    def check_mstock_endpoints():
        """
        Check availability for each of the given MStock API endpoints.

        Args:
            endpoints (dict):  
                key = descriptive name  
                value = (method, full_url)
            headers (dict): optional HTTP headers to send (e.g., Authorization, X-Mirae-Version, X-PrivateKey)
            timeout (int): seconds to wait

        Returns:
            dict: { name: (status, info) }  
                status = "UP" or "DOWN"  
                info = HTTP status code or error message
        """
        base = "https://api.mstock.trade"
        timeout=5
        headers=None
        endpoints = {
            "Login": ("POST", f"{base}/openapi/typea/connect/login"),
            "Verify TOTP": ("POST", f"{base}/openapi/typea/session/verifytotp"),
            "Get OHLC (TypeA)": ("GET", f"{base}/openapi/typea/instruments/quote/ohlc"),
            "Get LTP (TypeA)": ("GET", f"{base}/openapi/typea/instruments/quote/ltp"),
            "Instrument Master (TypeA)": ("GET", f"{base}/openapi/typea/instruments/scriptmaster"),
            "Holdings (TypeB)": ("GET", f"{base}/openapi/typeb/portfolio/holdings"),
            "Market Quote (TypeB)": ("GET", f"{base}/openapi/typeb/instruments/quote"),
            "Calculate Margin (TypeB)": ("POST", f"{base}/openapi/typeb/margins/orders"),
            # ... you can add more endpoints similarly
        }        

        results = {}
        for name, (method, url) in endpoints.items():
            m = method.upper()
            try:
                if m == "GET":
                    resp = requests.get(url, headers=headers, timeout=timeout)
                elif m == "POST":
                    resp = requests.post(url, headers=headers, timeout=timeout)
                elif m == "PUT":
                    resp = requests.put(url, headers=headers, timeout=timeout)
                elif m == "DELETE":
                    resp = requests.delete(url, headers=headers, timeout=timeout)
                else:
                    results[name] = ("INVALID_METHOD", m)
                    continue

                if 200 <= resp.status_code < 400:
                    results[name] = ("UP", resp.status_code)
                else:
                    results[name] = ("DOWN", resp.status_code)

                for ep, (s, info) in results.items():
                    logger.info(f"{ep:25} => {s} ({info})")

            except requests.exceptions.Timeout:
                results[name] = ("DOWN", "Timeout")
            except requests.exceptions.ConnectionError:
                results[name] = ("DOWN", "ConnectionError")
            except Exception as e:
                results[name] = ("DOWN", str(e))