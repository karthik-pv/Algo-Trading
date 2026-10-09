import os
import sys
import json
import time
import hmac
import base64
import struct
import hashlib
import subprocess
import threading
from datetime import datetime
import logging
import requests
import kiteconnect
from kiteconnect.exceptions import (
    TokenException, InputException, PermissionException,
)
from dotenv import load_dotenv
from loguru import logger
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse


from core.utils import get_access_token_from_json , write_to_json , fetch_from_json , windows_login_dialog

#logging.basicConfig(level=logging.DEBUG)

load_dotenv()

KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_SECRET_KEY = os.getenv("KITE_SECRET_KEY")
KITE_USER_ID = os.getenv("KITE_USER_ID")
KITE_PASSWORD = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

KITE_LOGIN_BASE = "https://kite.zerodha.com"
KITE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class Kite2FAAborted(RuntimeError):
    pass


def _totp_code(secret):
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int(time.time() // 30)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def _prompt_kite_2fa(twofa_type, attempts_left=None):
    label = "App Code shown in your Kite app" if twofa_type == "app_code" else f"{twofa_type.upper()} code from your authenticator app"
    if attempts_left is not None:
        label += f"  ({attempts_left} attempt(s) remain before lockout)"
    if sys.stdin is not None and sys.stdin.isatty():
        return input(f"Please enter Kite {label} - ").strip()

    if sys.platform == "win32":
        return windows_login_dialog(
            "AlgoOptionScalper - Zerodha 2FA",
            f"Kite login: enter the 6-digit {label}",
        )

    if sys.platform != "darwin":
        raise RuntimeError(
            f"Kite login needs the {twofa_type} code but no console is attached"
        )

    script = (
        "text returned of (display dialog "
        f'"Kite login: enter the 6-digit {label}" '
        'default answer "" with hidden answer '
        'with title "AlgoOptionScalper - Zerodha 2FA" '
        'buttons {"Cancel", "OK"} default button "OK")'
    )
    out = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=300,
    )
    if out.returncode != 0:
        raise RuntimeError("Kite 2FA entry cancelled")
    code = out.stdout.strip()
    if not code:
        raise RuntimeError("Empty Kite 2FA code entered")
    return code


# Connection and authorization code present in this file
# NO BUSINESS LOGIC RESIDES IN THIS FILE
# NEED NOT CHANGE AT ALL


