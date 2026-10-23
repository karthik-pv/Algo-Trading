"""Update the Nifty 30/5/1 files (+ the 1D file) through 2026-10-19.

Sources:
  COMB  ~/Downloads/NSE_NIFTY1!, 30m 5m 1m.csv  (1m HA bars Aug 3 -> Oct 19
        15:39, 57 cols; glossary-verified block map)
  NEW5  ~/Downloads/NSE_NIFTY1!, 5_c8600.csv    (TRUE 5m HA bars Oct 9 ->
        Oct 19 15:35; exact OHLC + D/2H EMA carries; its MACD/PVT panes are
        D-resolved -> NOT used)

Verified on the Aug 3 -> Sep 25 overlap (see scripts/verify_*.py):
  - EMAs carried on every 1m row = the containing bar's FINAL value (1.0000)
  - 5m MACD/Stoch/PVT populate on the 5m close-minute rows (+4); 30m on the
    30m close-minute rows (+29)
  - old-file MACD M1 = 12/26, M2 = 24/52 (1m pairs exactly recomputable)
  - D EMAs = EMA over the daily HA closes (worst 0.3 pt)
  - 2H EMA200 in COMB is warmup-divergent -> continued from the old chain /
    taken from NEW5 where available

Run:  venv/bin/python scripts/update_nifty_store.py [--write]
"""
import csv
import os
import shutil
import sys
from collections import defaultdict

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = os.path.join(REPO, "data", "insight", "csv")
COMB = "/Users/phvrao/Downloads/NSE_NIFTY1!, 30m 5m 1m.csv"
NEW5 = "/Users/phvrao/Downloads/NSE_NIFTY1!, 5_c8600.csv"
BACKUP = os.path.join(CSV, "backup_2026-10-20")

F30 = os.path.join(CSV, "NSE_NIFTY1!, 30_42a03.csv")
F5 = os.path.join(CSV, "NSE_NIFTY1!, 5_be22a.csv")
F1 = os.path.join(CSV, "NSE_NIFTY1!, 1_20adf.csv")
FD = os.path.join(CSV, "NSE_NIFTY1!, 1D_6b212.csv")

START = "2026-09-28"          # first appended date
LAST = "2026-10-19"


def load_named(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f)
        raw = [x.strip() for x in next(rd)]
        hdr, cnt = [], {}
        for h in raw:
            cnt[h] = cnt.get(h, 0) + 1
            hdr.append(h if cnt[h] == 1 else "%s#%d" % (h, cnt[h]))
        rows = []
        for r in rd:
            if len(r) < len(hdr):
                r += [""] * (len(hdr) - len(r))
            rows.append(dict(zip(hdr, r)))
    return hdr, rows


