"""Scalp test: price rises from 1E100/200 with a 1m PVT cross-up + 15s-MACD
swing trigger (user Q, 20 Oct).

Context: 3PM up (30m PVT>0 + 30m M2>signal, completed-bar carry) + 5m MACD2
retracing (last completed 5m bar above signal but falling / gap shrinking).
Event: the 1m bar retraces down into 1E100/1E200 (prior close above, low
through the level), one event per pullback leg (re-arm on the reclaim).

Triggers (entry = the trigger bar's close, scanned up to 30 min):
  T0    the touch bar's close (the baseline)
  XPVT  the 1m PVT Cross Up marker (the zero-cross, the file's own marker)
  XP+15 the same cross-up AND the 15s-MACD proxy swinging up — the proxy =
        MACD(3,6) of the 1m closes with the signal EMA2 (the span match for
        the 15s MACD 12/26/9: ~6.5 min of memory; the REAL 15s data exists
        only for Oct 14-16 — the case studies at the bottom use it)

Outcomes from the entry: the next-30-min close move, the EOD, the max
favourable/adverse excursion over the next 30 1m bars.

Run: venv/bin/python scripts/validate_scalp_1pvt_15s.py
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
F15S = os.path.join(CSV, "NSE_NIFTY1!, 15S_3c57c.csv")
OUT = os.path.join(CSV, "validate_scalp_1pvt_15s_events.csv")

START, END = "2026-07-13", "2026-10-19"


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

# ---- the 15s-MACD proxy: MACD(3,6) of the 1m closes, signal EMA2 ----
closes_all = [f(r["close"]) for r in r1]


def ema_chain(vals, period, seed=None):
    a = 2.0 / (period + 1)
    e = seed
    out = []
    for v in vals:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


l36 = [a - b for a, b in zip(ema_chain(closes_all, 3), ema_chain(closes_all, 6))]
s36 = ema_chain(l36, 2)
for i, r in enumerate(r1):
    r["_l36"], r["_s36"], r["_l36p"] = l36[i], s36[i], (l36[i - 1] if i else None)

# ---- the 30m state ----
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
    target = m - 29
    if target < 555:
        i = dates30.index(date)
        if i == 0:
            return None
        return s30.get((dates30[i - 1], "15:15"))
    s = 555 + ((target - 555) // 30) * 30
    return s30.get((date, "%02d:%02d" % (s // 60, s % 60)))


# ---- the 5m MACD2 (the last completed 5m bar) ----
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


# ---- the events ----
day1 = defaultdict(list)
for r in r1:
    t = r.get("time")
    if t and START <= t[:10] <= END:
        day1[t[:10]].append(r)
dates1 = sorted(day1.keys())

events = []
for date in dates1:
    rows = sorted(day1[date], key=lambda r: r["time"])
    for level_name in ("1E100", "1E200"):
        lvl_key = "1m EMA" + level_name[2:]
        armed = True
        for j in range(1, len(rows)):
            cur, prev = rows[j], rows[j - 1]
            m = mins(cur["time"])
            c, p, lo = f(cur["close"]), f(prev["close"]), f(cur["low"])
            lvl = f(cur[lvl_key])
            if None in (c, p, lo, lvl):
                continue
            st30 = carry30(date, m)
            c5 = carry5(date, m)
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
            eod = next((f(x["close"]) for x in reversed(rows)
                        if mins(x["time"]) <= 15 * 60 + 29), None)

            def outcomes(ii, ec):
                if ec is None:
                    return None, None, None, None
                fut = rows[ii + 1:ii + 31]
                c30 = f(fut[29]["close"]) if len(fut) > 29 else None
                hs = [f(x["high"]) for x in fut if f(x["high"]) is not None]
                ls = [f(x["low"]) for x in fut if f(x["low"]) is not None]
                return ((c30 - ec) if c30 is not None else None,
                        (eod - ec) if eod is not None else None,
                        (max(hs) - ec) if hs else None,
                        (ec - min(ls)) if ls else None)

            # the trigger scan
            xpvt = xp15 = xswing = None
            pvt_up_seen = False
            for k in range(0, min(31, len(rows) - j)):
                r_ = rows[j + k]
                xup = (r_.get("Cross Up") or "").strip() != ""
                pv = (f(r_["PVT"]) or 0) > 0
                swing = (r_["_l36"] is not None and r_["_l36p"] is not None
                         and r_["_l36"] > r_["_l36p"])
                above_sig = r_["_l36"] is not None and r_["_s36"] is not None and r_["_l36"] > r_["_s36"]
                if xup:
                    pvt_up_seen = True
                    if xpvt is None:
                        xpvt = (j + k, f(r_["close"]))
                if pvt_up_seen and pv and swing and xp15 is None:
                    xp15 = (j + k, f(r_["close"]))
                # T-SWING: the 15s-MACD proxy swings up on its own (the line back
                # above the signal AND rising) — the EARLY entry, no PVT wait
                if xswing is None and swing and above_sig:
                    xswing = (j + k, f(r_["close"]))
            recs = {"T0": (j, c)}
            if xpvt:
                recs["XPVT"] = xpvt
            if xp15:
                recs["XP+15"] = xp15
            if xswing:
                recs["SWING"] = xswing
            for trig, (ii, ec) in recs.items():
                f30, fe, fav, adv = outcomes(ii, ec)
                events.append({
                    "date": date, "touch_time": cur["time"][11:16], "level": level_name,
                    "trigger": trig, "entry_time": rows[ii]["time"][11:16],
                    "delay_min": mins(rows[ii]["time"]) - m,
                    "entry_close": ec, "fwd30": f30, "fwd_eod": fe,
                    "max_fav": fav, "max_adv": adv,
                })

# ---- stats ----
def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else float("nan")


print("=" * 78)
print("SCALP TEST — 3PM up + 5M2 retrace + the 1E100/200 touch, %s..%s" % (START, END))
print("  XPVT = the 1m PVT cross-up   SWING = the 15s-proxy swing alone (early)")
print("  XP+15 = the PVT cross + the swing")
print("=" * 78)
for level in ("1E100", "1E200"):
    print("\n%s" % level)
    print("  %-7s %-5s %-13s %-12s %-12s %-12s %-11s %-7s" % (
        "entry", "n", "30min up %", "30min med", "max-fav med", "max-adv med", "EOD med", "delay"))
    for trig in ("T0", "SWING", "XPVT", "XP+15"):
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
        print("  %-7s %-5d %-13s %-12s %-12s %-12s %-11s %-7s" % (
            trig, len(sub), "%.0f%%" % (100.0 * win / len(f30)),
            "%+.1f" % med(f30), "%+.1f" % med(mu), "%+.1f" % med(md), "%+.1f" % med(fe),
            "~%dmin" % med(dl) if dl else "-"))

with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["date", "touch_time", "level", "trigger",
                                       "entry_time", "delay_min", "entry_close",
                                       "fwd30", "fwd_eod", "max_fav", "max_adv"])
    w.writeheader()
    for e in sorted(events, key=lambda x: (x["date"], x["touch_time"], x["level"], x["trigger"])):
        w.writerow({k: ("" if e.get(k) is None else e[k]) for k in w.fieldnames})
print("\nevents -> %s (%d rows)" % (OUT, len(events)))
