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
sup/res top row (user rule change 2026-11-04): shows the level the current
1m-PVT leg ORIGINATED from (launch rung at the leg's swing extreme), not
the highest level below price - needs the server's rolling price memory.
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


def _cross_cell(block, trend_key, over_key, under_key, allow_down=True, max_age=4):
    """One momentor slot: dark X-form while the cross in the current
    direction is fresh (1-max_age), light state form once aged out.
    trend_key=None derives direction purely from which cross index is
    more recent (used for the VWAP-cross CVX rows, which have no flag)."""
    if not isinstance(block, dict):
        return {"label": "-", "count": None, "color": "grey"}
    if trend_key is None:
        over, under = _i(block.get(over_key)), _i(block.get(under_key))
        if over is None and under is None:
            return {"label": "-", "count": None, "color": "grey"}
        trend = 1 if (under is None or (over is not None and over <= under)) else -1
        age = over if trend == 1 else under
        direction = "up" if trend == 1 else "down"
        if 1 <= age <= max_age:
            return {"label": "X", "count": age, "color": _DARK[direction], "dir": direction}
        return {"label": "o", "count": None, "color": _LIGHT[direction], "dir": direction}
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
    if age is not None and 1 <= age <= max_age:
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
    Colour = zone x swing direction (user rule 2026-11-01):
      upswing   - OSZ dark-green (room to go up), MZ light-green, OBZ red
      downswing - OBZ dark-red (room to go down), MZ light-red,  OSZ red
    (OSZ = K<25 oversold, OBZ = K>75 overbought, MZ = mid zone). No volume."""
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
    # colour = zone x swing direction (see docstring)
    if x_up:
        color = ("dark-green" if zone == "OSZ" else
                 "light-green" if zone == "MZ" else "red")
    else:
        color = ("dark-red" if zone == "OBZ" else
                 "light-red" if zone == "MZ" else "red")
    arrow = "\u2191" if x_up else "\u2193"
    # bg=True: the renderer paints the colour as the CELL BACKGROUND with a
    # fixed text colour (user: "give background, keep text colour same")
    return {"value": "%s %s" % (zone, arrow), "color": color, "bg": True,
            "zone": zone, "cross": "up" if x_up else "down"}


# ------------------------------------------------------------- level row

import time


def _short_level(name):
    """Ladder row name -> compact token: '1m EMA100' -> '1E100',
    '5m EMA50' -> '5E50', '2h EMA200' -> '2E200', 'D EMA100' -> 'DE100';
    non-EMA levels keep their name (Cam R3, RN + ...)."""
    try:
        tf_tok, rest = name.split(" ", 1)
    except ValueError:
        return name
    tf_char = {"1m": "1", "5m": "5", "30m": "3", "2h": "2"}.get(tf_tok, tf_tok)
    if rest.startswith("EMA"):
        return "%sE%s" % (tf_char, rest[3:])
    return name


def _leg_extreme(price_history, age_bars, low=True):
    """Swing extreme of the current 1m-PVT leg: min price (low=True) or max
    price over the last `age_bars` minutes of the rolling price memory.
    Returns None when no usable history exists (fresh start / leg older
    than the memory span - the caller then falls back)."""
    if not price_history or not age_bars or age_bars <= 0:
        return None
    if age_bars > 180:  # memory spans ~3h; an older leg is out of reach
        return None
    cutoff = time.time() - age_bars * 60 - 15  # 1m bar + one sample slack
    pts = [p for ts, p in price_history if ts >= cutoff]
    if len(pts) < 2:  # too thin to trust as a swing extreme
        return None
    return min(pts) if low else max(pts)


def _support_resistance_row(levels, price, rising, leg_extreme=None):
    """Top momentor row: the level the rise/fall ORIGINATED from (user rule
    change 2026-11-04, replacing the old 'highest level below price' read,
    which kept advancing to every newly reclaimed rung - a rise from 5E100
    that later reclaimed CAM R3 used to flip the row to CAM R3).
    Rising: the rung closest ABOVE the current 1m-PVT leg's swing low (the
    launch support). Falling (mirror): the rung closest BELOW the leg's
    swing high (the launch resistance). No leg extreme available (fresh
    start, leg older than the price memory) -> falls back to the old rule:
    highest level below price rising, highest above price falling."""
    if price is None:
        return {"name": "-", "value": "-", "color": "grey"}
    rows = []
    for r in ((levels or {}).get("CW_CD") or []):
        if isinstance(r, (list, tuple)) and len(r) >= 2 and str(r[0]) != "Close":
            try:
                rows.append((str(r[0]), float(r[1])))
            except (TypeError, ValueError):
                continue
    if rising:
        if leg_extreme is not None:
            cands = [(n, p) for n, p in rows if p >= leg_extreme]
            if cands:
                n, p = min(cands, key=lambda x: x[1])
                return {"name": "sup", "value": "%s \u2191" % _short_level(n),
                        "color": "dark-green", "lvl": p}
        cands = [(n, p) for n, p in rows if p < price]
        if cands:
            n, p = max(cands, key=lambda x: x[1])
            return {"name": "sup", "value": "%s \u2191" % _short_level(n),
                    "color": "dark-green", "lvl": p}
        return {"name": "sup", "value": "-", "color": "grey"}
    if rising is False:
        if leg_extreme is not None:
            cands = [(n, p) for n, p in rows if p <= leg_extreme]
            if cands:
                n, p = max(cands, key=lambda x: x[1])
                return {"name": "res", "value": "%s \u2193" % _short_level(n),
                        "color": "dark-red", "lvl": p}
        cands = [(n, p) for n, p in rows if p > price]
        if cands:
            n, p = max(cands, key=lambda x: x[1])
            return {"name": "res", "value": "%s \u2193" % _short_level(n),
                    "color": "dark-red", "lvl": p}
        return {"name": "res", "value": "-", "color": "grey"}
    return {"name": "lvl", "value": "-", "color": "grey"}


# ------------------------------------------------------------- momentors

def build_momentors(snapshot, price=None, levels=None, price_history=None):
    """Rows for the Momentors table, in the user's sheet order.
    First column is ALL LOWERCASE (user rule). Row order:
    sup/res level row, 5s (5m Stoch zone), 1e50, then fresh crosses.
    price_history: rolling (ts, price) samples kept by the server - used to
    anchor the sup/res row on the 1m-PVT leg's launch rung (user rule
    2026-11-04); falls back to the old nearest-level read without it."""
    ind = ((snapshot or {}).get("INDICATORS")) or {}

    def tf(key):
        b = ind.get(key)
        return b if isinstance(b, dict) else {}

    e50 = _1pme50_cell(ind, price)
    rising = True if e50.get("value") == "UP" else \
        (False if e50.get("value") == "DN" else None)

    # 1m-PVT leg anchor for the sup/res row: the cross age (carried,
    # no-repaint) says when the current leg started; the rolling price
    # memory supplies the leg's swing extreme within that window.
    leg_extreme = None
    if rising is not None:
        b1 = tf("1M")
        leg_age = _i(b1.get("PVTCrossOverIndex") if rising
                     else b1.get("PVTCrossUnderIndex"))
        leg_extreme = _leg_extreme(price_history, leg_age, low=rising)

    rows = [
        dict(_support_resistance_row(levels, price, rising, leg_extreme),
             name="sup"
             if rising else ("res" if rising is False else "lvl")),
        dict(_5sxtoz_cell(ind), name="5s"),
        dict(e50, name="1e50"),
    ]
    specs = [
        # slot, block-key, cross label, state label, trend/over/under keys, allow_down
        ("5P", "5M", "5px", "5p",
         "PVTTrendFlag", "PVTCrossOverIndex", "PVTCrossUnderIndex", True),
        ("1O", "1M", "1ox", "1o",
         "VOCTrendFlag", "VOCCrossOverIndex", "VOCCrossUnderIndex", False),
        # v1.9: price crossing session VWAP - direction from recency only,
        # fresh window 1-20 bars on 1m
        ("1VX", "1M", "1cvx", "1cvx",
         None, "VWAPCrossOverIndex", "VWAPCrossUnderIndex", True, 20),
        ("5O", "5M", "5ox", "5o",
         "VOCTrendFlag", "VOCCrossOverIndex", "VOCCrossUnderIndex", False),
        # v1.9: 5m VWAP cross, fresh window 1-4 bars (explicit user spec)
        ("5VX", "5M", "5cvx", "5cvx",
         None, "VWAPCrossOverIndex", "VWAPCrossUnderIndex", True, 4),
        ("3P", "30M", "3px", "3p",
         "PVTTrendFlag", "PVTCrossOverIndex", "PVTCrossUnderIndex", True),
        ("3M1", "30M", "3m1x", "3m1",
         "Trend_1226", "X_1226", "Y_1226", True),
        ("3M2", "30M", "3m2x", "3m2",
         "Trend_2452", "X_2452", "Y_2452", True),
        ("3O", "30M", "3ox", "3o",
         "VOCTrendFlag", "VOCCrossOverIndex", "VOCCrossUnderIndex", False),
        ("2P", "2H", "2px", "2p",
         "PVTTrendFlag", "PVTCrossOverIndex", "PVTCrossUnderIndex", True),
        ("2M1", "2H", "2m1x", "2m1",
         "Trend_1226", "X_1226", "Y_1226", True),
        ("2M2", "2H", "2m2x", "2m2",
         "Trend_2452", "X_2452", "Y_2452", True),
        ("2O", "2H", "2ox", "2o",
         "VOCTrendFlag", "VOCCrossOverIndex", "VOCCrossUnderIndex", False),
    ]
    for _slot, tfkey, xlabel, slabel, tkey, okey, ukey, allow_dn, *win in specs:
        cell = _cross_cell(tf(tfkey), tkey, okey, ukey, allow_dn,
                           max_age=win[0] if win else 4)
        if cell["label"] != "X":
            continue  # v1.1 UX: show fresh crosses only - aged-out states hidden
        rows.append({"name": xlabel, "label": xlabel, "count": cell.get("count"),
                     "color": cell["color"], "dir": cell.get("dir")})

    # Daily rows: the Pine payload's D INDICATORS block is commented out, so
    # D cross ages are unavailable — under the crosses-only rule nothing from
    # the D layer renders (removed 2026-11-01: the pinned Do row confused the
    # display; it returns once the Pine D/W blocks are enabled).
    return rows


# ----------------------------------------------------------- top strip + states

def _align_color(flags):
    """Green when every component is up, red when every one is down,
    grey (blank) when mixed or unknown."""
    vals = [v for v in flags if v is not None]
    if not vals or len(vals) < len(flags):
        return "grey"
    if all(v == 1 for v in vals):
        return "dark-green"
    if all(v == -1 for v in vals):
        return "dark-red"
    return "grey"


def _pm4_color(p, m):
    """PM cell colour (user scheme 2026-11-01, all timeframes):
    P+M2 both up = dark green; both down = dark red;
    M2 retreating down while P up = light red; P down while M2 up =
    light green (recovering); unknown = grey."""
    if p is None or m is None:
        return "grey"
    if p == 1 and m == 1:
        return "dark-green"
    if p == -1 and m == -1:
        return "dark-red"
    if p == 1 and m == -1:
        return "light-red"
    return "light-green"


def build_topstrip(snapshot, price, levels):
    """Cells for the top strip that spans the Momentors + Levels tables
    (user spec 2026-11-01): PA / 5PM / CV / 3S and, set apart on the
    right, 3PM (the 30m bias - only counts once held > 4 bars).
    Each cell also carries a 'trend' string: one arrow per underlying
    component (up = arrow up, down = arrow down); 3S appends its Stoch
    zone (OSZ/MZ/OBZ)."""
    ind = ((snapshot or {}).get("INDICATORS")) or {}

    def _arr(flag):
        return "\u25b2" if flag == 1 else ("\u25bc" if flag == -1 else "\u2013")

    e50 = _1pme50_cell(ind, price)
    pa = "dark-green" if e50.get("value") == "UP" else \
        ("dark-red" if e50.get("value") == "DN" else "grey")
    pa_trend = "\u25b2" if e50.get("value") == "UP" else \
        ("\u25bc" if e50.get("value") == "DN" else "\u2013")
    b5 = ind.get("5M") or {}
    p5, m5 = _sign(b5.get("PVTTrendFlag")), _sign(b5.get("Trend_2452"))
    m1_5 = _sign(b5.get("Trend_1226"))
    # 5PM: same logic as 3PM - background = 5m PVT vs zero, arrows M1/M2
    f5 = "dark-green" if p5 == 1 else ("dark-red" if p5 == -1 else "grey")
    vwap = _f((ind.get("1M") or {}).get("VWAP"))
    cv = None
    if price is not None and vwap is not None:
        cv = "dark-green" if price >= vwap else "dark-red"
    b30 = ind.get("30M") or {}
    p30, m30 = _sign(b30.get("PVTTrendFlag")), _sign(b30.get("Trend_2452"))
    m1_30 = _sign(b30.get("Trend_1226"))
    kt30 = _sign(b30.get("KTrendFlag"))
    k30 = _f(b30.get("K"))
    zone30 = ("OSZ" if k30 < 25 else ("OBZ" if k30 > 75 else "MZ")) if k30 is not None else ""
    # 3S colour (user rule 2026-11-01): 2PM direction x 30m Stoch zone -
    # 2PM up:  OSZ green, MZ green, OBZ red
    # 2PM down (mirror): OSZ red, MZ red, OBZ green
    b2 = ind.get("2H") or {}
    p2, m2 = _sign(b2.get("PVTTrendFlag")), _sign(b2.get("Trend_2452"))
    pm2_up, pm2_dn = (p2 == 1 and m2 == 1), (p2 == -1 and m2 == -1)
    s3 = "grey"
    if zone30:
        if pm2_up:
            s3 = "red" if zone30 == "OBZ" else "dark-green"
        elif pm2_dn:
            s3 = "green" if zone30 == "OBZ" else "dark-red"
        # 2PM mixed -> grey
    # 3PM: background = 30m PVT vs zero (user rule 2026-11-01); arrows =
    # M1 (MACD 12/26) then M2 (MACD 24/52) on 30m
    pm3 = "dark-green" if p30 == 1 else ("dark-red" if p30 == -1 else "grey")
    return [
        {"name": "PA", "color": pa, "trend": pa_trend},
        {"name": "5PM", "color": f5, "trend": _arr(m1_5) + _arr(m5)},
        {"name": "CV", "color": cv or "grey",
         "trend": "\u25b2" if cv == "dark-green" else
                  ("\u25bc" if cv == "dark-red" else "\u2013")},
        {"name": "3S", "color": s3,
         "trend": (_arr(kt30) + " " + zone30).strip()},
        {"name": "3PM", "color": pm3, "far": True,
         "trend": _arr(m1_30) + _arr(m30)},
    ]


def _group_tokens(components):
    """Join component codes by state group: same-TF components concatenate
    ('5P'+'5M1' -> '5PM1'), different-TF components join with '_'
    ('5M2'+'3S' -> '5M2_3S'). M1+M2 together collapse to 'M' (user rule
    2026-11-01: '2M1'+'2M2' -> 'M', so '2P'+'2M1'+'2M2' -> '2PM').
    Matches the user's sheet tokens."""
    groups = []
    for code in components:
        if groups and groups[-1][0] == code[0]:
            groups[-1] = (groups[-1][0], groups[-1][1] + code[1:])
        else:
            groups.append((code[0], code))
    return "_".join(g[1] for g in groups).replace("M1M2", "M")


