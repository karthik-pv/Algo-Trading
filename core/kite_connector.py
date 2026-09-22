import os
import json
from datetime import datetime
import logging
import kiteconnect
from dotenv import load_dotenv
from loguru import logger
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse


from core.utils import get_access_token_from_json , write_to_json , fetch_from_json

#logging.basicConfig(level=logging.DEBUG)

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
        logger.debug("KiteConnect instance created.")

    def create_session(self):
        logger.info("Please visit the following URL to authorize the application:")
        logger.debug(self._kite.login_url())
        login_url = self._kite.login_url()        
        #request_token = input("Enter request token here - ")

        # 1. Automatically open the browser
        webbrowser.open_new(login_url)
        
        # 2. Start a temporary local HTTP server to listen for the redirect
        # Override via KITE_REDIRECT_PORT in .env; must match the Redirect
        # URL port configured in the Kite Developer Console.
        port = int(os.getenv("KITE_REDIRECT_PORT", "8080"))
        logger.info(f"Waiting for redirect with request token on port {port}...")
        
        server_address = ('127.0.0.1', port)
        httpd = HTTPServer(server_address, RequestTokenHandler)
        httpd.request_token = None
        
        # Wait until a request is handled and the token is captured
        while httpd.request_token is None:
            httpd.handle_request()
            
        request_token = httpd.request_token
        logger.info("Successfully captured request token automatically!")


        if not KITE_SECRET_KEY:
            raise ValueError("KITE_SECRET_KEY not found in environment variables")
        data = self._kite.generate_session(
            request_token=request_token, api_secret=os.getenv("KITE_SECRET_KEY")
        )
        #logger.debug(f"Profile data received: {data}")  # contains access_token
        self.set_access_token(data["access_token"])
        write_to_json({"kite_access_token" : data["access_token"] , "kite_last_token_timestamp" : datetime.now().isoformat()} , "access_token.json")
        #logger.debug(f"Access token set: {data['access_token']}")  # do not log the token


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
        #logger.debug(f"Access token set: {access_token}")  # do not log the token

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
        last_updated_date = fetch_from_json("access_token.json" , "kite_last_token_timestamp")
        last_update_datetime = datetime.fromisoformat(last_updated_date)
        today_date = datetime.now().date()
        if not last_update_datetime.date() == today_date:
            self.create_session()
        else:
            logging.debug("Access token already generated for the day")
            access_token = fetch_from_json("access_token.json" , "kite_access_token")
            self.set_access_token(access_token)
        logger.debug("Kite session created for production.")


class RequestTokenHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Parse the query parameters from the path
        query_components = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "request_token" in query_components:
            # Store the token in the server object to access it later
            self.server.request_token = query_components["request_token"][0]
            
            # Send a success response to the browser
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Login successful! You can close this window and return to the app.</h1></body></html>")
        else:
            # Handle failure
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Error: No request token found.</h1></body></html>")
