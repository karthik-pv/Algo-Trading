"""
Feed Lab: dual-broker (KITE + M.Stock) tick capture for feed-quality
comparison.

Self-contained feature. To unplug completely, delete:
  - core/feed_capture.py (this file)
  - the "Feed Lab" block of routes in server.py (marked block)
  - the Feed Lab section (markup, styles, script) of the System Audit
    tab in templates/app.html
  - the FEED_LAB_ENABLED key in config/appconfig.json
The FEED_LAB_ENABLED flag alone hides the section and disables the
routes without any code change.
"""

import os
import csv
import json
import re
import time
import base64
import socket as pysocket
import threading
import asyncio
from datetime import datetime, date

import pandas as pd
import requests
import websockets
from loguru import logger
from kiteconnect import KiteTicker
from core import broker_stats

from core.utils import resolve_data_path, fetch_from_json, DATA_DIR
from adapter.mstock_utils import parse_quote_message
from core.kite_connector import KITE_API_KEY, KiteSingleton
from core.mstock_connector import MStockSingleton, WS_HOST, WS_PORT, _prompt_otp

UNDERLYING_CONFIG = {
    "SENSEX": {"exchange": "BFO", "step": 100, "mstock_seg": "BFO"},
    "NIFTY": {"exchange": "NFO", "step": 100, "mstock_seg": "NFO"},
    "CRUDEOILM": {"exchange": "MCX", "step": 10, "mstock_seg": "MCX"},
    "CRUDEOIL": {"exchange": "MCX", "step": 50, "mstock_seg": "MCX"},
}

MSTOCK_EXCHANGE_TYPE = {"NSE": 2, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5}
CSV_HEADER = ["recv_ns", "recv_iso", "token", "symbol", "ltp", "bid", "ask", "volume", "oi"]
STALL_SECONDS = 5.0

# Report analysis window per underlying (Asia/Kolkata wall clock).
# NIFTY/SENSEX trade 09:15-15:30 but the user asked for 09:15-15:45;
# CRUDEOIL analysis is scoped to the morning 09:00-11:30. Everything a
# capture recorded outside this window is ignored by the report.
MARKET_WINDOWS = {
    "NIFTY": ((9, 15), (15, 45)),
    "SENSEX": ((9, 15), (15, 45)),
    "CRUDEOIL": ((9, 0), (11, 30)),
    "CRUDEOILM": ((9, 0), (11, 30)),
}

# ------------------------------------------------------------------
# CHAIN CAPTURE (always-on, market hours): per underlying, capture the
# near-month FUT plus every CE and PE at ATM +/- CHAIN_STRIKES strikes
# from BOTH brokers - so any trade's holding-window peak (max LTP
# between buy and sell) is reconstructable from disk afterwards, even
# after app restarts. Runs for every underlying in CHAIN_UNDERLYINGS
# simultaneously; the manual Feed Lab capture and the position-scoped
# capture continue to exist alongside it.
# ------------------------------------------------------------------
CHAIN_UNDERLYINGS = ("NIFTY", "SENSEX")
CHAIN_STRIKES = 5          # ATM +/- 5 strikes -> 11 strikes x CE & PE
_CHAIN_POLL_S = 30.0       # supervisor poll interval
_CHAIN_TAG_PREFIX = "chain"

# A silence longer than this between consecutive captured rows means
# the capture itself was not running (app closed/restarted or capture
# stopped) - it is an app-uptime boundary, never a feed stall.
CAPTURE_RUN_BOUNDARY_S = 60.0

# INTERNET pill: neutral, speed-oriented targets - deliberately NOT
# broker endpoints, so broker outages never masquerade as internet loss.
_INTERNET_TARGETS = [
    "https://www.google.com/generate_204",
    "https://1.1.1.1",
]
# K / M pills: per-broker API health, probed and shown separately.
_BROKER_TARGETS = {
    "KITE": "https://api.kite.trade",
    "MSTOCK": "https://api.mstock.trade",
}
NET_CHECK_INTERVAL_SECONDS = 10.0
NET_CSV_HEADER = ["ts_ns", "ts_iso", "target", "ok", "latency_ms"]

# Latency thresholds per pill group (ms). Probes ride a keep-alive
# session (see _probe_session), so a sample measures the raw network
# path - RTT + server time - with no per-probe DNS/TCP/TLS handshake
# tax. Internet: green <=30ms is scalping-grade first mile (RTT ~25ms),
# amber 80ms means exits start slipping on the tick -> decision -> order
# chain, red 200ms is a genuinely degraded path. Broker API is
# order-critical: >500ms per call can slip SL/BP exits on a scalping
# cadence.
_NET_THRESHOLDS = {
    "internet": {"amber": 80.0, "red": 200.0},
    "broker": {"amber": 150.0, "red": 500.0},
}
# Hysteresis: escalate a pill only after the worse raw level is seen on
# this many consecutive evaluations (a single-sample spike must not
# flap the pill). De-escalation is immediate on recovery.
_ESCALATE_AFTER = 2

_net_lock = threading.Lock()
_net_stop = threading.Event()
_net_state = {
    "targets": {},
    "last_check_at": None,
    "monitor_running": False,
}

# ONE keep-alive session for all net probes: requests pools connections
# per host, so after the first cycle each probe reuses a warm TCP+TLS
# connection and the sample reflects the network path (RTT + server
# time), not a fresh DNS+TCP+TLS handshake (which floors the reading at
# ~60-80ms and makes a good connection read amber). Single writer:
# only _net_worker touches this session.
_probe_session = requests.Session()

_broker_ref = None
_feed_pill_state = {"level": None}


def register_broker(broker):
    """Server hands the live broker adapter in at startup so the FEED
    pill can read its websocket sampler without a circular import."""
    global _broker_ref
    _broker_ref = broker


def feed_status():
    global _broker_ref
    broker = _broker_ref
    if broker is None:
        return {"level": "idle", "reason": "broker not initialised yet"}
    if not hasattr(broker, "feed_health"):
        return {"level": "idle", "reason": f"broker {type(broker).__name__} has no feed"}

    try:
        health = broker.feed_health()
    except Exception as e:
        logger.warning(f"Feed Lab: feed_health read failed: {e}")
        return {"level": "idle", "reason": "feed health unavailable"}

    since_tick = health.get("seconds_since_tick")
    connected = health.get("connected")

    # Market-hours gate FIRST: outside trading hours the pill must show
    # the gray IDLE state ("market closed"), never GREEN/AMBER/RED -
    # stray pre-open/heartbeat packets or a stale last tick are not a
    # live feed, and no amount of amber tells the user anything useful
    # when the exchange is shut.
    from core.utils import is_market_open
    try:
        market_open = is_market_open()
    except Exception:
        market_open = True  # session-config problem: fail LOUD, not idle

    if not market_open:
        level = "idle"
        reason = "market closed - no live feed expected"
    elif since_tick is None or not connected:
        level = "idle"
        reason = "no live feed data"
    elif since_tick < 3:
        level = "green"
        reason = None
    elif since_tick < 15:
        level = "amber"
        reason = None
    else:
        level = "red"
        reason = None

    prev = _feed_pill_state.get("level")
    if prev != level:
        logger.info(
            f"Feed Lab: feed pill {prev or 'INIT'} -> {level.upper()} | "
            f"connected={connected}, since_tick={since_tick}s, "
            f"rate={health.get('rate_per_min')}/min, max_gap={health.get('max_gap_s')}s"
        )
        _feed_pill_state["level"] = level

    return {"level": level, "reason": reason, "health": health}


def _net_record(url, ok, latency_ms):
    now_ns = time.time_ns()
    now_iso = datetime.now().isoformat()
    with _net_lock:
        state = _net_state["targets"].setdefault(url, {
            "last_ok": None, "last_latency_ms": None, "last_ok_at": None,
            "last_fail_at": None, "consec_failures": 0,
        })
        state["last_ok"] = ok
        state["last_latency_ms"] = latency_ms
        if ok:
            state["last_ok_at"] = now_iso
            state["consec_failures"] = 0
        else:
            state["last_fail_at"] = now_iso
            state["last_fail_latency_ms"] = latency_ms
            state["consec_failures"] = int(state.get("consec_failures", 0)) + 1
        _net_state["last_check_at"] = now_iso
    try:
        path = os.path.join(_day_dir(), "net.csv")
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(NET_CSV_HEADER)
            writer.writerow([now_ns, now_iso, url, 1 if ok else 0, latency_ms])
    except Exception as e:
        logger.error(f"Feed Lab: net CSV write failed: {e}")


def _net_level(urls, group="internet"):
    """Raw (instantaneous) health across the given target URLs - the
    worst of: consecutive probe failures, a failure in the last 30s, or
    latency breaches against the group's thresholds. Hysteresis is
    applied on top by _apply_hysteresis(); this function does not
    smooth anything."""
    amber_ms = _NET_THRESHOLDS[group]["amber"]
    red_ms = _NET_THRESHOLDS[group]["red"]
    with _net_lock:
        now = time.time()
        level = "green"
        for url in urls:
            state = _net_state["targets"].get(url) or {}
            if state.get("consec_failures", 0) >= 2:
                return "red"
            last_fail_at = state.get("last_fail_at")
            if state.get("last_ok") is False and last_fail_at:
                fail_age = now - datetime.fromisoformat(last_fail_at).timestamp()
                if fail_age < 30:
                    level = max(level, "amber", key=lambda x: ["green", "amber", "red"].index(x))
            latency = state.get("last_latency_ms")
            if latency is not None and latency > red_ms:
                return "red"
            if latency is not None and latency > amber_ms:
                level = max(level, "amber", key=lambda x: ["green", "amber", "red"].index(x))
    return level


def _fail_age(ts):
    try:
        return time.time() - float(ts) if ts else None
    except (TypeError, ValueError):
        return None


def _broker_api_level(broker, url):
    """API half of a broker pill = endpoint probe (reachability +
    latency) overlaid with live order-flow results from broker_stats:
    ONE failed order call -> red for 60s (order-critical; decays so the
    pill self-heals when the next order isn't due for hours); two
    consecutive failed data calls -> red; a recent data failure ->
    amber."""
    level = _net_level([url], "broker")
    st = broker_stats.snapshot(broker)
    api = st.get("api") or {}
    orders = st.get("orders") or {}

    if orders.get("failed"):
        order_fail_age = _fail_age(orders.get("last_fail_ts"))
        if order_fail_age is not None and order_fail_age < 60:
            return "red"
    if (api.get("consec_failures") or 0) >= 2:
        return "red"
    if level == "green":
        api_fail_age = _fail_age(api.get("last_fail_ts"))
        if api_fail_age is not None and api_fail_age < 30:
            level = "amber"
    return level


_SEVERITY = ["green", "amber", "red"]
_pill_states = {}


def _apply_hysteresis(pill, raw_level):
    """Smoothed pill level: escalation needs the worse raw level on
    _ESCALATE_AFTER consecutive evaluations (a single-sample spike must
    not flap the pill); de-escalation is immediate on recovery. While
    escalation is pending the level stays None - the pill simply stays
    hidden until the worse level is confirmed."""
    st = _pill_states.setdefault(pill, {"level": None, "raw": None, "streak": 0})
    if raw_level == st["raw"]:
        st["streak"] += 1
    else:
        st["raw"] = raw_level
        st["streak"] = 1
    current = st["level"] or "green"
    if _SEVERITY.index(raw_level) <= _SEVERITY.index(current):
        st["level"] = raw_level
    elif st["streak"] >= _ESCALATE_AFTER:
        st["level"] = raw_level
    return st["level"]