def build_states(snapshot, price):
    """SET UP table below Momentors (user spec 2026-11-01, revised same
    day): rows are timeframes (1/5/3/2/D). Each row's components are
    evaluated individually - up-components occupy the LEFT (always green)
    cell, retracing/downtrending ones the RIGHT (always red) cell, joined
    same-TF and with '_' across TFs (e.g. left '5PM1', right '5M2_3S').
    P = PVT vs 0, S = Stoch K-trend, M1 = MACD 12/26, M2 = MACD 24/52,
    V = VOC, E50 = price vs 1m EMA50, DS/WS = Daily/Weekly Stoch."""
    ind = ((snapshot or {}).get("INDICATORS")) or {}
    htf_d = ind.get("D") or {}
    htf_w = ind.get("W") or {}

    def s(key):
        return ind.get(key) or {}

    b1, b5, b3, b2 = s("1M"), s("5M"), s("30M"), s("2H")

    def state1e50():
        e50, above = _f(b1.get("EMA50")), None
        if price is not None and e50 is not None:
            above = price >= e50
        return "up" if above else ("down" if above is False else "unknown")

    # (code, state) per row, in display order
    rows_def = [
        ("1", [("1P", _sign(b1.get("PVTTrendFlag"))),
               ("1M1", _sign(b1.get("Trend_1226"))),
               ("1M2", _sign(b1.get("Trend_2452"))),
               ("1E50", state1e50()),
               ("5S", _sign(b5.get("KTrendFlag")))]),
        ("5", [("5P", _sign(b5.get("PVTTrendFlag"))),
               ("5S", _sign(b5.get("KTrendFlag"))),
               ("5M1", _sign(b5.get("Trend_1226"))),
               ("5V", _sign(b5.get("VOCTrendFlag"))),
               ("5M2", _sign(b5.get("Trend_2452"))),
               ("3S", _sign(b3.get("KTrendFlag")))]),
        ("3", [("3P", _sign(b3.get("PVTTrendFlag"))),
               ("3S", _sign(b3.get("KTrendFlag"))),
               ("3M1", _sign(b3.get("Trend_1226"))),
               ("3M2", _sign(b3.get("Trend_2452"))),
               ("2S", _sign(b2.get("KTrendFlag")))]),
        ("2", [("2P", _sign(b2.get("PVTTrendFlag"))),
               ("2S", _sign(b2.get("KTrendFlag"))),
               ("2M1", _sign(b2.get("Trend_1226"))),
               ("2M2", _sign(b2.get("Trend_2452"))),
               ("DS", _sign(htf_d.get("KTrendFlag")))]),
        ("D", [("DP", _sign(htf_d.get("PVTTrendFlag"))),
               ("DS", _sign(htf_d.get("KTrendFlag"))),
               ("DM1", _sign(htf_d.get("Trend_1226"))),
               ("DM2", _sign(htf_d.get("Trend_2452"))),
               ("WS", _sign(htf_w.get("KTrendFlag")))]),
    ]
    rows = []
    for tf_label, comps in rows_def:
        up = [code for code, st in comps if st == 1 or st == "up"]
        down = [code for code, st in comps if not (st == 1 or st == "up")]
        rows.append({"tf": tf_label,
                     "left": _group_tokens(up) if up else "",
                     "right": _group_tokens(down) if down else ""})
    return rows


