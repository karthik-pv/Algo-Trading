"""TrendTracker: live per-timeframe trend-state transitions from TV webhooks.

Companion to forecasting.engine (which forecasts the rest-of-day shape from
each snapshot). Where the engine answers "what to expect", the tracker
answers "what just changed and how long did each state last":

  1. On every MSG:"DATA" webhook, derive each TF's trend state
     (5S/15S/1M/5M/30M/2H: PVT sign + MACD1 12/26 + MACD2 24/52) and log
     every TRANSITION with a timestamp (JSONL, one file per day).
  2. On every MSG:"LEVELS" webhook, track the seven FAM family tags and the
     compact W/D/Q HTF PM states the same way.
  3. summary() -> current state per TF (with time-in-state), recent events;
     attached to the Socket.IO payload as "TrendTracking" and served by
     GET /tv/trend.

The 5M block is tracked with the finest granularity (price-vs-EMA/VWAP flags
on top of the indicator states) - the session is anchored on 5m tracking.

All failures are contained: on_data / on_levels / summary never raise.
Runtime output: data/trend_events/YYYY-MM-DD.jsonl (gitignored).
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_EVENT_DIR = os.path.join(_REPO_ROOT, "data", "trend_events")

IST = timezone(timedelta(hours=5, minutes=30))

# intraday TFs as they appear in the DATA payload's INDICATORS blocks
INTRADAY_TFS = ("5S", "15S", "1M", "5M", "30M", "2H")
# the finest-grained TF - gets the extra price-vs-EMA/VWAP flags
PRIMARY_TF = "5M"

# FAM tag order on the wire (Pine v1.12): "5,3,2,D,W,M,Q"
FAM_SLOTS = ("5", "3", "2", "D", "W", "M", "Q")

MAX_RECENT_EVENTS = 20


def _ist_now():
    return datetime.now(IST)


def _flag(block, key):
    """Read a +/-1 int flag from a TF block, None when missing/NaN."""
    try:
        v = float(block.get(key))
    except (TypeError, ValueError, AttributeError):
        return None
    if v != v:  # NaN
        return None
    return 1 if v > 0 else -1


def _price(block):
    try:
        v = float(block.get("VWAP"))
    except (TypeError, ValueError, AttributeError):
        return None
    return v if v and v > 0 else None


def _tf_state(block, price=None):
    """Derive one TF's trend state dict from a DATA INDICATORS block."""
    pvt = _flag(block, "PVTTrendFlag")
    m1 = _flag(block, "Trend_1226")
    m2 = _flag(block, "Trend_2452")
    k = _flag(block, "KTrendFlag")
    voc = _flag(block, "VOCTrendFlag")
    state = {"pvt": pvt, "m1": m1, "m2": m2, "k": k, "voc": voc}
    # label: PVT + MACD2 24/52 are the trend core (same pair the FAM tag uses)
    if pvt == 1 and m2 == 1:
        state["label"] = "UP"
    elif pvt == -1 and m2 == -1:
        state["label"] = "DOWN"
    elif pvt is None or m2 is None:
        state["label"] = None
    else:
        state["label"] = "MIXED"
    # 5M extra: where price sits vs the EMA stack / VWAP (price proxy = 1m VWAP)
    pos = {}
    if price:
        for n in (9, 21, 50, 100, 200):
            try:
                ema = float(block.get("EMA%d" % n))
            except (TypeError, ValueError, AttributeError):
                continue
            if ema and ema > 0:
                pos["EMA%d" % n] = 1 if price > ema else -1
        try:
            vwap = float(block.get("VWAP"))
            if vwap and vwap > 0:
                pos["VWAP"] = 1 if price > vwap else -1
        except (TypeError, ValueError, AttributeError):
            pass
    if pos:
        state["price_vs"] = pos
    return state


def _simplify(state):
    """State reduced to the fields worth comparing across webhooks."""
    return json.dumps(state, sort_keys=True)