def _pill_level(pill):
    """Last hysteresis-applied level for a pill (None before the first
    probe cycle)."""
    return _pill_states.get(pill, {}).get("level")


def _log_pill_transition(pill, level):
    prev = _pill_states.get(pill, {}).get("level")
    if level != prev:
        logger.info(f"Feed Lab: {pill} pill {str(prev or 'INIT').upper()} -> {level.upper()}")
        _pill_states.setdefault(pill, {})["level"] = level


def _net_worker():
    while not _net_stop.is_set():
        for url in list(_INTERNET_TARGETS) + list(_BROKER_TARGETS.values()):
            if _net_stop.is_set():
                break
            started = time.time()
            ok = False
            try:
                response = _probe_session.get(url, timeout=2)
                ok = response.status_code < 500
            except requests.RequestException:
                # A pooled connection can go stale (NAT/idle timeout) and
                # fail once; drop the pool and retry immediately before
                # calling it a real failure.
                _probe_session.close()
                try:
                    response = _probe_session.get(url, timeout=2)
                    ok = response.status_code < 500
                except requests.RequestException:
                    ok = False
            _net_record(url, ok, round((time.time() - started) * 1000, 1))

        # Hysteresis is applied HERE (the single writer of pill state):
        # escalate after _ESCALATE_AFTER consecutive breaches, recover
        # immediately.
        _log_pill_transition("internet", _apply_hysteresis(
            "internet", _net_level(_INTERNET_TARGETS, "internet")))
        for broker, url in _BROKER_TARGETS.items():
            _log_pill_transition(broker, _apply_hysteresis(
                broker, _broker_api_level(broker, url)))
        _net_stop.wait(NET_CHECK_INTERVAL_SECONDS)


def start_net_monitor():
    with _net_lock:
        if _net_state["monitor_running"]:
            return
        _net_state["monitor_running"] = True
    _net_stop.clear()
    threading.Thread(target=_net_worker, daemon=True, name="feedlab-net").start()
    logger.info("Feed Lab: internet quality monitor started")


def _ws_level(health):
    """Pill color for a websocket-feed health snapshot (market gate is
    applied by the caller before this)."""
    since_tick = health.get("seconds_since_tick")
    connected = health.get("connected")
    if since_tick is None or not connected:
        return "idle"
    if since_tick < 3:
        return "green"
    if since_tick < 15:
        return "amber"
    return "red"


def _ws_health_for(broker):
    """
    WS (feed) half of a broker pill. A broker's websocket is only
    observable while its socket actually runs, so resolve by precedence:
      1. the ACTIVE broker adapter's feed_health() sampler
      2. the Feed Lab capture socket for that broker (while capturing)
      3. gray idle ("socket not running")
    Outside market hours the pill is idle regardless - no live feed is
    expected, so tick freshness is not meaningful.
    """
    from core.utils import is_market_open
    try:
        market_open = is_market_open()
    except Exception:
        market_open = True  # session-config problem: fail LOUD, not idle
    if not market_open:
        return {"level": "idle", "reason": "market closed - no live feed expected"}

    broker_ref = _broker_ref
    active = None
    if broker_ref is not None:
        name = type(broker_ref).__name__.lower()
        active = "KITE" if "kite" in name else "MSTOCK"
    if broker == active and hasattr(broker_ref, "feed_health"):
        try:
            health = broker_ref.feed_health()
            return {"level": _ws_level(health), **health}
        except Exception as e:
            logger.warning(f"Feed Lab: {broker} feed_health read failed: {e}")

    try:
        if feed_capture_service._running:
            health = feed_capture_service.capture_ws_health(broker)
            if health.get("connected") or health.get("seconds_since_tick") is not None:
                return {"level": _ws_level(health), **health}
    except Exception:
        pass

    return {"level": "idle", "reason": "socket not running"}


