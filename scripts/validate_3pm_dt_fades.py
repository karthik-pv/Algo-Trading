"""Does the Oct-19 fade playbook generalize to all 3PM-downtrend days?

Context window: 3PM down = the last COMPLETED 30m bar has PVT < 0 AND
MACD2(24/52) < signal (the carried state, per 1m bar).

Leg: price retraces UP into an overhead 1m rung (prior close < level <= high)
— the rung = the highest of 1E9/1E21/1E50/1E100/1E200 touched in the leg.
Fall triggers (the entry = the trigger bar's close, the pair-mates within
+/-10 min):
  T1  the 15s-MACD2 proxy cross-down alone (the MACD(6,13) of the 1m closes,
      sig EMA2 — the span match for the 24/52-of-15s)
  T2  the 1m PVT cross-down + the proxy cross-down, with the 1m MACD2 already
      below its signal
  T3  the 1m PVT cross-down + the 1m MACD2 cross-down (the either order)
Priority T3 > T2 > T1. One event per 30 min per rung (the cooldown).

Split: the phase-1 state (the 5m PVT<0 + the 5m M2<sig = 5PM down) vs the
phase-2 state (the 5m PVT<0 + the 5m M2>sig = the 5M2 retrace-up).
Outcome: the entry -> the min low over the next 30 1m bars (the fall), the
max high (the adverse), the win = the fall >= 10 pts.

Run: venv/bin/python scripts/validate_3pm_dt_fades.py
Out: data/insight/csv/threepm_dt_fade_events.csv
"""
import csv
import os
import statistics
from collections import defaultdict

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = os.path.join(REPO, "data", "insight", "csv") + "/"
F1 = CSV + "NSE_NIFTY1!, 1_20adf.csv"
F5 = CSV + "NSE_NIFTY1!, 5_be22a.csv"
F30 = CSV + "NSE_NIFTY1!, 30_42a03.csv"
OUT = CSV + "threepm_dt_fade_events.csv"

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


def ema_chain(vals, period, seed=None):
    a = 2.0 / (period + 1)
    e = seed
    out = []
    for v in vals:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


r1 = load_named(F1)
r5 = load_named(F5)
r30 = load_named(F30)

# ---- the 15s-MACD2 proxy over the 1m closes ----
closes = [f(r["close"]) for r in r1]
l613 = [a - b for a, b in zip(ema_chain(closes, 6), ema_chain(closes, 13))]
s613 = ema_chain(l613, 2)
prev_l = [None] + l613[:-1]
prev_s = [None] + s613[:-1]
xdn_proxy = []
for i in range(len(r1)):
    if None in (l613[i], s613[i], prev_l[i], prev_s[i]):
        xdn_proxy.append(False)
    else:
        xdn_proxy.append(l613[i] < s613[i] and prev_l[i] >= prev_s[i])
for r, xd in zip(r1, xdn_proxy):
    r["_p15xdn"] = xd

