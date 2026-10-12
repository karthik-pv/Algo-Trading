"""Kite TOTP pre-flight doctor.

Offline / read-only checks so any login-config problem is caught BEFORE
touching the login flow (each rejected 2FA submission burns one of the
few attempts before the account locks):

  1. .env credentials present
  2. KITE_TOTP_SECRET decodes (spaces/dashes/case tolerated)
  3. Live code generation (compare the printed code with Google
     Authenticator - they must match before attempting a login)
  4. Local clock vs Zerodha server clock (unauthenticated Date header)
  5. Cached access_token.json age + live acceptance via margins()

Run:  .venv\\Scripts\\python.exe scripts\\kite_totp_doctor.py
"""

import base64
import binascii
import email.utils
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

WINDOW = 30
SKEW_LIMIT = 15.0

failures = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def b32_key(secret):
    clean = "".join(secret.split()).replace("-", "").upper()
    return base64.b32decode(
        clean + "=" * ((8 - len(clean) % 8) % 8), casefold=True
    )


def totp_code(key, counter=None):
    import hashlib
    import hmac
    import struct

    counter = counter if counter is not None else int(time.time() // WINDOW)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def main():
    print("=== Kite TOTP doctor ===")
    print(f"repo: {ROOT}")
    print()

    # 1. credentials
    creds = {k: os.getenv(k) for k in (
        "KITE_API_KEY", "KITE_SECRET_KEY", "KITE_USER_ID",
        "KITE_PASSWORD", "KITE_TOTP_SECRET",
    )}
    for name, value in creds.items():
        shown = f"set ({len(value)} chars)" if value else "MISSING"
        if "SECRET" in name and value:
            shown = "set (value hidden)"
        check(f".env {name}", bool(value), shown)
    print()

    # 2. + 3. secret format and live codes
    secret = creds["KITE_TOTP_SECRET"] or ""
    key = None
    if secret:
        try:
            key = b32_key(secret)
            check(
                "KITE_TOTP_SECRET decodes",
                len(key) >= 10,
                f"{len(key)} key bytes (standard enrolments are 20)",
            )
        except (binascii.Error, ValueError) as e:
            check("KITE_TOTP_SECRET decodes", False, str(e))
    if key:
        now_window = int(time.time() // WINDOW)
        remaining = WINDOW - int(time.time() % WINDOW)
        print()
        print("  Compare with your authenticator app NOW:")
        print(f"    current code: {totp_code(key, now_window)}  (~{remaining}s left)")
        print(f"    next code:    {totp_code(key, now_window + 1)}")
        print("  They must match before any login attempt is made.")
    print()

    # 4. clock skew
    try:
        resp = requests.get("https://kite.zerodha.com/", timeout=10)
        server = email.utils.parsedate_to_datetime(resp.headers["Date"])
        skew = time.time() - server.timestamp()
        check(
            "clock skew vs Zerodha",
            abs(skew) <= SKEW_LIMIT,
            f"{skew:+.1f}s (limit {SKEW_LIMIT}s)",
        )
    except Exception as e:
        check("clock skew vs Zerodha", False, f"inconclusive: {e}")
    print()

    # 5. cached token
    token_file = ROOT / "data" / "access_token.json"
    token = None
    if token_file.exists():
        try:
            data = json.loads(token_file.read_text(encoding="utf-8"))
            token = data.get("kite_access_token")
            ts = data.get("kite_last_token_timestamp")
            age = ""
            if ts:
                when = datetime.fromisoformat(ts)
                age = f", generated {when:%Y-%m-%d %H:%M}"
            check("access_token.json", bool(token), f"token present{age}")
        except Exception as e:
            check("access_token.json", False, f"unreadable: {e}")
    else:
        check("access_token.json", False, "file does not exist (no cached token)")

    if token and creds["KITE_API_KEY"]:
        try:
            from kiteconnect import KiteConnect
            from kiteconnect.exceptions import TokenException

            kite = KiteConnect(api_key=creds["KITE_API_KEY"])
            kite.set_access_token(token)
            kite.margins()
            check("cached token accepted (margins)", True)
        except TokenException as e:
            check("cached token accepted (margins)", False, str(e))
        except Exception as e:
            print(f"[SKIP] cached token check inconclusive: {e}")
    print()

    if failures:
        print(f"VERDICT: {len(failures)} problem(s) - fix before attempting a login.")
        return 1
    print("VERDICT: ready - a login attempt should succeed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