def _session_info(broker):
    """Token/session validity for the pill hover: Kite tokens are daily
    (refreshed ~06:00 IST), M.Stock is a JWT with an exp claim."""
    try:
        if broker == "KITE":
            ts = fetch_from_json("access_token.json", "kite_last_token_timestamp")
            if not ts:
                return {"valid": False, "detail": "no token - login needed"}
            token_dt = datetime.fromisoformat(ts)
            if token_dt.date() == datetime.now().date():
                return {
                    "valid": True,
                    "detail": f"token generated {token_dt.strftime('%H:%M')} - valid till ~06:00 IST",
                }
            return {"valid": False, "detail": f"token from {token_dt.date()} - login needed"}

        if broker == "MSTOCK":
            jwt = fetch_from_json("access_token.json", "mstock_jwt_token") or ""
            try:
                payload = jwt.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload))
                remaining = int(claims.get("exp") or 0) - time.time()
                if remaining > 0:
                    hours, mins = int(remaining // 3600), int((remaining % 3600) // 60)
                    return {"valid": True, "detail": f"JWT expires in {hours}h {mins}m"}
                return {"valid": False, "detail": "JWT expired - login needed"}
            except Exception:
                return {"valid": False, "detail": "no/invalid JWT - login needed"}
    except Exception as e:
        return {"valid": None, "detail": f"unknown ({e})"}
    return {"valid": None, "detail": "unknown broker"}


def net_status():
    """Health for the I / K / M pills: one level for pure internet speed
    (neutral targets) and, per broker, API + WebSocket (feed) + session
    sections."""
    with _net_lock:
        targets = {url: dict(state) for url, state in _net_state["targets"].items()}
        last_check_at = _net_state["last_check_at"]
        monitor_running = _net_state["monitor_running"]

    live = monitor_running and targets
    internet_urls = list(_INTERNET_TARGETS)
    brokers = {}
    for broker, url in _BROKER_TARGETS.items():
        probe = targets.get(url, {})
        # Pill level comes from the hysteresis-smoothed state written by
        # _net_worker (single writer). None = escalation pending (pill
        # stays hidden) or the monitor has not run yet - the raw level
        # is only used before the worker's very first cycle.
        worker_ran = broker in _pill_states
        api_level = _pill_level(broker) if worker_ran else (
            _net_level([url], "broker") if live else None
        )
        brokers[broker] = {
            "url": url,
            "api": {
                "level": api_level,
                "last_ok": probe.get("last_ok"),
                "last_latency_ms": probe.get("last_latency_ms"),
                "consec_failures": probe.get("consec_failures"),
                "last_ok_at": probe.get("last_ok_at"),
            },
            "ws": _ws_health_for(broker),
            "session": _session_info(broker),
            "stats": broker_stats.snapshot(broker),
        }
    return {
        "internet": {
            "level": (
                _pill_level("internet") if "internet" in _pill_states
                else (_net_level(internet_urls, "internet") if live else None)
            ),
            "targets": {u: targets[u] for u in internet_urls if u in targets},
        },
        "brokers": brokers,
        "monitor_running": monitor_running,
        "last_check_at": last_check_at,
    }


def _net_stats():
    path = os.path.join(_day_dir(), "net.csv")
    if not os.path.exists(path):
        return {"samples": 0}
    df = pd.read_csv(path)
    out = {"samples": int(len(df)), "level_now": _net_level(list(_INTERNET_TARGETS) + list(_BROKER_TARGETS.values()), "internet")}
    per_target = {}
    for url, g in df.groupby("target"):
        oks = g["ok"].astype(bool)
        per_target[url] = {
            "samples": int(len(g)),
            "ok_pct": round(100.0 * float(oks.mean()), 1),
            "avg_latency_ms": round(float(g["latency_ms"].mean()), 1),
            "p95_latency_ms": round(float(g["latency_ms"].quantile(0.95)), 1),
            "failures": int((~oks).sum()),
        }
    out["targets"] = per_target
    return out


def _capture_root():
    return os.path.join(DATA_DIR, "feed_compare")


def _day_dir(day=None):
    d = (day or date.today()).isoformat()
    path = os.path.join(_capture_root(), d)
    os.makedirs(path, exist_ok=True)
    return path


def _parse_expiry(value):
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _mstock_jwt_fresh(jwt_token, min_seconds_left=300):
    try:
        payload_b64 = str(jwt_token).split(".")[1]
        payload_b64 += "=" * ((8 - len(payload_b64) % 8) % 8)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = int(payload.get("exp", 0))
        return exp - time.time() > min_seconds_left
    except Exception as e:
        logger.warning(f"Could not read M.Stock JWT expiry: {e}")
        return False


def _kite_symbol_sym(row):
    return str(row.get("tradingsymbol", ""))


def _mstock_expiry_date(row):
    return _parse_expiry(row.get("expiry"))


class FeedCaptureService:
    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._stop_event = threading.Event()
        self._threads = []
        self._counts = {"KITE": 0, "MSTOCK": 0}
        self._started_at = None
        self._underlying = None
        self._session = None
        self._last_error = None
        # Rate-limits the repeated "chain session resolution failed"
        # error while the supervisor retries every poll (e.g. a stale
        # Kite token would otherwise log every 30s for hours).
        self._resolve_fail_logged_at = None
        self._kite_ticker = None
        # Per-broker WS stats for the CAPTURE sockets (the app's own
        # feed socket is tracked by the adapters' samplers) - read by
        # _ws_health_for() while a capture is running.
        self._capture_ws = {
            broker: {"connected": False, "last_packet_ts": 0.0,
                     "window_start": 0.0, "packets": 0, "rate": 0.0}
            for broker in ("KITE", "MSTOCK")
        }

    def capture_ws_health(self, broker):
        """WS health of the capture socket for one broker (rate from the
        rolling 60s packet window)."""
        ws = self._capture_ws.get(broker) or {}
        last = ws.get("last_packet_ts")
        return {
            "connected": bool(ws.get("connected")),
            "seconds_since_tick": round(time.time() - last, 1) if last else None,
            "rate_per_min": round(ws.get("rate") or 0.0, 1),
            "max_gap_s": None,
        }

    def _capture_ws_packet(self, broker):
        """Called by both workers on every packet: refresh freshness and
        the rolling 60s rate window."""
        ws = self._capture_ws[broker]
        now = time.time()
        if not ws["window_start"]:
            ws["window_start"] = now
        ws["packets"] += 1
        elapsed = now - ws["window_start"]
        if elapsed >= 60:
            ws["rate"] = ws["packets"] / elapsed * 60.0
            ws["window_start"] = now
            ws["packets"] = 0
        ws["last_packet_ts"] = now

    # ------------------------------------------------------------------
    # session resolution (ATM locked per day)
    # ------------------------------------------------------------------

    def _session_file(self, day=None):
        return os.path.join(_day_dir(day), "session.json")

    def _load_locked_session(self):
        try:
            with open(self._session_file()) as f:
                session = json.load(f)
            if session.get("underlying"):
                return session
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return None

    def _ensure_kite_instrument_csv(self, exchange, day_dir):
        path = os.path.join(day_dir, f"kite_instruments_{exchange}.csv")
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            return path
        logger.info(f"Feed Lab: downloading Kite {exchange} instrument dump...")
        kite = KiteSingleton().get_kite()
        rows = kite.instruments(exchange)
        if not rows:
            raise RuntimeError(f"Kite returned no instruments for {exchange}")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        logger.info(f"Feed Lab: saved {len(rows)} Kite {exchange} instruments to {path}")
        return path

    def _ensure_mstock_master(self, underlying, day_dir):
        master = resolve_data_path("mstock_instrument_list.json")
        if self._mstock_master_covers(master, underlying):
            return master
        path = os.path.join(day_dir, f"mstock_master_{underlying}.json")
        if os.path.exists(path) and os.path.getsize(path) > 1000 and self._mstock_master_covers(path, underlying):
            return path
        logger.info(f"Feed Lab: downloading M.Stock instrument master for {underlying}...")
        jwt = self._mstock_jwt()
        resp = __import__("requests").get(
            f"https://api.mstock.trade/openapi/instruments/{underlying}",
            headers={
                "X-Mirae-Version": "1",
                "X-PrivateKey": fetch_from_json("access_token.json", "mstock_api_key") or _mstock_api_key_fallback(),
                "Authorization": f"Bearer {jwt}",
            },
            timeout=30,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"M.Stock instrument master returned HTTP {resp.status_code} (non-JSON)")
        rows = data if isinstance(data, list) else data.get("data", [])
        if not rows:
            raise RuntimeError("M.Stock instrument master download came back empty")
        with open(path, "w") as f:
            json.dump(rows, f)
        logger.info(f"Feed Lab: saved {len(rows)} M.Stock instruments to {path}")
        return path

    def _mstock_master_covers(self, path, underlying):
        try:
            with open(path) as f:
                first = json.load(f)
        except Exception:
            return False
        rows = first if isinstance(first, list) else first.get("data", [])
        prefix = self._mstock_symbol_prefix(underlying)
        for r in rows[:200000]:
            if str(r.get("instrumenttype", "")).upper() == "FUTIDX" and str(r.get("symbol", "")).upper().startswith(prefix):
                return True
        return False

    def _mstock_symbol_prefix(self, underlying):
        return str(underlying).upper()

    def _resolve_session(self, underlying):
        locked = self._load_locked_session()
        if locked and locked.get("underlying") == underlying:
            logger.info(f"Feed Lab: reusing today's locked session (ATM {locked.get('atm_strike')})")
            return locked

        cfg = UNDERLYING_CONFIG[str(underlying).upper()]
        exchange = cfg["exchange"]
        step = cfg["step"]
        day_dir = _day_dir()

        kite_csv = self._ensure_kite_instrument_csv(exchange, day_dir)
        with open(kite_csv, newline="") as f:
            kite_rows = list(csv.DictReader(f))

        today = date.today()
        fut_rows = [
            r for r in kite_rows
            if str(r.get("instrument_type", "")) == "FUT"
            and str(r.get("name", "")).upper() == str(underlying).upper()
            and _parse_expiry(r.get("expiry")) and _parse_expiry(r.get("expiry")) >= today
        ]
        if not fut_rows:
            raise RuntimeError(f"No Kite {exchange} futures found for {underlying}")
        fut = min(fut_rows, key=lambda r: _parse_expiry(r.get("expiry")))

        kite_singleton = KiteSingleton()
        if not kite_singleton._access_token:
            kite_singleton.set_access_token(_kite_token())
        kite = kite_singleton.get_kite()
        ltp_resp = kite.ltp([f"{exchange}:{_kite_symbol_sym(fut)}"])
        fut_ltp = float(list(ltp_resp.values())[0]["last_price"])
        atm_strike = int(round(fut_ltp / step) * step)

        opt_rows = [
            r for r in kite_rows
            if str(r.get("name", "")).upper() == str(underlying).upper()
            and str(r.get("instrument_type", "")) in ("CE", "PE")
            and int(float(r.get("strike", 0) or 0)) == atm_strike
            and _parse_expiry(r.get("expiry")) and _parse_expiry(r.get("expiry")) >= today
        ]
        kite_expiries = {_parse_expiry(r.get("expiry")) for r in opt_rows}

        mstock_master = self._ensure_mstock_master(underlying, day_dir)
        with open(mstock_master) as f:
            ms_rows = json.load(f)
        ms_rows = ms_rows if isinstance(ms_rows, list) else ms_rows.get("data", [])
        symbol_exact = str(underlying).upper()
        seg = cfg["mstock_seg"]

        ms_futs = [
            r for r in ms_rows
            if str(r.get("instrumenttype", "")).upper() == "FUTIDX"
            and str(r.get("symbol", "")).upper() == symbol_exact
            and str(r.get("exch_seg", "")).upper() == seg
            and _mstock_expiry_date(r) and _mstock_expiry_date(r) >= today
        ]
        ms_fut = min(ms_futs, key=_mstock_expiry_date) if ms_futs else None

        def ms_opts(opt_type):
            return [
                r for r in ms_rows
                if str(r.get("instrumenttype", "")).upper() == "OPTIDX"
                and str(r.get("symbol", "")).upper() == symbol_exact
                and str(r.get("exch_seg", "")).upper() == seg
                and str(r.get("name", "")).upper().endswith(opt_type)
                and int(float(r.get("strike", 0) or 0)) == atm_strike
                and _mstock_expiry_date(r) and _mstock_expiry_date(r) >= today
            ]

        ms_ce_all, ms_pe_all = ms_opts("CE"), ms_opts("PE")
        mstock_expiries = {_mstock_expiry_date(r) for r in ms_ce_all + ms_pe_all}
        common_expiries = sorted(kite_expiries & mstock_expiries)
        if not common_expiries:
            raise RuntimeError(
                f"Kite and M.Stock share no ATM {atm_strike} expiry for {underlying} "
                f"(Kite: {sorted(d.isoformat() for d in kite_expiries)[:3]}, "
                f"M.Stock: {sorted(d.isoformat() for d in mstock_expiries)[:3]})"
            )
        chosen_expiry = common_expiries[0]

        ce = next(r for r in opt_rows if r.get("instrument_type") == "CE" and _parse_expiry(r.get("expiry")) == chosen_expiry)
        pe = next(r for r in opt_rows if r.get("instrument_type") == "PE" and _parse_expiry(r.get("expiry")) == chosen_expiry)

        ms_ce = next((r for r in ms_ce_all if _mstock_expiry_date(r) == chosen_expiry), None)
        ms_pe = next((r for r in ms_pe_all if _mstock_expiry_date(r) == chosen_expiry), None)
        if not (ms_ce and ms_pe):
            raise RuntimeError(
                f"M.Stock master has no ATM {atm_strike} CE/PE for {underlying} "
                f"expiry {chosen_expiry}; capture needs both brokers' feeds"
            )

        session = {
            "date": today.isoformat(),
            "underlying": str(underlying).upper(),
            "atm_strike": atm_strike,
            "expiry": chosen_expiry.isoformat(),
            "fut_ltp_at_lock": fut_ltp,
            "locked_at": datetime.now().isoformat(),
            "KITE": {
                "FUT": {"token": str(fut["instrument_token"]), "symbol": _kite_symbol_sym(fut)},
                "CE": {"token": str(ce["instrument_token"]), "symbol": _kite_symbol_sym(ce)},
                "PE": {"token": str(pe["instrument_token"]), "symbol": _kite_symbol_sym(pe)},
            },
            "MSTOCK": {
                "FUT": {"token": str(ms_fut["token"]) if ms_fut else "", "symbol": str(ms_fut["name"]) if ms_fut else ""},
                "CE": {"token": str(ms_ce["token"]), "symbol": str(ms_ce["name"])},
                "PE": {"token": str(ms_pe["token"]), "symbol": str(ms_pe["name"])},
            },
        }
        with open(self._session_file(), "w") as f:
            json.dump(session, f, indent=2)
        logger.info(f"Feed Lab: session locked - ATM {atm_strike} {chosen_expiry} (fut LTP {fut_ltp})")
        return session

    # ------------------------------------------------------------------
    # CHAIN session resolution (ATM +/- N strikes, both CE & PE, + FUT)
    # ------------------------------------------------------------------

    def _chain_session_file(self, underlying, day=None):
        """Per-underlying lock file - the single session.json can only
        hold ONE underlying, and NIFTY + SENSEX chain captures run
        simultaneously."""
        return os.path.join(_day_dir(day), f"session_{str(underlying).upper()}.json")

    def _fut_ltp(self, underlying, day_dir, kite_fut_sym=None, ms_fut=None):
        """Near-month future's last traded price, for anchoring the
        ATM. Primary source: Kite REST quote. Fallback (no Kite token):
        M.Stock's quote API, but ONLY when a fresh JWT already exists -
        the supervisor must never trigger an OTP dialog."""
        cfg = UNDERLYING_CONFIG[str(underlying).upper()]
        exchange = cfg["exchange"]
        try:
            kite = KiteSingleton().get_kite()
            ltp_resp = kite.ltp([f"{exchange}:{kite_fut_sym}"])
            return float(list(ltp_resp.values())[0]["last_price"]), "KITE"
        except Exception as kite_error:
            logger.debug(f"Feed Lab: Kite fut LTP unavailable ({kite_error})")
        if ms_fut and _mstock_jwt_fresh(fetch_from_json("access_token.json", "mstock_jwt_token") or ""):
            try:
                resp = requests.post(
                    "https://api.mstock.trade/openapi/typeb/instruments/quote",
                    headers={
                        "X-Mirae-Version": "1",
                        "X-PrivateKey": fetch_from_json("access_token.json", "mstock_api_key") or _mstock_api_key_fallback(),
                        "Authorization": f"Bearer {self._mstock_jwt()}",
                    },
                    json={"mode": "OHLC", "exchangeTokens": {
                        cfg["mstock_seg"]: [str(ms_fut["token"])]}},
                    timeout=15,
                )
                resp.raise_for_status()
                fetched = resp.json().get("data", {}).get("fetched", [])
                for q in fetched:
                    if q.get("ltp") is not None:
                        return float(q["ltp"]), "MSTOCK"
            except Exception as ms_error:
                logger.debug(f"Feed Lab: M.Stock fut LTP unavailable ({ms_error})")
        return None, None

    def _resolve_chain_session(self, underlying):
        """Resolve/refresh the chain session for one underlying: FUT +
        every CE and PE at ATM +/- CHAIN_STRIKES, both brokers, common
        weekly expiry. Strikes already captured today are UNIONED in so
        an ATM drift never orphans a contract that was held earlier in
        the session. Writes session_<UNDERLYING>.json."""
        underlying = str(underlying).upper()
        cfg = UNDERLYING_CONFIG[underlying]
        exchange = cfg["exchange"]
        step = cfg["step"]
        day_dir = _day_dir()
        today = date.today()

        kite_csv = self._ensure_kite_instrument_csv(exchange, day_dir)
        with open(kite_csv, newline="") as f:
            kite_rows = list(csv.DictReader(f))

        fut_rows = [
            r for r in kite_rows
            if str(r.get("instrument_type", "")) == "FUT"
            and str(r.get("name", "")).upper() == underlying
            and _parse_expiry(r.get("expiry")) and _parse_expiry(r.get("expiry")) >= today
        ]
        if not fut_rows:
            raise RuntimeError(f"No Kite {exchange} futures found for {underlying}")
        fut = min(fut_rows, key=lambda r: _parse_expiry(r.get("expiry")))

        # Previously locked strikes (same day) are unioned in - a held
        # contract must not fall out of scope because the ATM moved.
        old_strikes = set()
        session_path = self._chain_session_file(underlying)
        try:
            with open(session_path) as f:
                prev = json.load(f)
            if prev.get("date") == today.isoformat() and prev.get("strikes"):
                old_strikes = {int(s) for s in prev["strikes"]}
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            pass

        fut_ltp, anchor = self._fut_ltp(
            underlying, day_dir,
            kite_fut_sym=_kite_symbol_sym(fut),
            ms_fut=None,
        )
        if fut_ltp is None:
            raise RuntimeError("no fut LTP from either broker (Kite token missing/stale?)")
        atm_strike = int(round(fut_ltp / step) * step)
        new_strikes = {atm_strike + i * step for i in range(-CHAIN_STRIKES, CHAIN_STRIKES + 1)}
        strikes = sorted(old_strikes | new_strikes)

        # Expiry: the first weekly expiry both brokers share at the ATM.
        kite_atm = [
            r for r in kite_rows
            if str(r.get("name", "")).upper() == underlying
            and str(r.get("instrument_type", "")) in ("CE", "PE")
            and int(float(r.get("strike", 0) or 0)) == atm_strike
            and _parse_expiry(r.get("expiry")) and _parse_expiry(r.get("expiry")) >= today
        ]
        kite_expiries = {_parse_expiry(r.get("expiry")) for r in kite_atm}

        mstock_master = self._ensure_mstock_master(underlying, day_dir)
        with open(mstock_master) as f:
            ms_rows = json.load(f)
        ms_rows = ms_rows if isinstance(ms_rows, list) else ms_rows.get("data", [])
        seg = cfg["mstock_seg"]

        def ms_ce_rows(strike_s):
            """MStock OPTIDX rows for one strike (CE side, for expiry
            matching) - master stores strike as number or numeric
            string, so normalise both sides to float."""
            out = []
            for r in ms_rows:
                if str(r.get("instrumenttype", "")).upper() != "OPTIDX":
                    continue
                if str(r.get("symbol", "")).upper() != underlying:
                    continue
                if str(r.get("exch_seg", "")).upper() != seg:
                    continue
                try:
                    if abs(float(r.get("strike", 0) or 0) - float(strike_s)) > 1e-6:
                        continue
                except (TypeError, ValueError):
                    continue
                if not (_mstock_expiry_date(r) and _mstock_expiry_date(r) >= today):
                    continue
                out.append(r)
            return out

        ms_ce_atm = [r for r in ms_ce_rows(atm_strike)
                     if str(r.get("name", "")).upper().endswith("CE")]
        mstock_expiries = {_mstock_expiry_date(r) for r in ms_ce_atm}
        common_expiries = sorted(kite_expiries & mstock_expiries)
        if not common_expiries:
            raise RuntimeError(
                f"Kite and M.Stock share no ATM {atm_strike} expiry for {underlying}"
            )
        chosen_expiry = common_expiries[0]

        session = {
            "date": today.isoformat(),
            "underlying": underlying,
            "tag": f"{_CHAIN_TAG_PREFIX}_{underlying}",
            "atm_strike": atm_strike,
            "strikes": [str(s) for s in strikes],
            "expiry": chosen_expiry.isoformat(),
            "fut_symbol": _kite_symbol_sym(fut),
            "fut_ltp_at_lock": fut_ltp,
            "atm_anchor": anchor,
            "locked_at": datetime.now().isoformat(),
            "KITE": {},
            "MSTOCK": {},
        }

        # Kite side: FUT + every CE/PE at each strike for the chosen expiry.
        kite_strikes = {float(s) for s in strikes}
        for r in kite_rows:
            if str(r.get("name", "")).upper() != underlying:
                continue
            typ = str(r.get("instrument_type", ""))
            if typ not in ("CE", "PE", "FUT"):
                continue
            try:
                if typ == "FUT":
                    if r is not fut:
                        continue
                else:
                    if float(r.get("strike", 0) or 0) not in kite_strikes:
                        continue
                    if _parse_expiry(r.get("expiry")) != chosen_expiry:
                        continue
            except (TypeError, ValueError):
                continue
            tok = str(r["instrument_token"])
            sym = _kite_symbol_sym(r)
            session["KITE"][tok] = {"symbol": sym, "token": tok}

        # MStock side: same slice from its own master (name = exchange
        # format symbol; token = MStock token).
        ms_futs = [
            r for r in ms_rows
            if str(r.get("instrumenttype", "")).upper() == "FUTIDX"
            and str(r.get("symbol", "")).upper() == underlying
            and str(r.get("exch_seg", "")).upper() == seg
            and _mstock_expiry_date(r) and _mstock_expiry_date(r) >= today
        ]
        ms_fut = min(ms_futs, key=_mstock_expiry_date) if ms_futs else None
        if ms_fut:
            session["MSTOCK"][str(ms_fut["token"])] = {
                "symbol": str(ms_fut["name"]), "token": str(ms_fut["token"])}
        ms_strikes = {float(s) for s in strikes}
        for r in ms_rows:
            if str(r.get("instrumenttype", "")).upper() != "OPTIDX":
                continue
            if str(r.get("symbol", "")).upper() != underlying:
                continue
            if str(r.get("exch_seg", "")).upper() != seg:
                continue
            try:
                if float(r.get("strike", 0) or 0) not in ms_strikes:
                    continue
            except (TypeError, ValueError):
                continue
            if _mstock_expiry_date(r) != chosen_expiry:
                continue
            tok = str(r.get("token", ""))
            sym = str(r.get("name", ""))
            if tok and sym:
                session["MSTOCK"][tok] = {"symbol": sym, "token": tok}

        if not session["KITE"] and not session["MSTOCK"]:
            raise RuntimeError(f"chain session for {underlying} resolved to zero instruments")

        with open(session_path, "w") as f:
            json.dump(session, f, indent=2)
        logger.info(
            f"Feed Lab: chain session locked - {underlying} ATM {atm_strike} "
            f"{chosen_expiry} ({len(strikes)} strikes x CE+PE + FUT; "
            f"kite={len(session['KITE'])}, mstock={len(session['MSTOCK'])}; "
            f"anchor {anchor} fut LTP {fut_ltp})"
        )
        return session

    def start_chain(self, underlying):
        """Start the market-hours chain capture for one underlying
        (FUT + ATM +/- CHAIN_STRIKES CE/PE on BOTH brokers). Safe to
        call repeatedly - the supervisor does. Requires a valid Kite
        token today (ATM anchoring) and a fresh M.Stock JWT for the
        MStock socket; a missing MStock JWT degrades to Kite-only with
        a note, never an OTP prompt from the supervisor."""
        with self._lock:
            if self._running:
                return True, None
            underlying = str(underlying or "").upper()
            if underlying not in UNDERLYING_CONFIG:
                return False, f"Unsupported underlying: {underlying}"
            try:
                session = self._resolve_chain_session(underlying)
                self._resolve_fail_logged_at = None
            except Exception as e:
                # The supervisor retries every _CHAIN_POLL_S, so the
                # same failure (stale Kite token) would log 2+ lines
                # per poll for hours. Log the first occurrence, then
                # repeat at most every 5 minutes.
                now = time.monotonic()
                if (self._resolve_fail_logged_at is None
                        or now - self._resolve_fail_logged_at >= 300):
                    logger.error(f"Feed Lab: chain session resolution failed for {underlying}: {e}")
                    self._resolve_fail_logged_at = now
                return False, str(e)

            self._stop_event.clear()
            self._counts = {"KITE": 0, "MSTOCK": 0}
            self._started_at = datetime.now().isoformat()
            self._underlying = underlying
            self._session = session
            self._last_error = None

            threads = []
            if session["KITE"]:
                threads.append(threading.Thread(
                    target=self._kite_worker, args=(session,),
                    daemon=True, name=f"feedlab-kite-chain-{underlying}"))
            if session["MSTOCK"]:
                threads.append(threading.Thread(
                    target=self._mstock_worker, args=(session,),
                    daemon=True, name=f"feedlab-mstock-chain-{underlying}"))
            for i, t in enumerate(threads):
                t.start()
                if i == 0:
                    time.sleep(0.5)
            self._threads = threads
            self._running = True
            logger.info(
                f"Feed Lab: CHAIN capture STARTED for {underlying} "
                f"(ATM {session['atm_strike']} {session['expiry']}; "
                f"kite={len(session['KITE'])}, mstock={len(session['MSTOCK'])})"
            )
            return True, None

    # ------------------------------------------------------------------
    # MStock auth
    # ------------------------------------------------------------------

    def _mstock_jwt(self):
        try:
            jwt = fetch_from_json("access_token.json", "mstock_jwt_token")
            if jwt and _mstock_jwt_fresh(jwt):
                return jwt
        except Exception as e:
            logger.warning(f"Feed Lab: stored M.Stock JWT unusable ({e})")
        logger.info("Feed Lab: M.Stock login required (OTP dialog will appear)...")
        ms = MStockSingleton()
        refresh_token = ms.login()
        ms.get_session_token(refresh_token)
        return ms._access_token

    # ------------------------------------------------------------------
    # capture workers
    # ------------------------------------------------------------------

    def _flush_buffer(self, broker, buffer, path):
        if not buffer:
            return
        try:
            new_file = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                if new_file:
                    writer.writerow(CSV_HEADER)
                writer.writerows(buffer)
            buffer.clear()
        except Exception as e:
            logger.error(f"Feed Lab: {broker} CSV flush failed: {e}")

    def _kite_worker(self, session):
        broker = "KITE"
        path = os.path.join(
            _day_dir(date.fromisoformat(session["date"])),
            f"{broker.lower()}_{session.get('tag') or session['underlying']}.csv")
        buffer = []
        state = {"last_flush": time.time()}
        tokens = [int(v["token"]) for v in session[broker].values() if v.get("token")]
        symbols = {str(v["token"]): v["symbol"] for v in session[broker].values()}

        def on_ticks(ws, ticks):
            now_ns = time.time_ns()
            now_iso = datetime.now().isoformat()
            for t in ticks:
                token = str(t.get("instrument_token", ""))
                depth = t.get("depth") or {}
                bids = depth.get("buy") or []
                asks = depth.get("sell") or []
                buffer.append([
                    now_ns, now_iso, token, symbols.get(token, ""),
                    t.get("last_price", ""),
                    bids[0].get("price", "") if bids else "",
                    asks[0].get("price", "") if asks else "",
                    t.get("volume", ""), t.get("oi", ""),
                ])
                self._counts[broker] += 1
            self._capture_ws_packet(broker)
            if len(buffer) >= 500 or time.time() - state["last_flush"] >= 1.0:
                self._flush_buffer(broker, buffer, path)
                state["last_flush"] = time.time()

        def on_connect(ws, response):
            logger.info(f"Feed Lab: Kite websocket connected ({len(tokens)} instruments)")
            self._capture_ws[broker]["connected"] = True
            try:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_FULL, tokens)
            except Exception as e:
                logger.error(f"Feed Lab: Kite subscribe failed: {e}")

        def on_close(ws, code, reason):
            logger.warning(f"Feed Lab: Kite websocket closed ({code}, {reason})")
            self._capture_ws[broker]["connected"] = False

        ticker = KiteTicker(KITE_API_KEY, _kite_token())
        ticker.on_ticks = on_ticks
        ticker.on_connect = on_connect
        ticker.on_close = on_close
        self._kite_ticker = ticker
        ticker.connect(threaded=True)

        self._stop_event.wait()
        try:
            ticker.close()
        except Exception:
            pass
        self._flush_buffer(broker, buffer, path)

    def _mstock_worker(self, session):
        broker = "MSTOCK"
        day_dir = _day_dir(date.fromisoformat(session["date"]))
        path = os.path.join(
            day_dir,
            f"{broker.lower()}_{session.get('tag') or session['underlying']}.csv")
        buffer = []
        state = {"last_flush": time.time()}
        tokens_by_extype = {}
        for v in session[broker].values():
            if v.get("token"):
                extype = MSTOCK_EXCHANGE_TYPE.get(_underlying_exchange(session["underlying"]), 2)
                tokens_by_extype.setdefault(extype, []).append(str(v["token"]))
        symbols = {str(v["token"]): v["symbol"] for v in session[broker].values()}

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._mstock_ws_loop(session, tokens_by_extype, symbols, buffer, path, state, broker))
        finally:
            loop.close()
            self._flush_buffer(broker, buffer, path)

    async def _mstock_ws_loop(self, session, tokens_by_extype, symbols, buffer, path, state, broker):
        ms = MStockSingleton()
        jwt = self._mstock_jwt()
        ms.set_access_token(jwt)
        cached_ips = []

        while not self._stop_event.is_set():
            ws = None
            try:
                url = ms._ws_url()
                try:
                    ws = await websockets.connect(url)
                except (pysocket.gaierror, OSError):
                    if not cached_ips:
                        infos = await asyncio.get_running_loop().getaddrinfo(
                            WS_HOST, WS_PORT, family=pysocket.AF_INET, proto=pysocket.IPPROTO_TCP)
                        cached_ips = sorted({i[4][0] for i in infos})
                    if cached_ips:
                        url = ms._ws_url(cached_ips[0])
                        ws = await websockets.connect(url, server_hostname=WS_HOST)
                self._socket = ws
                self._capture_ws[broker]["connected"] = True
                logger.info("Feed Lab: M.Stock websocket connected")
                await ws.send(f"LOGIN:{jwt}")
                await asyncio.sleep(1)
                token_list = [
                    {"exchangeType": int(extype), "tokens": toks}
                    for extype, toks in tokens_by_extype.items() if toks
                ]
                await ws.send(json.dumps({
                    "correlationID": "feedlab",
                    "action": 1,
                    "params": {"mode": 3, "tokenList": token_list},
                }))
                logger.info(f"Feed Lab: M.Stock subscribed: {token_list}")

                async for message in ws:
                    if self._stop_event.is_set():
                        break
                    if not isinstance(message, (bytes, bytearray)):
                        continue
                    pkt = parse_quote_message(message)
                    if not pkt:
                        continue
                    token = str(pkt.get("token", ""))
                    depth = pkt.get("market_depth") or {}
                    bids = depth.get("bids") or []
                    asks = depth.get("asks") or []
                    buffer.append([
                        time.time_ns(), datetime.now().isoformat(), token, symbols.get(token, ""),
                        pkt.get("ltp", ""),
                        bids[0].get("price", "") if bids else "",
                        asks[0].get("price", "") if asks else "",
                        pkt.get("volume_traded", ""), pkt.get("open_interest", ""),
                    ])
                    self._counts[broker] += 1
                    self._capture_ws_packet(broker)
                    if len(buffer) >= 500 or time.time() - state["last_flush"] >= 1.0:
                        self._flush_buffer(broker, buffer, path)
                        state["last_flush"] = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.warning(f"Feed Lab: M.Stock websocket error ({e}); reconnecting in 5s...")
            finally:
                self._capture_ws[broker]["connected"] = False
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
            if not self._stop_event.is_set():
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def start(self, underlying="SENSEX"):
        with self._lock:
            if self._running:
                return False, "Capture is already running"
            # Broker-independent since the position-tick capture proved
            # both sockets coexist with any login: the Kite worker needs
            # a valid Kite token for the day (its own websocket), and the
            # MStock worker opens a SEPARATE websocket that never touches
            # the trading feed - the old "must run on broker KITE" gate
            # blocked MStock users for no technical reason.
            if not _kite_token():
                return False, (
                    "Feed Lab needs a Kite login for today (no valid Kite "
                    "token) - generate the Kite session, then retry."
                )
            underlying = str(underlying or "SENSEX").upper()
            if underlying not in UNDERLYING_CONFIG:
                return False, f"Unsupported underlying: {underlying}"
            try:
                session = self._resolve_session(underlying)
            except Exception as e:
                logger.error(f"Feed Lab: session resolution failed: {e}")
                return False, str(e)

            # One capture at a time: the manual capture supersedes a
            # running position capture (same instruments, ATM-scoped) -
            # the supervisor restarts the position capture after this
            # stops.
            if _position_capture_service._running:
                _position_capture_service.stop()

            self._stop_event.clear()
            self._counts = {"KITE": 0, "MSTOCK": 0}
            self._started_at = datetime.now().isoformat()
            self._underlying = underlying
            self._session = session
            self._last_error = None

            kite_t = threading.Thread(target=self._kite_worker, args=(session,), daemon=True, name="feedlab-kite")
            ms_t = threading.Thread(target=self._mstock_worker, args=(session,), daemon=True, name="feedlab-mstock")
            kite_t.start()
            time.sleep(0.5)
            ms_t.start()
            self._threads = [kite_t, ms_t]
            self._running = True
            logger.info(f"Feed Lab: capture STARTED for {underlying} (ATM {session['atm_strike']} {session['expiry']})")
            return True, None

    def stop(self):
        with self._lock:
            if not self._running:
                return False, "Capture is not running"
            self._running = False
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=8)
        self._threads = []
        self._kite_ticker = None
        logger.info("Feed Lab: capture STOPPED")
        return True, None

    def start_position_capture(self, underlying, symbols):
        """Capture BOTH brokers' ticks for explicit symbols (the held
        positions) - no ATM resolution, no active-broker gate.

        Unlike start() this works under any broker login: the Kite
        worker needs only a valid Kite access token for the day (the
        report marks KITE 'no session' when it is absent); the MStock
        worker needs a fresh JWT - both are independent sockets that
        never touch the trading feed. Ticks stream into the day dir as
        kite_position.csv / mstock_position.csv (same CSV_HEADER as the
        manual Feed Lab capture), so the EOD report counts per-position
        ticks by time-window without any live bookkeeping.

        Returns (ok, error_or_skip_note)."""
        with self._lock:
            if self._running:
                return True, None  # already capturing - caller re-checks the symbol set
            underlying = str(underlying or "NIFTY").upper()
            try:
                session = {
                    "date": date.today().isoformat(),
                    "underlying": underlying,
                    "tag": "position",
                    "KITE": {},
                    "MSTOCK": {},
                }
                skip_note = None
                exchange = UNDERLYING_CONFIG[underlying]["exchange"]
                trader = _position_trader_ref

                # MStock tokens: the trader's weekly-contract window maps
                # name -> MStock token, but ONLY under an MStock login -
                # under Kite the same window carries KITE tokens (it is
                # built from the ACTIVE broker's instrument meta). So:
                # use it under MStock, and resolve anything left over
                # from the MStock instrument master (name = exchange-
                # format symbol, token = MStock token) - that file is
                # broker-independent.
                active_broker_name = type(
                    getattr(trader, "_broker", None)
                ).__name__.lower()
                contracts = getattr(
                    trader, "_five_weekly_option_contracts", {}) or {}
                unresolved = set(symbols)
                if "mstock" in active_broker_name:
                    for sym in list(unresolved):
                        tok = None
                        for tok_, c in contracts.items():
                            if str(c.get("name", "")).upper() == sym:
                                tok = str(tok_)
                                break
                        if tok:
                            session["MSTOCK"][tok] = {"symbol": sym, "token": tok}
                            unresolved.discard(sym)
                if unresolved:
                    try:
                        master_path = resolve_data_path(
                            "mstock_instrument_list_reduced.json")
                        with open(master_path) as f:
                            master = json.load(f)
                        ms_rows = (master.get("instruments")
                                   if isinstance(master, dict) else master)
                        for sym in list(unresolved):
                            for row in ms_rows or []:
                                if str(row.get("name", "")).upper() == sym:
                                    tok = str(row.get("token", ""))
                                    if tok:
                                        session["MSTOCK"][tok] = {
                                            "symbol": sym, "token": tok}
                                    break
                            unresolved.discard(sym)
                    except Exception as master_error:
                        logger.warning(
                            f"Feed Lab: MStock master token resolution "
                            f"failed: {master_error}")

                # Kite tokens: resolved from the instrument dump by
                # tradingsymbol. Needs a valid Kite token today.
                try:
                    if not _kite_token():
                        raise RuntimeError("no Kite access token for today")
                    kite_csv = self._ensure_kite_instrument_csv(exchange, _day_dir())
                    with open(kite_csv, newline="") as f:
                        for row in csv.DictReader(f):
                            ksym = str(row.get("tradingsymbol", "")).upper()
                            if ksym in symbols:
                                ktok = str(row.get("instrument_token", ""))
                                if ktok:
                                    session["KITE"][ktok] = {"symbol": ksym, "token": ktok}
                except Exception as kite_error:
                    skip_note = f"KITE ticks not captured: {kite_error}"
                    logger.warning(f"Feed Lab: {skip_note}")

                if not session["MSTOCK"] and not session["KITE"]:
                    return False, "no held symbol could be resolved on either broker"

                self._stop_event.clear()
                self._counts = {"KITE": 0, "MSTOCK": 0}
                self._started_at = datetime.now().isoformat()
                self._underlying = underlying
                self._session = session
                self._last_error = None

                threads = []
                if session["KITE"]:
                    threads.append(threading.Thread(
                        target=self._kite_worker, args=(session,),
                        daemon=True, name="feedlab-kite-position"))
                if session["MSTOCK"]:
                    threads.append(threading.Thread(
                        target=self._mstock_worker, args=(session,),
                        daemon=True, name="feedlab-mstock-position"))
                for i, t in enumerate(threads):
                    t.start()
                    if i == 0:
                        time.sleep(0.5)
                self._threads = threads
                self._running = True
                logger.info(
                    f"Feed Lab: POSITION capture STARTED for {underlying} "
                    f"({len(symbols)} symbol(s); "
                    f"kite={len(session['KITE'])}, mstock={len(session['MSTOCK'])})"
                )
                return True, skip_note
            except Exception as e:
                self._last_error = str(e)
                logger.error(f"Feed Lab: position capture start failed: {e}")
                return False, str(e)

    def running_symbols(self):
        """Set of symbols this capture is currently streaming (empty when
        stopped) - lets the supervisor detect a held-symbol change."""
        session = self._session
        if not self._running or not session:
            return set()
        syms = set()
        for broker in ("KITE", "MSTOCK"):
            for v in (session.get(broker) or {}).values():
                if v.get("symbol"):
                    syms.add(str(v["symbol"]).upper())
        return syms

    def status(self):
        with self._lock:
            running = self._running
            counts = dict(self._counts)
            started_at = self._started_at
            session = json.loads(json.dumps(self._session)) if self._session else None
            last_error = self._last_error
        broker = str(fetch_from_json("appconfig.json", "BROKER") or "").upper()
        try:
            chain = chain_status()
        except Exception as chain_error:
            logger.warning(f"Feed Lab: chain status failed: {chain_error}")
            chain = {}
        return {
            "enabled": feed_lab_enabled(),
            "running": running,
            "broker": broker,
            "underlying": self._underlying,
            "started_at": started_at,
            "counts": counts,
            "net": net_status(),
            "chain": chain,
            "session": {
                "atm_strike": session.get("atm_strike"),
                "expiry": session.get("expiry"),
                "locked_at": session.get("locked_at"),
                "instruments": {
                    kite_broker: {k: v["symbol"] for k, v in (session.get(kite_broker) or {}).items()}
                    for kite_broker in ("KITE", "MSTOCK")
                } if session else None,
            } if session else None,
            "last_error": last_error,
            "locked_session_today": bool(self._load_locked_session()),
        }

    def report(self):
        day_dir = _day_dir()
        # Merge EVERY tick CSV captured today (manual, chain, position)
        # per broker - the analysis is per-symbol, so files from
        # different sessions simply add coverage.
        def _tick_csvs(prefix, exclude):
            return [
                os.path.join(day_dir, f)
                for f in sorted(os.listdir(day_dir))
                if f.startswith(prefix) and f.endswith(".csv")
                and not f.startswith(exclude)
            ]

        kite_csvs = _tick_csvs("kite_", "kite_instruments")
        mstock_csvs = _tick_csvs("mstock_", "mstock_master")
        if not kite_csvs or not mstock_csvs:
            return {"ok": False, "error": "No captured data for today yet - press Start and let it run during market hours."}

        session = self._load_locked_session() or {}
        underlying = session.get("underlying")
        window = _market_window(underlying)
        window_label = f"{window[0][0]:02d}:{window[0][1]:02d}-{window[1][0]:02d}:{window[1][1]:02d}"

        # App restart tally for the day (per-launch log files), so the
        # report can state how often the capture itself was interrupted.
        day = date.fromisoformat(day_dir.split(os.sep)[-1])
        restarts = _count_app_restarts(day, window)

        # Internet-down windows + broker REST attribution, from the net
        # probes captured while the app ran (market hours only).
        net_path = os.path.join(day_dir, "net.csv")
        net_df = None
        internet_windows = []
        rest_anomalies = {}
        if os.path.exists(net_path):
            try:
                net_df = _filter_market_hours(pd.read_csv(net_path), "ts_ns", window)
                internet_windows = _internet_down_windows(net_df)
                rest_anomalies = _rest_anomalies(net_df, internet_windows)
            except Exception as e:
                logger.warning(f"Feed Lab: net.csv analysis failed: {e}")

        def _down_minutes(windows):
            return round(sum((e - s) / 60e9 for s, e in windows), 1)

        internet_down_min = _down_minutes(internet_windows)

        per_broker = {}
        # Capture-run spans per broker, crossed over so a hole on one
        # broker can be checked against "did the other keep flowing".
        kite_df = _filter_market_hours(pd.concat(
            [pd.read_csv(p) for p in kite_csvs], ignore_index=True), "recv_ns", window)
        mstock_df = _filter_market_hours(pd.concat(
            [pd.read_csv(p) for p in mstock_csvs], ignore_index=True), "recv_ns", window)
        runs_by_broker = {
            "KITE": _ns_runs(kite_df["recv_ns"]) if not kite_df.empty else [],
            "MSTOCK": _ns_runs(mstock_df["recv_ns"]) if not mstock_df.empty else [],
        }
        per_broker["KITE"] = _broker_stats(
            kite_df, internet_windows=internet_windows,
            other_runs=runs_by_broker["MSTOCK"],
        )
        per_broker["MSTOCK"] = _broker_stats(
            mstock_df, internet_windows=internet_windows,
            other_runs=runs_by_broker["KITE"],
        )

        if per_broker["KITE"]["ticks"] == 0 and per_broker["MSTOCK"]["ticks"] == 0:
            return {
                "ok": True,
                "generated_at": datetime.now().isoformat(),
                "day": day_dir.split(os.sep)[-1],
                "underlying": underlying,
                "atm_strike": session.get("atm_strike"),
                "expiry": session.get("expiry"),
                "chain": chain_status(),
                "market_window": window_label,
                "app_restarts": restarts,
                "internet_down_min": internet_down_min,
                "rest_anomalies": rest_anomalies,
                "per_broker": per_broker,
                "agreement": {}, "leader": {}, "network": _net_stats(),
                "verdict": (
                    f"No captured ticks inside the {window_label} market window - "
                    f"capture ran outside market hours only."
                ),
            }

        agreement, leader = {}, {"KITE_first": 0, "MSTOCK_first": 0, "tie": 0, "median_lead_ms": None}
        try:
            agreement = _agreement_stats(kite_df, mstock_df)
            leader = _leader_stats(kite_df, mstock_df)
        except Exception as e:
            logger.warning(f"Feed Lab: cross-broker stats failed: {e}")

        kite_health = per_broker.get("KITE", {})
        ms_health = per_broker.get("MSTOCK", {})
        kite_rate = kite_health.get("ticks_per_min", 0) or 0
        ms_rate = ms_health.get("ticks_per_min", 0) or 0
        ratio = (ms_rate / kite_rate) if kite_rate else 0
        ms_max_gap = ms_health.get("max_gap_s", 999)
        ms_stalls = ms_health.get("stalls", 999)
        median_delta = agreement.get("max_instrument_median_abs_delta", 999)
        ready = (
            kite_rate > 0 and ratio >= 0.8
            and ms_max_gap <= 10 and ms_stalls == 0
            and median_delta <= 0.25
        )

        # Context suffix: what the analysis already excused, so a clean
        # verdict is trusted and a dirty one is explainable. Only
        # app-closure holes are called out here; internet downtime is
        # shown separately.
        interruptions = max(
            kite_health.get("app_closure_gaps", 0) or 0,
            ms_health.get("app_closure_gaps", 0) or 0,
        )
        context = (
            f" ({interruptions} app-closure gap(s) excluded, "
            f"internet down {internet_down_min} min"
        )
        kite_rest = rest_anomalies.get("KITE", 0) if rest_anomalies else 0
        mstock_rest = rest_anomalies.get("MSTOCK", 0) if rest_anomalies else 0
        if kite_rest or mstock_rest:
            context += f", REST anomalies K:{kite_rest}/M:{mstock_rest}"
        context += ")"

        if kite_rate == 0:
            verdict = "No KITE ticks captured in the market window - was the capture running during market hours?"
        elif ready:
            verdict = (
                f"M.Stock READY: {ratio:.0%} of Kite's tick rate, {ms_stalls} stalls, "
                f"median price gap {median_delta:.2f} pts."
            )
        else:
            verdict = (
                f"M.Stock NOT ready: {ratio:.0%} of Kite's tick rate, "
                f"max silence {ms_max_gap:.0f}s, {ms_stalls} stalls, "
                f"median price gap {median_delta:.2f} pts."
            )
        verdict += context

        return {
            "ok": True,
            "generated_at": datetime.now().isoformat(),
            "day": day_dir.split(os.sep)[-1],
            "underlying": underlying,
            "atm_strike": session.get("atm_strike"),
            "expiry": session.get("expiry"),
            "chain": chain_status(),
            "market_window": window_label,
            "app_restarts": restarts,
            "internet_down_min": internet_down_min,
            "internet_down_windows": len(internet_windows),
            "rest_anomalies": rest_anomalies,
            "per_broker": per_broker,
            "agreement": agreement,
            "leader": leader,
            "network": _net_stats(),
            "verdict": verdict,
        }


