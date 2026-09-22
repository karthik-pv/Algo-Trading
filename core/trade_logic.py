import re
import threading
import logging
import queue
import datetime
import os
from flask import Flask,request
from typing import Optional , Dict , Any
from flask_socketio import SocketIO
from interface.broker_interface import BrokerInterface
from loguru import logger
import json

import time
import pythoncom
import win32com.client



from core.trade_utils import calculate_accurate_average_buy_price_and_update_positions
from core.shared_state import latest_tradingview_data


from core.utils import fetch_from_json , find_matching_object , is_market_open , write_to_json , compute_limit_margin , get_exchange_for_underlying , get_underlying_for_symbol , resolve_data_path
import time

# NOTE: Trading thresholds (PCT/PTS book-profit, stop loss, strategy
# factors, log frequency) are intentionally NOT cached in module
# globals. They are fetched via fetch_from_json("appconfig.json", ...)
# at the point of decision, so a config save (which clears the JSON
# cache) applies them live without an app restart. Only MODE, BROKER
# and UNDERLYING are captured at import time and require a restart.


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
    # MODE, BROKER and UNDERLYING are captured at import time on purpose:
    # they define the running adapter/session and require an app restart.
    _use_max_margin = True
    _mode = fetch_from_json("appconfig.json" , "MODE")
    _broker_string = fetch_from_json("appconfig.json" , "BROKER")
    _exchange = get_exchange_for_underlying(fetch_from_json("appconfig.json" , "UNDERLYING"))
    _underlying = fetch_from_json("appconfig.json" , "UNDERLYING")


    _near_month_future_ltp = 0.0
    ltp_near_month_future = 0.0
    open_near_month_future = 0.0
    prev_close_near_month_future = 0.0
    _near_month_future_quote_cache = {}
    _order_strategy_mapping = {}
    _order_sell_mode_mapping = {}

    _positions_to_subscribe = []
    
    _tradingView_Data = {}

    # ---------------------------------------------------------
    # TradingView entry validation state (TV_DATA_Validation_REQUIRED)
    # Defaults are fail-safe: until a valid TradingView webhook is
    # processed, CE and PE buys are considered NOT allowed.
    # ---------------------------------------------------------
    _tv_validation_required = False
    _tv_ce_buy_allowed = False
    _tv_pe_buy_allowed = False
    _tv_ce_reason = "No TradingView data received yet"
    _tv_pe_reason = "No TradingView data received yet"

    _latest_prices = {}
    _prev_close_prices = {}
    # Per-token throttle timestamps for persisting last-known prices on
    # live ticks (one entry per token per minute).
    _price_persist_ts = {}
    # Tick-path persistence is batched: entries accumulate here on ticks
    # and are flushed as a single file write per minute.
    _pending_tick_price_updates = {}
    _last_price_flush_ts = 0.0
    _last_position_pnl_log_ts = 0.0
    _last_freq_log_ts = {}
    _last_watcher_heartbeat_ts = 0.0
    # Watchdog bookkeeping for the SL watcher thread. The loop stamps
    # _watcher_loop_alive_ts every cycle; the supervisor restarts the
    # thread when it dies or stops making progress. The generation
    # counter ensures a previously stalled-but-revived thread exits
    # instead of running alongside its replacement (double sells).
    _watcher_loop_alive_ts = 0.0
    _watcher_generation = 0
    _watcher_thread_ref = None
    _watcher_supervisor_started = False
    _voice_announcement_enabled = True

    # ---------------------------------------------------------
    # Paper trading store
    # ---------------------------------------------------------
    _paper_trade_lock = threading.Lock()
    _PAPER_TRADE_FILENAME = resolve_data_path("PaperTrading.txt")
    _paper_realized_pnl = 0.0

    # Serializes weekly-option table builds: websocket reconnects
    # (on_connect re-fires) and manual refresh clicks can trigger
    # overlapping setup runs that would corrupt the table state.
    _woc_setup_lock = threading.Lock()

    _sell_order_lock = threading.Lock()
    _sell_in_flight = {}
    _SELL_IN_FLIGHT_TIMEOUT_SECS = 10

    # Browser-close shutdown: connected frontend socket ids. When the
    # LAST one disconnects (browser/app window closed), the app shuts
    # itself down so the server/log console windows close with it.
    _browser_sids = set()
    _BROWSER_EXIT_GRACE_SECONDS = 10

    # ---------------------------------------------------------
    # Persisted last-known real prices. Written whenever a websocket
    # tick or REST quote resolves the near-month future price, read
    # as a data-driven fallback when every live source fails (fresher
    # than the static NIFTY/SENSEX fallback constants).
    # ---------------------------------------------------------
    _LAST_KNOWN_PRICES_FILE = resolve_data_path("last_known_prices.json")
    _LAST_KNOWN_PRICE_MAX_AGE_DAYS = 7

    # ---------------------------------------------------------
    # Persisted order-selection defaults. The per-token strategy and
    # sell-mode radio choices survive restarts so the next session's
    # buys (and residual-leg keys) keep using what was selected.
    # ---------------------------------------------------------
    _ORDER_SELECTIONS_FILE = resolve_data_path("order_selections.json")

    # Standard PaperTrading.txt columns. Shared by write_paper_trade()
    # (writer) and fetch_paper_trades() (reader) so the two can never
    # drift apart again.
    _PAPER_TRADE_HEADERS = [
        "Time", "Type", "Instrument", "Token", "Product",
        "Qty.", "Avg. price", "Status", "Duration",
        "PnL Rate", "PnL %", "PnL", "Strategy", "SellType"
    ]

    # Cash used when a live fund-summary fetch fails, so lot sizing does
    # not silently run on an arbitrary in-code number. Fetched per use
    # (see _refresh_fund_summary_after_position_update) so config edits
    # apply without a restart.

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Trader_Singleton, cls).__new__(cls)
            cls._load_order_selections()
        return cls._instance

    def set_broker(self, broker):
        self._broker = broker
        # The broker label shown by the UI must reflect the ADAPTER that
        # actually runs (SIMULATION -> SIMULATOR, PLAYBACK -> PLAYBACK),
        # not the raw appconfig BROKER value the class attribute carries.
        self._broker_string = broker.__class__.__name__.replace("Adapter", "").upper()


    def get_price_with_fallback(self, token, symbol):

        price = self.get_latest_price(token)

        if price:
            return price

        try:
            price = self._broker.get_quote(symbol)[0]
            return price
        except:
            return 0        

    def wait_for_prices(self, tokens, timeout=2.0):
        """
        Wait briefly for websocket ticks to arrive before falling back to REST.
        Returns dictionary {token: price}

        Polls every 50ms and exits as soon as every requested token has a
        price, so a generous timeout costs nothing when ticks arrive fast
        while still giving slow first ticks (typically 0.3-1.5s after
        subscribe) a real chance before the REST fallback kicks in.
        """

        start = time.time()
        prices = {}
        remaining = set(str(token) for token in tokens)

        while remaining and time.time() - start < timeout:
            for token in list(remaining):
                price = self.get_latest_price(token)
                if price:
                    prices[token] = price
                    remaining.discard(token)

            if not remaining:
                break

            time.sleep(0.05)

        return prices
    
    def _get_position_key(self, token_or_symbol, is_token=True):
        """Resolve an instrument token from either a leg key, a token or a trading symbol."""
        raw = str(token_or_symbol)

        # A composite leg key ("token:STRATEGY:MODE") carries its token
        # as the prefix before the first colon.
        if is_token and ":" in raw:
            return raw.split(":", 1)[0]

        if is_token:
            return raw

        for key, data in self._position_data.items():
            if data.get("tradingsymbol") == token_or_symbol:
                return str(key).split(":", 1)[0]
        return None

    @staticmethod
    def _normalise_strategy(value):
        """
        Canonical strategy label for position legs and the radio grid.

        The buy widget sends the raw radio value ('UltraScalping'), while
        the grid matches 'ULTRA_SCALPING' (with underscore). Anything
        unrecognised falls back to ULTRA_SCALPING, matching the default
        used everywhere a strategy lookup misses.
        """
        text = str(
            value or ""
        ).strip().upper().replace("-", "_").replace(" ", "_")

        if text == "ULTRA_SCALPING" or text == "ULTRASCALPING":
            return "ULTRA_SCALPING"
        if text == "SCALPING":
            return "SCALPING"
        if text == "INTRA":
            return "INTRA"
        return "ULTRA_SCALPING"

    def _position_key(self, token, strategy, sell_mode):
        """Composite identity for a position leg: token:STRATEGY:MODE.

        Same token+strategy+sellmode buys merge into one leg; a different
        strategy or sell type opens a separate leg (and a separate grid row).
        """
        return (
            f"{str(token)}:"
            f"{self._normalise_strategy(strategy)}:"
            f"{str(sell_mode or 'T').strip().upper()}"
        )

    def _legs_for_token(self, token):
        """All leg keys in _position_data that belong to an instrument token."""
        prefix = f"{str(token)}:"
        return [key for key in self._position_data if str(key).startswith(prefix)]

    def _parse_position_key(self, key):
        """Split a composite leg key into (token, strategy, sell_mode)."""
        parts = str(key).split(":")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        return str(key), "ULTRA_SCALPING", "T"

    def _update_leg_pl(self, key):
        """Recalculate P/L fields of one leg from its average_price/latest_price."""
        position = self._position_data.get(key)
        if not position:
            return

        buy_price = position.get('average_price', 0)
        latest = position.get('latest_price', 0)
        net_qty = position.get('net_quantity', 0)
        lotsize = position.get('lotsize', 0)

        try:
            buy_price = float(buy_price or 0)
            latest = float(latest or 0)
        except (TypeError, ValueError):
            return

        if buy_price <= 0:
            return

        pts_pl = self.calculate_point_difference(buy_price, latest)
        pct_pl = self.calculate_pctg_difference(buy_price, latest)
        position['pts_pl'] = pts_pl
        position['pct_pl'] = pct_pl
        position['total_pl'] = pts_pl * net_qty * lotsize
    
    def _calculate_option_lots(self, price: float, lot_size: Any) -> int:
        """Return a no-zero lot count for option rows, even when broker cash balance is unavailable."""
        try:
            safe_lot_size = int(lot_size)
        except (TypeError, ValueError):
            safe_lot_size = 1

        if price <= 0 or safe_lot_size <= 0:
            return 1

        # Fetched per call so MARGIN_USAGE_PCT edits apply without a restart.
        margin_pct = float(fetch_from_json("appconfig.json", "MARGIN_USAGE_PCT") or 1)

        # PAPER mode always sizes from the configured paper trading
        # cash balance, independent of the live broker's funds. Realized
        # PnL from closed paper trades is credited and cash already
        # committed to open paper positions is subtracted so the paper
        # account cannot over-allocate.
        if self._mode == "PAPER":
            available = self._get_paper_available_cash()
            if available <= 0:
                return 0
            # Buy orders are priced with a market-protection buffer on
            # top of LTP (LIMIT at LTP + buffer on Kite, slippage
            # allowance for MARKET fills on M.Stock), so size
            # affordability against the buffered price, not the LTP.
            return int(((available * margin_pct) / (price + compute_limit_margin(price))) / safe_lot_size)

        cash_balance = float(self._fund_summary.get("cash_balance", 0) or 0)

        if cash_balance <= 0:
            logger.warning(
                "Zero/invalid cash balance while calculating option lots; falling back to 1 lot. "
                f"Price={price}, lot_size={safe_lot_size}, margin_pct={margin_pct}"
            )
            return 1

        # Buy orders are priced with a market-protection buffer on top
        # of LTP (LIMIT at LTP + buffer on Kite, slippage allowance for
        # MARKET fills on M.Stock), so size affordability against the
        # buffered price, not the LTP.
        return int(((cash_balance * margin_pct) / (price + compute_limit_margin(price))) / safe_lot_size)
        # We dont want to return 1 lot if the computed_lots is 0. This is because we want to avoid placing orders when the cash balance is low and the computed lots is 0. Hence we will return 0 in such cases.
        #return max(1, computed_lots) 


    def _should_log_with_frequency(self, channel: str = "default") -> bool:
        # Fetched per call so POSITION_PNL_LOG_FREQ edits apply without a restart.
        freq_raw = str(fetch_from_json("appconfig.json", "POSITION_PNL_LOG_FREQ") or "TICK").strip().upper()

        if freq_raw == "TICK":
            return True

        try:
            interval_seconds = int(float(freq_raw))
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid POSITION_PNL_LOG_FREQ '{freq_raw}'. Falling back to TICK logging."
            )
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

    def weekly_options_initialize(self , token , data , call_or_put , price , level , prev_close, strike=None):
        
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
        logger.debug(f" Cash Balance {self._fund_summary.get('cash_balance', 0)} and Cash Balance Margin Pct {fetch_from_json('appconfig.json', 'MARGIN_USAGE_PCT')} and Price {price} and Lot Size {data['lot_size']}")
        self._five_weekly_option_contracts[token]["lots"] = self._calculate_option_lots(price, data["lot_size"])
        logger.debug(f"Calculated lots is   {self._five_weekly_option_contracts[token]['lots']}")
        self._five_weekly_option_contracts[token]["call_or_put"] = call_or_put
        self._five_weekly_option_contracts[token]["level"] = level
        self._five_weekly_option_contracts[token]["prev_close"] = prev_close
        # Authoritative strike from the WOC window so the frontend grid
        # never has to parse it out of the tradingsymbol.
        self._five_weekly_option_contracts[token]["strike"] = strike

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

       

    
    def _target_profit_points(self, strategy_type):
        """Strategy-scaled book-profit target in points (strategy factor x PTS_PROFIT)."""
        pnt_book_profit = float(fetch_from_json("appconfig.json", "PTS_PROFIT") or 0)
        strategy_type = self._normalise_strategy(strategy_type)
        if strategy_type == "INTRA":
            return float(fetch_from_json("appconfig.json", "PTS_PROFIT_INTRA_FACTOR") or 1) * pnt_book_profit
        if strategy_type == "SCALPING":
            return float(fetch_from_json("appconfig.json", "PTS_PROFIT_SCALPING_FACTOR") or 1) * pnt_book_profit
        if strategy_type == "ULTRA_SCALPING":
            return float(fetch_from_json("appconfig.json", "PTS_PROFIT_ULTRA_SCALPING_FACTOR") or 1) * pnt_book_profit
        return 0.0

    def _effective_book_profit_pct(self, strategy_type):
        """Strategy-scaled book-profit threshold in percent (PCT_BOOK_PROFIT x strategy factor)."""
        pct_book_profit = float(fetch_from_json("appconfig.json", "PCT_BOOK_PROFIT") or 0)
        strategy_type = self._normalise_strategy(strategy_type)
        if strategy_type == "INTRA":
            factor = float(fetch_from_json("appconfig.json", "PCT_PROFIT_INTRA_FACTOR") or 1)
        elif strategy_type == "SCALPING":
            factor = float(fetch_from_json("appconfig.json", "PCT_PROFIT_SCALPING_FACTOR") or 1)
        elif strategy_type == "ULTRA_SCALPING":
            factor = float(fetch_from_json("appconfig.json", "PCT_PROFIT_ULTRA_SCALPING_FACTOR") or 1)
        else:
            factor = 1
        return pct_book_profit * factor

    def _default_override_book_profit_pct(self, strategy_type):
        """
        Per-leg BK% seed, comparison-function aware: PCT mode seeds the
        strategy-scaled percent; PNT mode seeds None so the visible
        PTS_* point thresholds govern the leg unless the user types an
        explicit percent override in the BK% box.
        """
        comparison_function = fetch_from_json("appconfig.json", "COMPARISON_FUNCTION") or []
        if "PNT" in comparison_function:
            return None
        return self._effective_book_profit_pct(strategy_type)

    def _cancel_pending_exit_orders(self, tradingsymbol):
        """
        Cancel resting SELL exit orders for an instrument.

        U/D legs keep a broker-side exit limit from buy time. When the
        SL safety net (or a manual sell) exits the position, that limit
        must be cancelled first - otherwise a later fill would leave
        the account net short. Returns the number of cancelled orders.
        """
        try:
            pending = self._broker.fetch_all_pending_orders() or []
        except Exception as e:
            logger.error(
                f"Could not fetch pending orders to cancel the exit for "
                f"{tradingsymbol}: {e}"
            )
            return 0

        symbol_upper = str(tradingsymbol).upper()
        cancelled = 0
        for order in pending:
            if str(order.get("tradingsymbol", "") or "").upper() != symbol_upper:
                continue
            if str(order.get("transaction_type", "") or "").upper() != "SELL":
                continue
            order_id = order.get("order_id")
            if not order_id:
                continue
            try:
                if self._broker.cancel_order(order_id):
                    cancelled += 1
                    logger.info(
                        f"Cancelled pending exit order {order_id} "
                        f"for {tradingsymbol}"
                    )
                else:
                    logger.warning(
                        f"Broker did not confirm the cancel of pending "
                        f"exit order {order_id} for {tradingsymbol}"
                    )
            except Exception as e:
                logger.error(
                    f"Failed to cancel pending exit order {order_id} "
                    f"for {tradingsymbol}: {e}"
                )

        if cancelled:
            logger.info(
                f"Cancelled {cancelled} pending exit order(s) "
                f"for {tradingsymbol}"
            )
        return cancelled

    def position_data_update(self, token_or_symbol, attribute_name, updated_value, is_token=True):
        """Update an attribute on position leg(s).

        token_or_symbol may be:
          - a composite leg key ("token:STRATEGY:MODE") - that leg only
          - an instrument token - every leg of that instrument (ticks)
          - a tradingsymbol (is_token=False) - resolved to a token first

        Missing legs are NOT created implicitly; an update with no open
        leg is a no-op (logged) so stale UI calls cannot seed empty rows.
        """
        raw = str(token_or_symbol)

        if is_token and ":" in raw:
            # Exact composite leg key (position-grid radio updates).
            if raw not in self._position_data:
                logger.warning(
                    f"Position leg {raw} not found. Skipping {attribute_name} update."
                )
                return
            keys = [raw]
        else:
            token = self._get_position_key(token_or_symbol, is_token)
            if token is None:
                logger.warning(f"Could not find position for {token_or_symbol}. Skipping update.")
                return
            keys = self._legs_for_token(token)
            if not keys:
                logger.warning(
                    f"No open legs for token {token}. Skipping {attribute_name} update."
                )
                return

        for key in keys:
            position = self._position_data.setdefault(key, {})
            position[attribute_name] = updated_value

            if attribute_name == 'latest_price':
                self._update_leg_pl(key)
                if self._should_log_position_pnl():
                    logger.debug(
                        f" For {key} : Buy Price {position.get('average_price')} , "
                        f"LTP {updated_value} , PTS : {position.get('pts_pl')}, "
                        f"PCT {position.get('pct_pl'):.2f}% and "
                        f"Total P/L {position.get('total_pl')}. Position Grid Updated."
                    )

    def update_fund_summary(self, fund_summary: Dict[str, Any]):
        logger.info("Updating fund summary...")
        for key , value in fund_summary.items():
            self._fund_summary[key] = float(value)
        logger.debug(f"Fund Summary updated to {self._fund_summary}")

#     def refresh_open_pos_buy_price(self):
#         logger.info("Refreshing open positions and buy prices...")
#         # self._broker.unsubscribe_from_all(self.get_relevant_instruments_to_track())
        
#         #How could we avoid this call here..as this results in multiple fund summary calls
#         logger.info("Fetching latest fund summary from broker...")
#         try:
#             fund_summary = self._broker.fetch_fund_summary()

#             if not fund_summary or not isinstance(fund_summary, dict):
#                 raise ValueError("Fund summary returned None or invalid data")
            
#             for key, value in fund_summary.items():
#                 self._fund_summary[key] = float(value)

#             logger.debug(f"Fund Summary fetched is {self._fund_summary}")
#         except Exception as e:
#             logger.error(f"Fund Summary fetch failed. Using fallback cash balance. Error: {e}", exc_info=True)
#             # ✅ Fallback values
#             self._fund_summary = {}
#             self._fund_summary["cash_balance"] = 10000.0

#             logger.warning("Fallback cash balance of 10000 assigned.")

#         positions_from_broker = self._broker.fetch_all_positions()

#         if isinstance(positions_from_broker, dict):
#             positions_from_broker = positions_from_broker.get("net", [])

#         if not positions_from_broker:
#             logger.warning("positions_from_broker is None or empty — skipping avg price calculation")
#             self._position_data.clear()
#             try:
#                 if not self.frontend_data_socket:
#                     raise ConnectionRefusedError
#                 self.frontend_data_socket.emit('update_open_positions', self._position_data)
#                 self.frontend_data_socket.emit('update_weekly_options', self._five_weekly_option_contracts)
#                 self.frontend_data_socket.emit('status_message', {"success": True, "message": "Refreshed position ..."})
#             except Exception:
#                 logger.error("Frontend socket yet to be instantiated")
#             logger.info(f"Updated positions: {len(self._position_data)}")
#             return
#         #  MSTOCK_SENSEX

#         # =========================================================
#         # FAST POSITION GRID UPDATE
#         # M.Stock SENSEX only.
#         # Show the position immediately after the broker confirms
#         # the position, without waiting for the orders REST call.
#         # The existing accurate order-based calculation below will
#         # reconcile the average price immediately afterwards.
#         # =========================================================
#         if self._broker_string == "MSTOCK" and self._underlying == "SENSEX":
#             logger.info("M.Stock SENSEX: Performing fast position grid update before orders fetch...")

#             for position in positions_from_broker:
#                 token = str(position["instrument_token"])

#                 avg_price = float(position.get("average_price", 0) or 0)
#                 net_qty = int(position.get("quantity", 0) or 0)
#                 lotsize = int(position.get("lotsize", 1) or 1)

#                 pts_pl = self.calculate_point_difference(avg_price, avg_price)
#                 pct_pl = self.calculate_pctg_difference(avg_price, avg_price)

