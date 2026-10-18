"""Multi-TF trend/price-action insight from the 5m TradingView export.

Source: data/insight/csv/NSE_NIFTY1!, 5_Sep 2025-Oct 2026.csv
(each 5m row carries 5m/30m/2H/D indicator state: PVT, MACD, Stoch, VOC, EMAs, VWAP).

Two export layouts supported (auto-detected in load_bars):
  - LEGACY prefixed headers: "5m MACD Line", "30m PVT", ... (one MACD 24/52 pair per TF).
  - CURRENT unprefixed positional layout (59 cols, repeated generic names). Per HTF
    block TWO MACD pairs: M1 = 12/26 (not used by current setups - reserved for a
    future setup), M2 = 24/52 (USE - the research pair).
    New columns vs legacy: "5m EMA4", "1m EMA100", "2H VOC".
    "2H VOC" is a 30m-computed VOC with periods x4 (36/84) = the 2H equivalent;
    it updates on 30m boundaries (13-14 bars/day), NOT on 2H closes.
    User-renamed header vocabulary (from Nifty Data.numbers): 3M1/3M2, 2M1/2M2,
    DM1/DM2 = the MACD pairs; "5m Stock D"/"30m Stock D" mean Stoch D (typo normalized).

Dev window: 2025-09-15 .. 2026-03-31 (insight development).
Out-of-sample: 2026-04-01 .. file end (kept untouched until validation).

Run:
    python -m forecasting.trend_insight --verify     # Phase 1 data checks
    python -m forecasting.trend_insight --build      # Phase 2/3 insight pack

Column notes for THIS export (differs from the old 5_be22a.csv):
  - One MACD Line/Signal Line pair per TF, prefixed: "5m MACD Line",
    "30m MACD Line", "2H MACD Line", "D MACD Line". Variant (12/26 vs 24/52)
    is identified numerically in verify().
  - Generic "Plot"/"Cross Up"/"Cross Down" columns repeat once per TF block;
    ownership is by position (block = columns since the previous TF's PVT).
  - "5m VOC Plot"/"30m VOC" are dashboard plots; not used here.
  - CSV PVT columns are normalized (small values) -> signs/zero-crosses only.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta

from forecasting.store_builder import read_tv_csv, fnum, sgn

DEFAULT_CSV = os.path.join("data", "insight", "csv", "NSE_NIFTY1!, 5_Sep 2025-Oct 2026.csv")
DEFAULT_OUT_DIR = os.path.join("data", "insight", "reports")

DEV_START = "2025-09-15"
DEV_END = "2026-03-31"
SESSION_START = "09:15"

TF_PREFIX = {  # logical TF -> header prefix used in this export
    "5m": "5m", "30m": "30m", "2h": "2H", "1d": "D",
}

# Columns we read by (prefixed) header name
def col_names():
    return {
        "5m": {
            "pvt": "5m PVT",
            "macd": "5m MACD Line",
            "signal": "5m Signal Line",
            "k": "5m K", "d": "5m D",
            "ema9": "5m EMA9", "ema21": "5m EMA21",
            "ema50": "5m EMA50", "ema100": "5m EMA100", "ema200": "5m EMA200",
            "vwap": "5m VWAP",
            "voc": "5m VOC Plot",
        },
        "30m": {
            "pvt": "30m PVT", "macd": "30m MACD Line", "signal": "30m Signal Line",
            "k": "30m K", "d": "30m D",
            "ema50": "30m EMA50", "ema100": "30m EMA100", "ema200": "30m EMA200",
            "voc": "30m VOC",
        },
        "2h": {
            "pvt": "2H PVT", "macd": "2H MACD Line", "signal": "2H Signal Line",
            "ema50": "2H EMA50", "ema100": "2H EMA100", "ema200": "2H EMA200",
        },
        "1d": {
            "pvt": "D PVT", "macd": "D MACD Line", "signal": "D Signal Line",
            "ema100": "D EMA100", "ema200": "D EMA200",
        },
        "micro": {"ema200_1m": "1m EMA200"},
    }


def session_stamp(hhmm, step_min):
    """Map HH:MM to the containing bar stamp (bar grid from 09:15). None if outside."""
    if not hhmm:
        return None
    try:
        t = datetime.strptime(hhmm, "%H:%M")
    except ValueError:
        return None
    s = datetime.strptime(SESSION_START, "%H:%M")
    delta = int((t - s).total_seconds() // 60)
    if delta < 0:
        return None
    m = (delta // step_min) * step_min
    return (s + timedelta(minutes=m)).strftime("%H:%M")


def load_bars(csv_path):
    """Parse a 5m export into bar dicts; auto-detects the layout (see module docstring)."""
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        header = [h.strip() for h in next(csv.reader(f))]
    if "5m MACD Line" in header:
        return _load_bars_prefixed(csv_path)
    return _load_bars_positional(csv_path)


def _load_bars_prefixed(csv_path):
    """Legacy layout: per-TF prefixed header names (one MACD 24/52 pair per TF)."""
    rows = read_tv_csv(csv_path)
    cn = col_names()
    bars = []
    for r in rows:
        if not r.get("date") or not r.get("hhmm"):
            continue
        b = {
            "date": r["date"], "hhmm": r["hhmm"],
            "open": fnum(r, "open"), "high": fnum(r, "high"),
            "low": fnum(r, "low"), "close": fnum(r, "close"),
        }
        for tf, cols in cn.items():
            if "pvt" in cols:
                pvt = fnum(r, cols["pvt"])
                b["%s_pvt" % tf] = pvt
                b["%s_pvt_sign" % tf] = sgn(pvt)
            b["%s_macd" % tf] = fnum(r, cols.get("macd", ""))
            b["%s_signal" % tf] = fnum(r, cols.get("signal", ""))
            if "voc" in cols:
                b["%s_voc" % tf] = fnum(r, cols["voc"])
            for lv in ("ema50", "ema100", "ema200", "ema9", "ema21"):
                if lv in cols:
                    b["%s_%s" % (tf, lv)] = fnum(r, cols[lv])
            if "vwap" in cols:
                b["vwap"] = fnum(r, cols["vwap"])
            if "k" in cols:
                b["%s_k" % tf] = fnum(r, cols["k"])
                b["%s_d" % tf] = fnum(r, cols["d"])
        b["ema200_1m"] = fnum(r, cn["micro"]["ema200_1m"])
        bars.append(b)
    return bars


def _read_raw_csv(path):
    """Raw reader for the unprefixed positional layout (duplicate headers kept by index)."""
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader)]
        rows = []
        for raw in reader:
            if not raw or not raw[0].strip():
                continue
            if len(raw) < len(header):
                raw = raw + [""] * (len(header) - len(raw))
            rows.append(raw)
    return header, rows


def _load_bars_positional(csv_path):
    """Current unprefixed layout: repeated generic headers, blocks anchored from the right.

    0-indexed (59 cols): 0-4 time/OHLC | 5-20 EMAs (1m E100/E200, 5m E4/E9/E21/E50/
    E100/E200, 30m/2H/D) | 21 VWAP | 5m: MACD 22-23, VOC 24, PVT 25, X-markers
    26-28, Stoch 29-30 | 30m: VOC 31, Stoch 32-33, MACD M1 34-35, M2 36-37, PVT 38,
    markers 39-41 | 2H: MACD M1 42-43, M2 44-45, VOC 46, PVT 47, markers 48-50 |
    D: MACD M1 51-52, M2 53-54, PVT 55, markers 56-58.
    """
    header, raw = _read_raw_csv(csv_path)

    def expect(idx, name):
        got = header[idx] if idx < len(header) else None
        if got != name:
            raise ValueError(
                "unexpected export layout: col %d = %r, expected %r "
                "(layout changed? update _load_bars_positional)" % (idx, got, name))

    for idx, name in ((5, "1m EMA100"), (6, "1m EMA200"), (7, "5m EMA4"), (21, "VWAP"),
                      (25, "PVT"), (38, "PVT"), (47, "PVT"), (55, "PVT")):
        expect(idx, name)

    def fv(v):
        v = (v or "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None

    bars = []
    for r in raw:
        ts = r[0].strip()
        b = {
            "date": ts[:10], "hhmm": ts[11:16] if len(ts) > 15 else "",
            "open": fv(r[1]), "high": fv(r[2]), "low": fv(r[3]), "close": fv(r[4]),
            "vwap": fv(r[21]),
            "5m_pvt": fv(r[25]), "30m_pvt": fv(r[38]), "2h_pvt": fv(r[47]), "1d_pvt": fv(r[55]),
            # MACD pairs: use M2 (24/52, the research pair);
            # M1 (12/26) NOT used by current setups - reserved for a future setup
            "5m_macd": fv(r[22]), "5m_signal": fv(r[23]),
            "30m_macd": fv(r[36]), "30m_signal": fv(r[37]),
            "30m_macd_1226": fv(r[34]), "30m_signal_1226": fv(r[35]),
            "2h_macd": fv(r[44]), "2h_signal": fv(r[45]),
            "2h_macd_1226": fv(r[42]), "2h_signal_1226": fv(r[43]),
            "1d_macd": fv(r[53]), "1d_signal": fv(r[54]),
            "1d_macd_1226": fv(r[51]), "1d_signal_1226": fv(r[52]),
            "5m_k": fv(r[29]), "5m_d": fv(r[30]),
            "30m_k": fv(r[32]), "30m_d": fv(r[33]),
            "5m_voc": fv(r[24]), "30m_voc": fv(r[31]), "2h_voc": fv(r[46]),
            "5m_ema4": fv(r[7]), "ema100_1m": fv(r[5]), "ema200_1m": fv(r[6]),
            "5m_ema9": fv(r[8]), "5m_ema21": fv(r[9]), "5m_ema50": fv(r[10]),
            "5m_ema100": fv(r[11]), "5m_ema200": fv(r[12]),
            "30m_ema50": fv(r[13]), "30m_ema100": fv(r[14]), "30m_ema200": fv(r[15]),
            "2h_ema50": fv(r[16]), "2h_ema100": fv(r[17]), "2h_ema200": fv(r[18]),
            "1d_ema100": fv(r[19]), "1d_ema200": fv(r[20]),
        }
        for tf in ("5m", "30m", "2h", "1d"):
            b["%s_pvt_sign" % tf] = sgn(b["%s_pvt" % tf])
        bars.append(b)
    return bars


# ---------------------------------------------------------------------------
# Phase 1: verification
# ---------------------------------------------------------------------------

def ema_series(values, n):
    """EMA with first-value seeding (close enough for variant identification)."""
    alpha = 2.0 / (n + 1.0)
    out = [None] * len(values)
    e = None
    for i, v in enumerate(values):
        if v is None:
            out[i] = e
            continue
        e = v if e is None else alpha * v + (1 - alpha) * e
        out[i] = e
    return out


def identify_macd_variant(bars):
    """Correlate '5m MACD Line' against self-computed 12/26 and 24/52."""
    closes = [b["close"] for b in bars]
    col = [b["5m_macd"] for b in bars]
    e12 = ema_series(closes, 12)
    e26 = ema_series(closes, 26)
    e24 = ema_series(closes, 24)
    e52 = ema_series(closes, 52)
    res = {}
    for name, macd in (("12/26", [a - b2 for a, b2 in zip(e12, e26)]),
                       ("24/52", [a - b2 for a, b2 in zip(e24, e52)])):
        pairs = [(m, c) for m, c in zip(macd[300:], col[300:]) if m is not None and c is not None]
        if len(pairs) < 100:
            res[name] = None
            continue
        n = len(pairs)
        mx = sum(p[0] for p in pairs) / n
        my = sum(p[1] for p in pairs) / n
        cov = sum((p[0] - mx) * (p[1] - my) for p in pairs)
        vx = sum((p[0] - mx) ** 2 for p in pairs)
        vy = sum((p[1] - my) ** 2 for p in pairs)
        res[name] = round(cov / ((vx * vy) ** 0.5), 4) if vx > 0 and vy > 0 else None
        res[name + "_n"] = n
    return res


def htf_liveness(bars, step_min, tf):
    """Is the HTF PVT column live (varies within its HTF bar) or last-completed?

    For each HTF window (date, stamp) collect distinct non-null values across
    member 5m bars. live windows = >1 distinct value.
    """
    from collections import defaultdict
    win = defaultdict(set)
    for b in bars:
        st = session_stamp(b["hhmm"], step_min)
        if st is None:
            continue
        v = b["%s_pvt" % tf]
        if v is not None:
            win[(b["date"], st)].add(round(v, 10))
    total = len(win)
    live = sum(1 for v in win.values() if len(v) > 1)
    return {"windows": total, "live_windows": live,
            "live_pct": round(live / float(total), 4) if total else None}


def verify(bars):
    out = {}
    dates = sorted(set(b["date"] for b in bars))
    out["rows_total"] = len(bars)
    out["date_first"], out["date_last"] = dates[0], dates[-1]

    dev = [b for b in bars if DEV_START <= b["date"] <= DEV_END]
    dev_dates = sorted(set(b["date"] for b in dev))
    out["dev_rows"] = len(dev)
    out["dev_days"] = len(dev_dates)
    out["dev_first"], out["dev_last"] = dev_dates[0], dev_dates[-1]

    # session structure
    hhmm_counts = {}
    for b in dev:
        hhmm_counts[b["hhmm"]] = hhmm_counts.get(b["hhmm"], 0) + 1
    hhmms = sorted(hhmm_counts)
    out["bar_labels_first_last"] = [hhmms[0], hhmms[-1]]
    out["distinct_bar_labels"] = len(hhmms)
    per_day = len(dev) / float(len(dev_dates))
    out["bars_per_day_avg"] = round(per_day, 1)

    # missing-value profile on dev window (first bars may be empty)
    def n_null(key):
        return sum(1 for b in dev if b.get(key) is None)
    out["null_counts"] = {
        "5m_pvt": n_null("5m_pvt"), "30m_pvt": n_null("30m_pvt"),
        "2h_pvt": n_null("2h_pvt"), "1d_pvt": n_null("1d_pvt"),
        "vwap": n_null("vwap"), "5m_ema9": n_null("5m_ema9"),
        "30m_ema50": n_null("30m_ema50"), "1d_ema100": n_null("1d_ema100"),
        "ema200_1m": n_null("ema200_1m"),
    }

    out["macd_variant_check"] = identify_macd_variant(bars)

    out["pvt_liveness"] = {
        "30m": htf_liveness(dev, 30, "30m"),
        "2h": htf_liveness(dev, 120, "2h"),
        "1d": htf_liveness(dev, 9999, "1d"),  # whole-day windows
    }

    # regime prevalence on dev window (all four signs known)
    from collections import Counter
    reg4 = Counter()
    reg3 = Counter()
    for b in dev:
        s5, s30, s2h, s1d = (b["5m_pvt_sign"], b["30m_pvt_sign"],
                             b["2h_pvt_sign"], b["1d_pvt_sign"])
        if None not in (s5, s30, s2h, s1d):
            reg4["D%s|2H%s|30%s|5%s" % (s1d > 0 and "+" or "-", s2h > 0 and "+" or "-",
                                        s30 > 0 and "+" or "-", s5 > 0 and "+" or "-")] += 1
        if None not in (s30, s2h, s1d):
            reg3["D%s|2H%s|30%s" % (s1d > 0 and "+" or "-", s2h > 0 and "+" or "-",
                                    s30 > 0 and "+" or "-")] += 1
    tot = sum(reg3.values()) or 1
    out["regime3_prevalence_pct"] = {k: round(100.0 * v / tot, 1)
                                     for k, v in sorted(reg3.items())}
    tot4 = sum(reg4.values()) or 1
    top4 = sorted(reg4.items(), key=lambda kv: -kv[1])[:12]
    out["regime4_top_pct"] = {k: round(100.0 * v / tot4, 1) for k, v in top4}
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def contiguous_runs(bars, step_min=5):
    """Split bars into day-contiguous runs (break on date change / time gap)."""
    runs, cur = [], []
    for b in bars:
        if cur:
            p = cur[-1]
            t1 = datetime.strptime(p["hhmm"], "%H:%M")
            t2 = datetime.strptime(b["hhmm"], "%H:%M")
            if b["date"] != p["date"] or (t2 - t1) != timedelta(minutes=step_min):
                runs.append(cur)
                cur = []
        cur.append(b)
    if cur:
        runs.append(cur)
    return runs


def sign_episode_lengths(bars, key):
    """Episode lengths (in bars) of constant sign for column key. {+1: [...], -1: [...]}"""
    eps = {1: [], -1: []}
    cur_sign, cur_len = None, 0
    for b in bars:
        s = b.get(key)
        if s not in (1, -1):
            if cur_sign is not None:
                eps[cur_sign].append(cur_len)
            cur_sign, cur_len = None, 0
            continue
        if s == cur_sign:
            cur_len += 1
        else:
            if cur_sign is not None:
                eps[cur_sign].append(cur_len)
            cur_sign, cur_len = s, 1
    if cur_sign is not None:
        eps[cur_sign].append(cur_len)
    return eps


def aggregate_daily(bars):
    """Daily OHLC from 5m bars. {date: {open,high,low,close,nbars}}"""
    out = {}
    for b in bars:
        d = out.setdefault(b["date"], {"open": None, "high": None, "low": None,
                                       "close": None, "nbars": 0})
        if d["open"] is None:
            d["open"] = b["open"]
        d["high"] = b["high"] if d["high"] is None else max(d["high"], b["high"])
        d["low"] = b["low"] if d["low"] is None else min(d["low"], b["low"])
        d["close"] = b["close"]
        d["nbars"] += 1
    return out


def pct(x, nd=1):
    return "%.*f%%" % (nd, 100.0 * x) if x is not None else "-"


def med(vals):
    if not vals:
        return None
    s = sorted(vals)
    return s[len(s) // 2]


def fmt_med(vals, nd=1, scale=1.0):
    v = med(vals)
    return "%.*f" % (nd, v * scale) if v is not None else "-"


# ---------------------------------------------------------------------------
# Phase 2: insight pack
# ---------------------------------------------------------------------------

REGIME_GROUP = {
    "D+|2H+|30+": "BULL", "D-|2H-|30-": "BEAR",
}


def regime_group(b):
    s = (b["1d_pvt_sign"], b["2h_pvt_sign"], b["30m_pvt_sign"])
    if None in s:
        return None
    key = "D%s|2H%s|30%s" % ("+" if s[0] > 0 else "-", "+" if s[1] > 0 else "-",
                             "+" if s[2] > 0 else "-")
    return REGIME_GROUP.get(key, "MIXED")


def t1_regime(dev):
    """Prevalence + persistence of PVT-sign regimes."""
    out = {}
    from collections import Counter
    reg3, reg4 = Counter(), Counter()
    for b in dev:
        s5, s30, s2h, s1d = (b["5m_pvt_sign"], b["30m_pvt_sign"],
                             b["2h_pvt_sign"], b["1d_pvt_sign"])
        if None not in (s30, s2h, s1d):
            reg3["D%s|2H%s|30%s" % tuple("+" if x > 0 else "-" for x in (s1d, s2h, s30))] += 1
        if None not in (s5, s30, s2h, s1d):
            reg4["D%s|2H%s|30%s|5%s" % tuple("+" if x > 0 else "-" for x in (s1d, s2h, s30, s5))] += 1
    tot3 = sum(reg3.values()) or 1
    tot4 = sum(reg4.values()) or 1
    out["prevalence_3tf"] = {k: {"pct": round(100.0 * v / tot3, 1), "n": v}
                             for k, v in sorted(reg3.items(), key=lambda kv: -kv[1])}
    out["prevalence_4tf"] = {k: {"pct": round(100.0 * v / tot4, 1), "n": v}
                             for k, v in sorted(reg4.items(), key=lambda kv: -kv[1])}

    # persistence: episode lengths of constant sign per TF
    eps = {}
    for tf, key in (("5m", "5m_pvt_sign"), ("30m", "30m_pvt_sign"),
                    ("2h", "2h_pvt_sign")):
        e = sign_episode_lengths(dev, key)
        eps[tf] = {sg: {"n": len(e[sg]), "median_bars": med(e[sg]),
                        "median_hours": round(med(e[sg]) * 5.0 / 60.0, 1) if e[sg] else None}
                   for sg in (1, -1)}
    from collections import OrderedDict
    day_sign = OrderedDict()
    for b in dev:
        if b["1d_pvt_sign"] in (1, -1):
            day_sign[b["date"]] = b["1d_pvt_sign"]
    dvals = list(day_sign.values())
    same = sum(1 for i in range(1, len(dvals)) if dvals[i] == dvals[i - 1])
    eps["1d"] = {
        "days": len(dvals),
        "sign_flips": len(dvals) - 1 - same,
        "p_sign_same_next_day": round(same / float(len(dvals) - 1), 3) if len(dvals) > 1 else None,
        "pct_days_D+": round(100.0 * sum(1 for v in dvals if v > 0) / len(dvals), 1) if dvals else None,
    }
    out["persistence"] = eps
    return out


def t2_anatomy(dev):
    """5m trend anatomy inside BULL / BEAR aligned regimes."""
    out = {"definition": "BULL = D+|2H+|30+; BEAR = D-|2H-|30-; events need 5m PVT sign known"}

    # streaks of same-direction closes within contiguous aligned runs
    streaks = {"BULL": {1: [], -1: []}, "BEAR": {1: [], -1: []}}
    bar_char = {"BULL": {1: [], -1: []}, "BEAR": {1: [], -1: []}}  # dir -> ranges/|body|
    for run in contiguous_runs(dev):
        for grp in ("BULL", "BEAR"):
            bars = [b for b in run if regime_group(b) == grp]
            cur_dir, cur_len = 0, 0
            for i, b in enumerate(bars):
                if i == 0 or bars[i - 1]["close"] is None or b["close"] is None:
                    d = 0
                else:
                    d = sgn(b["close"] - bars[i - 1]["close"])
                if d in (1, -1):
                    bar_char[grp][d].append(b)
                if d == 0:
                    if cur_dir in (1, -1):
                        streaks[grp][cur_dir].append(cur_len)
                    cur_dir, cur_len = 0, 0
                elif d == cur_dir:
                    cur_len += 1
                else:
                    if cur_dir in (1, -1):
                        streaks[grp][cur_dir].append(cur_len)
                    cur_dir, cur_len = d, 1
            if cur_dir in (1, -1):
                streaks[grp][cur_dir].append(cur_len)

    def streak_table(grp):
        r = {}
        for d, name in ((1, "with-trend"), (-1, "counter")):
            vals = streaks[grp][d]
            tot = float(len(vals)) or 1
            r[name] = {
                "n": len(vals),
                "p_len1": round(sum(1 for v in vals if v == 1) / tot, 3),
                "p_len2-3": round(sum(1 for v in vals if 2 <= v <= 3) / tot, 3),
                "p_len4+": round(sum(1 for v in vals if v >= 4) / tot, 3),
                "median": med(vals),
            }
        return r

    out["streaks"] = {"BULL": streak_table("BULL"), "BEAR": streak_table("BEAR")}

    def wick_stats(grp):
        r = {}
        for d, name in ((1, "with-trend"), (-1, "counter")):
            bars = bar_char[grp][d]
            if not bars:
                r[name] = None
                continue
            ranges = [b["high"] - b["low"] for b in bars if b["high"] and b["low"]]
            bodies = [abs(b["close"] - b["open"]) for b in bars]
            r[name] = {
                "n_bars": len(bars),
                "median_range": round(med(ranges), 2) if ranges else None,
                "median_body": round(med(bodies), 2),
                "body_over_range": round(med(bodies) / med(ranges), 2) if ranges and med(ranges) else None,
            }
        return r

    out["bar_character"] = {"BULL": wick_stats("BULL"), "BEAR": wick_stats("BEAR")}

    # dip/rally episodes within aligned regime:
    #   BULL dip  : episode starts when 5m close crosses below level from above;
    #               resume = close back above level within 6 bars (30 min)
    #   BEAR rally: episode starts when 5m close crosses above level from below;
    #               resume = close back below level within 6 bars
    # Only the first bar of each episode counts (no double-counting).
    def episode_stats(grp, level_key, cross_dir, window=6):
        # cross_dir = -1: dip below (BULL), +1: rally above (BEAR)
        n = 0
        resumed = 0
        depths = []
        resumes_in = []
        for run in contiguous_runs(dev):
            in_ep = False
            ep_i = -1
            ep_lv = None
            for i, b in enumerate(run):
                lv = b.get(level_key)
                if lv is None or b["close"] is None:
                    continue
                below = b["close"] < lv
                if not in_ep:
                    if cross_dir < 0 and below:
                        in_ep, ep_i, ep_lv = True, i, lv
                        n += 1
                        depths.append(lv - b["low"] if b["low"] is not None else None)
                    elif cross_dir > 0 and not below:
                        in_ep, ep_i, ep_lv = True, i, lv
                        n += 1
                        depths.append(b["high"] - lv if b["high"] is not None else None)
                else:
                    done = (cross_dir < 0 and b["close"] > ep_lv) or \
                           (cross_dir > 0 and b["close"] < ep_lv)
                    if done:
                        resumes_in.append(i - ep_i)
                        resumed += 1
                        in_ep = False
                    elif i - ep_i >= window:
                        in_ep = False  # episode did not resume within window
        return {"n": n,
                "p_resume_in_%dbars" % window: round(resumed / float(n), 3) if n else None,
                "median_resume_bars": med(resumes_in),
                "median_depth_pts": round(med([d for d in depths if d is not None]), 1)
                    if depths else None}

    out["dips"] = {
        "BULL_dip_below_5mEMA21": episode_stats("BULL", "5m_ema21", -1),
        "BULL_dip_below_VWAP": episode_stats("BULL", "vwap", -1),
        "BEAR_rally_above_5mEMA21": episode_stats("BEAR", "5m_ema21", +1),
        "BEAR_rally_above_VWAP": episode_stats("BEAR", "vwap", +1),
    }

    # extension: close distance from 5m EMA21 within aligned regimes
    for grp, sg in (("BULL", 1), ("BEAR", -1)):
        dists = [sg * (b["close"] - b["5m_ema21"]) for b in dev
                 if regime_group(b) == grp and b.get("5m_ema21") is not None and b["close"]]
        if dists:
            dists.sort()
            out.setdefault("extension_vs_5mEMA21_pts", {})[grp] = {
                "n": len(dists),
                "p25": round(dists[len(dists) // 4], 1),
                "median": round(dists[len(dists) // 2], 1),
                "p75": round(dists[3 * len(dists) // 4], 1),
                "p90": round(dists[9 * len(dists) // 10], 1),
            }
    return out


BUCKETS = [("09:15", "10:15"), ("10:15", "11:15"), ("11:15", "12:15"),
           ("12:15", "13:15"), ("13:15", "14:15"), ("14:15", "15:30")]


def t3_timeofday(dev):
    """Per-hour drift/vol/efficiency by regime group + opening-range behaviour."""
    out = {"buckets": {}, "opening_range": {}}
    rets = {g: {i: [] for i in range(len(BUCKETS))} for g in ("BULL", "BEAR", "MIXED")}
    for run in contiguous_runs(dev):
        for i, b in enumerate(run):
            g = regime_group(b)
            if g is None or i == 0:
                continue
            r = b["close"] - run[i - 1]["close"]
            if r is None:
                continue
            for bi, (t0, t1) in enumerate(BUCKETS):
                if t0 <= b["hhmm"] < t1:
                    rets[g][bi].append(r)
                    break
    for g in ("BULL", "BEAR", "MIXED"):
        out["buckets"][g] = {}
        for bi, (t0, t1) in enumerate(BUCKETS):
            rs = rets[g][bi]
            if not rs:
                continue
            net = sum(rs)
            absum = sum(abs(r) for r in rs)
            out["buckets"][g][t0 + "-" + t1] = {
                "n_bars": len(rs),
                "mean_5m_move_pts": round(net / len(rs), 3),
                "efficiency": round(net / absum, 3) if absum else None,
            }

    # opening range: first 6 bars (09:15..09:40); breakout scanned from 09:45
    or_days = []
    for run in contiguous_runs(dev):
        if len(run) < 20:
            continue
        orbars = [b for b in run if b["hhmm"] < "09:45"]
        post = [b for b in run if b["hhmm"] >= "09:45"]
        if len(orbars) != 6 or not post:
            continue
        orh = max(b["high"] for b in orbars)
        orl = min(b["low"] for b in orbars)
        g45 = regime_group(run[6])
        brk = None
        for b in post:
            up = b["high"] >= orh
            dn = b["low"] <= orl
            if up and dn:
                brk = "BOTH" if brk is None else brk
            elif up:
                brk = "UP" if brk is None else brk
                break
            elif dn:
                brk = "DOWN" if brk is None else brk
                break
        close = run[-1]["close"]
        or_days.append({"orh": orh, "orl": orl, "brk": brk, "close": close, "grp": g45})
    for g in ("BULL", "BEAR", "MIXED"):
        ds = [d for d in or_days if d["grp"] == g]
        up = [d for d in ds if d["brk"] == "UP"]
        dn = [d for d in ds if d["brk"] == "DOWN"]
        out["opening_range"][g] = {
            "n_days": len(ds),
            "p_first_break_UP": round(len(up) / float(len(ds)), 3) if ds else None,
            "p_first_break_DOWN": round(len(dn) / float(len(ds)), 3) if ds else None,
            "p_close_beyond_OR_after_UP_break": round(
                sum(1 for d in up if d["close"] > d["orh"]) / float(len(up)), 3) if up else None,
            "p_close_beyond_OR_after_DOWN_break": round(
                sum(1 for d in dn if d["close"] < d["orl"]) / float(len(dn)), 3) if dn else None,
        }
    return out


LEVEL_FAMILIES = ["VWAP", "PDH", "PDC", "PDL", "PDO", "Round100"]


def t4_levels(dev, daily):
    """5m touch/reject/hold by level family x regime group."""
    from forecasting.store_builder import round100_resistance, round100_support
    dates = sorted(daily)
    prev_of = {dates[i]: (dates[i - 1] if i > 0 else None) for i in range(len(dates))}
    stats = {side: {f: {g: [0, 0] for g in ("BULL", "BEAR", "MIXED")}
                    for f in LEVEL_FAMILIES} for side in ("resist", "support")}
    for b in dev:
        g = regime_group(b)
        if g is None:
            continue
        p = prev_of.get(b["date"])
        pd_ = daily[p] if p else None
        lv = {
            "VWAP": b.get("vwap"),
            "PDH": pd_ and pd_["high"], "PDC": pd_ and pd_["close"],
            "PDL": pd_ and pd_["low"], "PDO": pd_ and pd_["open"],
        }
        o, h, l, c = b["open"], b["high"], b["low"], b["close"]
        if None in (o, h, l, c):
            continue
        for f in LEVEL_FAMILIES:
            if f == "Round100":
                lr = round100_resistance(o)
                ls = round100_support(o)
            else:
                lr = ls = lv[f]
            if lr is not None and o < lr and h >= lr:
                stats["resist"][f][g][0] += 1
                if c < lr:
                    stats["resist"][f][g][1] += 1
            if ls is not None and o > ls and l <= ls:
                stats["support"][f][g][0] += 1
                if c > ls:
                    stats["support"][f][g][1] += 1
    out = {"definition": "resistance: open<L, high>=L, rejected if close<L; "
                         "support: open>L, low<=L, held if close>L; 5m bars, dev window"}
    for side in ("resist", "support"):
        out[side] = {f: {g: {"n_touch": v[0],
                             ("rej_rate" if side == "resist" else "hold_rate"):
                                 round(v[1] / float(v[0]), 3) if v[0] else None}
                         for g, v in gs.items()}
                     for f, gs in stats[side].items()}
    return out


def t5_daytypes(dev, daily):
    """Continuation vs reversal days + gap behaviour."""
    dates = sorted(daily)
    out = {"definition": "CONT = today's direction matches prev-day D-PVT sign; "
                         "REV = opposite; CLV = (C-L)/(H-L)"}
    rows = {"CONT": {"range": [], "clv": [], "morning_agree": [0, 0], "n": 0},
            "REV": {"range": [], "clv": [], "morning_agree": [0, 0], "n": 0}}
    gap = {"CONT": {"gaps": [], "filled": [0, 0]}, "REV": {"gaps": [], "filled": [0, 0]}}
    for i in range(1, len(dates)):
        d = dates[i]
        p = daily[dates[i - 1]]
        cur = daily[d]
        if cur["nbars"] < 60:
            continue
        dsign = None
        for b in dev:
            if b["date"] == d and b["1d_pvt_sign"] in (1, -1):
                dsign = b["1d_pvt_sign"]
                break
        if dsign is None:
            continue
        direction = 1 if cur["close"] > cur["open"] else -1
        typ = "CONT" if direction == dsign else "REV"
        rng = cur["high"] - cur["low"]
        clv = (cur["close"] - cur["low"]) / rng if rng else None
        rows[typ]["n"] += 1
        rows[typ]["range"].append(rng)
        if clv is not None:
            rows[typ]["clv"].append(clv)
        morning = [b for b in dev if b["date"] == d and b["hhmm"] <= "10:15"]
        if morning:
            mdir = 1 if morning[-1]["close"] > cur["open"] else -1
            rows[typ]["morning_agree"][0] += 1
            if mdir == direction:
                rows[typ]["morning_agree"][1] += 1
        g = cur["open"] - p["close"]
        if abs(g) > 0:
            gap[typ]["gaps"].append(g)
            filled = (cur["low"] <= p["close"] <= cur["high"])
            gap[typ]["filled"][0] += 1
            if filled:
                gap[typ]["filled"][1] += 1
    out["day_types"] = {}
    for typ in ("CONT", "REV"):
        r = rows[typ]
        out["day_types"][typ] = {
            "n_days": r["n"],
            "median_range_pts": round(med(r["range"]), 1) if r["range"] else None,
            "median_CLV": round(med(r["clv"]), 2) if r["clv"] else None,
            "p_morning_dir_matches_close": round(r["morning_agree"][1] / float(r["morning_agree"][0]), 3)
                if r["morning_agree"][0] else None,
            "gap_median_pts": round(med(gap[typ]["gaps"]), 1) if gap[typ]["gaps"] else None,
            "p_gap_filled": round(gap[typ]["filled"][1] / float(gap[typ]["filled"][0]), 3)
                if gap[typ]["filled"][0] else None,
        }
    return out


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_md(res, dev_first, dev_last):
    L = []
    A = L.append
    A("# Trend / Price-Action Insight Pack")
    A("")
    A("- **Dev window**: %s .. %s (out-of-sample Apr 2026+ held out)" % (dev_first, dev_last))
    A("- **Source**: 5m TradingView export with embedded 30m/2H/D indicator state")
    A("- **Semantics (verified)**: MACD pairs are the 24/52 variant; HTF PVT columns are "
      "last-completed values (constant within each HTF bar) -> regime = last confirmed "
      "HTF trend, no intrabar repaint. D-PVT on day T = previous day's final PVT.")
    A("- Every number carries n. Cells with tiny n are not evidence.")
    A("")
    A("## T1 - Regime prevalence & persistence")
    A("")
    A("### 3-TF regime (%% of 5m bars, dev window)")
    A("")
    A("| Regime | %% | n |")
    A("|---|---|---|")
    for k, v in res["t1"]["prevalence_3tf"].items():
        A("| %s | %.1f | %d |" % (k, v["pct"], v["n"]))
    A("")
    A("### Persistence (PVT-sign episodes)")
    A("")
    A("| TF | Sign | Episodes | Median duration |")
    A("|---|---|---|---|")
    for tf, e in res["t1"]["persistence"].items():
        if tf == "1d":
            A("| D | flips: %d of %d day-pairs | p(same sign next day): %s | D+ share: %s%% |" % (
                e["sign_flips"], e["days"], e["p_sign_same_next_day"], e["pct_days_D+"]))
            continue
        for sg, lbl in ((1, "+"), (-1, "-")):
            v = e[sg]
            A("| %s | %s | %d | %s bars (~%s h) |" % (tf, lbl, v["n"], v["median_bars"], v["median_hours"]))
    A("")
    A("## T2 - Trend anatomy (5m)")
    A("")
    for grp in ("BULL", "BEAR"):
        A("### %s" % grp)
        A("")
        st = res["t2"]["streaks"][grp]
        A("- Streaks of same-direction closes: with-trend n=%s, P(len1)=%s, P(len2-3)=%s, P(len4+)=%s; "
          "counter n=%s, P(len1)=%s" % (
              st["with-trend"]["n"], pct(st["with-trend"]["p_len1"], 0),
              pct(st["with-trend"]["p_len2-3"], 0), pct(st["with-trend"]["p_len4+"], 0),
              st["counter"]["n"], pct(st["counter"]["p_len1"], 0)))
        bc = res["t2"]["bar_character"][grp]
        A("- Bar character: with-trend body/range=%s (n=%s); counter body/range=%s (n=%s)" % (
            bc["with-trend"]["body_over_range"], bc["with-trend"]["n_bars"],
            bc["counter"]["body_over_range"], bc["counter"]["n_bars"]))
        A("")
    A("### Dip / resume (within aligned regime)")
    A("")
    A("| Event | n | P(resume in 6 bars = 30 min) | Median bars to resume | Median depth (pts) |")
    A("|---|---|---|---|---|")
    for k, v in res["t2"]["dips"].items():
        A("| %s | %d | %s | %s | %s |" % (k, v["n"],
                                          pct(v.get("p_resume_in_6bars")),
                                          v.get("median_resume_bars"),
                                          v.get("median_depth_pts")))
    A("")
    A("### Extension from 5m EMA21 (pts, signed with trend)")
    A("")
    A("| Group | n | P25 | Median | P75 | P90 |")
    A("|---|---|---|---|---|---|")
    for g, v in res["t2"].get("extension_vs_5mEMA21_pts", {}).items():
        A("| %s | %d | %s | %s | %s | %s |" % (g, v["n"], v["p25"], v["median"], v["p75"], v["p90"]))
    A("")
    A("## T3 - Time-of-day structure (per-5m-bar stats by hour bucket)")
    A("")
    A("| Group | Bucket | n | Mean 5m move (pts) | Efficiency (net/sum abs) |")
    A("|---|---|---|---|---|")
    for g, buckets in res["t3"]["buckets"].items():
        for bname, v in buckets.items():
            A("| %s | %s | %d | %+.3f | %s |" % (g, bname, v["n_bars"],
                                                 v["mean_5m_move_pts"], v["efficiency"]))
    A("")
    A("### Opening range (09:15-09:45) breakout follow-through")
    A("")
    A("| Group | Days | P(first break UP) | P(close>ORH after UP) | P(close<ORL after DOWN) |")
    A("|---|---|---|---|---|")
    for g, v in res["t3"]["opening_range"].items():
        A("| %s | %d | %s | %s | %s |" % (g, v["n_days"], pct(v["p_first_break_UP"]),
                                          pct(v["p_close_beyond_OR_after_UP_break"]),
                                          pct(v["p_close_beyond_OR_after_DOWN_break"])))
    A("")
    A("## T4 - Level touch behaviour by regime (5m bars)")
    A("")
    for side, label, rate in (("resist", "Resistance touches (reject rate)", "rej_rate"),
                              ("support", "Support touches (hold rate)", "hold_rate")):
        A("### %s" % label)
        A("")
        A("| Level | BULL | BEAR | MIXED |")
        A("|---|---|---|---|")
        for f in LEVEL_FAMILIES:
            cells = []
            for g in ("BULL", "BEAR", "MIXED"):
                v = res["t4"][side][f][g]
                cells.append("%s [n=%d]" % (pct(v[rate]), v["n_touch"]))
            A("| %s | %s |" % (f, " | ".join(cells)))
        A("")
    A("## T5 - Day types: continuation vs reversal")
    A("")
    A("| Metric | CONT days | REV days |")
    A("|---|---|---|")
    c = res["t5"]["day_types"]["CONT"]
    r = res["t5"]["day_types"]["REV"]
    for name, key in (("n days", "n_days"), ("median range (pts)", "median_range_pts"),
                      ("median CLV", "median_CLV"),
                      ("P(morning dir = close dir)", "p_morning_dir_matches_close"),
                      ("median gap (pts)", "gap_median_pts"), ("P(gap filled)", "p_gap_filled")):
        A("| %s | %s | %s |" % (name, c.get(key), r.get(key)))
    A("")
    return "\n".join(L)


def build(csv_path=DEFAULT_CSV, out_dir=DEFAULT_OUT_DIR):
    bars = load_bars(csv_path)
    dev = [b for b in bars if DEV_START <= b["date"] <= DEV_END]
    daily = aggregate_daily(dev)
    res = {
        "t1": t1_regime(dev),
        "t2": t2_anatomy(dev),
        "t3": t3_timeofday(dev),
        "t4": t4_levels(dev, daily),
        "t5": t5_daytypes(dev, daily),
        "meta": {"dev_rows": len(dev), "dev_days": len(set(b["date"] for b in dev)),
                 "dev_first": min(b["date"] for b in dev),
                 "dev_last": max(b["date"] for b in dev)},
    }
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "trend_insight.json")
    with open(json_path, "w") as f:
        json.dump(res, f, indent=1)
    md_path = os.path.join(out_dir, "trend_insight.md")
    with open(md_path, "w") as f:
        f.write(render_md(res, res["meta"]["dev_first"], res["meta"]["dev_last"]))
    print("Wrote %s" % json_path)
    print("Wrote %s" % md_path)
    return res


# ---------------------------------------------------------------------------
# Ad-hoc queries
# ---------------------------------------------------------------------------

def q_2h_uptrend_by_d_context(dev):
    """2H uptrend (PVT>0 / MACD>Signal) survival by D-trend context at episode start.

    Scenarios (D state on the day the 2H up-episode starts):
      D-start : D trend just turned up (previous trading day was down)
      D-cont  : D trend already up the previous day too
      D-down  : D trend is down
    Column semantics (verified): 2H PVT is carried on every 5m bar as the last
    COMPLETED 2H value. 2H/D MACD+Signal are populated once per HTF bar, on its
    closing 5m bar (2H: 11:10/13:10/15:10/15:25; D: 15:25). So:
      - PVT episodes: runs of the carried sign on the 5m grid.
      - MACD episodes: runs of window-close states on the 4-windows/day grid,
        where window w's known state = previous window's closing value.
    """
    dates = sorted(set(b["date"] for b in dev))
    date_idx = {d: i for i, d in enumerate(dates)}
    last_bar_of_day = {}
    for i, b in enumerate(dev):
        last_bar_of_day[b["date"]] = i

    # --- daily states ---
    d_sign, d_macd_up = {}, {}
    for b in dev:
        if b["1d_pvt_sign"] in (1, -1):
            d_sign[b["date"]] = b["1d_pvt_sign"]
        if b["hhmm"] == "15:25" and b.get("1d_macd") is not None and b.get("1d_signal") is not None:
            d_macd_up[b["date"]] = b["1d_macd"] > b["1d_signal"]

    def d_state(d, use_macd):
        i = date_idx[d]
        if use_macd:
            if i < 2:
                return None
            s, ps = d_macd_up.get(dates[i - 1]), d_macd_up.get(dates[i - 2])
            if s is None or ps is None:
                return None
            if s:
                return "D-start" if not ps else "D-cont"
            return "D-down"
        if i == 0:
            return None
        s, ps = d_sign.get(d), d_sign.get(dates[i - 1])
        if s is None or ps is None:
            return None
        if s > 0:
            return "D-start" if ps < 0 else "D-cont"
        return "D-down"

    # --- episode grids ---
    def pvt_episodes():
        eps, cur = [], None
        for i, b in enumerate(dev):
            up = b["2h_pvt_sign"] == 1
            if up and cur is None:
                cur = i
            elif not up and cur is not None:
                eps.append((cur, i - 1))
                cur = None
        if cur is not None:
            eps.append((cur, len(dev) - 1))
        return eps

    closes = [b for b in dev if b["hhmm"] in ("11:10", "13:10", "15:10", "15:25")
              and b.get("2h_macd") is not None]
    close_dates = [b["date"] for b in closes]

    def close_episodes(key_fn):
        eps, cur = [], None
        for i, b in enumerate(closes):
            if key_fn(b):
                if cur is None:
                    cur = i
            elif cur is not None:
                eps.append((cur, i - 1))
                cur = None
        if cur is not None:
            eps.append((cur, len(closes) - 1))
        return eps

    def summarize(rows, unit_hours):
        out = {}
        for st, r in rows.items():
            m = med(r["durs"])
            out[st] = {
                "n": r["n"],
                "median_duration_units": m,
                "median_duration_hours": round(m * unit_hours, 1) if m else None,
                "p_alive_end_of_start_day": round(r["eod"][1] / float(r["eod"][0]), 3) if r["eod"][0] else None,
                "p_alive_+1d": round(r["d1"][1] / float(r["d1"][0]), 3) if r["d1"][0] else None,
                "p_alive_+2d": round(r["d2"][1] / float(r["d2"][0]), 3) if r["d2"][0] else None,
                "p_alive_+3d": round(r["d3"][1] / float(r["d3"][0]), 3) if r["d3"][0] else None,
            }
        return out

    def collect(eps, unit_hours, state_of_start, alive_at):
        rows = {}
        for start, end in eps:
            st = state_of_start(start)
            if st is None:
                continue
            r = rows.setdefault(st, {"n": 0, "durs": [],
                                     "eod": [0, 0], "d1": [0, 0], "d2": [0, 0], "d3": [0, 0]})
            r["n"] += 1
            r["durs"].append(end - start + 1)
            alive_checks = alive_at(start)
            for key, is_alive in alive_checks:
                r[key][0] += 1
                if is_alive:
                    r[key][1] += 1
        return summarize(rows, unit_hours)

    # PVT: 5m-grid episodes
    def pvt_alive_at(start_i):
        d0 = dev[start_i]["date"]
        di = date_idx[d0]
        checks = [("eod", dev[last_bar_of_day[d0]]["2h_pvt_sign"] == 1)]
        for k, key in ((1, "d1"), (2, "d2"), (3, "d3")):
            if di + k < len(dates):
                checks.append((key, dev[last_bar_of_day[dates[di + k]]]["2h_pvt_sign"] == 1))
        return checks

    pvt_rows = collect(
        [(s, e) for s, e in pvt_episodes() if date_idx[dev[s]["date"]] < len(dates) - 1],
        5.0 / 60.0, lambda i: d_state(dev[i]["date"], use_macd=False), pvt_alive_at)

    # MACD: window-close-grid episodes
    def close_alive_at(close_i):
        d0 = close_dates[close_i]
        di = date_idx[d0]
        checks = []
        for k, key in ((0, "eod"), (1, "d1"), (2, "d2"), (3, "d3")):
            if di + k < len(dates):
                ld = dates[di + k]
                idxs = [i for i, dd in enumerate(close_dates) if dd == ld]
                if idxs:
                    checks.append((key, key_fn_closes(closes[idxs[-1]])))
        return checks

    def state_close(i):
        return d_state(close_dates[i], use_macd=True)

    macd_up_c = lambda b: b["2h_macd"] > b["2h_signal"]
    key_fn_closes = macd_up_c
    macd_rows = collect(
        [(s, e) for s, e in close_episodes(macd_up_c) if date_idx[close_dates[s]] < len(dates) - 1],
        2.0, state_close, close_alive_at)

    combined_c = lambda b: b["2h_pvt_sign"] == 1 and b["2h_macd"] > b["2h_signal"]
    key_fn_closes = combined_c
    comb_rows = collect(
        [(s, e) for s, e in close_episodes(combined_c) if date_idx[close_dates[s]] < len(dates) - 1],
        2.0, lambda i: d_state(close_dates[i], use_macd=False), close_alive_at)

    return {
        "definition": "2H uptrend: PVT = carried last-completed 2H PVT sign on the 5m grid; "
                      "MACD = 2H MACD_2452>Signal at window closes (4 windows/day; 1 window = 2h). "
                      "alive = condition true at the day's last observation; episodes starting on "
                      "the final dev day excluded (censored). Combined = window-close MACD up AND "
                      "carried PVT+ (PVT lags MACD by one window).",
        "pvt": pvt_rows,
        "macd": macd_rows,
        "pvt_and_macd": comb_rows,
    }


# ---------------------------------------------------------------------------
# Query: 5m PVT cross-up context & follow-through, by HTF alignment
# ---------------------------------------------------------------------------

def q_5m_crossup_context(dev):
    """5m PVT cross-up events (sign -1 -> +1, same-day contiguous bars),
    grouped by HTF state at the event; context (where price is) + forward move."""
    # forward-fill MACD up-states (values only populated at HTF window closes)
    last2h, last30 = None, None
    for b in dev:
        if b.get("2h_macd") is not None and b.get("2h_signal") is not None:
            last2h = b["2h_macd"] > b["2h_signal"]
        if b.get("30m_macd") is not None and b.get("30m_signal") is not None:
            last30 = b["30m_macd"] > b["30m_signal"]
        b["ff_2h_macd_up"] = last2h
        b["ff_30m_macd_up"] = last30

    events = []
    day_open_map = {}
    for b in dev:
        if b["date"] not in day_open_map and b["open"] is not None:
            day_open_map[b["date"]] = b["open"]
    for run in contiguous_runs(dev):
        for i in range(1, len(run)):
            p, b = run[i - 1], run[i]
            if p["5m_pvt_sign"] == -1 and b["5m_pvt_sign"] == 1:
                b["swing_hi_1h"] = max(x["high"] for x in run[max(0, i - 12):i]
                                       if x["high"] is not None) if i >= 1 else None
                b["fwd"] = {}
                for k, key in ((3, "r15m"), (6, "r30m"), (12, "r60m")):
                    b["fwd"][key] = (run[i + k]["close"] - b["close"]) \
                        if i + k < len(run) and b["close"] is not None else None
                b["fwd"]["r_eod"] = (run[-1]["close"] - b["close"]) if b["close"] is not None else None
                events.append(b)

    def grp(b):
        g30, g2h = b["30m_pvt_sign"] == 1, b["2h_pvt_sign"] == 1
        m30, m2h = b["ff_30m_macd_up"], b["ff_2h_macd_up"]
        if g30 and g2h and m30 and m2h:
            return "A_30+2H PVT&MACD up"
        if g30 and g2h:
            return "B_30+2H PVT up"
        if g30 or g2h:
            return "C_one TF up"
        return "D_none up"

    TIME_BUCKETS = [("09:15", "10:15"), ("10:15", "12:15"), ("12:15", "14:15"), ("14:15", "15:30")]

    def stats(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        pos = sum(1 for v in vals if v > 0)
        return {"n": len(vals), "p_pos": round(pos / float(len(vals)), 3),
                "median_pts": round(med(vals), 2), "mean_pts": round(sum(vals) / len(vals), 2)}

    out = {"definition": "5m PVT cross-up = 5m PVT sign -1 -> +1 on contiguous same-day bars. "
                         "HTF state = carried last-completed PVT signs + forward-filled MACD_2452 "
                         "states at the event bar. fwd = close-to-close from event bar, same day."}
    groups = {}
    for b in events:
        groups.setdefault(grp(b), []).append(b)

    for g, evs in sorted(groups.items()):
        n = len(evs)
        above_vwap = sum(1 for b in evs if b.get("vwap") is not None and b["close"] > b["vwap"])
        above_e9 = sum(1 for b in evs if b.get("5m_ema9") is not None and b["close"] > b["5m_ema9"])
        above_e21 = sum(1 for b in evs if b.get("5m_ema21") is not None and b["close"] > b["5m_ema21"])
        above_e50 = sum(1 for b in evs if b.get("5m_ema50") is not None and b["close"] > b["5m_ema50"])
        above_open = sum(1 for b in evs if b["close"] is not None
                         and b["close"] > day_open_map.get(b["date"], None))
        pulls = [b["swing_hi_1h"] - b["close"] for b in evs
                 if b.get("swing_hi_1h") is not None and b["close"] is not None]
        tod = {}
        for t0, t1 in TIME_BUCKETS:
            tod["%s-%s" % (t0, t1)] = sum(1 for b in evs if t0 <= b["hhmm"] < t1)
        out[g] = {
            "n_events": n,
            "context": {
                "pct_above_VWAP": round(100.0 * above_vwap / n, 1) if n else None,
                "pct_above_5mEMA9": round(100.0 * above_e9 / n, 1) if n else None,
                "pct_above_5mEMA21": round(100.0 * above_e21 / n, 1) if n else None,
                "pct_above_5mEMA50": round(100.0 * above_e50 / n, 1) if n else None,
                "median_dist_to_5mEMA9_pts": stats([b["close"] - b["5m_ema9"]
                                                    for b in evs if b.get("5m_ema9") is not None]),
                "median_pullback_from_1h_swing_high_pts": round(med(pulls), 1) if pulls else None,
                "time_of_day_counts": tod,
            },
            "follow_through": {k: stats([b["fwd"][k] for b in evs])
                               for k in ("r15m", "r30m", "r60m", "r_eod")},
        }
    return out


# ---------------------------------------------------------------------------
# Query: 1m EMA price action joined to 5m-file HTF regime (by date+time)
# ---------------------------------------------------------------------------

def load_1m_bars(csv_path):
    """Load a 1m TradingView export with named 1m EMA columns (e.g. 1_5fb3d)."""
    rows = read_tv_csv(csv_path)
    bars = []
    for r in rows:
        if not r.get("date") or not r.get("hhmm"):
            continue
        b = {
            "date": r["date"], "hhmm": r["hhmm"],
            "open": fnum(r, "open"), "high": fnum(r, "high"),
            "low": fnum(r, "low"), "close": fnum(r, "close"),
            "vwap": fnum(r, "VWAP"), "pvt": fnum(r, "PVT"),
        }
        for e in ("1m EMA9", "1m EMA21", "1m EMA50", "1m EMA100", "1m EMA200"):
            b[e] = fnum(r, e)
        # Stochastic + PVT: prefer explicit 1m-named columns (new export),
        # fall back to generic names (old export, where K/D was 5m-cadence)
        b["K"] = fnum(r, "1m K") if "1m K" in r else fnum(r, "K")
        b["D"] = fnum(r, "1m D") if "1m D" in r else fnum(r, "D")
        pvt1 = fnum(r, "1m PVT") if "1m PVT" in r else None
        b["pvt"] = pvt1 if pvt1 is not None else fnum(r, "PVT")
        b["pvt_sign"] = sgn(b["pvt"])
        b["macd"] = fnum(r, "1m MACD Line")
        b["signal"] = fnum(r, "1m Signal Line")
        bars.append(b)
    return bars


def q_1m_ema_vs_htf(bars_5m, bars_1m):
    """Price action around 1m EMAs, grouped by the containing 5m bar's HTF regime.

    Regime comes from the 5m export (2H/30m PVT signs, carried values); each 1m
    bar is mapped to its containing 5m bar via (date, 5m stamp).
    """
    reg = {}
    for b in bars_5m:
        reg[(b["date"], b["hhmm"])] = (b["30m_pvt_sign"], b["2h_pvt_sign"])

    joined = []
    for b in bars_1m:
        st = session_stamp(b["hhmm"], 5)
        key = (b["date"], st) if st else (b["date"], None)
        s = reg.get(key)
        if not s or None in s:
            continue
        b["grp"] = "UP" if (s[0] == 1 and s[1] == 1) else \
                   ("DOWN" if (s[0] == -1 and s[1] == -1) else "MIXED")
        joined.append(b)

    EMAS = ["1m EMA9", "1m EMA21", "1m EMA50", "1m EMA100", "1m EMA200"]
    out = {
        "definition": "1m bars mapped to containing-5m regime (2H/30m PVT signs from the "
                      "5m export). touch/resist: open<L, high>=L, reject if close<L. "
                      "touch/support: open>L, low<=L, hold if close>L.",
        "n_1m_bars_by_group": {},
    }
    for g in ("UP", "DOWN", "MIXED"):
        out["n_1m_bars_by_group"][g] = sum(1 for b in joined if b["grp"] == g)

    for g in ("UP", "DOWN", "MIXED"):
        grp_bars = [b for b in joined if b["grp"] == g]
        if not grp_bars:
            continue
        sec = {}
        for e in EMAS:
            rj = [0, 0]  # resistance touches, rejected
            hd = [0, 0]  # support touches, held
            for b in grp_bars:
                lv, o, h, l, c = b[e], b["open"], b["high"], b["low"], b["close"]
                if None in (lv, o, h, l, c):
                    continue
                if o < lv and h >= lv:
                    rj[0] += 1
                    if c < lv:
                        rj[1] += 1
                if o > lv and l <= lv:
                    hd[0] += 1
                    if c > lv:
                        hd[1] += 1
            sec[e] = {
                "resist_touch_reject": [rj[0], round(rj[1] / float(rj[0]), 2) if rj[0] else None],
                "support_touch_hold": [hd[0], round(hd[1] / float(hd[0]), 2) if hd[0] else None],
            }

        # where do 1m PVT cross-ups fire (which EMA band is price in)?
        bands = {"<EMA200": [0, 0], "EMA100-200": [0, 0], "EMA50-100": [0, 0],
                 "EMA21-50": [0, 0], "EMA9-21": [0, 0], ">EMA9": [0, 0]}
        for run in contiguous_runs(grp_bars, step_min=1):
            for i in range(1, len(run)):
                p, b = run[i - 1], run[i]
                if p["pvt_sign"] == -1 and b["pvt_sign"] == 1 and b["close"] is not None:
                    c = b["close"]
                    if b["1m EMA200"] is not None and c < b["1m EMA200"]:
                        band = "<EMA200"
                    elif b["1m EMA100"] is not None and c < b["1m EMA100"]:
                        band = "EMA100-200"
                    elif b["1m EMA50"] is not None and c < b["1m EMA50"]:
                        band = "EMA50-100"
                    elif b["1m EMA21"] is not None and c < b["1m EMA21"]:
                        band = "EMA21-50"
                    elif b["1m EMA9"] is not None and c < b["1m EMA9"]:
                        band = "EMA9-21"
                    else:
                        band = ">EMA9"
                    # forward move (same day, +15 1m-bars)
                    if i + 15 < len(run):
                        r = run[i + 15]["close"] - c
                        bands[band][0] += 1
                        if r > 0:
                            bands[band][1] += 1
        out["grp_%s" % g] = {"ema_levels": sec, "pvt_crossup_bands": bands}
    return out


# ---------------------------------------------------------------------------
# Query: 23PM / 23PM_5PM setups -> where do rises start (1m EMA ladder)
# ---------------------------------------------------------------------------

def q_23pm(bars_5m, bars_1m):
    """Validate: 23PM_5PM uptrend -> rises from 1m EMA21/50; 5m-MACD-cooling
    (PVT still +) -> retrace to 1m EMA100/200 then rise."""
    # forward-fill HTF MACD states + 5m histogram on the 5m grid
    last30 = last2h = None
    for b in bars_5m:
        if b.get("30m_macd") is not None and b.get("30m_signal") is not None:
            last30 = b["30m_macd"] > b["30m_signal"]
        if b.get("2h_macd") is not None and b.get("2h_signal") is not None:
            last2h = b["2h_macd"] > b["2h_signal"]
        b["ff30"], b["ff2h"] = last30, last2h
    for run in contiguous_runs(bars_5m):
        for b in run:
            b["hist"] = (b["5m_macd"] - b["5m_signal"]) \
                if (b.get("5m_macd") is not None and b.get("5m_signal") is not None) else None
        for i in range(2, len(run)):
            h = [run[j]["hist"] for j in (i - 2, i - 1, i)]
            run[i]["hist_fall"] = None not in h and h[0] > h[1] > h[2]
        for b in run:
            b.setdefault("hist_fall", None)

    for b in bars_5m:
        up23 = (b["30m_pvt_sign"] == 1 and b["2h_pvt_sign"] == 1
                and b.get("ff30") and b.get("ff2h"))
        b["reg23"] = bool(up23)
        b["reg235"] = bool(up23 and b["5m_pvt_sign"] == 1
                           and b["5m_macd"] is not None and b["5m_signal"] is not None
                           and b["5m_macd"] > b["5m_signal"])

    regmap = {(b["date"], b["hhmm"]): b for b in bars_5m}
    joined = []
    for b1 in bars_1m:
        st = session_stamp(b1["hhmm"], 5)
        b5 = regmap.get((b1["date"], st)) if st else None
        if b5 is None:
            continue
        b1["r235"] = b5["reg235"]
        b1["r23_not235"] = b5["reg23"] and not b5["reg235"]
        b1["pvt5_pos"] = b5["5m_pvt_sign"] == 1
        b1["macd5_xdn"] = (b5["5m_macd"] is not None and b5["5m_signal"] is not None
                           and b5["5m_macd"] < b5["5m_signal"])
        b1["macd5_fall"] = bool(b5.get("hist_fall"))
        joined.append(b1)

    EMAS = ["1m EMA9", "1m EMA21", "1m EMA50", "1m EMA100", "1m EMA200"]
    out = {"definition": "23PM = 2H+30m PVT+ & MACD_2452 up (ff from window closes); "
                         "23PM_5PM = 23PM + 5m PVT+ & 5m MACD up. Claim A: 23PM_5PM rises start "
                         "at 1m EMA21/50. Claim B: 23PM + 5m MACD cooling/retracing with "
                         "5m PVT+ -> retrace to 1m EMA100/200 then rise."}

    def hold_rates(bars):
        r = {}
        for e in EMAS:
            hd = [0, 0]
            rj = [0, 0]
            for b in bars:
                lv, o, h, l, c = b[e], b["open"], b["high"], b["low"], b["close"]
                if None in (lv, o, h, l, c):
                    continue
                if o > lv and l <= lv:
                    hd[0] += 1
                    if c > lv:
                        hd[1] += 1
                if o < lv and h >= lv:
                    rj[0] += 1
                    if c < lv:
                        rj[1] += 1
            r[e] = {"support_touch": hd[0], "hold_pct": round(100.0 * hd[1] / hd[0], 1) if hd[0] else None,
                    "resist_touch": rj[0], "reject_pct": round(100.0 * rj[1] / rj[0], 1) if rj[0] else None}
        return r

    # Claim A: 23PM_5PM bars
    a_bars = [b for b in joined if b["r235"]]
    out["claimA_23PM_5PM"] = {
        "n_1m_bars": len(a_bars),
        "ema_hold_reject": hold_rates(a_bars),
        "pullback_episodes": {},
    }
    # pullback episodes: 1m close below EMA21 from above, in 23PM_5PM
    for lvl, key in (("1m EMA21", "pb21"), ("1m EMA50", "pb50")):
        n = resumed = 0
        depths = []
        bands = {"tagged_EMA9_only": 0, "below_EMA21": 0, "below_EMA50": 0,
                 "below_EMA100": 0, "below_EMA200": 0}
        for run in contiguous_runs([b for b in a_bars], step_min=1):
            in_ep = False
            ep_low_lv = None
            for i, b in enumerate(run):
                lv = b[lvl]
                if lv is None or b["close"] is None:
                    continue
                if not in_ep and b["close"] < lv:
                    in_ep = True
                    ep_low_lv = None
                    n += 1
                if in_ep:
                    if b["low"] is not None:
                        depth = lv - b["low"]
                        if depth is not None and (ep_low_lv is None or depth > ep_low_lv):
                            ep_low_lv = depth
                    done = b["close"] > lv
                    if done or i == len(run) - 1:
                        if done:
                            resumed += 1
                        if ep_low_lv is not None:
                            depths.append(ep_low_lv)
                            # classify deepest penetration by band: which EMA did the low pierce
                            lows = b  # need low vs next EMAs
                        in_ep = False
        out["claimA_23PM_5PM"]["pullback_episodes"][key] = {
            "level": lvl, "n": n,
            "p_reclaim_within_day": round(resumed / float(n), 3) if n else None,
            "median_depth_below_pts": round(med(depths), 1) if depths else None,
        }
    # pullback low band distribution for EMA21 episodes
    bands = Counter = __import__("collections").Counter()
    for run in contiguous_runs([b for b in a_bars], step_min=1):
        in_ep = False
        lowest = None
        for i, b in enumerate(run):
            lv = b["1m EMA21"]
            if lv is None or b["close"] is None:
                continue
            if not in_ep and b["close"] < lv:
                in_ep = True
                lowest = None
            if in_ep and b["low"] is not None:
                if lowest is None or b["low"] < lowest[0]:
                    lowest = (b["low"], b)
                if b["close"] > lv or i == len(run) - 1:
                    if lowest:
                        lo, lb = lowest
                        e9, e21, e50, e100, e200 = (lb["1m EMA9"], lb["1m EMA21"],
                                                    lb["1m EMA50"], lb["1m EMA100"], lb["1m EMA200"])
                        if e200 is not None and lo < e200:
                            bands["below_1E200"] += 1
                        elif e100 is not None and lo < e100:
                            bands["1E200-1E100"] += 1
                        elif e50 is not None and lo < e50:
                            bands["1E100-1E50"] += 1
                        elif e21 is not None and lo < e21:
                            bands["1E50-1E21"] += 1
                        elif e9 is not None and lo < e9:
                            bands["1E21-1E9"] += 1
                        else:
                            bands["above_1E9"] += 1
                    in_ep = False
    out["claimA_23PM_5PM"]["pullback_low_band"] = dict(bands)

    # Claim B: 23PM (not 23PM_5PM) with 5m PVT+ and MACD cooling/retracing
    cool_bars = [b for b in joined if b["r23_not235"] and b["pvt5_pos"]
                 and (b["macd5_xdn"] or b["macd5_fall"])]
    out["claimB_cooling"] = {
        "n_1m_bars": len(cool_bars),
        "of_which_macd_below_signal": sum(1 for b in cool_bars if b["macd5_xdn"]),
        "of_which_hist_falling_only": sum(1 for b in cool_bars if not b["macd5_xdn"] and b["macd5_fall"]),
        "ema_hold_reject": hold_rates(cool_bars),
        "tag_EMA100_200": {},
    }
    # from cooling-state starts, does price tag 1E100/1E200 within 24 1m bars, then rise?
    states = [b for b in joined if b["r23_not235"] and b["pvt5_pos"]]
    for name, cond in (("macd_below_signal", lambda b: b["macd5_xdn"]),
                       ("hist_falling_only", lambda b: b["macd5_fall"] and not b["macd5_xdn"])):
        trig = []
        prev_state = False
        for run in contiguous_runs(states, step_min=1):
            prev_state = False
            for i, b in enumerate(run):
                s = cond(b)
                if s and not prev_state:
                    trig.append((i, run))
                prev_state = s
        n_tag = n_rise = n_fwd = 0
        for i, run in trig:
            b0 = run[i]
            tag_lv = None
            for j in range(i, min(i + 24, len(run))):
                lv100, lv200 = run[j]["1m EMA100"], run[j]["1m EMA200"]
                lo = run[j]["low"]
                if lo is None:
                    continue
                if lv200 is not None and lo <= lv200:
                    tag_lv = ("EMA200", lv200, j)
                    break
                if lv100 is not None and lo <= lv100:
                    tag_lv = ("EMA100", lv100, j)
                    break
            if tag_lv:
                n_tag += 1
                j = tag_lv[2]
                if j + 15 < len(run) and run[j + 15]["close"] is not None and tag_lv[1]:
                    n_fwd += 1
                    if run[j + 15]["close"] > tag_lv[1]:
                        n_rise += 1
        out["claimB_cooling"]["tag_EMA100_200"][name] = {
            "n_triggers": len(trig),
            "p_tag_EMA100_or_200_within_2h": round(n_tag / float(len(trig)), 3) if trig else None,
            "p_rise_after_tag": round(n_rise / float(n_fwd), 3) if n_fwd else None,
        }
    # contrast: in plain 23PM_5PM (no cooling), how often do pullbacks even reach EMA100/200?
    out["contrast"] = {
        "note": "claimB window = 23PM up but 5m not-full-alignment; claimA window = full 23PM_5PM",
        "n_23PM_5PM_bars": len(a_bars),
        "n_23PM_not235_bars": sum(1 for b in joined if b["r23_not235"]),
    }
    return out


def detect_1spx15pm(bars_5m, bars_1m, confluence_bars=5, cluster_min=15):
    """1SPX_15PM detection: inside 23PM_5PM state, Stochastic cross-up (available at
    5m cadence in the export) + 1m PVT cross-up within +/-confluence_bars 1m-bars.
    Clusters within cluster_min minutes merge into one instance.
    Returns [(date_ddmmyy, start_hhmm, end_hhmm)]."""
    last30 = last2h = None
    for b in bars_5m:
        if b.get("30m_macd") is not None and b.get("30m_signal") is not None:
            last30 = b["30m_macd"] > b["30m_signal"]
        if b.get("2h_macd") is not None and b.get("2h_signal") is not None:
            last2h = b["2h_macd"] > b["2h_signal"]
        b["ff30"], b["ff2h"] = last30, last2h
    for b in bars_5m:
        b["reg23"] = bool(b["30m_pvt_sign"] == 1 and b["2h_pvt_sign"] == 1
                          and b.get("ff30") and b.get("ff2h"))
        b["reg235"] = bool(b["reg23"]
                           and b["5m_pvt_sign"] == 1
                           and b["5m_macd"] is not None and b["5m_signal"] is not None
                           and b["5m_macd"] > b["5m_signal"])
    regmap = {(b["date"], b["hhmm"]): b["reg235"] for b in bars_5m}
    reg23map = {(b["date"], b["hhmm"]): b["reg23"] for b in bars_5m}

    def ts(b):
        return datetime.strptime(b["date"] + " " + b["hhmm"], "%Y-%m-%d %H:%M")

    events = []  # (t, kind, variant, run_index)
    for run in contiguous_runs(bars_1m, step_min=1):
        last_kd = None  # (K, D) at previous populated bar
        for i, b in enumerate(run):
            st = session_stamp(b["hhmm"], 5)
            r235 = regmap.get((b["date"], st)) if st else None
            r23 = reg23map.get((b["date"], st)) if st else None
            p = run[i - 1] if i else None
            if p is not None and b.get("pvt_sign") == 1 and p.get("pvt_sign") == -1:
                if r235:
                    events.append((ts(b), "pvt", 1, i))
                if r23 and not r235:
                    events.append((ts(b), "pvt", 2, i))
            if b.get("K") is not None and b.get("D") is not None:
                if last_kd is not None:
                    pk, pd = last_kd
                    if pk <= pd and b["K"] > b["D"]:
                        if r235:
                            events.append((ts(b), "stoch", 1, i))
                        if r23 and not r235:
                            events.append((ts(b), "stoch", 2, i))
                last_kd = (b["K"], b["D"])

    from collections import defaultdict
    ev_by = defaultdict(list)
    for t, kind, variant, i in events:
        ev_by[(variant, kind)].append((t, i))

    # confluence per variant: pvt & stoch events within +/- confluence_bars
    instances = {1: [], 2: []}
    for variant in (1, 2):
        pv = ev_by[(variant, "pvt")]
        sc = ev_by[(variant, "stoch")]
        for t, i in pv:
            if any(abs(j - i) <= confluence_bars for _, j in sc if _.date() == t.date()):
                instances[variant].append(t)
        for t, i in sc:
            if any(abs(j - i) <= confluence_bars for _, j in pv if _.date() == t.date()):
                instances[variant].append(t)
        instances[variant].sort()

    out = {}
    for variant, label in ((1, "inside_23PM_5PM"), (2, "23PM_not235_transition")):
        merged = []
        for t in instances[variant]:
            if merged and (t - merged[-1][1]).total_seconds() <= cluster_min * 60 \
                    and t.date() == merged[-1][1].date():
                merged[-1][1] = t
            else:
                merged.append([t, t])
        out[label] = [(t0.strftime("%d:%m:%y"), t0.strftime("%H:%M"), t1.strftime("%H:%M"))
                      for t0, t1 in merged]
    return out


# ---------------------------------------------------------------------------
# Momentors: cross-up age counters (PX/MX/SX/OX per timeframe)
# ---------------------------------------------------------------------------

MOMENTOR_COLS = ["3PX", "3MX", "3SX", "3OX", "2PX", "2MX", "2SX", "2OX",
                 "DPX", "DMX", "DSX", "DOX"]
# print window per TF: 30m counts cross candle as 1 (valid 1-13);
# 2H and Daily count the cross candle as 0 (valid 0-4). Outside -> NA.
MOMENTOR_RULES = {"3": (1, 1, 13), "2": (0, 0, 4), "D": (0, 0, 4)}


def compute_momentor_ages(bars):
    """Age of the last cross-up, per TF x indicator, at each 5m bar.

    30m: age = candles since cross-up, cross candle = 1; print 1..13.
    2H/D: cross candle = 0; print 0..4. Anything else (below zero, aged,
    cross predating the data window) -> None -> rendered as NA.
    """
    # forward-fill states populated only at window closes
    last30macd = last2hmacd = last1dmacd = None
    last30k = None
    last30voc = None
    for b in bars:
        if b.get("30m_macd") is not None and b.get("30m_signal") is not None:
            last30macd = b["30m_macd"] > b["30m_signal"]
        if b.get("2h_macd") is not None and b.get("2h_signal") is not None:
            last2hmacd = b["2h_macd"] > b["2h_signal"]
        if b.get("1d_macd") is not None and b.get("1d_signal") is not None:
            last1dmacd = b["1d_macd"] > b["1d_signal"]
        if b.get("30m_k") is not None and b.get("30m_d") is not None:
            last30k = b["30m_k"] > b["30m_d"]
        if b.get("30m_voc") is not None:
            last30voc = b["30m_voc"] > 0
        b["ff_30m_macd_up"] = last30macd
        b["ff_2h_macd_up"] = last2hmacd
        b["ff_1d_macd_up"] = last1dmacd
        b["ff_30m_stoch_up"] = last30k
        b["ff_30m_voc_pos"] = last30voc

    def ages_for(bars, state_of, window_of, offset, lo, hi):
        """Walk bars chronologically; per-bar cross-up age.
        state below zero / age outside [lo, hi] / cross never seen
        (while positive) -> None (NA)."""
        ages = {}
        prev_state = None
        flip_seq = None
        seq = 0
        last_key = None
        for b in bars:
            key = window_of(b)
            if key != last_key:
                seq += 1
                last_key = key
            s = state_of(b)
            if s is None:
                continue
            if prev_state is not None and s > 0 and prev_state < 0:
                flip_seq = seq
            prev_state = s
            if s > 0 and flip_seq is not None:
                age = seq - flip_seq + offset
                ages[(b["date"], b["hhmm"])] = age if lo <= age <= hi else None
            else:
                ages[(b["date"], b["hhmm"])] = None
        return ages

    def stamp(step):
        def g(b):
            return (b["date"], session_stamp(b["hhmm"], step))
        return g

    def day_of(b):
        return (b["date"],)

    r3 = MOMENTOR_RULES["3"]
    r2 = MOMENTOR_RULES["2"]
    rd = MOMENTOR_RULES["D"]
    specs = {
        "3PX": (lambda b: b.get("30m_pvt_sign"), stamp(30), r3),
        "3MX": (lambda b: sgn(1 if b.get("ff_30m_macd_up") else (-1 if b.get("ff_30m_macd_up") is not None else None)), stamp(30), r3),
        "3SX": (lambda b: sgn(1 if b.get("ff_30m_stoch_up") else (-1 if b.get("ff_30m_stoch_up") is not None else None)), stamp(30), r3),
        "3OX": (lambda b: 1 if b.get("ff_30m_voc_pos") else (-1 if b.get("ff_30m_voc_pos") is not None else None), stamp(30), r3),
        "2PX": (lambda b: b.get("2h_pvt_sign"), stamp(120), r2),
        "2MX": (lambda b: sgn(1 if b.get("ff_2h_macd_up") else (-1 if b.get("ff_2h_macd_up") is not None else None)), stamp(120), r2),
        "DPX": (lambda b: b.get("1d_pvt_sign"), day_of, rd),
        "DMX": (lambda b: sgn(1 if b.get("ff_1d_macd_up") else (-1 if b.get("ff_1d_macd_up") is not None else None)), day_of, rd),
    }
    out = {}
    for col, (state_of, window_of, rule) in specs.items():
        ages = ages_for(bars, state_of, window_of, rule[0], rule[1], rule[2])
        for k, v in ages.items():
            out.setdefault(k, {})[col] = v
    for k in out:
        for col in MOMENTOR_COLS:
            out[k].setdefault(col, None)  # 2SX/2OX/DSX/DOX unavailable in export
    return out


# ---------------------------------------------------------------------------
# Query: 23PM_5PX - 5m PVT cross-up with 2PM+3PM up (sign-based, carried values);
# lives while the 1m MACD_2452 up-swing running at the trigger completes
# (ends at the first 1m cross-down at/after the trigger; if 1m MACD is still
# down at trigger, the next cross-up's swing is "the" swing).
# ---------------------------------------------------------------------------

PX_LAST_5M = "15:25"    # last tradeable 5m bar stamp (15:30+ = post-close tail)
PX_LAST_1M = "15:29"    # last tradeable 1m bar stamp


def px_momentor_string(mom, date, hhmm, x_fired=False):
    """Render Momentors token string (3/2/D TF tokens, PMSO letters) for a bar;
    trailing _X when the row's 1SPX-class entry trigger fired (uniform rule)."""
    d = mom.get((date, hhmm), {})
    toks = []
    for tf, cmap in (("3", (("P", "3PX"), ("M", "3MX"), ("S", "3SX"), ("O", "3OX"))),
                     ("2", (("P", "2PX"), ("M", "2MX"))),
                     ("D", (("P", "DPX"), ("M", "DMX")))):
        letters = "".join(L for L, c in cmap if d.get(c) is not None)
        if letters:
            toks.append(tf + letters)
    if not toks:
        return "X" if x_fired else "NA"
    s = "_".join(toks)
    return s + "_X" if x_fired else s


