"""1PM Momentors & Levels Ladder widget — pure builders over the live
TradingView webhook snapshot (READ-ONLY; no writes, no order-path touchpoints).

Sources (both already in memory on the trader object, no new state created):
  - snapshot = trader._tradingView_Data  -> INDICATORS / SETUPS blocks
  - levels   = trader._tv_levels         -> LEVELS.CW_CD rows [Name, Price]

Every INDICATORS TF block carries (tv/TV_Algo_JSON/Strategy_TradingInvestAlgo_Data):
  PVT + PVTTrendFlag + PVTCrossOverIndex/UnderIndex (barssince+1),
  MACD_1226/Signal_1226 + X_1226/Y_1226, MACD_2452/Signal_2452 + X_2452/Y_2452,
  K/D + KTrendFlag + KCrossoverIndex/KCrossUnderIndex,
  EMA9..EMA200, VWAP, VOC + VOCTrendFlag + VOCCrossOverIndex/UnderIndex.
D and W blocks are commented out in the Pine payload -> D rows are STATE-ONLY
(from SETUPS.DASHBOARD); W/M/Q PM states (WPM/MPM/QPM2 chips) are not
available live yet.

Momentor representation (user spec, 2026-11-01):
  fresh cross (age 1-4)  -> uppercase X form, dark green/red, count shown
  aged out (age > 4)     -> lowercase state form, light green/red, no count
VOC state rows below zero (1o/5o) render grey (no downtrend mirror).
"""

# ---------------------------------------------------------------- helpers

def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    x = _f(v)
    return int(x) if x is not None else None


def _sign(v):
    x = _f(v)
    if x is None:
        return None
    return 1 if x > 0 else -1


_LIGHT = {"up": "light-green", "down": "light-red", "flat": "grey"}
_DARK = {"up": "dark-green", "down": "dark-red", "flat": "grey"}


def _cross_cell(block, trend_key, over_key, under_key, allow_down=True):
    """One momentor slot: dark X-form while the cross in the current
    direction is fresh (1-4), light state form once aged out."""
    if not isinstance(block, dict):
        return {"label": "-", "count": None, "color": "grey"}
    trend = _sign(block.get(trend_key))
    over = _i(block.get(over_key))
    under = _i(block.get(under_key)) if allow_down else None
    age = None
    if trend == 1 and over is not None:
        age = over
    elif trend == -1 and under is not None:
        age = under
    direction = "up" if trend == 1 else ("down" if trend == -1 else "flat")
    if trend is None:
        return {"label": "-", "count": None, "color": "grey"}
    if age is not None and 1 <= age <= 4:
        return {"label": "X", "count": age, "color": _DARK[direction], "dir": direction}
    # aged out / persistent state
    if not allow_down and trend < 0:
        return {"label": "o", "count": None, "color": "grey", "dir": direction}
    return {"label": "o", "count": None, "color": _LIGHT[direction], "dir": direction}


def _pm_up(block):
    """PM state (PVT + MACD2 24/52 both aligned up) for a TF block."""
    if not isinstance(block, dict):
        return None
    p = _sign(block.get("PVTTrendFlag"))
    m = _sign(block.get("Trend_2452"))
    if p is None or m is None:
        return None
    return p == 1 and m == 1


def _1pme50_cell(ind, price):
    b = ind.get("1M") or {}
    pm_up = _pm_up(b)
    e50 = _f(b.get("EMA50"))
    above = None
    if price is not None and e50 is not None:
        above = price >= e50
    if pm_up and above:
        return {"value": "UP", "color": "dark-green"}
    if pm_up is False or above is False:
        return {"value": "DN", "color": "dark-red"}
    return {"value": "-", "color": "grey"}


def _5sxtoz_cell(ind):
    """5m Stoch cross + the zone it is crossing/trending FROM.
    OSZ (K<25) favorable, MZ good, OBZ (K>75) unfavorable. No volume."""
    b = ind.get("5M") or {}
    if not isinstance(b, dict):
        return {"value": "-", "color": "grey"}
    k = _f(b.get("K"))
    over = _i(b.get("KCrossoverIndex"))
    under = _i(b.get("KCrossUnderIndex"))
    zone = None
    if k is not None:
        zone = "OSZ" if k < 25 else ("OBZ" if k > 75 else "MZ")
    if zone is None:
        return {"value": "-", "color": "grey"}
    x_up = over is not None and (under is None or over <= under)
    if zone == "OBZ":
        color = "dark-red"
    elif zone == "OSZ":
        color = "dark-green"
    else:
        color = "amber"
    arrow = "\u2191" if x_up else "\u2193"
    return {"value": "%s %s" % (zone, arrow), "color": color,
            "zone": zone, "cross": "up" if x_up else "down"}


# ------------------------------------------------------------- momentors