# ---- the 30m state (the completed-bar carry) ----
s30 = {}
for r in r30:
    if r.get("time"):
        m2, s2 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
        s30[(r["time"][:10], r["time"][11:16])] = {
            "pvt": f(r["PVT"]),
            "m2_dn": (m2 < s2) if (m2 is not None and s2 is not None) else None,
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


# ---- the 5m state (the last completed 5m bar) ----
m5 = {}
prevrow = None
prev_by_day = {}
for r in r5:
    t = r.get("time")
    if not t:
        continue
    d, hh = t[:10], t[11:16]
    m2, s2 = f(r["MACD Line#2"]), f(r["Signal Line#2"])
    pvt = f(r["PVT"])
    if m2 is None or s2 is None:
        continue
    prev = prev_by_day.get(d) or (prevrow if prevrow and prevrow[0] != d else None)
    m5[(d, hh)] = {"m2": m2, "s2": s2, "pvt": pvt, "prev": prev}
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

RUNGS = [("1E200", "1m EMA200"), ("1E100", "1m EMA100"),
         ("1E50", "1m EMA50"), ("1E21", "1m EMA21"), ("1E9", "1m EMA9")]

events = []
for date in dates1:
    rows = sorted(day1[date], key=lambda r: r["time"])
    cooldown_until = -1
    for j in range(1, len(rows)):
        cur, prev = rows[j], rows[j - 1]
        m = mins(cur["time"])
        if m < cooldown_until:
            continue
        c, p, hi = f(cur["close"]), f(prev["close"]), f(cur["high"])
        if None in (c, p, hi):
            continue
        st30 = carry30(date, m)
        if not st30 or st30["pvt"] is None or st30["pvt"] >= 0 or not st30["m2_dn"]:
            continue                       # the 3PM-down window only
        c5 = carry5(date, m)
        if not c5 or c5["pvt"] is None or c5["pvt"] >= 0:
            continue                       # the 5m PVT must be red too
        phase = ("5M2-retrace-up" if c5["m2"] > c5["s2"] else "5PM-down")
        # the highest rung touched from below
        rung = None
        for nm, key in RUNGS:
            lv = f(cur[key])
            if lv is not None and p < lv <= hi:
                rung = (nm, lv)
                break
        if rung is None:
            continue
        # the trigger scan: the cross events within +/-10 min (the 20 1m bars)
        lo_i, hi_i = max(0, j - 10), min(len(rows), j + 11)
        pvt_xdn_i = next((i2 for i2 in range(lo_i, hi_i)
                          if (rows[i2].get("Cross Down") or "").strip() != ""
                          or (i2 > 0 and (f(rows[i2 - 1]["PVT"]) or 0) > 0
                              and (f(rows[i2]["PVT"]) or 0) < 0)), None)
        m2_xdn_i = next((i2 for i2 in range(lo_i, hi_i)
                         if f(rows[i2]["MACD Line#2"]) is not None
                         and f(rows[i2]["Signal Line#2"]) is not None
                         and f(rows[i2 - 1]["MACD Line#2"]) is not None
                         and f(rows[i2 - 1]["Signal Line#2"]) is not None
                         and f(rows[i2]["MACD Line#2"]) < f(rows[i2]["Signal Line#2"])
                         and f(rows[i2 - 1]["MACD Line#2"]) >= f(rows[i2 - 1]["Signal Line#2"])), None)
        px_xdn_i = next((i2 for i2 in range(lo_i, hi_i) if rows[i2]["_p15xdn"]), None)
        m1_dn = (f(cur["MACD Line#2"]) or 0) < (f(cur["Signal Line#2"]) or 0)

        trig, ti = None, None
        if pvt_xdn_i is not None and m2_xdn_i is not None and abs(pvt_xdn_i - m2_xdn_i) <= 10:
            trig, ti = "T3-pvt+m2", max(pvt_xdn_i, m2_xdn_i)
        elif pvt_xdn_i is not None and px_xdn_i is not None and abs(pvt_xdn_i - px_xdn_i) <= 10 and m1_dn:
            trig, ti = "T2-pvt+px", max(pvt_xdn_i, px_xdn_i)
        elif px_xdn_i is not None:
            trig, ti = "T1-px-only", px_xdn_i
        else:
            continue                        # no trigger = not a framed fade
        entry_c = f(rows[ti]["close"])
        fut = rows[ti + 1:ti + 31]
        ls = [f(x["low"]) for x in fut if f(x["low"]) is not None]
        hs = [f(x["high"]) for x in fut if f(x["high"]) is not None]
        c30 = f(fut[29]["close"]) if len(fut) > 29 else None
        fall = (entry_c - min(ls)) if ls else None
        adv = (max(hs) - entry_c) if hs else None
        events.append({
            "date": date, "time": cur["time"][11:16], "rung": rung[0],
            "phase": phase, "trigger": trig,
            "entry_close": entry_c,
            "fall30": fall, "adverse30": adv,
            "c30": (c30 - entry_c) if c30 is not None else None,
            "win": (fall >= 10) if fall is not None else None,
        })
        cooldown_until = m + 30

# ---- stats ----
def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else float("nan")


print("=" * 78)
print("3PM-DOWNTREND FADE STUDY — %s .. %s (%d 1m bars)" % (START, END, sum(len(v) for v in day1.values())))
print("=" * 78)

print("\n--- by trigger x phase ---")
print("  %-22s %-16s %-5s %-7s %-9s %-9s %-8s" % (
    "trigger", "phase", "n", "win>=10", "fall med", "adv med", "c30 med"))
for trig in ("T3-pvt+m2", "T2-pvt+px", "T1-px-only"):
    for phase in ("5PM-down", "5M2-retrace-up"):
        sub = [e for e in events if e["trigger"] == trig and e["phase"] == phase]
        if not sub:
            continue
        fl = [e["fall30"] for e in sub]
        win = sum(1 for e in sub if e["win"])
        print("  %-22s %-16s %-5d %-7.0f%% %-9.1f %-9.1f %-8.1f" % (
            trig, phase, len(sub), 100.0 * win / len(sub),
            med(fl), med([e["adverse30"] for e in sub]), med([e["c30"] for e in sub])))

print("\n--- by rung (where the retrace died) ---")
for nm, _ in RUNGS:
    sub = [e for e in events if e["rung"] == nm]
    if not sub:
        continue
    fl = [e["fall30"] for e in sub]
    win = sum(1 for e in sub if e["win"])
    print("  %-6s n=%-4d win=%3.0f%% fall-med=%+6.1f  adv-med=%+6.1f" % (
        nm, len(sub), 100.0 * win / len(sub), med(fl), med([e["adverse30"] for e in sub])))

# ---- the day table ----
by_day = defaultdict(list)
for e in events:
    by_day[e["date"]].append(e)
print("\n--- days with 3PM-DT fade legs (%d of %d days) ---" % (len(by_day), len(dates1)))
print("  %-12s %-5s %-10s %-9s %-11s" % ("date", "legs", "win>=10", "fall med", "best trig"))
for d in sorted(by_day.keys()):
    evs = by_day[d]
    fl = [e["fall30"] for e in evs]
    win = sum(1 for e in evs if e["win"])
    best = min(evs, key=lambda e: (e["fall30"] if e["fall30"] is not None else 0))
    print("  %-12s %-5d %-10.0f%% %-9.1f %-11s" % (
        d, len(evs), 100.0 * win / len(evs), med(fl), best["trigger"]))

with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["date", "time", "rung", "phase", "trigger",
                                       "entry_close", "fall30", "adverse30", "c30", "win"])
    w.writeheader()
    for e in sorted(events, key=lambda x: (x["date"], x["time"])):
        w.writerow({k: ("" if e.get(k) is None else e[k]) for k in w.fieldnames})
print("\nevents -> %s (%d rows)" % (OUT, len(events)))