def px_1m_band(b1):
    """Price Action band: where the entry close sits vs the 1m EMA ladder."""
    c = b1["close"]
    ladder = (("1m EMA200", "<1E200"), ("1m EMA100", "1E100-200"), ("1m EMA50", "1E50-100"),
              ("1m EMA21", "1E21-50"), ("1m EMA9", "1E9-21"))
    for key, label in ladder:
        v = b1.get(key)
        if v is not None and c < v:
            return label
    return ">1E9"


def px_5m_band(b5):
    """Price Action band vs the 5m EMA ladder (basis when no 1m coverage)."""
    c = b5["close"]
    ladder = (("5m_ema200", "<5E200"), ("5m_ema100", "5E100-200"), ("5m_ema50", "5E50-100"),
              ("5m_ema21", "5E21-50"), ("5m_ema9", "5E9-21"), ("5m_ema4", "5E4-9"))
    for key, label in ladder:
        v = b5.get(key)
        if v is not None and c < v:
            return label
    return ">5E4"


def px_current_1m_cross(one, date, entry_stamp, direction, lookback_days=10):
    """MOST RECENT 1m PVT cross (up for long side / down for short side) at or
    before entry_stamp, scanning back up to lookback_days of 1m coverage.
    Returns (cross_bar, leg_extreme): leg_extreme = the low of the down-leg
    into an up-cross (min low since the preceding cross-down, same day), or
    the high of the up-leg into a down-cross. Same-day leg only; a cross-day
    leg falls back to the cross bar's own extreme."""
    dts = sorted(one.keys())
    prior = [d for d in dts if d <= date][-lookback_days:]
    for d in reversed(prior):
        best = best_ext = None
        leg_ext = None
        prev = None
        for b in one[d]:
            if d == date and b["hhmm"] > entry_stamp:
                break
            ps = b.get("pvt_sign")
            if prev is not None and ps is not None:
                if direction == "up":
                    if prev == 1 and ps == -1:          # cross-down opens a fresh down-leg
                        leg_ext = b.get("low")
                    elif prev == -1 and ps == 1:        # cross-up: candidate - keep the LATEST
                        v = b.get("low")
                        ext = v if leg_ext is None else \
                            (min(leg_ext, v) if v is not None else leg_ext)
                        best, best_ext = b, ext
                else:
                    if prev == -1 and ps == 1:          # cross-up opens a fresh up-leg
                        leg_ext = b.get("high")
                    elif prev == 1 and ps == -1:        # cross-down: candidate - keep the LATEST
                        v = b.get("high")
                        ext = v if leg_ext is None else \
                            (max(leg_ext, v) if v is not None else leg_ext)
                        best, best_ext = b, ext
            prev = ps
            # keep the open leg's extreme current
            v = b.get("low") if direction == "up" else b.get("high")
            if leg_ext is not None and v is not None:
                leg_ext = min(leg_ext, v) if direction == "up" else max(leg_ext, v)
            elif leg_ext is None and v is not None and \
                    ((direction == "up" and ps == -1) or (direction == "down" and ps == 1)):
                leg_ext = v
        if best is not None:
            return best, best_ext
    return None, None