# ----------------------------------------------------------- levels ladder

# EMA rungs -> the PM state (PVT + Trend_2452, per _pm_up) that endorses
# them. Higher-TF PM uptrend colors its lower-TF EMA rungs as support
# (dark-green chip); downtrend makes them resistances (dark-red).
# Mapping (user spec 2026-11-01): 3PM2 -> 1m EMAs, 2PM -> 5m EMAs,
# DPM -> 30m EMAs, WPM -> 2h EMAs, MPM -> D EMAs, QPM2 -> W EMAs.
CHIP_MAP = [
    (("1m EMA50", "1m EMA100", "1m EMA200"), "3PM2", ("30M",)),
    (("5m EMA50", "5m EMA100", "5m EMA200"), "2PM", ("2H",)),
    # user spec 2026-11-01: DPM endorses the 30m EMAs, WPM the 2h EMAs,
    # MPM the D EMAs (same PM logic as 2PM/3PM2: PVT + Trend_2452)
    (("30m EMA50", "30m EMA100", "30m EMA200"), "DPM", ("D",)),
    (("2h EMA50", "2h EMA100", "2h EMA200"), "WPM", ("W",)),
    (("D EMA100", "D EMA200"), "MPM", ("D",)),
    (("W EMA100", "W EMA200"), "QPM2", ("Q",)),
]
# Pine v1.4 supplies the compact W/D/Q INDICATORS blocks (PVTTrendFlag +
# Trend_2452) - the higher-TF PM state colors its lower-TF EMA rungs.
# (2026-11-01: the +/-150pt headroom filter was removed - the ladder now
# shows ALL levels; the scroll container + Close-centering handle the rest.)

