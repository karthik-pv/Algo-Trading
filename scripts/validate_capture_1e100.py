"""Can the 1E100/200 rise be captured with 1PM / 15PM swing? (user Q, 20 Oct)

Context: 3PM still uptrending (30m PVT > 0 AND 30m MACD2 > signal, last
COMPLETED 30m bar) + 5m MACD2 retracing (last completed 5m bar: above signal
but falling / gap shrinking) + price retraces down and touches 1E100 or 1E200.

Triggers tested (entry = the trigger bar's close, first trigger within 30 min
of the touch):
  T0    none (the touch bar's close — the baseline)
  1PM   the 1m PVT > 0 AND 1m MACD2(24/52) > signal — the state flipping ON
  15SW  the 15m MACD(24/52) line > signal AND rising (the 15m-close proxies;
        the 15m PVT is payload-only, not in the exports — MACD swing only)

Outcomes from the entry: the next-30-min close move, the EOD, the max
favourable/adverse excursion (30 min). Window: the 1m era 2026-07-13..
2026-10-19 (the 1m PVT exists only here).

Run: venv/bin/python scripts/validate_capture_1e100.py
"""
import csv
import os
import statistics
from collections import defaultdict

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = os.path.join(REPO, "data", "insight", "csv")
F1 = os.path.join(CSV, "NSE_NIFTY1!, 1_20adf.csv")
F5 = os.path.join(CSV, "NSE_NIFTY1!, 5_be22a.csv")
F30 = os.path.join(CSV, "NSE_NIFTY1!, 30_42a03.csv")
OUT = os.path.join(CSV, "validate_capture_1e100_events.csv")

START = "2026-07-13"
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


def mins(t):
    return int(t[11:13]) * 60 + int(t[14:16])


r1 = load_named(F1)
r5 = load_named(F5)
r30 = load_named(F30)

# ---- 30m state: PVT sign + M2 up, keyed (date, stamp); completed-bar carry ----
s30 = {}
for r in r30:
    if r.get("time"):
        m2, s2 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
        s30[(r["time"][:10], r["time"][11:16])] = {
            "pvt": (f(r["PVT"]) or 0) > 0,
            "m2_up": (m2 > s2) if (m2 is not None and s2 is not None) else None,
        }
dates30 = sorted({d for d, _ in s30})


