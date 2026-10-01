"""Regime detection: snapshot INDICATORS/SETUPS -> regime vector.

Regime key for the store tables: D{+/-}|2H{+/-}|30{+/-}
  - D   : daily regime from SETUPS.DASHBOARD DPMPVT (fallback: previous-day
          D-PVT sign from the D-CSV)
  - 2H  : INDICATORS["2H"]["PVTTrendFlag"]
  - 30  : INDICATORS["30M"]["PVTTrendFlag"]

Locked field semantics (HANDOFF 3): PVTTrendFlag = +1 if PVT>0 else -1.
MACD2 = MACD_2452/Signal_2452 (the 12/26 pair is discarded). Stoch = K vs D.
"""

import logging

logger = logging.getLogger(__name__)

_TF_KEYS = {"5S": "5S", "15S": "15S", "1M": "1M", "5M": "5M", "30M": "30M", "2H": "2H"}


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def sign_from_flag(v):
    """PVTTrendFlag -> +1/-1 (0 treated as None/unknown)."""
    x = _f(v)
    if x is None or x == 0:
        return None
    return 1 if x > 0 else -1


def parse_side_str(s):
    """Parse a dashboard trend string ('Bull'/'Bear'/'+1'/'-1'/'PVT+'...) -> +-1."""
    if s is None:
        return None
    t = str(s).strip().lower()
    if not t:
        return None
    if t in ("+1", "1", "+", "pos", "positive", "bull", "bullish", "up", "green", "pvt+"):
        return 1
    if t in ("-1", "-", "neg", "negative", "bear", "bearish", "down", "red", "pvt-"):
        return -1
    if "bull" in t or "positive" in t or "up" in t:
        return 1
    if "bear" in t or "negative" in t or "down" in t:
        return -1
    return None


def tf_state(block):
    """Per-TF indicator state from one INDICATORS block (all values optional)."""
    if not isinstance(block, dict):
        return {}
    pvt = _f(block.get("PVT"))
    macd2 = _f(block.get("MACD_2452"))
    sig2 = _f(block.get("Signal_2452"))
    trend2452 = sign_from_flag(block.get("Trend_2452"))
    k = _f(block.get("K"))
    d = _f(block.get("D"))
    ktrend = sign_from_flag(block.get("KTrendFlag"))
    state = {
        "pvt": pvt,
        "pvt_sign": sign_from_flag(block.get("PVTTrendFlag")),
        "poise": _f(block.get("PVTPoiseFlag")),
        "macd2_bear": None,
        "stoch_bear": None,
        "vwap": _f(block.get("VWAP")),
        "ema9": _f(block.get("EMA9")),
        "ema21": _f(block.get("EMA21")),
        "ema50": _f(block.get("EMA50")),
        "ema100": _f(block.get("EMA100")),
        "ema200": _f(block.get("EMA200")),
        "raw": block,
    }
    if macd2 is not None and sig2 is not None:
        state["macd2_bear"] = macd2 < sig2
    elif trend2452 is not None:
        state["macd2_bear"] = trend2452 < 0
    if k is not None and d is not None:
        state["stoch_bear"] = k < d
    elif ktrend is not None:
        state["stoch_bear"] = ktrend < 0
    return state


def parse_indicators(data):
    """INDICATORS section -> {tf: state}."""
    ind = (data or {}).get("INDICATORS") or {}
    return {tf: tf_state(ind.get(tf)) for tf in _TF_KEYS}


def bearish_factor_count(state_30m):
    """Confluence bearish factors on 30m: [PVT<0, MACD2<Signal, K<D] -> 0..3."""
    if not state_30m:
        return None
    n = 0
    known = 0
    if state_30m.get("pvt_sign") is not None:
        known += 1
        if state_30m["pvt_sign"] < 0:
            n += 1
    if state_30m.get("macd2_bear") is not None:
        known += 1
        if state_30m["macd2_bear"]:
            n += 1
    if state_30m.get("stoch_bear") is not None:
        known += 1
        if state_30m["stoch_bear"]:
            n += 1
    if known == 0:
        return None
    return n


def regime_vector(data, daily_prev_sign=None):
    """Full regime vector from a webhook snapshot.

    Returns dict with:
      key      : 'D+|2H+|30-' style key (or None where unknown)
      d_sign, h2_sign, m30_sign
      d_source : 'dashboard' | 'csv_fallback' | 'none'
      states   : per-TF states
      n_bear_30m : confluence factor count (0..3)
      pvt_alignment : count of bearish PVT TFs among {30m, 2H, D-prev}
    """
    states = parse_indicators(data)
    dash = ((data or {}).get("SETUPS") or {}).get("DASHBOARD") or {}

    d_sign = parse_side_str(dash.get("DPMPVT"))
    d_source = "dashboard"
    if d_sign is None:
        d_sign = parse_side_str(dash.get("DPM"))
        d_source = "dashboard_dpm"
    if d_sign is None and daily_prev_sign is not None:
        d_sign = daily_prev_sign
        d_source = "csv_fallback"

    h2_sign = states["2H"].get("pvt_sign")
    m30_sign = states["30M"].get("pvt_sign")

    key = None
    if d_sign and h2_sign and m30_sign:
        key = "D%s|2H%s|30%s" % (
            "+" if d_sign > 0 else "-",
            "+" if h2_sign > 0 else "-",
            "+" if m30_sign > 0 else "-",
        )

    pvt_alignment = 0
    known = 0
    for s in (m30_sign, h2_sign, d_sign):
        if s is not None:
            known += 1
            if s < 0:
                pvt_alignment += 1

    return {
        "key": key,
        "d_sign": d_sign,
        "d_source": d_source,
        "h2_sign": h2_sign,
        "m30_sign": m30_sign,
        "states": states,
        "n_bear_30m": bearish_factor_count(states.get("30M")),
        "pvt_alignment": pvt_alignment,
        "pvt_known": known,
        "dashboard_raw": {k: dash.get(k) for k in ("DPM", "DPMPVT", "2PM", "3PM", "WS", "DS", "2S", "DSPVT", "3PMPVT", "2PMPVT")},
    }
