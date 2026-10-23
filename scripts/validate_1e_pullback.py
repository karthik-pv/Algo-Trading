"""Validate the 5PM-1E50/1E100/1E200 pullback setup (user insight, 20 Oct 2026).

Bull: 30m PVT > 0 (last COMPLETED 30m bar), 5PM up (5m PVT > 0 + 5m MACD2 >
signal). Price retraces down to 1m EMA50 and rises from it (bar low <= 1E50
while the prior close > 1E50, bar close back above).
  Sub-insight: when 5P is up but 5M2 is RETRACING (falling / gap shrinking),
  the pullback extends to 1E100/1E200 and price rises from THERE.

Bear: mirror (30m PVT < 0, 5m PVT < 0 + 5M2 < signal, the high >= 1E50 with
the prior close < 1E50, the close back below; extension up to 1E100/200).

Window: last 1 year (2025-10-20 .. 2026-10-19), 5m resolution (the 1m EMA
columns are the carries at the 5m close, verified). Outcomes are raw proxies:
fwd15/30 = the close N bars later, EOD = the day's last 5m close, all minus
the event close. Hold = the min low over the next 3 bars stays on the right
side of the level.

Run: venv/bin/python scripts/validate_1e_pullback.py
Out: data/insight/csv/validate_1e_pullback_events.csv
"""
import csv
import os
import statistics
from collections import defaultdict

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = os.path.join(REPO, "data", "insight", "csv")
F5 = os.path.join(CSV, "NSE_NIFTY1!, 5_be22a.csv")
F30 = os.path.join(CSV, "NSE_NIFTY1!, 30_42a03.csv")
OUT = os.path.join(CSV, "validate_1e_pullback_events.csv")

START = "2025-10-20"
END = "2026-10-19"


def load_named(path):
    with open(path, newline="") as f:
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
    return rows


def f(x):
    try:
        return float((x or "").strip())
    except (ValueError, TypeError):
        return None


def sgn(x):
    return None if x is None else (1 if x > 0 else (-1 if x < 0 else 0))


r5 = load_named(F5)
r30 = load_named(F30)

# ---- span-matched EMA proxies for the 1m-EMA rungs -----------------------
# The 5m file's carried 1m EMA columns are only populated from 2026-07-13
# (TradingView's 1m history limit at export). For the full-year study use
# EMAs over the 5m HA closes with matched decay constants:
#   1m EMA50  -> 5m EMA10   (per-5m-bar alpha = 1-(1-2/51)^5)
#   1m EMA100 -> 5m EMA20
#   1m EMA200 -> 5m EMA40
# Validated against the carried values on the Jul 13+ overlap (printed).
closes5_all = [f(r["close"]) for r in r5]


def ema_chain(vals, period, seed=None):
    a = 2.0 / (period + 1)
    e = seed
    out = []
    for v in vals:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


# effective periods from the decay match (see docstring): 10.35 / 20.05 / 40.07
import math
eff = []
for span, a1m in ((50, 2 / 51.0), (100, 2 / 101.0), (200, 2 / 201.0)):
    a5 = 1 - (1 - a1m) ** 5
    eff.append(2.0 / a5 - 1)
p50, p100, p200 = eff[0], eff[1], eff[2]
px50 = ema_chain(closes5_all, p50)
px100 = ema_chain(closes5_all, p100)
px200 = ema_chain(closes5_all, p200)
for r, a, b, c_ in zip(r5, px50, px100, px200):
    r["_px50"], r["_px100"], r["_px200"] = a, b, c_

# validation: proxies vs the carried values where the latter exist
d50 = d100 = d200 = None
diffs = {"50": [], "100": [], "200": []}
for r, a, b, c_ in zip(r5, px50, px100, px200):
    t = r.get("time") or ""
    if t[:10] < "2026-07-13":
        continue
    for key, px in (("50", a), ("100", b), ("200", c_)):
        v = f(r["1m EMA" + key])
        if v is not None:
            diffs[key].append(abs(v - px))
for k, v in diffs.items():
    if v:
        print("proxy 1mEMA%s vs carried: n=%d median=%.2f pts max=%.2f pts" % (
            k, len(v), statistics.median(v), max(v)))