def _underlying_exchange(underlying):
    return UNDERLYING_CONFIG.get(str(underlying).upper(), {}).get("mstock_seg", "NFO")


def _kite_token():
    token = fetch_from_json("access_token.json", "kite_access_token")
    if not token:
        raise RuntimeError("No Kite access token - start the app and complete the Kite login first")
    return token


def _mstock_api_key_fallback():
    try:
        from core.mstock_connector import MSTOCK_API_KEY
        return MSTOCK_API_KEY
    except Exception:
        return ""


def _market_window(underlying):
    """Report analysis window (start, end) as time-of-day tuples in
    exchange-local (Asia/Kolkata) wall clock. Defaults to the
    NIFTY/SENSEX window for unknown underlyings."""
    return MARKET_WINDOWS.get(
        str(underlying or "").strip().upper(), MARKET_WINDOWS["NIFTY"]
    )


def _ist_parts(ns_series):
    """Asia/Kolkata wall-clock datetime for a Series of epoch ns."""
    return pd.to_datetime(ns_series, unit="ns", utc=True).dt.tz_convert("Asia/Kolkata")


def _filter_market_hours(df, ns_col, window):
    """Keep only rows whose Asia/Kolkata time of day falls inside the
    (start, end) market window (inclusive)."""
    if df is None or df.empty:
        return df
    ist = _ist_parts(df[ns_col])
    start = (window[0][0] * 60 + window[0][1]) * 60
    end = (window[1][0] * 60 + window[1][1]) * 60
    secs = ist.dt.hour * 3600 + ist.dt.minute * 60 + ist.dt.second
    return df[(secs >= start) & (secs <= end)]


