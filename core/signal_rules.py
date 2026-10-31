"""
Ultra-Scalping entry/exit criteria evaluator.

Evaluates configurable conditions (appconfig group "ultra_scalping")
against the ATM option indicator data delivered by the Pine script's
MSG:"OPT" webhook (CE/PE blocks, 5S/15S/1M/5M timeframes).

All conditions read their settings live via fetch_from_json - the JSON
cache is cleared on every App Config save, so changes apply without a
restart.
"""

from loguru import logger

from core.utils import fetch_from_json

# Timeframe keys as they appear in the OPT payload blocks
_VALID_TFS = ("5S", "15S", "1M", "5M")


def _cfg(key, default="OFF"):
    try:
        val = fetch_from_json("appconfig.json", key)
    except Exception:
        val = None
    return str(val).strip() if val is not None else default


def _truthy(val):
    return str(val).strip().lower() in ("true", "1", "yes", "on")


def entry_check_mode():
    """US_ENTRY_CHECK: "WARN" (toast, allow) or "STOP" (block order)."""
    mode = _cfg("US_ENTRY_CHECK", "WARN").upper()
    return mode if mode in ("WARN", "STOP") else "WARN"


def _flag(block, key):
    """Read a ±1 int flag from a TF block, None when missing/NaN."""
    try:
        return int(float(block.get(key)))
    except (TypeError, ValueError, AttributeError):
        return None


def _num(block, key):
    try:
        return float(block.get(key))
    except (TypeError, ValueError, AttributeError):
        return None


def _trend_up(block):
    return _flag(block, "Trend_2452") == 1


def _pvt_and_macd_up(block):
    return _flag(block, "PVTTrendFlag") == 1 and _flag(block, "Trend_2452") == 1


def evaluate_ultra_scalping_entry(option_data, ltp=None):
    """
    Evaluate the configured entry criteria for a CE (CALL) order against
    the ATM option indicator data.

    Returns (ok: bool, failed: [str], states: [str]) - states carries the
    per-condition outcome for logging / toasts.
    """
    if not _truthy(_cfg("US_ENTRY_ENABLED")):
        return True, [], ["entry check disabled"]

    ce = (option_data or {}).get("CE") or {}
    failed, states = [], []

    # --- 1m PM uptrend ------------------------------------------------
    mode = _cfg("US_ENTRY_1PM", "TREND_2452_UP")
    if mode != "OFF":
        block = ce.get("1M") or {}
        passed = _pvt_and_macd_up(block) if mode == "PVT_AND_MACD" else _trend_up(block)
        states.append(f"1PM {mode}: {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append("1m PM not uptrending")

    # --- 15s PVT -------------------------------------------------------
    mode = _cfg("US_ENTRY_15S_PVT", "POISE_UP")
    if mode != "OFF":
        block = ce.get("15S") or {}
        if mode == "ABOVE_ZERO":
            passed = _flag(block, "PVTTrendFlag") == 1
        else:  # POISE_UP
            passed = _flag(block, "PVTPoiseFlag") == 1
        states.append(f"15sPVT {mode}: {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append("15s PVT not uptrending")

    # --- price above TF:EMA -------------------------------------------
    mode = _cfg("US_ENTRY_PRICE_ABOVE", "15S:EMA9")
    if mode and mode.upper() != "OFF" and ":" in mode:
        tf, ema = mode.split(":", 1)
        tf, ema = tf.strip().upper(), ema.strip().upper()
        block = ce.get(tf) or {}
        ema_val, ltp_f = _num(block, ema), _num({"ltp": ltp}, "ltp")
        passed = ema_val is not None and ltp_f is not None and ltp_f > ema_val
        states.append(f"LTP>{tf}:{ema} ({ltp_f} vs {ema_val}): {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append(f"price not above {tf} {ema}")

    # --- 5s PM uptrend --------------------------------------------------
    mode = _cfg("US_ENTRY_5S_PM", "TREND_2452_UP")
    if mode != "OFF":
        block = ce.get("5S") or {}
        passed = _pvt_and_macd_up(block) if mode == "PVT_AND_MACD" else _trend_up(block)
        states.append(f"5sPM {mode}: {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append("5s PM not uptrending")

    # --- VOC above zero (any-of across configured TFs) ------------------
    mode = _cfg("US_ENTRY_VOC", "5S|15S")
    if mode and mode.upper() != "OFF":
        tfs = [t.strip().upper() for t in mode.split("|") if t.strip()]
        passed, per_tf = False, []
        for tf in tfs:
            if tf not in _VALID_TFS:
                continue
            voc = _num(ce.get(tf) or {}, "VOC")
            per_tf.append(f"{tf}={voc}")
            if voc is not None and voc > 0:
                passed = True
        states.append(f"VOC>0 [{mode}] ({', '.join(per_tf) or 'n/a'}): {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append(f"VOC not above zero ({mode})")

    return (not failed), failed, states


def evaluate_ultra_scalping_exit(option_data):
    """
    Indicator-driven exit: currently the 5s PM cross-down
    (5S MACD 24/52 trend flag flips to -1).

    Returns (should_exit: bool, reason: str).
    """
    mode = _cfg("US_EXIT_5S_PM", "TREND_2452_DOWN")
    if mode == "OFF":
        return False, ""

    ce = (option_data or {}).get("CE") or {}
    block = ce.get("5S") or {}
    if _flag(block, "Trend_2452") == -1:
        reason = "5s PM crossed down (5S MACD 24/52 bearish)"
        logger.info(f"US EXIT trigger: {reason}")
        return True, reason
    return False, ""
