import os
import json
import logging
import kiteconnect
from dotenv import load_dotenv

from utils import get_access_token_from_json

logging.basicConfig(level=logging.DEBUG)

load_dotenv()

KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_SECRET_KEY = os.getenv("KITE_SECRET_KEY")


# Connection and authorization code present in this file
# NO BUSINESS LOGIC RESIDES IN THIS FILE
# NEED NOT CHANGE AT ALL


class KiteSingleton:
    _instance = None
    _kite = None
    _access_token = None
    _kite_socket = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(KiteSingleton, cls).__new__(cls)
            cls._instance._initialize_kite()
        return cls._instance

    def _initialize_kite(self):
        if not KITE_API_KEY:
            raise ValueError("KITE_API_KEY not found in environment variables")

        self._kite = kiteconnect.KiteConnect(api_key=KITE_API_KEY)
        logging.debug("KiteConnect instance created.")

    def create_session(self):
        print(self._kite.login_url())
        request_token = input("Please paste the request token obtained here - ")
        if not KITE_SECRET_KEY:
            raise ValueError("KITE_SECRET_KEY not found in environment variables")
        data = self._kite.generate_session(
            request_token=request_token, api_secret=os.getenv("KITE_SECRET_KEY")
        )
        logging.debug(f"Profile data received: {data}")
        self.set_access_token(data["access_token"])
        logging.debug(f"Access token set: {data['access_token']}")

    def get_kite_socket_connection(self):
        if self._kite_socket is None:
            self._kite_socket = kiteconnect.KiteTicker(
                api_key=KITE_API_KEY, access_token=self._access_token
            )
        return self._kite_socket

    def get_kite(self):
        return self._kite

    def set_access_token(self, access_token):
        self._kite.set_access_token(access_token)
        self._access_token = access_token
        logging.debug(f"Access token set: {access_token}")

    def get_access_token(self):
        return get_access_token_from_json()

    # development
    def initialise_kite_for_dev(self):
        access_token = self._instance.get_access_token()
        if not access_token:
            access_token = input("Paste your access token for development: ").strip()
        self.set_access_token(access_token)

    # prod
    def initialise_kite_for_prod(self):
        self.create_session()
        logging.debug("Kite session created for production.")