def px_support_label(b1, t5, direction, leg_ext, move_ext):
    """Price Action label (user rule + notation): the MAJOR rungs the move
    reclaimed, grouped in hierarchy order (major TF first: 3E -> 5E -> 1E),
    levels ascending within each TF, digits compacted - all but the last rung
    as their leading digit, the last in full: 3E(50,100) -> '3E5100';
    5E(50,100,200) -> '5E51200'; 1E(100,200) -> '1E1200'.

    Move range = leg_ext (dip low at the 1m PVT cross / rally top for shorts)
    to move_ext (farthest print of the containing 5m-PVT run). Major rung set:
    1E100, 1E200, 3E50, 3E100, 5E50, 5E100, 5E200. Fallback when no major is
    in range: the 1E50/1E21 minor shelf."""
    def g(*vals):
        for v in vals:
            if v is not None:
                return v
        return None

    majors = [("1E", "100", g(b1.get("1m EMA100"), b1.get("ema100_1m"))),
              ("1E", "200", g(b1.get("1m EMA200"), b1.get("ema200_1m"))),
              ("3E", "50", t5.get("30m_ema50")), ("3E", "100", t5.get("30m_ema100")),
              ("3E", "200", t5.get("30m_ema200")),
              ("5E", "50", t5.get("5m_ema50")), ("5E", "100", t5.get("5m_ema100")),
              ("5E", "200", t5.get("5m_ema200"))]
    if leg_ext is None:
        leg_ext = b1["close"]
    if move_ext is None:
        move_ext = b1["close"]
    lo, hi = (leg_ext, move_ext) if direction == "up" else (move_ext, leg_ext)
    in_range = [(tf, num, v) for tf, num, v in majors
                if v is not None and lo < v < hi]
    if in_range:
        parts = []
        for tf in ("3E", "5E", "1E"):            # hierarchy: major TF first
            grp = sorted([x for x in in_range if x[0] == tf], key=lambda x: int(x[1]))
            if not grp:
                continue
            digits = "".join(x[1][0] for x in grp[:-1]) + grp[-1][1]
            parts.append(tf + digits)
        return ("Rise from " if direction == "up" else "Fall from ") + ", ".join(parts)
    return ("Rise from 1E50, 1E21" if direction == "up" else "Fall from 1E50, 1E21")


