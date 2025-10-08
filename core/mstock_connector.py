import os
import rel
import json
import asyncio
import logging
import http.client
import websockets
import threading
import datetime
from dotenv import load_dotenv

from utils import fetch_from_json , write_to_json
from adapter.mstock_utils import parse_quote_message

logging.basicConfig(level=logging.DEBUG)

load_dotenv()

MSTOCK_API_KEY = os.getenv("MSTOCK_API_KEY")
MSTOCK_CLIENT_CODE = os.getenv("MSTOCK_CLIENT_CODE")
MSTOCK_CLIENT_PASSWORD = os.getenv("MSTOCK_CLIENT_PASSWORD")


class MStockSingleton:
    _instance = None
    _api_key = None
    _access_token = None
    _socket = None
    _trader = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MStockSingleton, cls).__new__(cls)
            cls._instance._initialize_client()
        return cls._instance

    def _initialize_client(self):
        self._api_key = MSTOCK_API_KEY
        if not self._api_key:
            raise ValueError("MSTOCK_API_KEY not found in environment variables")

    def create_session(self):
        refresh_token = self.login()
        self.get_session_token(refresh_token)

    def login(self):
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
        print(response)
        return response["data"]["refreshToken"]

    def get_session_token(self, refresh_token):
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
        print(response)
        access_token = response["data"]["jwtToken"]
        print(access_token)
        data_to_update_json = {
            "mstock_jwt_token" : access_token , 
            "mstock_last_token_timestamp" : datetime.datetime.now().isoformat()
        }
        write_to_json(data_to_update_json , "access_token.json")
        self.set_access_token(access_token)

    async def start_socket_connection(self, shutdown_event, trader_instance):
        """Starts the WebSocket connection and handles automatic reconnection."""
        self._trader = trader_instance
        url = f"wss://ws.mstock.trade?API_KEY={self._api_key}&ACCESS_TOKEN={self._access_token}"

        while True:
            try:
                logging.info("Attempting to connect to M.Stock WebSocket...")
                async with websockets.connect(url) as ws:
                    self._socket = ws
                    logging.info("Connection successful. Logging in...")

                    login_message = f"LOGIN:{self._access_token}"
                    await ws.send(login_message)

                    await asyncio.sleep(1)
                    await self._subscribe_to_instruments(trader_instance.get_relevant_instruments_to_track())

                    async for message in ws:
                        await self._handle_message(message)

            except websockets.exceptions.ConnectionClosed as e:
                logging.warning(f"Connection closed: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logging.error(f"WebSocket error: {e}. Retrying in 5s...")
                await asyncio.sleep(5)

    async def _subscribe_to_instruments(self, instruments):
        print("here")
        if not self._socket:
            return
        if instruments:
            subscription_message = {
                "correlationID": "Optional Field",
                "action": 1,
                "params": {
                    "mode": 3,
                    "tokenList": [{"exchangeType": 2, "tokens": instruments }],
                },
            }
            await self._socket.send(json.dumps(subscription_message))
            print("####################################")
            logging.info(f"Subscription sent for instruments: {instruments}")
            print("####################################")

    async def _unsubscribe_from_all(self , instruments):
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
            logging.info("Unsubscribed from all the instruments")

    async def _handle_message(self, message):
        try:
            market_update = parse_quote_message(message)
            # print(market_update)
            self._trader.set_latest_price(market_update["token"] , None , market_update["ltp"])

        except:
            logging.debug("Error parsing market data")

    def set_access_token(self, access_token):
        self._access_token = access_token
        logging.debug(f"Access token set: {access_token}")

    # development
    def initialise_for_dev(self):
        if not self._access_token:
            access_token = fetch_from_json("access_token.json", "mstock_jwt_token")
            print(access_token)
            self.set_access_token(access_token)

    # production
    def initialise_for_prod(self):
        last_updated_date = fetch_from_json("access_token.json" , "mstock_last_token_timestamp")
        last_update_datetime = datetime.datetime.fromisoformat(last_updated_date)
        today_date = datetime.datetime.now().date()
        if not last_update_datetime.date() == today_date:
            self.create_session()
            logging.debug("MStock session created for production.")
        else:
            access_token = fetch_from_json("access_token.json", "mstock_jwt_token")
            print(access_token)
            self.set_access_token(access_token)
