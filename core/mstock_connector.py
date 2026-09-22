import os
import rel
import json
import asyncio
import logging
import socket
import time
import http.client
import websockets
import threading
import datetime
from dotenv import load_dotenv
from loguru import logger

from core.utils import fetch_from_json , write_to_json , get_exchange_for_underlying
from adapter.mstock_utils import parse_quote_message

logging.basicConfig(level=logging.DEBUG)

load_dotenv()

MSTOCK_API_KEY = os.getenv("MSTOCK_API_KEY")
MSTOCK_CLIENT_CODE = os.getenv("MSTOCK_CLIENT_CODE")
MSTOCK_CLIENT_PASSWORD = os.getenv("MSTOCK_CLIENT_PASSWORD")

WS_HOST = "ws.mstock.trade"
WS_PORT = 443


class MStockSingleton:
    _instance = None
    _api_key = None
    _access_token = None
    _socket = None
    _trader = None

    # Feed-rate sampler state: M.Stock pushes one 379-byte binary
    # packet per instrument update. The sampler logs packets/min and
    # the longest quiet gap every 60s, and warns when the feed goes
    # silent - that is when the grid prices freeze on screen.
    _feed_stats_window_start = 0.0
    _feed_packets = 0
    _feed_last_packet_ts = 0.0
    _feed_max_gap = 0.0

    def __new__(cls):
        logger.info("Creating MStockSingleton instance...")
        if cls._instance is None:
            cls._instance = super(MStockSingleton, cls).__new__(cls)
            cls._instance._initialize_client()
        return cls._instance

    def _initialize_client(self):
        logger.info("Initializing M.Stock client...")
        self._api_key = MSTOCK_API_KEY
        if not self._api_key:
            raise ValueError("MSTOCK_API_KEY not found in environment variables")
        self._socket_loop = None
        self._ws_cached_ips = []

    def create_session(self):
        logger.info("Creating M.Stock session...")
        refresh_token = self.login()
        self.get_session_token(refresh_token)

    def login(self):
        logger.info("Logging in to M.Stock...")
        conn = http.client.HTTPSConnection("api.mstock.trade")
        headers = {
            "X-Mirae-Version": "1",
            "Content-Type": "application/json",
        }
        json_data = {
            "clientcode": MSTOCK_CLIENT_CODE,
            "password": MSTOCK_CLIENT_PASSWORD,
            "totp": "",
            "state": "",
        }
        conn.request(
            "POST",
            "/openapi/typeb/connect/login",
            json.dumps(json_data),
            headers,
        )
        response = json.loads(conn.getresponse().read().decode("utf-8"))
        #logger.debug("Login response: {response}", response=response)  # prints jwtToken/refreshToken/feedToken
        return response["data"]["refreshToken"]

    def get_session_token(self, refresh_token):
        logger.info("Getting session token from M.Stock...")
        otp = input("Please enter OTP received - ")
        conn = http.client.HTTPSConnection("api.mstock.trade")
        headers = {
            "X-Mirae-Version": "1",
            "X-PrivateKey": self._api_key,
            "Content-Type": "application/json",
        }
        json_data = {
            "refreshToken": refresh_token,
            "otp": otp,
        }
        conn.request(
            "POST",
            "/openapi/typeb/session/token",
            json.dumps(json_data),
            headers,
        )
        response = json.loads(conn.getresponse().read().decode("utf-8"))
        #logger.debug("Session token response: {response}", response=response)
        
        access_token = response["data"]["jwtToken"]
        #logger.debug(f"Access token: {access_token}")
        

        data_to_update_json = {
            "mstock_jwt_token" : access_token , 
            "mstock_last_token_timestamp" : datetime.datetime.now().isoformat()
        }
        write_to_json(data_to_update_json , "access_token.json")
        self.set_access_token(access_token)

    def _ws_url(self, host=None):
        host = host or WS_HOST
        return f"wss://{host}?API_KEY={self._api_key}&ACCESS_TOKEN={self._access_token}"

    async def _refresh_ws_ip_cache(self):
        """
        Resolve the websocket host (IPv4) and cache the addresses.

        DNS lookups for this host fail intermittently - transient resolver
        glitches amplified by Windows negative DNS caching. The cached
        addresses let the reconnect logic bypass DNS entirely while the
        resolver recovers.
        """
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(
                WS_HOST, WS_PORT, family=socket.AF_INET, proto=socket.IPPROTO_TCP
            )
            ips = sorted({info[4][0] for info in infos})
            if ips:
                self._ws_cached_ips = ips
        except Exception:
            # Resolver currently failing - keep previously cached addresses
            pass

    async def _connect_ws(self):
        """
        Connect to the websocket. When DNS resolution fails, fall back to
        the last known good IP. TLS still verifies the real hostname via
        server_hostname, so the connection remains secure.
        """
        try:
            return await websockets.connect(self._ws_url())
        except (socket.gaierror, OSError) as e:
            if not self._ws_cached_ips:
                raise
            ip = self._ws_cached_ips[0]
            logger.warning(
                f"DNS lookup for {WS_HOST} failed ({e}); "
                f"connecting via cached IP {ip}"
            )
            return await websockets.connect(
                self._ws_url(ip),
                server_hostname=WS_HOST,
            )

    async def start_socket_connection(self, shutdown_event, trader_instance):
        # logger.info("Starting M.Stock WebSocket connection...")
        """Starts the WebSocket connection and handles automatic reconnection."""
        self._trader = trader_instance

        while True:
            try:
                await self._refresh_ws_ip_cache()

                logger.info("Attempting to connect to M.Stock WebSocket...")
                async with await self._connect_ws() as ws:
                    self._socket = ws
                    self._socket_loop = asyncio.get_running_loop()
                    logger.info("Connection successful. Logging in...")

                    login_message = f"LOGIN:{self._access_token}"
                    await ws.send(login_message)

                    await asyncio.sleep(1)
                    
                    await self._subscribe_to_instruments(trader_instance.get_relevant_instruments_to_track())

                    try:
                        logger.info("Bootstrapping M.Stock weekly option contracts in background thread...")
                        await asyncio.to_thread(trader_instance.setup_woc_subscriptions)
                    except Exception as e:
                        logger.exception(f"M.Stock options bootstrap failed after socket connect: {e}")
                    # logger.info("MSTOCK WS RECEIVE LOOP STARTED")
                    # logger.info("MSTOCK WS WAITING FOR NEXT MESSAGE")
                    async for message in ws:
                        # logger.info("MSTOCK WS MESSAGE RECEIVED1")
                        await self._handle_message(message)
                        # logger.info("MSTOCK WS MESSAGE RECEIVED2")
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"Connection closed: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Retrying in 5s...")
                await asyncio.sleep(5)

    # async def _subscribe_to_instruments(self, instruments):
    #     logger.info(f"Subscribing to instruments: {instruments}")
    #     if not self._socket:
    #         return
    #     if instruments:
    #         subscription_message = {
    #             "correlationID": "Optional Field",
    #             "action": 1,
    #             "params": {
    #                 "mode": 3,
    #                 "tokenList": [{"exchangeType": 2, "tokens": instruments }],
    #             },
    #         }
    #         await self._socket.send(json.dumps(subscription_message))
    #         logger.info(f"Subscription sent for instruments: {instruments}")
    #MSTOCK_SENSEX
    # async def _subscribe_to_instruments(self, instruments):
    #     logger.info(f"Subscribing to instruments: {instruments}")

    #     if not self._socket:
    #         logger.warning("M.Stock socket is not connected.")
    #         return

    #     if not instruments:
    #         logger.info("No instruments to subscribe.")
    #         return

    #     # instruments should be grouped by exchange type.
    #     # Expected format:
    #     # {
    #     #     2: ["NIFTY_TOKEN1", "NIFTY_TOKEN2"],
    #     #     3: ["SENSEX_TOKEN1", "SENSEX_TOKEN2"]
    #     # }

    #     token_list = []

    #     for exchange_type, tokens in instruments.items():
    #         if not tokens:
    #             continue

    #         token_list.append({
    #             "exchangeType": int(exchange_type),
    #             "tokens": [str(token) for token in tokens]
    #         })

    #     if not token_list:
    #         logger.info("No valid instruments to subscribe.")
    #         return

    #     # subscription_message = {
    #     #     "correlationID": "Optional Field",
    #     #     "action": 1,
    #     #     "params": {
    #     #         "mode": 3,
    #     #         "tokenList": token_list
    #     #     }
    #     # }
        
    #     #MSTOCK_SENSEX
    #     exchange = fetch_from_json("appconfig.json", "EXCHANGE")

    #     exchange_type = 3 if exchange == "BFO" else 2

    #     subscription_message = {
    #         "correlationID": "Optional Field",
    #         "action": 1,
    #         "params": {
    #             "mode": 3,
    #             "tokenList": [{"exchangeType": exchange_type, "tokens": instruments}],
    #         },
    #     }



    #     await self._socket.send(json.dumps(subscription_message))

    #     logger.info(
    #         f"M.Stock subscription sent: {subscription_message}"
    #     )


    # async def _subscribe_to_instruments(self, instruments):
    #     logger.info(f"Subscribing to instruments: {instruments}")

    #     if not self._socket:
    #         logger.warning("M.Stock socket is not connected.")
    #         return

    #     if not instruments:
    #         logger.info("No instruments to subscribe.")
    #         return

    #     # instruments should be grouped by exchange type.
    #     #
    #     # Expected format:
    #     # {
    #     #     2: ["NIFTY_TOKEN1", "NIFTY_TOKEN2"],
    #     #     3: ["SENSEX_TOKEN1", "SENSEX_TOKEN2"]
    #     #     }

    #     token_list = []

    #     for exchange_type, tokens in instruments.items():
    #         if not tokens:
    #             continue

    #         token_list.append({
    #             "exchangeType": int(exchange_type),
    #             "tokens": [str(token) for token in tokens]
    #         })

    #     if not token_list:
    #         logger.info("No valid instruments to subscribe.")
    #         return

    #     subscription_message = {
    #         "correlationID": "Optional Field",
    #         "action": 1,
    #         "params": {
    #             "mode": 3,
    #             "tokenList": token_list
    #         }
    #     }

    #     await self._socket.send(json.dumps(subscription_message))

    #     logger.info(
    #         f"M.Stock subscription sent: {subscription_message}"
    #     )            

    async def _subscribe_to_instruments(self, instruments):
        logger.info(f"Subscribing to instruments: {instruments}")

        if not self._socket:
            logger.warning("M.Stock socket is not connected.")
            return

        if not instruments:
            logger.info("No instruments to subscribe.")
            return

        token_list = []

        # Support both:
        # 1. Simple list format:
        #    ['TOKEN1', 'TOKEN2']
        #
        # 2. Exchange-grouped format:
        #    {
        #        2: ['NIFTY_TOKEN1', 'NIFTY_TOKEN2'],
        #        4: ['SENSEX_TOKEN1', 'SENSEX_TOKEN2']
        #    }

        if isinstance(instruments, list):

            # Exchange is derived from the configured underlying (the
            # old appconfig.json EXCHANGE key no longer exists).
            exchange = get_exchange_for_underlying(
                fetch_from_json("appconfig.json", "UNDERLYING")
            )

            if exchange == "BFO":
                exchange_type = 4
            elif exchange == "BSE":
                exchange_type = 3
            elif exchange == "NFO":
                exchange_type = 2
            else:
                exchange_type = 2

            token_list.append({
                "exchangeType": exchange_type,
                "tokens": [str(token) for token in instruments]
            })

        else:

            for exchange_type, tokens in instruments.items():
                if not tokens:
                    continue

                token_list.append({
                    "exchangeType": int(exchange_type),
                    "tokens": [str(token) for token in tokens]
                })

        if not token_list:
            logger.info("No valid instruments to subscribe.")
            return

        subscription_message = {
            "correlationID": "Optional Field",
            "action": 1,
            "params": {
                "mode": 3,
                "tokenList": token_list
            }
        }

        await self._socket.send(json.dumps(subscription_message))

        logger.info(
        f"M.Stock subscription sent: {subscription_message}"
    )    

    async def _unsubscribe_from_all(self , instruments):
        logger.info("Unsubscribing from all instruments")
        if not self._socket:
            return
        if instruments:
            subscription_message = {
                "correlationID": "Optional Field",
                "action": 0,
                "params": {
                    "mode": 3,
                    "tokenList": [{"exchangeType": 2, "tokens": [str(instrument) for instrument in instruments]}],
                },
            }
            await self._socket.send(json.dumps(subscription_message))
            logger.info("Unsubscribed from all the instruments")

    # async def _handle_message(self, message):
    #     #logger.debug(f"Received message: {message}")
    #     try:
    #         market_update = parse_quote_message(message)
    #         #logger.debug(f"Market update received: {market_update}")
    #         self._trader.set_latest_price(market_update["token"] , None , market_update["ltp"])

    #     except:
    #         logger.debug("Error parsing market data")

    async def _handle_message(self, message):
        try:
            # logger.debug(
            #     f"MSTOCK WS RX | type={type(message).__name__} | "
            #     f"length={len(message) if isinstance(message, (bytes, bytearray)) else 'N/A'}"
            # )

            # M.Stock can send packets other than the 379-byte quote packet.
            if not isinstance(message, (bytes, bytearray)):
                # logger.debug(
                #     f"MSTOCK WS IGNORE | non-binary message | "
                #     f"type={type(message).__name__}"
                # )
                return

            if len(message) != 379:
                # logger.debug(
                #     f"MSTOCK WS IGNORE | unsupported packet length={len(message)}"
                # )
                return

            # --- FEED-RATE SAMPLER ---
            # Counted per quote packet (one instrument update each).
            now_ts = time.time()
            if not self._feed_stats_window_start:
                self._feed_stats_window_start = now_ts
                self._feed_last_packet_ts = now_ts
            self._feed_packets += 1
            packet_gap = now_ts - self._feed_last_packet_ts
            self._feed_last_packet_ts = now_ts
            if packet_gap > self._feed_max_gap:
                self._feed_max_gap = packet_gap
            if packet_gap > 10:
                logger.warning(
                    f"MSTOCK FEED STALL | no quote packet for "
                    f"{packet_gap:.1f}s - grid prices were frozen "
                    f"during this window"
                )
            if now_ts - self._feed_stats_window_start >= 60:
                elapsed = now_ts - self._feed_stats_window_start
                rate = self._feed_packets / elapsed * 60
                logger.info(
                    f"MSTOCK FEED RATE | {self._feed_packets} quote "
                    f"packets in {elapsed:.0f}s ({rate:.1f}/min) | "
                    f"longest quiet gap {self._feed_max_gap:.1f}s"
                )
                self._feed_stats_window_start = now_ts
                self._feed_packets = 0
                self._feed_max_gap = 0.0

            # logger.debug("MSTOCK WS QUOTE PACKET | 379-byte packet received")

            market_update = parse_quote_message(message)

            # logger.debug(
            #     f"MSTOCK WS PARSED | market_update={market_update}"
            # )

            if not market_update:
                logger.warning(
                    "MSTOCK WS PARSE EMPTY | parse_quote_message returned no data"
                )
                return

            token = str(market_update["token"])
            ltp = market_update["ltp"]

            # logger.info(
            #     f"MSTOCK TICK | token={token} | ltp={ltp}"
            # )

            self._trader.set_latest_price(
                token,
                None,
                ltp,
                market_update.get("close")
            )

            # logger.debug(
            #     f"MSTOCK TICK FORWARDED | token={token} | ltp={ltp}"
            # )

        except Exception as e:
            logger.exception(
                f"MSTOCK WS HANDLER ERROR | {e}"
            )


    def set_access_token(self, access_token):
        logger.info("Setting access token for M.Stock...")
        self._access_token = access_token
        #logger.debug(f"Access token set: {access_token}")  # do not log the token

    # development
    def initialise_for_dev(self):
        logger.info("Initializing M.Stock for development...")
        if not self._access_token:
            access_token = fetch_from_json("access_token.json", "mstock_jwt_token")
            logger.info("Fetched access token from JSON for development.")
            self.set_access_token(access_token)

    # production
    def initialise_for_prod(self):
        logger.info("Initializing M.Stock for production...")
        last_updated_date = fetch_from_json("access_token.json" , "mstock_last_token_timestamp")
        last_update_datetime = datetime.datetime.fromisoformat(last_updated_date)
        today_date = datetime.datetime.now().date()
        if not last_update_datetime.date() == today_date:
            self.create_session()
            logger.debug("MStock session created for production.")
        else:
            access_token = fetch_from_json("access_token.json", "mstock_jwt_token")
            #logger.debug(access_token)  # do not log the token
            logger.info("Fetched access token from JSON for production.")
            self.set_access_token(access_token)