def _ns_runs(ns_series, boundary_s=CAPTURE_RUN_BOUNDARY_S):
    """Split a sorted Series of epoch ns into contiguous runs. A gap
    larger than `boundary_s` starts a new run. Returns [(start, end)]
    epoch-ns tuples (empty list for no data)."""
    ns = sorted(ns_series.dropna().astype("int64").tolist())
    if not ns:
        return []
    runs = []
    start = prev = ns[0]
    for v in ns[1:]:
        if (v - prev) / 1e9 > boundary_s:
            runs.append((start, prev))
            start = v
        prev = v
    runs.append((start, prev))
    return runs


def _overlaps(start_ns, end_ns, windows):
    """True when [start_ns, end_ns] overlaps any (s, e) window."""
    for s, e in windows or []:
        if start_ns <= e and s <= end_ns:
            return True
    return False


def _internet_down_windows(net_df):
    """
    Epoch-ns spans when the INTERNET itself was down, derived from the
    control-target probes (google/1.1.1.1 - deliberately not broker
    endpoints). A failed control probe opens a window that stays open
    until the next successful control probe.
    """
    if net_df is None or net_df.empty:
        return []
    ctrl = net_df[net_df["target"].isin(_INTERNET_TARGETS)].sort_values("ts_ns")
    if ctrl.empty:
        return []
    windows = []
    open_at = None
    for ts, ok in zip(ctrl["ts_ns"].astype("int64"), ctrl["ok"].astype(bool)):
        if not ok:
            if open_at is None:
                open_at = int(ts)
        else:
            if open_at is not None:
                windows.append((open_at, int(ts)))
                open_at = None
    if open_at is not None:
        windows.append((open_at, int(ctrl["ts_ns"].astype("int64").iloc[-1])))
    return windows


