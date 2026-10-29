#!/usr/bin/env python3
"""
TREPA receiver — webhook endpoint for TradingView alert JSON (via ngrok).
- POST /            -> any path accepted
- Raw body parsed tolerantly (bare NaN -> None per handoff doc §6.1)
- Routed by MSG (DATA / LEVELS / other), keyed by SYM
- Appends every message to  alerts_log.jsonl  (raw + received_at)
- Maintains   state_latest.json  (latest DATA + LEVELS per SYM, rolling stats)
- GET /health -> quick check;  GET /dump -> last 20 raw messages
"""

import json
import time
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import deque
from threading import Lock

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "alerts_log.jsonl")
STATE = os.path.join(BASE, "state_latest.json")
PORT = 8080

lock = Lock()
recent = deque(maxlen=50)          # last N messages (raw) for noise-burst merging
msg_count = 0
start_ts = time.time()

def tolerant_loads(body: bytes):
    def nan_to_none(c):
        return None
    try:
        return json.loads(body.decode("utf-8", errors="replace"),
                          parse_constant=nan_to_none)
    except Exception as e:
        return {"_parse_error": str(e), "_raw": body[:2000].decode("utf-8", errors="replace")}

def update_state(msg):
    """Merge a parsed message into state_latest.json."""
    if os.path.exists(STATE):
        try:
            with open(STATE) as f:
                state = json.load(f)
        except Exception:
            state = {}
    else:
        state = {}

    sym = msg.get("SYM", "UNKNOWN")
    mtype = msg.get("MSG", "UNKNOWN")
    entry = state.setdefault(sym, {})
    entry["last_" + mtype] = msg
    entry["last_received_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    # counters for burst detection
    entry["count_" + mtype] = entry.get("count_" + mtype, 0) + 1
    state["_meta"] = {"total": msg_count, "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ts))}

    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE)

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global msg_count
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        msg = tolerant_loads(body)

        with lock:
            msg_count += 1
            rec = {"received_at": time.time(),
                   "received_hhmmss": time.strftime("%H:%M:%S"),
                   "remote": self.client_address[0],
                   "msg": msg}
            recent.append(rec)
            with open(LOG, "a") as f:
                f.write(json.dumps(rec) + "\n")
            if not isinstance(msg.get("_parse_error"), str):
                try:
                    update_state(msg)
                except Exception as e:
                    print("state update failed:", e)

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        if self.path.startswith("/health"):
            out = {"status": "ok", "msgs": msg_count, "uptime_s": int(time.time() - start_ts)}
        elif self.path.startswith("/dump"):
            with lock:
                out = {"recent": list(recent)[-20:]}
        else:
            out = {"status": "TREPA receiver", "msgs": msg_count}
        payload = json.dumps(out, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print(time.strftime("%H:%M:%S"), self.client_address[0], fmt % args, flush=True)

if __name__ == "__main__":
    print(f"TREPA receiver on :{PORT}  ->  {LOG}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
