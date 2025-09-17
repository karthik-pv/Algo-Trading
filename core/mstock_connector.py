import os
import rel
import json
import asyncio
import logging
import http.client
import websockets
import threading
from dotenv import load_dotenv

from utils import fetch_from_json

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
                    await self._subscribe_to_instruments()

                    async for message in ws:
                        await self._handle_message(message)

            except websockets.exceptions.ConnectionClosed as e:
                logging.warning(f"Connection closed: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logging.error(f"WebSocket error: {e}. Retrying in 5s...")
                await asyncio.sleep(5)

    async def _subscribe_to_instruments(self):
        """Sends the subscription message for relevant instruments."""
        if not self._socket:
            return

        instruments = [55256, 55412]
        if instruments:
            subscription_message = {"a": "subscribe", "v": instruments}
            await self._socket.send(json.dumps(subscription_message))
            logging.info(f"Subscription sent for instruments: {instruments}")

    async def _handle_message(self, message):
        """Processes incoming messages from the WebSocket."""
        if isinstance(message, bytes):
            # Ticks are binary data
            self._trader.set_latest_price(message)
        else:
            try:
                data = json.loads(message)
                if data.get("type") == "order_update":
                    self._trader.refresh_open_positions_and_buy_price()
                    await self._subscribe_to_instruments()
            except json.JSONDecodeError:
                logging.warning(f"Received non-JSON text: {message}")

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
        self.create_session()
        logging.debug("MStock session created for production.")
