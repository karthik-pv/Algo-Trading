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


def entry_check_mode(key):
    """Entry-check action for a strategy: "WARN" (toast, allow) or "STOP" (block order)."""
    mode = _cfg(key, "WARN").upper()
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


def evaluate_ultra_scalping_entry(option_data, ltp=None, side="CE"):
    """
    Evaluate the configured entry criteria for a CE/PE order against the
    traded contract's own indicator data (the CE or PE block of the OPT
    payload).

    Returns (ok: bool, failed: [str], states: [str]) - states carries the
    per-condition outcome for logging / toasts.
    """
    if not _truthy(_cfg("US_ENTRY_ENABLED")):
        return True, [], ["entry check disabled"]

    side_data = (option_data or {}).get((side or "CE").upper()) or {}
    failed, states = [], []

    # --- 1m PM uptrend ------------------------------------------------
    mode = _cfg("US_ENTRY_1PM", "TREND_2452_UP")
    if mode != "OFF":
        block = side_data.get("1M") or {}
        passed = _pvt_and_macd_up(block) if mode == "PVT_AND_MACD" else _trend_up(block)
        states.append(f"{side} 1PM {mode}: {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append(f"{side} 1m PM not uptrending")

    # --- 15s PVT -------------------------------------------------------
    mode = _cfg("US_ENTRY_15S_PVT", "POISE_UP")
    if mode != "OFF":
        block = side_data.get("15S") or {}
        if mode == "ABOVE_ZERO":
            passed = _flag(block, "PVTTrendFlag") == 1
        else:  # POISE_UP
            passed = _flag(block, "PVTPoiseFlag") == 1
        states.append(f"{side} 15sPVT {mode}: {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append(f"{side} 15s PVT not uptrending")

    # --- price above TF:EMA -------------------------------------------
    mode = _cfg("US_ENTRY_PRICE_ABOVE", "15S:EMA9")
    if mode and mode.upper() != "OFF" and ":" in mode:
        tf, ema = mode.split(":", 1)
        tf, ema = tf.strip().upper(), ema.strip().upper()
        block = side_data.get(tf) or {}
        ema_val, ltp_f = _num(block, ema), _num({"ltp": ltp}, "ltp")
        passed = ema_val is not None and ltp_f is not None and ltp_f > ema_val
        states.append(f"{side} LTP>{tf}:{ema} ({ltp_f} vs {ema_val}): {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append(f"price not above {side} {tf} {ema}")

    # --- 5s PM uptrend --------------------------------------------------
    mode = _cfg("US_ENTRY_5S_PM", "TREND_2452_UP")
    if mode != "OFF":
        block = side_data.get("5S") or {}
        passed = _pvt_and_macd_up(block) if mode == "PVT_AND_MACD" else _trend_up(block)
        states.append(f"{side} 5sPM {mode}: {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append(f"{side} 5s PM not uptrending")

    # --- VOC above zero (any-of across configured TFs) ------------------
    mode = _cfg("US_ENTRY_VOC", "5S|15S")
    if mode and mode.upper() != "OFF":
        tfs = [t.strip().upper() for t in mode.split("|") if t.strip()]
        passed, per_tf = False, []
        for tf in tfs:
            if tf not in _VALID_TFS:
                continue
            voc = _num(side_data.get(tf) or {}, "VOC")
            per_tf.append(f"{tf}={voc}")
            if voc is not None and voc > 0:
                passed = True
        states.append(f"{side} VOC>0 [{mode}] ({', '.join(per_tf) or 'n/a'}): {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append(f"{side} VOC not above zero ({mode})")

    return (not failed), failed, states


def evaluate_ultra_scalping_exit(option_data, side="CE"):
    """
    Indicator-driven exit: currently the 5s PM cross-down
    (5S MACD 24/52 trend flag flips to -1) on the traded contract's
    own data.

    Returns (should_exit: bool, reason: str).
    """
    mode = _cfg("US_EXIT_5S_PM", "TREND_2452_DOWN")
    if mode == "OFF":
        return False, ""

    block = _side_block(option_data, side, "5S")
    if _flag(block, "Trend_2452") == -1:
        reason = f"5s PM crossed down ({side} 5S MACD 24/52 bearish)"
        logger.info(f"US EXIT trigger: {reason}")
        return True, reason
    return False, ""


# ---------------------------------------------------------------------------
# INTRA Trading setup (IN_ENTRY_* / IN_EXIT_*)
# ---------------------------------------------------------------------------
# Both setups evaluate on the traded option contract's own indicator data
# (MSG:"OPT"): the CE block for CE orders, the PE block for PE orders.
# The NIFTY1 underlying feed is never used for these criteria.

def _side_block(option_data, side, tf):
    """Return the tf indicator block for the traded side (CE/PE)."""
    side = (side or "CE").upper()
    side_data = (option_data or {}).get(side) or {}
    return side_data.get(tf) or {}


def evaluate_intra_entry(option_data, ltp=None, side="CE"):
    """
    Entry criteria for INTRA CE/PE orders, evaluated on the traded
    contract's own indicator data.

    Configured conditions:
      - IN_ENTRY_5M_PVT: 5m PVT > 0 (ABOVE_ZERO) or OFF.

    Returns (ok: bool, failed: [str], states: [str]).
    """
    if not _truthy(_cfg("IN_ENTRY_ENABLED")):
        return True, [], ["entry check disabled"]

    failed, states = [], []

    mode = _cfg("IN_ENTRY_5M_PVT", "ABOVE_ZERO")
    if mode != "OFF":
        block = _side_block(option_data, side, "5M")
        pvt = _num(block, "PVT")
        passed = pvt is not None and pvt > 0
        states.append(f"{side} 5mPVT>0 ({pvt}): {'pass' if passed else 'FAIL'}")
        if not passed:
            failed.append(f"{side} 5m PVT not above zero")

    return (not failed), failed, states


def evaluate_intra_exit(option_data, side="CE"):
    """
    Indicator-driven exit for INTRA: 1m MACD2 (24/52) cross-down
    (1M Trend_2452 flips to -1) on the traded contract's own data.

    Returns (should_exit: bool, reason: str).
    """
    mode = _cfg("IN_EXIT_1M_MACD2", "TREND_2452_DOWN")
    if mode == "OFF":
        return False, ""

    block = _side_block(option_data, side, "1M")
    if _flag(block, "Trend_2452") == -1:
        reason = f"1m MACD2 crossed down ({side} 1M MACD 24/52 bearish)"
        logger.info(f"INTRA EXIT trigger: {reason}")
        return True, reason
    return False, ""