class KiteSingleton:
    _instance = None
    _kite = None
    _access_token = None
    _kite_socket = None

    # Mid-session re-login guard. A dead token surfaces as 403 on many
    # concurrent calls (the ~15s pending-orders poll, grid refresh,
    # positions sync ...); without a lock+cooldown they would stampede
    # the login flow and repeated 2FA prompts risk locking the account.
    _relogin_lock = threading.Lock()
    _last_relogin_ts = 0.0
    _no_totp_notice_logged = False

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
        # Serialize ALL login flows. Startup runs create_session() while
        # monitor threads (fund-summary refresh, Feed Lab) can trigger
        # ensure_fresh_session() at the same moment. Two parallel logins
        # for the same user invalidate each other's 2FA challenge and
        # both TOTP submissions then come back "Invalid TOTP" (observed
        # 2026-10-09: two flows 200ms apart, both rejected, 2 lockout
        # attempts burned on a perfectly valid secret). Sharing the
        # ensure_fresh_session relogin lock makes them run one after
        # the other.
        with KiteSingleton._relogin_lock:
            # A concurrent flow that finished while we waited may have
            # already refreshed the token - keep it instead of logging
            # in a second time (each needless login burns TOTP window).
            if self._access_token and self._token_is_accepted():
                logger.info("Kite session already refreshed by a concurrent login - reusing it.")
                return
            try:
                self.create_session_automated()
                return
            except Exception as e:
                logger.warning(f"Automated Kite login failed ({e}) - falling back to browser login")

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


    def create_session_automated(self, max_attempts=3):
        if not KITE_USER_ID or not KITE_PASSWORD:
            raise ValueError("KITE_USER_ID / KITE_PASSWORD not set in .env")

        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                self._create_session_automated_once()
                return
            except Kite2FAAborted:
                raise
            except (requests.RequestException, RuntimeError, ValueError) as e:
                last_error = e
                logger.warning(f"Kite automated login attempt {attempt}/{max_attempts} failed: {e}")
                if attempt < max_attempts:
                    time.sleep(2 * attempt)
        raise RuntimeError(f"Kite automated login failed after {max_attempts} attempts: {last_error}")

    def _create_session_automated_once(self):
        session = requests.Session()
        session.headers.update({"X-Kite-Version": "3", "User-Agent": KITE_UA})

        for seed_attempt in range(2):
            seed_resp = session.get(
                f"{KITE_LOGIN_BASE}/connect/login",
                params={"api_key": KITE_API_KEY},
                timeout=15,
            )
            if "kf_session" in session.cookies and seed_resp.status_code == 200:
                break
            logger.warning(
                f"Kite login page seed weak (HTTP {seed_resp.status_code}, "
                f"kf_session={'yes' if 'kf_session' in session.cookies else 'no'}) - retrying"
            )
            time.sleep(1)

        resp = session.post(
            f"{KITE_LOGIN_BASE}/api/login",
            data={"user_id": KITE_USER_ID, "password": KITE_PASSWORD},
            timeout=15,
        )
        try:
            login_resp = resp.json()
        except ValueError:
            raise RuntimeError(f"Kite login page returned HTTP {resp.status_code}")

        data = login_resp.get("data") or {}
        if "request_url" in data or "request_token" in data:
            self._finish_automated_session(session, data)
            return

        twofa_type = str(data.get("twofa_type") or "").lower()
        if not twofa_type:
            raise RuntimeError(
                login_resp.get("message") or
                f"Kite credentials rejected (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        logger.debug(f"Kite 2FA required: type={twofa_type}")

        user_id = data.get("user_id") or KITE_USER_ID
        request_id = data.get("request_id")
        wrong_codes = 0
        attempts_left = None
        while True:
            if KITE_TOTP_SECRET and twofa_type in ("totp", "app_code"):
                code = _totp_code(KITE_TOTP_SECRET)
            else:
                code = _prompt_kite_2fa(twofa_type, attempts_left)

            resp = session.post(
                f"{KITE_LOGIN_BASE}/api/twofa",
                data={
                    "user_id": user_id,
                    "request_id": request_id,
                    "twofa_type": twofa_type,
                    "twofa_code": code,
                },
                timeout=15,
            )
            try:
                twofa_resp = resp.json()
            except ValueError:
                raise RuntimeError(f"Kite 2FA step returned HTTP {resp.status_code}")

            twofa_data = twofa_resp.get("data") or {}
            if twofa_resp.get("status") == "success" and (
                "request_url" in twofa_data or "request_token" in twofa_data
            ):
                self._finish_automated_session(session, twofa_data)
                return

            message = twofa_resp.get("message") or f"Kite 2FA failed (HTTP {resp.status_code})"
            attempts_left = twofa_data.get("attempts_remaining")
            wrong_codes += 1
            if wrong_codes >= 2 or (attempts_left is not None and attempts_left <= 1):
                raise Kite2FAAborted(
                    f"{message}"
                    + (f" ({attempts_left} attempt(s) remain before lockout)" if attempts_left is not None else "")
                    + " - falling back to browser login to be safe"
                )
            logger.warning(f"Kite 2FA code rejected: {message}")
            if KITE_TOTP_SECRET:
                raise Kite2FAAborted(f"{message} - TOTP secret did not work, falling back to browser login")

    def _finish_automated_session(self, session, data):
        request_token = data.get("request_token")
        if not request_token:
            resp = session.get(data["request_url"], allow_redirects=False, timeout=15)
            location = resp.headers.get("Location", "")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
            request_token = (params.get("request_token") or [None])[0]
        if not request_token:
            raise RuntimeError("Kite login finished without a request token")

        if not KITE_SECRET_KEY:
            raise ValueError("KITE_SECRET_KEY not found in environment variables")
        session_data = self._kite.generate_session(
            request_token=request_token, api_secret=KITE_SECRET_KEY
        )
        self.set_access_token(session_data["access_token"])
        write_to_json(
            {
                "kite_access_token": session_data["access_token"],
                "kite_last_token_timestamp": datetime.now().isoformat(),
            },
            "access_token.json",
        )
        logger.info("Kite session created with in-app login (no browser).")

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
    def initialise_kite_for_prod(self, allow_interactive=True):
        last_updated_date = fetch_from_json("access_token.json" , "kite_last_token_timestamp")
        last_update_datetime = datetime.fromisoformat(last_updated_date)
        today_date = datetime.now().date()
        if last_update_datetime.date() == today_date:
            logging.debug("Access token already generated for the day")
            access_token = fetch_from_json("access_token.json" , "kite_access_token")
            self.set_access_token(access_token)
            # A same-day token can still be DEAD - Kite invalidates it
            # when a new session is generated elsewhere or the account
            # signs out. Trusting the date alone made the app limp
            # through the whole day on 403s with an inactive pill and
            # empty grids. One cheap call decides: rejected tokens get
            # a fresh login right here at startup.
            if not self._token_is_accepted():
                logger.warning(
                    "Same-day Kite token is rejected by the API - "
                    "running a fresh login."
                )
                if not allow_interactive:
                    # Background (Feed Lab) session check: the ONLY
                    # safe re-login is the silent TOTP one. NEVER run
                    # create_session() off the main thread - its
                    # dialog/browser fallback puts tkinter on a
                    # non-main thread, which hard-crashes pythonw
                    # (tcl86t.dll APPCRASH, 2026-10-09).
                    if self.ensure_fresh_session():
                        logger.info(
                            "Kite session silently renewed in the "
                            "background."
                        )
                    else:
                        logger.error(
                            "Kite token rejected and silent re-login "
                            "unavailable (KITE_TOTP_SECRET unset or "
                            "failed) - the Feed Lab K-side stays "
                            "degraded until a manual login. Trading "
                            "on the active broker is unaffected."
                        )
                else:
                    self.create_session()
        elif allow_interactive:
            self.create_session()
        else:
            logger.error(
                "Kite token is from a previous day and the background "
                "session check is non-interactive - no browser/dialog "
                "login can run off the main thread (tkinter APPCRASH). "
                "The Feed Lab K-side stays degraded until a manual "
                "login. Trading on the active broker is unaffected."
            )

    def _token_is_accepted(self):
        """One cheap authenticated call: True only when Kite accepts
        the current token. margins() is used because it is a core
        endpoint every Kite Connect app can call (profile is not).
        Rejections come in TWO shapes - TokenException 'Incorrect
        api_key or access_token' AND PermissionException 'Insufficient
        permission for that call' (observed 2026-10-01: a wedged token
        answered every core endpoint with the permission error, and a
        fresh token worked immediately). Both mean: run a fresh
        login. Other errors are inconclusive - keep the token."""
        try:
            self._kite.margins()
            return True
        except (TokenException, InputException, PermissionException) as e:
            logger.warning(f"Kite token check rejected: {e}")
            return False
        except Exception as e:
            logger.warning(
                f"Kite token check inconclusive "
                f"({type(e).__name__}: {e}) - keeping session"
            )
            return True

    def ensure_fresh_session(self, min_interval_seconds=60):
        """
        Re-login when the current token is rejected mid-session
        (adapter methods call this on a token rejection and retry the
        call once on True).

        NON-INTERACTIVE BY DESIGN. Mid-session re-login only runs when
        it can complete silently (KITE_TOTP_SECRET configured). An
        unattended machine must never pop passcode dialogs in a loop -
        with a dead token the ~15s polls would re-trigger the dialog
        every cooldown, nagging a user who is away (and repeated
        automated prompts risk locking the account). Without a TOTP
        secret this returns False immediately: the session pill shows
        "login needed" and restarting the app runs the full login flow
        (app-code prompt / browser fallback) where the user expects it.
        Interactive or not, a lock+cooldown guards the login attempts.
        """
        if not KITE_TOTP_SECRET:
            if not KiteSingleton._no_totp_notice_logged:
                KiteSingleton._no_totp_notice_logged = True
                logger.error(
                    "Kite token rejected mid-session and KITE_TOTP_SECRET "
                    "is not configured, so re-login cannot run silently. "
                    "Automatic dialogs are suppressed while unattended - "
                    "restart the app to log in."
                )
            return False
        now = time.time()
        if now - KiteSingleton._last_relogin_ts < min_interval_seconds:
            return False
        if not KiteSingleton._relogin_lock.acquire(blocking=False):
            return False
        try:
            KiteSingleton._last_relogin_ts = time.time()
            self.create_session_automated()
            KiteSingleton._no_totp_notice_logged = False
            logger.info("Kite session refreshed mid-run (automated login).")
            return True
        except Kite2FAAborted as e:
            logger.warning(f"Kite re-login aborted: {e}")
            return False
        except Exception as e:
            logger.error(f"Kite re-login failed: {e}")
            return False
        finally:
            KiteSingleton._relogin_lock.release()


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
