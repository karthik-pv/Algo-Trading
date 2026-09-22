import asyncio
import csv
import http.client
import json
import logging
import datetime
import calendar
import time
import threading
import os
import tempfile
import ssl
import shutil


import pandas as pd
from datetime import timedelta  

import re
from collections import defaultdict, deque




from utils import (
    get_trading_symbols_from_json,
    find_matching_object,
    fetch_from_json,
    write_to_json,
    clear_json_cache,
    read_instrument_meta,
    get_exchange_for_underlying,
    resolve_data_path,
    is_market_open,
    INSTRUMENTS_SECTION_PREFIX,
)
from core.mstock_connector import MStockSingleton
from interface.broker_interface import BrokerInterface
from core.trade_logic import Trader_Singleton
from adapter.mstock_utils import (
    position_attribute_mgmt, fund_summary_attribute_mgmt, order_attribute_mgmt,
    instr_det_attrib_mgmt, normalize_contract_symbol, write_orders_workbook,
    format_lakhs, apply_trade_charges,
)
from loguru import logger


_SSL_CONTEXT = ssl.create_default_context()


def _sanitise_closed_market_quote(response):
    """
    Rewrite bogus LTP/OHLC values in a quote response when the market
    is closed.

    Outside market hours the last traded price of an instrument IS its
    previous close. M.Stock's quote endpoint has been observed serving
    garbage LTP/open/high/low for the near-month future token in that
    state (e.g. ltp 25500 / open 24700 / high 25649 alongside the true
    close 23378.5). Trusting that LTP centres the option chain on a
    phantom ATM strike (NIFTY 25500 instead of ~23400) and the grid
    then shows deep-OTM contracts at ~0.50.

    When the market is closed and a positive close is present, every
    OHLC field is normalised to the close: no trades can have happened,
    so close is the last traded price. Market-open responses pass
    through untouched.
    """
    try:
        if is_market_open():
            return response

        data = response.get("data") if isinstance(response, dict) else None
        fetched = data.get("fetched") if isinstance(data, dict) else None
        if not fetched:
            return response

        sanitised_tokens = []

        for quote in fetched:
            if not isinstance(quote, dict):
                continue

            try:
                close_val = float(quote.get("close") or quote.get("prevClose") or 0)
            except (TypeError, ValueError):
                continue

            if close_val <= 0:
                continue

            try:
                ltp_val = float(quote.get("ltp") or 0)
            except (TypeError, ValueError):
                ltp_val = 0.0

            if ltp_val > 0 and abs(ltp_val - close_val) / close_val < 0.001:
                continue

            sanitised_tokens.append(str(quote.get("symbolToken", "?")))

            quote["ltp"] = close_val
            quote["open"] = close_val
            quote["high"] = close_val
            quote["low"] = close_val

        if sanitised_tokens:
            # One summary per quote batch instead of a warning per token
            # (after hours every token in the batch gets sanitised).
            logger.warning(
                f"Closed-market sanitise: normalised "
                f"{len(sanitised_tokens)} quote(s) to previous close "
                f"(tokens: {', '.join(sanitised_tokens)})"
            )

        return response
    except Exception as e:
        logger.warning(f"Closed-market quote sanitisation skipped: {e}")
        return response