class TrendTracker(object):
    def __init__(self, event_dir=DEFAULT_EVENT_DIR):
        self._lock = threading.Lock()
        self._event_dir = event_dir
        self._day = None
        self._last = {}          # kind -> last simplified state (tf-level)
        self._since = {}         # kind -> epoch seconds the state started
        self._states = {}        # kind -> current full state dict
        self._fam = None         # last FAM tag tuple
        self._htf = None         # last HTF state dict
        self._recent = []        # recent transition events (newest last)
        self._counts = {}        # per-day event count

    # -- event plumbing ----------------------------------------------------

    def _transition(self, now_ist, kind, tf, frm, to, extra=None):
        ev = {
            "ts": now_ist.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "kind": kind,
            "tf": tf,
            "from": frm,
            "to": to,
        }
        if extra:
            ev.update(extra)
        self._recent.append(ev)
        self._recent = self._recent[-MAX_RECENT_EVENTS * 4:]
        self._write_event(now_ist, ev)

    def _write_event(self, now_ist, ev):
        try:
            day = now_ist.strftime("%Y-%m-%d")
            os.makedirs(self._event_dir, exist_ok=True)
            path = os.path.join(self._event_dir, "%s.jsonl" % day)
            with open(path, "a") as f:
                f.write(json.dumps(ev) + "\n")
            self._counts[day] = self._counts.get(day, 0) + 1
        except Exception as e:
            print("[trend_track] event write failed: %s" % e)

    def _rollover(self, now_ist):
        day = now_ist.strftime("%Y-%m-%d")
        if self._day != day:
            # intraday states are re-derived from the first webhook of the
            # day; clear the transition memory so the day's first snapshot
            # is a baseline, not a wall of spurious "transitions".
            self._day = day
            self._last = {}
            self._since = {}
            self._fam = None
            self._htf = None

    # -- ingestion ---------------------------------------------------------

    def on_data(self, data):
        """Track MSG:"DATA" webhooks (per-TF trend states)."""
        try:
            now_ist = _ist_now()
            with self._lock:
                self._rollover(now_ist)
                ind = (data or {}).get("INDICATORS") or {}
                # price proxy: 1m VWAP (same fallback the forecast engine uses)
                price = _price(ind.get("1M") or {}) or _price(ind.get("5M") or {})
                for tf in INTRADAY_TFS:
                    block = ind.get(tf)
                    if not isinstance(block, dict):
                        continue
                    state = _tf_state(block, price=(price if tf == PRIMARY_TF else None))
                    if state.get("label") is None and "price_vs" not in state:
                        continue
                    key = "TF:" + tf
                    snap = _simplify(state)
                    prev = self._last.get(key)
                    if prev != snap:
                        if prev is not None:
                            self._transition(now_ist, "TF", tf,
                                             json.loads(prev), state)
                        # day's first snapshot = baseline (no transition event)
                        self._since[key] = time.time()
                        self._last[key] = snap
                    self._states[key] = state
        except Exception as e:
            print("[trend_track] on_data failed: %s" % e)

    def on_levels(self, data):
        """Track MSG:"LEVELS" webhooks (FAM family tags + HTF PM states)."""
        try:
            now_ist = _ist_now()
            with self._lock:
                self._rollover(now_ist)
                lv = (data or {}).get("LEVELS") or {}
                fam = lv.get("FAM")
                if isinstance(fam, str) and fam:
                    tags = fam.split(",")
                    if len(tags) == len(FAM_SLOTS):
                        if self._fam is not None and tags != self._fam:
                            for slot, old, new in zip(FAM_SLOTS, self._fam, tags):
                                if old != new:
                                    self._transition(now_ist, "FAM", slot, old, new)
                            self._since["FAM"] = time.time()
                        elif self._fam is None:
                            self._since["FAM"] = time.time()
                        self._fam = tags
                htf = (data or {}).get("HTF") or {}
                if isinstance(htf, dict) and htf:
                    if self._htf is not None and htf != self._htf:
                        for tf in sorted(set(self._htf) | set(htf)):
                            old, new = self._htf.get(tf), htf.get(tf)
                            if old != new:
                                self._transition(now_ist, "HTF", tf, old, new)
                        self._since["HTF"] = time.time()
                    elif self._htf is None:
                        self._since["HTF"] = time.time()
                    self._htf = htf
        except Exception as e:
            print("[trend_track] on_levels failed: %s" % e)

    # -- output ------------------------------------------------------------

    def summary(self):
        """Current states + recent transitions (never raises)."""
        try:
            now = time.time()
            states = {}
            for key, state in self._states.items():
                entry = dict(state)
                entry["since_s"] = int(now - self._since[key]) if key in self._since else None
                states[key.split(":", 1)[1]] = entry
            fam = None
            if self._fam:
                fam = {slot: tag for slot, tag in zip(FAM_SLOTS, self._fam)}
                if "FAM" in self._since:
                    fam["since_s"] = int(now - self._since["FAM"])
            htf = dict(self._htf) if self._htf else None
            return {
                "ts": _ist_now().strftime("%Y-%m-%dT%H:%M:%S%z"),
                "day": self._day,
                "events_today": self._counts.get(self._day, 0),
                "states": states,
                "fam": fam,
                "htf": htf,
                "recent_events": self._recent[-MAX_RECENT_EVENTS:],
            }
        except Exception as e:
            print("[trend_track] summary failed: %s" % e)
            return None


class _LazyTracker(object):
    """Lazy singleton proxy: tracker initializes on first attribute use."""

    def __init__(self):
        self._tracker = None
        self._lock = threading.Lock()

    def _get(self):
        if self._tracker is None:
            with self._lock:
                if self._tracker is None:
                    self._tracker = TrendTracker()
        return self._tracker

    def __getattr__(self, name):
        return getattr(self._get(), name)


trend_tracker = _LazyTracker()
