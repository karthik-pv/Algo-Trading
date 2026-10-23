"""Extract Oct 19's phase structure for the user's framework commentary:

Phase 1: 3P downtrending + 5PM downtrending -> the retrace-ups to 1E9/21/50
         and the falls from there (the triggers: the 15s-MACD2-proxy cross-down /
         the 1m PVT cross-down + the 15s-MACD2-proxy cross-down with the 1m MACD2
         already down / the 1m PVT cross-down + the 1m MACD2 cross-down).
Phase 2: the 5m MACD2 retracing UP (the 5P still red) -> the price keeps falling
         from 1E100/200 with the 1m PVT + the 1m MACD2 cross-downs.

The 15s MACD2 (24/52 of 15s) has no Oct 19 data -> the span-matched proxy =
MACD(6,13) of the 1m closes, the signal EMA2 (the same decay span).
"""
import csv
import os
from collections import defaultdict

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = os.path.join(REPO, "data", "insight", "csv") + "/"
DAY = "2026-10-19"


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


def mins(t):
    return int(t[11:13]) * 60 + int(t[14:16])


def ema_chain(vals, period, seed=None):
    a = 2.0 / (period + 1)
    e = seed
    out = []
    for v in vals:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


r1 = load_named(CSV + "NSE_NIFTY1!, 1_20adf.csv")
r5 = load_named(CSV + "NSE_NIFTY1!, 5_be22a.csv")
r30 = load_named(CSV + "NSE_NIFTY1!, 30_42a03.csv")

rows1 = sorted([r for r in r1 if r.get("time") and r["time"][:10] == DAY],
               key=lambda r: r["time"])
rows5 = sorted([r for r in r5 if r.get("time") and r["time"][:10] == DAY],
               key=lambda r: r["time"])
rows30 = sorted([r for r in r30 if r.get("time") and r["time"][:10] == DAY],
                key=lambda r: r["time"])

# the 15s-MACD2 proxy over the 1m closes (the continuous chain)
closes = [f(r["close"]) for r in r1]
l = [a - b for a, b in zip(ema_chain(closes, 6), ema_chain(closes, 13))]
s = ema_chain(l, 2)
lmap = {id(r): i for i, r in enumerate(r1)}
for r, li, si, lp in zip(r1, l, s, [None] + l[:-1]):
    r["_p15"], r["_s15"], r["_p15p"] = li, si, lp

# ---- the 30m timeline ----
print("=== 30m (the 3P trend + the 3M2) ===")
prev_pvt = prev_m2 = None
for r in rows30:
    pvt = f(r["PVT"])
    m2, s2 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
    trend = ""
    if prev_pvt is not None:
        trend = "PVT falling" if pvt < prev_pvt else "PVT rising"
    x = ""
    if prev_m2 is not None and m2 is not None and prev_m2 is not None:
        up_now, up_prev = m2 > s2, prev_m2 > s2
        if up_now != up_prev:
            x = "  <<< 3M2X %s" % ("UP" if up_now else "DOWN")
    print("  %s  C=%s  3PVT %+7.4f (%s)  3M2 %8.2f/%8.2f%s" % (
        r["time"][11:16], r["close"][:7], pvt, trend, m2, s2, x))
    prev_pvt, prev_m2 = pvt, m2

# ---- the 5m timeline ----
print("\n=== 5m (the 5P sign + the 5M2 state) ===")
for i, r in enumerate(rows5):
    pvt = f(r["PVT"])
    m2, s2 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
    prev = rows5[i - 1] if i else None
    pm2 = f(prev["MACD Line#2"]) if prev else None
    ps2 = f(prev["Signal Line#2"]) if prev else None
    state = "5PMdn" if (pvt < 0 and m2 < s2) else ("5M2-retrace-up" if (pvt < 0 and m2 > s2) else "")
    x = ""
    if prev and pm2 is not None:
        if (m2 > s2) != (pm2 > ps2):
            x = "  <<< 5M2X %s" % ("UP" if m2 > s2 else "DOWN")
        elif abs(m2 - pm2) > 3:
            x = "  (M2 %s)" % ("curling" if m2 > pm2 else "rolling")
    print("  %s  C=%s  5PVT %+6.3f  5M2 %8.2f/%8.2f gap %+7.2f  %-14s%s" % (
        r["time"][11:16], r["close"][:7], pvt, m2, s2, m2 - s2, state, x))

# ---- the 1m events ----
print("\n=== 1m events (the rung touches + the crosses) ===")
def near(c, lvl, tol=6):
    return lvl is not None and abs(c - lvl) <= tol

p1 = rows1[0]
p_prev = None
prev_m12 = prev_s12 = None
prev_p15 = prev_s15 = None
for i, r in enumerate(rows1):
    c = f(r["close"])
    if c is None:
        continue
    m = mins(r["time"])
    e9, e21, e50 = f(r["1m EMA9"]), f(r["1m EMA21"]), f(r["1m EMA50"])
    e100, e200 = f(r["1m EMA100"]), f(r["1m EMA200"])
    pvt = f(r["PVT"])
    pvt_xdn = (r.get("Cross Down") or "").strip() != ""
    m12, s12 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
    m12_xdn = prev_m12 is not None and s12 is not None and (m12 < s12) and prev_m12 >= prev_s12
    p15, s15 = r["_p15"], r["_s15"]
    p15_xdn = prev_p15 is not None and prev_s15 is not None and (p15 < s15) and prev_p15 >= prev_s15
    tags = []
    for nm, lv in (("1E9", e9), ("1E21", e21), ("1E50", e50), ("1E100", e100), ("1E200", e200)):
        if lv is not None and p_prev is not None and f(p_prev["close"]) is not None:
            if f(p_prev["close"]) > lv >= c:
                tags.append("touch-%s(v%s)" % (nm, "%.1f" % lv))
            elif f(p_prev["close"]) < lv <= c:
                tags.append("claim-%s" % nm)
    if pvt_xdn or (p_prev is not None and (f(p_prev["PVT"]) or 0) > 0 and (pvt or 0) < 0):
        tags.append("1PX-DN")
    if m12_xdn:
        tags.append("1M2X-DN")
    if p15_xdn:
        tags.append("15M2X-DN(proxy)")
    if tags:
        print("  %s  C=%-8s %s" % (r["time"][11:16], ("%.1f" % c), " ".join(tags)))
    p_prev = r
    prev_m12, prev_s12 = m12, s12
    prev_p15, prev_s15 = p15, s15