def _rest_anomalies(net_df, internet_windows):
    """
    Attribute REST probe failures. A broker-endpoint failure counts as
    a BROKER API anomaly only when the internet was demonstrably up
    (no control failure within +/-90s and not inside an internet-down
    window); otherwise it is internet-explained.
    """
    out = {
        "KITE": 0, "MSTOCK": 0,
        "internet_explained": 0,
        "latency_breaches": {"KITE": 0, "MSTOCK": 0},
    }
    if net_df is None or net_df.empty:
        return out
    ctrl = net_df[net_df["target"].isin(_INTERNET_TARGETS)]
    ctrl_fail_ts = ctrl[~ctrl["ok"].astype(bool)]["ts_ns"].astype("int64").tolist()

    for broker, url in _BROKER_TARGETS.items():
        rows = net_df[net_df["target"] == url]
        for ts, ok, lat in zip(
            rows["ts_ns"].astype("int64"), rows["ok"].astype(bool),
            rows["latency_ms"].astype(float),
        ):
            if ok:
                continue
            near_fail = any(abs(ts - f) <= 90e9 for f in ctrl_fail_ts)
            if near_fail or _overlaps(ts, ts, internet_windows):
                out["internet_explained"] += 1
            else:
                out[broker] += 1
        # Latency red-line breaches with internet up are amiss too.
        red_ms = _NET_THRESHOLDS["broker"]["red"]
        breaches = rows[
            (rows["latency_ms"].astype(float) > red_ms)
            & (rows["ok"].astype(bool))
        ]
        for ts in breaches["ts_ns"].astype("int64"):
            near_fail = any(abs(ts - f) <= 90e9 for f in ctrl_fail_ts)
            if not near_fail and not _overlaps(ts, ts, internet_windows):
                out["latency_breaches"][broker] += 1
    return out