def px_run_move_ext(bars_5m, idx5, date, stamp, direction, sign_col="5m_pvt_sign"):
    """Farthest print of the sign-run containing (date, stamp): walk forward
    while the sign column holds, track max close (up) / min close (down)."""
    i = idx5.get((date, stamp))
    if i is None:
        return None
    s = 1 if direction == "up" else -1
    ext = bars_5m[i]["close"]
    j = i
    while j + 1 < len(bars_5m) and bars_5m[j + 1][sign_col] == s:
        j += 1
        c = bars_5m[j]["close"]
        ext = max(ext, c) if direction == "up" else min(ext, c)
    return ext


def px_stoch_confirmed(one, date, hhmm, direction, window=5):
    """X-flag test (1SPX-class confluence, row-level): did the 1m Stochastic
    cross in the trend's direction within +/-window 1m-bars of the anchor
    1m PVT cross? (up: K crossed above D; down: K crossed below D)."""
    day = one.get(date, [])
    idx = next((i for i, b in enumerate(day) if b["hhmm"] == hhmm), None)
    if idx is None:
        return False
    lo = max(0, idx - window)
    hi = min(len(day), idx + window + 1)
    for i in range(lo + 1, hi):
        p, b = day[i - 1], day[i]
        if None in (p.get("K"), p.get("D"), b.get("K"), b.get("D")):
            continue
        if direction == "up" and p["K"] <= p["D"] and b["K"] > b["D"]:
            return True
        if direction == "down" and p["K"] >= p["D"] and b["K"] < b["D"]:
            return True
    return False


