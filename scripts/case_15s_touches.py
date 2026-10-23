"""Case studies on the REAL 15s MACD (the only 15s data: Oct 14 11:28 ->
Oct 16 15:39). For each 1E100/200 touch (3PM up + 5m M2 retracing), walk the
15s bars: when did the 15s MACD line swing up / cross its signal, where was
price, and what happened over the next 30 min.
"""
import csv
import os
import statistics
from collections import defaultdict

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = os.path.join(REPO, "data", "insight", "csv")
F15S = os.path.join(CSV, "NSE_NIFTY1!, 15S_3c57c.csv")
F5 = os.path.join(CSV, "NSE_NIFTY1!, 5_be22a.csv")
F30 = os.path.join(CSV, "NSE_NIFTY1!, 30_42a03.csv")


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
    hh, mm, ss = int(t[11:13]), int(t[14:16]), int(t[17:19])
    return hh * 3600 + mm * 60 + ss


r15 = load_named(F15S)
r5 = load_named(F5)
r30 = load_named(F30)

# the 30m / 5m states (the same carry conventions as before)
s30 = {}
for r in r30:
    if r.get("time"):
        m2, s2 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
        s30[(r["time"][:10], r["time"][11:16])] = {
            "pvt": (f(r["PVT"]) or 0) > 0,
            "m2_up": (m2 > s2) if (m2 is not None and s2 is not None) else None}
dates30 = sorted({d for d, _ in s30})


def carry30(date, sec):
    m = sec // 60
    target = m - 29
    if target < 555:
        i = dates30.index(date)
        if i == 0:
            return None
        return s30.get((dates30[i - 1], "15:15"))
    s = 555 + ((target - 555) // 30) * 30
    return s30.get((date, "%02d:%02d" % (s // 60, s % 60)))


m5 = {}
prevrow = None
prev_by_day = {}
for r in r5:
    t = r.get("time")
    if not t:
        continue
    d, hh = t[:10], t[11:16]
    m2, s2 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
    if m2 is None or s2 is None:
        continue
    prev = prev_by_day.get(d) or (prevrow if prevrow and prevrow[0] != d else None)
    m5[(d, hh)] = {"m2": m2, "s2": s2, "prev": prev}
    prev_by_day[d] = (d, hh, m2, s2)
    prevrow = (d, hh, m2, s2)


def carry5(date, m):
    target = m - 4
    if target < 555:
        i = dates30.index(date)
        if i == 0:
            return None
        return m5.get((dates30[i - 1], "15:35"))
    s = 555 + ((target - 555) // 5) * 5
    return m5.get((date, "%02d:%02d" % (s // 60, s % 60)))


# group the 15s bars per day
day15 = defaultdict(list)
for r in r15:
    if r.get("time"):
        day15[r["time"][:10]].append(r)

print("15s data: %s .. %s (%d bars)" % (
    r15[0]["time"], r15[-1]["time"], len(r15)))

for date in sorted(day15.keys()):
    rows = sorted(day15[date], key=lambda r: r["time"])
    for level_name in ("1E100", "1E200"):
        lvl_key = "1m EMA" + level_name[2:]
        armed = True
        for j in range(1, len(rows)):
            cur, prev = rows[j], rows[j - 1]
            c, p, lo = f(cur["close"]), f(prev["close"]), f(cur["low"])
            lvl = f(cur[lvl_key])
            if None in (c, p, lo, lvl):
                continue
            sec = mins(cur["time"])
            st30 = carry30(date, sec // 60)
            c5 = carry5(date, sec // 60)
            if not st30 or not c5 or not st30["pvt"] or not st30["m2_up"] or not c5["prev"]:
                continue
            m2, s2 = c5["m2"], c5["s2"]
            m2p, s2p = c5["prev"][2], c5["prev"][3]
            if not (m2 > s2 and (m2 < m2p or (m2 - s2) < (m2p - s2p))):
                continue
            touch = p > lvl >= lo
            if not touch:
                if c > lvl:
                    armed = True
                continue
            if not armed:
                continue
            armed = False
            # walk the next 30 min of 15s bars
            fut = rows[j + 1:j + 121]
            m15_line, m15_sig = f(cur["MACD Line"]), f(cur["Signal Line"])   # the 15s MACD = cols 29-30
            swing_t = cross_t = None
            for k, r_ in enumerate(fut):
                ln, sg = f(r_["MACD Line"]), f(r_["Signal Line"])
                pln = f(rows[j + k]["MACD Line"])
                if ln is None or sg is None:
                    continue
                if swing_t is None and pln is not None and ln > pln:
                    swing_t = (k * 15 / 60.0, f(r_["close"]))
                if cross_t is None and ln > sg and (f(rows[j + k - 1]["MACD Line"]) or 0) <= (f(rows[j + k - 1]["Signal Line"]) or 1):
                    cross_t = (k * 15 / 60.0, f(r_["close"]))
            c30 = f(fut[119]["close"]) if len(fut) > 119 else None
            hs = [f(x["high"]) for x in fut if f(x["high"]) is not None]
            ls = [f(x["low"]) for x in fut if f(x["low"]) is not None]
            fav = (max(hs) - c) if hs else None
            adv = (c - min(ls)) if ls else None
            print("\n%s %s touch %s @ %.1f (close %.1f)" % (
                date, cur["time"][11:19], level_name, lvl, c))
            print("   15s MACD at touch: line %.2f / sig %.2f" % (
                m15_line if m15_line is not None else float("nan"),
                m15_sig if m15_sig is not None else float("nan")))
            if swing_t:
                print("   15s MACD line swung UP +%.1f min @ %.1f  (+%.1f from touch-close)" % (
                    swing_t[0], swing_t[1], swing_t[1] - c))
            else:
                print("   15s MACD line never swung up in 30 min")
            if cross_t:
                print("   15s MACD crossed signal UP +%.1f min @ %.1f" % (
                    cross_t[0], cross_t[1]))
            print("   next 30min: close %+v".replace("+v", "s") % () if False else
                  "   next 30min: C30 %s | max-fav %+.1f | max-adv %+.1f" % (
                      ("%.1f" % (c30 - c)) if c30 is not None else "n/a",
                      fav if fav is not None else 0, adv if adv is not None else 0))