def _count_app_restarts(day, window):
    """
    How many times the app (re)started today, from the per-launch log
    files in data/logs/MM_DD/app_YYYYMMDD_HHMMSS.log. Returns
    {"total": n, "market_hours": n}. Falls back to None counts when
    the log dir is unavailable.
    """
    try:
        log_dir = os.path.join(DATA_DIR, "logs", day.strftime("%m_%d"))
        files = [f for f in os.listdir(log_dir) if f.startswith("app_") and f.endswith(".log")]
        total = len(files)
        in_window = 0
        for f in files:
            try:
                # app_YYYYMMDD_HHMMSS.log -> HHMMSS part
                hhmmss = f.split("_", 2)[2].removesuffix(".log")
                t = datetime.strptime(hhmmss, "%H%M%S").time()
                start = (window[0][0] * 60 + window[0][1]) * 60
                end = (window[1][0] * 60 + window[1][1]) * 60
                secs = t.hour * 3600 + t.minute * 60 + t.second
                if start <= secs <= end:
                    in_window += 1
            except (ValueError, IndexError):
                continue
        return {"total": total, "market_hours": in_window, "source": "logs"}
    except FileNotFoundError:
        return {"total": None, "market_hours": None, "source": "unavailable"}


def _in_run(start_ns, end_ns, runs):
    """True when [start_ns, end_ns] fits entirely inside one run."""
    for s, e in runs or []:
        if s <= start_ns and end_ns <= e:
            return True
    return False


def _broker_stats(df, internet_windows=None, other_runs=None):
    """
    Feed-quality stats for one broker, computed ONLY over rows inside
    the market-hours window (filtered upstream), with every silence
    gap attributed before it can count against the broker:

      - hole in BOTH brokers' capture (the other broker stopped too)
            -> capture-off time (app restart / capture stopped) -
               excused as app-closure, never a broker fault
      - hole while the OTHER broker kept flowing
            -> this broker's feed genuinely died -> broker stall
      - gap/hole overlapping an internet-down window (control probes
        failing)
            -> internet-explained, excused
      - per-symbol gap inside a live capture run -> broker stall

    Attribution of holes runs at BROKER level (one wall-clock hole
    counts once, never once per symbol); the per-symbol table then
    shows the gaps inside contiguous capture runs. Active time =
    capture-run spans, so closures and outages never flatter or
    punish the tick rate.
    """
    if df is None or df.empty:
        return {"ticks": 0, "runs": 0, "span_min": 0.0, "ticks_per_min": 0,
                "max_gap_s": 0, "stalls": 0, "stalls_internet_explained": 0,
                "max_gap_internet_explained_s": 0,
                "app_closure_gaps": 0, "app_closure_min": 0.0,
                "instruments": {}}
    df = df.sort_values("recv_ns")

    runs = _ns_runs(df["recv_ns"])
    active_s = sum((e - s) / 1e9 for s, e in runs)

    # ---- broker-level attribution of the holes BETWEEN capture runs
    # (each wall-clock hole counted exactly once) ----
    app_gaps = 0
    app_gap_s = 0.0
    boundary_int_stalls = 0
    boundary_int_max = 0.0
    boundary_stalls = 0
    boundary_max = 0.0
    for (s1, e1), (s2, e2) in zip(runs, runs[1:]):
        a, b = int(e1), int(s2)
        hole_s = (b - a) / 1e9
        if other_runs and _in_run(a, b, other_runs):
            # The other broker captured straight through this hole -
            # the capture pipeline was alive, this feed went dark.
            boundary_stalls += 1
            boundary_max = max(boundary_max, hole_s)
        elif _overlaps(a, b, internet_windows):
            boundary_int_stalls += 1
            boundary_int_max = max(boundary_int_max, hole_s)
        else:
            app_gaps += 1
            app_gap_s += hole_s

    # ---- per-symbol table: gaps INSIDE contiguous capture runs ----
    per_symbol = {}
    total_stalls = 0
    total_max_gap = 0.0
    exc_int_stalls = 0
    exc_int_max = 0.0
    for symbol, g in df.groupby("symbol"):
        gaps_ms = []
        exc_int_ms = []
        silent_s = 0.0
        present_s = 0.0
        for run_start, run_end in runs:
            rg = g[(g["recv_ns"] >= run_start) & (g["recv_ns"] <= run_end)]
            if rg.empty:
                continue
            rs = rg.sort_values("recv_ns")
            run_span_s = (rs["recv_ns"].iloc[-1] - rs["recv_ns"].iloc[0]) / 1e9
            present_s += run_span_s
            if run_span_s > 1:
                seconds = pd.to_datetime(
                    rs["recv_ns"], unit="ns", utc=True
                ).dt.floor("s")
                silent_s += run_span_s - seconds.nunique()
            ts = rs["recv_ns"].astype("int64").tolist()
            for a, b in zip(ts[:-1], ts[1:]):
                ms = (b - a) / 1e6
                if ms <= 0:
                    continue
                if _overlaps(a, b, internet_windows):
                    exc_int_ms.append(ms)
                else:
                    gaps_ms.append(ms)
        gaps_s = [m / 1000.0 for m in gaps_ms]
        exc_s = [m / 1000.0 for m in exc_int_ms]
        per_symbol[symbol] = {
            "ticks": int(len(g)),
            "p50_gap_ms": round(float(pd.Series(gaps_ms).quantile(0.5)), 1) if gaps_ms else 0,
            "p95_gap_ms": round(float(pd.Series(gaps_ms).quantile(0.95)), 1) if gaps_ms else 0,
            "p99_gap_ms": round(float(pd.Series(gaps_ms).quantile(0.99)), 1) if gaps_ms else 0,
            "max_gap_s": round(max(gaps_s), 1) if gaps_s else 0,
            "stalls_gt_5s": int(sum(1 for s in gaps_s if s > STALL_SECONDS)),
            "silent_seconds_pct": round(100.0 * max(silent_s, 0) / present_s, 1) if present_s > 1 else 0.0,
            "excluded_stalls_internet": int(sum(1 for s in exc_s if s > STALL_SECONDS)),
            "max_excluded_gap_s": round(max(exc_s), 1) if exc_s else 0,
        }
        total_stalls += per_symbol[symbol]["stalls_gt_5s"]
        total_max_gap = max(total_max_gap, per_symbol[symbol]["max_gap_s"])
        exc_int_stalls += per_symbol[symbol]["excluded_stalls_internet"]
        exc_int_max = max(exc_int_max, per_symbol[symbol]["max_excluded_gap_s"])

    out = {}
    out["ticks"] = int(len(df))
    out["runs"] = len(runs)
    out["span_min"] = round(active_s / 60.0, 1) if active_s > 0 else 0.0
    out["ticks_per_min"] = round(len(df) / (active_s / 60.0), 1) if active_s > 0 else 0
    out["instruments"] = per_symbol
    out["max_gap_s"] = round(max(total_max_gap, boundary_max), 1)
    out["stalls"] = total_stalls + boundary_stalls
    out["stalls_internet_explained"] = exc_int_stalls + boundary_int_stalls
    out["max_gap_internet_explained_s"] = round(max(exc_int_max, boundary_int_max), 1)
    out["app_closure_gaps"] = app_gaps
    out["app_closure_min"] = round(app_gap_s / 60.0, 1)
    return out


def _agreement_stats(kite_df, mstock_df):
    result = {}
    worst_median = 0.0
    for symbol in set(kite_df["symbol"]) & set(mstock_df["symbol"]):
        if not symbol:
            continue
        k = kite_df[kite_df["symbol"] == symbol].copy()
        m = mstock_df[mstock_df["symbol"] == symbol].copy()
        if k.empty or m.empty:
            continue
        k["sec"] = pd.to_datetime(k["recv_ns"], unit="ns").dt.floor("s")
        m["sec"] = pd.to_datetime(m["recv_ns"], unit="ns").dt.floor("s")
        k_sec = k.groupby("sec")["ltp"].median()
        m_sec = m.groupby("sec")["ltp"].median()
        joined = pd.concat([k_sec.rename("kite"), m_sec.rename("mstock")], axis=1, join="inner").dropna()
        if joined.empty:
            continue
        delta = (joined["kite"] - joined["mstock"]).abs()
        result[symbol] = {
            "aligned_seconds": int(len(joined)),
            "median_abs_delta": round(float(delta.median()), 2),
            "max_abs_delta": round(float(delta.max()), 2),
            "equal_pct": round(100.0 * float((delta < 0.01).mean()), 1),
        }
        worst_median = max(worst_median, float(delta.median()))
    result["max_instrument_median_abs_delta"] = round(worst_median, 2)
    return result


def _leader_stats(kite_df, mstock_df):
    counts = {"KITE_first": 0, "MSTOCK_first": 0, "tie": 0}
    leads = []
    for symbol in set(kite_df["symbol"]) & set(mstock_df["symbol"]):
        if not symbol:
            continue
        k = kite_df[kite_df["symbol"] == symbol].sort_values("recv_ns")
        m = mstock_df[mstock_df["symbol"] == symbol].sort_values("recv_ns")
        if k.empty or m.empty:
            continue
        k["sec"] = pd.to_datetime(k["recv_ns"], unit="ns").dt.floor("s")
        m["sec"] = pd.to_datetime(m["recv_ns"], unit="ns").dt.floor("s")
        k_sec = k.groupby("sec")["ltp"].median()
        m_sec = m.groupby("sec")["ltp"].median()
        k_changes = set(k_sec[k_sec.diff().abs() > 0.01].index)
        m_changes = set(m_sec[m_sec.diff().abs() > 0.01].index)
        for sec in k_changes | m_changes:
            in_k, in_m = sec in k_changes, sec in m_changes
            if in_k and in_m:
                kt = k[k["sec"] == sec]["recv_ns"].min()
                mt = m[m["sec"] == sec]["recv_ns"].min()
                if kt < mt:
                    counts["KITE_first"] += 1
                    leads.append((mt - kt) / 1e6)
                else:
                    counts["MSTOCK_first"] += 1
                    leads.append((kt - mt) / 1e6)
            elif in_k:
                counts["KITE_first"] += 1
            else:
                counts["MSTOCK_first"] += 1
        both = k_sec.index.intersection(m_sec.index)
        for sec in both:
            if abs(float(k_sec[sec]) - float(m_sec[sec])) < 0.01:
                counts["tie"] += 1
    counts["median_lead_ms"] = round(float(pd.Series(leads).median()), 1) if leads else None
    return counts