#                 self._position_data[token] = {
#                     "tradingsymbol": position["tradingsymbol"],
#                     "average_price": avg_price,
#                     "net_quantity": net_qty,
#                     "latest_price": avg_price,
#                     "instrument_token": position["instrument_token"],
#                     "exchange": position["exchange"],
#                     "lotsize": lotsize,
#                     "lots": net_qty,
#                     "pts_pl": pts_pl,
#                     "pct_pl": pct_pl,
#                     "total_pl": pts_pl * net_qty,
#                     "order_strategy": self._order_strategy_mapping.get(
#                         position["instrument_token"],
#                         "ULTRA_SCALPING"
#                     )
#                 }

#             try:
#                 self.frontend_data_socket.emit(
#                     'update_open_positions',
#                     self._position_data
#                 )
#                 logger.info(
#                     f"M.Stock SENSEX fast position grid update sent: "
#                     f"{len(self._position_data)} position(s)"
#                 )
#             except Exception:
#                 logger.error("Frontend socket yet to be instantiated")

#         # Existing accurate order-based reconciliation        
#         orders_from_broker = self._broker.fetch_all_orders()

#         if not orders_from_broker:
#             logger.info("Broker returned None for orders. Using empty list.")
#             orders_from_broker = []
#         logger.debug(f"positions_from_broker is {positions_from_broker}")
#         positions = calculate_accurate_average_buy_price_and_update_positions(positions_from_broker , orders_from_broker)
#         logger.debug(f"positions after calculating average buy price is  {positions}")

#         if not positions_from_broker:
#             logger.info("Broker returned None for positions. Using empty list.")
#             positions_from_broker = []

#         new_position_tokens = {str(pos["instrument_token"]) for pos in positions}
        
#         tokens_to_remove = [
#             token for token in self._position_data 
#             if token not in new_position_tokens
#         ]
#         for token in tokens_to_remove:
#             del self._position_data[token]
            
#         for position in positions: # Assuming this is the accurately calculated 'positions' list
#             token = str(position["instrument_token"])

#             logger.debug(f"POS CHECK {position}")
            
#             if token not in self._position_data:
#                 self._position_data[token] = {}

#             # --- MODIFIED BLOCK ---
#             avg_price = position["average_price"]
#             net_qty = int(position["quantity"])
#             lotsize = int(position["lotsize"])

#             # Calculate initial P/L values (they will be 0)
#             pts_pl = self.calculate_point_difference(avg_price, avg_price)
#             pct_pl = self.calculate_pctg_difference(avg_price, avg_price)
#             total_pl = pts_pl * net_qty