# v1.13 FAM: the SPATAM levels-table 4th-column tags arrive IN the LEVELS
# webhook (LEVELS.FAM) - seven comma-joined "<tag><U|D><1|0>" segments in the
# fixed family order 5,3,2,D,W,M,Q (tag text exactly as the chart prints it;
# U/D = PVT direction, 1/0 = dark shade = MACD2 agrees with the PVT trend).
# Level name -> family segment index (same mapping as SPATAM's f_levelTag).
FAM_IDX = {
    "1m EMA50": 0,
    "1m EMA100": 1, "1m EMA200": 1,
    "5m EMA50": 2, "5m EMA100": 2, "5m EMA200": 2,
    "30m EMA50": 3, "30m EMA100": 3, "30m EMA200": 3,
    "2h EMA50": 4, "2h EMA100": 4, "2h EMA200": 4,
    "D EMA100": 5, "D EMA200": 5,
    "W EMA100": 6, "W EMA200": 6,
}
# (up, dark) -> chip_color key; shades match SPATAM's table colors exactly.
FAM_COLORS = {
    (True, True): "spatam-dark-green", (True, False): "spatam-light-green",
    (False, True): "spatam-dark-red", (False, False): "spatam-light-red",
}


def _parse_fam(levels):
    """Parse LEVELS.FAM into {level_name: (tag, up, dark)}; None when absent
    (pre-v1.13 webhook) or malformed."""
    fam = (levels or {}).get("FAM")
    if not isinstance(fam, str) or not fam:
        return None
    segs = fam.split(",")
    if len(segs) != 7:
        return None
    out = {}
    for name, idx in FAM_IDX.items():
        seg = segs[idx] or ""
        if len(seg) >= 3:
            out[name] = (seg[:-2], seg[-2] == "U", seg[-1] == "1")
    return out


def build_ladder(snapshot, levels, price=None):
    """ALL Levels Ladder rows, sorted price-desc, the Close row highlighted
    and centered by the widget's scroll logic. Chip = the endorsing
    higher-TF PM state, green when it uptrends (rung = support), red when
    it downtrends."""
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

    # resolve endorsing PM states once per chip (fallback when the webhook
    # carries no FAM - pre-v1.13 Pine)
    chip_states = {}
    for _names, chip, tfs in CHIP_MAP:
        vals = [_pm_up(ind.get(tf)) for tf in tfs]
        known = [v for v in vals if v is not None]
        chip_states[chip] = (all(known) and known[0]) if known else None

    fam = _parse_fam(levels)

    out = []
    if price is not None:
        for row in parsed:
            if row["name"] == "Close":
                out.append({"name": "Close", "price": row["price"], "pts": 0,
                            "close": True, "chip": None, "chip_color": None})
                continue
            dist = abs(row["price"] - price)
            chip = chip_for(row["name"])
            chip_color = None
            if fam is not None and row["name"] in fam:
                # v1.13: the SPATAM tag, verbatim + its own table shading
                chip, up, dark = fam[row["name"]]
                chip_color = FAM_COLORS[(up, dark)]
            elif chip is not None:
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