# ---- 30m PVT sign per (date, stamp); the completed-bar carry per 5m bar ----
pvt30 = {}
for r in r30:
    if r.get("time"):
        pvt30[(r["time"][:10], r["time"][11:16])] = sgn(f(r["PVT"]))

dates30 = sorted({d for d, _ in pvt30})


def completed_30m_key(date, hhmm):
    """the last 30m bar COMPLETED by this 5m bar's close (stamp+5min)"""
    m = int(hhmm[:2]) * 60 + int(hhmm[3:])
    close_m = m + 5                       # the 5m bar closes at +5 min
    target = close_m - 30                 # the last 30m bar closed: S+30 <= close_m
    if target < 555:                      # before 09:45: the previous day's last bar
        i = dates30.index(date)
        if i == 0:
            return None
        return (dates30[i - 1], "15:15")
    s = 555 + ((target - 555) // 30) * 30
    return (date, "%02d:%02d" % (s // 60, s % 60))


# ---- index the 5m bars per day ----
day5 = defaultdict(list)
for r in r5:
    t = r.get("time")
    if t and START <= t[:10] <= END:
        day5[t[:10]].append(r)
dates5 = sorted(day5.keys())

events = []

for di, date in enumerate(dates5):
    bars = day5[date]
    for j in range(1, len(bars)):
        cur, prev = bars[j], bars[j - 1]
        if cur["time"][11:16] <= prev["time"][11:16]:
            continue                       # a day-boundary duplicate guard
        c, p = f(cur["close"]), f(prev["close"])
        lo, hi = f(cur["low"]), f(cur["high"])
        if None in (c, p, lo, hi):
            continue
        k30 = completed_30m_key(date, cur["time"][11:16])
        s30 = pvt30.get(k30) if k30 else None
        p5 = sgn(f(cur["PVT"]))
        m2, s2 = f(cur["MACD Line#2"]), f(cur["Signal Line#2"])
        m2p, s2p = f(prev["MACD Line#2"]), f(prev["Signal Line#2"])
        if None in (m2, s2, m2p, s2p) or s30 is None or p5 is None:
            continue
        m2_up = m2 > s2
        m2_retracing = m2 < m2p or (m2 - s2) < (m2p - s2p)
        e50, e100, e200 = cur["_px50"], cur["_px100"], cur["_px200"]
        if None in (e50, e100, e200):
            continue

        # future closes/lows within the day for outcomes
        fut = bars[j + 1:]
        c15 = f(fut[2]["close"]) if len(fut) > 2 else None
        c30 = f(fut[5]["close"]) if len(fut) > 5 else None
        eod = f(bars[-1]["close"])
        nxt_c = f(fut[0]["close"]) if fut else None
        lows6 = [f(x["low"]) for x in fut[:6] if f(x["low"]) is not None]
        highs6 = [f(x["high"]) for x in fut[:6] if f(x["high"]) is not None]
        maxup30 = (max(highs6) - c) if highs6 else None
        maxdn30 = (c - min(lows6)) if lows6 else None

        def rec(side, level_name, level, hold):
            # curl = the 5M2 gap widening again on the next bar (the retrace ending)
            if fut and f(fut[0]["MACD Line#2"]) is not None and f(fut[0]["Signal Line#2"]) is not None:
                curl = (f(fut[0]["MACD Line#2"]) - f(fut[0]["Signal Line#2"])) > (m2 - s2)
                entry_c = f(fut[0]["close"])
                entry_f30 = (f(fut[5]["close"]) - entry_c) if len(fut) > 5 and f(fut[5]["close"]) is not None else None
                entry_eod = (eod - entry_c) if (eod is not None and entry_c is not None) else None
            else:
                curl = entry_c = entry_f30 = entry_eod = None
            events.append({
                "date": date, "time": cur["time"][11:16], "side": side,
                "level": level_name, "m2_state": ("retrace" if m2_retracing else "rising"),
                "event_close": c, "hold_3bar": hold,
                "curl_next": curl,
                "entry_close": entry_c, "entry_f30": entry_f30, "entry_eod": entry_eod,
                "soft_hold": ((f(fut[2]["close"]) > level) if (side == "bull" and len(fut) > 2 and f(fut[2]["close"]) is not None)
                              else ((f(fut[2]["close"]) < level) if (side == "bear" and len(fut) > 2 and f(fut[2]["close"]) is not None)
                                    else None)),
                "confirmed": ((f(fut[0]["close"]) > c) if (side == "bull" and fut and f(fut[0]["close"]) is not None)
                              else ((f(fut[0]["close"]) < c) if (side == "bear" and fut and f(fut[0]["close"]) is not None)
                                    else None)),
                "fwd15": (c15 - c) if c15 is not None else None,
                "fwd30": (c30 - c) if c30 is not None else None,
                "fwd_eod": (eod - c) if eod is not None else None,
                "maxup30": (max(highs6) - c) if (side == "bull" and highs6) else ((c - min(lows6)) if lows6 else None),
                "maxdn30": (c - min(lows6)) if (side == "bull" and lows6) else ((max(highs6) - c) if highs6 else None),
                "ext_1E100": any(x <= e100 for x in lows6) if side == "bull" else any(x >= e100 for x in highs6),
                "ext_1E200": any(x <= e200 for x in lows6) if side == "bull" else any(x >= e200 for x in highs6),
            })

        # BULL 1E50: regime up + 5PM up + the retrace touched 1E50 + the close back above
        if s30 == 1 and p5 == 1 and m2_up and p > e50 and lo <= e50 < c:
            fut3 = [f(x["low"]) for x in fut[:3] if f(x["low"]) is not None]
            hold = all(x > e50 for x in fut3) if fut3 else None
            rec("bull", "1E50", e50, hold)
        # BULL 1E100/1E200 bounce (deeper rung in the same regime + 5P up)
        if s30 == 1 and p5 == 1 and p > e100 and lo <= e100 < c:
            fut3 = [f(x["low"]) for x in fut[:3] if f(x["low"]) is not None]
            rec("bull", "1E100", e100, all(x > e100 for x in fut3) if fut3 else None)
        if s30 == 1 and p5 == 1 and p > e200 and lo <= e200 < c:
            fut3 = [f(x["low"]) for x in fut[:3] if f(x["low"]) is not None]
            rec("bull", "1E200", e200, all(x > e200 for x in fut3) if fut3 else None)
        # BEAR 1E50
        if s30 == -1 and p5 == -1 and (not m2_up) and p < e50 and hi >= e50 > c:
            fut3 = [f(x["high"]) for x in fut[:3] if f(x["high"]) is not None]
            hold = all(x < e50 for x in fut3) if fut3 else None
            rec("bear", "1E50", e50, hold)
        # BEAR 1E100/1E200
        if s30 == -1 and p5 == -1 and p < e100 and hi >= e100 > c:
            fut3 = [f(x["high"]) for x in fut[:3] if f(x["high"]) is not None]
            rec("bear", "1E100", e100, all(x < e100 for x in fut3) if fut3 else None)
        if s30 == -1 and p5 == -1 and p < e200 and hi >= e200 > c:
            fut3 = [f(x["high"]) for x in fut[:3] if f(x["high"]) is not None]
            rec("bear", "1E200", e200, all(x < e200 for x in fut3) if fut3 else None)

# ---- stats ----
def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else float("nan")


def block(side, level):
    ev = [e for e in events if e["side"] == side and e["level"] == level and "hold_3bar" in e]
    if not ev:
        return
    print("\n%s %s touch-and-reclaim events: n=%d" % (side.upper(), level, len(ev)))
    print("  %-9s %-5s %-8s %-9s %-12s %-12s %-11s %-11s" % (
        "5M2", "n", "hold3", "soft-hold", "reach1E100", "fwd30 med", "EOD med", "conf"))
    for st in ("rising", "retrace"):
        sub = [e for e in ev if e["m2_state"] == st]
        if not sub:
            continue
        holds = [e["hold_3bar"] for e in sub if e["hold_3bar"] is not None]
        softs = [e["soft_hold"] for e in sub if e["soft_hold"] is not None]
        conf = [e["confirmed"] for e in sub if e["confirmed"] is not None]
        f30 = [e["fwd30"] for e in sub if e["fwd30"] is not None]
        fe = [e["fwd_eod"] for e in sub if e["fwd_eod"] is not None]
        ext100 = [e["ext_1E100"] for e in sub]
        print("  %-9s %-5d %-8s %-9s %-12s %-12s %-11s %-11s" % (
            st, len(sub),
            "%.0f%%" % (100.0 * sum(holds) / len(holds)) if holds else "-",
            "%.0f%%" % (100.0 * sum(softs) / len(softs)) if softs else "-",
            "%.0f%%" % (100.0 * sum(ext100) / len(ext100)),
            "%+.1f" % med(f30), "%+.1f" % med(fe),
            "%.0f%%" % (100.0 * sum(conf) / len(conf)) if conf else "-"))


def deep_block(side, level):
    ev = [e for e in events if e["side"] == side and e["level"] == level]
    if not ev:
        return
    print("\n%s %s deeper-rung bounces: n=%d" % (side.upper(), level, len(ev)))
    print("  %-9s %-5s %-12s %-12s %-12s %-12s %-11s" % (
        "5M2", "n", "fwd30>0", "fwd30 med", "max-fav med", "max-adv med", "EOD med"))
    for st in ("rising", "retrace"):
        sub = [e for e in ev if e["m2_state"] == st]
        if not sub:
            continue
        f30 = [e["fwd30"] for e in sub if e["fwd30"] is not None]
        mu = [e["maxup30"] for e in sub if e["maxup30"] is not None]
        md = [e["maxdn30"] for e in sub if e["maxdn30"] is not None]
        fe = [e["fwd_eod"] for e in sub if e["fwd_eod"] is not None]
        pos = sum(1 for x in f30 if x > 0)
        print("  %-9s %-5d %-12s %-12s %-12s %-12s %-11s" % (
            st, len(sub), "%.0f%%" % (100.0 * pos / len(f30)) if f30 else "-",
            "%+.1f" % med(f30), "%+.1f" % med(mu), "%+.1f" % med(md), "%+.1f" % med(fe)))
    # the curl-confirmation entry: the touch bar -> the 5M2 gap widening on the
    # next bar -> enter the next close; the outcome = the next-30-min / the EOD
    cur_l = [e for e in ev if e["curl_next"] and e["entry_f30"] is not None]
    no_l = [e for e in ev if e["curl_next"] is False and e["entry_f30"] is not None]
    good = (lambda x: x > 0) if side == "bull" else (lambda x: x < 0)
    for tag, sub in (("curl-up entry", cur_l), ("no-curl entry", no_l)):
        if not sub:
            continue
        ef = [e["entry_f30"] for e in sub]
        ee = [e["entry_eod"] for e in sub]
        win = sum(1 for x in ef if good(x))
        print("    %-14s n=%-4d next-30min %s %3.0f%% med=%+6.1f | entry->EOD med=%+6.1f" % (
            tag, len(sub), "up" if side == "bull" else "down",
            100.0 * win / len(ef), statistics.median(ef), statistics.median(ee)))


print("=" * 78)
print("VALIDATION — %s .. %s (5m resolution, 1m EMAs as carried at 5m closes)" % (START, END))
print("=" * 78)
for side in ("bull", "bear"):
    block(side, "1E50")
    deep_block(side, "1E100")
    deep_block(side, "1E200")

with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["date", "time", "side", "level", "m2_state",
                                       "event_close", "hold_3bar", "soft_hold", "confirmed",
                                       "curl_next", "entry_close", "entry_f30", "entry_eod",
                                       "fwd15", "fwd30", "fwd_eod", "maxup30", "maxdn30",
                                       "ext_1E100", "ext_1E200"])
    w.writeheader()
    for e in sorted(events, key=lambda x: (x["date"], x["time"], x["side"], x["level"])):
        w.writerow({k: ("" if e.get(k) is None else e[k]) for k in w.fieldnames})
print("\nevents -> %s (%d rows)" % (OUT, len(events)))