def carry30(date, m):
    """the last 30m bar completed by the END of minute m"""
    target = m - 29
    if target < 555:
        i = dates30.index(date)
        if i == 0:
            return None
        return s30.get((dates30[i - 1], "15:15"))
    s = 555 + ((target - 555) // 30) * 30
    return s30.get((date, "%02d:%02d" % (s // 60, s % 60)))


# ---- 5m MACD2 (last completed 5m bar): value + prev, for the retrace test ----
m5 = {}
prev_by_day = {}
prevrow = None
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
    """the last 5m bar completed by the END of minute m"""
    target = m - 4
    if target < 555:
        i = dates30.index(date)
        if i == 0:
            return None
        # the previous day's last 5m bar (15:35)
        return m5.get((dates30[i - 1], "15:35"))
    s = 555 + ((target - 555) // 5) * 5
    return m5.get((date, "%02d:%02d" % (s // 60, s % 60)))


# ---- the 1m series per day + the 15m MACD chain (24/52 over the 15m closes) ----
day1 = defaultdict(list)
for r in r1:
    t = r.get("time")
    if t and START <= t[:10] <= END:
        day1[t[:10]].append(r)
dates1 = sorted(day1.keys())

# the 15m close proxies = the last 1m close of each 15m window (from 09:15)
c15_series = []          # (date, hhmm, close)
for d in dates1:
    rows = sorted(day1[d], key=lambda r: r["time"])
    for k in range(0, len(rows), 15):
        chunk = rows[k:k + 15]
        c15_series.append((d, chunk[-1]["time"][11:16], f(chunk[-1]["close"])))


def ema_chain(vals, period, seed=None):
    a = 2.0 / (period + 1)
    e = seed
    out = []
    for v in vals:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


closes15 = [c for _, _, c in c15_series if c is not None]
l15 = [a - b for a, b in zip(ema_chain(closes15, 24), ema_chain(closes15, 26))]
s15 = ema_chain(l15, 9)
macd15 = {}
idx = 0
for (d, hh, c) in c15_series:
    if c is None:
        continue
    macd15[(d, hh)] = {"line": l15[idx], "sig": s15[idx], "prevline": l15[idx - 1] if idx else None}
    idx += 1


def macd15_up(date, hh):
    v = macd15.get((date, hh))
    if not v or v["prevline"] is None:
        return None
    return v["line"] > v["sig"] and v["line"] > v["prevline"]


# ---- event detection ----
events = []
for date in dates1:
    rows = sorted(day1[date], key=lambda r: r["time"])
    for level_name in ("1E100", "1E200"):
        lvl_key = "1m EMA" + level_name[2:]
        armed = True          # the new event = allowed once the price reclaims
        for j in range(1, len(rows)):
            cur, prev = rows[j], rows[j - 1]
            m = mins(cur["time"])
            c, p = f(cur["close"]), f(prev["close"])
            lo = f(cur["low"])
            lvl = f(cur[lvl_key])
            if None in (c, p, lo, lvl):
                continue
            st30 = carry30(date, m)
            c5 = carry5(date, m)
            if not st30 or not c5 or not st30["pvt"] or not st30["m2_up"]:
                continue
            if not c5["prev"]:
                continue
            m2, s2 = c5["m2"], c5["s2"]
            m2p, s2p = c5["prev"][2], c5["prev"][3]
            m2_retracing = m2 > s2 and (m2 < m2p or (m2 - s2) < (m2p - s2p))
            if not m2_retracing:
                continue
            touch = p > lvl >= lo          # the retrace down INTO the rung
            if not touch:
                if c > lvl:
                    armed = True
                continue
            if not armed:
                continue
            armed = False                  # one event per leg until the reclaim

            # T0: the touch bar's close (the baseline)
            # 1PM: the first 1m bar within 30 min where 1m PVT>0 + 1m M2>signal,
            #      flipping ON (the state was off at the touch bar)
            pvt1 = (f(cur["PVT"]) or 0) > 0
            m12, s12 = f(cur["MACD Line#2"]), f(cur["Signal Line#2"])
            pm1 = pvt1 and m12 is not None and s12 is not None and m12 > s12
            t1pm = t15 = None
            for k in range(0, min(len(rows), j + 31) - j):
                r_ = rows[j + k]
                m_ = mins(r_["time"])
                pv = (f(r_["PVT"]) or 0) > 0
                a2, b2 = f(r_["MACD Line#2"]), f(r_["Signal Line#2"])
                st1 = pv and a2 is not None and b2 is not None and a2 > b2
                if k == 0:
                    was_on = st1
                if st1 and not was_on and t1pm is None:
                    t1pm = (r_, f(r_["close"]))
                if not st1:
                    was_on = False
                # 15SW: the first 15m-close bar with the MACD line > sig + rising
                if t15 is None and (m_ - 555) % 15 == 14:
                    if macd15_up(date, r_["time"][11:16]):
                        t15 = (r_, f(r_["close"]))
            eod = next((f(x["close"]) for x in reversed(rows) if mins(x["time"]) <= 15 * 60 + 29), None)

            def outcomes(entry_i, entry_c):
                if entry_c is None:
                    return None, None, None, None
                fut = rows[entry_i + 1:entry_i + 31]
                c30 = f(fut[29]["close"]) if len(fut) > 29 else None
                highs = [f(x["high"]) for x in fut if f(x["high"]) is not None]
                lows = [f(x["low"]) for x in fut if f(x["low"]) is not None]
                fav = (max(highs) - entry_c) if highs else None
                adv = (entry_c - min(lows)) if lows else None
                return ((c30 - entry_c) if c30 is not None else None,
                        (eod - entry_c) if eod is not None else None, fav, adv)

            recs = {"T0": (j, c)}
            if t1pm:
                recs["1PM"] = (rows.index(t1pm[0]), t1pm[1])
            if t15:
                recs["15SW"] = (rows.index(t15[0]), t15[1])
            for trig, (ii, ec) in recs.items():
                f30, fe, fav, adv = outcomes(ii, ec)
                events.append({
                    "date": date, "touch_time": cur["time"][11:16], "level": level_name,
                    "trigger": trig, "entry_time": rows[ii]["time"][11:16] if trig != "T0" else cur["time"][11:16],
                    "entry_close": ec, "fwd30": f30, "fwd_eod": fe,
                    "max_fav": fav, "max_adv": adv,
                    "delay_min": (mins(rows[ii]["time"]) - m) if trig != "T0" else 0,
                })

# ---- stats ----
def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else float("nan")


print("=" * 76)
print("CAPTURE TEST — 3PM up + 5M2 retracing + the 1E100/200 touch, %s .. %s" % (START, END))
print("=" * 76)
for level in ("1E100", "1E200"):
    print("\n%s (the retrace down to the rung, 3PM up, 5M2 retracing)" % level)
    print("  %-6s %-5s %-16s %-14s %-12s %-12s %-11s" % (
        "entry", "n", "30min up %", "30min med", "max-fav med", "max-adv med", "EOD med"))
    for trig in ("T0", "1PM", "15SW"):
        sub = [e for e in events if e["level"] == level and e["trigger"] == trig
               and e["fwd30"] is not None]
        if not sub:
            continue
        f30 = [e["fwd30"] for e in sub]
        mu = [e["max_fav"] for e in sub]
        md = [e["max_adv"] for e in sub]
        fe = [e["fwd_eod"] for e in sub]
        dl = [e["delay_min"] for e in sub if e["trigger"] != "T0"]
        win = sum(1 for x in f30 if x > 0)
        print("  %-6s %-5d %-16s %-14s %-12s %-12s %-11s" % (
            trig, len(sub), "%.0f%%" % (100.0 * win / len(f30)),
            "%+.1f" % med(f30), "%+.1f" % med(mu), "%+.1f" % med(md), "%+.1f" % med(fe)))

with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["date", "touch_time", "level", "trigger",
                                       "entry_time", "delay_min", "entry_close",
                                       "fwd30", "fwd_eod", "max_fav", "max_adv"])
    w.writeheader()
    for e in sorted(events, key=lambda x: (x["date"], x["touch_time"], x["level"], x["trigger"])):
        w.writerow({k: ("" if e.get(k) is None else e[k]) for k in w.fieldnames})
print("\nevents -> %s (%d rows)" % (OUT, len(events)))
