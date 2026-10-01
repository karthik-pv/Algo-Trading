"""Setup detection: 15s snapshot + regime state -> active setup call.

Reads forecast/setup_library.json (user-editable). First match wins by
library order. Detector is heuristic v0.1 — thresholds are placeholders
for user tweaking.

Called from ForecastEngine.on_snapshot; the result rides the forecast dict
as "setup_call" and is rendered by the front-end Setup Call panel.
"""

import json
import os
import threading
from datetime import datetime, timedelta

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_PATH = os.path.join(_REPO_ROOT, "forecasting", "setup_library.json")

IST_TZ = None  # set from engine to avoid duplication; detector receives datetimes

OR_START = (9, 15)    # opening range window (IST, market open)
OR_END = (9, 45)
MORNING_END = (11, 0)
SQUEEZE_WIDTH_PCT = 0.0012   # 1m EMA ribbon max-min within 0.12% of price
TREND_LOOSE_PCT = 0.0006     # ribbon "loose" threshold for non-squeeze
VWAP_EPS_PCT = 0.0004        # "at VWAP" tolerance

# library order == priority
_STATE_ACTIVE = "ACTIVE"
_STATE_WATCH = "WATCH"


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _within(t, hhmm_start, hhmm_end):
    m = t.hour * 60 + t.minute
    return hhmm_start[0] * 60 + hhmm_start[1] <= m < hhmm_end[0] * 60 + hhmm_end[1]


def _stack_ordered(st, bull):
    """1m EMA stack ordered (bull: E9>=E21>=E50>=E100>=E200; bear mirrored)."""
    vals = [st.get(k) for k in ("ema9", "ema21", "ema50", "ema100", "ema200")]
    if any(v is None for v in vals):
        return False
    diffs = [vals[i] - vals[i + 1] for i in range(4)]
    return all(d >= 0 for d in diffs) if bull else all(d <= 0 for d in diffs)


def _ribbon_width_pct(st, price):
    vals = [st.get(k) for k in ("ema9", "ema21", "ema50", "ema100", "ema200")]
    vals = [v for v in vals if v is not None]
    if len(vals) < 4 or not price:
        return None
    return (max(vals) - min(vals)) / float(price)