class MStockAdapter(BrokerInterface):

    def __init__(self):
        logger.info("Initializing MStockAdapter...")
        self._trader = Trader_Singleton()
        self.mstock_instance = MStockSingleton()

        self._instrument_cache = {}       # stores processed instruments keyed by tradingsymbol
        self._instrument_data_loaded = {}  # raw data loaded from CSV once
        self._csv_loaded = False           # flag to check if CSV has been read


        self._underlying = fetch_from_json("appconfig.json" , "UNDERLYING")

        self._MSTOCK_INSTRUMENT_FILE = resolve_data_path("mstock_instrument_list_reduced.json")

        # Instrument-file-derived meta (replaces the old derived keys in
        # appconfig.json). Populated at startup by download_instrument_list
        # or read from the reduced instrument file's meta block.
        self._instrument_meta = None

        # When one endpoint reports an expired session, every other
        # authenticated endpoint will too. Remember the failure briefly
        # so the burst of startup calls fails fast instead of burning an
        # HTTP round-trip each. Auto-expires, so after a fresh login the
        # next call goes out normally without any wiring.
        self._session_expired_until = 0.0
        self._SESSION_EXPIRED_COOLDOWN_SECONDS = 10.0

        # Broker-side exit-fill detection. The U/D sell modes leave a
        # resting SELL LIMIT at the broker; when the broker executes it,
        # nothing in the app flow refreshes the position grid - the leg
        # keeps showing (and the SL watcher keeps evaluating) a
        # position the broker no longer holds. The pending-orders poll
        # (the frontend pings it every ~15s) diffs the pending SELL set
        # and forces a debounced grid refresh when one of them trades.
        self._prev_pending_sell_order_ids = set()
        self._last_fill_refresh_ts = 0.0
        self._FILL_REFRESH_DEBOUNCE_SECONDS = 10.0

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
        symbol = str(tradingsymbol).upper()
        key = "SENSEX_FALLBACK_LTP" if "SENSEX" in symbol else "NIFTY_FALLBACK_LTP"
        return float(fetch_from_json("appconfig.json", key) or 0)

    def get_instrument_meta(self):
        if self._instrument_meta is None:
            # Reduced file may already carry meta from an earlier session
            # (e.g. constants page actions before any download ran).
            self._instrument_meta = read_instrument_meta(self._MSTOCK_INSTRUMENT_FILE) or None
        if self._instrument_meta:
            # Persisted meta may predate the fixed 100-point strike grid;
            # always enforce it so a stale file cannot bring back 50.
            self._instrument_meta["strike_interval"] = 100
            return self._instrument_meta
        return super().get_instrument_meta()


    def _mstock_request(
        self,
        method,
        endpoint,
        body=None,
        timeout=10,
        max_retries=3,
        backoff_seconds=0.5,
    ):
        """
        Authenticated REST call to api.mstock.trade with 502 handling.

        M.Stock intermittently answers with a bare "502 Bad Gateway" HTML
        page (typically right after session creation). Feeding such a body
        straight into json.loads() made every caller fail with
        "Expecting value: line 1 column 1 (char 0)". This helper:
          - adds a socket timeout (the old code could hang forever)
          - retries empty/non-JSON bodies and transport errors with a
            short backoff (these are the transient gateway failures)
          - returns the parsed JSON as soon as the body is valid JSON so
            the existing status/message handling (including the
            invalid-session detection) keeps working unchanged
        Returns the parsed dict/list, or None after all attempts fail.
        """
        now = time.time()
        if now < self._session_expired_until:
            logger.debug(
                f"M.Stock API {method} {endpoint} skipped - session known "
                f"expired (cooldown for another "
                f"{self._session_expired_until - now:.1f}s)"
            )
            return {
                "status": False,
                "message": "invalid session. Kindly logout and login again",
                "errorcode": "MAInternalServerError",
                "data": None,
            }

        last_reason = "unknown error"

        for attempt in range(1, max_retries + 1):
            conn = None
            try:
                conn = http.client.HTTPSConnection(
                    "api.mstock.trade", context=_SSL_CONTEXT, timeout=timeout
                )
                headers = {
                    "X-Mirae-Version": "1",
                    "X-PrivateKey": self.mstock_instance._api_key,
                    "Authorization": f"Bearer {self.mstock_instance._access_token}",
                }
                payload = None
                if body is not None:
                    payload = json.dumps(body)
                    headers["Content-Type"] = "application/json"

                conn.request(method, endpoint, payload, headers)
                response = conn.getresponse()
                status = response.status
                raw = response.read().decode("utf-8")

                stripped = raw.lstrip()
                if not stripped:
                    last_reason = f"HTTP {status} with empty body"
                elif stripped[0] not in "{[":
                    # e.g. the bare "<html>502 Bad Gateway</html>" page
                    last_reason = f"HTTP {status} with non-JSON body: {stripped[:120]!r}"
                else:
                    parsed = json.loads(raw)
                    if (
                        isinstance(parsed, dict)
                        and not parsed.get("status")
                        and "invalid session" in str(parsed.get("message", "")).lower()
                    ):
                        self._session_expired_until = (
                            time.time() + self._SESSION_EXPIRED_COOLDOWN_SECONDS
                        )
                        logger.warning(
                            f"M.Stock session expired (reported by "
                            f"{method} {endpoint}); API calls fail fast for "
                            f"{self._SESSION_EXPIRED_COOLDOWN_SECONDS}s"
                        )
                    return parsed

            except (http.client.HTTPException, OSError, ValueError) as e:
                last_reason = f"{type(e).__name__}: {e}"

            finally:
                if conn is not None:
                    conn.close()

            logger.warning(
                f"M.Stock API {method} {endpoint} attempt {attempt}/{max_retries} "
                f"failed: {last_reason}"
            )
            if attempt < max_retries:
                time.sleep(backoff_seconds * attempt)

        logger.error(
            f"M.Stock API {method} {endpoint} failed after {max_retries} "
            f"attempts: {last_reason}"
        )
        return None


    def cancel_order(self, order_id):
        logger.info(f"Cancelling order: {order_id}")

        # MStock throttles order-management requests (429) and often
        # answers rapid-fire cancels with a transient IA500 "try again"
        # (500). One retry after a short pause recovers most of those.
        for attempt in (1, 2):
            try:
                conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)

                headers = {
                    "X-Mirae-Version": "1",
                    "X-PrivateKey": self.mstock_instance._api_key,
                    "Authorization": f"Bearer {self.mstock_instance._access_token}",
                    "Content-Type": "application/json"
                }
                # MStock's cancel endpoint REQUIRES a body carrying the
                # variety and the orderid (docs: tradingapi.mstock.com
                # typeB "Order Cancellation"). An empty/"{}" body passes
                # routing but their controller 500s (IA500) without it.
                endpoint = f"/openapi/typeb/orders/regular/{order_id}"
                body = json.dumps({"variety": "NORMAL", "orderid": str(order_id)})
                conn.request("DELETE", endpoint, body=body, headers=headers)

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
                            logger.error(f"Cancel failed (HTTP {response.status}): {res}")
                    except Exception:
                        # Unexpected body - treat as success per m.Stock inconsistency
                        logger.warning(f"Non-JSON cancel response, treating as success.")
                        return True
                else:
                    logger.error(f"Cancel failed for {order_id}: HTTP {response.status}, empty body")

            except Exception as e:
                logger.error(f"Cancel order error for {order_id} (attempt {attempt}): {e}")

            if attempt == 1:
                logger.info(f"Retrying cancel for {order_id} after a short pause...")
                time.sleep(1.0)

        return False


    def fetch_all_pending_orders(self):
        logger.info("Fetching pending orders from M.Stock...")

        try:
            response = self._mstock_request("GET", "/openapi/typeb/orders")

            if response is None:
                logger.error("Error fetching pending orders: no valid response from M.Stock \n\n CONSIDER LOGGING IN AGAIN \n\n")
                return []

            data = response.get("data", [])

            # Full raw dumps of the order book on every 15s poll inflated
            # the logs by tens of MB/day; log a compact histogram instead.
            if isinstance(data, list):
                status_counts = {}
                for order in data:
                    key = str(order.get("orderstatus") or order.get("status") or "?").upper()
                    status_counts[key] = status_counts.get(key, 0) + 1
                logger.debug(f"Pending orders fetch: {len(data)} order(s) by status: {status_counts}")
            else:
                logger.debug(f"Pending orders fetch: unexpected payload type {type(data).__name__}")

            # Apply your existing transformation logic
            orders = order_attribute_mgmt(data)

            # MStock reports live orders under several status labels
            # ("O-Pending" at the exchange, "Pending" at the OMS, plus
            # casing variants) - match them all, case-insensitively, or
            # genuinely open orders vanish from the pending grid.
            pending_statuses = {"pending", "o-pending", "open", "validated"}
            pending_orders = [
                o for o in orders
                if str(o.get("order_status", "")).strip().lower() in pending_statuses
            ]

            logger.info(f"Total orders: {len(orders)}, Pending: {len(pending_orders)}")
            self._detect_broker_side_sell_fills(orders, pending_orders)
            self._trader.frontend_data_socket.emit('update_pending_orders', pending_orders)
            return pending_orders

        except Exception as e:
            logger.error(f"Error fetching pending orders: {e} \n\n CONSIDER LOGGING IN AGAIN \n\n")
            return []

    def _detect_broker_side_sell_fills(self, all_orders, pending_orders):
        """
        Detect SELL orders that left the pending set because the broker
        executed them, and reconcile the position grid when that happens.

        A filled resting exit (the U/D immediate target sell, or any
        other algo SELL filled broker-side) must remove its position
        leg. Without this reconciliation the grid keeps showing the
        sold leg, the SL watcher keeps evaluating a phantom position
        and a manual exit hits the RMS as a fresh short
        (FUND LIMIT INSUFFICIENT).
        """
        try:
            pending_ids = {
                str(o.get("order_id"))
                for o in pending_orders
                if o.get("order_id")
            }

            filled_statuses = {
                "traded", "executed", "complete", "completed",
                "fully executed", "fully_executed",
            }

            def _filled_qty(order):
                try:
                    return float(order.get("filled_quantity") or 0)
                except (TypeError, ValueError):
                    return 0.0

            filled_now = []
            for order in all_orders or []:
                if str(order.get("transaction_type") or "").upper() != "SELL":
                    continue
                order_id = str(order.get("order_id") or "")
                if not order_id or order_id not in self._prev_pending_sell_order_ids:
                    continue
                if order_id in pending_ids:
                    continue
                if str(order.get("order_status") or "").strip().lower() in filled_statuses:
                    filled_now.append(order_id)

            # A still-pending SELL with a partial fill has already
            # changed the broker position - reconcile for it too.
            partial_now = [
                str(o.get("order_id"))
                for o in pending_orders
                if str(o.get("transaction_type") or "").upper() == "SELL"
                and _filled_qty(o) > 0
            ]

            self._prev_pending_sell_order_ids = pending_ids

            if not filled_now and not partial_now:
                return

            now_ts = time.time()
            if now_ts - self._last_fill_refresh_ts < self._FILL_REFRESH_DEBOUNCE_SECONDS:
                return
            self._last_fill_refresh_ts = now_ts

            logger.info(
                f"Broker-side SELL fill detected "
                f"(filled: {filled_now or '[]'}, partial: {partial_now or '[]'}) - "
                f"refreshing position grid to reconcile with the broker."
            )
            threading.Thread(
                target=self._trader.refresh_open_pos_buy_price,
                daemon=True
            ).start()
            # A resting U/D exit filling broker-side fires neither
            # buy_order_result nor sell_order_result, so the Orders
            # grid never re-fetched and the trade only showed up after
            # a manual refresh / app restart (13:53:48 on 2026-09-22).
            # Nudge the frontend explicitly.
            try:
                self._trader.frontend_data_socket.emit(
                    'broker_side_fill_detected',
                    {"filled": filled_now, "partial": partial_now}
                )
            except Exception as emit_error:
                logger.debug(f"Could not emit broker-side fill event: {emit_error}")
        except Exception as e:
            logger.error(f"Broker-side SELL fill detection failed: {e}", exc_info=True)

    def cancel_all_pending_orders(self):
        logger.info("Cancelling ALL pending orders...")

        # Official one-shot endpoint first (docs: POST /openapi/typeb/
        # orders/cancelall). Whatever survives it (or any transport
        # error) is handled by the paced per-order loop below.
        try:
            conn = http.client.HTTPSConnection("api.mstock.trade", context=_SSL_CONTEXT)
            headers = {
                "X-Mirae-Version": "1",
                "X-PrivateKey": self.mstock_instance._api_key,
                "Authorization": f"Bearer {self.mstock_instance._access_token}",
                "Content-Type": "application/json"
            }
            conn.request("POST", "/openapi/typeb/orders/cancelall", body="", headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
            conn.close()
            logger.debug(f"Cancel-all HTTP status: {response.status} | {raw}")
        except Exception as e:
            logger.error(f"Cancel-all endpoint failed: {e}")

        # MStock throttles order requests: firing cancels back-to-back
        # (10/sec) draws 429s and transient IA500 500s. Pace each call
        # and retry the failures across passes until none are left or
        # the retry budget runs out.
        PACE_SECONDS = 0.3
        MAX_PASSES = 3

        pending_orders = self.fetch_all_pending_orders()
        logger.info(f"Pending orders found: {len(pending_orders)}")

        failed = [
            order for order in pending_orders if order.get("order_id")
        ]
        cancelled = 0

        for pass_number in range(1, MAX_PASSES + 1):
            still_failed = []

            for order in failed:
                order_id = order["order_id"]

                success = self.cancel_order(order_id)
                if success:
                    cancelled += 1
                else:
                    still_failed.append(order)

                time.sleep(PACE_SECONDS)

            if not still_failed:
                break

            if pass_number < MAX_PASSES:
                logger.info(
                    f"Cancel pass {pass_number}: {len(still_failed)} order(s) "
                    f"still pending; retrying after a pause..."
                )
                time.sleep(1.5)

            failed = still_failed

        logger.info(
            f"Cancelled {cancelled}/{len(pending_orders)} pending orders "
            f"in {pass_number} pass(es)."
        )

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
        filename = self._MSTOCK_INSTRUMENT_FILE
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(instrument_data, f, ensure_ascii=False, indent=4)
        return
    

    def fetch_all_positions(self):
        logger.info("Fetching all positions from M.Stock...")
        try:
            response = self._mstock_request("GET", "/openapi/typeb/portfolio/positions")

            if response is None:
                logger.error(
                    f"Error fetching positions: no valid response from M.Stock \n\n CONSIDER LOGGING IN AGAIN \n\n"
                )
                return None

            positions = position_attribute_mgmt(response.get("data"))
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
                
                quote = self.fetch_instrument_quote(data["exch_seg"] , data["token"])
                if (
                    not isinstance(quote, dict)
                    or not quote.get("data")
                    or not quote.get("data", {}).get("fetched")
                ):
                    raise ValueError(f"no quote data in response: {quote}")
                ltp = quote["data"]["fetched"][0]["ltp"]
                return ltp
            except Exception as e:
                logger.error(f"Fetch Instrument Quote failed for {tradingsymbol}: {e}")
                # Handle fallback for Nifty / Sensex
                fallback = self._fallback_ltp(tradingsymbol)
                logger.warning(f"Returning fallback LTP {fallback} for {tradingsymbol}.")
                return fallback

        except Exception as e:
            logger.error(f"Error getting LTP for {tradingsymbol}: {e}")
            # Same fallback logic for outer exception too
            return self._fallback_ltp(tradingsymbol)

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

            fetched_quote = quote["data"]["fetched"][0]
            ltp = fetched_quote["ltp"]
            open = fetched_quote["open"]
            close = fetched_quote.get("close") or fetched_quote.get("prevClose")
            return [ltp, open, close]
        except Exception as e:
            logger.error(f"Fetch Instrument Quote failed for {tradingsymbol}: {e}")
            # Handle fallback for Nifty / Sensex
            fallback = self._fallback_ltp(tradingsymbol)
            logger.warning(f"Returning fallback LTP {fallback} for {tradingsymbol}.")
            return [fallback, fallback]


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
            json_data = {
                "mode": "OHLC",
                "exchangeTokens": exchange_tokens
            }

            logger.debug(
                f"M.Stock batch quote request: {json_data}"
            )

            response_data = self._mstock_request(
                "POST",
                "/openapi/typeb/instruments/quote",
                body=json_data,
            )

            if response_data is None:
                logger.error(
                    "M.Stock batch quote failed: no valid response from M.Stock"
                )
                return {}

            logger.debug(
                f"M.Stock batch quote response: {response_data}"
            )

            if not response_data.get("status", False):
                logger.error(
                    f"M.Stock batch quote failed: {response_data}"
                )
                return {}

            response_data = _sanitise_closed_market_quote(response_data)

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
                close_price = quote.get("close") or quote.get("prevClose")

                if ltp is None:
                    logger.warning(
                        f"LTP missing for {symbol}: {quote}"
                    )
                    continue

                result[symbol] = [
                    ltp,
                    open_price if open_price is not None else ltp,
                    close_price
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

    def fetch_trades_for_date(self, trade_date):
        """
        Fetch executed fills for a specific (past) date from the M.Stock
        Trade History endpoint. The daily order book only covers the
        current session, but /openapi/typeb/trades accepts a from/to
        date range, so past days remain retrievable.

        Fills are clubbed per order (quantity-weighted average price,
        earliest fill time) to match the order-book granularity, and
        returned in the normalized shape consumed by
        build_orders_export.
        """
        logger.info(f"Fetching trade history for {trade_date} from M.Stock...")
        response = self._mstock_request(
            "GET",
            "/openapi/typeb/trades",
            body={"fromdate": str(trade_date), "todate": str(trade_date)},
        )

        if isinstance(response, list):
            response = response[0] if response else None

        if not isinstance(response, dict) or not response.get("status"):
            message = (
                response.get("message", "Unknown error")
                if isinstance(response, dict)
                else "No valid response"
            )
            logger.error(f"M.Stock trade history error for {trade_date}: {message}")
            return []

        fills = response.get("data") or []
        clubbed = {}

        for fill in fills:
            try:
                order_id = str(fill.get("orderid") or "")
                side = str(fill.get("transactiontype") or "").upper()
                symbol = str(fill.get("tradingsymbol") or "")
                fill_time = str(fill.get("filltime") or "").strip()
                quantity = float(fill.get("fillsize") or 0)
                price = float(fill.get("fillprice") or 0)
            except (TypeError, ValueError):
                continue

            if not order_id or side not in ("BUY", "SELL") or quantity <= 0 or not fill_time:
                continue

            key = (order_id, side, symbol)
            entry = clubbed.get(key)
            if entry is None:
                clubbed[key] = {
                    "timestamp": f"{trade_date} {fill_time}",
                    "tradingsymbol": symbol,
                    "transaction_type": side,
                    "quantity": quantity,
                    "average_price": price,
                    "order_id": order_id,
                    "order_status": "COMPLETE",
                }
            else:
                total_quantity = entry["quantity"] + quantity
                entry["average_price"] = (
                    (entry["average_price"] * entry["quantity"])
                    + (price * quantity)
                ) / total_quantity
                entry["quantity"] = total_quantity
                if fill_time < entry["timestamp"].split(" ", 1)[1]:
                    entry["timestamp"] = f"{trade_date} {fill_time}"

        orders = sorted(clubbed.values(), key=lambda item: item["timestamp"])
        logger.info(
            f"Trade history for {trade_date}: {len(fills)} fills "
            f"-> {len(orders)} orders"
        )
        return orders
    
    def _fetch_fund_summary_raw(self):
        logger.info("Fetching fund summary from M.Stock...")
        try:
            response = self._mstock_request("GET", "/openapi/typeb/user/fundsummary")

            if response is None:
                logger.error("Invalid JSON received from fund summary API (no valid response after retries)")
                return None

            logger.debug("Fund summary response: {response}", response=response)


            # ✅ 1. HANDLE INVALID SESSION CLEANLY
            if not response.get("status"):
                error_msg = response.get("message", "Unknown API error")
                logger.error(f"Fund summary API error: {error_msg}")

                # Optional: trigger logout or UI popup
                if "invalid session" in error_msg.lower():
                    logger.critical("Session expired. User must log in again.")
                    # The trader may not implement the hook; never let the
                    # expiry handler itself crash the caller.
                    session_hook = getattr(self._trader, "on_session_expired", None)
                    if callable(session_hook):
                        session_hook()

                return None  # ✅ graceful exit
        
            # ✅ 2. HANDLE MISSING DATA
            # M.Stock intermittently answers with status SUCCESS but an
            # empty data payload (observed mainly pre-market); the very
            # next call returns full data. One short retry absorbs this
            # transient instead of failing the caller.
            if response.get("data") is None:
                logger.warning("Fund summary data is None; retrying once...")
                time.sleep(0.5)
                response = self._mstock_request(
                    "GET", "/openapi/typeb/user/fundsummary"
                )
                if response is None:
                    logger.error(
                        "Invalid JSON received from fund summary API retry"
                    )
                    return None
                if not response.get("status"):
                    logger.error(
                        f"Fund summary API retry error: "
                        f"{response.get('message', 'Unknown API error')}"
                    )
                    return None
                if response.get("data") is None:
                    logger.error("Fund summary data is still None after retry")
                    return None

            return response

        except Exception as e:
            logger.error(f"Error fetching fund summary: {e}")
            return None
    
    def fetch_fund_summary(self):
        response = self._fetch_fund_summary_raw()
        if response is None:
            return None

        # ✅ 3. SAFE PROCESSING
        fund_summary = fund_summary_attribute_mgmt(response)
        self._trader.update_fund_summary(fund_summary)

        return fund_summary

    def fetch_day_start_cash(self):
        logger.info("Fetching start-of-day cash (LIMIT_SOD) from M.Stock...")
        response = self._fetch_fund_summary_raw()
        if response is None:
            return None

        try:
            data_list = response.get("data") or []
            sod_value = data_list[0].get("LIMIT_SOD") if data_list else None
            if sod_value is None:
                logger.warning("LIMIT_SOD not present in fund summary response")
                return None
            return float(sod_value)
        except (TypeError, ValueError, IndexError, KeyError) as e:
            logger.error(f"Could not read LIMIT_SOD from fund summary: {e}")
            return None
    
    def fetch_instrument_quote(self , exchange , instrument_token):
        logger.info(f"Fetching instrument quote for {exchange} {instrument_token} from M.Stock...")
        json_data = {
            'mode': 'OHLC',
            'exchangeTokens': {
            exchange : [instrument_token]
            },
        }
        response = self._mstock_request(
            'POST',
            '/openapi/typeb/instruments/quote',
            body=json_data,
        )
        if response is None:
            logger.error(f"Instrument quote fetch failed for {exchange} {instrument_token}: no valid response from M.Stock")
            return None
        if not response.get("status", True):
            logger.error("Instrument quote failed: {}", response)
        else:
            # Payload available at DEBUG only - the full body was logged
            # twice (INFO + DEBUG) on every call.
            logger.debug("Instrument quote response: {}", response)
        return _sanitise_closed_market_quote(response)
    
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


    def _schedule_post_sell_fund_refresh(self, max_wait_seconds=60, poll_interval=10):
        """
        M.Stock does not credit the sell proceeds back to AVAILABLE_BALANCE
        immediately after a square-off. The single fund fetch performed inside
        refresh_open_pos_buy_price() races the broker ledger and stores the
        pre-credit (low) cash balance, because of which the lots recalculated
        for the next buy stay at 0.

        Poll the fund summary in the background until the balance improves
        (or the retry window expires). Each re-fetch goes through the trader's
        _refresh_fund_summary_after_position_update(), which recalculates the
        weekly option lots and refreshes the frontend margin display.
        """
        try:
            balance_at_sell = float(
                self._trader._fund_summary.get("cash_balance", 0) or 0
            )
        except Exception:
            balance_at_sell = 0.0

        def _poll_fund_summary():
            waited = 0
            while waited < max_wait_seconds:
                time.sleep(poll_interval)
                waited += poll_interval
                try:
                    self._trader._refresh_fund_summary_after_position_update()

                    current_balance = float(
                        self._trader._fund_summary.get("cash_balance", 0) or 0
                    )
                    if current_balance > balance_at_sell:
                        logger.info(
                            f"Post-sell fund balance credited "
                            f"({balance_at_sell} -> {current_balance})."
                        )
                        return
                except Exception as e:
                    logger.error(f"Post-sell fund summary refresh failed: {e}")
                    return

            logger.info(
                "Post-sell fund refresh window expired. "
                "Using the latest broker balance."
            )

        threading.Thread(
            target=_poll_fund_summary,
            name="post-sell-fund-refresh",
            daemon=True
        ).start()
        logger.info(
            f"Scheduled post-sell fund summary refresh "
            f"(every {poll_interval}s for up to {max_wait_seconds}s). "
            f"Balance at sell: {balance_at_sell}"
        )

    def sell_units(self, trading_symbol, instrument_token , quantity , exchange , ltp, position_key=""):
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
                data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE, "token", str(instrument_token), prefix=INSTRUMENTS_SECTION_PREFIX)
                logger.info(f"Found instrument {instrument_token} in file "+ self._MSTOCK_INSTRUMENT_FILE)

            logger.debug(f"Instrument data for selling: {data}")
            trading_symbol_for_transaction = data["name"] or data.get("tradingsymbol", trading_symbol)
            #instrument_token = ["instrument_token"]
            lotsize = data["lotsize"]

            # Exit with the same product the position is held in - a
            # CARRYFORWARD sell against a MARGIN long is rejected by the
            # RMS as a fresh short (span requirement).
            sell_product = ""
            leg = self._trader._position_data.get(str(position_key or ""))
            if leg and leg.get("producttype"):
                sell_product = str(leg["producttype"]).upper()
            if not sell_product:
                for leg in self._trader._position_data.values():
                    if str(leg.get("instrument_token")) == str(instrument_token) and leg.get("producttype"):
                        sell_product = str(leg["producttype"]).upper()
                        break
            if not sell_product:
                sell_product = "CARRYFORWARD"

            json_data = { 
                'variety': 'NORMAL',
                'tradingsymbol': trading_symbol_for_transaction,
                'symboltoken': instrument_token,
                'exchange': exchange,
                'transactiontype': 'SELL',
                'ordertype': 'MARKET',
                'quantity': str(int(quantity) * int(lotsize)),
                'producttype': sell_product,
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

            self._trader.frontend_data_socket.emit('sell_order_result',{"success": True, "tradingsymbol": trading_symbol , "lots": quantity, "position_key": position_key})
            #self._trader.frontend_data_socket.emit('status_message', {"message": "Refreshing position now..."})
            logger.info("Refreshing open positions and buy prices after SELL...This would update the fund summar / cash balance as well")
            self._trader.refresh_open_pos_buy_price()
            self._schedule_post_sell_fund_refresh()

            return True
            
        except Exception as e:
            logger.error(f"Error selling units {e}")
            raise
    def place_exit_limit(self, trading_symbol, instrument_token, quantity, exchange, price):
        """Place a SELL LIMIT exit order. quantity is in LOTS.

        Used by the U/D immediate exit at buy time (via sell_units_temp)
        and to re-place a partial leg's exit after a manual partial sell.
        """
        return self.sell_units_temp(
            trading_symbol,
            instrument_token,
            quantity,
            exchange,
            ltp=price,
            limit_market="LIMIT",
            price=price
        )

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
                data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE, "token", str(instrument_token), prefix=INSTRUMENTS_SECTION_PREFIX)
                logger.info(f"Found instrument {instrument_token} in file "+ self._MSTOCK_INSTRUMENT_FILE)

            logger.debug(f"Instrument data for selling: {data}")
            trading_symbol_for_transaction = data["name"] or data.get("tradingsymbol", trading_symbol)
            #instrument_token = ["instrument_token"]
            lotsize = data["lotsize"]

            # Exit with the same product the position is held in - a
            # CARRYFORWARD exit against a MARGIN long is rejected by the
            # RMS as a fresh short (span requirement).
            sell_product = ""
            for leg in self._trader._position_data.values():
                if str(leg.get("instrument_token")) == str(instrument_token) and leg.get("producttype"):
                    sell_product = str(leg["producttype"]).upper()
                    break
            if not sell_product:
                sell_product = "CARRYFORWARD"

            json_data = { 
                'variety': 'NORMAL',
                'tradingsymbol': trading_symbol_for_transaction,
                'symboltoken': instrument_token,
                'exchange': exchange,
                'transactiontype': 'SELL',
                'ordertype': limit_market,
                'quantity': str(int(quantity) * int(lotsize)),
                'producttype': sell_product,
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

            self._trader.frontend_data_socket.emit('sell_order_result',{"success": True, "tradingsymbol": trading_symbol , "lots": quantity})
            self._trader.frontend_data_socket.emit('status_message',{"success": True, "message": "Refreshing position now..."})
            logger.info("Refreshing open positions and buy prices after SELL...This would update the fund summar / cash balance as well")
            self._trader.refresh_open_pos_buy_price()
            self._schedule_post_sell_fund_refresh()

            return True
            
        except Exception as e:
            logger.error(f"Error selling units {e}")
            raise

    def buy_units(self, trading_symbol = None, instrument_token = None, quantity = 0, exchange="",ltp=0,mode="",sell_mode="",target_profit=0,strategy=""):
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
                    data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE , "name" , trading_symbol, prefix=INSTRUMENTS_SECTION_PREFIX)
                elif instrument_token:
                    data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE , "token" , instrument_token, prefix=INSTRUMENTS_SECTION_PREFIX)
            
            if not data:
                raise Exception(f"Instrument not found in {self._MSTOCK_INSTRUMENT_FILE} for {trading_symbol or instrument_token}")

            trading_symbol_for_transaction = data["name"]
            #instrument_token = data["token"]
            lotsize = data["lotsize"]
            
            logger.debug(f"quantity : {quantity} and lotsize {lotsize} ")

            quantity = int(quantity) * int(lotsize)

            # Split into legs that are whole multiples of the lot size,
            # otherwise the exchange rejects the leg (MA200).
            leg_quantity = max(1, int(self._get_order_freeze_limit()) // int(lotsize)) * int(lotsize)
            full_legs = quantity // leg_quantity
            remainder = quantity % leg_quantity
            legs = [leg_quantity] * full_legs
            
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
                        self._trader.frontend_data_socket.emit('buy_order_result',{"success": True, "tradingsymbol": trading_symbol , "lots": buy_quantity})
                        # FAST POSITION GRID UPDATE
                        # Do this immediately after successful BUY,
                        # before any SELL handling or REST position refresh.
                        self._trader.fast_update_position_after_buy(
                            trading_symbol=trading_symbol,
                            instrument_token=instrument_token,
                            quantity=int(qty) // int(lotsize),
                            lotsize=lotsize,
                            executed_buy_price=ltp,
                            exchange=exchange,
                            strategy=strategy,
                            sell_mode=sell_mode or "T"
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
                            if sell_mode == "U": # Undetermined Profit at LTP + target_profit
                                # Active U branch: a U-leg bought through
                                # THIS path (insufficient-funds retry) used
                                # to get no resting exit at all - the U
                                # branch only lived in buy_sell_units. On
                                # 2026-09-22 the 10:26 / 12:08 / 12:21 U
                                # retries traded naked with no exit limit.
                                logger.info(f"System placing Sell Order at LTP + {target_profit} points immediately after buy order is placed")
                                self.sell_units_temp(trading_symbol, instrument_token , buy_quantity , exchange,ltp=ltp,limit_market="LIMIT",price=ltp+target_profit)
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

    def buy_sell_units(self, trading_symbol = None, instrument_token = None, quantity = 0, exchange="",ltp=0,mode="",sell_mode="",target_profit=0,strategy=""):
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
                        data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE , "name" , trading_symbol, prefix=INSTRUMENTS_SECTION_PREFIX)
                    elif instrument_token:
                        data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE , "token" , instrument_token, prefix=INSTRUMENTS_SECTION_PREFIX)
                
                if not data:
                    raise Exception(f"Instrument not found in {self._MSTOCK_INSTRUMENT_FILE} for {trading_symbol or instrument_token}")

                trading_symbol_for_transaction = data["name"]
                #instrument_token = data["token"]
                lotsize = data["lotsize"]
                
                logger.debug(f"quantity : {quantity} and lotsize {lotsize} ")

                quantity = int(quantity) * int(lotsize)

                # Split into legs that are whole multiples of the lot size,
                # otherwise the exchange rejects the leg (MA200).
                leg_quantity = max(1, int(self._get_order_freeze_limit()) // int(lotsize)) * int(lotsize)
                full_legs = quantity // leg_quantity
                remainder = quantity % leg_quantity
                legs = [leg_quantity] * full_legs
                
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
                            self._trader.frontend_data_socket.emit('buy_order_result',{"success": True, "tradingsymbol": trading_symbol , "lots": buy_quantity})
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

                        self._trader.frontend_data_socket.emit('sell_order_result',{"success": True, "tradingsymbol": trading_symbol , "lots": buy_quantity,"Sell Price": ltp+target_profit})
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
    def _select_contract_note_time(order_time, trade_time):
        """Select OrderTime, falling back to TradeTime.

        Seconds are kept exactly as printed (00 when the note shows a
        whole minute or omits seconds). They must never be fabricated
        from the price: deriving seconds from price turned 09:37:00 on
        a Rs.11.45 fill into 09:37:11 and corrupted durations/FIFO.
        """
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
                        order_time, trade_time
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

    @staticmethod
    def _extract_trade_date(pdf_path):
        """
        Resolve the trade date printed on the contract note.

        The TRADE DATE field is repeated on the leading pages (cover
        block, settlement summary) and combined notes also reference
        the previous day, so every occurrence is parsed and the latest
        one wins: the trades table always belongs to the most recent
        trade date printed on the note. Falls back to today only when
        nothing matches, which is logged loudly.
        """
        import pymupdf

        month = r"[A-Za-z]{3,9}"
        day = r"\d{1,2}"
        year = r"\d{4}"
        patterns = [
            # "TRADE DATE Sep 18 2026" / "TRADE DATE: September 18, 2026"
            rf"TRADE DATE\s*:?\s*({month})\s+({day}),?\s+({year})",
            # "TRADE DATE 18 Sep 2026" / "TRADE DATE: 18-Sep-2026"
            rf"TRADE DATE\s*:?\s*({day})\s*[-\s]\s*({month})\s*[-\s]\s*({year})",
        ]
        formats = ["%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"]

        document = pymupdf.open(pdf_path)
        try:
            text = "\n".join(page.get_text() for page in document[:3])
        finally:
            document.close()

        parsed_dates = []
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                value = " ".join(part for part in match.groups() if part)
                for fmt in formats:
                    try:
                        parsed_dates.append(
                            datetime.datetime.strptime(value, fmt).date()
                        )
                        break
                    except ValueError:
                        continue

        if parsed_dates:
            trade_date = max(parsed_dates)
            if len(set(parsed_dates)) > 1:
                logger.info(
                    "Contract note shows multiple trade dates "
                    f"{sorted(set(parsed_dates))}; using {trade_date}"
                )
            return trade_date

        logger.warning(
            "No TRADE DATE found on the contract note leading pages; "
            "falling back to today's date"
        )
        return datetime.datetime.now().date()

    def convert_mstock_contract_note_data(self, pdf_path: str):
        """
        Converts mStock contract note PDF into FIFO-paired trade rows.
        Returns (output_rows, pine_script, trade_date).
        """
        import camelot

        # ---------- Pass 1a: Legacy page-3 parse ----------
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
                f"Camelot could not parse page 3 of the contract note: {error}"
            )
            raw_trades = []

        # ---------- Pass 1b: All-pages parse ----------
        # The trade table is not always on page 3: short/daily contract
        # notes carry it on page 1-2, and asking Camelot for a page the
        # PDF does not have makes it raise an IndexError. Retry across
        # every page (Camelot resolves "all" against the real page
        # count) before falling back to OCR.
        if not raw_trades:
            try:
                tables = camelot.read_pdf(
                    pdf_path,
                    pages="all",
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
            raise ValueError(
                "No BUY/SELL trades found in the contract note "
                "(tried page 3, all pages and OCR). The PDF may be "
                "scanned, encrypted or use an unsupported layout."
            )

        trade_date = self._extract_trade_date(pdf_path)
        logger.info(f"Contract-note trade date resolved as {trade_date}")

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
                    previous["price"] = round(
                        (
                            (previous["price"] * previous["quantity"])
                            + (trade["price"] * trade["quantity"])
                        ) / total_quantity,
                        2,
                    )
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
            # VBA parity: bare ints (Hour(dt) etc.) — zero-padded values
            # like 09 produce leading-zero literals Pine rejects.
            note_dt = trade["datetime"]
            timeline_script.append(
                f"{time_var}=timestamp('Asia/Kolkata',{note_dt.year},{note_dt.month},{note_dt.day},{note_dt.hour},{note_dt.minute},{note_dt.second})"
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
                    # VBA parity: pnlRate is rounded to 2 decimals BEFORE
                    # the pnl multiplication, pct uses the ROUNDED pnl and
                    # PnL_RT accumulates the rounded pnl.
                    pnl_rate_value = round(trade["price"] - average_buy, 2)
                    pnl = round(pnl_rate_value * matched_quantity)
                    cumulative_pnl += pnl
                    elapsed_seconds = int(
                        (trade["datetime"] - first_buy_time).total_seconds()
                    )
                    duration = f"{elapsed_seconds // 60:02d}:{elapsed_seconds % 60:02d}"
                    pnl_rate = pnl_rate_value
                    purchase_value = average_buy * matched_quantity
                    pnl_pct = round((pnl / purchase_value) * 100, 2) if purchase_value else 0
                    pnl_rt = cumulative_pnl

                    sell_time_var = time_var
                    for match_number, (buy, matched) in enumerate(matched_buys, start=1):
                        # VBA computes duration/pnl/pct against THE matched
                        # buy; with one sell filled across several buys each
                        # zone label carries its own match's values.
                        pair_purchase_value = buy["price"] * matched
                        pair_pnl = round(
                            (trade["price"] - buy["price"]) * matched
                        )
                        pair_pct = (
                            round((pair_pnl / pair_purchase_value) * 100, 2)
                            if pair_purchase_value else 0
                        )
                        pair_seconds = int(
                            (trade["datetime"] - buy["datetime"]).total_seconds()
                        )
                        pair_duration = (
                            f"{pair_seconds // 60:02d}:{pair_seconds % 60:02d}"
                        )
                        # VBA names the boxes/labels after the sell's
                        # timeline var (bz_t260918_5); the _N suffix only
                        # appears when one sell was filled across several
                        # buys, which the VBA never had to name.
                        pine_id = (
                            f"t{sequence_id}"
                            if match_number == 1
                            else f"t{sequence_id}_{match_number}"
                        )
                        top_color = "color.new(color.green,0)" if buy["option_type"] == "CE" else "color.new(color.red,0)"
                        bottom_color = "color.new(color.green,0)" if pnl >= 0 else "color.new(color.red,0)"
                        mid_time = f"({buy['time_var']}+{sell_time_var})/2"
                        mid_y = "(high+low)/2"
                        order_zone_script.append(
                            f"if barstate.islast\n    var bz_{pine_id} = box.new({buy['time_var']},high,{sell_time_var},{mid_y},xloc=xloc.bar_time,bgcolor={top_color},border_width=0)"
                        )
                        pnl_zone_script.append(
                            f"if barstate.islast\n    var pbz_{pine_id} = box.new({buy['time_var']},{mid_y},{sell_time_var},low,xloc=xloc.bar_time,bgcolor={bottom_color},border_width=0)"
                        )
                        order_label_script.append(
                            f"if barstate.islast\n    var l_{pine_id} = label.new({mid_time},(high+{mid_y})/2,text='{matched:g}  {pair_duration}  {round(trade['price'] - buy['price'], 1):.1f}',xloc=xloc.bar_time,style=label.style_label_center,color=color.new(color.black,15),textcolor=color.white,size=size.normal,textalign=text.align_center,text_font_family=font.family_monospace)"
                        )
                        pnl_label_script.append(
                            f"if barstate.islast\n    var pl_{pine_id} = label.new({mid_time},(low+{mid_y})/2,text='{format_lakhs(pair_purchase_value)}  {pair_pnl:,.0f}  {pair_pct:.2f}%',xloc=xloc.bar_time,style=label.style_label_center,color=color.new(color.black,15),textcolor=color.white,size=size.normal,textalign=text.align_center,text_font_family=font.family_monospace)"
                        )

            output_rows.append(apply_trade_charges({
                "ORDERDATE": trade["datetime"].strftime("%m/%d/%Y"),
                "ORDERTIME": trade["datetime"].strftime("%H:%M:%S"),
                "TRAN": trade["type"],
                "CONT": trade["instrument"],
                "Product": "MIS",
                "Qty.": int(trade["quantity"]),
                "RATE": trade["price"],
                "STATUS": "COMPLETE",
                "Duration": duration,
                "PurValue": format_lakhs(average_buy * matched_quantity) if trade["type"] == "SELL" and matched_quantity else "",
                "PnL_Rate": pnl_rate,
                "PnL": pnl,
                "PnL%": pnl_pct,
                "PnL_RT": pnl_rt,
            }))
            sequence += 1

        # Section order and headers mirror the VBA final-output cell:
        # TIMELINE, ORDER ZONES, ORDER LABELS, PNL ZONES, PNL LABELS.
        pine_script = "\n".join(
            [
                "// === TIMELINE ===",
                *timeline_script,
                "// === ORDER ZONES ===",
                *order_zone_script,
                "// === ORDER LABELS ===",
                *order_label_script,
                "// === PNL ZONES ===",
                *pnl_zone_script,
                "// === PNL LABELS ===",
                *pnl_label_script,
            ]
        )

        return output_rows, pine_script, trade_date

    def convert_mstock_contract_note(self, pdf_path: str) -> str:
        """
        Converts mStock contract note PDF into the standard Orders
        workbook. Returns path to the generated xlsx (Utilities flow).
        """
        output_rows, pine_script, _trade_date = self.convert_mstock_contract_note_data(pdf_path)
        return write_orders_workbook(output_rows, pine_script)



    def get_instrument_details(self , tradingsymbol):
        logger.info(f"Getting instrument details for {tradingsymbol} from M.Stock...")
        data = find_matching_object(self._MSTOCK_INSTRUMENT_FILE , "name" , tradingsymbol, prefix=INSTRUMENTS_SECTION_PREFIX)
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
            if self.get_instrument_meta().get("expire_together"):
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
                        token,
                        prefix=INSTRUMENTS_SECTION_PREFIX,
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


        # The reduced file's meta block records which underlying it was
        # built for (and when). A missing/legacy meta block means the
        # file predates the meta design, so it is treated as stale.
        reduced_meta = read_instrument_meta("mstock_instrument_list_reduced.json")
        underlying_exists = reduced_meta.get("underlying") == underlying

        #UnderlyingExist = true check helps to check if the underlying exists int the reduced file ..if not then we need to download the latest instrument list again and then reduce it again
        #This scenario can happen when user changes the underlying from NIFTY to SENSEX or vice versa post download of instrument list and reduction for the day
        if last_downloaded_date.date() == datetime.datetime.now().date() and underlying_exists == True:
            instruments = None
            meta_fresh = False
            try:
                if (
                    reduced_meta.get("near_month_future_token")
                    and reduced_meta.get("generated_on") == datetime.datetime.now().date().isoformat()
                ):
                    meta_fresh = True
            except Exception as e:
                logger.debug(f"Instrument meta freshness check failed: {e}")
                meta_fresh = False

            if meta_fresh:
                self._instrument_meta = reduced_meta
                logger.info("✅ Instrument meta already computed today for this underlying; skipping recompute.")
            else:
                instruments, meta = self.compute_instrument_meta("mstock_instrument_list.json", underlying)
                if meta:
                    self._instrument_meta = meta

            if not self._instrument_meta:
                # No usable meta (e.g. the prod_start(None, None) pre-fetch);
                # the master file is refreshed but nothing can be reduced.
                return False
            self.reduce_instrument_list(self._instrument_meta, instruments=instruments)
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
            
            file_path = resolve_data_path("mstock_instrument_list.json")
            with open(file_path, 'w') as f:
                json.dump(response_json, f, separators=(",", ":"))

            logger.info(f"✅ Successfully downloaded and saved instrument list data to {file_path}.")
            write_to_json({"mstock_last_downloaded_instruments_timestamp" : datetime.datetime.now().isoformat()} , "access_token.json")

            instruments, meta = self.compute_instrument_meta("mstock_instrument_list.json", underlying)
            if not meta:
                return False
            self._instrument_meta = meta
            self.reduce_instrument_list(self._instrument_meta, instruments=instruments)

            return True

        except json.JSONDecodeError:
            logger.error(f"❌ Error decoding JSON response. Response was: {response_body[:200]}...")
            return False
        except Exception as e:
            logger.error(f"❌ Error downloading instrument list: {e}")
            return False

    def _load_reduced_instruments(self):
        """
        Load the instruments array from the reduced file. Handles both
        the current {"meta": ..., "instruments": [...]} shape and the
        legacy plain-array shape.
        """
        try:
            with open(self._MSTOCK_INSTRUMENT_FILE, "r") as f:
                existing = json.load(f)
        except Exception:
            return None

        if isinstance(existing, dict):
            return existing.get("instruments") or []
        return existing

    def _reduced_list_covers_window(self, fut_token, underlying, target_expiry, ltp, margin=1000):
        """
        Return True when the existing reduced instrument file already
        contains the near-month future plus CE and PE option strikes for
        the target expiry covering ltp +/- margin, meaning a reload of
        the 40MB+ master list is unnecessary.
        """
        existing = self._load_reduced_instruments()
        if existing is None:
            return False

        fut_ok = False
        has_ce = False
        has_pe = False
        strikes = []

        for inst in existing:
            name = inst.get("name") or inst.get("tradingsymbol") or inst.get("symbol")
            if name == fut_token:
                fut_ok = True
                continue

            if (
                inst.get("symbol") == underlying
                and str(inst.get("instrumenttype", "")).upper() == "OPTIDX"
                and str(inst.get("expiry", "")).upper() == str(target_expiry).upper()
            ):
                try:
                    strikes.append(int(float(inst.get("strike", 0))))
                except (TypeError, ValueError):
                    continue

                upper_name = str(name or "").upper()
                if upper_name.endswith("CE"):
                    has_ce = True
                elif upper_name.endswith("PE"):
                    has_pe = True

        if not (fut_ok and has_ce and has_pe and strikes):
            return False

        return min(strikes) <= ltp - margin and max(strikes) >= ltp + margin

    def _reduced_list_has_target_expiry(self, fut_token, underlying, target_expiry):
        """
        Return True when the existing reduced instrument file contains the
        near-month future plus at least one CE and one PE option row for
        the target expiry — i.e. it is usable as-is even when the current
        LTP is unknown (no live quote, no persisted price).
        """
        existing = self._load_reduced_instruments()
        if existing is None:
            return False

        fut_ok = False
        has_ce = False
        has_pe = False

        for inst in existing:
            name = inst.get("name") or inst.get("tradingsymbol") or inst.get("symbol")
            if name == fut_token:
                fut_ok = True
                continue

            if (
                inst.get("symbol") == underlying
                and str(inst.get("instrumenttype", "")).upper() == "OPTIDX"
                and str(inst.get("expiry", "")).upper() == str(target_expiry).upper()
            ):
                upper_name = str(name or "").upper()
                if upper_name.endswith("CE"):
                    has_ce = True
                elif upper_name.endswith("PE"):
                    has_pe = True

        return fut_ok and has_ce and has_pe

    def reduce_instrument_list(self, meta: dict, instruments=None):
        """
        Reduce mstock_instrument_list.json based on the instrument meta:
        - Keep only the near month future token
        - Keep only underlying options expiring on the near option expiry (exact match)
        - Keep only strikes within +/-3000 of FUT LTP and multiples of the
          derived strike interval

        `instruments` may carry an already-loaded instrument list (e.g.
        from compute_instrument_meta) so the 40MB+ master JSON is
        parsed only once per startup; when omitted the file is loaded
        from disk as before.

        Fast path: when the master list is not already loaded and the
        existing same-day reduced file still covers the current LTP
        window, the master file is not loaded at all.

        Output shape: {"meta": <meta>, "instruments": [...]} so the
        derived values persist inside the instrument file itself.
        """

        logger.info("Reducing instrument list based on instrument meta...")

        try:
            # Load meta
            FUT_TOKEN   = meta["near_month_future_token"]
            UNDERLYING  = meta["underlying"]
            STRIKE_INTERVAL = int(meta.get("strike_interval") or 100)

            # Near option expiry is stored as ISO (e.g. "2026-09-17");
            # the instrument rows use "17Sep2026".
            near_option_expiry = datetime.date.fromisoformat(str(meta["near_option_expiry"]))
            TARGET_EXPIRY = near_option_expiry.strftime("%d%b%Y")

            fut_details = None
            if instruments is not None:
                for inst in instruments:
                    token = inst.get("name") or inst.get("tradingsymbol") or inst.get("symbol")
                    if token == FUT_TOKEN:
                        fut_details = inst
                        break
            else:
                try:
                    fut_details = find_matching_object(
                        self._MSTOCK_INSTRUMENT_FILE, "name", FUT_TOKEN,
                        prefix=INSTRUMENTS_SECTION_PREFIX,
                    )
                except Exception:
                    fut_details = None

            fut_ltp = None
            fut_open = None
            fut_close = None
            if fut_details:
                # The M.Stock quote gateway is intermittently flaky (502s /
                # timeouts). One retry after a short pause recovers most
                # transient failures before any fallback LTP is considered.
                for attempt in (1, 2):
                    try:
                        quote = self.fetch_instrument_quote(
                            fut_details.get("exch_seg", ""),
                            str(fut_details.get("token", ""))
                        )
                        # ✅ HARD VALIDATION BEFORE ACCESSING
                        if (
                            not isinstance(quote, dict)
                            or not quote.get("data")
                            or not quote.get("data", {}).get("fetched")
                        ):
                            raise ValueError(f"no quote data in response: {quote}")
                        fetched = quote["data"]["fetched"][0]
                        fut_ltp = fetched["ltp"]
                        fut_open = fetched.get("open")
                        fut_close = fetched.get("close") or fetched.get("prevClose")
                        break
                    except Exception as e:
                        logger.error(
                            f"Fetch Instrument Quote failed for {FUT_TOKEN} "
                            f"(attempt {attempt}/2): {e}"
                        )
                        if attempt == 1:
                            time.sleep(2)

            if fut_ltp is None:
                # Data-driven fallback first: a real price persisted by an
                # earlier session beats these months-old hardcoded levels.
                try:
                    persisted = self._trader.load_last_known_price(str(FUT_TOKEN))
                except Exception:
                    persisted = None

                if persisted:
                    logger.warning(
                        f"Using persisted last-known price {persisted['ltp']} "
                        f"for {FUT_TOKEN} (age {persisted['age_days']} day(s))."
                    )
                    fut_ltp = persisted["ltp"]
                    if fut_open is None:
                        fut_open = persisted.get("open")
                    if fut_close is None:
                        fut_close = persisted.get("prev_close")

            used_fallback_ltp = fut_ltp is None
            if used_fallback_ltp:
                fut_ltp = self._fallback_ltp(FUT_TOKEN)
                logger.warning(f"Returning fallback LTP = {fut_ltp} for {FUT_TOKEN}.")

            self._trader._NEAR_MONTH_FUTURE_LTP = fut_ltp
            self._trader._near_month_future_quote_cache = {
                "ltp": fut_ltp,
                "open": fut_open,
                "prev_close": fut_close,
                "ts": time.time(),
            }

            strike_lower = int(fut_ltp - 3000)
            strike_upper = int(fut_ltp + 3000)

            logger.info(
                f"Filtering: FUT={FUT_TOKEN}, expiry={TARGET_EXPIRY}, "
                f"strike range={strike_lower} to {strike_upper}"
            )

            if instruments is None and self._reduced_list_covers_window(
                FUT_TOKEN, UNDERLYING, TARGET_EXPIRY, fut_ltp
            ):
                logger.info(
                    "✅ Existing reduced instrument list still covers the current "
                    "LTP window; skipping master list reload."
                )
                return True

            # The fallback LTP can be stale. Regenerating the
            # reduced list around it would wipe out the last good list with
            # a wrong strike window and break weekly-option lookups. When no
            # real price was available, keep the existing list instead (as
            # long as it already has the future plus CE and PE rows for the
            # target expiry).
            if used_fallback_ltp and self._reduced_list_has_target_expiry(
                FUT_TOKEN, UNDERLYING, TARGET_EXPIRY
            ):
                logger.warning(
                    "⚠️ No live or persisted LTP for the future; the fallback "
                    "LTP may be stale, so the existing reduced instrument "
                    "list is kept instead of regenerating it around a wrong "
                    "strike window."
                )
                return True

            if instruments is None:
                with open(resolve_data_path("mstock_instrument_list.json"), "r") as f:
                    data = json.load(f)
            else:
                data = instruments

            filtered = []

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
                        and strike % STRIKE_INTERVAL == 0
                    ):
                        filtered.append(inst)

            # Write reduced output with the derived meta persisted in the
            # same file (replaces the old appconfig.json derived keys).
            output = {"meta": meta, "instruments": filtered}
            with open(resolve_data_path("mstock_instrument_list_reduced.json"), "w") as f:
                json.dump(output, f, indent=2)

            logger.info(
                f"Done! {len(filtered)} records + meta saved to mstock_instrument_list_reduced.json"
            )
            return True

        except Exception as e:
            logger.error(f"❌ Error reducing instrument list: {e}")
            return False


    def compute_instrument_meta(self, json_instrument_file, underlying):
        """
        Derive the instrument meta (exchange, near-month future token,
        near option expiry, expire-together flag, strike interval) from
        the instrument master file. Nothing is written to appconfig.json
        anymore; the meta is persisted in the reduced instrument file by
        reduce_instrument_list and exposed via get_instrument_meta().

        Returns (instruments, meta) where `instruments` is the loaded
        master list (so the caller avoids re-parsing the 40MB+ file) and
        `meta` is None when derivation failed.
        """
        json_instrument_file = resolve_data_path(json_instrument_file)
        logger.debug(f"Instrument meta to be computed for {json_instrument_file} and underlying {underlying}")

        # --- 1️⃣ Load instrument data from JSON ---
        try:
            with open(json_instrument_file, "r") as f:
                instruments = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load {json_instrument_file}: {e}")
            return None, None

        if not underlying:
            # prod_start() calls download_instrument_list(None, None) just
            # to refresh the master file; no meta can be derived yet.
            logger.debug("No underlying supplied; skipping instrument meta computation.")
            return instruments, None

        # Convert list of dicts → DataFrame for easier filtering
        df = pd.DataFrame(instruments)
        logger.debug(f"Loaded {len(df)} instruments from {json_instrument_file}")
        underlying = underlying.upper().strip()

        # --- 2️⃣ Filter for futures of the given underlying ---
        fut_df = df[
            (df["instrumenttype"].str.upper() == "FUTIDX") &
            (df["symbol"].str.upper() == underlying)
        ].copy()

        exchange = ""
        trading_symbol = ""
        expiry_date_future = None

        if fut_df.empty:
            logger.warning(f"No futures found for {underlying}")
        else:
            fut_df["expiry"] = pd.to_datetime(fut_df["expiry"], errors="coerce")
            nearest_future = fut_df.sort_values("expiry").iloc[0]

            exchange = str(nearest_future.get("exch_seg", "") or "")
            trading_symbol = str(nearest_future.get("name", "") or "")
            expiry_date_future = nearest_future.get("expiry", None)

        # --- 3️⃣ Find nearest weekly options of this underlying ---
        today = pd.Timestamp.now().normalize()

        opt_df = df[
            (df["instrumenttype"].str.upper() == "OPTIDX") &
            (df["symbol"].str.upper() == underlying) &
            (df["name"].str.upper().str.endswith(("CE", "PE")))
        ].copy()

        near_option_expiry = None
        strike_interval = 100

        if not opt_df.empty:
            opt_df["expiry"] = pd.to_datetime(opt_df["expiry"], errors="coerce")
            future_opts = opt_df[opt_df["expiry"] >= today]
            if not future_opts.empty:
                near_option_expiry_ts = future_opts.sort_values("expiry")["expiry"].dropna().iloc[0]
                near_option_expiry = near_option_expiry_ts.date()

        # --- 5️⃣ Determine if future and option expire together ---
        expire_together = False
        if (
            expiry_date_future is not None
            and pd.notna(expiry_date_future)
            and near_option_expiry is not None
        ):
            expire_together = near_option_expiry == expiry_date_future.date()

        if not exchange:
            exchange = get_exchange_for_underlying(underlying) or ""

        meta = {
            "underlying": underlying,
            "exchange": exchange,
            "near_month_future_token": trading_symbol,
            "near_option_expiry": near_option_expiry.isoformat() if near_option_expiry else None,
            "expire_together": bool(expire_together),
            "strike_interval": int(strike_interval),
            "generated_on": datetime.datetime.now().date().isoformat(),
        }

        logger.info(f"Computed instrument meta: {json.dumps(meta, indent=4)}")

        return instruments, meta

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
        import requests
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