#             self._position_data[token].update({
#                 "tradingsymbol": position["tradingsymbol"],
#                 "average_price": avg_price,
#                 "net_quantity": net_qty, 
#                 "latest_price": avg_price, # Initial latest_price is the buy price
#                 "instrument_token": position["instrument_token"],
#                 "exchange" : position["exchange"],
#                 "lotsize" : lotsize,
#                 # "lots": int(net_qty / lotsize), MSTOCK COUPLED CODE 
#                 "lots" : net_qty,
#                 # ADDED: Initial P/L fields
#                 "pts_pl": pts_pl,
#                 "pct_pl": pct_pl,
#                 "total_pl": total_pl,
#                 "order_strategy" : self._order_strategy_mapping.get(position["instrument_token"], "ULTRA_SCALPING")
#             })
#             # --- END MODIFIED BLOCK ---

            
#         # self._broker.subscribe_to_all(self.get_relevant_instruments_to_track())
#         try:
#             if not self.frontend_data_socket:
#                 raise ConnectionRefusedError
#             self.frontend_data_socket.emit(
#                     'update_open_positions',
#                     self._position_data
#                 )
#             #PUTTA, why are we emitting socket even though there was no change done to _five_weekly_option_contracts?
#             #How should we recalculate the lot size post the buy transaction with the the latest fund summary data?
#             #How was it (recalculated lot size for five_weekly_options post the buy/sell transactions) working early? 
#             #Can we compare this version of the file with the your latest git version
#             self.frontend_data_socket.emit(
#                     'update_weekly_options',
#                     self._five_weekly_option_contracts
#                 )
#             self.frontend_data_socket.emit('status_message',{"success": True, "message": "Refreshed position ..."}
# )
#         except Exception as e:
#             logger.error("Frontend socket yet to be instantiated")
#         logger.info(f"Updated positions: {len(self._position_data)}")
#         logger.debug(self._position_data)

    def _refresh_fund_summary_after_position_update(self, prefetched=None):
        """
        Fetch the latest fund summary after the position grid has
        already been refreshed.

        Fund summary is secondary to position/P&L visibility.

        `prefetched` may carry a fund summary that was fetched
        concurrently (e.g. by refresh_open_pos_buy_price); when it
        is provided no extra REST call is made.
        """
        logger.info(
            "Fetching latest fund summary after position update..."
        )

        try:
            if prefetched is not None:
                fund_summary = prefetched
            else:
                fund_summary = self._broker.fetch_fund_summary()

            if not fund_summary or not isinstance(fund_summary, dict):
                raise ValueError(
                    "Fund summary returned None or invalid data"
                )

            for key, value in fund_summary.items():
                self._fund_summary[key] = float(value)

            logger.debug(
                f"Fund Summary fetched is {self._fund_summary}"
            )

            # Recalculate weekly-option buying capacity using the
            # newly updated fund summary.
            for token, contract in self._five_weekly_option_contracts.items():
                contract_ltp = (
                    contract.get("ltp")
                    or contract.get("price")
                    or 0
                )
                contract_lot_size = (
                    contract.get("lotsize")
                    or contract.get("lot_size")
                    or 1
                )
                if contract_ltp and contract_ltp > 0:
                    contract["lots"] = self._calculate_option_lots(
                        contract_ltp,
                        contract_lot_size
                    )

            if self.frontend_data_socket:
                self.frontend_data_socket.emit(
                    'update_weekly_options',
                    self._five_weekly_option_contracts
                )

                self.frontend_data_socket.emit('refresh_fund_summary')

        except Exception as e:
            logger.error(
                f"Fund Summary fetch failed. "
                f"Using fallback cash balance. Error: {e}",
                exc_info=True
            )

            self._fund_summary = {}
            self._fund_summary["cash_balance"] = float(
                fetch_from_json("appconfig.json", "FALLBACK_CASH_BALANCE") or 10000
            )

            logger.warning(
                f"Fund summary unavailable. Using configured fallback "
                f"cash balance of {self._fund_summary['cash_balance']} for lot "
                f"sizing. Verify broker connectivity."
            )

    def fast_update_position_after_buy(
    self,
    trading_symbol,
    instrument_token,
    quantity,
    lotsize,
    executed_buy_price,
    exchange,
    strategy="",
    sell_mode="T"
    ):
        """
        Immediately create/update the position grid after a successful BUY.

        This is a TEMPORARY display update only.
        The authoritative BUY price will still be calculated later by
        refresh_open_pos_buy_price() using the broker positions + orders.

        The temporary BUY price shown in the grid is the current live LTP.

        Positions are per-leg (token:STRATEGY:MODE): a BUY with a different
        strategy or sell type opens a separate leg instead of merging.
        """

        try:
            token = str(instrument_token)

            # ---------------------------------------------------------
            # 0. Resolve the leg identity for this BUY.
            # ---------------------------------------------------------
            strategy = self._normalise_strategy(
                strategy
                or self._order_strategy_mapping.get(token, "ULTRA_SCALPING")
            )
            sell_mode = str(
                sell_mode
                or self._order_sell_mode_mapping.get(token, "T")
            ).strip().upper()
            leg_key = self._position_key(token, strategy, sell_mode)

            # ---------------------------------------------------------
            # 1. Get the latest LTP already available in memory.
            # ---------------------------------------------------------
            latest_ltp = self.get_latest_price(token)

            # If websocket LTP is not available, use executed BUY price
            # so that the grid can still be populated immediately.
            if not latest_ltp:
                latest_ltp = float(executed_buy_price)

            latest_ltp = float(latest_ltp)
            quantity = int(quantity)
            lotsize = int(lotsize)

            # ---------------------------------------------------------
            # 2. If the same leg already exists, add this BUY to it.
            # ---------------------------------------------------------
            existing = self._position_data.get(leg_key)

            if existing:

                old_qty = int(
                    existing.get("net_quantity", 0) or 0
                )

                old_avg = float(
                    existing.get("average_price", 0) or 0
                )

                new_qty = old_qty + quantity

                if new_qty > 0:
                    weighted_avg = (
                        (old_avg * old_qty)
                        + (latest_ltp * quantity)
                    ) / new_qty
                else:
                    weighted_avg = latest_ltp

                net_qty = new_qty
                avg_price = weighted_avg

            else:

                net_qty = quantity
                avg_price = latest_ltp

            # ---------------------------------------------------------
            # 3. Calculate temporary P/L using current LTP.
            # ---------------------------------------------------------
            pts_pl = self.calculate_point_difference(
                avg_price,
                latest_ltp
            )

            pct_pl = self.calculate_pctg_difference(
                avg_price,
                latest_ltp
            )

            total_pl = pts_pl * net_qty * lotsize

            # ---------------------------------------------------------
            # 4. Update the leg's position structure.
            #
            # IMPORTANT:
            # No buy_price_status / status column is added.
            # ---------------------------------------------------------
            self._position_data[leg_key] = {
                "tradingsymbol": trading_symbol,
                "average_price": avg_price,
                "net_quantity": net_qty,
                "latest_price": latest_ltp,
                "instrument_token": instrument_token,
                "exchange": exchange,
                "lotsize": lotsize,
                "lots": net_qty,
                "pts_pl": pts_pl,
                "pct_pl": pct_pl,
                "total_pl": total_pl,
                "order_strategy": strategy,
                "sell_mode": sell_mode,
                "override_book_profit_pct": self._default_override_book_profit_pct(strategy)
            }

            # ---------------------------------------------------------
            # 5. Immediately send the position to the grid.
            # ---------------------------------------------------------
            if self.frontend_data_socket:
                self.frontend_data_socket.emit(
                    'update_open_positions',
                    self._position_data
                )

                self.frontend_data_socket.emit(
                    'temporary_buy_price',
                    {
                        'token': str(instrument_token),
                        'position_key': leg_key
                    }
                )

            logger.info(
                f"TEMP BUY GRID UPDATE: "
                f"{trading_symbol} [{leg_key}] | "
                f"Qty={net_qty} | "
                f"Temporary Buy={avg_price:.2f} | "
                f"LTP={latest_ltp:.2f}"
            )

        except Exception as e:
            logger.error(
                f"Temporary BUY position grid update failed: {e}",
                exc_info=True
            )





    def temporary_update_position_after_buy(
        self,
        trading_symbol,
        instrument_token,
        quantity,
        exchange,
        ltp,
        lotsize=1,
        strategy="",
        sell_mode="T"
    ):
        """
        Immediately show a newly executed BUY in the position grid.

        IMPORTANT:
        - This is only a temporary/provisional grid update.
        - The existing refresh_open_pos_buy_price() remains
          responsible for the authoritative BUY price.
        - This method is intentionally broker/underlying neutral.
        - It does NOT call the broker.
        - It does NOT change the existing refresh workflow.

        Positions are per-leg (token:STRATEGY:MODE): a BUY with a different
        strategy or sell type opens a separate leg instead of merging.
        """

        try:
            token = str(instrument_token)
            quantity = int(quantity)
            lotsize = int(lotsize or 1)
            temporary_buy_price = float(ltp)

            if quantity <= 0:
                logger.warning(
                    f"TEMP BUY GRID skipped: invalid quantity={quantity} "
                    f"for {trading_symbol}"
                )
                return

            if temporary_buy_price <= 0:
                logger.warning(
                    f"TEMP BUY GRID skipped: invalid LTP={ltp} "
                    f"for {trading_symbol}"
                )
                return

            # ---------------------------------------------------------
            # Resolve the leg identity for this BUY.
            # ---------------------------------------------------------
            strategy = self._normalise_strategy(
                strategy
                or self._order_strategy_mapping.get(token, "ULTRA_SCALPING")
            )
            sell_mode = str(
                sell_mode
                or self._order_sell_mode_mapping.get(token, "T")
            ).strip().upper()
            leg_key = self._position_key(token, strategy, sell_mode)

            # ---------------------------------------------------------
            # If this same leg already exists, preserve it and add the
            # newly bought quantity.
            # ---------------------------------------------------------
            existing = self._position_data.get(leg_key)

            if existing:
                old_qty = int(existing.get("net_quantity", 0) or 0)

                # The existing BUY price may itself be temporary or
                # already authoritative. For the immediate display,
                # calculate a provisional weighted average.
                old_avg = float(
                    existing.get("average_price", 0) or 0
                )

                new_qty = old_qty + quantity

                if new_qty > 0 and old_avg > 0:
                    temporary_avg_price = (
                        (old_avg * old_qty)
                        + (temporary_buy_price * quantity)
                    ) / new_qty
                else:
                    temporary_avg_price = temporary_buy_price

                net_qty = new_qty

            else:
                # Brand-new leg.
                temporary_avg_price = temporary_buy_price
                net_qty = quantity

            # ---------------------------------------------------------
            # Temporary P&L is calculated against the current LTP.
            # Since BUY price is initially the same LTP, P&L starts at 0.
            # ---------------------------------------------------------
            pts_pl = self.calculate_point_difference(
                temporary_avg_price,
                temporary_buy_price
            )

            pct_pl = self.calculate_pctg_difference(
                temporary_avg_price,
                temporary_buy_price
            )

            total_pl = pts_pl * net_qty

            # ---------------------------------------------------------
            # Store the provisional leg.
            #
            # buy_price_status is the important new field.
            # Frontend will later use this to show the BUY Price cell
            # in yellow.
            # ---------------------------------------------------------
            self._position_data[leg_key] = {
                "tradingsymbol": trading_symbol,
                "average_price": temporary_avg_price,
                "net_quantity": net_qty,
                "latest_price": temporary_buy_price,
                "instrument_token": instrument_token,
                "exchange": exchange,
                "lotsize": lotsize,
                "lots": net_qty,
                "pts_pl": pts_pl,
                "pct_pl": pct_pl,
                "total_pl": total_pl,
                "order_strategy": strategy,
                "sell_mode": sell_mode,
                "buy_price_status": "TEMPORARY",
                "override_book_profit_pct": self._effective_book_profit_pct(strategy)
            }

            # ---------------------------------------------------------
            # Immediately send the provisional position to the grid.
            # ---------------------------------------------------------
            if self.frontend_data_socket:
                self.frontend_data_socket.emit(
                    'update_open_positions',
                    self._position_data
                )

            logger.info(
                f"TEMP BUY GRID: "
                f"{trading_symbol} [{leg_key}] | "
                f"Qty={net_qty} | "
                f"Temporary Buy={temporary_avg_price:.2f} | "
                f"LTP={temporary_buy_price:.2f}"
            )

        except Exception as e:
            # Never allow the temporary UI update to break the BUY flow.
            logger.error(
                f"Temporary BUY position grid update failed: {e}",
                exc_info=True
            )




    def refresh_open_pos_buy_price(self):
        logger.info("Refreshing open positions and buy prices...")
        logger.info(
            f"REFRESH_OPEN_POS_BUY_PRICE START | "
            f"time={time.time():.3f}"
        )

        # =========================================================
        # PAPER GUARD
        #
        # The paper position grid comes from the paper trade log, NOT
        # from live broker REST calls; the broker path below would
        # overwrite/clear it (e.g. via /manual-refresh-positions or any
        # adapter call site that bypasses the mode checks in on_start()
        # and refresh_table()). The mode is re-read fresh because this
        # is a destructive operation and MODE can flip at runtime.
        # =========================================================
        if str(fetch_from_json("appconfig.json", "MODE") or "").upper() == "PAPER":
            self.refresh_paper_positions()
            return

        # =========================================================
        # 0. PREFETCH FUND SUMMARY CONCURRENTLY
        #
        # The fund summary REST call runs in parallel with the
        # positions call so startup does not pay both latencies
        # sequentially. The thread is joined right after the
        # positions fetch; on failure the result stays None and
        # _refresh_fund_summary_after_position_update fetches it
        # directly as before.
        # =========================================================
        fund_prefetch = {"result": None}

        def _prefetch_fund_summary():
            try:
                fund_prefetch["result"] = self._broker.fetch_fund_summary()
            except Exception as e:
                logger.error(f"Fund summary prefetch failed: {e}")

        fund_prefetch_thread = threading.Thread(
            target=_prefetch_fund_summary,
            daemon=True
        )
        fund_prefetch_thread.start()

        # =========================================================
        # 1. FETCH POSITIONS FIRST
        # =========================================================
        positions_from_broker = self._broker.fetch_all_positions()

        fund_prefetch_thread.join(timeout=10)
        fund_summary_prefetched = fund_prefetch["result"]

        if isinstance(positions_from_broker, dict):
            positions_from_broker = positions_from_broker.get("net", [])

        if not positions_from_broker:
            logger.warning(
                "positions_from_broker is None or empty — skipping avg price calculation"
            )

            self._position_data.clear()

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

                self.frontend_data_socket.emit(
                    'status_message',
                    {"success": True, "message": "Refreshed position ..."}
                )

            except Exception:
                logger.error("Frontend socket yet to be instantiated")

            logger.info(f"Updated positions: {len(self._position_data)}")

            # Fund summary is refreshed after the position grid.
            self._refresh_fund_summary_after_position_update(
                prefetched=fund_summary_prefetched
            )

            return

        # =========================================================
        # 2. FETCH ORDERS
        #
        # IMPORTANT:
        # Do NOT send an intermediate position-grid update here.
        #
        # For M.Stock SENSEX, fast_update_position_after_buy()
        # already puts the BUY into the grid immediately.
        #
        # The broker position API can temporarily return a stale
        # average price immediately after a BUY. Sending that value
        # here would overwrite the correct fast BUY value.
        # =========================================================
        orders_from_broker = self._broker.fetch_all_orders()

        if not orders_from_broker:
            logger.info(
                "Broker returned None for orders. Using empty list."
            )
            orders_from_broker = []

        logger.debug(
            f"positions_from_broker is {positions_from_broker}"
        )

        # =========================================================
        # 3. ACCURATE ORDER-BASED RECONCILIATION
        # =========================================================
        positions = calculate_accurate_average_buy_price_and_update_positions(
            positions_from_broker,
            orders_from_broker
        )

        logger.debug(
            f"positions after calculating average buy price is {positions}"
        )

        if not positions_from_broker:
            logger.info(
                "Broker returned None for positions. Using empty list."
            )
            positions_from_broker = []

        # =========================================================
        # 4. RECONCILE THE STRATEGY-LEG LEDGER WITH BROKER NET
        #
        # The broker reports one NET position per tradingsymbol and
        # cannot see strategy/sell-type legs. The internal ledger
        # (keyed token:STRATEGY:MODE) stays the source of per-leg
        # identity and average prices; the broker net is used to:
        #   - drop every leg of tokens the broker no longer holds
        #   - FIFO-reduce legs (oldest first) when the net shrank
        #   - top up a default leg when the net grew (external buys)
        # =========================================================
        broker_tokens = {
            str(pos["instrument_token"]) for pos in positions
        }

        stale_keys = [
            key for key in self._position_data
            if self._parse_position_key(key)[0] not in broker_tokens
        ]

        for key in stale_keys:
            del self._position_data[key]

        for position in positions:
            token = str(position["instrument_token"])

            logger.debug(f"POS CHECK {position}")

            avg_price = position["average_price"]
            net_qty = int(position["quantity"])
            lotsize = int(position["lotsize"])

            leg_keys = self._legs_for_token(token)
            held_qty = sum(
                int(self._position_data[k].get("net_quantity", 0) or 0)
                for k in leg_keys
            )

            if held_qty > net_qty:
                # FIFO reduction: shrink the oldest legs first.
                excess = held_qty - net_qty
                for key in leg_keys:
                    if excess <= 0:
                        break
                    leg = self._position_data[key]
                    leg_qty = int(leg.get("net_quantity", 0) or 0)
                    if leg_qty <= excess:
                        del self._position_data[key]
                        excess -= leg_qty
                    else:
                        leg["net_quantity"] = leg_qty - excess
                        leg["lots"] = leg["net_quantity"]
                        excess = 0
                leg_keys = self._legs_for_token(token)
                held_qty = sum(
                    int(self._position_data[k].get("net_quantity", 0) or 0)
                    for k in leg_keys
                )

            residual_qty = net_qty - held_qty

            if residual_qty > 0:
                # Broker holds more than the ledger knows (external buy
                # or first refresh after startup). Book the residual on
                # the leg matching the strategy/sell type selected at
                # buy time, using the accurate netted average price of
                # the whole broker position.
                default_strategy = str(
                    self._order_strategy_mapping.get(token, "ULTRA_SCALPING")
                ).upper()
                default_sell_mode = str(
                    self._order_sell_mode_mapping.get(token, "T")
                ).upper()
                default_key = self._position_key(
                    token, default_strategy, default_sell_mode
                )

                held_value = sum(
                    float(self._position_data[k].get("average_price", 0) or 0)
                    * int(self._position_data[k].get("net_quantity", 0) or 0)
                    for k in leg_keys
                )
                residual_value = float(avg_price) * net_qty - held_value
                residual_avg = (
                    residual_value / residual_qty
                    if residual_value > 0
                    else float(avg_price)
                )

                leg = self._position_data.setdefault(default_key, {})
                old_qty = int(leg.get("net_quantity", 0) or 0)
                old_avg = float(leg.get("average_price", 0) or 0)
                new_qty = old_qty + residual_qty
                leg["average_price"] = (
                    ((old_avg * old_qty) + (residual_avg * residual_qty)) / new_qty
                    if new_qty > 0
                    else residual_avg
                )
                leg["net_quantity"] = new_qty
                leg_keys = self._legs_for_token(token)

            # Fill in broker-derived attributes on every leg of this
            # token without touching per-leg identity or averages.
            contract = self._five_weekly_option_contracts.get(token)
            for key in leg_keys:
                leg = self._position_data[key]
                leg["tradingsymbol"] = position["tradingsymbol"]
                leg["instrument_token"] = position["instrument_token"]
                leg["exchange"] = position["exchange"]
                leg["lotsize"] = lotsize
                leg["lots"] = int(leg.get("net_quantity", 0) or 0)
                leg["producttype"] = str(
                    position.get("producttype")
                    or leg.get("producttype")
                    or "CARRYFORWARD"
                ).upper()
                if contract and contract.get("level"):
                    leg["level"] = contract["level"]
                if not leg.get("latest_price"):
                    leg["latest_price"] = float(avg_price)
                if not leg.get("order_strategy"):
                    leg["order_strategy"] = self._parse_position_key(key)[1]
                if not leg.get("sell_mode"):
                    leg["sell_mode"] = self._parse_position_key(key)[2]
                # Per-position BK% override: seeded with the strategy
                # default, preserved across refreshes once the user
                # edits it in the grid.
                leg.setdefault(
                    "override_book_profit_pct",
                    self._default_override_book_profit_pct(leg["order_strategy"])
                )
                self._update_leg_pl(key)

        # =========================================================
        # 5. FINAL POSITION GRID UPDATE
        # =========================================================
        try:
            if not self.frontend_data_socket:
                raise ConnectionRefusedError

            self.frontend_data_socket.emit(
                'update_open_positions',
                self._position_data
            )

            # The grid has now been rendered with the authoritative
            # BUY price. Tell the frontend to remove the temporary
            # yellow BUY-price indication.
            for key in self._position_data:
                self.frontend_data_socket.emit(
                    'authoritative_buy_price',
                    {
                        'token': self._parse_position_key(key)[0],
                        'position_key': str(key)
                    }
                )

            self.frontend_data_socket.emit(
                'update_weekly_options',
                self._five_weekly_option_contracts
            )

            self.frontend_data_socket.emit(
                'status_message',
                {
                    "success": True,
                    "message": "Refreshed position ..."
                }
            )

        except Exception:
            logger.error("Frontend socket yet to be instantiated")

        logger.info(
            f"Updated positions: {len(self._position_data)}"
        )

        logger.debug(self._position_data)

        # =========================================================
        # 7. FUND SUMMARY LAST
        # =========================================================
        self._refresh_fund_summary_after_position_update(
            prefetched=fund_summary_prefetched
        )


        

    def get_relevant_instruments_to_track(self):
        logger.info("Compiling list of relevant instruments to track: 1) Positions, 2) 5 Weekly Options and 3) Near Month Future")
        """Returns a list of instrument tokens currently in the _position_data."""
        # Position keys are composite legs ("token:STRATEGY:MODE"); the
        # broker websocket needs the raw instrument tokens only.
        instruments = list({
            self._parse_position_key(key)[0]
            for key in self._position_data
        })
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

        my_generation = self._watcher_generation
        market_closed_logged = False

        while not self._stop_event.is_set() and self._watcher_generation == my_generation:
            # Liveness stamp for the supervisor thread - refreshed every
            # cycle so a stall (not just a crash) is detectable.
            self._watcher_loop_alive_ts = time.time()
            # LIVE only: no point watching when the market is shut -
            # the broker takes no orders. PAPER / SIMULATION / PLAYBACK
            # watch 24/7 so exits can be tested anytime (the sim/playback
            # tick engines also run around the clock).
            if not is_market_open() and self._mode == "LIVE":
                if not market_closed_logged:
                    logger.info("Market closed. Stop-loss watcher idling until market open...")
                    market_closed_logged = True
                self._stop_event.wait(timeout=30)
                continue

            if market_closed_logged:
                logger.info("Market open. Stop-loss watcher is now monitoring positions.")
                market_closed_logged = False

            for token , data in list(self._position_data.items()):
                # One bad row (or one failing decision) must not kill
                # the watcher thread - a silent death here disables the
                # stop-loss for every subsequent position while the UI
                # keeps looking alive. Failures are logged with the
                # traceback and the cycle continues.
                if not isinstance(data, dict):
                    logger.error(f"Skipping malformed position record for {token}: {data}")
                    continue

                try:
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
                    sell_type = data.get("sell_mode" , "T")

                    # Position keys are composite legs; skip malformed or
                    # incomplete entries so one bad row cannot kill the
                    # watcher thread.
                    if not tradingsymbol or not instrument_token or not qty:
                        if should_log_eval:
                            logger.debug(f"Skipping incomplete position record for {token}")
                        continue


                    match = re.search(r'(CE|PE)$', tradingsymbol.upper())
                    contract_type = match.group(1) if match else None

                    if should_log_eval:
                        logger.debug(f"Order Strategy Type set is {order_strategy_type}")

                    # For now just running the check for the Stop Loss..As we would be resorting to book profit undeterministically
                    # where in As soon as Buy Order is raised Sell Order is also raised with Sell Price = LTP + x (Limit Order)
                    # Refactor this change where this could be driven by the appconfig.json parameters
                    # Ultra Scalping is determined by 15s MACD2 Swing Set Up where in we are capturing 1s MACD2  + 5s Stochastic Swing

                    # This trigger check is only applicable when Sell Type = T. For other cases U and D system raises Sell Order immediately after Buy Order is executed

                    #sell = self.to_sell_or_not_to_sell(buy_price , ltp , order_strategy_type,contract_type, should_log_eval)
                    sell = self._watcher_sell_decision(
                        buy_price, ltp, order_strategy_type, contract_type,
                        sell_type, should_log_eval,
                        override_book_profit_pct=data.get("override_book_profit_pct")
                    )

                    if should_log_eval:
                        logger.debug(f"Decision To Sell {tradingsymbol} - {sell}")
                    # Execute Sell Order if these conditions are met
                    # 1. If sell is True and Sell Mode is TRIGGER_SELL and LTP > Buy Price (Book Profit Scenario)
                    # 2. If sell is True and LTP < Buy Price (Stop Loss Scenario). In this case, Sell Mode can be anything. We dont expect TRIGGER_SELL to be set here.


                    if self._mode in ("LIVE", "SIMULATION", "PLAYBACK"):
                        if sell:
                            # U/D legs hold a resting exit limit at the
                            # broker; cancel it first or a later fill would
                            # leave the account net short.
                            if sell_type in ("U", "D"):
                                self._cancel_pending_exit_orders(tradingsymbol)

                            logger.info(f"Placing SELL order for {tradingsymbol} {instrument_token} : {qty} lots at LTP {ltp}")
                            try:
                                broker.sell_units(tradingsymbol,instrument_token,qty,exchange,ltp, position_key=str(token))
                            except Exception as sell_error:
                                # A failed auto-sell must not kill the
                                # watcher thread; the next cycle retries.
                                logger.error(
                                    f"Auto-sell failed for {tradingsymbol} "
                                    f"[{token}]: {sell_error}",
                                    exc_info=True
                                )
                        else:
                            if should_log_eval:
                                logger.debug(f"No SELL action taken for {tradingsymbol} at LTP {ltp}")
                    else:
                        # PAPER mode: mirror the LIVE branch - only record the
                        # SELL when the decision maker actually says sell, then
                        # rebuild the paper position grid. A failure here must
                        # not kill the watcher thread.
                        if not sell:
                            if should_log_eval:
                                logger.debug(f"PAPER: No SELL action taken for {tradingsymbol} at LTP {ltp}")
                        else:
                            try:
                                logger.info(f"PAPER: Recording paper SELL for {tradingsymbol} {instrument_token} : {qty} lots at LTP {ltp}")

                                self.write_paper_trade(
                                    transaction_type="SELL",
                                    tradingsymbol=tradingsymbol,
                                    token=instrument_token,
                                    qty=qty,
                                    ltp=ltp,
                                    product="MIS",
                                    buy_price=buy_price,
                                    lot_size=data.get("lotsize", 1),
                                    buy_time=self.get_open_position_buy_time(
                                        instrument_token,
                                        tradingsymbol=tradingsymbol,
                                        strategy=order_strategy_type,
                                        sell_type=sell_type
                                    ),
                                    strategy=order_strategy_type,
                                    sell_type=sell_type
                                )

                                # Recalculate paper positions so the sold
                                # quantity disappears from the grid.
                                self.refresh_paper_positions()
                            except Exception as paper_sell_error:
                                logger.error(
                                    f"PAPER auto-sell failed for {tradingsymbol}: {paper_sell_error}",
                                    exc_info=True
                                )
                except Exception as eval_error:
                    logger.error(
                        f"SL watcher evaluation failed for {token}: {eval_error}",
                        exc_info=True
                    )

            # Heartbeat - proves the watcher is alive in the log file.
            # A silent watcher previously went unnoticed because a
            # crashed thread only printed to the console, never to the
            # log.
            now_ts = time.time()
            if now_ts - self._last_watcher_heartbeat_ts >= 300:
                self._last_watcher_heartbeat_ts = now_ts
                logger.info(
                    f"SL watcher heartbeat | monitoring {len(self._position_data)} position leg(s)"
                )

            # Wait once per cycle (not once per leg) - an empty
            # position dict must idle, not busy-spin.
            self._stop_event.wait(timeout=2)

    # ---------------------- DECISION MAKER ----------------------------

    def _watcher_sell_decision(self, buy_price, ltp, order_strategy_type, contract_type, sell_type, log_enabled=True, override_book_profit_pct=None):
        """
        The watcher's per-leg exit decision.

        Sell Type T is watcher-managed (profit + SL) in every mode.
        Sell Types U/D own a configured exit that books their profit:
        a broker-side resting exit limit in LIVE, or the strategy-scaled
        POINTS target below in PAPER / SIMULATION / PLAYBACK. The
        watcher never PCT/PNT profit-books a U/D leg - that threshold
        differs from the configured exit values - it only runs the
        stop-loss safety net for them (check_profit=False).
        """
        if sell_type in ("U", "D") and self._mode != "LIVE":
            target_points = self._target_profit_points(order_strategy_type)
            try:
                profit_hit = (
                    target_points > 0
                    and float(buy_price) > 0
                    and float(ltp) >= float(buy_price) + target_points
                )
            except (TypeError, ValueError):
                profit_hit = False

            if profit_hit and log_enabled:
                logger.debug(
                    f"U/D profit target reached: LTP {ltp} >= "
                    f"{buy_price} + {target_points} pts"
                )

            sl_hit = self.to_sell_or_not_to_sell_SL(
                buy_price, ltp, order_strategy_type, contract_type,
                log_enabled, check_profit=False,
                override_book_profit_pct=override_book_profit_pct
            )
            return profit_hit or sl_hit

        # T in every mode: full profit + SL check. U/D in LIVE: SL only -
        # profit belongs to the resting exit limit at the broker.
        return self.to_sell_or_not_to_sell_SL(
            buy_price, ltp, order_strategy_type, contract_type,
            log_enabled,
            check_profit=sell_type not in ("U", "D"),
            override_book_profit_pct=override_book_profit_pct
        )

    def to_sell_or_not_to_sell(self,buy_price , current_price , order_strategy_type, log_enabled=True):
        # that is the question 
        if log_enabled:
            logger.debug(f"Deciding to sell or not: Buy Price = {buy_price}, Current Price = {current_price} and Order Strategy Type is {order_strategy_type}")

        # Fetched per call so config edits apply without a restart.
        comparison_function = fetch_from_json("appconfig.json", "COMPARISON_FUNCTION") or []
        PCT_BOOK_PROFIT = float(fetch_from_json("appconfig.json", "PCT_BOOK_PROFIT") or 0)
        PCT_STOP_LOSS = float(fetch_from_json("appconfig.json", "PCT_STOP_LOSS") or 0)
        PNT_BOOK_PROFIT = float(fetch_from_json("appconfig.json", "PTS_PROFIT") or 0)
        PNT_STOP_LOSS = float(fetch_from_json("appconfig.json", "PTS_LOSS") or 0)
        PROFIT_FACTOR = 1

        if order_strategy_type == "INTRA":
            PROFIT_FACTOR = float(fetch_from_json("appconfig.json", "PTS_PROFIT_INTRA_FACTOR") or 1)
        elif order_strategy_type == "SCALPING":
            PROFIT_FACTOR = float(fetch_from_json("appconfig.json", "PTS_PROFIT_SCALPING_FACTOR") or 1)
        elif order_strategy_type == "ULTRA_SCALPING":
            PROFIT_FACTOR = float(fetch_from_json("appconfig.json", "PTS_PROFIT_ULTRA_SCALPING_FACTOR") or 1)
        
        if log_enabled:
            logger.debug(f"Profit factor is {PROFIT_FACTOR} and Compare Function is {comparison_function}")
        
        if "PCT" in comparison_function:
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
        if "PNT" in comparison_function:
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
    def _pnt_sell_decision(self, buy_price, current_price, order_strategy_type, log_enabled=True, check_profit=True, override_book_profit_pct=None):
        """
        PNT (points) branch of the SL safety net - mirrors the PNT branch
        of to_sell_or_not_to_sell: profit threshold is PTS_PROFIT scaled
        by the strategy's PTS factor, stop loss is PTS_LOSS in points.
        The per-leg BK% override stays a percent and is converted to
        points off the buy price here (0 still disables profit booking).
        """
        try:
            if float(buy_price) <= 0:
                return False
        except (TypeError, ValueError):
            return False

        if order_strategy_type == "INTRA":
            PNT_FACTOR = float(fetch_from_json("appconfig.json", "PTS_PROFIT_INTRA_FACTOR") or 1)
        elif order_strategy_type == "SCALPING":
            PNT_FACTOR = float(fetch_from_json("appconfig.json", "PTS_PROFIT_SCALPING_FACTOR") or 1)
        elif order_strategy_type == "ULTRA_SCALPING":
            PNT_FACTOR = float(fetch_from_json("appconfig.json", "PTS_PROFIT_ULTRA_SCALPING_FACTOR") or 1)
        else:
            PNT_FACTOR = 1

        PNT_BOOK_PROFIT = float(fetch_from_json("appconfig.json", "PTS_PROFIT") or 0)
        PNT_STOP_LOSS = float(fetch_from_json("appconfig.json", "PTS_LOSS") or 0)

        if override_book_profit_pct is not None:
            override = max(0.0, float(override_book_profit_pct))
            BOOK_PROFIT_THRESHOLD_PTS = float(buy_price) * override / 100.0
        else:
            BOOK_PROFIT_THRESHOLD_PTS = PNT_BOOK_PROFIT * PNT_FACTOR
        STOP_LOSS_THRESHOLD_PTS = PNT_STOP_LOSS

        if log_enabled:
            logger.debug(
                f"PNT thresholds: factor {PNT_FACTOR} | Book Profit >= {BOOK_PROFIT_THRESHOLD_PTS:.2f} pts "
                f"| Stop Loss >= {STOP_LOSS_THRESHOLD_PTS:.2f} pts | check_profit={check_profit}"
            )

        # Profit booking: skipped entirely when check_profit is False
        # (U/D sell modes handle profit via the broker-side limit order).
        if check_profit and BOOK_PROFIT_THRESHOLD_PTS > 0 and current_price > buy_price:
            point_diff = self.calculate_point_difference(buy_price, current_price)
            if log_enabled:
                logger.debug(
                    f"PTS_PROFIT based Book Profit Check. Buy Price is {buy_price} and "
                    f"Current Price is {current_price}. Point Diff is {point_diff} "
                    f"and Book Profit threshold is {BOOK_PROFIT_THRESHOLD_PTS:.2f} pts"
                )
            if point_diff >= BOOK_PROFIT_THRESHOLD_PTS:
                if log_enabled:
                    logger.debug("Returning True for Booking Profit. PTS_PROFIT triggered.")
                return True

        if log_enabled:
            logger.debug(
                f"PTS_LOSS based Stop Loss Check. Buy Price is {buy_price} and "
                f"Current Price is {current_price} and Stop Loss threshold is {STOP_LOSS_THRESHOLD_PTS:.2f} pts"
            )
        if STOP_LOSS_THRESHOLD_PTS > 0 and current_price < buy_price:
            diff = abs(self.calculate_point_difference(buy_price, current_price))
            if diff >= STOP_LOSS_THRESHOLD_PTS:
                if log_enabled:
                    logger.debug("Returning True for Booking Loss. PTS_LOSS triggered.")
                return True
        if log_enabled:
            logger.debug("Returning False for Booking Loss")
        return False

    def to_sell_or_not_to_sell_SL(self,buy_price , current_price , order_strategy_type,contract_type, log_enabled=True, check_profit=True, override_book_profit_pct=None):
        # that is the question
        if log_enabled:
            logger.debug(f"Deciding to sell or not for Stop Loss: Buy Price = {buy_price}, Current Price = {current_price} and Order Strategy Type is {order_strategy_type}")

        # The SL path must apply the thresholds the UI actually shows:
        # PNT mode -> PTS_* points; PCT mode (and EMA, whose indicator
        # logic lives elsewhere) keeps the PCT_* percentages below.
        comparison_function = fetch_from_json("appconfig.json", "COMPARISON_FUNCTION") or []
        if "PNT" in comparison_function:
            return self._pnt_sell_decision(
                buy_price, current_price, order_strategy_type,
                log_enabled, check_profit, override_book_profit_pct
            )

        # PCT thresholds are scaled per strategy: TP% = PCT_BOOK_PROFIT * factor and
        # SL% = PCT_STOP_LOSS * factor. Unknown strategy types default to factor 1.
        # Fetched per call so config edits apply without a restart.
        if order_strategy_type == "INTRA":
            PCT_FACTOR = float(fetch_from_json("appconfig.json", "PCT_PROFIT_INTRA_FACTOR") or 10)
        elif order_strategy_type == "SCALPING":
            PCT_FACTOR = float(fetch_from_json("appconfig.json", "PCT_PROFIT_SCALPING_FACTOR") or 4)
        elif order_strategy_type == "ULTRA_SCALPING":
            PCT_FACTOR = float(fetch_from_json("appconfig.json", "PCT_PROFIT_ULTRA_SCALPING_FACTOR") or 1)
        else:
            PCT_FACTOR = 1

        PCT_BOOK_PROFIT = float(fetch_from_json("appconfig.json", "PCT_BOOK_PROFIT") or 0)
        PCT_STOP_LOSS = float(fetch_from_json("appconfig.json", "PCT_STOP_LOSS") or 0)

        EFFECTIVE_SL_PCT = PCT_STOP_LOSS
        # A per-position override (BK% box on the positions grid) replaces
        # the strategy-scaled book-profit threshold; 0 disables profit
        # booking for that leg. The stop-loss threshold is unaffected.
        if override_book_profit_pct is not None:
            BOOK_PROFIT_THRESHOLD_PCT = max(0.0, float(override_book_profit_pct))
        else:
            BOOK_PROFIT_THRESHOLD_PCT = PCT_BOOK_PROFIT * PCT_FACTOR
        STOP_LOSS_THRESHOLD_PCT = EFFECTIVE_SL_PCT * PCT_FACTOR

        if log_enabled:
            logger.debug(
                f"PCT thresholds: factor {PCT_FACTOR} | Book Profit >= {BOOK_PROFIT_THRESHOLD_PCT}% "
                f"| Stop Loss >= {STOP_LOSS_THRESHOLD_PCT}% | check_profit={check_profit}"
            )


        #logger.debug(f"STOP_LOSS settings : STOP_LOSS_ULTRA_SCALPING = {STOP_LOSS_ULTRA_SCALPING} , STOP_LOSS_SCALPING = {STOP_LOSS_SCALPING} , STOP_LOSS_INTRA = {STOP_LOSS_INTRA} and PCT_STOP_LOSS = {PCT_STOP_LOSS} ")
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
        #     logger.warning("TradingView Data is empty. Cannot make Stop Loss decision based on indicators. So proceeding with PCT_STOP_LOSS check")
        
        # This will be the fall back Stop Loss based on PCT difference
        # Orders will be one of the above types : Intra , Scalping , Ultra Scalping...Even if 1m/15s/5s MACD Swing dont trigger the Stop Loss we rely on the PCT_STOP_LOSS parameter
        # logger.debug("Falling back to PCT_STOP_LOSS based Stop Loss Check")

        # Book Profit check: sell when the position has gained at least
        # PCT_BOOK_PROFIT * strategy factor percent over the buy price.
        # A value of 0 disables profit booking. Skipped entirely when
        # check_profit is False (U/D sell modes - their profit is
        # handled by the broker-side limit order).
        tp_enabled = (
            PCT_BOOK_PROFIT > 0 if override_book_profit_pct is None
            else BOOK_PROFIT_THRESHOLD_PCT > 0
        )
        if check_profit and tp_enabled and current_price > buy_price:
            profit_diff = self.calculate_pctg_difference(buy_price, current_price)
            if log_enabled:
                logger.debug(
                    f"PCT_BOOK_PROFIT based Book Profit Check. Buy Price is {buy_price} and "
                    f"Current Price is {current_price}. Profit PCT Diff is {profit_diff} "
                    f"and Book Profit threshold is {BOOK_PROFIT_THRESHOLD_PCT} (PCT_BOOK_PROFIT {PCT_BOOK_PROFIT} x factor {PCT_FACTOR})"
                )
            if profit_diff >= BOOK_PROFIT_THRESHOLD_PCT:
                if log_enabled:
                    logger.debug("Returning True for Booking Profit. PCT_BOOK_PROFIT triggered.")
                return True

        if log_enabled:
            logger.debug(f"PCT_STOP_LOSS based Stop Loss Check. Buy Price is {buy_price} and Current Price is {current_price} and Stop Loss threshold is {STOP_LOSS_THRESHOLD_PCT}")
        if EFFECTIVE_SL_PCT > 0 and current_price < buy_price :
            diff = abs(self.calculate_pctg_difference(buy_price,current_price))
            if log_enabled:
                logger.debug(f"PCT Diff between BUY and Current Market Price is {diff} and Stop Loss threshold is {STOP_LOSS_THRESHOLD_PCT}")
            if diff >= STOP_LOSS_THRESHOLD_PCT:
                if log_enabled:
                    logger.debug("Returning True for Booking Loss. PCT_STOP_LOSS triggered.")
                return True
        if log_enabled:
            logger.debug("Returning False for Booking Loss")
        return False

    # ---------------------- GENERATE OTMS/ITMS ------------------------------    
        
    # def get_5_woc(
    #     self,
    #     nifty_price: float,
    #     call_or_put: str,
    #     strike_interval: int = 100,
    #     underlying: str = "NIFTY"
    # ) -> Dict[str, any]:
    #     logger.info(f"Generating 5 weekly option contracts for {underlying} at price {nifty_price} ({call_or_put})")
    #     if strike_interval <= 0:
    #         raise ValueError("strike_interval must be positive integer")
        
    #     atm_strike = int((nifty_price // strike_interval) * strike_interval)
    #     remainder = nifty_price%100
    #     if remainder >= 50:
    #         atm_strike+=100
        

    #     expiry = get_expiry_date(underlying)

    #     logger.info(f"Calculated ATM Strike: {atm_strike}, Expiry Date: {expiry}")

    #     if call_or_put.upper() == "CE":
    #         strikes = {
    #             "ITM2": atm_strike - 2 * strike_interval,
    #             "ITM1": atm_strike - 1 * strike_interval,
    #             "ATM":  atm_strike,
    #             "OTM1": atm_strike + 1 * strike_interval,
    #             "OTM2": atm_strike + 2 * strike_interval,
    #         }
    #     elif call_or_put.upper() == "PE":
    #         strikes = {
    #             "ITM2": atm_strike + 2 * strike_interval,
    #             "ITM1": atm_strike + 1 * strike_interval,
    #             "ATM":  atm_strike,
    #             "OTM1": atm_strike - 1 * strike_interval,
    #             "OTM2": atm_strike - 2 * strike_interval,
    #         }
    #     else:
    #         raise ValueError("call_or_put must be 'CE' or 'PE'")
        
    #     logger.debug(f"Initial strikes calculated: {strikes}")

    #     for k, v in strikes.items():
    #         if v is None or v < 0:
    #             strikes[k] = None

    #     symbols = {
    #         k: (self._broker.format_option_symbol(underlying, expiry, v, call_or_put) if v is not None else None)
    #         for k, v in strikes.items()
    #     }

    #     return {"expiry": expiry, "strikes": strikes, "symbols": symbols}

    def get_5_woc(
        self,
        nifty_price: float,
        call_or_put: str,
        strike_interval: int = 100,
        underlying: str = "NIFTY",
        base_strike: int = None
    ) -> Dict[str, any]:
        logger.info(
            f"Generating 5 weekly option contracts for "
            f"{underlying} at price {nifty_price} ({call_or_put})"
        )

        if strike_interval <= 0:
            raise ValueError("strike_interval must be positive integer")

        # ---------------------------------------------------------
        # Calculate BASE ATM strike
        #
        # IMPORTANT:
        # This is the existing ATM calculation based on the
        # Nifty Future LTP. DO NOT CHANGE THIS LOGIC.
        #
        # In PLAYBACK mode a base_strike parsed from the playback
        # instrument overrides the ATM calculation so the displayed
        # window is centered on the replayed option.
        # ---------------------------------------------------------
        if base_strike is not None and base_strike > 0:
            atm_strike = int((base_strike // strike_interval) * strike_interval)
            logger.info(f"PLAYBACK: using base strike {base_strike} (grid {atm_strike}) for the option window")
        else:
            atm_strike = int((nifty_price // strike_interval) * strike_interval)

            remainder = nifty_price % strike_interval

            if remainder >= strike_interval // 2:
                atm_strike += strike_interval

        # Near option expiry comes from the instrument-file-derived meta
        # (replaces the old EXPIRY_YEAR/EXPIRY_MONTH/*_EXPIRY_DATE keys).
        meta = self._broker.get_instrument_meta()
        expiry_iso = meta.get("near_option_expiry")
        if not expiry_iso:
            raise ValueError(
                f"No near option expiry in instrument meta for {underlying}; "
                f"cannot build the weekly option table."
            )
        expiry = datetime.date.fromisoformat(str(expiry_iso))

        logger.info(
            f"Calculated ATM Strike: {atm_strike}, "
            f"Expiry Date: {expiry}"
        )

        # ---------------------------------------------------------
        # Determine which WOC factor to use
        # ---------------------------------------------------------
        if call_or_put.upper() == "CE":
            factor_key = "WOC_CALL_FACTOR"
        elif call_or_put.upper() == "PE":
            factor_key = "WOC_PUT_FACTOR"
        else:
            raise ValueError("call_or_put must be 'CE' or 'PE'")

        try:
            factor = int(
                fetch_from_json("appconfig.json", factor_key) or 0
            )
        except (TypeError, ValueError, KeyError):
            factor = 0

        logger.info(
            f"{factor_key} = {factor}"
        )

        # ---------------------------------------------------------
        # FACTOR ONLY DECIDES WHICH 5 STRIKES ARE DISPLAYED.
        #
        # The BASE ATM remains atm_strike and is calculated above
        # from the Nifty Future LTP using the existing logic.
        #
        # Factor 0:
        #   offsets -2,-1,0,+1,+2
        #
        # Factor 1:
        #   offsets -1,0,+1,+2,+3
        #
        # Factor 2:
        #   offsets  0,+1,+2,+3,+4
        #
        # Factor 3:
        #   offsets +1,+2,+3,+4,+5
        #
        # General:
        #   start_offset = factor - 2
        # ---------------------------------------------------------
        start_offset = factor - 2

        display_offsets = list(
            range(start_offset, start_offset + 5)
        )

        # ---------------------------------------------------------
        # Generate the 5 displayed strikes.
        #
        # LEVEL NUMBERING IS ALWAYS BASED ON THE BASE ATM.
        #
        # Factor does NOT affect level numbering.
        #
        # CE:
        #   -2 -> ITM2
        #   -1 -> ITM1
        #    0 -> ATM
        #   +1 -> OTM1
        #   +2 -> OTM2
        #   +3 -> OTM3
        #   ...
        #
        # PE:
        #   -2 -> OTM2
        #   -1 -> OTM1
        #    0 -> ATM
        #   +1 -> ITM1
        #   +2 -> ITM2
        #   +3 -> ITM3
        #   ...
        # ---------------------------------------------------------
        strikes = {}

        for offset in display_offsets:

            strike = atm_strike + (
                offset * strike_interval
            )

            if offset == 0:
                level = "ATM"

            elif call_or_put.upper() == "CE":
                if offset < 0:
                    level = f"ITM{abs(offset)}"
                else:
                    level = f"OTM{offset}"

            else:  # PE
                if offset < 0:
                    level = f"OTM{abs(offset)}"
                else:
                    level = f"ITM{offset}"

            strikes[level] = strike

        logger.debug(
            f"Base ATM: {atm_strike}, "
            f"Factor: {factor}, "
            f"Display offsets: {display_offsets}, "
            f"Initial strikes calculated: {strikes}"
        )

        # ---------------------------------------------------------
        # Validate strikes
        # ---------------------------------------------------------
        for k, v in strikes.items():
            if v is None or v < 0:
                strikes[k] = None

        # ---------------------------------------------------------
        # Generate broker symbols
        # ---------------------------------------------------------
        symbols = {
            k: (
                self._broker.format_option_symbol(
                    underlying,
                    expiry,
                    v,
                    call_or_put
                )
                if v is not None
                else None
            )
            for k, v in strikes.items()
        }

        logger.debug(
            f"Generated weekly option symbols: {symbols}"
        )

        return {
            "expiry": expiry,
            "strikes": strikes,
            "symbols": symbols
        }


    @staticmethod
    def _last_known_price_entry(symbol, ltp, open_price=None, prev_close=None):
        return {
            "symbol": str(symbol),
            "ltp": float(ltp),
            "open": (float(open_price) if open_price else None),
            "prev_close": (float(prev_close) if prev_close else None),
            "ts": datetime.datetime.now().isoformat(),
        }

    def save_last_known_price(self, symbol, ltp, open_price=None, prev_close=None):
        """
        Persist the latest real (non-fallback) price for a symbol so a
        later session can fall back to it when every live source fails.
        """
        try:
            write_to_json(
                {str(symbol): self._last_known_price_entry(symbol, ltp, open_price, prev_close)},
                self._LAST_KNOWN_PRICES_FILE
            )
        except Exception as e:
            logger.debug(f"Could not persist last-known price for {symbol}: {e}")

    def save_last_known_prices(self, entries):
        """
        Persist many last-known price entries in a single file write.
        entries: {symbol: entry_dict} as produced by _last_known_price_entry.
        """
        if not entries:
            return
        try:
            write_to_json(entries, self._LAST_KNOWN_PRICES_FILE)
        except Exception as e:
            logger.debug(f"Could not persist batched last-known prices: {e}")

    def load_last_known_price(self, symbol):
        """
        Return the persisted last-known price entry for a symbol when it
        is positive and fresh enough, else None.

        The returned dict carries ltp/open/prev_close/ts plus the
        computed age_days for logging.
        """
        try:
            with open(self._LAST_KNOWN_PRICES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            entry = data.get(str(symbol))
            if not entry:
                return None

            ltp = float(entry.get("ltp") or 0)
            ts = datetime.datetime.fromisoformat(entry.get("ts"))
            age_days = (datetime.datetime.now() - ts).total_seconds() / 86400.0

            if ltp <= 0 or age_days < 0 or age_days > self._LAST_KNOWN_PRICE_MAX_AGE_DAYS:
                return None

            entry["ltp"] = ltp
            entry["age_days"] = round(age_days, 2)
            return entry

        except FileNotFoundError:
            return None
        except Exception as e:
            logger.debug(f"Could not load last-known price for {symbol}: {e}")
            return None

    # ---------------------------------------------------------
    # Order-selection persistence (strategy + sell mode per token)
    # ---------------------------------------------------------
    @classmethod
    def _load_order_selections(cls):
        """
        Hydrate the in-memory strategy/sell-mode mappings from
        data/order_selections.json so selections survive a restart.
        Runs once, when the singleton instance is first created.
        """
        try:
            with open(cls._ORDER_SELECTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            for token, selection in (data or {}).items():
                if not isinstance(selection, dict):
                    continue
                token = str(token)
                strategy = str(
                    selection.get("strategy") or "ULTRA_SCALPING"
                )
                sell_mode = str(selection.get("sell_mode") or "T").strip().upper()
                cls._order_strategy_mapping[token] = cls._normalise_strategy(strategy)
                cls._order_sell_mode_mapping[token] = sell_mode

            if data:
                logger.debug(f"Restored order selections: {data}")

        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug(f"Could not load persisted order selections: {e}")

    def _save_order_selections(self):
        """
        Snapshot both selection mappings into a single JSON file write.
        File format: {token: {"strategy": ..., "sell_mode": ...}}.
        """
        try:
            tokens = set(self._order_strategy_mapping) | set(self._order_sell_mode_mapping)
            payload = {
                token: {
                    "strategy": self._normalise_strategy(
                        self._order_strategy_mapping.get(token, "ULTRA_SCALPING")
                    ),
                    "sell_mode": str(
                        self._order_sell_mode_mapping.get(token, "T")
                    ).strip().upper(),
                }
                for token in tokens
            }
            write_to_json(payload, self._ORDER_SELECTIONS_FILE)
        except Exception as e:
            logger.debug(f"Could not persist order selections: {e}")

    def setup_woc_subscriptions(self):
        """
        Public entry point for the weekly-option table build.

        Re-entrancy guard: a second concurrent build (websocket
        reconnect while a build is still running, or a refresh click
        during startup build) would corrupt the option table state
        mid-write, so concurrent requests are skipped instead.
        """
        if not self._woc_setup_lock.acquire(blocking=False):
            logger.warning(
                "Weekly option setup already in progress; skipping this request."
            )
            return

        try:
            self._setup_woc_subscriptions_impl()
        finally:
            self._woc_setup_lock.release()

    def _setup_woc_subscriptions_impl(self):

        logger.info("Setting up weekly option contract subscriptions...")

        try:

            # Instrument-file-derived meta (persisted in the reduced
            # instrument file / computed by the broker adapter). Read
            # fresh on every setup run so a regenerated instrument list
            # takes effect on the next refresh-options-table click.
            instrument_meta = self._broker.get_instrument_meta()

            # Strike grid step (points) for the Buy grid (fixed 100).
            strike_interval = int(instrument_meta.get("strike_interval") or 100)
            logger.info(f"Using strike interval {strike_interval}")

            near_month_symbol = instrument_meta.get("near_month_future_token")
            logger.info(f"NEAR_MONTH_FUTURE_TOKEN = {near_month_symbol}")

            if not near_month_symbol:
                logger.error(
                    "No near-month future token in instrument meta; cannot "
                    "build the weekly option table. Regenerate the instrument "
                    "list (restart or switch underlying) to refresh the meta."
                )
                return

            near_month_data = self._broker.get_instrument_details(near_month_symbol)

            if not near_month_data:
                logger.error(
                    f"Near-month future {near_month_symbol} not found in the "
                    f"reduced instrument list; cannot build the weekly option "
                    f"table. Regenerate the instrument list (restart or switch "
                    f"underlying) to refresh the instrument meta."
                )
                return

            near_month_token = str(near_month_data["instrument_token"])

            # # ------------------------------------------------
            # # Step 1: Subscribe to near-month future first
            # # ------------------------------------------------
            # self._broker.subscribe_to_all([near_month_token])

            # # First try websocket price
            # price = self.get_latest_price(str(near_month_token))
            # open_price = price


            self._broker.subscribe_to_all([near_month_token])

            # Give WebSocket up to 2s (with early exit) to deliver the
            # first tick — Kite's first quote packet typically lands
            # 0.3-1.5s after subscribe. During market hours only; when
            # the market is closed no tick can arrive, so skip the wait
            # entirely and go straight to REST
            future_prices = self.wait_for_prices(
                [near_month_token],
                timeout=2.0 if is_market_open() else 0.0
            )

            price = future_prices.get(near_month_token)
            open_price = price
            price_source = "websocket" if price else None

            

            # ------------------------------------------------
            # Step 1.5: If websocket price is unavailable,
            # reuse the quote fetched moments ago during
            # instrument-list reduction; only fall back to
            # a fresh REST quote when that cache is stale.
            # ------------------------------------------------
            if not price:
                cached_quote = self._near_month_future_quote_cache or {}
                cache_age = time.time() - cached_quote.get("ts", 0)

                if (
                    cached_quote.get("ltp")
                    and cached_quote.get("open")
                    and 0 <= cache_age < 300
                ):
                    price = cached_quote["ltp"]
                    open_price = cached_quote["open"]
                    logger.info(
                        f"Using future quote cached during instrument-list "
                        f"reduction (age {cache_age:.1f}s)."
                    )

            fut_prev_close_rest = None

            if not price:
                logger.info(
                    f"Near-month future price not available from websocket "
                    f"for {near_month_data['tradingsymbol']}. Fetching REST quote."
                )

                batch_quotes = self._broker.get_quotes_batch(
                    [near_month_data["tradingsymbol"]]
                )

                q = batch_quotes.get(near_month_data["tradingsymbol"])

                if q:
                    price = q[0]
                    open_price = q[1]
                    price_source = "rest"
                    if len(q) > 2 and q[2]:
                        fut_prev_close_rest = q[2]

            # ------------------------------------------------
            # Step 1.6: Fallbacks when no live, cached, or REST
            # price is available.
            #
            # 1st: the persisted last-known real price for this exact
            #      future symbol (saved by an earlier session, valid
            #      for up to 7 days) - data-driven and much fresher
            #      than a static constant.
            # 2nd: static fallback levels - NIFTY/SENSEX only, kept
            #      as the final safety net.
            # ------------------------------------------------
            if not price:

                persisted = self.load_last_known_price(near_month_symbol)

                if persisted:
                    logger.warning(
                        f"Near-month future quote unavailable for "
                        f"{self._underlying}; using persisted last-known "
                        f"price {persisted['ltp']} for {near_month_symbol} "
                        f"(age {persisted['age_days']} day(s), may be stale)."
                    )
                    price = persisted["ltp"]
                    open_price = persisted.get("open") or price
                    if not fut_prev_close_rest:
                        fut_prev_close_rest = persisted.get("prev_close")

                elif self._underlying == "SENSEX":
                    logger.warning(
                        "Near-month SENSEX future price unavailable. "
                        "Using SENSEX fallback."
                    )
                    price = float(fetch_from_json("appconfig.json", "SENSEX_FALLBACK_LTP") or 0)
                    open_price = price

                elif self._underlying == "NIFTY":
                    logger.warning(
                        "Near-month NIFTY future price unavailable. "
                        "Using NIFTY fallback."
                    )
                    price = float(fetch_from_json("appconfig.json", "NIFTY_FALLBACK_LTP") or 0)
                    open_price = price

                else:
                    logger.error(
                        f"Unable to determine near-month future price for "
                        f"{self._underlying}. Skipping weekly option setup."
                    )
                    return

            # ------------------------------------------------
            # Persist the resolved price for future sessions. Only
            # provably real sources are saved (websocket tick or REST
            # quote); the in-memory reduction cache is deliberately
            # skipped because the M.Stock reduction path can seed it
            # with a fallback level.
            # ------------------------------------------------
            if price and price_source in ("websocket", "rest"):
                self.save_last_known_price(
                    near_month_symbol,
                    price,
                    open_price,
                    fut_prev_close_rest
                )

            logger.info(
                f"Using near-month future price {price} for "
                f"{self._underlying} option generation"
            )

            # ------------------------------------------------
            # Step 2: Generate contracts using the ACTUAL
            # near-month future price.
            #
            # In PLAYBACK mode, center the strike window on the
            # playback instrument's own strike (parsed from its
            # tradingsymbol) so the replayed option is guaranteed
            # a row in the dashboard. The base for the playback
            # side is shifted by (factor - 2) strikes so the
            # playback strike is the first row of its window;
            # the opposite side just uses the snapped strike.
            # ------------------------------------------------
            playback_base_strike = None
            playback_symbol = None
            playback_ce_base = None
            playback_pe_base = None
            if self._mode == "PLAYBACK" and hasattr(self._broker, "get_playback_instrument_name"):
                playback_symbol = str(self._broker.get_playback_instrument_name()).upper()
                # Prefer the authoritative strike from the instrument list;
                # fall back to parsing it from the tradingsymbol.
                if hasattr(self._broker, "get_playback_strike"):
                    playback_base_strike = self._broker.get_playback_strike()
                if playback_base_strike is None:
                    playback_match = re.search(r"(\d{4,6})(CE|PE)$", playback_symbol)
                    if playback_match:
                        playback_base_strike = int(playback_match.group(1))
                if playback_base_strike:
                    snapped_base = int((playback_base_strike // strike_interval) * strike_interval)
                    if playback_symbol.endswith("CE"):
                        try:
                            call_factor = int(fetch_from_json("appconfig.json", "WOC_CALL_FACTOR") or 0)
                        except (TypeError, ValueError):
                            call_factor = 0
                        playback_ce_base = playback_base_strike - (call_factor - 2) * strike_interval
                        playback_pe_base = snapped_base
                    else:
                        try:
                            put_factor = int(fetch_from_json("appconfig.json", "WOC_PUT_FACTOR") or 0)
                        except (TypeError, ValueError):
                            put_factor = 0
                        playback_pe_base = playback_base_strike - (put_factor - 2) * strike_interval
                        playback_ce_base = snapped_base
                    logger.info(
                        f"PLAYBACK: option windows centered on strike "
                        f"{playback_base_strike} from {playback_symbol} "
                        f"(CE base {playback_ce_base}, PE base {playback_pe_base})"
                    )

            woc_ce = self.get_5_woc(
                price,
                "CE",
                strike_interval=strike_interval,
                underlying=self._underlying,
                base_strike=playback_ce_base
            )
            contracts_ce = woc_ce["symbols"]
            strikes_ce = woc_ce["strikes"]

            woc_pe = self.get_5_woc(
                price,
                "PE",
                strike_interval=strike_interval,
                underlying=self._underlying,
                base_strike=playback_pe_base
            )
            contracts_pe = woc_pe["symbols"]
            strikes_pe = woc_pe["strikes"]

            all_contracts = {}

            tokens_to_subscribe = [near_month_token]


            # self._broker.subscribe_to_all([near_month_token])
            # price = self.get_latest_price(str(near_month_token))

            # if not price:
            #     if self._underlying == "SENSEX":
            #         price = self._SENSEX_FALLBACK_LTP
            #     else:
            #         price = self._NIFTY_FALLBACK_LTP



            # # ------------------------------------
            # # Step 1: Generate all contracts first
            # # ------------------------------------
            # #float(self._NIFTY_FALLBACK_LTP),
            # contracts_ce = self.get_5_woc(
            #     price,   
            #     "CE",
            #     underlying=self._underlying
            # )["symbols"]

            # contracts_pe = self.get_5_woc(
            #     price,   
            #     "PE",
            #     underlying=self._underlying
            # )["symbols"]

            # all_contracts = {}

            # tokens_to_subscribe = [near_month_token]


            for key, value in contracts_ce.items():

                data = self._broker.get_instrument_details(value)

                if not data:
                    logger.error(
                        f"Weekly CE option {key} {value} not found in the "
                        f"reduced instrument list (strike outside the "
                        f"generated window); skipping it."
                    )
                    continue

                token = str(data["instrument_token"])

                tokens_to_subscribe.append(token)

                all_contracts[token] = ("CE", key, data, strikes_ce.get(key))


            for key, value in contracts_pe.items():

                data = self._broker.get_instrument_details(value)

                if not data:
                    logger.error(
                        f"Weekly PE option {key} {value} not found in the "
                        f"reduced instrument list (strike outside the "
                        f"generated window); skipping it."
                    )
                    continue

                token = str(data["instrument_token"])

                tokens_to_subscribe.append(token)

                all_contracts[token] = ("PE", key, data, strikes_pe.get(key))


            # ------------------------------------------------
            # PLAYBACK safety net: guarantee the playback
            # instrument has a row in the dashboard even when
            # its strike/expiry naming did not match the
            # generated window symbols.
            # ------------------------------------------------
            if (
                playback_symbol
                and playback_symbol.endswith(("CE", "PE"))
                and not any(
                    str(contract_data["tradingsymbol"]).upper() == playback_symbol
                    for _cp, _level, contract_data, _strike in all_contracts.values()
                )
            ):
                logger.info(
                    f"PLAYBACK: injecting {playback_symbol} into the options table"
                )
                playback_data = self._broker.get_instrument_details(playback_symbol)
                if playback_data:
                    playback_token = str(playback_data["instrument_token"])
                    if playback_token not in all_contracts:
                        tokens_to_subscribe.append(playback_token)
                        all_contracts[playback_token] = (
                            "CE" if playback_symbol.endswith("CE") else "PE",
                            "PLAYBACK",
                            playback_data,
                            playback_base_strike
                        )
                else:
                    logger.error(
                        f"Playback instrument {playback_symbol} not found in "
                        f"the reduced instrument list; skipping injection."
                    )


            logger.info(f"Subscribing to {len(tokens_to_subscribe)} instruments")

            # ------------------------------------
            # Step 2: Subscribe once
            # ------------------------------------

            self._broker.subscribe_to_all(tokens_to_subscribe)


            # ------------------------------------
            # Step 3: Wait once for websocket
            # (up to 2s during market hours, with
            # early exit once every token has a
            # tick; no wait when the market is
            # closed since no tick can arrive —
            # REST seeds the prices and websocket
            # overwrites them as ticks come in)
            # ------------------------------------

            prices = self.wait_for_prices(
                tokens_to_subscribe,
                timeout=2.0 if is_market_open() else 0.0
            )

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

                # One retry pass for symbols that came back empty: the
                # M.Stock REST gateway is flaky right after startup and a
                # single failure must not leave weekly options stranded on
                # the fallback price of 100 (lots would then size to 0).
                still_missing = [
                    symbol for symbol in missing_symbols
                    if not batch_quotes.get(symbol)
                ]

                if still_missing:
                    logger.info(
                        f"{len(still_missing)} quotes still missing after first batch. "
                        f"Retrying once after a short pause."
                    )
                    time.sleep(2)

                    batch_quotes.update(
                        self._broker.get_quotes_batch(still_missing)
                    )

            else:

                batch_quotes = {}


            # # ------------------------------------
            # # Step 4: Handle Near Month Future
            # # ------------------------------------

            # price = prices.get(near_month_token)

            # if not price:

            #     q = batch_quotes.get(near_month_data["tradingsymbol"])

            #     if q:
            #         price, open_price = q

            # # if not price:

            # #     logger.warning("Future REST quote failed. Using fallback")

            # #     price = self._NIFTY_FALLBACK_LTP

            # #     open_price = price

            # # MSTOCK_SENSEX
            # if not price:
            #     logger.warning("Future REST quote failed. Using fallback")

            #     if self._underlying == "SENSEX":
            #         price = self._SENSEX_FALLBACK_LTP
            #     else:
            #         price = self._NIFTY_FALLBACK_LTP

            #     open_price = price            


            self.ltp_near_month_future = price

            self.open_near_month_future = open_price

            # ------------------------------------------------
            # Step 4.5: Resolve previous day close for chgO.
            # chgO must be vs PREVIOUS DAY CLOSE (not today's
            # open) so that gap up/down is reflected correctly.
            # Priority: websocket tick close -> REST close ->
            # reduction quote cache -> today's open -> LTP.
            # ------------------------------------------------
            prev_close = (
                self._prev_close_prices.get(near_month_token)
                or fut_prev_close_rest
                or (self._near_month_future_quote_cache or {}).get("prev_close")
                or open_price
                or price
            )

            self.prev_close_near_month_future = prev_close

            chgO = (price - prev_close) * 100 / prev_close if prev_close else 0.0

            pts_chg = round(price - prev_close, 2) if prev_close else 0.0

            self.frontend_data_socket.emit(
                "near-month-ltp-updated",
                {"ltp": price, "chgO": round(chgO, 2), "ptsChg": pts_chg}
            )

            self._near_month_data[near_month_token] = price


            # ------------------------------------
            # Step 5: Initialize options
            # ------------------------------------

            
            pending_price_updates = {}

            # Rebuild from scratch: drop contracts from the previous
            # factor/window so a changed WOC factor cannot leave stale
            # rows in the grid. The dict is repopulated below from
            # all_contracts, which always reflects the current factor.
            self._five_weekly_option_contracts.clear()

            # Mirror the frontend's grid sort (ATM first, OTMs deepest
            # first, ITMs shallowest first) so the positional row index
            # here matches the row order the user actually sees - the
            # window can shift (OTM3/ITM3...) with the WOC factor.
            def _woc_row_sort_key(level_name):
                if level_name == "ATM":
                    return 0
                match = re.match(r"^(OTM|ITM)(\d+)$", str(level_name or ""))
                if not match:
                    return 999999
                number = int(match.group(2))
                return -number if match.group(1) == "OTM" else number

            side_contracts = {}
            for token, (cp, level, data, strike) in all_contracts.items():
                side_contracts.setdefault(cp, []).append((level, token))

            row_position = {}
            for cp, items in side_contracts.items():
                items.sort(key=lambda item: _woc_row_sort_key(item[0]))
                for idx, (_level, _token) in enumerate(items):
                    row_position[_token] = idx

            for token, (cp, level, data, strike) in all_contracts.items():

                price = prices.get(token)

                if not price:

                    logger.debug(f"Websocket price missing for {data['tradingsymbol']}; checking REST fallback")

                q = batch_quotes.get(data["tradingsymbol"])

                if q:

                    price = q[0]

                # Persist real prices (websocket tick or REST quote) so a
                # later session can fall back to the last real premium
                # instead of the flat 100. Fallback prices are never saved.
                # Entries are collected here and written once after the loop
                # to avoid a full-file read/rewrite per contract.
                if price:

                    pending_price_updates[str(data["tradingsymbol"])] = self._last_known_price_entry(
                        data["tradingsymbol"],
                        price,
                        open_price=(q[1] if q else None),
                        prev_close=(
                            self._prev_close_prices.get(token)
                            or (q[2] if (q and len(q) > 2) else None)
                        )
                    )

                persisted = None

                if not price:

                    # Yesterday's (or earlier) persisted real premium is far
                    # more accurate than the flat fallback of 100.
                    persisted = self.load_last_known_price(data["tradingsymbol"])

                    if persisted:

                        logger.info(
                            f"Websocket and REST quotes unavailable for "
                            f"{data['tradingsymbol']}; using persisted last-known "
                            f"price {persisted['ltp']} (age {persisted['age_days']} day(s))."
                        )

                        price = persisted["ltp"]

                    else:

                        logger.info(f"REST quote unavailable for {data['tradingsymbol']}; using fallback price 100")

                        price = 100


                prev_close = self._prev_close_prices.get(token)

                if not prev_close and q and len(q) > 2 and q[2]:
                    prev_close = q[2]

                if not prev_close and persisted:
                    prev_close = persisted.get("prev_close")

                if not prev_close:
                    prev_close = price


                self.weekly_options_initialize(
                    token,
                    data,
                    cp,
                    price,
                    level=level,
                    prev_close=prev_close,
                    strike=strike
                )

                # Positional defaults mirroring the buy-grid rows (the
                # display window can shift, so level NAMES may be any
                # OTMx/ITMx - the row's position within each option type
                # is the reference):
                #   row 1 (top) -> INTRA + T
                #   row 2       -> SCALPING + D
                #   row 3 (mid) -> ULTRA_SCALPING + U
                #   rows 4/5    -> ULTRA_SCALPING + T
                # Used when a leg is created without a frontend buy
                # event (SIM/LIVE residual legs, fast-update fallback).
                side_index = row_position.get(token, 999999)
                if side_index == 0:
                    level_strategy, level_sell_mode = "INTRA", "T"
                elif side_index == 1:
                    level_strategy, level_sell_mode = "SCALPING", "D"
                elif side_index == 2:
                    level_strategy, level_sell_mode = "ULTRA_SCALPING", "U"
                else:
                    level_strategy, level_sell_mode = "ULTRA_SCALPING", "T"

                self._order_strategy_mapping[token] = level_strategy
                self._order_sell_mode_mapping[token] = level_sell_mode


            # Single write after the loop - the snapshot covers every
            # token, so saving inside the loop would just re-write the
            # file once per contract with the same values.
            self._save_order_selections()

            self.save_last_known_prices(pending_price_updates)

            # The setup batch just persisted every contract's price; seed
            # the tick-flush timer so the first tick after startup does
            # not immediately re-write the same values.
            self._last_price_flush_ts = time.time()

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

    #         near_month_symbol = fetch_from_json("appconfig.json" , "NEAR_MONTH_FUTURE_TOKEN")

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
    #         near_month_symbol = fetch_from_json("appconfig.json" , "NEAR_MONTH_FUTURE_TOKEN")
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

    def emit_price_updated_order(self, payload):
        try:
            logger.info(
                f"MSTOCK SOCKET BACKGROUND EMIT | "
                f"token={payload.get('token')} | "
                f"ltp={payload.get('ltp')}"
            )

            logger.info(f"MSTOCK SOCKET CLIENTS | count={len(self.frontend_data_socket.server.eio.sockets)}")

            self.frontend_data_socket.emit(
                'price-updated-order',
                payload,
                namespace='/'
            )
            

            logger.info(
                f"MSTOCK SOCKET BACKGROUND EMIT DONE | "
                f"token={payload.get('token')}"
            )

        except Exception as e:
            logger.exception(
                f"MSTOCK SOCKET BACKGROUND EMIT ERROR | "
                f"token={payload.get('token')} | "
                f"error={e}"
            )


    def set_latest_price(self, instrument_token, tradingsymbol, price, close=None):
        try:
            token = str(instrument_token)

            # logger.info(
            #     f"MSTOCK set_latest_price | "
            #     f"token={token} | price={price} | "
            #     f"in_weekly={token in self._five_weekly_option_contracts} | "
            #     f"in_position={token in self._position_data} | "
            #     f"in_near_month={token in self._near_month_data}"
            # )

            # Store latest price for strategy use
            self._latest_prices[token] = price

            # Previous day close is constant for the whole session;
            # store the first valid value seen for the token.
            if close is not None:
                try:
                    close_val = float(close)
                except (TypeError, ValueError):
                    close_val = 0.0
                if close_val > 0 and token not in self._prev_close_prices:
                    self._prev_close_prices[token] = close_val

            leg_keys = self._legs_for_token(token)

            if leg_keys:
                # Fan the tick out to every strategy/sell-type leg of
                # this instrument; each leg keeps its own P/L.
                self.position_data_update(
                    token,
                    "latest_price",
                    price,
                    is_token=True
                )

                for leg_key in leg_keys:
                    position_info = self._position_data[leg_key]

                    payload = {
                        'token': token,
                        'position_key': str(leg_key),
                        'name': tradingsymbol,
                        'ltp': price,
                        'pts_pl': position_info.get('pts_pl'),
                        'pct_pl': position_info.get('pct_pl'),
                        'total_pl': position_info.get('total_pl')
                    }

                    self.frontend_data_socket.emit(
                        'price-updated-position',
                        payload
                    )

            if token in self._five_weekly_option_contracts:
                self.update_weekly_options(
                    token,
                    "ltp",
                    price
                )

                prev_close = self._prev_close_prices.get(token)

                if prev_close:
                    self._five_weekly_option_contracts[token]["prev_close"] = prev_close

                # Keep the persisted last-known premium fresh during the
                # session so the next session can fall back to the most
                # recent real price instead of the flat 100. Entries are
                # throttled to one per token per minute, accumulated in
                # memory, and flushed as a single batched write every 60
                # seconds. Some tick paths deliver no tradingsymbol, so
                # resolve it from the contract table.
                now_ts = time.time()
                if now_ts - self._price_persist_ts.get(token, 0) >= 60:
                    self._price_persist_ts[token] = now_ts
                    symbol = tradingsymbol or self._five_weekly_option_contracts[token].get("name")
                    if symbol:
                        self._pending_tick_price_updates[str(symbol)] = self._last_known_price_entry(
                            symbol,
                            price,
                            prev_close=self._prev_close_prices.get(token)
                        )

                if self._pending_tick_price_updates and now_ts - self._last_price_flush_ts >= 60:
                    self._last_price_flush_ts = now_ts
                    pending = self._pending_tick_price_updates
                    self._pending_tick_price_updates = {}
                    self.save_last_known_prices(pending)

                payload = {
                    'token': token,
                    'name': tradingsymbol,
                    'ltp': price,
                    'lots': self._five_weekly_option_contracts[token]["lots"]
                }

                if prev_close:
                    payload['prev_close'] = prev_close

                # logger.info(
                #     f"MSTOCK EMIT price-updated-order | "
                #     f"token={token} | ltp={price}"
                # )

                # logger.info(
                #     f"MSTOCK SOCKET CLIENT CHECK | "
                #     f"server_exists={self.frontend_data_socket.server is not None}"
                # )

                self.frontend_data_socket.emit(
                    'price-updated-order',
                    payload
                )
                # self.frontend_data_socket.start_background_task(
                #     self.emit_price_updated_order,
                #     payload
                # )                

                # logger.info(
                #     f"MSTOCK EMIT price-updated-order-debug | "
                #     f"token={token} | ltp={price}"
                # )

                self.frontend_data_socket.emit(
                    'price-updated-order-debug',
                    payload
                )

            if token in self._near_month_data:
                self._near_month_data[token] = price

                prev_close = (
                    self._prev_close_prices.get(token)
                    or self.prev_close_near_month_future
                    or self.open_near_month_future
                )

                chgO = round(
                    (price - prev_close)
                    * 100
                    / prev_close,
                    2
                ) if prev_close else 0.0

                payload = {
                    "ltp": price,
                    "chgO": chgO,
                    "ptsChg": round(price - prev_close, 2) if prev_close else 0.0
                }

                self._near_month_future_ltp = price

                # logger.info(
                #     f"MSTOCK EMIT near-month-ltp-updated | "
                #     f"token={token} | ltp={price}"
                # )

                self.frontend_data_socket.emit(
                    'near-month-ltp-updated',
                    payload
                )

        except Exception as e:
            logger.error(
                f"Error setting latest price {e}",
                exc_info=True
            )




    # def set_latest_price(self, instrument_token, tradingsymbol, price):
    #     try:
    #         #logger.debug(f"Received tick for {tradingsymbol} (Token: {instrument_token}) with Latest price {price}")
    #         #token = instrument_token
    #         token = str(instrument_token)

    #         # ⭐ ADD THIS LINE (store price for strategy use)
    #         self._latest_prices[token] = price


    #         if token in self._position_data:
    #             self.position_data_update(
    #                 token, 
    #                 "latest_price", 
    #                 price, 
    #                 is_token=True
    #             )
    #             position_info = self._position_data[token]
    #             payload = {
    #                 'token': token,
    #                 'name': tradingsymbol,
    #                 'ltp': price,
    #                 'pts_pl': position_info.get('pts_pl'),
    #                 'pct_pl': position_info.get('pct_pl'),
    #                 'total_pl': position_info.get('total_pl')
    #             }
    #             self.frontend_data_socket.emit('price-updated-position', payload)
            
            
    #         if token in self._five_weekly_option_contracts:
    #             self.update_weekly_options(token , "ltp" , price)
    #             payload = {
    #                 'token': token,             
    #                 'name': tradingsymbol,      
    #                 'ltp': price,
    #                 'lots' : self._five_weekly_option_contracts[token]["lots"]
    #             }
    #             # logger.debug("UPDATING ORDER PRICES")
    #             self.frontend_data_socket.emit('price-updated-order', payload)
            
    #         if token in self._near_month_data:
    #             self._near_month_data[token] = price

    #             chgO = round((price - self.open_near_month_future )*100 / self.open_near_month_future,2) 
    #             payload = {"ltp" : price, "chgO": chgO}

    #             self._near_month_future_ltp = price
    #             self.frontend_data_socket.emit('near-month-ltp-updated', payload)




    #         # # When Near Month Future's LTP gets updated, let us also update the TradingViewData in the front end for reference
    #         # data = latest_tradingview_data
    #         # if not data:
    #         #     logger.warning("No TradingView data available for validation.")
    #         #     return
    #         # payload = {"TradingView_Data" : data}
    #         # #self.frontend_data_socket.emit('TradingView_Data', payload)


            
    #     except Exception as e:
    #         logger.error(f"Error setting latest price {e}")


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
                port=int(os.getenv("SOCKET_IO_PORT", "5001")),
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
            self._watcher_loop_alive_ts = time.time()
            thread = threading.Thread(
                target=self.stop_loss_book_profit_core,
                args=(self._broker,),
                daemon=True,
            )
            self._watcher_thread_ref = thread
            logger.info("Launching trading watcher thread 🚀")
            thread.start()

            # Start the new voice readout thread
            voice_thread = threading.Thread(
                target=self.voice_readout_core,
                daemon=True,
            )
            logger.info("Launching voice readout thread 🔊")
            voice_thread.start()

            # One supervisor per process watches the watcher and
            # relaunches it if it ever dies or stalls again.
            if not self._watcher_supervisor_started:
                self._watcher_supervisor_started = True
                supervisor = threading.Thread(
                    target=self._watcher_supervisor_core,
                    daemon=True,
                )
                logger.info("Launching SL watcher supervisor thread 🛡")
                supervisor.start()

    def _watcher_supervisor_core(self):
        """
        Watchdog for the stop-loss watcher thread.

        A silent watcher death disables the stop-loss for every open
        position while the UI keeps looking alive - this happened on
        2026-09-22 at ~10:31 and the 10:40 loss excursion went
        unguarded. Restart the thread when it exits (crash) or stops
        making progress (hang). The generation bump makes any revived
        old thread exit at its next cycle check instead of running
        alongside the replacement.
        """
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=30)
            if self._stop_event.is_set():
                return

            if not self._trading_watcher_thread_running:
                continue

            thread = self._watcher_thread_ref
            is_alive = thread is not None and thread.is_alive()
            age = time.time() - self._watcher_loop_alive_ts
            stalled = age > 120  # loop cycles every ~2s; 60x headroom

            if is_alive and not stalled:
                continue

            logger.critical(
                f"SL watcher thread "
                f"{'DIED' if not is_alive else f'IS STALLED ({age:.0f}s without a loop tick)'} - "
                f"stop-loss was NOT being enforced. Relaunching it now."
            )

            self._watcher_generation += 1
            self._watcher_loop_alive_ts = time.time()
            replacement = threading.Thread(
                target=self.stop_loss_book_profit_core,
                args=(self._broker,),
                daemon=True,
            )
            self._watcher_thread_ref = replacement
            replacement.start()            

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
        if self._mode == "PAPER":
            # PaperTrading.txt is a single global log: it keeps rows from
            # every underlying ever paper-traded. Starting a session for
            # a new underlying must purge the previous underlying's rows
            # first, otherwise they flow into the Orders tab and the
            # position grid with wrong lot sizes/tokens (and can break
            # the grid rendering).
            self.clear_paper_trades_for_other_underlyings()
            # PAPER mode: the position grid comes from the paper trade
            # log, NOT from live broker REST calls. refresh_open_pos_
            # buy_price() would overwrite/clear the paper positions.
            self.refresh_paper_positions()
        else:
            self.refresh_open_pos_buy_price()

    def clear_paper_trades_for_other_underlyings(self):
        """
        PAPER-mode startup cleanup: rewrite PaperTrading.txt keeping only
        rows that belong to the current session underlying (rows with an
        unclassifiable instrument are dropped as well). The original file
        is backed up alongside the log before anything is removed.
        """

        filename = self._PAPER_TRADE_FILENAME
        current_underlying = str(self._underlying or "").strip().upper()

        if not current_underlying:
            logger.warning(
                "No UNDERLYING configured; skipping paper trade cleanup"
            )
            return

        if not os.path.exists(filename):
            return

        with self._paper_trade_lock:

            try:
                with open(filename, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception as e:
                logger.warning(
                    f"Could not read {filename} for underlying "
                    f"cleanup: {e}"
                )
                return

            if not lines:
                return

            # Mirror fetch_paper_trades(): optional header line, then
            # fixed-column data rows. Instrument is column 3 (index 2).
            first_fields = lines[0].rstrip("\n").split("\t")
            header_is_present = first_fields[0].strip() == "Time"

            if header_is_present:
                header_line = lines[0]
                data_lines = lines[1:]
            else:
                header_line = None
                data_lines = lines

            kept_lines = []
            removed_count = 0

            for line in data_lines:

                if not line.strip():
                    continue

                fields = line.split("\t")
                symbol = fields[2].strip() if len(fields) > 2 else ""

                row_underlying = get_underlying_for_symbol(symbol)

                if row_underlying == current_underlying:
                    kept_lines.append(line)
                else:
                    removed_count += 1
                    logger.info(
                        f"Removing paper trade row for other/"
                        f"unrecognized underlying "
                        f"(row underlying: {row_underlying}): "
                        f"{symbol or '<blank>'}"
                    )

            if not removed_count:
                return

            backup_path = (
                f"{os.path.splitext(filename)[0]}_backup_"
                f"{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"
            )

            try:
                with open(backup_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                logger.info(
                    f"Paper trade log backed up to {backup_path}"
                )
            except Exception as e:
                logger.error(
                    f"Could not back up {filename}; aborting cleanup "
                    f"so no data is lost: {e}"
                )
                return

            try:
                with open(filename, "w", encoding="utf-8") as f:
                    if header_line is not None:
                        f.write(header_line)
                    f.writelines(kept_lines)
            except Exception as e:
                logger.error(
                    f"Could not rewrite {filename} during underlying "
                    f"cleanup: {e}"
                )
                return

            logger.info(
                f"Paper trade cleanup for {current_underlying}: "
                f"removed {removed_count} row(s) from "
                f"{len(data_lines)} total row(s)"
            )

            return

    

   #{'INDICATORS': {'15S': {'PVT': 0.97, 'PVTPoiseFlag': -1, 'PVTTrendFlag': 1, 'PVTCrossOverIndex': 4, 'PVTCrossUnderIndex': 5, 'Trend_1226': 1, 'X_1226': 15, 'Y_1226': 32, 
   #                'MACD_1226': 39.15, 'Signal_1226': 29.19, 'Hist_1226': 9.95, 'Trend_2452': 1, 'X_2452': 10, 'Y_2452': 29, 'MACD_2452': 50.72, 'Signal_2452': 43.56, 'Hist_2452': 7.16, 
   #                'ZCross': 0, 'K': 71.65, 'KTrendFlag': -1, 'StochPoiseFlag': -1, 'KCrossoverIndex': 21, 'KCrossUnderIndex': 5, 'EMA9': 103905, 'EMA21': 103864, 'EMA50': 103810, 
   #                'EMA100': 103728, 'EMA200': 103598, 'VWAP': 102244.27, 'VOC': 29.14, 'VOCTrendFlag': -1, 'VOCCrossOverIndex': 6, 'VOCCrossUnderIndex': 14}, 
   #
   #                '1M': {'PVT': 0.61, 'PVTPoiseFlag': 1, 'PVTTrendFlag': 1, 'PVTCrossOverIndex': 5, 'PVTCrossUnderIndex': 7, 'Trend_1226': 1, 'X_1226': 1, 'Y_1226': 6, 'MACD_1226': 87.8, 'Signal_1226': 87.36, 'Hist_1226': 0.44, 'Trend_2452': 1, 'X_2452': 44, 'Y_2452': 74, 'MACD_2452': 144.72, 'Signal_2452': 138.74, 'Hist_2452': 5.98, 'ZCross': 0, 'K': 36.27, 'KTrendFlag': 1, 'StochPoiseFlag': 1, 'KCrossoverIndex': 1, 'KCrossUnderIndex': 13, 'EMA9': 103828, 'EMA21': 103752, 'EMA50': 103598, 'EMA100': 103391, 'EMA200': 103061, 'VWAP': 102242.58, 'VOC': 21.48, 'VOCTrendFlag': 1, 'VOCCrossOverIndex': 1, 'VOCCrossUnderIndex': 3}, '5M': {'PVT': 0.24, 'PVTPoiseFlag': 1, 'PVTTrendFlag': 1, 'PVTCrossOverIndex': 34, 'PVTCrossUnderIndex': 40, 'Trend_1226': 1, 'X_1226': 4, 'Y_1226': 12, 'MACD_1226': 274.3, 'Signal_1226': 253.67, 'Hist_1226': 20.63, 'Trend_2452': 1, 'X_2452': 33, 'Y_2452': 36, 'MACD_2452': 405.67, 'Signal_2452': 374.73, 'Hist_2452': 30.95, 'ZCross': 0, 'K': 69.72, 'KTrendFlag': 1, 'StochPoiseFlag': 1, 'KCrossoverIndex': 7, 'KCrossUnderIndex': 16, 'EMA9': 103643, 'EMA21': 103392, 'EMA50': 102953, 'EMA100': 102571, 'EMA200': 102302, 'VWAP': 102240.71, 'VOC': -7.05, 'VOCTrendFlag': 1, 'VOCCrossOverIndex': 45, 'VOCCrossUnderIndex': 10}, '30M': {'PVT': 1, 'PVTPoiseFlag': 1, 'PVTTrendFlag': 1, 'PVTCrossOverIndex': 9, 'PVTCrossUnderIndex': 13, 'Trend_1226': 1, 'X_1226': 20, 'Y_1226': 30, 'MACD_1226': 382.46, 'Signal_1226': 191.07, 'Hist_1226': 191.38, 'Trend_2452': 1, 'X_2452': 10, 'Y_2452': 12, 'MACD_2452': 213.28, 'Signal_2452': 72.33, 'Hist_2452': 140.94, 'ZCross': 0, 'K': 97.07, 'KTrendFlag': 1, 'StochPoiseFlag': 1, 'KCrossoverIndex': 7, 'KCrossUnderIndex': 11, 'EMA9': 102959, 'EMA21': 102497, 'EMA50': 102232, 'EMA100': 102199, 'EMA200': 102691, 'VWAP': 102239.26, 'VOC': 16.28, 'VOCTrendFlag': -1, 'VOCCrossOverIndex': 8, 'VOCCrossUnderIndex': 10}, '2H': {'PVT': 0.25, 'PVTPoiseFlag': 1, 'PVTTrendFlag': 1, 'PVTCrossOverIndex': 2, 'PVTCrossUnderIndex': 6, 'Trend_1226': 1, 'X_1226': 2, 'Y_1226': 6, 'MACD_1226': 39.07, 'Signal_1226': -87.12, 'Hist_1226': 126.19, 'Trend_2452': 1, 'X_2452': 24, 'Y_2452': 26, 'MACD_2452': -554.26, 'Signal_2452': -697.93, 'Hist_2452': 143.68, 'ZCross': 0, 'K': 51.58, 'KTrendFlag': 1, 'StochPoiseFlag': 1, 'KCrossoverIndex': 2, 'KCrossUnderIndex': 16, 'EMA9': 102320, 'EMA21': 102195, 'EMA50': 102697, 'EMA100': 104320, 'EMA200': 106715, 'VWAP': 102236.68, 'VOC': -6.56, 'VOCTrendFlag': 1, 'VOCCrossOverIndex': 25, 'VOCCrossUnderIndex': 20}, 'D': {'PVT': -0.57, 'PVTPoiseFlag': 1, 'PVTTrendFlag': -1, 'PVTCrossOverIndex': 40, 'PVTCrossUnderIndex': 31, 'Trend_1226': -1, 'X_1226': 15, 'Y_1226': 7, 'MACD_1226': -2783.24, 'Signal_1226': -2130.73, 'Hist_1226': -652.51, 'Trend_2452': -1, 'X_2452': 39, 'Y_2452': 29, 'MACD_2452': -2627.45, 'Signal_2452': -1943.24, 'Hist_2452': -684.21, 'ZCross': 0, 'K': 4.31, 'KTrendFlag': -1, 'StochPoiseFlag': -1, 'KCrossoverIndex': 20, 'KCrossUnderIndex': 10, 'EMA9': 104564, 'EMA21': 107442, 'EMA50': 110461, 'EMA100': 111285, 'EMA200': 108040, 'VWAP': 102771.11, 'VOC': -3.22, 'VOCTrendFlag': -1, 'VOCCrossOverIndex': 6, 'VOCCrossUnderIndex': 1}, 'W': {'PVT': -0.5, 'PVTPoiseFlag': -1, 'PVTTrendFlag': -1, 'PVTCrossOverIndex': 28, 'PVTCrossUnderIndex': 4, 'Trend_1226': -1, 'X_1226': 26, 'Y_1226': 11, 'MACD_1226': 2937.8, 'Signal_1226': 4649.85, 'Hist_1226': -1712.05, 'Trend_2452': -1, 'X_2452': 25, 'Y_2452': 9, 'MACD_2452': 9634.24, 'Signal_2452': 10667.07, 'Hist_2452': -1032.83, 'ZCross': 0, 'K': 13.96, 'KTrendFlag': -1, 'StochPoiseFlag': -1, 'KCrossoverIndex': 4, 'KCrossUnderIndex': 3, 'EMA9': 111835, 'EMA21': 110478, 'EMA50': 100727, 'EMA100': 84918, 'EMA200': 65364, 'VWAP': 105734.63, 'VOC': 2.56, 'VOCTrendFlag': 1, 'VOCCrossOverIndex': 1, 'VOCCrossUnderIndex': 2}}}

    def _is_tv_validation_required(self):
        val = fetch_from_json("appconfig.json", "TV_DATA_Validation_REQUIRED")
        if isinstance(val, str):
            return val.strip().upper() in ("TRUE", "1", "YES", "Y", "ON")
        return bool(val)

    def _update_tv_buy_validation(self, data):
        """
        Evaluate TradingView entry validation and update CE/PE buy permissions.

        CE buy allowed when 30M PVT > 0 OR (5M PVT > 0 AND 1m close > 1M VWAP).
        PE buy allowed when 30M PVT < 0 OR (5M PVT < 0 AND 1m close < 1M VWAP).
        1m close comes from the webhook's 1M.CLOSE when present, else the
        near-month future LTP. Fail-safe: any missing/invalid input blocks
        the affected permission(s).
        """
        try:
            indicators = data.get("INDICATORS", {}) or {}
            pvt_30m = float(indicators["30M"]["PVT"])
            pvt_5m = float(indicators["5M"]["PVT"])
            vwap_1m = float(indicators["1M"]["VWAP"])

            close_1m = indicators["1M"].get("CLOSE")
            if close_1m is None:
                close_1m = self._near_month_future_ltp or self.ltp_near_month_future
            close_1m = float(close_1m)
            close_valid = close_1m > 0

            ce_via_30m = pvt_30m > 0
            ce_via_5m = close_valid and pvt_5m > 0 and close_1m > vwap_1m
            pe_via_30m = pvt_30m < 0
            pe_via_5m = close_valid and pvt_5m < 0 and close_1m < vwap_1m

            new_ce_allowed = ce_via_30m or ce_via_5m
            new_pe_allowed = pe_via_30m or pe_via_5m

            if ce_via_30m:
                ce_reason = f"30M PVT {pvt_30m:g} > 0"
            elif ce_via_5m:
                ce_reason = f"5M PVT {pvt_5m:g} > 0 and 1m close {close_1m:g} > 1M VWAP {vwap_1m:g}"
            elif close_valid:
                ce_reason = f"30M PVT {pvt_30m:g} not > 0 and (5M PVT {pvt_5m:g} not > 0 or 1m close {close_1m:g} not > 1M VWAP {vwap_1m:g})"
            else:
                ce_reason = f"30M PVT {pvt_30m:g} not > 0 and 1m close unavailable (underlying LTP {close_1m:g})"

            if pe_via_30m:
                pe_reason = f"30M PVT {pvt_30m:g} < 0"
            elif pe_via_5m:
                pe_reason = f"5M PVT {pvt_5m:g} < 0 and 1m close {close_1m:g} < 1M VWAP {vwap_1m:g}"
            elif close_valid:
                pe_reason = f"30M PVT {pvt_30m:g} not < 0 and (5M PVT {pvt_5m:g} not < 0 or 1m close {close_1m:g} not < 1M VWAP {vwap_1m:g})"
            else:
                pe_reason = f"30M PVT {pvt_30m:g} not < 0 and 1m close unavailable (underlying LTP {close_1m:g})"

            if new_ce_allowed != self._tv_ce_buy_allowed or new_pe_allowed != self._tv_pe_buy_allowed:
                logger.info(f"TradingView buy validation changed - CE allowed: {new_ce_allowed} ({ce_reason}) | PE allowed: {new_pe_allowed} ({pe_reason})")

            self._tv_ce_buy_allowed = new_ce_allowed
            self._tv_pe_buy_allowed = new_pe_allowed
            self._tv_ce_reason = ce_reason
            self._tv_pe_reason = pe_reason

        except Exception as e:
            self._tv_ce_buy_allowed = False
            self._tv_pe_buy_allowed = False
            self._tv_ce_reason = f"Invalid TradingView data: {e}"
            self._tv_pe_reason = f"Invalid TradingView data: {e}"
            logger.warning(f"TradingView buy validation failed, blocking CE and PE buys: {e}")

    def process_TradingView_Data(self , data):
        try:
            recommendation = ""
            tradeRecomDescription = ""
            #logger.info(f"TradingView Data received: {data}")
            self._tradingView_Data = data
            logger.debug(f"TradingView Data is assigned to internal variable {self._tradingView_Data}")
            #logger.debug(f"1m EMA 50:{data['EMA']['1m']['EMA50']}") 

            # TradingView entry validation (TV_DATA_Validation_REQUIRED)
            self._tv_validation_required = self._is_tv_validation_required()
            self._update_tv_buy_validation(data)

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
                "TradeSetups": trade_setups,
                "TVValidationRequired": self._tv_validation_required,
                "CE_BUY_ALLOWED": self._tv_ce_buy_allowed,
                "PE_BUY_ALLOWED": self._tv_pe_buy_allowed,
                "CE_REASON": self._tv_ce_reason,
                "PE_REASON": self._tv_pe_reason
            }

            # ✅ Emit event to frontend (Socket.IO)
            self.frontend_data_socket.emit("update_trading_view_data", json_data)

            logger.debug(f"Emitted TradeSetups: {trade_setups}")

            # Optional: also return for backend usage/testing
            return trade_setups

        except Exception as e:
            logger.error(f"TradingView Data processing failed: {e}")    
            # Fail-safe: block both sides and push the blocked state to the frontend
            self._tv_ce_buy_allowed = False
            self._tv_pe_buy_allowed = False
            self._tv_ce_reason = f"TradingView data processing failed: {e}"
            self._tv_pe_reason = f"TradingView data processing failed: {e}"
            try:
                self.frontend_data_socket.emit("update_trading_view_data", {
                    "TradingView_Data": self._tradingView_Data,
                    "TradeSetups": [],
                    "TVValidationRequired": self._is_tv_validation_required(),
                    "CE_BUY_ALLOWED": False,
                    "PE_BUY_ALLOWED": False,
                    "CE_REASON": self._tv_ce_reason,
                    "PE_REASON": self._tv_pe_reason
                })
            except Exception as emit_err:
                logger.error(f"Failed to emit blocked TradingView validation state: {emit_err}")
            return []
        

    def get_open_position_buy_time(self, token, tradingsymbol=None, strategy=None, sell_type=None):
        """
        Find the latest BUY time for a position leg from the paper trade log.

        Matches by tradingsymbol (+ strategy/sell type when the trade
        rows carry them) so per-leg durations are computed from the
        correct BUY row, falling back to symbol-then-token matching.
        """
        trades = self.fetch_paper_trades()

        token = str(token)
        symbol = str(tradingsymbol).strip().upper() if tradingsymbol else None
        strategy = str(strategy).strip().upper() if strategy else None
        sell_type = str(sell_type).strip().upper() if sell_type else None

        def _row_strategy(trade):
            return str(trade.get("Strategy", "") or "").strip().upper()

        def _row_sell_type(trade):
            return str(trade.get("SellType", "") or "").strip().upper()

        def _latest_buy_time(match_symbol=None, match_token=None,
                             match_strategy=None, match_sell_type=None):
            for trade in reversed(trades):
                if match_symbol is not None:
                    if str(trade.get("Instrument", "")).strip().upper() != match_symbol:
                        continue
                if match_token is not None:
                    if str(trade.get("Token")) != match_token:
                        continue
                if trade.get("Type", "").upper() != "BUY":
                    continue
                if match_strategy is not None and _row_strategy(trade) != match_strategy:
                    continue
                if match_sell_type is not None and _row_sell_type(trade) != match_sell_type:
                    continue
                buy_time = trade.get("Time")
                if buy_time:
                    return buy_time
            return None

        buy_time = None
        if symbol:
            # Most specific first: symbol + strategy + sell type.
            if strategy or sell_type:
                buy_time = _latest_buy_time(
                    match_symbol=symbol,
                    match_strategy=strategy,
                    match_sell_type=sell_type
                )
            if buy_time is None:
                buy_time = _latest_buy_time(match_symbol=symbol)
        if buy_time is None:
            buy_time = _latest_buy_time(match_token=token)

        if buy_time:
            logger.debug(f"Latest BUY time for {symbol or token}: {buy_time}")
            return buy_time

        logger.warning(f"No BUY time found for {symbol or token}")
        return None


    def write_paper_trade(
        self,
        transaction_type,
        tradingsymbol,
        token,
        qty,
        ltp,
        product="MIS",
        buy_price=None,
        lot_size=1,
        buy_time=None,
        strategy="",
        sell_type="T"
    ):
        """
        Append one row to the paper trade file.

        Shared by the buy/sell socket handlers and the auto-sell watcher
        so every writer produces the identical 14-column format that
        fetch_paper_trades() parses. Repairs a missing header line so
        legacy header-less files become readable again.

        Strategy/SellType tag each row with its position leg
        (token:STRATEGY:MODE) so paper position aggregation can keep
        legs separate and sells close the correct leg.
        """

        filename = self._PAPER_TRADE_FILENAME

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
        WIDTH_PNL_PCT    = 12
        WIDTH_PNL        = 16
        WIDTH_STRATEGY   = 16
        WIDTH_SELL_TYPE  = 10

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
            "PnL".rjust(WIDTH_PNL),
            "Strategy".ljust(WIDTH_STRATEGY),
            "SellType".ljust(WIDTH_SELL_TYPE)
        ]

        header_line = "\t".join(headers)

        # ---------------------------------------------------------
        # Normalize leg tags so every writer records the same
        # canonical values the aggregator matches on.
        # ---------------------------------------------------------

        strategy = self._normalise_strategy(strategy)
        sell_type = str(sell_type or "T").strip().upper()

        # ---------------------------------------------------------
        # Default PnL values (BUY rows keep these blank)
        # ---------------------------------------------------------

        pnl_rate = ""
        pnl = ""
        pnl_pct = ""
        duration = ""

        # ---------------------------------------------------------
        # Calculate PnL ONLY for SELL. Duration is optional: a missing
        # or unparsable buy_time must not fail the SELL record.
        # ---------------------------------------------------------

        if transaction_type.upper() == "SELL" and buy_price is not None:

            sell_time = datetime.datetime.now()

            parsed_buy_time = None
            if isinstance(buy_time, datetime.datetime):
                parsed_buy_time = buy_time
            elif isinstance(buy_time, str) and buy_time.strip():
                try:
                    parsed_buy_time = datetime.datetime.strptime(
                        buy_time.strip(),
                        "%m/%d/%Y %H:%M:%S"
                    )
                except ValueError:
                    logger.warning(
                        f"Unparsable buy_time '{buy_time}' for "
                        f"{tradingsymbol}; duration will be blank"
                    )

            if parsed_buy_time is not None:
                elapsed = sell_time - parsed_buy_time
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
            quantity = int(qty) * int(lot_size)

            # PnL Rate = Sell Price - Buy Price
            pnl_rate_value = sell_price - buy_price

            # PnL = (Sell Price - Buy Price) * Qty (units)
            pnl_value = pnl_rate_value * quantity

            # PnL % = (Sell Price - Buy Price) * 100 / Buy Price
            pnl_pct_value = (
                (pnl_rate_value * 100) / buy_price
                if buy_price != 0
                else 0
            )

            pnl_rate = f"{pnl_rate_value:.2f}"
            pnl = f"{pnl_value:,.0f}"
            pnl_pct = f"{pnl_pct_value:.2f}"

        # ---------------------------------------------------------
        # Timestamp (platform independent: no %-m / %#m directives)
        # ---------------------------------------------------------

        now = datetime.datetime.now()
        timestamp = f"{now.month}/{now.day}/{now.year} {now:%H:%M:%S}"

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
            pnl_rate.rjust(WIDTH_PNL_RATE),
            pnl_pct.rjust(WIDTH_PNL_PCT),
            pnl.rjust(WIDTH_PNL),
            strategy.ljust(WIDTH_STRATEGY),
            sell_type.ljust(WIDTH_SELL_TYPE)
        ]

        # ---------------------------------------------------------
        # Write transaction. Header check + repair + append run under
        # a lock so the watcher thread and the socket handlers can
        # never interleave a repair with a row append.
        # ---------------------------------------------------------

        with self._paper_trade_lock:

            needs_header = True
            if os.path.exists(filename):
                try:
                    with open(filename, "r", encoding="utf-8") as f:
                        first_line = f.readline()
                    first_fields = first_line.rstrip("\n").split("\t")
                    # Repair when the header line is missing OR when it
                    # predates the current column set (e.g. legacy
                    # 12-column headers without Strategy/SellType):
                    # rows written in the new 14-column format would
                    # otherwise be skipped on read.
                    if (
                        first_fields[0].strip() == "Time"
                        and len(first_fields) == len(self._PAPER_TRADE_HEADERS)
                    ):
                        needs_header = False
                except Exception as e:
                    logger.warning(f"Could not inspect {filename}: {e}")

            if needs_header:
                existing = ""
                if os.path.exists(filename):
                    try:
                        with open(filename, "r", encoding="utf-8") as f:
                            existing = f.read()
                    except Exception as e:
                        logger.warning(
                            f"Could not read {filename} for header repair: {e}"
                        )

                with open(filename, "w", encoding="utf-8") as f:
                    f.write(header_line + "\n")
                    if existing:
                        f.write(existing)

                logger.info("PaperTrading.txt header written/repaired")

            with open(filename, "a", encoding="utf-8") as f:
                f.write("\t".join(values) + "\n")

    def fetch_paper_trades(self):

        filename = self._PAPER_TRADE_FILENAME

        if not os.path.exists(filename):
            return []

        trades = []

        try:

            with open(filename, "r", encoding="utf-8") as f:
                lines = f.readlines()

            if not lines:
                return []

            # ---------------------------------------------------------
            # Read header.
            #
            # Legacy files were written without a header line (the
            # first line is a DATA row). In that case fall back to the
            # standard header and parse every line as data.
            # ---------------------------------------------------------

            first_fields = lines[0].rstrip("\n").split("\t")
            header_is_present = first_fields[0].strip() == "Time"

            if header_is_present:
                headers = [h.strip() for h in first_fields]
                data_lines = lines[1:]
            else:
                headers = list(self._PAPER_TRADE_HEADERS)
                data_lines = lines
                logger.warning(
                    "PaperTrading.txt has no header line; "
                    "using default header for all rows"
                )

            logger.debug(f"PaperTrading headers: {headers}")

            # ---------------------------------------------------------
            # Read transactions
            # ---------------------------------------------------------

            for line_number, line in enumerate(
                data_lines,
                start=2 if header_is_present else 1
            ):

                line = line.rstrip("\n")

                if not line.strip():
                    continue

                values = line.split("\t")

                # -----------------------------------------------------
                # Legacy rows written by older formats (8-column inline
                # writers, or the previous 12-column standard before
                # Strategy/SellType were added): pad the missing
                # trailing columns so they still parse.
                # -----------------------------------------------------

                if len(values) < len(headers):
                    values = values + [""] * (len(headers) - len(values))

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
                # Default leg tags for rows written before the
                # Strategy/SellType columns existed.
                # -----------------------------------------------------

                if not trade.get("Strategy"):
                    trade["Strategy"] = "ULTRA_SCALPING"

                if not trade.get("SellType"):
                    trade["SellType"] = "T"

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

    def fetch_paper_orders(self):
        """
        Paper trades as normalized broker-order dicts (quantity in lots
        plus an explicit lot_size) so the Orders grid and Excel export
        render paper fills through the same pipeline as broker fills.
        """
        orders = []

        for trade in self.fetch_paper_trades():

            try:
                symbol = str(trade["Instrument"]).strip()
                qty = int(str(trade["Qty."]).split("/")[0])
                price = float(trade["Avg. price"])
                transaction_type = str(trade["Type"]).strip().upper()
                timestamp = datetime.datetime.strptime(
                    str(trade["Time"]).strip(),
                    "%m/%d/%Y %H:%M:%S"
                )
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(
                    f"Skipping invalid paper trade for orders grid: {e}"
                )
                continue

            if not symbol:
                continue

            lot_size = 1
            try:
                instrument_data = self._broker.get_instrument_details(
                    symbol
                )
                if instrument_data:
                    lot_size = int(
                        instrument_data.get("lot_size", 1) or 1
                    )
            except Exception as e:
                logger.warning(
                    f"Lot size lookup failed for paper order "
                    f"{symbol}: {e}"
                )

            orders.append({
                "timestamp": timestamp,
                "tradingsymbol": symbol,
                "transaction_type": transaction_type,
                "quantity": qty,
                "average_price": price,
                "order_status": "COMPLETE",
                "status": "COMPLETE",
                "lot_size": lot_size,
            })

        return orders

    def calculate_paper_positions(self):
        """
        Aggregate paper trades into net positions keyed by
        (tradingsymbol, strategy, sell type).

        Keying by symbol keeps accounting correct when the same
        instrument was recorded under different broker/exchange token
        spaces (e.g. after switching BROKER or UNDERLYING); adding
        strategy/sell type to the key keeps each position leg separate
        so same-contract trades with different strategies show as
        distinct rows. Sells reduce the matching leg first.
        """

        trades = self.fetch_paper_trades()

        positions = {}

        for trade in trades:

            try:
                symbol = str(trade["Instrument"]).strip()
                token = str(trade["Token"]).strip()

                qty = int(str(trade["Qty."]).split("/")[0])
                price = float(trade["Avg. price"])
                transaction_type = trade["Type"].upper()

            except Exception as e:
                logger.warning(
                    f"Skipping invalid paper trade {trade}: {e}"
                )
                continue

            if not symbol:
                continue

            strategy = self._normalise_strategy(trade.get("Strategy"))
            sell_type = str(trade.get("SellType", "") or "").strip().upper() or "T"

            position_key = f"{symbol}|{strategy}|{sell_type}"

            if position_key not in positions:
                positions[position_key] = {
                    "tradingsymbol": symbol,
                    "instrument_token": token,
                    "strategy": strategy,
                    "sell_type": sell_type,
                    "buy_quantity": 0,
                    "sell_quantity": 0,
                    "buy_value": 0.0,
                    "sell_value": 0.0,
                }

            position = positions[position_key]

            if transaction_type == "BUY":

                position["buy_quantity"] += qty
                position["buy_value"] += qty * price

            elif transaction_type == "SELL":

                position["sell_quantity"] += qty
                position["sell_value"] += qty * price

        return positions

    def _get_paper_initial_cash(self):
        try:
            value = fetch_from_json("appconfig.json", "CASH_BALANCE_PAPER_TRADING")
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
        return 100000.0

    def _calculate_paper_invested(self):
        invested = 0.0
        for pos in self._position_data.values():
            try:
                invested += (
                    float(pos.get("average_price", 0) or 0)
                    * int(pos.get("net_quantity", 0) or 0)
                    * int(pos.get("lotsize", 1) or 1)
                )
            except (TypeError, ValueError):
                continue
        return invested

    def _calculate_paper_realized_pnl(self, trades):
        realized_pnl = 0.0
        for trade in trades:
            if str(trade.get("Type", "")).upper() != "SELL":
                continue
            raw_pnl = str(trade.get("PnL", "") or "").replace(",", "").strip()
            if not raw_pnl:
                continue
            try:
                realized_pnl += float(raw_pnl)
            except ValueError:
                logger.warning(
                    f"Skipping unparsable paper PnL '{trade.get('PnL')}' "
                    f"for {trade.get('Instrument')}"
                )
        return realized_pnl

    def _get_paper_available_cash(self):
        return (
            self._get_paper_initial_cash()
            + float(self._paper_realized_pnl or 0)
            - self._calculate_paper_invested()
        )

    def get_paper_fund_summary(self):
        """
        Paper-account fund summary mirroring the broker payload shape
        (cash_balance) so the UI displays paper funds in PAPER mode.
        """
        realized_pnl = self._calculate_paper_realized_pnl(
            self.fetch_paper_trades()
        )
        self._paper_realized_pnl = realized_pnl

        initial_cash = self._get_paper_initial_cash()
        invested = self._calculate_paper_invested()
        available = initial_cash + realized_pnl - invested

        return {
            "cash_balance": round(available, 2),
            "initial_cash": round(initial_cash, 2),
            "realized_pnl": round(realized_pnl, 2),
            "invested": round(invested, 2),
            "mode": "PAPER",
        }

    def refresh_paper_positions(self):

        logger.info("========================================")
        logger.info("Refreshing PAPER positions")
        logger.info("========================================")

        trades = self.fetch_paper_trades()

        logger.info(f"Paper trades read: {len(trades)}")
        logger.debug(f"Paper trades: {trades}")

        self._paper_realized_pnl = self._calculate_paper_realized_pnl(trades)

        paper_positions = self.calculate_paper_positions()

        logger.info(f"Calculated paper positions: {paper_positions}")

        new_position_data = {}

        for position_key, position in paper_positions.items():

            symbol = position["tradingsymbol"]
            net_quantity = (
                position["buy_quantity"]
                - position["sell_quantity"]
            )

            logger.debug(
                f"Symbol={symbol} "
                f"Strategy={position.get('strategy')} "
                f"SellType={position.get('sell_type')} "
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

            # ---------------------------------------------------------
            # Resolve the instrument through the ACTIVE broker master:
            #   1. correct lot size for PnL math
            #   2. re-bind the position to the active broker's token
            #      space so websocket ticks keep updating positions
            #      recorded under a different broker/exchange earlier.
            # Fall back to the token recorded in the trade file when
            # the instrument is not in today's master.
            # ---------------------------------------------------------

            lotsize = 1
            exchange = self._exchange
            active_token = str(position["instrument_token"])

            instrument_data = None
            try:
                instrument_data = self._broker.get_instrument_details(
                    symbol
                )
            except Exception as e:
                logger.warning(
                    f"Could not get instrument details for "
                    f"{symbol}: {e}"
                )

            if instrument_data:
                try:
                    lotsize = int(
                        instrument_data.get("lot_size", 1) or 1
                    )
                    resolved_token = str(
                        instrument_data.get("instrument_token") or ""
                    ).strip()
                    if resolved_token:
                        active_token = resolved_token
                    resolved_exchange = str(
                        instrument_data.get("exchange") or ""
                    ).strip()
                    if resolved_exchange:
                        exchange = resolved_exchange
                except Exception as e:
                    logger.warning(
                        f"Instrument detail parsing failed for "
                        f"{symbol}: {e}"
                    )
            else:
                logger.warning(
                    f"Instrument {symbol} not found in the active "
                    f"broker instrument master. Using lotsize=1 and "
                    f"the token recorded in the paper trade file; PnL "
                    f"and lot sizing for this position may be wrong."
                )

            latest_price = self.get_latest_price(active_token)

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

            # net_quantity is in LOTS - multiply by lotsize for units,
            # consistent with position_data_update() and
            # write_paper_trade().
            total_pl = pts_pl * net_quantity * lotsize

            # Composite leg key so same-contract trades with different
            # strategies/sell types stay separate rows in the grid.
            leg_key = self._position_key(
                active_token,
                position.get("strategy"),
                position.get("sell_type")
            )

            new_position_data[leg_key] = {
                "tradingsymbol": position["tradingsymbol"],
                "average_price": average_buy_price,
                "net_quantity": net_quantity,
                "latest_price": latest_price,
                "instrument_token": active_token,
                "exchange": exchange,
                "lotsize": lotsize,
                "lots": net_quantity,
                "pts_pl": pts_pl,
                "pct_pl": pct_pl,
                "total_pl": total_pl,
                "order_strategy": str(
                    position.get("strategy") or "ULTRA_SCALPING"
                ).upper(),
                "sell_mode": str(
                    position.get("sell_type") or "T"
                ).upper()
            }

            contract = self._five_weekly_option_contracts.get(
                str(active_token)
            )
            if contract and contract.get("level"):
                new_position_data[leg_key]["level"] = contract["level"]

            # Paper rebuilds the whole dict every refresh - carry the
            # user's per-position BK% override across rebuilds, seeded
            # with the strategy default on first appearance.
            previous_leg = self._position_data.get(leg_key, {})
            new_position_data[leg_key]["override_book_profit_pct"] = (
                previous_leg.get(
                    "override_book_profit_pct",
                    self._default_override_book_profit_pct(
                        new_position_data[leg_key]["order_strategy"]
                    )
                )
            )

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

    def _shutdown_if_no_browser(self):
        """
        Browser/app window closed and no frontend client reconnected
        within the grace window: stop the application so the console
        windows (server + logs) close along with it.
        """
        if self._browser_sids:
            return

        logger.info(
            "App window closed - no browser clients remain. "
            "Shutting down the application."
        )
        try:
            self._stop_event.set()
        except Exception:
            pass
        time.sleep(0.5)
        os._exit(0)

    def register_socket_handlers(self):
        logger.info("Registering socket event handlers...")

        @self.frontend_data_socket.on('connect')
        def handle_connect():
            self._browser_sids.add(request.sid)
            logger.info(f"MSTOCK SOCKET CONNECTED | sid={request.sid} | browser clients={len(self._browser_sids)}")
            logger.debug(self._near_month_data)
            payload = {
                    "broker" : self._broker_string ,
                    "exchange" : self._exchange ,
                    "underlying" : self._underlying,
                    "positions_data" : self._position_data,
                    "order_strategy_mapping" : self._order_strategy_mapping,
                    "order_sell_mode_mapping" : self._order_sell_mode_mapping,
                    "mode" : self._mode
                }
            setup_payload = {
                    "broker" : self._broker_string ,
                    "exchange" : self._exchange ,
                    "underlying" : self._underlying,
                    "positions_data" : self._position_data,
                    "order_strategy_mapping" : self._order_strategy_mapping,
                    "order_sell_mode_mapping" : self._order_sell_mode_mapping,
                    "mode" : self._mode,
                    "TVValidationRequired" : self._is_tv_validation_required(),
                    "CE_BUY_ALLOWED" : self._tv_ce_buy_allowed,
                    "PE_BUY_ALLOWED" : self._tv_pe_buy_allowed,
                    "CE_REASON" : self._tv_ce_reason,
                    "PE_REASON" : self._tv_pe_reason
                }

            # In playback mode, share the replay time bounds so the
            # dashboard can constrain its date/time picker.
            if self._mode == "PLAYBACK" and hasattr(self._broker, "get_playback_status"):
                try:
                    playback_status = self._broker.get_playback_status()
                    setup_payload["playback"] = playback_status
                except Exception as playback_error:
                    logger.warning(f"Could not fetch playback status for setup_data: {playback_error}")

            self.frontend_data_socket.emit(
                'setup_data',
                setup_payload
            )

                        # Send the last known near-month future price to a newly connected frontend
            # _near_month_future_ltp is set per websocket tick; when no tick
            # has arrived yet (e.g. weekend / ws down) fall back to the
            # setup-seeded ltp_near_month_future so a value is always sent.
            ltp = self._near_month_future_ltp or self.ltp_near_month_future

            if ltp and self.open_near_month_future:
                prev_close = None

                for token in self._near_month_data:
                    prev_close = self._prev_close_prices.get(token)
                    if prev_close:
                        break

                prev_close = (
                    prev_close
                    or self.prev_close_near_month_future
                    or self.open_near_month_future
                )

                chgO = round(
                    (ltp - prev_close)
                    * 100
                    / prev_close,
                    2
                ) if prev_close else 0.0

                self.frontend_data_socket.emit(
                    'near-month-ltp-updated',
                    {
                        'ltp': ltp,
                        'chgO': chgO,
                        'ptsChg': round(ltp - prev_close, 2) if prev_close else 0.0
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
            self._browser_sids.discard(request.sid)
            logger.info("Client disconnected.")
            logger.info(
                f"MSTOCK SOCKET DISCONNECTED | sid={request.sid} | "
                f"browser clients={len(self._browser_sids)}"
            )
            if not self._browser_sids:
                # Last window gone: allow brief reconnects (page
                # refresh / tab switch) before shutting down.
                threading.Timer(
                    self._BROWSER_EXIT_GRACE_SECONDS,
                    self._shutdown_if_no_browser
                ).start()

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
            if self._mode == "PAPER":
                # PAPER mode: rebuild from the paper trade log. The live
                # REST refresh would wipe the paper position grid.
                self.refresh_paper_positions()
                self.refresh_subscriptions()
            else:
                self.refresh_open_pos_buy_price()
            self.setup_woc_subscriptions()

        # =========================================================
        # PLAYBACK CONTROLS
        # =========================================================
        def _emit_playback_status():
            if hasattr(self._broker, "get_playback_status"):
                self.frontend_data_socket.emit(
                    'playback_status',
                    self._broker.get_playback_status()
                )

        @self.frontend_data_socket.on('start_playback')
        def handle_start_playback(payload):
            """Starts price replay from the user-selected date/time."""
            logger.info(f"Received playback start request: {payload}")
            try:
                if self._mode != "PLAYBACK":
                    self.frontend_data_socket.emit(
                        'status_message',
                        {"success": False, "message": "Playback is only available in PLAYBACK mode"}
                    )
                    return

                start_datetime = (payload or {}).get("start_datetime")
                if not start_datetime:
                    self.frontend_data_socket.emit(
                        'status_message',
                        {"success": False, "message": "Select a playback date and time first"}
                    )
                    return

                playback_symbol = str(self._broker.get_playback_instrument_name()).upper()

                # Requirement: if the playback instrument is not present in
                # the buy dashboard, flash an error in the status box.
                matching_tokens = [
                    token
                    for token, data in self._five_weekly_option_contracts.items()
                    if str(data.get("name", "")).upper() == playback_symbol
                ]
                if not matching_tokens:
                    self.frontend_data_socket.emit(
                        'status_message',
                        {"success": False, "message": f"Playback instrument {playback_symbol} not found in the buy dashboard"}
                    )
                    return

                actual_start = self._broker.start_playback(start_datetime)

                self.frontend_data_socket.emit(
                    'status_message',
                    {"success": True, "message": f"Playback started from {actual_start:%Y-%m-%d %H:%M:%S}"}
                )
                _emit_playback_status()
            except Exception as playback_error:
                logger.error(f"Error starting playback: {playback_error}", exc_info=True)
                self.frontend_data_socket.emit(
                    'status_message',
                    {"success": False, "message": f"Playback start failed: {playback_error}"}
                )

        @self.frontend_data_socket.on('pause_playback')
        def handle_pause_playback():
            logger.info("Received playback pause request")
            self._broker.pause_playback()
            self.frontend_data_socket.emit(
                'status_message',
                {"success": True, "message": "Playback paused"}
            )
            _emit_playback_status()

        @self.frontend_data_socket.on('resume_playback')
        def handle_resume_playback():
            logger.info("Received playback resume request")
            self._broker.resume_playback()
            self.frontend_data_socket.emit(
                'status_message',
                {"success": True, "message": "Playback resumed"}
            )
            _emit_playback_status()

        @self.frontend_data_socket.on('stop_playback')
        def handle_stop_playback():
            logger.info("Received playback stop request")
            self._broker.stop_playback()
            self.frontend_data_socket.emit(
                'status_message',
                {"success": True, "message": "Playback stopped"}
            )
            _emit_playback_status()

        @self.frontend_data_socket.on('set_playback_speed')
        def handle_set_playback_speed(payload):
            speed = (payload or {}).get("speed", 1)
            logger.info(f"Received playback speed request: {speed}")
            try:
                self._broker.set_playback_speed(speed)
                self.frontend_data_socket.emit(
                    'status_message',
                    {"success": True, "message": f"Playback speed set to {self._broker.get_playback_status().get('speed')}x"}
                )
                _emit_playback_status()
            except Exception as speed_error:
                self.frontend_data_socket.emit(
                    'status_message',
                    {"success": False, "message": f"Could not set playback speed: {speed_error}"}
                )

        @self.frontend_data_socket.on('request_playback_status')
        def handle_request_playback_status():
            _emit_playback_status()


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

            position_key = str(order_strategy_details.get("position_key") or "")

            if position_key and ":" in position_key:
                # Per-leg strategy switch: only this leg's TP/SL
                # thresholds change.
                self.position_data_update(
                    position_key,
                    "order_strategy",
                    order_strategy_details["strategy_type"]
                )
                # The BK% box follows the strategy: reset the per-position
                # override to the new strategy's default.
                leg = self._position_data.get(position_key)
                if leg is not None:
                    leg["override_book_profit_pct"] = self._default_override_book_profit_pct(
                        order_strategy_details["strategy_type"]
                    )
            else:
                # Legacy payload without a leg key: apply to every open
                # leg of the instrument.
                self.position_data_update(
                    order_strategy_details["instrument_token"],
                    "order_strategy",
                    order_strategy_details["strategy_type"]
                )
                for key in self._legs_for_token(
                    str(order_strategy_details.get("instrument_token", ""))
                ):
                    self._position_data[key]["override_book_profit_pct"] = (
                        self._default_override_book_profit_pct(
                            order_strategy_details["strategy_type"]
                        )
                    )

            logger.debug(f"Updated positions data: {self._position_data}")

            if self.frontend_data_socket:
                self.frontend_data_socket.emit(
                    'update_open_positions',
                    self._position_data
                )

        @self.frontend_data_socket.on("order_strategy_data_updated_orders")
        def handle_order_strategy_type_orders(order_strategy_details):
            logger.debug(f"Received updated order strategy type for orders {order_strategy_details}")
            token = str(order_strategy_details["instrument_token"])
            self._order_strategy_mapping[token] = order_strategy_details["strategy_type"]
            self._save_order_selections()

            # NOTE: this mapping is only the DEFAULT strategy for the
            # NEXT buy of this instrument. Open legs keep the strategy
            # stamped at buy time; per-leg switches happen through
            # 'order_strategy_data_updated_positions' (position-row radio).

            logger.debug(f"Updated Order Strategy Mapping: {self._order_strategy_mapping}")

        @self.frontend_data_socket.on("order_sell_mode_data_updated_orders")
        def handle_order_sell_mode_orders(sell_mode_details):
            # Buy-grid sell-mode radio: the DEFAULT sell mode for the NEXT
            # buy of this instrument. Persisted so the choice survives a
            # restart (open legs keep the sell mode stamped at buy time).
            logger.debug(f"Received updated sell mode for orders {sell_mode_details}")
            token = str(sell_mode_details["instrument_token"])
            self._order_sell_mode_mapping[token] = str(
                sell_mode_details.get("sell_mode") or "T"
            ).strip().upper()
            self._save_order_selections()
            logger.debug(f"Updated Order Sell Mode Mapping: {self._order_sell_mode_mapping}")

        @self.frontend_data_socket.on("position_sell_mode_updated")
        def handle_position_sell_mode_updated(sell_mode_details):
            logger.debug(f"Received updated sell mode for positions {sell_mode_details}")
            position_key = str(sell_mode_details.get("position_key") or "")
            new_mode = str(sell_mode_details.get("sell_mode") or "T").strip().upper()
            old_mode = None
            affected_sym = None
            affected_qty = 0
            affected_token = None
            affected_strategy = None
            affected_ltp = 0.0
            affected_avg = 0.0

            if position_key and ":" in position_key and position_key in self._position_data:
                # Sell mode is part of the leg identity - re-key the leg
                # so the key and the entry's sell_mode stay consistent
                # across reconciliations.
                token, strategy, current_mode = self._parse_position_key(position_key)
                if current_mode != new_mode:
                    new_key = self._position_key(token, strategy, new_mode)
                    leg = self._position_data.pop(position_key)
                    leg["sell_mode"] = new_mode
                    self._position_data[new_key] = leg
                    logger.info(
                        f"Leg {position_key} re-keyed to {new_key} "
                        f"(sell mode switched mid-trade)"
                    )
                    old_mode = current_mode
                    affected_sym = leg.get("tradingsymbol")
                    affected_qty = leg.get("lots") or leg.get("net_quantity") or 0
                    affected_token = leg.get("instrument_token") or token
                    affected_strategy = leg.get("order_strategy") or strategy
                    affected_ltp = leg.get("latest_price") or 0.0
                    affected_avg = leg.get("average_price") or 0.0
            else:
                # Legacy payload without a leg key: apply to every open
                # leg of the instrument.
                for key in self._legs_for_token(
                    str(sell_mode_details.get("instrument_token", ""))
                ):
                    leg_data = self._position_data.get(key) or {}
                    if not old_mode:
                        old_mode = str(leg_data.get("sell_mode") or "T").strip().upper()
                        affected_sym = leg_data.get("tradingsymbol")
                        affected_qty = leg_data.get("lots") or leg_data.get("net_quantity") or 0
                        affected_token = leg_data.get("instrument_token")
                        affected_strategy = leg_data.get("order_strategy", "DEFAULT")
                        affected_ltp = leg_data.get("latest_price") or 0.0
                        affected_avg = leg_data.get("average_price") or 0.0
                    leg_data["sell_mode"] = new_mode

            # A U/D leg holds a broker-side resting exit limit placed at
            # buy time. Leaving U/D must cancel it, or the stale limit
            # keeps managing the exit behind the new mode's back - it
            # filled at 13:53:48 on 2026-09-22 after a U -> T switch and
            # the T watcher's own exit then rejected as a phantom short.
            if old_mode in ("U", "D") and new_mode != old_mode and affected_sym and affected_qty:
                try:
                    cancelled = self._cancel_pending_exit_orders(affected_sym)
                    if cancelled:
                        logger.info(
                            f"Cancelled {cancelled} resting exit order(s) for "
                            f"{affected_sym} after sell mode switch "
                            f"{old_mode} -> {new_mode}"
                        )
                except Exception as cancel_error:
                    logger.error(
                        f"Could not cancel resting exit for {affected_sym} "
                        f"after sell mode switch {old_mode} -> {new_mode}: "
                        f"{cancel_error}"
                    )

            # Entering U/D from T: no resting exit exists (exits are
            # placed at buy time only), so place one now with the same
            # semantics as the buy-time U/D exit - U anchors the limit
            # to the current LTP, D anchors it to the leg's average buy
            # price - both plus the strategy-scaled target points.
            if (
                new_mode in ("U", "D")
                and old_mode == "T"
                and affected_sym
                and affected_qty
                and self._mode == "LIVE"
            ):
                target_pts = self._target_profit_points(affected_strategy)
                base_price = affected_ltp if new_mode == "U" else affected_avg
                if not base_price or float(base_price) <= 0:
                    # One of the two anchors is always valid for an
                    # open leg; fall back across before giving up.
                    base_price = affected_avg or affected_ltp
                if base_price and float(base_price) > 0 and target_pts > 0:
                    exit_price = round(float(base_price) + target_pts, 2)
                    logger.info(
                        f"Placing {new_mode}-mode exit limit for "
                        f"{affected_sym}: {affected_qty} lot(s) at "
                        f"{exit_price} (base {base_price} + "
                        f"{target_pts} target pts) after sell mode "
                        f"switch {old_mode} -> {new_mode}"
                    )
                    try:
                        self._broker.place_exit_limit(
                            affected_sym,
                            affected_token,
                            affected_qty,
                            self._exchange,
                            exit_price
                        )
                    except Exception as place_error:
                        logger.error(
                            f"Could not place {new_mode}-mode exit limit "
                            f"for {affected_sym} after sell mode switch: "
                            f"{place_error}"
                        )
                elif target_pts <= 0:
                    logger.warning(
                        f"No target points configured for strategy "
                        f"{affected_strategy}; the leg stays "
                        f"watcher-managed after the "
                        f"{old_mode} -> {new_mode} switch."
                    )

            if self.frontend_data_socket:
                self.frontend_data_socket.emit(
                    'update_open_positions',
                    self._position_data
                )

        @self.frontend_data_socket.on("position_book_profit_updated")
        def handle_position_book_profit_updated(bp_details):
            logger.debug(f"Received updated book-profit override for positions {bp_details}")
            position_key = str(bp_details.get("position_key") or "")
            try:
                new_pct = max(0.0, float(bp_details.get("book_profit_pct")))
            except (TypeError, ValueError):
                logger.warning(f"Invalid book-profit override received: {bp_details}")
                return

            if position_key and ":" in position_key and position_key in self._position_data:
                self._position_data[position_key]["override_book_profit_pct"] = new_pct
                logger.info(f"Book-profit override for {position_key} set to {new_pct}%")
            else:
                # Legacy payload without a leg key: apply to every open
                # leg of the instrument.
                legs = self._legs_for_token(
                    str(bp_details.get("instrument_token", ""))
                )
                for key in legs:
                    self._position_data[key]["override_book_profit_pct"] = new_pct
                logger.info(
                    f"Book-profit override for {len(legs)} leg(s) of token "
                    f"{bp_details.get('instrument_token')} set to {new_pct}%"
                )

            if self.frontend_data_socket:
                self.frontend_data_socket.emit(
                    'update_open_positions',
                    self._position_data
                )

        @self.frontend_data_socket.on('place_sell_order')
        def handle_sell_order(order_details):
            """Handles a sell order request from the frontend (one position leg)."""
            logger.debug(f"Available positions: {self._position_data}")
            logger.debug(f"Received sell order from client: {order_details}")

            token = str(order_details.get("token", ""))
            position_key = str(order_details.get("position_key") or "")

            # Resolve the exact leg to sell. Falls back to the token's
            # only leg when the frontend has not sent a leg key.
            if position_key not in self._position_data:
                legs = self._legs_for_token(token)
                if len(legs) == 1:
                    position_key = legs[0]
                else:
                    self.frontend_data_socket.emit(
                        'sell_order_result',
                        {
                            "success": False,
                            "tradingsymbol": order_details.get("tradingsymbol", "?"),
                            "position_key": order_details.get("position_key", ""),
                            "error": f"No matching open position leg for {order_details.get('tradingsymbol', token)}; refresh the grid.",
                        },
                    )
                    return

            now = time.time()
            with self._sell_order_lock:
                last_ts = self._sell_in_flight.get(position_key)
                if last_ts is not None and (now - last_ts) < self._SELL_IN_FLIGHT_TIMEOUT_SECS:
                    logger.warning(
                        f"Duplicate sell request ignored for {position_key} "
                        f"({order_details.get('tradingsymbol', '?')}); another sell for this position is already in progress."
                    )
                    self.frontend_data_socket.emit(
                        'sell_order_result',
                        {
                            "success": False,
                            "tradingsymbol": order_details.get("tradingsymbol", "?"),
                            "position_key": position_key,
                            "error": "Duplicate sell request ignored - a sell for this position is already in progress.",
                        },
                    )
                    return
                self._sell_in_flight[position_key] = now

            try:
                #ltp = self._five_weekly_option_contracts[order_details["token"]]["ltp"]
                ltp = self._position_data[position_key]["latest_price"]
                position = self._position_data.get(position_key)
                logger.debug(f"LTP fetched from the position : {ltp}")

                # Get current mode
                self._mode = fetch_from_json("appconfig.json", "MODE")
                logger.debug(f"Current trading mode: {self._mode}")

                # =========================================================
                # PAPER MODE
                # =========================================================
                if self._mode not in ("LIVE", "SIMULATION", "PLAYBACK"):

                    if not position:
                        raise Exception(
                            f"No open paper position for leg "
                            f"{position_key}"
                        )

                    held_lots = int(position.get("net_quantity", 0) or 0)
                    sell_lots = int(order_details["lots"])

                    if sell_lots <= 0:
                        raise Exception("Sell lots must be greater than zero")

                    if sell_lots > held_lots:
                        raise Exception(
                            f"Cannot sell {sell_lots} lots of "
                            f"{order_details['tradingsymbol']}; only "
                            f"{held_lots} lots held"
                        )

                    buy_price = position["average_price"]
                    lot_size = position["lotsize"]
                    leg_strategy = position.get("order_strategy", "ULTRA_SCALPING")
                    leg_sell_mode = position.get("sell_mode", "T")
                    buy_time = self.get_open_position_buy_time(
                        position_key.split(":", 1)[0],
                        tradingsymbol=order_details["tradingsymbol"],
                        strategy=leg_strategy,
                        sell_type=leg_sell_mode
                    )

                    self.write_paper_trade(
                        transaction_type="SELL",
                        tradingsymbol=order_details["tradingsymbol"],
                        token=order_details["token"],
                        qty=sell_lots,
                        ltp=ltp,
                        product="MIS",
                        buy_price=buy_price,
                        lot_size=lot_size,
                        buy_time=buy_time,
                        strategy=leg_strategy,
                        sell_type=leg_sell_mode
                    )
                    logger.info(
                        f"Paper SELL recorded for "
                        f"{order_details['tradingsymbol']} "
                        f"[{position_key}] "
                        f"({sell_lots} lots) @ {ltp}"
                    )

                    # Recalculate paper positions
                    self.refresh_paper_positions()

                    self.frontend_data_socket.emit(
                        'sell_order_result',
                        {
                            "success": True,
                            "tradingsymbol": order_details["tradingsymbol"],
                            "lots": sell_lots,
                            "position_key": position_key,
                            "paper": True
                        }
                    )
                # =========================================================
                # LIVE / SIMULATION MODE
                # =========================================================
                else:                    

                    leg_sell_mode = str(
                        (position or {}).get("sell_mode", "T") or "T"
                    ).upper()
                    leg_strategy = (position or {}).get(
                        "order_strategy", "ULTRA_SCALPING"
                    )

                    # U/D legs hold a resting exit limit at the broker;
                    # cancel it before the manual sell (full or partial)
                    # or a later fill would leave the account net short.
                    if leg_sell_mode in ("U", "D"):
                        self._cancel_pending_exit_orders(
                            order_details["tradingsymbol"]
                        )

                    result = self._broker.sell_units(order_details["tradingsymbol"] , order_details["token"] , order_details["lots"] , self._exchange , ltp, position_key=position_key)

                    logger.info(f"Sell order successful for {order_details['tradingsymbol']} ({order_details['lots']} lots)")

                    # Partial manual sell on a U/D leg: re-place the exit
                    # for the remaining lots so the leg keeps its target.
                    if leg_sell_mode in ("U", "D") and position:
                        remaining_lots = (
                            int(position.get("net_quantity", 0) or 0)
                            - int(order_details["lots"])
                        )
                        if remaining_lots > 0:
                            target_points = self._target_profit_points(leg_strategy)
                            if target_points > 0:
                                exit_price = (
                                    float(position.get("average_price", 0) or 0)
                                    + target_points
                                )
                                logger.info(
                                    f"Re-placing {leg_sell_mode}-mode exit for "
                                    f"remaining {remaining_lots} lots of "
                                    f"{order_details['tradingsymbol']} "
                                    f"at {exit_price:.2f}"
                                )
                                self._broker.place_exit_limit(
                                    order_details["tradingsymbol"],
                                    order_details["token"],
                                    remaining_lots,
                                    self._exchange,
                                    exit_price
                                )
                            else:
                                logger.warning(
                                    f"No target points configured for strategy "
                                    f"{leg_strategy}; the remaining "
                                    f"{remaining_lots} lots will be managed by "
                                    f"the stop-loss watcher only."
                                )
                    #self.frontend_data_socket.emit('sell_order_result',{"success": True, "tradingsymbol": order_details["tradingsymbol"], "lots": order_details["lots"]})
            except Exception as e:
                logger.error(f"Error placing sell order: {e}", exc_info=False)
                self.frontend_data_socket.emit(
                    'sell_order_result',
                    {"success": False, "tradingsymbol": order_details.get("tradingsymbol", "?"), "position_key": position_key, "error": str(e)}
                )
            finally:
                with self._sell_order_lock:
                    self._sell_in_flight.pop(position_key, None)

        @self.frontend_data_socket.on('request_pending_orders')
        def send_pending():
            # Watchdog: the UI polls every ~15s while the window is
            # focused. Background/minimized browsers throttle timers to
            # once a minute, which used to show up as unexplained 38-66s
            # gaps in the log. Flag any suspicious gap explicitly.
            now_ts = time.time()
            gap = now_ts - getattr(self, "_last_frontend_poll_ts", 0.0)
            self._last_frontend_poll_ts = now_ts
            if gap > 30:
                logger.warning(
                    f"Frontend pending-orders poll gap was {gap:.0f}s "
                    f"(expected ~15s) - browser timer throttling "
                    f"(window minimized/background) or socket stall suspected."
                )
            self.frontend_data_socket.emit('update_pending_orders', self._broker.fetch_all_pending_orders())

        @self.frontend_data_socket.on('cancel_all_pending_orders')
        def handle_cancel_all_pending_orders():
            """Cancel every pending order at the broker (Cancel All button)."""
            try:
                cancelled = self._broker.cancel_all_pending_orders()
                logger.info(f"Cancel All: {cancelled} pending order(s) cancelled")

                self.frontend_data_socket.emit(
                    'status_message',
                    {"success": True, "message": f"Cancelled {cancelled} pending order(s)"}
                )
            except Exception as e:
                logger.error(f"Cancel All pending orders failed: {e}", exc_info=True)
                self.frontend_data_socket.emit(
                    'status_message',
                    {"success": False, "message": f"Cancel All failed: {e}"}
                )

            # Re-fetch and re-render the grid either way so the button
            # re-enables and the rows reflect reality.
            try:
                pending_orders = self._broker.fetch_all_pending_orders()
                self.frontend_data_socket.emit('update_pending_orders', pending_orders)
            except Exception as e:
                logger.error(f"Cancel All: could not refresh pending orders: {e}")
                self.frontend_data_socket.emit('update_pending_orders', [])

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


        @self.frontend_data_socket.on('place_buy_order')
        def handle_buy_order(order_details):
            logger.debug(f"Received buy order from client: {order_details}")
            try:
                # Trading mode is PAPER, LIVE, SIMULATION, or PLAYBACK.
                self._mode = fetch_from_json("appconfig.json" , "MODE")
                sell_mode = order_details.get("SELL_MODE", "T")
                # The buy widget sends the raw radio value
                # ('UltraScalping'); normalise it before it reaches the
                # leg key, the paper file or the strategy mapping.
                strategy = self._normalise_strategy(
                    order_details.get("strategy", "")
                )
                # Strategy-scaled book-profit target in points; shared
                # with the exit re-placement on partial manual sells.
                target_profit = self._target_profit_points(strategy)
                self._order_sell_mode_mapping[order_details["token"]] = sell_mode
                self._order_strategy_mapping[order_details["token"]] = strategy
                self._save_order_selections()
                logger.debug(f"Mode set is {self._mode} ; Strategy set is {strategy}  Sell Mode is {sell_mode} ")

                # TradingView entry validation (TV_DATA_Validation_REQUIRED).
                # Applies to every mode; sells are not affected.
                if self._is_tv_validation_required():
                    tradingsymbol = str(order_details.get("tradingsymbol", ""))
                    is_pe = tradingsymbol.upper().endswith("PE")
                    side = "PE" if is_pe else "CE"
                    allowed = self._tv_pe_buy_allowed if is_pe else self._tv_ce_buy_allowed
                    reason = self._tv_pe_reason if is_pe else self._tv_ce_reason
                    if not allowed:
                        logger.warning(f"{side} buy rejected by TradingView validation for {tradingsymbol}: {reason}")
                        self.frontend_data_socket.emit('buy_order_result', {
                            "success": False,
                            "tradingsymbol": tradingsymbol,
                            "lots": order_details["lots"],
                            "error": f"{side} buy blocked by TradingView validation: {reason}"
                        })
                        return

                ltp = self._five_weekly_option_contracts[order_details["token"]]["ltp"]

                if self._mode not in ("LIVE", "SIMULATION", "PLAYBACK"):
                    self.write_paper_trade(transaction_type="BUY",tradingsymbol=order_details["tradingsymbol"],token=order_details["token"],qty=order_details["lots"],ltp=ltp,product="MIS",strategy=strategy,sell_type=sell_mode)
                    logger.info(
                            f"Paper BUY recorded: "
                            f"{order_details['tradingsymbol']} "
                            f"Token={order_details['token']} "
                            f"Qty={order_details['lots']} "
                            f"Price={ltp} "
                            f"Strategy={strategy} "
                            f"SellMode={sell_mode}"
                        )

                    logger.info("Calling refresh_paper_positions() after BUY...")                    
                    self.refresh_paper_positions()
                    logger.info(f"After BUY refresh, position_data = {self._position_data}")
                else:
                    if sell_mode == "U":
                        logger.info(f"Placing Buy-Sell order as Sell Mode is set to {sell_mode} ")
                        self._broker.buy_sell_units(order_details["tradingsymbol"] , order_details["token"] , order_details["lots"] , self._exchange , ltp,self._mode,sell_mode,target_profit,strategy)
                    else: # Sell Mode is 'T' or 'D'
                        logger.info("Placing Buy order")
                        self._broker.buy_units(order_details["tradingsymbol"] , order_details["token"] , order_details["lots"] , self._exchange , ltp,self._mode,sell_mode,target_profit,strategy)
                    # ✅ Once order completes, tell frontend to refresh fund summary
                    #self.frontend_data_socket.emit('refresh_fund_summary')
                    # ✅ Notify frontend of success
                    #self.frontend_data_socket.emit('buy_order_result', { "success": True,"tradingsymbol": order_details["tradingsymbol"],"lots": order_details["lots"]})
            except Exception as e:
                    logger.error(f"|Error placing buy order: {e}|", exc_info=True)
                    #Check if the error is because of insufficient fund..If so, reduce the lots by 1 and reattempt
                    error_message = str(e).upper()

                    # New Implementation to keep decrementing the Lots till 0 to check if we could buy some lots with the available funds
                    # Provided by ChatGPT Go

                    # if "FUND LIMIT INSUFFICIENT" in error_message:
                    #     reduced_lots = max(1, int(order_details["lots"]) - 1)
                    #     logger.warning(f"Insufficient funds for {order_details['lots']} lots — retrying with {reduced_lots} lot(s)...")
                    #     try:
                    #         ltp = self._five_weekly_option_contracts[order_details["token"]]["ltp"]
                    #         self._broker.buy_units(order_details["tradingsymbol"] , order_details["token"] , reduced_lots , self._exchange , ltp)
                    #         self.frontend_data_socket.emit('buy_order_result', { 
                    #             "success": True,
                    #             "tradingsymbol": order_details["tradingsymbol"],
                    #             "lots": reduced_lots,
                    #             "note": "Retried with reduced lots due to insufficient funds"
                    #         })
                    #         return
                    #     except Exception as retry_error:
                    #         logger.error(f"Retry buy order failed: {retry_error}", exc_info=True)
                    #         self.frontend_data_socket.emit('buy_order_result', {
                    #             "success": False,
                    #             "tradingsymbol": order_details["tradingsymbol"],
                    #             "lots": reduced_lots,
                    #             "error": str(retry_error)
                    #         })
                    #         return
                    # #logger.exception(f"Error placing buy order: {e}")
                    # # ❌ Notify frontend of failure
                    # self.frontend_data_socket.emit('buy_order_result', {
                    #     "success": False,
                    #     "tradingsymbol": order_details["tradingsymbol"],
                    #     "lots": order_details["lots"],
                    #     "error": str(e)
                    # })
                    if "FUND LIMIT INSUFFICIENT" in error_message:

                        retry_lots = int(order_details["lots"]) - 1

                        while retry_lots >= 1:

                            logger.warning(
                                f"Insufficient funds for {retry_lots + 1} lots "
                                f"— retrying with {retry_lots} lot(s)..."
                            )

                            try:
                                ltp = self._five_weekly_option_contracts[
                                    order_details["token"]
                                ]["ltp"]

                                self._broker.buy_units(
                                    order_details["tradingsymbol"],
                                    order_details["token"],
                                    retry_lots,
                                    self._exchange,
                                    ltp,
                                    self._mode,
                                    sell_mode,
                                    target_profit,
                                    strategy
                                )

                                self.frontend_data_socket.emit('buy_order_result', {
                                    "success": True,
                                    "tradingsymbol": order_details["tradingsymbol"],
                                    "lots": retry_lots,
                                    "note": "Retried with reduced lots due to insufficient funds"
                                })

                                return

                            except Exception as retry_error:

                                retry_error_message = str(retry_error).upper()

                                if "FUND LIMIT INSUFFICIENT" not in retry_error_message:
                                    logger.error(
                                        f"Retry buy order failed: {retry_error}",
                                        exc_info=True
                                    )

                                    self.frontend_data_socket.emit('buy_order_result', {
                                        "success": False,
                                        "tradingsymbol": order_details["tradingsymbol"],
                                        "lots": retry_lots,
                                        "error": str(retry_error)
                                    })

                                    return

                                retry_lots -= 1

                        # No lot size could be placed
                        logger.error(
                            f"Insufficient funds even for 1 lot of "
                            f"{order_details['tradingsymbol']}"
                        )

                        self.frontend_data_socket.emit('buy_order_result', {
                            "success": False,
                            "tradingsymbol": order_details["tradingsymbol"],
                            "lots": 0,
                            "error": "Insufficient funds even for 1 lot"
                        })

                        return

                    self.frontend_data_socket.emit('buy_order_result', {
                        "success": False,
                        "tradingsymbol": order_details.get("tradingsymbol", ""),
                        "lots": order_details.get("lots", 0),
                        "error": str(e)
                    })

                        

                            