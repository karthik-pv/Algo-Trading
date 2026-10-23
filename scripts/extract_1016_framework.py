"""Extract Oct 16 through the user's reference framework:
  3PM up + 5PM up -> the retrace to 1E9/21/50 -> the rise (the support taken)
      -> the 1m PVT retrace-down + the cross-up
  3PM up + 5PM up with M2 retracing -> the retrace to 1E100/200 -> the rise
      -> the 5m PVT retrace-down + the cross-up
Signals: the 15m MACD swing starts (the proxy), the 1m PVT crosses, the 1m
MACD2 crosses, the 1PME50 (the price vs the 1m EMA50), the 5m PVT crosses.
"""
import csv
import os
from collections import defaultdict

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = os.path.join(REPO, "data", "insight", "csv") + "/"
DAY = "2026-10-16"


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

rows1 = sorted([r for r in r1 if r.get("time") and r["time"][:10] == DAY], key=lambda r: r["time"])
rows5 = sorted([r for r in r5 if r.get("time") and r["time"][:10] == DAY], key=lambda r: r["time"])
rows30 = sorted([r for r in r30 if r.get("time") and r["time"][:10] == DAY], key=lambda r: r["time"])

# the 15m MACD (24/52, sig 9) over the 15m close proxies (the last 1m close per 15m window)
c15 = []
rows1s = sorted(rows1, key=lambda r: r["time"])
for k in range(0, len(rows1s), 15):
    chunk = rows1s[k:k + 15]
    c15.append((chunk[-1]["time"][11:16], f(chunk[-1]["close"])))
l15 = [a - b for a, b in zip(ema_chain([c for _, c in c15], 24), ema_chain([c for _, c in c15], 26))]
s15 = ema_chain(l15, 9)
m15 = {}
for i, (hh, _) in enumerate(c15):
    m15[hh] = {"line": l15[i], "sig": s15[i],
               "prevline": l15[i - 1] if i else None,
               "prevsig": s15[i - 1] if i else None}

# ---- the 30m ----
print("=== 30m ===")
pp = None
for r in rows30:
    pvt = f(r["PVT"])
    m2, s2 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
    tr = "" if pp is None else ("3PVT rising" if pvt > pp else "3PVT falling")
    print("  %s 3PVT %+7.4f %-13s 3M2 %8.2f/%8.2f %s" % (
        r["time"][11:16], pvt, tr, m2, s2, "M2up" if m2 > s2 else "M2dn"))
    pp = pvt

# ---- the 5m ----
print("\n=== 5m ===")
prev = None
for r in rows5:
    pvt = f(r["PVT"])
    m2, s2 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
    st = "5PM-up" if (pvt > 0 and m2 > s2) else ("5PMdn" if (pvt < 0 and m2 < s2) else
         ("5M2-retrace(5Pup)" if pvt > 0 else "5Pdn-M2up"))
    x = ""
    if prev:
        pm2, ps2 = f(prev["MACD Line#2"]), f(prev["Signal Line#2"])
        if (m2 > s2) != (pm2 > ps2):
            x = " <<< 5M2X %s" % ("UP" if m2 > s2 else "DOWN")
        elif (pvt > 0) != ((f(prev["PVT"]) or 0) > 0):
            x = " <<< 5m PVT cross %s" % ("UP" if pvt > 0 else "DOWN")
        elif abs(m2 - pm2) > 3:
            x = " (M2 %s)" % ("curling up" if m2 > pm2 else "rolling over")
    print("  %s 5PVT %+7.3f 5M2 %8.2f/%8.2f gap %+7.2f  %-18s%s" % (
        r["time"][11:16], pvt, m2, s2, m2 - s2, st, x))
    prev = r

# ---- the 1m events ----
print("\n=== 1m events ===")
RUNGS = [("1E9", "1m EMA9"), ("1E21", "1m EMA21"), ("1E50", "1m EMA50"),
         ("1E100", "1m EMA100"), ("1E200", "1m EMA200"),
         ("5E50", "5m EMA50"), ("5E100", "5m EMA100"), ("5E200", "5m EMA200")]
prev_c = None
prev_m2 = prev_s2 = None
prev_l15 = None
prev_above50 = None
for r in rows1:
    c = f(r["close"])
    if c is None:
        continue
    m = mins(r["time"])
    hi, lo = f(r["high"]), f(r["low"])
    tags = []
    for nm, key in RUNGS:
        lv = f(r[key])
        if lv is None or prev_c is None or hi is None or lo is None:
            continue
        if prev_c > lv and lo <= lv and c > lv:
            tags.append("ST-%s" % nm)          # the support taken (the dip held)
        elif prev_c > lv and c < lv:
            tags.append("SB-%s" % nm)          # the support broken
        elif prev_c < lv and hi >= lv and c < lv:
            tags.append("RF-%s" % nm)          # the resistance faced (the pop rejected)
        elif prev_c < lv and c > lv:
            tags.append("RB-%s" % nm)          # the resistance broken (the reclaim)
    xup = (r.get("Cross Up") or "").strip() != ""
    xdn = (r.get("Cross Down") or "").strip() != ""
    if xup:
        tags.append("1PX-UP")
    if xdn:
        tags.append("1PX-DN")
    m2, s2 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
    if m2 is not None and s2 is not None and prev_m2 is not None and prev_s2 is not None:
        if m2 > s2 and prev_m2 <= prev_s2:
            tags.append("1M2X-UP")
        if m2 < s2 and prev_m2 >= prev_s2:
            tags.append("1M2X-DN")
    above50 = (f(r["1m EMA50"]) is not None and c > f(r["1m EMA50"]))
    if prev_above50 is not None and above50 != prev_above50:
        tags.append("1PME50-%s" % ("trend-start" if above50 else "retrace-start"))
    # the 15m MACD swing crosses at the 15m closes
    hh = r["time"][11:16]
    if hh in m15 and m15[hh]["prevline"] is not None:
        v = m15[hh]
        if v["line"] > v["sig"] and (v["prevline"] <= v["prevsig"]):
            tags.append("15SW-start")
        elif v["line"] < v["sig"] and (v["prevline"] >= v["prevsig"]):
            tags.append("15SW-retrace")
    if tags:
        print("  %s C=%-8s %s" % (r["time"][11:16], "%.1f" % c, " ".join(tags)))
    prev_c = c
    prev_m2, prev_s2 = m2, s2
    prev_above50 = above50
