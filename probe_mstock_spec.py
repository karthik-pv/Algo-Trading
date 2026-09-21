"""
Probe MStock API for an OpenAPI/Swagger spec and any order-cancel route
variants. Read-only probes except where noted.
"""
import json
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


def probe(path, method="GET"):
    headers = {
        "X-Mirae-Version": "1",
        "X-PrivateKey": API_KEY,
        "Authorization": f"Bearer {JWT}",
        "Content-Type": "application/json",
    }
    conn = http.client.HTTPSConnection("api.mstock.trade", timeout=10, context=CTX)
    conn.request(method, path, body="{}" if method != "GET" else None, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", errors="replace")
    conn.close()
    print(f"[{method}] {path} -> HTTP {resp.status} | {raw[:180]}")
    return resp.status, raw


print("=== swagger/spec probes ===")
for p in (
    "/swagger/v1/swagger.json",
    "/swagger/index.html",
    "/openapi/typeb/swagger.json",
    "/openapi/swagger.json",
):
    try:
        probe(p)
    except Exception as e:
        print(f"[GET] {p} -> {e}")
