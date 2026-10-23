"""Scan the 30m file for the user's morning setup (calibrated on Oct 19):

  (1) 30m MACD1 downtrending         : macd1 < sig1
  (2) 30m MACD2 poised down, still up: macd2 > sig2, macd2 falling + the
      gap (macd2-sig2) shrinking, 2 consecutive bars
  (3) 30m PVT up but retracing        : pvt > 0 and falling, 2 consecutive
      bars (saturated bars excluded)

Outcomes use a RAW-day proxy (the first 1m HA-close ~ the open print, the
15:29 1m HA-close ~ the session close) because the files' HA O->C inverts
the sign on gap days.
"""
import csv
import os
from collections import defaultdict

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = os.path.join(REPO, "data", "insight", "csv")
F30 = os.path.join(CSV, "NSE_NIFTY1!, 30_42a03.csv")
F1 = os.path.join(CSV, "NSE_NIFTY1!, 1_20adf.csv")


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


r30 = load_named(F30)

day = defaultdict(list)
for r in r30:
    if r.get("time"):
        day[r["time"][:10]].append(r)
dates = sorted(day.keys())

# raw-day proxies from the 30m file itself (works for the whole span):
# ~open = the 09:15 bar's HA-close, ~close = the 15:15 bar's HA-close
rawpx = {}
for d, rows in day.items():
    o_bar = next((f(r["close"]) for r in rows if r["time"][11:16] == "09:15"), None)
    c_bar = next((f(r["close"]) for r in rows if r["time"][11:16] == "15:15"), None)
    if o_bar is not None and c_bar is not None:
        rawpx[d] = (o_bar, c_bar, c_bar - o_bar)


def state_at(prev2, prev1, cur):
    m1, s1 = f(cur["MACD Line"]), f(cur["Signal Line"])
    m2, s2 = f(cur["MACD Line#2"]), f(cur["Signal Line#2"])
    pvt = f(cur["PVT"])
    if None in (m1, s1, m2, s2, pvt):
        return False
    if not (m1 < s1 and m2 > s2 and 0 < pvt < 0.999):
        return False

    def vals(r):
        return (f(r["MACD Line#2"]), f(r["Signal Line#2"]), f(r["PVT"]))
    (m2a, s2a, pa), (m2b, s2b, pb) = vals(prev1), vals(prev2)
    if None in (m2a, s2a, pa, m2b, s2b, pb) or pa >= 0.999 or pb >= 0.999:
        return False
    cond_1 = (m2a < m2b) and ((m2a - s2a) < (m2b - s2b)) and (0 < pa < pb)
    cond_2 = (m2 < m2a) and ((m2 - s2) < (m2a - s2a)) and (0 < pvt < pa)
    return cond_1 and cond_2


bell = []
intraday = []
for i, d in enumerate(dates):
    bars = day[d]
    prev_state = False
    for j in range(2, len(bars)):
        st = state_at(bars[j - 2], bars[j - 1], bars[j])
        t = bars[j]["time"][11:16]
        if st and t == "15:15" and i + 1 < len(dates):
            nxt = dates[i + 1]
            bell.append((d, nxt, rawpx.get(nxt)))
        elif st and not prev_state:
            px = rawpx.get(d)
            lvl = f(bars[j]["close"])
            intraday.append((d, t, (lvl - px[0]) if (px and lvl is not None) else None))
        prev_state = st

print("=== A. BELL-CARRIED (state on the day's LAST 30m bar; next day opens with it) ===")
print("%-12s %-12s %9s %9s %11s" % ("state on", "opens", "~next O", "~next C", "raw O->C"))
down = 0
tot = 0
for d, nxt, o in bell:
    if o:
        tot += 1
        down += (o[2] < 0)
        print("%-12s %-12s %9.1f %9.1f %+11.1f" % (d, nxt, o[0], o[1], o[2]))
    else:
        print("%-12s %-12s %9s" % (d, nxt, "n/a"))
print("total %d bell days; next day closed DOWN %d/%d" % (len(bell), down, tot))

print("\n=== B. INTRADAY FORMATIONS (run starts; outcome = bar level -> session close) ===")
print("%-12s %-6s %11s" % ("date", "time", "rest-of-day"))
dcount = defaultdict(int)
for d, t, mv in intraday:
    dcount[d] += 1
    print("%-12s %-6s %+11.1f" % (d, t, mv) if mv is not None else "%-12s %-6s %11s" % (d, t, "n/a"))
print("total %d intraday formations on %d dates" % (len(intraday), len(dcount)))
neg = sum(1 for _, _, mv in intraday if mv is not None and mv < 0)
print("rest-of-day negative: %d/%d" % (neg, sum(1 for _, _, mv in intraday if mv is not None)))
