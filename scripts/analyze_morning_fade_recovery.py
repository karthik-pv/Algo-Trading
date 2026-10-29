"""Morning-fade day-type study (user insight, 28 Oct 2026).

SETUP (both day-types start the same way):
  - Morning price falls
  - A 30m PVT Cross Down and/or 30m MACD Cross Down fires in the morning
    (confirmed on a completed 30m bar, by 11:45)
Then the day resolves one of two ways:
  RECOVERY (V-shape): price hits support, then closes above VWAP AND above
    the full 1E50/100/200 stack (proxied by 5m EMA10/20/40, per the validated
    decay match) and holds >= 2 consecutive 5m closes, before 15:00.
  CONSOLIDATION: never reclaims; price is capped at the EMA stack and
    oscillates down to support (D S1 / day low) into the close.

The second half of the study measures features observable EARLY (60 and 120
min after the trigger) and compares their distributions across the two
outcomes, plus simple binary hit-rates usable as a live checklist.

Window: last 1 year of the 5m export (Sep 2025 - Oct 2026).
Run: venv/bin/python scripts/analyze_morning_fade_recovery.py
Out: data/insight/csv/morning_fade_recovery_events.csv + printed stats
"""
import csv
import os
import statistics
from collections import defaultdict

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = os.path.join(REPO, "data", "insight", "csv")
F5 = os.path.join(CSV, "NSE_NIFTY1!, 5_Sep 2025-Oct 2026.csv")
F30 = os.path.join(CSV, "NSE_NIFTY1!, 30_42a03.csv")
OUT = os.path.join(CSV, "morning_fade_recovery_events.csv")

START = "2025-09-15"
END = "2026-10-27"
MIN_FALL_PTS = 30.0          # morning open -> low drop to qualify as a "fall"
TRIGGER_LATEST_CONFIRM = (11, 45)   # cross must be confirmed by this time
RECLAIM_HOLD_BARS = 2        # consecutive 5m closes above stack+VWAP
RECOVERY_LATEST = (15, 0)    # reclaim must happen by this time


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


def mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def hhmm(m):
    return "%02d:%02d" % (m // 60, m % 60)


r5 = load_named(F5)
r30 = load_named(F30)

# ---- 1m-EMA rung proxies over the 5m closes (validated decay match) -------
closes5_all = [f(r["close"]) for r in r5]


def ema_chain(vals, period, seed=None):
    a = 2.0 / (period + 1)
    e = seed
    out = []
    for v in vals:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


eff = []
for span, a1m in ((50, 2 / 51.0), (100, 2 / 101.0), (200, 2 / 201.0)):
    a5 = 1 - (1 - a1m) ** 5
    eff.append(2.0 / a5 - 1)
px = {}
for key, period in zip(("50", "100", "200"), eff):
    px[key] = ema_chain(closes5_all, period)
for r, a, b, c_ in zip(r5, px["50"], px["100"], px["200"]):
    r["_px50"], r["_px100"], r["_px200"] = a, b, c_

# ---- 30m completed bars: MACD / PVT cross-downs ----------------------------
# first MACD Line/Signal Line pair and first PVT(+Plot) block = the 30m block
bars30 = defaultdict(dict)
for r in r30:
    t = r.get("time") or ""
    if not t:
        continue
    d, s = t[:10], t[11:16]
    prev = bars30[d].get(s, {})
    bars30[d][s] = {
        "macd": f(r["MACD Line"]),
        "sig": f(r["Signal Line"]),
        "pvt": f(r["PVT"]),
        "pvt_plot": f(r["Plot"]),
        "close": f(r["close"]) or prev.get("close"),
    }
dates30 = sorted(bars30)


def cross_down(cur, prev, k1, k2):
    """fresh k1 crossing below k2 on this bar"""
    if not cur or not prev:
        return False
    a0, b0, a1, b1 = cur[k1], cur[k2], prev[k1], prev[k2]
    if None in (a0, b0, a1, b1):
        return False
    return a0 < b0 and a1 >= b1


def completed_30m_key(date, hh):
    """last 30m bar COMPLETED by this 5m bar's close"""
    m = mins(hh)
    close_m = m + 5
    target = close_m - 30
    if target < 555:
        i = dates30.index(date) if date in dates30 else None
        if i is None or i == 0:
            return None
        return (dates30[i - 1], "15:15")
    s = 555 + ((target - 555) // 30) * 30
    return (date, hhmm(s))


# ---- index 5m bars per day --------------------------------------------------
day5 = defaultdict(list)
for r in r5:
    t = r.get("time")
    if t and START <= t[:10] <= END:
        day5[t[:10]].append(r)


def md_confirmed_crosses(day):
    """fresh 30m MACD / PVT cross-downs keyed by CONFIRMATION time (S+30)"""
    out = {}
    stamps = sorted(bars30.get(day, {}))
    for i, s in enumerate(stamps):
        prev = bars30[day][stamps[i - 1]] if i else None
        cur = bars30[day][s]
        confirm_m = mins(s) + 30
        if confirm_m > 15 * 60 + 30:
            continue
        md = cross_down(cur, prev, "macd", "sig")
        pv = cross_down(cur, prev, "pvt", "pvt_plot")
        if md or pv:
            out[hhmm(confirm_m)] = ("MACD" if md else "") + \
                                   ("+PVT" if md and pv else ("PVT" if pv else ""))
    return out


events = []
for day, bars in sorted(day5.items()):
    bars = [b for b in bars if mins(b["time"][11:16]) >= 555]
    if len(bars) < 30:
        continue
    crosses = md_confirmed_crosses(day)
    trig = [(mins(s), k) for s, k in crosses.items()
            if mins(s) <= mins("%02d:%02d" % TRIGGER_LATEST_CONFIRM)]
    if not trig:
        continue
    trig_m, kind = min(trig)
    # morning fall: open -> lowest low up to the trigger close
    trig_bars = [b for b in bars if mins(b["time"][11:16]) + 5 <= trig_m]
    if not trig_bars:
        continue
    day_open = f(bars[0]["open"])
    low_bar = min(trig_bars, key=lambda b: f(b["low"]) or 9e9)
    day_low = f(low_bar["low"])
    if None in (day_open, day_low):
        continue
    fall = day_open - day_low
    if fall < MIN_FALL_PTS:
        continue

    # ---- post-trigger walk -------------------------------------------------
    stack_above = lambda b: max(b["_px50"], b["_px100"], b["_px200"])
    vwap_at_trig = None
    low_after = day_low
    reclaim_m = None
    hold_run = 0
    d_s1 = f(bars[0]["D Standard S1"])
    for b in bars:
        bm = mins(b["time"][11:16])
        if bm + 5 <= trig_m:
            if f(b["VWAP"]) is not None:
                vwap_at_trig = f(b["VWAP"])
            continue
        c, v, st = f(b["close"]), f(b["VWAP"]), stack_above(b)
        lo, hi = f(b["low"]), f(b["high"])
        if None in (c, v, st, lo, hi):
            continue
        low_after = min(low_after, lo)
        if reclaim_m is None:
            hold_run = hold_run + 1 if (c > v and c > st) else 0
            if hold_run >= RECLAIM_HOLD_BARS:
                reclaim_m = bm + 5 - RECLAIM_HOLD_BARS * 5
    eod = f(bars[-1]["close"])
    eod_vwap = f(bars[-1]["VWAP"])
    recovered = reclaim_m is not None and reclaim_m <= mins("%02d:%02d" % RECOVERY_LATEST)

    # ---- early features (observable live) ----------------------------------
    def snapshot(dec_m):
        """features computed only from bars closing by dec_m"""
        post = [b for b in bars if trig_m < mins(b["time"][11:16]) + 5 <= dec_m]
        if not post:
            return None
        lowv = min(f(b["low"]) for b in post)
        prev_low = day_low
        hl = 0
        for b in post:
            lo = f(b["low"])
            if lo is not None and lo > prev_low + 1e-9:
                hl += 1
            prev_low = min(prev_low, lo if lo is not None else prev_low)
        last = post[-1]
        c, v = f(last["close"]), f(last["VWAP"])
        st = stack_above(last)
        bounce = c - lowv
        touches_s1 = sum(1 for b in post if d_s1 and f(b["low"]) is not None
                         and f(b["low"]) <= d_s1 + 5)
        rejects = sum(1 for b in post if f(b["high"]) >= stack_above(b)
                      and (f(b["close"]) or 0) < stack_above(b))
        return {
            "net_from_low": bounce,
            "higher_low_bars": hl,
            "close_vs_vwap": c - v if None not in (c, v) else None,
            "close_vs_stack": c - st,
            "below_stack": c < st,
            "gap_vwap_at_low": (vwap_at_trig - lowv) if vwap_at_trig else None,
            "s1_touches": touches_s1,
            "rejections": rejects,
        }

    snap60 = snapshot(trig_m + 60)
    snap120 = snapshot(trig_m + 120)

    events.append({
        "date": day, "trigger": hhmm(trig_m), "cross": kind,
        "fall_pts": round(fall, 1),
        "recovered": 1 if recovered else 0,
        "reclaim_time": hhmm(reclaim_m) if reclaim_m else "",
        "eod_vs_vwap": round(eod - eod_vwap, 1) if None not in (eod, eod_vwap) else "",
        "eod_vs_open": round(eod - day_open, 1) if eod is not None else "",
        "low_to_s1": round(day_low - d_s1, 1) if d_s1 else "",
        **{("m60_" + k): (round(v, 1) if isinstance(v, float) else v)
           for k, v in (snap60 or {}).items()},
        **{("m120_" + k): (round(v, 1) if isinstance(v, float) else v)
           for k, v in (snap120 or {}).items()},
    })

with open(OUT, "w", newline="") as fo:
    w = csv.DictWriter(fo, fieldnames=list(events[0].keys()))
    w.writeheader()
    w.writerows(events)

# ---- stats -----------------------------------------------------------------
rec = [e for e in events if e["recovered"]]
con = [e for e in events if not e["recovered"]]
print("days with morning 30m cross-down + fall(>=%.0f): %d  |  RECOVERY %d (%.0f%%)  CONSOLIDATION %d (%.0f%%)" % (
    MIN_FALL_PTS, len(events), len(rec), 100 * len(rec) / len(events),
    len(con), 100 * len(con) / len(events)))

print("\n-- trigger cross type -> recovery rate --")
kinds = defaultdict(lambda: [0, 0])
for e in events:
    kinds[e["cross"]][e["recovered"]] += 1
for k, (ncon, nrec) in sorted(kinds.items(), key=lambda x: -sum(x[1])):
    print("  %-8s n=%3d  recovery %.0f%%" % (k, nrec + ncon, 100 * nrec / (nrec + ncon)))

print("\n-- trigger time bucket -> recovery rate --")
buckets = [(555, 600, "09:15-10:00"), (600, 630, "10:00-10:30"),
           (630, 660, "10:30-11:00"), (660, 705, "11:00-11:45")]
for a, b, lbl in buckets:
    grp = [e for e in events if a <= mins(e["trigger"]) < b]
    if grp:
        r = sum(e["recovered"] for e in grp)
        print("  %s  n=%3d  recovery %.0f%%" % (lbl, len(grp), 100 * r / len(grp)))

print("\n-- fall size bucket -> recovery rate --")
for a, b in ((30, 60), (60, 100), (100, 150), (150, 10000)):
    grp = [e for e in events if a <= e["fall_pts"] < b]
    if grp:
        r = sum(e["recovered"] for e in grp)
        print("  fall %4d-%4d  n=%3d  recovery %.0f%%" % (a, min(b, 999), len(grp), 100 * r / len(grp)))


def cont_stats(key):
    def g(e):
        v = e.get(key)
        return None if v in ("", None) else float(v)
    a = [g(e) for e in rec]; a = [x for x in a if x is not None]
    b = [g(e) for e in con]; b = [x for x in b if x is not None]
    if len(a) > 2 and len(b) > 2:
        print("  %-22s RECOVERY med %+8.1f   CONSOL med %+8.1f   (means %+8.1f / %+8.1f)" % (
            key, statistics.median(a), statistics.median(b),
            statistics.mean(a), statistics.mean(b)))


print("\n-- early features: RECOVERY vs CONSOLIDATION medians --")
for k in list(events[0].keys()):
    if k.startswith("m60_") or k.startswith("m120_"):
        cont_stats(k)

print("\n-- binary checklist hit-rates (feature true -> P(recovery) vs base) --")
base = len(rec) / len(events)
print("  base recovery rate: %.0f%%" % (100 * base))
checks = [
    ("m60_net_from_low > 20", lambda e: float(e.get("m60_net_from_low") or -1e9) > 20),
    ("m120_net_from_low > 20", lambda e: float(e.get("m120_net_from_low") or -1e9) > 20),
    ("m120_net_from_low > 40", lambda e: float(e.get("m120_net_from_low") or -1e9) > 40),
    ("m60_close_vs_vwap > 0", lambda e: float(e.get("m60_close_vs_vwap") or -1e9) > 0),
    ("m120_close_vs_vwap > 0", lambda e: float(e.get("m120_close_vs_vwap") or -1e9) > 0),
    ("m120_close_vs_vwap > 20", lambda e: float(e.get("m120_close_vs_vwap") or -1e9) > 20),
    ("m120_higher_low_bars >= 6", lambda e: float(e.get("m120_higher_low_bars") or 0) >= 6),
    ("m60_higher_low_bars >= 4", lambda e: float(e.get("m60_higher_low_bars") or 0) >= 4),
    ("m60_s1_touches == 0", lambda e: float(e.get("m60_s1_touches") or 0) == 0),
    ("m120_s1_touches == 0", lambda e: float(e.get("m120_s1_touches") or 0) == 0),
    ("m120_rejections <= 2", lambda e: float(e.get("m120_rejections") or 0) <= 2),
    ("m120_rejections >= 5", lambda e: float(e.get("m120_rejections") or 0) >= 5),
    ("low_to_s1 within 20pts", lambda e: 0 <= float(e.get("low_to_s1") or 1e9) <= 20),
    ("m120_below_stack", lambda e: e.get("m120_below_stack") in (1, "1", True, "True")),
]
for lbl, fn in checks:
    grp = [e for e in events if fn(e)]
    if grp:
        r = sum(e["recovered"] for e in grp)
        print("  %-26s n=%3d  recovery %.0f%%  (lift %+.0f%%)" % (
            lbl, len(grp), 100 * r / len(grp), 100 * (r / len(grp) - base)))

print("\n-- EOD follow-through --")
for grp, lbl in ((rec, "RECOVERY"), (con, "CONSOLIDATION")):
    v = [float(e["eod_vs_vwap"]) for e in grp if e["eod_vs_vwap"] != ""]
    o = [float(e["eod_vs_open"]) for e in grp if e["eod_vs_open"] != ""]
    if v:
        print("  %-14s n=%3d  EOD vs VWAP med %+7.1f  |  EOD vs open med %+7.1f" % (
            lbl, len(v), statistics.median(v), statistics.median(o)))

print("\nevents -> %s" % OUT)