def feed_lab_enabled():
    try:
        return str(fetch_from_json("appconfig.json", "FEED_LAB_ENABLED")).strip().lower() == "true"
    except Exception:
        return False


feed_capture_service = FeedCaptureService()


# ------------------------------------------------------------------
# POSITION-SCOPED DUAL-BROKER TICK CAPTURE
#
# While the user HOLDS a position, capture ticks for the held symbols
# from BOTH brokers (MStock + Kite) regardless of which one the app is
# logged into - the Kite capture socket needs only a valid Kite token
# for the day, and the MStock capture socket its own JWT; neither
# touches the trading feed. The EOD technical report then counts, per
# held position, how many ticks arrived from each broker during the
# holding window (see session_report._position_tick_section).
# ------------------------------------------------------------------

# Symbol formats seen in position legs that never equal the chain's
# exchange-format names ("NIFTY26O0622800CE"):
#   broker position rows:  "NIFTY-06Oct2026-22800-CE"
# Cross-broker token resolution keys off the exchange format, so
# normalize every leg symbol before use.
_POSITION_SYMBOL_RE = re.compile(
    r"^(?P<u>[A-Z]+)-?(?P<dd>\d{1,2})(?P<mon>[A-Z]{3})(?P<yyyy>\d{4})"
    r"-?(?P<strike>\d+)-?(?P<typ>CE|PE)$"
)
_MONTH_CODES = {"JAN": "1", "FEB": "2", "MAR": "3", "APR": "4",
                "MAY": "5", "JUN": "6", "JUL": "7", "AUG": "8",
                "SEP": "9", "OCT": "O", "NOV": "N", "DEC": "D"}


def _normalize_position_symbol(symbol):
    """'NIFTY-06Oct2026-22800-CE' / 'NIFTY06OCT202622800CE' ->
    'NIFTY26O0622800CE' (exchange weekly format). Unknown formats
    return uppercased input unchanged."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return sym
    m = _POSITION_SYMBOL_RE.match(sym)
    if not m:
        return sym
    mc = _MONTH_CODES.get(m.group("mon"))
    if not mc:
        return sym
    return (f"{m.group('u')}{m.group('yyyy')[-2:]}{mc}"
            f"{m.group('dd').zfill(2)}{m.group('strike')}{m.group('typ')}")


_position_trader_ref = None
_position_capture_service = FeedCaptureService()
_pos_supervisor_stop = threading.Event()
_POSITION_GRACE_S = 120.0      # keep capturing 2 min after the last close
_POSITION_POLL_S = 3.0


def register_trader(trader):
    """Give the position-capture supervisor a read handle on open
    positions (Trader_Singleton; call once at startup)."""
    global _position_trader_ref
    _position_trader_ref = trader


def _held_symbols():
    """Exchange-format symbols of every currently-open position leg."""
    trader = _position_trader_ref
    if trader is None:
        return set()
    position_map = getattr(trader, "_position_data", {}) or {}
    legs = (position_map.values() if hasattr(position_map, "values")
            else position_map)
    out = set()
    for leg in list(legs):
        if not isinstance(leg, dict):
            continue
        sym = _normalize_position_symbol(
            leg.get("tradingsymbol") or leg.get("symbol") or ""
        )
        if sym:
            out.add(sym)
    return out


def _position_capture_supervisor():
    """Start the dual-broker capture when a position opens, restart it
    when the held-symbol set changes, and stop it after a grace period
    once the last position closes (quick re-entries must not lose the
    first ticks)."""
    grace_until = None
    while not _pos_supervisor_stop.is_set():
        try:
            symbols = _held_symbols()
            # A manual Feed Lab capture or an always-on chain capture
            # may already cover some held symbols - only capture what
            # NEITHER covers (it is user-started / automatic ATM-scoped;
            # never leave a held symbol uncaptured just because it
            # trades on a different underlying than the manual session
            # - 2026-10-06 13:12 a NIFTY position went uncounted while
            # a SENSEX manual capture ran).
            covered = feed_capture_service.running_symbols()
            for chain_svc in _chain_services.values():
                covered |= chain_svc.running_symbols()
            symbols = symbols - covered
            if symbols:
                grace_until = None
                if _position_capture_service.running_symbols() != symbols:
                    if _position_capture_service._running:
                        _position_capture_service.stop()
                    underlying = str(
                        fetch_from_json("appconfig.json", "UNDERLYING") or "NIFTY"
                    ).upper()
                    ok, note = _position_capture_service.start_position_capture(
                        underlying, symbols
                    )
                    if note:
                        logger.info(f"Feed Lab: position capture note - {note}")
            elif _position_capture_service._running:
                if grace_until is None:
                    grace_until = time.time() + _POSITION_GRACE_S
                elif time.time() > grace_until:
                    _position_capture_service.stop()
                    grace_until = None
        except Exception as sup_error:
            logger.warning(f"Feed Lab: position-capture supervisor: {sup_error}")
        _pos_supervisor_stop.wait(_POSITION_POLL_S)


def start_position_capture_monitor():
    _pos_supervisor_stop.clear()
    threading.Thread(
        target=_position_capture_supervisor, daemon=True,
        name="feedlab-position-capture").start()
    logger.info("Feed Lab: position tick-capture monitor started")


# ------------------------------------------------------------------
# CHAIN CAPTURE SUPERVISOR (always-on market-hours capture)
#
# Keeps one FeedCaptureService per underlying in CHAIN_UNDERLYINGS
# running for the whole market window of that underlying. Each service
# streams FUT + ATM +/- CHAIN_STRIKES CE/PE from BOTH brokers. On app
# restart the next poll restarts the captures; when the ATM drifts
# beyond the captured strikes the session is re-resolved with the
# strikes UNIONED (an already-held contract never falls out of scope).
# The manual Feed Lab capture takes precedence over the same
# underlying's chain capture (no duplicate symbol rows for the
# agreement analysis) - the other underlying keeps streaming.
# ------------------------------------------------------------------

_chain_services = {}
_chain_supervisor_stop = threading.Event()
# Underlying -> last monotonic time the "chain capture note" warning
# was logged (rate-limited by the supervisor).
_chain_note_logged = {}
_drift_note_logged = {}


def _chain_service(underlying):
    svc = _chain_services.get(underlying)
    if svc is None:
        svc = FeedCaptureService()
        _chain_services[underlying] = svc
    return svc


def _in_market_hours(underlying):
    start, end = _market_window(underlying)
    now = datetime.now().time()
    return start <= (now.hour, now.minute) <= end


def _chain_atm_drifted(svc, underlying):
    """True when the current ATM's +/- CHAIN_STRIKES window is no
    longer fully inside the captured strike span (or the chosen expiry
    has rolled over) - time to re-resolve."""
    session = svc._session
    if not session:
        return False
    strikes = [int(s) for s in session.get("strikes", [])]
    if not strikes:
        return True
    # Expiry roll: yesterday's lock must not survive into a new expiry.
    try:
        if date.fromisoformat(str(session.get("expiry"))) < date.today():
            return True
    except (TypeError, ValueError):
        pass
    cfg = UNDERLYING_CONFIG[underlying]
    step = cfg["step"]
    fut_sym = session.get("fut_symbol")
    if not fut_sym:
        return False
    fut_ltp, _ = svc._fut_ltp(underlying, _day_dir(), kite_fut_sym=fut_sym)
    if fut_ltp is None:
        # Unreachable anchor (Kite token lost mid-day) is logged once
        # per underlying per hour, then silently retried.
        last = _drift_note_logged.get(underlying, 0)
        if time.time() - last > 3600:
            _drift_note_logged[underlying] = time.time()
            logger.warning(f"Feed Lab: chain drift check for {underlying} skipped - no fut LTP")
        return False
    new_atm = int(round(fut_ltp / step) * step)
    span_lo, span_hi = min(strikes), max(strikes)
    # Re-resolve the moment the ATM's own +/- CHAIN_STRIKES window
    # pokes out of the captured span (union policy keeps widening it).
    return (new_atm - CHAIN_STRIKES * step < span_lo
            or new_atm + CHAIN_STRIKES * step > span_hi)


def _chain_capture_supervisor():
    while not _chain_supervisor_stop.is_set():
        try:
            for underlying in CHAIN_UNDERLYINGS:
                if not _in_market_hours(underlying):
                    svc = _chain_services.get(underlying)
                    if svc is not None and svc._running:
                        svc.stop()
                        logger.info(f"Feed Lab: CHAIN capture for {underlying} stopped (outside market window)")
                    continue
                svc = _chain_service(underlying)
                manual_u = str(
                    (feed_capture_service._session or {}).get("underlying") or ""
                ).upper() if feed_capture_service._running else None
                if manual_u == underlying:
                    # Manual capture owns this underlying's slice today -
                    # pause the chain capture to avoid duplicate rows.
                    if svc._running:
                        svc.stop()
                        logger.info(
                            f"Feed Lab: CHAIN capture for {underlying} paused "
                            f"(manual capture running for {underlying})")
                    continue
                if svc._running:
                    if _chain_atm_drifted(svc, underlying):
                        logger.info(f"Feed Lab: chain re-resolving {underlying} (ATM drift/expiry roll)")
                        svc.stop()
                        ok, note = svc.start_chain(underlying)
                        if note:
                            logger.warning(f"Feed Lab: chain re-resolve note for {underlying}: {note}")
                    continue
                ok, note = svc.start_chain(underlying)
                if ok:
                    _chain_note_logged.pop(underlying, None)
                elif note:
                    # Pairs with the rate-limited error inside
                    # start_chain - repeat the note at most every 5
                    # minutes instead of every poll.
                    now = time.monotonic()
                    if now - _chain_note_logged.get(underlying, 0.0) >= 300:
                        logger.warning(f"Feed Lab: chain capture note for {underlying}: {note}")
                        _chain_note_logged[underlying] = now
        except Exception as chain_error:
            logger.warning(f"Feed Lab: chain-capture supervisor: {chain_error}")
        _chain_supervisor_stop.wait(_CHAIN_POLL_S)


def start_chain_capture_monitor():
    _chain_supervisor_stop.clear()
    threading.Thread(
        target=_chain_capture_supervisor, daemon=True,
        name="feedlab-chain-capture").start()
    logger.info(
        f"Feed Lab: chain tick-capture monitor started "
        f"({', '.join(CHAIN_UNDERLYINGS)}; FUT + ATM +/- {CHAIN_STRIKES} CE/PE, both brokers)")


def chain_status():
    """Status payload for the audit tab: one entry per chain underlying."""
    out = {}
    for underlying in CHAIN_UNDERLYINGS:
        svc = _chain_services.get(underlying)
        entry = {"running": False, "counts": {"KITE": 0, "MSTOCK": 0}}
        if svc is not None:
            session = svc._session if svc._running else None
            entry.update({
                "running": bool(svc._running),
                "started_at": svc._started_at,
                "counts": dict(svc._counts),
                "last_error": svc._last_error,
                "atm_strike": (session or {}).get("atm_strike"),
                "strikes": (session or {}).get("strikes"),
                "expiry": (session or {}).get("expiry"),
                "instruments": {
                    b: {v["symbol"] for v in (session or {}).get(b, {}).values()}
                    for b in ("KITE", "MSTOCK")
                } if session else None,
            })
        out[underlying] = entry
    return out

