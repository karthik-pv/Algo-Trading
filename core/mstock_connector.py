import os
import logging
from dotenv import load_dotenv

from utils import get_access_token_from_json

logging.basicConfig(level=logging.DEBUG)

load_dotenv()

MSTOCK_API_KEY = os.getenv("MSTOCK_API_KEY")
MSTOCK_SECRET_KEY = os.getenv("MSTOCK_SECRET_KEY")


class MStockSingleton:
    _instance = None
    _client = None
    _access_token = None
    _socket = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MStockSingleton, cls).__new__(cls)
            cls._instance._initialize_client()
        return cls._instance

    def _initialize_client(self):
        if not MSTOCK_API_KEY:
            raise ValueError("MSTOCK_API_KEY not found in environment variables")

        # Replace this with actual client init for MStock
        self._client = MStockAPI(api_key=MSTOCK_API_KEY)
        logging.debug("MStock API client created.")

    def create_session(self):
        print(self._client.login_url())
        request_token = input("Please paste the request token obtained here - ")
        if not MSTOCK_SECRET_KEY:
            raise ValueError("MSTOCK_SECRET_KEY not found in environment variables")

        data = self._client.generate_session(
            request_token=request_token, api_secret=MSTOCK_SECRET_KEY
        )
        logging.debug(f"Session data received: {data}")
        self.set_access_token(data["access_token"])

    def get_socket_connection(self):
        if self._socket is None:
            self._socket = MStockWebSocket(
                api_key=MSTOCK_API_KEY, access_token=self._access_token
            )
        return self._socket

    def get_client(self):
        return self._client

    def set_access_token(self, access_token):
        self._client.set_access_token(access_token)
        self._access_token = access_token
        logging.debug(f"Access token set: {access_token}")

    def get_access_token(self):
        return get_access_token_from_json()

    # development
    def initialise_for_dev(self):
        access_token = self._instance.get_access_token()
        if not access_token:
            access_token = input("Paste your access token for development: ").strip()
        self.set_access_token(access_token)

    # production
    def initialise_for_prod(self):
        self.create_session()
        logging.debug("MStock session created for production.")