class SetupDetector(object):
    """Stateful across snapshots: tracks session extremes, OR, VWAP side."""

    def __init__(self, lib_path=LIB_PATH):
        self._lib_path = lib_path
        self._lock = threading.Lock()
        self._lib = None
        self._day = None
        self._session_high = None
        self._session_low = None
        self._or_high = None
        self._or_low = None
        self._prev_call = None   # last emitted {id, state} for stability

    @property
    def library(self):
        if self._lib is None:
            try:
                with open(self._lib_path) as f:
                    self._lib = json.load(f)
            except Exception as e:
                print("[setups] failed to load setup_library.json: %s" % e)
                self._lib = {"setups": []}
        return self._lib

    def reset_day(self, day):
        with self._lock:
            self._day = day
            self._session_high = None
            self._session_low = None
            self._or_high = None
            self._or_low = None
            self._prev_call = None

    # -- main --------------------------------------------------------------

    def on_snapshot(self, rv, price, daily_ctx, setup_ab, now_ist, day=None):
        """Return setup_call dict or None (no data)."""
        if not rv or price is None:
            return None
        with self._lock:
            if day and day != self._day:
                self.reset_day(day)
            self._track_session(price, now_ist)
            call = self._detect(rv, price, daily_ctx or {}, setup_ab, now_ist)
            # WATCH/ACTIVE stickiness: hold previous id if nothing new qualifies
            if call is None and self._prev_call and self._prev_call.get("state") == _STATE_WATCH:
                pc = dict(self._prev_call)
                pc["state"] = _STATE_WATCH
                pc["reason"] = "watching (preconditions set, no trigger yet)"
                return pc
            if call:
                self._prev_call = {"id": call["id"], "state": call["state"]}
            return call

    def _track_session(self, price, now_ist):
        if self._session_high is None or price > self._session_high:
            self._session_high = price
        if self._session_low is None or price < self._session_low:
            self._session_low = price
        if _within(now_ist, OR_START, OR_END):
            if self._or_high is None or price > self._or_high:
                self._or_high = price
            if self._or_low is None or price < self._or_low:
                self._or_low = price

    # -- individual checks (return (state, reason) or None) ----------------

    def _check_tdy(self, rv, price, dc, setup_ab, now_ist):
        k = (rv.get("d_sign"), rv.get("h2_sign"), rv.get("m30_sign"))
        if not (k[0] and k[1] and k[2]) or not (k[0] == k[1] == k[2]):
            return None
        bull = k[0] > 0
        m1 = rv["states"].get("1M") or {}
        vwap = m1.get("vwap")
        if vwap is None:
            return None
        on_side = price > vwap if bull else price < vwap
        if not on_side:
            return None
        if not _stack_ordered(m1, bull):
            return None
        # trigger: pullback holds near EMA50/E100 (within loose band)
        e50, e100 = m1.get("ema50"), m1.get("ema100")
        near = None
        for e in (e50, e100):
            if e is not None and abs(price - e) / price < TREND_LOOSE_PCT * 4:
                near = e
                break
        state = _STATE_ACTIVE if near is not None else _STATE_WATCH
        return state, "full regime %s, 1m stack ordered, price on VWAP side%s" % (
            "+|" .join(("+" if x > 0 else "-") for x in k), "" if near else " — waiting pullback hold")

    def _check_tcp(self, rv, price, dc, setup_ab, now_ist):
        sigs = [s for s in (rv.get("d_sign"), rv.get("h2_sign"), rv.get("m30_sign")) if s]
        if len(sigs) < 2 or len(set(sigs)) != 1:
            return None
        bull = sigs[0] > 0
        m5 = rv["states"].get("5M") or {}
        e50, e100, e200 = m5.get("ema50"), m5.get("ema100"), m5.get("ema200")
        for e in (e50, e100, e200):
            if e is not None and (price - e) * (1 if bull else -1) / price < TREND_LOOSE_PCT * 6 \
                    and (price - e) * (1 if bull else -1) >= 0:
                # price sitting on/just above (bull) the 5m EMA cluster
                state = _STATE_ACTIVE if m5.get("macd2_bear") is not None and \
                    (m5["macd2_bear"] is False if bull else m5["macd2_bear"] is True) else _STATE_WATCH
                return state, "2/3 regime %s, price at 5m EMA %.1f pullback zone" % (
                    "bull" if bull else "bear", e)
        return None

    def _check_vwr_vwh(self, rv, price, dc, setup_ab, now_ist):
        m1 = rv["states"].get("1M") or {}
        vwap = m1.get("vwap")
        if vwap is None:
            return None
        at_vwap = abs(price - vwap) / price < VWAP_EPS_PCT * 8
        below = price < vwap
        m30_neg = (rv.get("m30_sign") or 0) < 0
        m30_pos = (rv.get("m30_sign") or 0) > 0
        if at_vwap and below and (m30_neg or rv.get("d_sign") == -1):
            ab = " (research Setup %s fired)" % setup_ab["type"] if setup_ab else ""
            if setup_ab or self._crossed_down_recently(rv):
                return _STATE_ACTIVE, "rejecting VWAP from below, 30m/D bearish" + ab, "VWR"
            return _STATE_WATCH, "price testing VWAP from below, bearish regime — waiting 15s cross-down", "VWR"
        if at_vwap and not below and (m30_pos or rv.get("d_sign") == 1):
            if setup_ab or self._crossed_up_recently(rv):
                return _STATE_ACTIVE, "holding VWAP from above, 30m bullish", "VWH"
            return _STATE_WATCH, "price testing VWAP from above, bullish regime — waiting 15s cross-up", "VWH"
        return None

    def _crossed_down_recently(self, rv):
        m1 = rv["states"].get("1M") or {}
        m5 = rv["states"].get("5M") or {}
        return m1.get("macd2_bear") is True and m5.get("macd2_bear") is not False

    def _crossed_up_recently(self, rv):
        m1 = rv["states"].get("1M") or {}
        m5 = rv["states"].get("5M") or {}
        return m1.get("macd2_bear") is False and m5.get("macd2_bear") is not True

    def _check_orb(self, rv, price, dc, setup_ab, now_ist):
        if self._or_high is None or self._or_low is None:
            return None
        if not _within(now_ist, OR_START, MORNING_END):
            return None
        width_ok = (self._or_high - self._or_low) / price > 0.0005
        if price > self._or_high:
            bull = True
            edge = self._or_high
        elif price < self._or_low:
            bull = False
            edge = self._or_low
        else:
            return None
        m1 = rv["states"].get("1M") or {}
        macd_ok = m1.get("macd2_bear") is False if bull else m1.get("macd2_bear") is True
        state = _STATE_ACTIVE if macd_ok and width_ok else _STATE_WATCH
        return state, "%s OR edge %.1f (OR %.0f-%.0f)%s" % (
            "above" if bull else "below", edge, self._or_low, self._or_high,
            "" if state == _STATE_ACTIVE else " — awaiting MACD_2452 agreement")

    def _check_pdx(self, rv, price, dc, setup_ab, now_ist):
        pdh, pdl = _f(dc.get("pdh")), _f(dc.get("pdl"))
        tol = price * 0.0002
        if pdh is not None and self._session_high is not None and \
                self._session_high >= pdh - tol and price < pdh - tol:
            return _STATE_ACTIVE, "swept PDH %.1f, closed back below — reclaim short zone" % pdh
        if pdl is not None and self._session_low is not None and \
                self._session_low <= pdl + tol and price > pdl + tol:
            return _STATE_ACTIVE, "swept PDL %.1f, closed back above — reclaim long zone" % pdl
        return None

    def _check_sqz(self, rv, price, dc, setup_ab, now_ist):
        m1 = rv["states"].get("1M") or {}
        w = _ribbon_width_pct(m1, price)
        if w is None or w > SQUEEZE_WIDTH_PCT:
            return None
        m5 = rv["states"].get("5M") or {}
        e_hi = max(v for v in (m1.get(k) for k in ("ema9", "ema21", "ema50", "ema100", "ema200")) if v is not None)
        e_lo = min(v for v in (m1.get(k) for k in ("ema9", "ema21", "ema50", "ema100", "ema200")) if v is not None)
        if price > e_hi:
            return _STATE_ACTIVE, "squeeze broke UP: 1m ribbon %.3f%% wide, price %.1f above coil" % (w * 100, e_hi)
        if price < e_lo:
            return _STATE_ACTIVE, "squeeze broke DOWN: 1m ribbon %.3f%% wide, price %.1f below coil" % (w * 100, e_lo)
        return _STATE_WATCH, "1m ribbon compressed %.3f%% — coil set, awaiting break" % (w * 100)

    def _check_trp(self, rv, price, dc, setup_ab, now_ist):
        # failed OR break (closed back inside)
        if self._or_high is not None and self._or_low is not None and \
                self._session_high is not None and self._session_high > self._or_high and \
                self._or_low < price < self._or_high:
            return _STATE_ACTIVE, "OR-high break failed — price back inside range"
        if self._or_high is not None and self._or_low is not None and \
                self._session_low is not None and self._session_low < self._or_low and \
                self._or_low > price > self._or_high - (self._or_high - self._or_low) * 99:
            pass  # covered by the general inside-range case above
        return None

    def _check_rng(self, rv, price, dc, setup_ab, now_ist):
        sigs = [s for s in (rv.get("h2_sign"), rv.get("m30_sign")) if s]
        if len(sigs) == 2 and sigs[0] != sigs[1]:
            return _STATE_WATCH, "2H/30 conflict (%s) — range day; fade Round100/edges" % rv.get("key")
        return None

    def _check_gap(self, rv, price, dc, setup_ab, now_ist):
        if not _within(now_ist, OR_START, MORNING_END):
            return None
        pdc, pdo = _f(dc.get("pdc")), _f(dc.get("pdopen"))
        if pdc is None:
            return None
        gap_pct = (price - pdc) / pdc
        if abs(gap_pct) < 0.002:
            return None
        tgt = pdo if pdo is not None else pdc
        if abs(price - tgt) / price < VWAP_EPS_PCT * 8:
            return _STATE_WATCH, "gap %.2f%% %s; price at fill target %.1f" % (
                gap_pct * 100, "up" if gap_pct > 0 else "down", tgt)
        return _STATE_WATCH, "gap %.2f%% %s open, fill target %.1f" % (
            gap_pct * 100, "up" if gap_pct > 0 else "down", tgt)

    # -- priority dispatcher ------------------------------------------------

    _CHECKS = ("_check_tdy", "_check_tcp", "_check_vwr_vwh", "_check_orb",
               "_check_pdx", "_check_sqz", "_check_trp", "_check_rng", "_check_gap")

    def _detect(self, rv, price, dc, setup_ab, now_ist):
        for name in self._CHECKS:
            try:
                res = getattr(self, name)(rv, price, dc, setup_ab, now_ist)
            except Exception as e:
                print("[setups] %s failed: %s" % (name, e))
                res = None
            if res:
                return self._build_call(name, res, rv, price, dc)
        return None

    def _build_call(self, check_name, res, rv, price, dc):
        state, reason = res[0], res[1]
        id_by_check = {
            "_check_tdy": "TDY", "_check_tcp": "TCP", "_check_vwr_vwh": "VWR",
            "_check_orb": "ORB", "_check_pdx": "PDX", "_check_sqz": "SQZ",
            "_check_trp": "TRP", "_check_rng": "RNG", "_check_gap": "GAP",
        }
        sid = res[2] if len(res) > 2 else id_by_check[check_name]
        entry = next((s for s in self.library.get("setups", []) if s.get("id") == sid), {})
        return {
            "id": sid,
            "trend": entry.get("trend", ""),
            "name": entry.get("name", sid),
            "liner": entry.get("liner", ""),
            "indicator_setup": entry.get("indicator_setup", ""),
            "indicator_setup_1": entry.get("indicator_setup_1", ""),
            "indicator_setup_2": entry.get("indicator_setup_2", ""),
            "state": state,
            "reason": reason,
            "regime": rv.get("key"),
            "price": round(price, 2),
            "tradecases": entry.get("tradecases", {}),
            "invalidation": entry.get("invalidation"),
            "history": entry.get("history", []),
        }
