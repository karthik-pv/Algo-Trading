"""Attempt to cancel the 4 remaining O-Pending orders (user-approved)."""
import json
import time
import os
import sys
import http.client
import ssl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("MSTOCK_API_KEY")
with open("data/access_token.json", "r", encoding="utf-8") as f:
    JWT = json.load(f)["mstock_jwt_token"]

CTX = ssl.create_default_context()
HEADERS = {
    "X-Mirae-Version": "1",
    "X-PrivateKey": API_KEY,
    "Authorization": f"Bearer {JWT}",
    "Content-Type": "application/json",
}


def call(method, path, body=None):
    conn = http.client.HTTPSConnection("api.mstock.trade", timeout=15, context=CTX)
    conn.request(method, path, body=body, headers=HEADERS)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", errors="replace")
    conn.close()
    return resp.status, raw


def pending_ids():
    status, raw = call("GET", "/openapi/typeb/orders")
    data = json.loads(raw).get("data", [])
    return {
        o["orderid"]: o
        for o in data
        if str(o.get("status") or o.get("orderstatus") or "").upper() == "O-PENDING"
    }


for round_no in (1, 2, 3):
    pending = pending_ids()
    if not pending:
        print(f"round {round_no}: all clear!")
        break

    print(f"round {round_no}: attempting {len(pending)} cancel(s)...")
    for oid, o in pending.items():
        status, raw = call("DELETE", f"/openapi/typeb/orders/regular/{oid}", "{}")
        ok = status in (200, 202, 204)
        print(f"  {oid} ({o.get('tradingsymbol')}) -> HTTP {status} "
              f"{'OK' if ok else raw[:110]}")
        time.sleep(0.4)

    time.sleep(1.5)

final = pending_ids()
print()
print(f"final pending: {len(final)}")
for oid, o in final.items():
    print(f"  STILL PENDING: {oid} | {o.get('tradingsymbol')} | "
          f"{o.get('transactiontype')} {o.get('quantity')} | updt {o.get('updatetime')}")
