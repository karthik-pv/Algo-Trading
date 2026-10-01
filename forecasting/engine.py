"""ForecastEngine: webhook snapshot -> rest-of-day forecast.

Singleton (lazy init on first webhook, day-rollover reset). Designed to be
called from core/trade_logic.process_TradingView_Data:

    from forecasting.engine import forecast_engine
    forecast = forecast_engine.on_snapshot(data)   # after trade_setups built
    json_data["Forecast"] = forecast               # rides existing Socket.IO emit

Behavior (HANDOFF 4):
  1. Parse INDICATORS + SETUPS; track per-TF PVT sign flips (cross events).
  2. Detect Setup A / B (1M PVT zero-cross down + MACD_2452 cross-down within
     +/-3 1m bars; A = 5M PVT stays green, B = 5M PVT crosses down too).
  3. Build the level map; look up (family, regime, side) base rates; adjust by
     confluence count (~+20pp per bearish factor, capped, shrunk when n small).
  4. Emit forecast dict and append {ts, snapshot, forecast} to
     data/forecasts/YYYY-MM-DD.jsonl.

All failures are contained: on_snapshot never raises; it returns None and logs.
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone

from forecasting.levels import build_daily_context, collect_levels, rank_levels
from forecasting.regime import _f, regime_vector
from forecasting.setups import SetupDetector

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE = os.path.join(_REPO_ROOT, "data", "insight", "store.json")
DEFAULT_CSV = os.path.join(_REPO_ROOT, "data", "insight", "csv")
DEFAULT_FORECAST_DIR = os.path.join(_REPO_ROOT, "data", "forecasts")

IST = timezone(timedelta(hours=5, minutes=30))

PP_PER_FACTOR = 0.20     # ~+20pp rejection per bearish factor (confluence curve)
FACTOR_NEUTRAL = 1.5     # neutral bearish-factor count
SHRINK_N = 30.0          # touches at which the confluence adjustment is full
CLAMP = (0.05, 0.95)
TOP_N = 4                # levels per side in the forecast

MICRO_FAMILIES = ("1m E50", "1m E100", "1m E200", "5m E50", "5m E100", "5m E200")

_EXPECTED_SHAPE = {
    ("-", "-", "-"): "Trend-down day: pullbacks reject into 1m/5m EMAs (E50 -> E100 chain), "
                     "lower highs; VWAP acts as a wall, not support.",
    ("+", "+", "+"): "Trend-up day: dips hold VWAP/EMAs, higher lows; grind up into "
                     "Round100/PDH tests.",
    ("+", "+", "-"): "Bigger-picture up, 30m pulling back: shallow dip; expect chop around "
                     "30m EMAs before continuation.",
    ("+", "-", "-"): "Bullish daily but 2H/30m rolling over: deep pullback day; watch PDL/PDC.",
    ("-", "-", "+"): "Bearish backdrop, 30m bouncing: relief rally into resistance "
                     "(VWAP/30m EMAs) likely to stall.",
    ("-", "+", "+"): "Bearish daily, short-term green: weak bounce; fade targets at PDH/PDC.",
    ("+", "-", "+"): "Mixed: 2H down, 30m up — range day between PDL and PDH.",
    ("-", "+", "-"): "Mixed: 2H up, 30m down — range day with failed breakouts.",
}


def _ist_now():
    return datetime.now(IST)


def _clamp(x):
    return max(CLAMP[0], min(CLAMP[1], x))


class ForecastEngine(object):
    def __init__(self, store_path=DEFAULT_STORE, csv_dir=DEFAULT_CSV,
                 forecast_dir=DEFAULT_FORECAST_DIR):
        self._lock = threading.Lock()
        self._store_path = store_path
        self._csv_dir = csv_dir
        self._forecast_dir = forecast_dir
        self._store = None
        self._daily_rows = None
        self._daily_ctx = None
        self._day = None
        self._frozen_day = None  # blind-test mode: pinned day, no rollover, no logging
        self._last_1m = {}       # last 1M state for cross detection
        self._last_5m = {}
        self._events = []        # recent cross events: (ts_ist, kind, tf)
        self._active_setup = None
        self._detector = SetupDetector()
        self._loaded = False

    # -- loading -----------------------------------------------------------

    def _ensure_loaded(self):
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                with open(self._store_path) as f:
                    self._store = json.load(f)
            except Exception as e:
                self._store = {}
                print("[forecasting] failed to load store: %s" % e)
            try:
                self._daily_rows = self._load_daily_rows()
            except Exception as e:
                self._daily_rows = []
                print("[forecasting] failed to load D-CSV: %s" % e)
            self._loaded = True

    def _load_daily_rows(self):
        from forecasting.store_builder import read_tv_csv, fnum
        path = os.path.join(self._csv_dir, "NSE_NIFTY1!, 1D_6b212.csv")
        rows = []
        for r in read_tv_csv(path):
            if not r.get("date"):
                continue
            rows.append({
                "date": r["date"],
                "open": fnum(r, "open"), "high": fnum(r, "high"),
                "low": fnum(r, "low"), "close": fnum(r, "close"),
                "pvt": fnum(r, "PVT"),
                "ema50": fnum(r, "D EMA50"),
                "ema100": fnum(r, "D EMA100"),
                "ema200": fnum(r, "D EMA200"),
            })
        return rows

    def set_day(self, date_str):
        """Pin the engine to a specific day (blind tests): uses that day's
        daily context and disables rollover + jsonl logging."""
        self._ensure_loaded()
        self._frozen_day = date_str
        self._day = date_str
        self._events = []
        self._active_setup = None
        self._detector.reset_day(date_str)
        self._last_1m = {}
        self._last_5m = {}
        if self._daily_rows is None:
            self._daily_rows = self._load_daily_rows()
        self._daily_ctx = build_daily_context(self._daily_rows, date_str)

    def _rollover_if_needed(self, now_ist):
        if self._frozen_day:
            return
        day = now_ist.strftime("%Y-%m-%d")
        if self._day != day:
            self._day = day
            self._events = []
            self._active_setup = None
            self._detector.reset_day(day)
            self._last_1m = {}
            self._last_5m = {}
            if self._daily_rows:
                self._daily_ctx = build_daily_context(self._daily_rows, day)

    # -- setup detection ---------------------------------------------------

    def _track_setups(self, now_ist, states):
        """Detect Setup A/B from cross events (1M PVT zero-cross down + 1M
        MACD_2452 cross-down within +/-3 1m bars; A: 5M PVT >= 0; B: 5M PVT
        cross-down within the window)."""
        m1 = states.get("1M") or {}
        m5 = states.get("5M") or {}
        events = self._events

        p1 = m1.get("pvt_sign")
        prev1 = self._last_1m.get("pvt_sign")
        if p1 is not None and prev1 is not None and prev1 >= 0 and p1 < 0:
            events.append((now_ist, "pvt_xdn_1m"))
        self._last_1m["pvt_sign"] = p1

        macd_bear = m1.get("macd2_bear")
        prev_macd = self._last_1m.get("macd2_bear")
        if macd_bear is True and prev_macd is False:
            events.append((now_ist, "macd_xdn_1m"))
        self._last_1m["macd2_bear"] = macd_bear

        p5 = m5.get("pvt_sign")
        prev5 = self._last_5m.get("pvt_sign")
        if p5 is not None and prev5 is not None and prev5 >= 0 and p5 < 0:
            events.append((now_ist, "pvt_xdn_5m"))
        self._last_5m["pvt_sign"] = p5

        # prune events older than 5 minutes
        cutoff = now_ist - timedelta(minutes=5)
        self._events = [e for e in events if e[0] >= cutoff]

        def recent(kind, within_s, since=None):
            t0 = now_ist - timedelta(seconds=within_s)
            for ts, k in reversed(self._events):
                if k == kind and ts >= t0 and (since is None or ts >= since):
                    return ts
            return None

        setup = None
        pvt_ts = recent("pvt_xdn_1m", 300)
        macd_ts = recent("macd_xdn_1m", 300)
        if pvt_ts is not None and macd_ts is not None and \
                abs((pvt_ts - macd_ts).total_seconds()) <= 180:
            strict = abs((pvt_ts - macd_ts).total_seconds()) < 1
            p5_now = m5.get("pvt_sign")
            is_a = p5_now is not None and p5_now >= 0 and \
                recent("pvt_xdn_5m", 180) is None
            is_b = recent("pvt_xdn_5m", 180) is not None
            if is_a or is_b:
                setup = {
                    "type": "B" if is_b else "A",
                    "timing": "strict" if strict else "relaxed",
                    "ts": now_ist.strftime("%H:%M:%S"),
                }
        self._active_setup = setup
        return setup

    # -- confidence --------------------------------------------------------

    def _confidence(self, family, side, regime_key, n_bear, layer):
        """Return (confidence_frac, n, basis_str) or None."""
        if layer == "micro":
            tf = "1m" if family.startswith("1m") else "5m"
            fam = family.replace(" E", " EMA")
            cell = (self._store.get("micro", {}).get(tf, {}).get("levels", {}) or {}).get(fam)
            if not cell or not cell.get("n_rej"):
                return None
            ft = cell["next_bar_conf"] / float(cell["n_rej"])
            basis = "micro: %d rejections while 30m PVT<0, %d%% next-bar follow-through" % (
                cell["n_rej"], round(ft * 100))
            return ft, cell["n_rej"], basis

        table = self._store.get("levels", {}).get(
            "resistance" if side == "resistance" else "support", {})
        cell = (table.get(family) or {}).get(regime_key or "")
        if not cell or not cell.get("n_touch") or cell.get("rej_rate") is None:
            return None
        base = cell["rej_rate"]
        n = cell["n_touch"]
        # confluence adjustment (~+20pp per bearish factor), shrunk when n small
        if n_bear is not None:
            w = min(1.0, n / SHRINK_N)
            delta = PP_PER_FACTOR * (n_bear - FACTOR_NEUTRAL) * w
            rate = base + delta if side == "resistance" else base - delta
        else:
            rate = base
        rate = _clamp(rate)
        basis = "%s base %.0f%%[n=%d]" % (regime_key or "?", base * 100, n)
        if n_bear is not None and n >= 5:
            basis += " %s confluence %d/3 bearish" % (
                "+" if rate >= base else "-", n_bear)
        return rate, n, basis

    # -- main entry --------------------------------------------------------

    def on_snapshot(self, data, log=True):
        try:
            return self._on_snapshot(data, log=log)
        except Exception as e:
            import traceback
            print("[forecasting] on_snapshot failed: %s\n%s" % (e, traceback.format_exc()))
            return None

    def _on_snapshot(self, data, log=True):
        self._ensure_loaded()
        now_ist = _ist_now()
        with self._lock:
            self._rollover_if_needed(now_ist)

            rv = regime_vector(data, self._daily_ctx and _sgn(self._daily_ctx.get("pd_pvt")))
            states = rv["states"]
            setup = self._track_setups(now_ist, states)
            price, levels = collect_levels(data, self._daily_ctx)
            if price is None:
                return None
            resistance, support = rank_levels(price, levels, self._store)

            n_bear = rv["n_bear_30m"]
            regime_key = rv["key"]

            def build_side(entries, max_n):
                out = []
                for entry in entries[:max_n * 3]:  # allow skip-outs
                    fam = entry["family"]
                    layer = "micro" if fam in MICRO_FAMILIES else "core"
                    conf = self._confidence(fam, entry["side"], regime_key, n_bear, layer)
                    if conf is None:
                        continue
                    rate, n, basis = conf
                    item = {
                        "level": round(entry["value"], 2),
                        "type": fam,
                        "confidence": round(rate * 100, 1),
                        "n": n,
                        "distance": entry.get("distance"),
                        "basis": basis,
                    }
                    if setup and layer == "micro":
                        tf = "1m" if fam.startswith("1m") else "5m"
                        fam_key = fam.replace(" E", " EMA")
                        skey = "%s_%s" % (setup["type"], "strict" if setup["timing"] == "strict" else "relaxed")
                        scell = (self._store.get("micro", {}).get("setup_ab", {}).get(skey) or {}).get(fam_key)
                        if scell and scell.get("n"):
                            item["setup_%s" % setup["type"]] = "%d/%d" % (scell["hits"], scell["n"])
                    out.append(item)
                    if len(out) >= max_n:
                        break
                return out

            res_levels = build_side(resistance, TOP_N)
            sup_levels = build_side(support, TOP_N)

            setup_call = self._detector.on_snapshot(
                rv, price, self._daily_ctx, setup, now_ist, day=self._day)

            forecast = self._assemble(rv, regime_key, price, res_levels, sup_levels,
                                      setup, now_ist, setup_call)
            if log:
                self._log(now_ist, data, forecast)
            return forecast

    def _assemble(self, rv, regime_key, price, res_levels, sup_levels, setup, now_ist,
                  setup_call=None):
        daily = self._store.get("daily", {}) if self._store else {}
        d_key = "D+" if (rv["d_sign"] or 0) > 0 else "D-"
        d_stats = daily.get(d_key) or {}
        median_move = d_stats.get("median_move_pct") or 0.0

        known = rv["pvt_known"]
        bear = rv["pvt_alignment"]
        bull = known - bear
        # bias in % units: daily median move prior + 0.08% per net bullish TF
        bias = median_move + 0.08 * (bull - bear)
        if bias > 0.08:
            label = "bullish"
        elif bias < -0.08:
            label = "bearish"
        else:
            label = "neutral"

        parts = regime_key.split("|") if regime_key else ("?", "?", "?")
        shape = _EXPECTED_SHAPE.get((parts[0][-1], parts[1][-1], parts[2][-1])) if regime_key else None
        if not shape:
            shape = "Regime incomplete (waiting for D/2H/30 PVT signs)."

        if label == "bearish" and res_levels:
            inv = "30m close above %s (%s) negates the bearish bias" % (
                res_levels[0]["level"], res_levels[0]["type"])
        elif label == "bullish" and sup_levels:
            inv = "30m close below %s (%s) negates the bullish bias" % (
                sup_levels[0]["level"], sup_levels[0]["type"])
        elif res_levels and sup_levels:
            inv = "Range: below %s favors %s, above %s favors %s" % (
                sup_levels[0]["level"], res_levels[0]["type"],
                res_levels[0]["level"], sup_levels[0]["type"])
        else:
            inv = None

        notes = []
        if rv["d_source"] == "csv_fallback":
            notes.append("D regime from previous-day D-PVT (DASHBOARD DPMPVT unparsed)")
        poise = (states_poise(rv)) 
        if poise is not None:
            notes.append("PVTPoiseFlag=%s (logged, model pending)" % poise)

        return {
            "ts": now_ist.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "horizon": "rest_of_day",
            "price": round(price, 2),
            "regime": regime_key,
            "d_source": rv["d_source"],
            "bias_pct": round(bias, 3),
            "bias_label": label,
            "confluence": {"n_bear_30m": rv["n_bear_30m"], "pvt_alignment": rv["pvt_alignment"]},
            "expected_shape": shape,
            "resistance": res_levels,
            "support": sup_levels,
            "invalidation": inv,
            "setup": setup,
            "setup_call": setup_call,
            "notes": notes,
        }

    # -- logging -----------------------------------------------------------

    def _log(self, now_ist, snapshot, forecast):
        try:
            if self._frozen_day or not self._forecast_dir:
                return
            os.makedirs(self._forecast_dir, exist_ok=True)
            path = os.path.join(self._forecast_dir, "%s.jsonl" % self._day)
            rec = {"ts": now_ist.isoformat(), "snapshot": snapshot, "forecast": forecast}
            with open(path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:
            print("[forecasting] jsonl logging failed: %s" % e)


def _sgn(x):
    if x is None:
        return None
    x = _f(x)
    if x is None or x == 0:
        return None
    return 1 if x > 0 else -1


def states_poise(rv):
    m30 = rv["states"].get("30M") or {}
    return m30.get("poise")


class _LazyEngine(object):
    """Lazy singleton proxy: engine initializes on first attribute use."""

    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()

    def _get(self):
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    self._engine = ForecastEngine()
        return self._engine

    def __getattr__(self, name):
        return getattr(self._get(), name)


forecast_engine = _LazyEngine()