def fnum(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float((x or "").strip())
    except (ValueError, TypeError):
        return None


def ema_chain(vals, period, seed=None):
    a = 2.0 / (period + 1)
    e = seed
    out = []
    for v in vals:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


def mins(ts):
    return int(ts[11:13]) * 60 + int(ts[14:16])


def stamp(ts_min):
    return "%02d:%02d" % (ts_min // 60, ts_min % 60)


def fmt(v):
    """old-file style float text; None -> ''"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return repr(float(v))


# --------------------------------------------------------------------------
# load everything
# --------------------------------------------------------------------------
hdr_c, comb = load_named(COMB)
hdr_n, new5 = load_named(NEW5)
h30, old30 = load_named(F30)
h5, old5 = load_named(F5)
h1, old1 = load_named(F1)
hd, oldD = load_named(FD)

# combined rows in the append window
crows = [r for r in comb if START <= r["time"][:10] <= LAST]
cdates = sorted({r["time"][:10] for r in crows})
print("combined rows %s..%s: %d rows, %d dates" % (crows[0]["time"], crows[-1]["time"], len(crows), len(cdates)))
holidays = sorted({r["time"][:10] for r in comb} - set(cdates) - {"2026-10-19"} if False else [])
print("append dates:", ", ".join(cdates))

# new 5m export rows (Oct 9+)
n5_by_stamp = {r["time"][:16]: r for r in new5}
# per-day FIRST row: the daily-carried columns (D EMA100/200, 2H EMA200)
# hold ONE value per day on these rows
n5_day_first = {}
for r in new5:
    n5_day_first.setdefault(r["time"][:10], r)


def new5_at(ts):
    return n5_by_stamp.get(ts[:16])


def new5_day(date):
    return n5_day_first.get(date)

# --------------------------------------------------------------------------
# bucket the combined's 1m rows: 5m / 30m / 2H / daily
# --------------------------------------------------------------------------
def bucket_of(ts, tf):
    m = mins(ts)
    b = (m - 555) // tf
    start = 555 + b * tf
    return ts[:10] + " " + stamp(start)


g5 = defaultdict(list)
g30 = defaultdict(list)
g2h = defaultdict(list)
gday = defaultdict(list)
for r in crows:
    g5[bucket_of(r["time"], 5)].append(r)
    g30[bucket_of(r["time"], 30)].append(r)
    g2h[bucket_of(r["time"], 120)].append(r)
    gday[r["time"][:10]].append(r)


def approx_bar(rows):
    """(rawO~, rawH, rawL, rawC~, haC) from 1m HA rows"""
    o = fnum(rows[0]["open"])
    c = fnum(rows[-1]["close"])
    h = max(fnum(x["high"]) for x in rows)
    l = min(fnum(x["low"]) for x in rows)
    hac = (o + h + l + c) / 4.0
    return o, h, l, c, hac


# --------------------------------------------------------------------------
# daily closes: recovered from NEW5's exact D-EMA carries where possible.
#   c_t = (EMA100_t - (1-a)*EMA100_{t-1}) / a   (a = 2/101)
# valid for consecutive trading days (Oct 12..19); other days fall back to
# the 1m-bucket approximation (~18 pts mean error, flagged).
# --------------------------------------------------------------------------
ALPHA100 = 2.0 / 101.0
implied_close = {}      # carried-on row date -> the daily HA close it implies
prev_ema100 = fnum(old5[-1]["D EMA100"])   # Sep 25 anchor (5m chart's chain)
prev_had_carry = False
for date in cdates:
    n5d = new5_day(date)
    ema100_t = fnum(n5d["D EMA100"]) if n5d else None
    if ema100_t is not None and prev_had_carry:
        implied_close[date] = (ema100_t - (1 - ALPHA100) * prev_ema100) / ALPHA100
    prev_ema100 = ema100_t if ema100_t is not None else prev_ema100
    prev_had_carry = ema100_t is not None

# day-alignment probe (diagnostic): the carry on date's rows implies the
# day's first-minute close (22889 on Oct 19 vs the 22897 session close)
for probe in ("2026-10-16", "2026-10-19"):
    if probe in implied_close:
        print("carry-on-%s implies first-minute close %.2f (chart-displayed D-EMA state)" % (
            probe, implied_close[probe]))

# NOTE on the NEW5 D-EMA carries: they are frozen at each day's first
# minute (the value the chart DISPLAYED all day — verified: the implied
# "close" backs out to the first-minute print, e.g. 22889 on Oct 19 vs the
# 22897 session close). Right semantics for replay ladders; NOT daily closes.
# Daily HA closes therefore come from the 1m-bucket approximation (~18 pts
# mean error) and are only used for the 1D MACD chain continuation.
day_close = {}
day_ohlc = {}
for d, rows in gday.items():
    o, h, l, c, hac = approx_bar(rows)
    day_ohlc[d] = (o, h, l, c, hac)
    day_close[d] = hac

# cross-check: the EMA200-implied closes must agree with the EMA100-implied
ALPHA200 = 2.0 / 201.0
prev_ema200 = fnum(old5[-1]["D EMA200"])
xchk = []
prev_had_carry = False
for date in cdates:
    n5d = new5_day(date)
    ema200_t = fnum(n5d["D EMA200"]) if n5d else None
    if ema200_t is not None and prev_had_carry and date in implied_close:
        c200 = (ema200_t - (1 - ALPHA200) * prev_ema200) / ALPHA200
        xchk.append(abs(c200 - implied_close[date]))
    prev_ema200 = ema200_t if ema200_t is not None else prev_ema200
    prev_had_carry = ema200_t is not None
print("implied-close cross-check (EMA100 vs EMA200 paths): n=%d max diff=%.3f pts" % (
    len(xchk), max(xchk) if xchk else 0))

dclose_seq = [day_close[d] for d in cdates]
day_idx = {d: i for i, d in enumerate(cdates)}

# HA-open chains
def ha_open_chain(prev_row, buckets, tf):
    """prev_row = last old row (dict); buckets = ordered (key, rows)."""
    out = {}
    prev_o = fnum(prev_row["open"])
    prev_c = fnum(prev_row["close"])
    for key, rows in buckets:
        ha_o = (prev_o + prev_c) / 2.0
        o, h, l, c, hac = approx_bar(rows)
        out[key] = (ha_o, h, l, hac)
        prev_o, prev_c = ha_o, hac
    return out


# --------------------------------------------------------------------------
# daily-EMA continuations (per-file anchors)
# --------------------------------------------------------------------------
def continue_ema(anchor_val, period, closes):
    a = 2.0 / (period + 1)
    e = anchor_val
    out = []
    for c in closes:
        e = a * c + (1 - a) * e
        out.append(e)
    return out


# --------------------------------------------------------------------------
# 2H EMA200 continuations (anchor per file; 2H HA closes approximated)
# --------------------------------------------------------------------------
g2h_keys = sorted(g2h.keys())
g2h_closes = [approx_bar(g2h[k])[4] for k in g2h_keys]
e200_2h_30 = continue_ema(fnum(old30[-1]["2H EMA200"]), 200, g2h_closes)
e200_2h_5 = continue_ema(fnum(old5[-1]["2H EMA200"]), 200, g2h_closes)
e200_2h_1 = continue_ema(fnum(old1[-1]["2H EMA200"]), 200, g2h_closes)
g2h_idx = {k: i for i, k in enumerate(g2h_keys)}

# --------------------------------------------------------------------------
# 1m MACD chain recompute (verified exact on the old file) + continuation
# --------------------------------------------------------------------------
closes1_old = [fnum(r["close"]) for r in old1]
m1a = ema_chain(closes1_old, 12)
m1b = ema_chain(closes1_old, 26)
m2a = ema_chain(closes1_old, 24)
m2b = ema_chain(closes1_old, 52)
mac1_line = [a - b for a, b in zip(m1a, m1b)]
mac2_line = [a - b for a, b in zip(m2a, m2b)]
sig1 = ema_chain(mac1_line, 9)
sig2 = ema_chain(mac2_line, 9)
state = {"e12": m1a[-1], "e26": m1b[-1], "s1": sig1[-1],
         "e24": m2a[-1], "e52": m2b[-1], "s2": sig2[-1]}


def macd1m_next(close):
    a12, a26, a9 = 2 / 13.0, 2 / 27.0, 2 / 10.0
    a24, a52 = 2 / 25.0, 2 / 53.0
    s = state
    s["e12"] = a12 * close + (1 - a12) * s["e12"]
    s["e26"] = a26 * close + (1 - a26) * s["e26"]
    s["e24"] = a24 * close + (1 - a24) * s["e24"]
    s["e52"] = a52 * close + (1 - a52) * s["e52"]
    line1 = s["e12"] - s["e26"]
    line2 = s["e24"] - s["e52"]
    s["s1"] = a9 * line1 + (1 - a9) * s["s1"]
    s["s2"] = a9 * line2 + (1 - a9) * s["s2"]
    return line1, s["s1"], line2, s["s2"]


# --------------------------------------------------------------------------
# 1D MACD chain recompute + continuation
# --------------------------------------------------------------------------
closesD_old = [fnum(r["close"]) for r in oldD]
m1a = ema_chain(closesD_old, 12)
m1b = ema_chain(closesD_old, 26)
m2a = ema_chain(closesD_old, 24)
m2b = ema_chain(closesD_old, 52)
Dstate = {"e12": m1a[-1], "e26": m1b[-1],
          "s1": ema_chain([a - b for a, b in zip(m1a, m1b)], 9)[-1],
          "e24": m2a[-1], "e52": m2b[-1],
          "s2": ema_chain([a - b for a, b in zip(m2a, m2b)], 9)[-1]}


def macd1d_next(close):
    a12, a26, a9 = 2 / 13.0, 2 / 27.0, 2 / 10.0
    a24, a52 = 2 / 25.0, 2 / 53.0
    s = Dstate
    s["e12"] = a12 * close + (1 - a12) * s["e12"]
    s["e26"] = a26 * close + (1 - a26) * s["e26"]
    s["e24"] = a24 * close + (1 - a24) * s["e24"]
    s["e52"] = a52 * close + (1 - a52) * s["e52"]
    line1 = s["e12"] - s["e26"]
    line2 = s["e24"] - s["e52"]
    s["s1"] = a9 * line1 + (1 - a9) * s["s1"]
    s["s2"] = a9 * line2 + (1 - a9) * s["s2"]
    return line1, s["s1"], line2, s["s2"]


# --------------------------------------------------------------------------
# helpers: source lookups with fallback to earlier minutes
# --------------------------------------------------------------------------
cby = {r["time"][:16]: r for r in crows}


def comb_at(ts, dm, col):
    """combined value at ts+dm minutes; falls back to -1..-6 minutes"""
    m = mins(ts) + dm
    for back in range(7):
        key = ts[:11] + stamp(m - back)
        r = cby.get(key)
        if r is not None:
            v = fnum(r.get(col))
            if v is not None:
                return v
    return None


# --------------------------------------------------------------------------
# build appended rows
# --------------------------------------------------------------------------
def sval(x):
    return "" if x is None else fmt(x)


# ---- 1m file ----
rows_1m = []
for r in crows:
    ts = r["time"]
    l1, s1v, l2, s2v = macd1m_next(fnum(r["close"]))
    rows_1m.append({
        "time": ts, "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
        "1m EMA9": r["1m EMA9"], "1m EMA21": r["1m EMA21"], "1m EMA50": r["1m EMA50"],
        "1m EMA100": r["1m EMA100"], "1m EMA200": r["1m EMA200"],
        "5m EMA50": r["5m EMA50"], "5m EMA100": r["5m EMA100"], "5m EMA200": r["5m EMA200"],
        "30m EMA50": r["30m EMA50"], "30m EMA100": r["30m EMA100"], "30m EMA200": r["30m EMA200"],
        "2H EMA100": r["2H EMA100"], "2H EMA200": "",      # 2H EMA200: divergent; filled below
        "D EMA100": "", "D EMA200": "", "W EMA100": "", "W EMA200": "",
        "M EMA100": "", "M EMA200": "",
        "VWAP": r["VWAP"],
        "MACD Line": fmt(l1), "Signal Line": fmt(s1v),
        "MACD Line#2": fmt(l2), "Signal Line#2": fmt(s2v),
        "Plot": "", "PVT": r["PVT"], "Plot#2": r["Plot#2"],
        "Cross Up": r["Cross Up"], "Cross Down": r["Cross Down"],
        "K": r["K"], "D": r["D"],
    })
# 2H EMA200 for 1m rows: the day's carry from NEW5; else the continued chain
for row in rows_1m:
    ts = row["time"]
    n5d = new5_day(ts[:10])
    if n5d and (n5d["2H EMA200"] or "").strip():
        row["2H EMA200"] = n5d["2H EMA200"]
    else:
        key = bucket_of(ts, 120)
        i = g2h_idx.get(key)
        row["2H EMA200"] = sval(e200_2h_1[i]) if i is not None else ""

# ---- 5m file ----
rows_5m = []
skipped_5m = 0
for date in cdates:
    day_stamps = [555 + 5 * i for i in range(77) if 555 + 5 * i <= 935]
    for m in day_stamps:
        st = stamp(m)
        ts = date + "T" + st + ":00+05:30"
        key = date + " " + st
        if m == 930:               # the 15:30 bar: no 1m source rows
            if date in ("2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14",
                        "2026-10-15", "2026-10-16", "2026-10-19"):
                pass                # Oct 9+: NEW5 has the true bar
            else:
                skipped_5m += 1
                continue
        b = g5.get(key)
        n5 = new5_at(ts)
        if m == 930 and n5 is None:
            skipped_5m += 1
            continue
        # OHLC + VWAP: prefer NEW5 (true bars)
        if n5 is not None:
            o, h, l, c = n5["open"], n5["high"], n5["low"], n5["close"]
            vwap = n5["VWAP"]
        else:
            o_, h_, l_, c_, hac = approx_bar(b)
            o_ = fnum(o_)
            # HA envelope: HA-H = max(rawH, HA-O, HA-C); HA-L = min(...)
            h_ = max(h_, o_, hac)
            l_ = min(l_, o_, hac)
            o, h, l, c = fmt(o_), fmt(h_), fmt(l_), fmt(hac)
            vwap = comb_at(ts, 0, "VWAP")
        di = day_idx[date]
        n5d = new5_day(date)
        if n5d is not None and (n5d["D EMA100"] or "").strip():
            d100, d200 = n5d["D EMA100"], n5d["D EMA200"]   # exact TradingView carries
        else:
            d100, d200 = "", ""   # Sep 28 -> Oct 8: no source (approx too noisy)
        if n5d is not None and (n5d["2H EMA200"] or "").strip():
            e2h200 = n5d["2H EMA200"]
        else:
            gi = g2h_idx.get(bucket_of(ts, 120))
            e2h200 = sval(e200_2h_5[gi]) if gi is not None else ""
        rows_5m.append({
            "time": ts, "open": o, "high": h, "low": l, "close": c,
            "1m EMA9": comb_at(ts, 0, "1m EMA9"), "1m EMA21": comb_at(ts, 0, "1m EMA21"),
            "1m EMA50": comb_at(ts, 0, "1m EMA50"), "1m EMA100": comb_at(ts, 0, "1m EMA100"),
            "1m EMA200": comb_at(ts, 0, "1m EMA200"),
            "5m EMA50": comb_at(ts, 4, "5m EMA50"), "5m EMA100": comb_at(ts, 4, "5m EMA100"),
            "5m EMA200": comb_at(ts, 4, "5m EMA200"),
            "30m EMA50": comb_at(ts, 0, "30m EMA50"), "30m EMA100": comb_at(ts, 0, "30m EMA100"),
            "30m EMA200": comb_at(ts, 0, "30m EMA200"),
            "2H EMA100": comb_at(ts, 0, "2H EMA100"), "2H EMA200": e2h200,
            "D EMA100": d100, "D EMA200": d200,
            "W EMA100": "", "W EMA200": "", "M EMA100": "", "M EMA200": "",
            "VWAP": vwap,
            "MACD Line": comb_at(ts, 4, "MACD Line#2"), "Signal Line": comb_at(ts, 4, "Signal Line#2"),
            "MACD Line#2": comb_at(ts, 4, "MACD Line#3"), "Signal Line#2": comb_at(ts, 4, "Signal Line#3"),
            "Plot": "",
            "PVT": comb_at(ts, 4, "PVT#2"), "Plot#2": comb_at(ts, 4, "Plot#4"),
            "Cross Up": comb_at(ts, 4, "Cross Up#2"), "Cross Down": comb_at(ts, 4, "Cross Down#2"),
            "K": comb_at(ts, 4, "K#2"), "D": comb_at(ts, 4, "D#2"),
        })

# ---- 30m file ----
rows_30m = []
prev30 = old30[-1]
prev_o, prev_c = fnum(prev30["open"]), fnum(prev30["close"])
for date in cdates:
    for b_start in range(555, 936, 30):
        st = stamp(b_start)
        ts = date + "T" + st + ":00+05:30"
        key = date + " " + st
        b = g30.get(key)
        if not b:
            continue
        o_, h_, l_, c_, hac = approx_bar(b)
        ha_o = (prev_o + prev_c) / 2.0
        # HA envelope: HA-H = max(rawH, HA-O, HA-C); HA-L = min(...)
        h_ = max(h_, ha_o, hac)
        l_ = min(l_, ha_o, hac)
        prev_o, prev_c = ha_o, hac
        n5 = new5_at(ts)
        n5d = new5_day(date)
        if n5d is not None and (n5d["2H EMA200"] or "").strip():
            e2h200 = n5d["2H EMA200"]
        else:
            gi = g2h_idx.get(key)
            e2h200 = sval(e200_2h_30[gi]) if gi is not None else ""
        vwap = n5["VWAP"] if (n5 and (n5["VWAP"] or "").strip()) else comb_at(ts, 0, "VWAP")
        rows_30m.append({
            "time": ts,
            "open": fmt(ha_o), "high": fmt(h_), "low": fmt(l_), "close": fmt(hac),
            "5m EMA100": comb_at(ts, 0, "5m EMA100"), "5m EMA200": comb_at(ts, 0, "5m EMA200"),
            "30m EMA9": comb_at(ts, 0, "30m EMA9"), "30m EMA21": comb_at(ts, 0, "30m EMA21"),
            "30m EMA50": comb_at(ts, 0, "30m EMA50"), "30m EMA100": comb_at(ts, 0, "30m EMA100"),
            "30m EMA200": comb_at(ts, 0, "30m EMA200"),
            "2H EMA50": comb_at(ts, 0, "2H EMA50"), "2H EMA100": comb_at(ts, 0, "2H EMA100"),
            "2H EMA200": e2h200,
            "D EMA50": "",   # no source in any export (approx chain too noisy)
            "D EMA100": (n5d["D EMA100"] if n5d and (n5d["D EMA100"] or "").strip() else ""),
            "D EMA200": (n5d["D EMA200"] if n5d and (n5d["D EMA200"] or "").strip() else ""),
            "W EMA50": "", "W EMA100": "", "W EMA200": "", "M EMA100": "", "M EMA200": "",
            "VWAP": vwap,
            "MACD Line": comb_at(ts, 29, "MACD Line#4"), "Signal Line": comb_at(ts, 29, "Signal Line#4"),
            "MACD Line#2": comb_at(ts, 29, "MACD Line#5"), "Signal Line#2": comb_at(ts, 29, "Signal Line#5"),
            "Plot": "", "PVT": comb_at(ts, 29, "PVT#3"), "Plot#2": comb_at(ts, 29, "Plot#5"),
            "Cross Up": comb_at(ts, 29, "Cross Up#3"), "Cross Down": comb_at(ts, 29, "Cross Down#3"),
            "K": comb_at(ts, 29, "K#3"), "D": comb_at(ts, 29, "D#3"),
        })

# ---- 1D file ----
rows_D = []
prevD = oldD[-1]
prev_o, prev_c = fnum(prevD["open"]), fnum(prevD["close"])
for date in cdates:
    o_, h_, l_, c_, hac = day_ohlc[date]
    ha_o = (prev_o + prev_c) / 2.0
    # the recovered (exact) daily HA close where available
    close_val = day_close[date]
    prev_o, prev_c = ha_o, close_val
    l1, s1v, l2, s2v = macd1d_next(close_val)
    n5_first = new5_day(date)
    rows_D.append({
        "time": date, "open": fmt(ha_o), "high": fmt(h_), "low": fmt(l_), "close": fmt(close_val),
        "30m EMA100": "", "30m EMA200": "", "2H EMA100": "", "2H EMA200": "",
        "D EMA9": "", "D EMA21": "", "D EMA50": "",
        "D EMA100": (n5_first["D EMA100"] if n5_first and (n5_first["D EMA100"] or "").strip() else ""),
        "D EMA200": (n5_first["D EMA200"] if n5_first and (n5_first["D EMA200"] or "").strip() else ""),
        "W EMA50": "", "W EMA100": "", "W EMA200": "", "M EMA100": "", "M EMA200": "",
        "MACD Line": fmt(l1), "Signal Line": fmt(s1v),
        "MACD Line#2": fmt(l2), "Signal Line#2": fmt(s2v),
        "Plot": "", "PVT": "", "Plot#2": "", "Cross Up": "", "Cross Down": "",
        "K": "", "D": "",
    })

# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------
print("\n--- validation ---")
cby_all = {r["time"][:16]: r for r in comb}
print("implied-close coverage: %d/%d days exact (%s)" % (
    len(implied_close), len(cdates),
    ", ".join(d for d in cdates if d in implied_close) or "none"))
print("appended rows: 1m=%d  5m=%d  30m=%d  D=%d  (5m 15:30 rows skipped: %d)" % (
    len(rows_1m), len(rows_5m), len(rows_30m), len(rows_D), skipped_5m))

# OHLC sanity + no dup stamps
for name, rows in [("1m", rows_1m), ("5m", rows_5m), ("30m", rows_30m)]:
    bad = 0
    seen = set()
    prev_ts = ""
    mono = True
    for r in rows:
        o, h, l, c = (fnum(r["open"]), fnum(r["high"]), fnum(r["low"]), fnum(r["close"]))
        if None in (o, h, l, c) or h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9 or h < l:
            bad += 1
        t = r["time"]
        if t in seen:
            mono = False
        seen.add(t)
        if t <= prev_ts:
            mono = False
        prev_ts = t
    print("  %s: OHLC-violations=%d stamps-unique-monotonic=%s" % (name, bad, mono))

# the 15:15 30m MACD delta (combined close-row vs the old file's true final)
d15 = []
for r in old30:
    if r["time"][11:16] != "15:15" or r["time"][:10] not in {d[:10] for d in cby_all}:
        continue
    a = fnum(r["MACD Line#2"])
    cr = cby_all.get(r["time"][:11] + "15:44")
    if cr is None:
        cr = cby_all.get(r["time"][:11] + "15:39")
    b_ = fnum(cr.get("MACD Line#5")) if cr else None
    if a is not None and b_ is not None:
        d15.append(abs(a - b_))
print("  30m 15:15-row MACD2 |old-final - combined-lastrow|: n=%d max=%.3f" % (
    len(d15), max(d15) if d15 else 0))

# missing critical values in appended rows
def _blank(v):
    if isinstance(v, float):
        return False
    return not (v or "").strip()


for name, rows, cols in [
    ("30m", rows_30m, ["MACD Line", "MACD Line#2", "PVT", "K", "D", "30m EMA50"]),
    ("5m", rows_5m, ["MACD Line#2", "PVT", "K", "D", "5m EMA50"]),
    ("1m", rows_1m, ["MACD Line", "MACD Line#2", "PVT", "K", "D"]),
]:
    miss = {c: sum(1 for r in rows if _blank(r[c])) for c in cols}
    print("  %s missing: %s" % (name, {k: v for k, v in miss.items() if v}))

# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------
def write_appended(path, hdr, old_rows, new_rows, do_write):
    if not do_write:
        print("DRY-RUN: would append %d rows -> %s" % (len(new_rows), os.path.basename(path)))
        return
    with open(path, "a", newline="", encoding="utf-8") as fb:
        w = csv.writer(fb, lineterminator="\n")
        for r in new_rows:
            w.writerow([r.get(h, "") for h in hdr])


if "--write" in sys.argv:
    os.makedirs(BACKUP, exist_ok=True)
    for p in (F30, F5, F1, FD):
        shutil.copy2(p, os.path.join(BACKUP, os.path.basename(p)))
    print("backups -> %s" % BACKUP)
    write_appended(F1, h1, old1, rows_1m, True)
    write_appended(F5, h5, old5, rows_5m, True)
    write_appended(F30, h30, old30, rows_30m, True)
    write_appended(FD, hd, oldD, rows_D, True)
    print("WROTE: 1m +%d, 5m +%d, 30m +%d, D +%d" % (
        len(rows_1m), len(rows_5m), len(rows_30m), len(rows_D)))
else:
    write_appended(F1, h1, old1, rows_1m, False)
    write_appended(F5, h5, old5, rows_5m, False)
    write_appended(F30, h30, old30, rows_30m, False)
    write_appended(FD, hd, oldD, rows_D, False)
    print("\nDRY-RUN only — rerun with --write to apply.")