def detect_1spx15pm_down(bars_5m, bars_1m, confluence_bars=5, cluster_min=15):
    """Downtrend mirror of detect_1spx15pm: inside the all-DOWN state
    (2H/30m PVT- & MACD-ff, 5m PVT- & MACD-), Stochastic cross-DOWN + 1m PVT
    cross-down within +/-confluence_bars. Same clustering/return format."""
    last30 = last2h = None
    for b in bars_5m:
        if b.get("30m_macd") is not None and b.get("30m_signal") is not None:
            last30 = b["30m_macd"] > b["30m_signal"]
        if b.get("2h_macd") is not None and b.get("2h_signal") is not None:
            last2h = b["2h_macd"] > b["2h_signal"]
        b["ff30"], b["ff2h"] = last30, last2h
    for b in bars_5m:
        b["reg23d"] = bool(b["30m_pvt_sign"] == -1 and b["2h_pvt_sign"] == -1
                           and b.get("ff30") is False and b.get("ff2h") is False)
        b["reg235d"] = bool(b["reg23d"]
                            and b["5m_pvt_sign"] == -1
                            and b["5m_macd"] is not None and b["5m_signal"] is not None
                            and b["5m_macd"] < b["5m_signal"])
    regmap = {(b["date"], b["hhmm"]): b["reg235d"] for b in bars_5m}

    def ts(b):
        return datetime.strptime(b["date"] + " " + b["hhmm"], "%Y-%m-%d %H:%M")

    events = []
    for run in contiguous_runs(bars_1m, step_min=1):
        last_kd = None
        for i, b in enumerate(run):
            st = session_stamp(b["hhmm"], 5)
            r235d = regmap.get((b["date"], st)) if st else None
            p = run[i - 1] if i else None
            if p is not None and b.get("pvt_sign") == -1 and p.get("pvt_sign") == 1 and r235d:
                events.append((ts(b), "pvt", i))
            if b.get("K") is not None and b.get("D") is not None:
                if last_kd is not None:
                    pk, pd = last_kd
                    if pk >= pd and b["K"] < b["D"] and r235d:
                        events.append((ts(b), "stoch", i))
                last_kd = (b["K"], b["D"])

    from collections import defaultdict
    ev_by = defaultdict(list)
    for t, kind, i in events:
        ev_by[kind].append((t, i))
    hits = []
    for t, i in ev_by["pvt"]:
        if any(abs(j - i) <= confluence_bars for _, j in ev_by["stoch"]
               if _.date() == t.date()):
            hits.append(t)
    for t, i in ev_by["stoch"]:
        if any(abs(j - i) <= confluence_bars for _, j in ev_by["pvt"]
               if _.date() == t.date()):
            hits.append(t)
    hits.sort()
    merged = []
    for t in hits:
        if merged and (t - merged[-1][1]).total_seconds() <= cluster_min * 60 \
                and t.date() == merged[-1][1].date():
            merged[-1][1] = t
        else:
            merged.append([t, t])
    return [(t0.strftime("%d:%m:%y"), t0.strftime("%H:%M"), t1.strftime("%H:%M"))
            for t0, t1 in merged]


