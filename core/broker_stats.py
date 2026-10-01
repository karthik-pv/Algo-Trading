"""
Per-broker health instrumentation for the tab-bar status pills.

Tracks, per broker (KITE / MSTOCK):
  - api    : REST call ok/fail counts, last latency, rate-limit (429) hits
  - orders : order placements/cancels, ack-latency samples (p50/p95), failures
  - ticks  : websocket tick quality (total vs bad - zero/missing LTP)
  - session: token validity + expiry label

Read by Feed Lab's net_status() to feed the I / K / M pills. Every method
is lock-guarded, never raises, and cheap enough to call per request/tick.
"""
import threading
import time
from collections import deque
from datetime import datetime

_lock = threading.Lock()
_stats = {}

_ACK_LATENCY_SAMPLES = 50


def _state(broker):
    broker = str(broker or "").upper()
    return _stats.setdefault(broker, {
        "api": {
            "ok": 0, "failed": 0, "rate_limited": 0,
            "consec_failures": 0,
            "last_latency_ms": None, "last_ok_at": None,
            "last_error": None, "last_error_at": None, "last_fail_ts": None,
        },
        "orders": {
            "placed": 0, "failed": 0, "rate_limited": 0,
            "consec_failures": 0,
            "ack_latency_ms": deque(maxlen=_ACK_LATENCY_SAMPLES),
            "last_ack_latency_ms": None,
            "last_error": None, "last_error_at": None, "last_fail_ts": None,
        },
        "ticks": {"total": 0, "bad": 0, "last_bad_at": None},
        "session": {"valid": None, "detail": None, "checked_at": None},
    })


def record_api(broker, ok, latency_ms=None, rate_limited=False, error=None):
    """Record a REST API call result (data or order endpoints)."""
    try:
        with _lock:
            api = _state(broker)["api"]
            if ok:
                api["ok"] += 1
                api["consec_failures"] = 0
                if latency_ms is not None:
                    api["last_latency_ms"] = round(float(latency_ms), 1)
                    api["last_ok_at"] = datetime.now().isoformat()
            else:
                api["failed"] += 1
                api["consec_failures"] += 1
                api["last_fail_ts"] = time.time()
                if error:
                    api["last_error"] = str(error)[:200]
                    api["last_error_at"] = datetime.now().isoformat()
            if rate_limited:
                api["rate_limited"] += 1
    except Exception:
        pass


def record_order(broker, ok, latency_ms=None, rate_limited=False, error=None):
    """Record an order-flow result (place/cancel). latency_ms is the
    order-ack latency (call -> broker response)."""
    try:
        with _lock:
            o = _state(broker)["orders"]
            if ok:
                o["placed"] += 1
                o["consec_failures"] = 0
                if latency_ms is not None:
                    o["ack_latency_ms"].append(float(latency_ms))
                    o["last_ack_latency_ms"] = round(float(latency_ms), 1)
            else:
                o["failed"] += 1
                o["consec_failures"] += 1
                o["last_fail_ts"] = time.time()
                if error:
                    o["last_error"] = str(error)[:200]
                    o["last_error_at"] = datetime.now().isoformat()
            if rate_limited:
                o["rate_limited"] += 1
    except Exception:
        pass


def record_tick(broker, bad=False):
    """Record one websocket tick/packet; bad=True when the LTP is
    missing/zero/unparsable (tick-quality metric)."""
    try:
        with _lock:
            t = _state(broker)["ticks"]
            t["total"] += 1
            if bad:
                t["bad"] += 1
                t["last_bad_at"] = datetime.now().isoformat()
    except Exception:
        pass


def set_session(broker, valid, detail=None):
    """Publish session/token validity (e.g. after a login or when the
    token timestamp/JWT expiry is inspected)."""
    try:
        with _lock:
            _state(broker)["session"].update({
                "valid": bool(valid),
                "detail": str(detail)[:200] if detail else None,
                "checked_at": datetime.now().isoformat(),
            })
    except Exception:
        pass


def _pctl(values, pct):
    vals = sorted(values)
    if not vals:
        return None
    idx = min(len(vals) - 1, int(len(vals) * pct / 100.0))
    return round(vals[idx], 1)


def snapshot(broker):
    """Read-only copy of one broker's stats for the pills/tooltip."""
    try:
        with _lock:
            src = _state(broker)
            out = {}
            for group in ("api", "orders", "ticks", "session"):
                out[group] = {
                    k: v for k, v in src[group].items()
                    if k != "ack_latency_ms"
                }
            # last_fail_ts (epoch) is deliberately KEPT: the pill level
            # logic in feed_capture reads it for the 60s order-failure
            # decay window. The tooltip ignores it.
            ack = list(src["orders"]["ack_latency_ms"])
            out["orders"]["ack_p50_ms"] = _pctl(ack, 50)
            out["orders"]["ack_p95_ms"] = _pctl(ack, 95)
            ticks = out["ticks"]
            ticks["bad_pct"] = (
                round(100.0 * ticks["bad"] / ticks["total"], 2)
                if ticks["total"] else None
            )
            return out
    except Exception:
        return {}
