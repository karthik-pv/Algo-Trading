import os
import json
import logging
import http.client
from dotenv import load_dotenv

from utils import fetch_from_json

logging.basicConfig(level=logging.DEBUG)

load_dotenv()

MSTOCK_API_KEY = os.getenv("MSTOCK_API_KEY")
MSTOCK_CLIENT_CODE = os.getenv("MSTOCK_CLIENT_CODE")
MSTOCK_CLIENT_PASSWORD = os.getenv("MSTOCK_CLIENT_PASSWORD")


class MStockSingleton:
    _instance = None
    _client = None
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
        print(json_data)
        print(headers)
        conn.request(
            "POST",
            "/openapi/typeb/session/token",
            json.dumps(json_data),
            headers,
        )
        response = json.loads(conn.getresponse().read().decode("utf-8"))
        access_token = response["data"]["jwt_token"]
        self.set_access_token(access_token)
        print(access_token)

    def get_client(self):
        return self._client

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
