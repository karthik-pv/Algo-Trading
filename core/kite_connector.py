import os
import logging
import kiteconnect
from dotenv import load_dotenv

logging.basicConfig(level=logging.DEBUG)

load_dotenv()

KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_SECRET_KEY = os.getenv("KITE_SECRET_KEY")

# Connection and auth esstablishment...........NEED NOT CHANGE AT ALL
# NO BUSINESS LOGIC RESIDES IN THIS FILE


class KiteSingleton:
    _instance = None
    _kite = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(KiteSingleton, cls).__new__(cls)
            cls._instance._initialize_kite()
        return cls._instance

    def _initialize_kite(self):
        api_key = os.getenv("KITE_API_KEY")
        if not api_key:
            raise ValueError("KITE_API_KEY not found in environment variables")

        self._kite = kiteconnect.KiteConnect(api_key=KITE_API_KEY)
        logging.debug("KiteConnect instance created.")

    def create_session(self):
        print(self._kite.login_url())
        request_token = input("Please paste the request token obtained here - ")
        data = self._kite.generate_session(
            request_token=request_token, api_secret=os.getenv("KITE_SECRET_KEY")
        )
        logging.debug(f"Data received: {data}")
        self._kite.set_access_token(data["access_token"])
        logging.debug(f"Access token set: {data['access_token']}")

    def get_kite(self):
        return self._kite

    # for development purposes only
    def set_access_token(self, access_token):
        self._kite.set_access_token(access_token)
        logging.debug(f"Access token set: {access_token}")


# development
def initialise_kite_for_dev():
    kite_instance = KiteSingleton()
    access_token = input("Paste your access token for debugging: ").strip()
    kite_instance.set_access_token(access_token)


# prod
def initialise_kite_for_prod():
    kite_instance = KiteSingleton()
    kite_instance.create_session()
    logging.debug("Kite session created for production.")