def build_momentors(snapshot, price=None):
    """Rows for the Momentors table, in the user's sheet order."""
    ind = ((snapshot or {}).get("INDICATORS")) or {}

    def tf(key):
        b = ind.get(key)
        return b if isinstance(b, dict) else {}

    rows = [
        dict(_5sxtoz_cell(ind), name="5S"),
        dict(_1pme50_cell(ind, price), name="1E50"),
    ]
    specs = [
        # slot, block-key, cross label, state label, trend/over/under keys, allow_down
        ("5P", "5M", "5px", "5p",
         "PVTTrendFlag", "PVTCrossOverIndex", "PVTCrossUnderIndex", True),
        ("1O", "1M", "1ox", "1o",
         "VOCTrendFlag", "VOCCrossOverIndex", "VOCCrossUnderIndex", False),
        ("5O", "5M", "5ox", "5o",
         "VOCTrendFlag", "VOCCrossOverIndex", "VOCCrossUnderIndex", False),
        ("3P", "30M", "3PX", "3p",
         "PVTTrendFlag", "PVTCrossOverIndex", "PVTCrossUnderIndex", True),
        ("3M1", "30M", "3M1X", "3M1",
         "Trend_1226", "X_1226", "Y_1226", True),
        ("3M2", "30M", "3M2X", "3M2",
         "Trend_2452", "X_2452", "Y_2452", True),
        ("3O", "30M", "3OX", "3o",
         "VOCTrendFlag", "VOCCrossOverIndex", "VOCCrossUnderIndex", True),
        ("2P", "2H", "2PX", "2p",
         "PVTTrendFlag", "PVTCrossOverIndex", "PVTCrossUnderIndex", True),
        ("2M1", "2H", "2M1X", "2M1",
         "Trend_1226", "X_1226", "Y_1226", True),
        ("2M2", "2H", "2M2X", "2M2",
         "Trend_2452", "X_2452", "Y_2452", True),
        ("2O", "2H", "2OX", "2o",
         "VOCTrendFlag", "VOCCrossOverIndex", "VOCCrossUnderIndex", True),
    ]
    for _slot, tfkey, xlabel, slabel, tkey, okey, ukey, allow_dn in specs:
        cell = _cross_cell(tf(tfkey), tkey, okey, ukey, allow_dn)
        if cell["label"] != "X":
            continue  # v1.1 UX: show fresh crosses only - aged-out states hidden
        rows.append({"name": xlabel, "label": xlabel, "count": cell.get("count"),
                     "color": cell["color"], "dir": cell.get("dir")})

    # Daily rows: the Pine payload's D INDICATORS block is commented out, so
    # D cross ages are unavailable — under the crosses-only rule nothing from
    # the D layer renders (removed 2026-11-01: the pinned Do row confused the
    # display; it returns once the Pine D/W blocks are enabled).
    return rows


# ----------------------------------------------------------- levels ladder

# higher-TF PM state -> the lower-TF EMA rungs it endorses as support
# (uptrend) / resistance (downtrend). User rule: 3PM up -> 1m EMA100/200
# support; 2PM up -> 5m EMA50/100/200 support; mirrors for downtrend.
CHIP_MAP = [
    (("1m EMA100", "1m EMA200"), "3PM2", ("30M",)),
    (("5m EMA50", "5m EMA100", "5m EMA200"), "2PM", ("2H",)),
]
# WPM -> 2h EMA200, MPM -> D EMA100/200, QPM2 -> W EMA100: pending payload
# extension (D/W INDICATORS blocks are commented out in the Pine JSON).

MAX_HEADROOM_PTS = 150


def build_ladder(snapshot, levels, price=None):
    """Levels Ladder rows within +/-150 pts of the close, sorted desc,
    the Close row highlighted. Chip = the endorsing higher-TF PM state,
    green when it uptrends (rung = support), red when it downtrends."""
    ind = ((snapshot or {}).get("INDICATORS")) or {}
    rows = (levels or {}).get("CW_CD") or []
    close = None
    parsed = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 2:
            continue
        name = str(r[0])
        px = _f(r[1])
        if px is None:
            continue
        if name == "Close":
            close = px
        parsed.append({"name": name, "price": px})
    if price is None:
        price = close

    # resolve endorsing PM states once per chip
    chip_states = {}
    for _names, chip, tfs in CHIP_MAP:
        vals = [_pm_up(ind.get(tf)) for tf in tfs]
        known = [v for v in vals if v is not None]
        chip_states[chip] = (all(known) and known[0]) if known else None

    out = []
    if price is not None:
        for row in parsed:
            if row["name"] == "Close":
                out.append({"name": "Close", "price": row["price"], "pts": 0,
                            "close": True, "chip": None, "chip_color": None})
                continue
            dist = abs(row["price"] - price)
            if dist > MAX_HEADROOM_PTS:
                continue
            chip = chip_for(row["name"])
            chip_color = None
            if chip is not None:
                st = chip_states[chip]
                if st is True:
                    chip_color = "dark-green"    # support in an uptrend
                elif st is False:
                    chip_color = "dark-red"      # resistance in a downtrend
            out.append({"name": row["name"], "price": row["price"],
                        "pts": round(dist, 1), "close": False,
                        "chip": chip, "chip_color": chip_color})
        out.sort(key=lambda r: (r["price"], 0 if r["close"] else 1), reverse=True)
    return out


def chip_for(level_name):
    for names, chip, _tfs in CHIP_MAP:
        if level_name in names:
            return chip
    return None