def q_23pm_5px(bars_5m, bars_1m, direction="up"):
    """Detect 23PM_5PX episodes (direction='up' | 'down' - exact mirrors).

    Trigger: 5m PVT sign -1 -> +1 (up) / +1 -> -1 (down), contiguous same-day
    bars, with 2PM+3PM fully aligned at the trigger bar - PVT sign AND
    forward-filled MACD_2452 state on both 2H and 30m (up: +1/up; down: -1/down),
    sign-based no-repaint, matching the 23PM_5PM playbook convention.

    Exit: first 5m bar AFTER the trigger with close across its own 5m EMA4
    (up: close < EMA4; down: close > EMA4) - price loses the micro-mean. EOD
    flat at 15:25 if unbroken. The 1m MACD_2452 cross (legacy exit, mirrored)
    is logged as a comparison note when 1m coverage exists (Aug 3 2026+).
    pts/pct = move in trade direction (positive = trend move captured).
    """
    from datetime import datetime
    s = 1 if direction == "up" else -1
    want = direction == "up"  # ff MACD state wanted (True = up / False = down)
    last30 = last2h = None
    for b in bars_5m:
        if b.get("30m_macd") is not None and b.get("30m_signal") is not None:
            last30 = b["30m_macd"] > b["30m_signal"]
        if b.get("2h_macd") is not None and b.get("2h_signal") is not None:
            last2h = b["2h_macd"] > b["2h_signal"]
        b["ff30"], b["ff2h"] = last30, last2h

    mom = compute_momentor_ages(bars_5m)

    one = {}
    for b in bars_1m:
        if (b["hhmm"] <= PX_LAST_1M and b.get("macd") is not None
                and b.get("signal") is not None and b.get("close") is not None):
            one.setdefault(b["date"], []).append(b)

    def minutes(a, b):
        d0 = datetime.strptime(a, "%H:%M")
        d1 = datetime.strptime(b, "%H:%M")
        return int((d1 - d0).total_seconds() // 60)

    stamp5 = {(x["date"], x["hhmm"]): x for x in bars_5m}
    idx5 = {(x["date"], x["hhmm"]): i for i, x in enumerate(bars_5m)}
    episodes = []
    for run in contiguous_runs(bars_5m):
        day5 = [x for x in run if x["hhmm"] <= PX_LAST_5M]
        for i in range(1, len(day5)):
            p, b = day5[i - 1], day5[i]
            if not (p["5m_pvt_sign"] == -s and b["5m_pvt_sign"] == s):
                continue
            if not (b["30m_pvt_sign"] == s and b["2h_pvt_sign"] == s
                    and b["ff30"] is want and b["ff2h"] is want):
                continue
            entry = b["close"]
            # exit: first LATER 5m bar closing across its own 5m EMA4
            end_bar, eod = None, True
            for x in day5[i + 1:]:
                if x["5m_ema4"] is None:
                    continue
                broke = x["close"] < x["5m_ema4"] if direction == "up" \
                    else x["close"] > x["5m_ema4"]
                if broke:
                    end_bar, eod = x, False
                    break
            if end_bar is None:
                end_bar = day5[-1]
            pts = (end_bar["close"] - entry) if direction == "up" \
                else (entry - end_bar["close"])
            # legacy exit note: 1m MACD_2452 cross against the position (if covered)
            seq1 = [x for x in one.get(b["date"], []) if x["hhmm"] >= b["hhmm"]]
            macd_note = None
            if seq1:
                st = ["up" if x["macd"] > x["signal"] else "dn" for x in seq1]
                st_prev = "up" if direction == "up" else "dn"
                st_hit = "dn" if direction == "up" else "up"
                j = next((j2 for j2 in range(1, len(seq1))
                          if st[j2 - 1] == st_prev and st[j2] == st_hit), None)
                ref = seq1[-1] if j is None else seq1[j]
                note_pts = (ref["close"] - entry) if direction == "up" \
                    else (entry - ref["close"])
                macd_note = {"time": ref["hhmm"], "pts": int(round(note_pts))}
            # Price Action (user rule): major level taken at the CURRENT 1m PVT cross
            x1, leg_ext = px_current_1m_cross(one, b["date"], b["hhmm"], direction)
            mext = px_run_move_ext(bars_5m, idx5, b["date"], b["hhmm"], direction)
            if x1 is not None:
                t5c = stamp5.get((x1["date"], session_stamp(x1["hhmm"], 5))) or b
                pa, basis = px_support_label(x1, t5c, direction, leg_ext, mext), "1m"
            else:
                pa = px_support_label(b, b, direction, None, mext)
                basis = "5m"
            band = px_1m_band(seq1[0]) if seq1 else px_5m_band(b)
            x_flag = px_stoch_confirmed(one, x1["date"], x1["hhmm"], direction) \
                if x1 is not None else False
            episodes.append({
                "date": b["date"], "from": b["hhmm"], "to": end_bar["hhmm"],
                "duration_min": minutes(b["hhmm"], end_bar["hhmm"]),
                "entry": round(entry, 2), "exit": round(end_bar["close"], 2),
                "pts": int(round(pts)), "pct": round(100.0 * pts / entry, 2) + 0.0,
                "eod": eod,
                "macd_note": macd_note,
                "momentors": px_momentor_string(mom, b["date"], b["hhmm"], x_flag),
                "price_action": pa,
                "price_action_band": band,
                "band_basis": "1m" if seq1 else "5m",
                "direction": direction,
            })

    pts_all = sorted(e["pts"] for e in episodes)
    out = {
        "definition": (
            "23PM_5PX (%s) = 2PM+3PM %s (PVT sign %s AND MACD_2452 %s, carried "
            "no-repaint values) at a 5m PVT %s (same-day contiguous bars, stamps "
            "<= 15:25). Exit = first 5m close %s its own 5m EMA4; EOD flat at "
            "15:25 if unbroken. pts/pct = move in trade direction. macd_note = "
            "where the legacy 1m MACD_2452 cross exit would have landed (1m "
            "coverage Aug 3 2026+ only).") % (
            direction,
            "uptrending" if direction == "up" else "downtrending",
            "+1" if direction == "up" else "-1",
            "up" if direction == "up" else "down",
            "cross-up (-1 -> +1)" if direction == "up" else "cross-down (+1 -> -1)",
            "below" if direction == "up" else "above"),
        "n_episodes": len(episodes),
        "n_positive": sum(1 for p in pts_all if p > 0),
        "n_eod_flat": sum(1 for e in episodes if e["eod"]),
        "total_pts": round(sum(pts_all), 1),
        "mean_pts": round(sum(pts_all) / len(pts_all), 2) if pts_all else None,
        "median_pts": pts_all[len(pts_all) // 2] if pts_all else None,
        "episodes": episodes,
    }
    return out


def q_2pm_3px(bars_5m, bars_1m, direction="up"):
    """Detect 2PM_3PX episodes (direction='up' | 'down' - exact mirrors).

    Trigger: 30m PVT sign -1 -> +1 (up) / +1 -> -1 (down) on carried values
    (the flip prints on the 5m bar carrying the 30m close value), with 2PM
    fully aligned at the trigger bar (2H PVT sign AND forward-filled 2H
    MACD_2452 state: up = +1/up; down = -1/down - sign-based, no-repaint).
    Exit: first 5m bar AFTER the trigger with close across its own 5m EMA21
    (up: below; down: above); EOD flat at 15:25 if unbroken.
    No 3PM condition (per definition).
    pts/pct = move in trade direction (positive = trend move captured).
    """
    from datetime import datetime
    s = 1 if direction == "up" else -1
    want = direction == "up"
    last2h = None
    for b in bars_5m:
        if b.get("2h_macd") is not None and b.get("2h_signal") is not None:
            last2h = b["2h_macd"] > b["2h_signal"]
        b["ff2h"] = last2h

    mom = compute_momentor_ages(bars_5m)

    one = {}  # 1m ladder bands for Price Action where coverage exists (Aug 3 2026+)
    for b in bars_1m:
        if b["hhmm"] <= PX_LAST_1M and b.get("close") is not None:
            one.setdefault(b["date"], []).append(b)

    def minutes(a, b):
        d0 = datetime.strptime(a, "%H:%M")
        d1 = datetime.strptime(b, "%H:%M")
        return int((d1 - d0).total_seconds() // 60)

    stamp5 = {(x["date"], x["hhmm"]): x for x in bars_5m}
    idx5 = {(x["date"], x["hhmm"]): i for i, x in enumerate(bars_5m)}
    episodes = []
    for run in contiguous_runs(bars_5m):
        day5 = [x for x in run if x["hhmm"] <= PX_LAST_5M]
        for i in range(1, len(day5)):
            p, b = day5[i - 1], day5[i]
            if not (p["30m_pvt_sign"] == -s and b["30m_pvt_sign"] == s):
                continue
            if not (b["2h_pvt_sign"] == s and b["ff2h"] is want):
                continue
            entry = b["close"]
            # exit: first LATER 5m bar closing across its own 5m EMA21
            end_bar, eod = None, True
            for x in day5[i + 1:]:
                if x["5m_ema21"] is None:
                    continue
                broke = x["close"] < x["5m_ema21"] if direction == "up" \
                    else x["close"] > x["5m_ema21"]
                if broke:
                    end_bar, eod = x, False
                    break
            if end_bar is None:
                end_bar = day5[-1]
            pts = (end_bar["close"] - entry) if direction == "up" \
                else (entry - end_bar["close"])
            # Price Action (user rule): major level taken at the CURRENT 1m PVT cross
            x1, leg_ext = px_current_1m_cross(one, b["date"], b["hhmm"], direction)
            mext = px_run_move_ext(bars_5m, idx5, b["date"], b["hhmm"], direction)
            if x1 is not None:
                t5c = stamp5.get((x1["date"], session_stamp(x1["hhmm"], 5))) or b
                pa, basis = px_support_label(x1, t5c, direction, leg_ext, mext), "1m"
            else:
                pa = px_support_label(b, b, direction, None, mext)
                basis = "5m"
            t1 = [x for x in one.get(b["date"], []) if x["hhmm"] >= b["hhmm"]]
            band = px_1m_band(t1[0]) if t1 else px_5m_band(b)
            x_flag = px_stoch_confirmed(one, x1["date"], x1["hhmm"], direction) \
                if x1 is not None else False
            episodes.append({
                "date": b["date"], "from": b["hhmm"], "to": end_bar["hhmm"],
                "duration_min": minutes(b["hhmm"], end_bar["hhmm"]),
                "entry": round(entry, 2), "exit": round(end_bar["close"], 2),
                "pts": int(round(pts)), "pct": round(100.0 * pts / entry, 2) + 0.0,
                "eod": eod,
                "momentors": px_momentor_string(mom, b["date"], b["hhmm"], x_flag),
                "price_action": pa,
                "price_action_band": band, "band_basis": basis,
                "direction": direction,
            })

    pts_all = sorted(e["pts"] for e in episodes)
    out = {
        "definition": (
            "2PM_3PX (%s) = 2PM %s (2H PVT sign %s AND 2H MACD_2452 %s, carried "
            "no-repaint values) at a 30m PVT %s (prints at the 30m close bar). "
            "Exit = first 5m close %s its own 5m EMA21; EOD flat at 15:25 if "
            "unbroken. No 3PM condition. pts/pct = move in trade direction.") % (
            direction,
            "uptrending" if direction == "up" else "downtrending",
            "+1" if direction == "up" else "-1",
            "up" if direction == "up" else "down",
            "cross-up (-1 -> +1)" if direction == "up" else "cross-down (+1 -> -1)",
            "below" if direction == "up" else "above"),
        "n_episodes": len(episodes),
        "n_positive": sum(1 for p in pts_all if p > 0),
        "n_eod_flat": sum(1 for e in episodes if e["eod"]),
        "total_pts": round(sum(pts_all), 1),
        "mean_pts": round(sum(pts_all) / len(pts_all), 2) if pts_all else None,
        "median_pts": pts_all[len(pts_all) // 2] if pts_all else None,
        "episodes": episodes,
    }
    return out


def q_23pm_5pm_down(bars_5m, bars_1m):
    """Machine mirror of the hand-marked 23PM_5PM phases, downtrend side.

    Phase = maximal same-day contiguous run of bars where 2H+30m+5m PVT/MACD
    are all DOWN (2H/30m PVT sign -1, forward-filled MACD_2452 down on both,
    5m PVT -1, 5m MACD_2452 < Signal) - exact mirror of the uptrend reg235
    phase recipe. Rows record the phase window, the close-to-close move in
    trend direction (positive = down-move), MFE (deepest low below the
    phase-start close), Momentors and the EMA-ladder band at phase start.
    """
    from datetime import datetime
    last30 = last2h = None
    for b in bars_5m:
        if b.get("30m_macd") is not None and b.get("30m_signal") is not None:
            last30 = b["30m_macd"] > b["30m_signal"]
        if b.get("2h_macd") is not None and b.get("2h_signal") is not None:
            last2h = b["2h_macd"] > b["2h_signal"]
        b["ff30"], b["ff2h"] = last30, last2h

    mom = compute_momentor_ages(bars_5m)

    down_inst = [(datetime.strptime("%s %s" % (d, s), "%d:%m:%y %H:%M"),
                  datetime.strptime("%s %s" % (d, e), "%d:%m:%y %H:%M"))
                 for d, s, e in detect_1spx15pm_down(bars_5m, bars_1m)]

    one = {}
    for b in bars_1m:
        if b["hhmm"] <= PX_LAST_1M and b.get("close") is not None:
            one.setdefault(b["date"], []).append(b)

    def minutes(a, b):
        d0 = datetime.strptime(a, "%H:%M")
        d1 = datetime.strptime(b, "%H:%M")
        return int((d1 - d0).total_seconds() // 60)

    def down(b):
        return (b["5m_pvt_sign"] == -1 and b["30m_pvt_sign"] == -1
                and b["2h_pvt_sign"] == -1
                and b["ff30"] is False and b["ff2h"] is False
                and b["5m_macd"] is not None and b["5m_signal"] is not None
                and b["5m_macd"] < b["5m_signal"])

    stamp5 = {(x["date"], x["hhmm"]): x for x in bars_5m}
    idx5 = {(x["date"], x["hhmm"]): i for i, x in enumerate(bars_5m)}
    phases = []
    for run in contiguous_runs(bars_5m):
        day5 = [x for x in run if x["hhmm"] <= PX_LAST_5M]
        i = 0
        while i < len(day5):
            if not down(day5[i]):
                i += 1
                continue
            j = i
            while j + 1 < len(day5) and down(day5[j + 1]):
                j += 1
            phase = day5[i:j + 1]
            entry = phase[0]["close"]
            move = entry - phase[-1]["close"]
            lows = [x["low"] for x in phase if x["low"] is not None]
            mfe = (entry - min(lows)) if lows else None
            # Price Action (user rule): major level taken at the CURRENT 1m PVT cross-down
            x1, leg_ext = px_current_1m_cross(one, phase[0]["date"], phase[0]["hhmm"], "down")
            mext = px_run_move_ext(bars_5m, idx5, phase[0]["date"], phase[0]["hhmm"], "down")
            if x1 is not None:
                t5c = stamp5.get((x1["date"], session_stamp(x1["hhmm"], 5))) or phase[0]
                pa, basis = px_support_label(x1, t5c, "down", leg_ext, mext), "1m"
            else:
                pa, basis = px_support_label(phase[0], phase[0], "down", None, mext), "5m"
            t1 = [x for x in one.get(phase[0]["date"], []) if x["hhmm"] >= phase[0]["hhmm"]]
            band = px_1m_band(t1[0]) if t1 else px_5m_band(phase[0])
            # X: a 1SPX-class instance (stoch cross-down + 1m PVT cross-down in the
            # down-state) overlapping the phase window
            x_flag = any(s0.date() == phase[0]["date"]
                         and s0 <= datetime.strptime("%s %s" % (phase[0]["date"], phase[-1]["hhmm"]),
                                                     "%Y-%m-%d %H:%M")
                         and e0 >= datetime.strptime("%s %s" % (phase[0]["date"], phase[0]["hhmm"]),
                                                     "%Y-%m-%d %H:%M")
                         for s0, e0 in down_inst)
            phases.append({
                "date": phase[0]["date"], "from": phase[0]["hhmm"], "to": phase[-1]["hhmm"],
                "duration_min": minutes(phase[0]["hhmm"], phase[-1]["hhmm"]),
                "entry": round(entry, 2), "exit": round(phase[-1]["close"], 2),
                "pts": int(round(move)), "pct": round(100.0 * move / entry, 2) + 0.0,
                "mfe": round(mfe, 1) + 0.0 if mfe is not None else None,
                "n_bars": len(phase),
                "ends_at_close": phase[-1]["hhmm"] == PX_LAST_5M,
                "momentors": px_momentor_string(mom, phase[0]["date"], phase[0]["hhmm"], x_flag),
                "price_action": pa,
                "price_action_band": band, "band_basis": basis,
            })
            i = j + 1

    pts_all = sorted(p["pts"] for p in phases)
    out = {
        "definition": (
            "23PM_5PM downtrend phases (machine mirror of the hand-marked "
            "uptrend phases): maximal same-day contiguous runs of 2H+30m+5m "
            "PVT/MACD all DOWN (2H/30m PVT sign -1, ff MACD_2452 down on both, "
            "5m PVT -1, 5m MACD_2452 < Signal). pts = close-to-close over the "
            "phase in trend direction (positive = down-move); mfe = deepest "
            "low below the phase-start close."),
        "n_episodes": len(phases),
        "n_positive": sum(1 for p in pts_all if p > 0),
        "n_single_bar": sum(1 for p in phases if p["n_bars"] == 1),
        "n_ending_at_close": sum(1 for p in phases if p["ends_at_close"]),
        "total_pts": round(sum(pts_all), 1),
        "mean_pts": round(sum(pts_all) / len(pts_all), 2) if pts_all else None,
        "median_pts": pts_all[len(pts_all) // 2] if pts_all else None,
        "episodes": phases,
    }
    return out


def q_2px_3pm(bars_5m, bars_1m, direction="up"):
    """2PX_3PM family: the 2H PVT cross BEFORE the 2H MACDs confirm.

    Trigger (up): 2H PVT sign -1 -> +1; (down): +1 -> -1 (carried values; the
    flip prints at the 2H close bar) with BOTH 2H MACD1 (12/26) and MACD2
    (24/52) still in their PRE-CROSS state (up: both still down; down: both
    still up) - the early window where the 2H PVT leads and the MACDs lag.

    Context names the variant (30m carried states, ff from 30m closes, on the
    trend's side): 30m PVT & M1 & M2 -> 2PX_3PM; M1 only -> 2PX_3PM1;
    M2 only -> 2PX_3PM2; neither -> 2PX_3P. Suffix O when the 30m VOC is on
    the trend's side of zero (above 0 for up / below 0 for down) at trigger.

    Price Action = the major rungs reclaimed by the 2H-PVT run (px_support_label
    at the current 1m PVT cross; run horizon = the 2H-PVT run).
    Forward moves = close-to-close from the trigger, in trend direction."""
    from datetime import datetime
    s = 1 if direction == "up" else -1
    want = direction == "up"      # ff MACD state = "uptrending" (up) / "downtrending" (down)
    pre = not want                # the 2H MACDs' pre-cross state
    last30_1 = last30_2 = last2h_1 = last2h_2 = None
    for b in bars_5m:
        if b.get("30m_macd_1226") is not None and b.get("30m_signal_1226") is not None:
            last30_1 = b["30m_macd_1226"] > b["30m_signal_1226"]
        if b.get("30m_macd") is not None and b.get("30m_signal") is not None:
            last30_2 = b["30m_macd"] > b["30m_signal"]
        if b.get("2h_macd_1226") is not None and b.get("2h_signal_1226") is not None:
            last2h_1 = b["2h_macd_1226"] > b["2h_signal_1226"]
        if b.get("2h_macd") is not None and b.get("2h_signal") is not None:
            last2h_2 = b["2h_macd"] > b["2h_signal"]
        b["f30m1"], b["f30m2"], b["f2h1"], b["f2h2"] = last30_1, last30_2, last2h_1, last2h_2

    mom = compute_momentor_ages(bars_5m)
    stamp5 = {(x["date"], x["hhmm"]): x for x in bars_5m}
    idx5 = {(x["date"], x["hhmm"]): i for i, x in enumerate(bars_5m)}
    one = {}
    for b in bars_1m:
        if b["hhmm"] <= PX_LAST_1M and b.get("close") is not None:
            one.setdefault(b["date"], []).append(b)

    episodes = []
    skipped_macd_confirmed = 0
    for run in contiguous_runs(bars_5m):
        day5 = [x for x in run if x["hhmm"] <= PX_LAST_5M]
        for i in range(1, len(day5)):
            p, b = day5[i - 1], day5[i]
            if not (p["2h_pvt_sign"] == -s and b["2h_pvt_sign"] == s):
                continue
            if not (b["f2h1"] is pre and b["f2h2"] is pre):
                skipped_macd_confirmed += 1   # a 2H MACD already crossed - not the early window
                continue
            if b["30m_pvt_sign"] != s:
                continue                      # 30m not aligned - not this family
            m1 = b["f30m1"] is want
            m2 = b["f30m2"] is want
            if m1 and m2:
                variant = "2PX_3PM"
            elif m1:
                variant = "2PX_3PM1"
            elif m2:
                variant = "2PX_3PM2"
            else:
                variant = "2PX_3P"
            voc = b.get("30m_voc")
            voc_ok = voc is not None and (voc > 0 if direction == "up" else voc < 0)
            name = variant + ("O" if voc_ok else "")
            entry = b["close"]
            fwd = {}
            for k, key in ((3, "r15m"), (6, "r30m"), (12, "r60m")):
                v = round(day5[i + k]["close"] - entry, 1) if i + k < len(day5) else None
                fwd[key] = v if direction == "up" else (-v if v is not None else None)
            v = round(day5[-1]["close"] - entry, 1)
            fwd["r_eod"] = v if direction == "up" else -v
            # Price Action: the majors the 2H-PVT run reclaimed (user support rule)
            x1, leg_ext = px_current_1m_cross(one, b["date"], b["hhmm"], direction)
            mext = px_run_move_ext(bars_5m, idx5, b["date"], b["hhmm"], direction,
                                   sign_col="2h_pvt_sign")
            if x1 is not None:
                t5c = stamp5.get((x1["date"], session_stamp(x1["hhmm"], 5))) or b
                pa, basis = px_support_label(x1, t5c, direction, leg_ext, mext), "1m"
            else:
                pa, basis = px_support_label(b, b, direction, None, mext), "5m"
            x_flag = px_stoch_confirmed(one, x1["date"], x1["hhmm"], direction) \
                if x1 is not None else False
            episodes.append({
                "date": b["date"], "from": b["hhmm"], "variant": name,
                "entry": round(entry, 2), "direction": direction,
                "m1": m1, "m2": m2,
                "voc": round(voc, 1) + 0.0 if voc is not None else None,
                "momentors": px_momentor_string(mom, b["date"], b["hhmm"], x_flag),
                "price_action": pa, "band_basis": basis,
                **fwd,
            })

    from collections import Counter
    vc = Counter(e["variant"] for e in episodes)
    out = {
        "definition": (
            "2PX_3PM family (%s) = 2H PVT %s with BOTH 2H MACD1 (12/26) and "
            "MACD2 (24/52) still in their pre-cross state (ff) - the early "
            "window where the 2H PVT leads. The 30m has already turned (30m "
            "PVT on the trend's side); the 30m MACD states name the variant. "
            "Trailing O = the 30m VOC on the trend's side of zero. "
            "r15m/r30m/r60m/r_eod = forward moves from the trigger in trend "
            "direction. price_action = the majors the 2H-PVT run reclaimed.") % (
            direction,
            "cross-up (-1 -> +1)" if direction == "up" else "cross-down (+1 -> -1)"),
        "n_episodes": len(episodes),
        "skipped_2h_macd_already_crossed": skipped_macd_confirmed,
        "variants": dict(vc),
        "episodes": episodes,
    }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Multi-TF trend insight from 5m export")
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--q2h", action="store_true",
                    help="query: 2H uptrend survival by D-trend context")
    ap.add_argument("--q5mxc", action="store_true",
                    help="query: 5m PVT cross-up context & follow-through by HTF alignment")
    ap.add_argument("--q1m", action="store_true",
                    help="query: 1m EMA price action joined to 5m-file HTF regime")
    ap.add_argument("--q23pm", action="store_true",
                    help="query: 23PM/23PM_5PM setup - where rises start (1m EMAs)")
    ap.add_argument("--q23pm5px", action="store_true",
                    help="query: 23PM_5PX - 5m PVT cross-up in 2PM+3PM up, EMA4 exit")
    ap.add_argument("--q2pm3px", action="store_true",
                    help="query: 2PM_3PX - 30m PVT cross-up in 2PM up, 5m EMA21 exit")
    ap.add_argument("--q23pm5pxd", action="store_true",
                    help="query: 23PM_5PX downtrend mirror (5m PVT cross-down in 2PM+3PM down)")
    ap.add_argument("--q2pm3pxd", action="store_true",
                    help="query: 2PM_3PX downtrend mirror (30m PVT cross-down in 2PM down)")
    ap.add_argument("--q23pm5pmd", action="store_true",
                    help="query: 23PM_5PM downtrend phases (machine mirror of the uptrend phases)")
    ap.add_argument("--q2px3pm", action="store_true",
                    help="query: 2PX_3PM family - 2H PVT cross-up before the 2H MACDs confirm")
    ap.add_argument("--q2px3pmd", action="store_true",
                    help="query: 2PX_3PM family downtrend mirror (2H PVT cross-down, MACDs yet to cross down)")
    ap.add_argument("--csv-1m", default=os.path.join("data", "insight", "csv", "NSE_NIFTY1!, 1m.csv"))
    args = ap.parse_args(argv)

    if not os.path.exists(args.csv):
        raise SystemExit("Missing CSV: %s" % args.csv)

    if args.verify:
        bars = load_bars(args.csv)
        print("Loaded %d bars from %s" % (len(bars), args.csv))
        print(json.dumps(verify(bars), indent=1))
    if args.q2h:
        bars = load_bars(args.csv)
        dev = [b for b in bars if DEV_START <= b["date"] <= DEV_END]
        print(json.dumps(q_2h_uptrend_by_d_context(dev), indent=1))
    if args.q5mxc:
        bars = load_bars(args.csv)
        dev = [b for b in bars if DEV_START <= b["date"] <= DEV_END]
        print(json.dumps(q_5m_crossup_context(dev), indent=1))
    if args.q1m:
        bars = load_bars(args.csv)
        one_m = load_1m_bars(args.csv_1m)
        print("1m bars loaded: %d" % len(one_m))
        print(json.dumps(q_1m_ema_vs_htf(bars, one_m), indent=1))
    if args.q23pm:
        bars = load_bars(args.csv)
        one_m = load_1m_bars(args.csv_1m)
        res = q_23pm(bars, one_m)
        with open(os.path.join("data", "insight", "reports", "q23pm.json"), "w") as f:
            json.dump(res, f, indent=1)
        print(json.dumps(res, indent=1))
    if args.q23pm5px:
        bars = load_bars(args.csv)
        one_m = load_1m_bars(args.csv_1m)
        print(json.dumps(q_23pm_5px(bars, one_m), indent=1))
    if args.q2pm3px:
        bars = load_bars(args.csv)
        one_m = load_1m_bars(args.csv_1m)
        print(json.dumps(q_2pm_3px(bars, one_m), indent=1))
    if args.q23pm5pxd:
        bars = load_bars(args.csv)
        one_m = load_1m_bars(args.csv_1m)
        print(json.dumps(q_23pm_5px(bars, one_m, direction="down"), indent=1))
    if args.q2pm3pxd:
        bars = load_bars(args.csv)
        one_m = load_1m_bars(args.csv_1m)
        print(json.dumps(q_2pm_3px(bars, one_m, direction="down"), indent=1))
    if args.q23pm5pmd:
        bars = load_bars(args.csv)
        one_m = load_1m_bars(args.csv_1m)
        print(json.dumps(q_23pm_5pm_down(bars, one_m), indent=1))
    if args.q2px3pm:
        bars = load_bars(args.csv)
        one_m = load_1m_bars(args.csv_1m)
        print(json.dumps(q_2px_3pm(bars, one_m), indent=1))
    if args.q2px3pmd:
        bars = load_bars(args.csv)
        one_m = load_1m_bars(args.csv_1m)
        print(json.dumps(q_2px_3pm(bars, one_m, direction="down"), indent=1))
    if args.build:
        build(args.csv)


if __name__ == "__main__":
    main()
