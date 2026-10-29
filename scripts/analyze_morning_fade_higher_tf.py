"""Morning-fade recovery vs higher-TF context (user question, 28 Oct 2026).

For each of the 38 morning-fade trigger days (see
analyze_morning_fade_recovery.py / morning_fade_recovery_events.csv) attach:

  1. 2PM  state at the trigger: 2H  PVT sign + 2H  MACD vs Signal
  2. DPM  state at the trigger: Daily PVT sign + Daily MACD vs Signal
  3. Major support under the day low, classified by what the low sat on
     (within 30 pts): Q/M/W pivot (P or S1) > Daily EMA100/200 > 2H EMA100/200
  4. Gap from the low to the nearest Q/M/W pivot level

Blocks in the 30m export verified by within-day constancy:
  cols 26-34 = 30m PM, 37-45 = 2H PM, 48-56 = Daily PM.
Run: venv/bin/python scripts/analyze_morning_fade_higher_tf.py
"""
import csv
import os
from collections import defaultdict

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = os.path.join(REPO, "data", "insight", "csv")
F5 = os.path.join(CSV, "NSE_NIFTY1!, 5_Sep 2025-Oct 2026.csv")
F30 = os.path.join(CSV, "NSE_NIFTY1!, 30_42a03.csv")
EV = os.path.join(CSV, "morning_fade_recovery_events.csv")


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
events = list(csv.DictReader(open(EV)))

# ---- 30m-file rows by (date, stamp) ----------------------------------------
idx30 = {}
for r in r30:
    t = r.get("time") or ""
    if t:
        idx30[(t[:10], t[11:16])] = r
dates30 = sorted({d for d, _ in idx30})


def completed_30m_key(date, hh):
    m = mins(hh)
    target = m + 5 - 30
    if target < 555:
        i = dates30.index(date)
        return None if i == 0 else (dates30[i - 1], "15:15")
    s = 555 + ((target - 555) // 30) * 30
    return (date, hhmm(s))


def pm_state(r, macd, sig, pvt):
    p, m, s = f(r[pvt]), f(r[macd]), f(r[sig])
    if None in (p, m, s):
        return None
    up = (p > 0) + (m > s)
    dn = (p < 0) + (m < s)
    if up == 2:
        return "UT"
    if dn == 2:
        return "DT"
    return "MIX"


# ---- 5m-file rows per day ----------------------------------------------------
day5 = defaultdict(list)
for r in r5:
    t = r.get("time")
    if t:
        day5[t[:10]].append(r)


def support_context(day, low):
    """what major level was the day's low sitting on (within 30 pts)"""
    bars = day5[day]
    row = None
    for b in bars:                       # first bar carries the daily levels
        if mins(b["time"][11:16]) <= 940:
            row = b
    if row is None:
        row = bars[0]
    cands = []
    for lbl, key in (("Q-P", "Q Standard P"), ("Q-S1", "Q Standard S1"),
                     ("M-P", "M Standard P"), ("M-S1", "M Standard S1"),
                     ("W-P", "W Standard P"), ("W-S1", "W Standard S1")):
        v = f(row[key])
        if v is not None:
            cands.append((abs(low - v), "pivot:" + lbl, v))
    for lbl, key in (("D-EMA100", "D EMA100"), ("D-EMA200", "D EMA200")):
        v = f(row[key])
        if v is not None:
            cands.append((abs(low - v), "ema:" + lbl, v))
    for lbl, key in (("2H-EMA100", "2H EMA100"), ("2H-EMA200", "2H EMA200")):
        v = f(row[key])
        if v is not None:
            cands.append((abs(low - v), "ema:" + lbl, v))
    if not cands:
        return None, None
    cands.sort()
    d0, lbl0, v0 = cands[0]
    if d0 <= 30:
        return lbl0, d0
    # nothing within 30: nearest pivot below (or above) and its gap
    below = sorted(((low - v, "pivot:" + l, v) for d, l, v in cands if low - v > 30))
    if below:
        return "none(+%.0f to %s)" % (below[0][0], below[0][1]), below[0][0]
    return "none(below all)", None


out = []
for e in events:
    day, trig = e["date"], e["trigger"]
    k30 = completed_30m_key(day, trig)
    row = idx30.get(k30)
    s2 = pm_state(row, "MACD Line#3", "Signal Line#3", "PVT#2") if row else None
    sd = pm_state(row, "MACD Line#5", "Signal Line#5", "PVT#3") if row else None
    lows = [f(b["low"]) for b in day5[day] if f(b["low"]) is not None]
    dlow = min(lows)
    sup, sup_gap = support_context(day, dlow)
    out.append({
        "date": day, "recovered": int(e["recovered"]),
        "2PM": s2 or "?", "DPM": sd or "?",
        "support": sup or "?", "sup_gap": round(sup_gap, 1) if sup_gap is not None else "",
        "day_low": round(dlow, 1),
    })

o = os.path.join(CSV, "morning_fade_hightf_context.csv")
with open(o, "w", newline="") as fo:
    w = csv.DictWriter(fo, fieldnames=list(out[0].keys()))
    w.writeheader()
    w.writerows(out)


def tab(key):
    groups = defaultdict(lambda: [0, 0])
    for r in out:
        groups[r[key]][r["recovered"]] += 1
    print("\n-- recovery by %s --" % key)
    for k, (ncon, nrec) in sorted(groups.items(), key=lambda x: -sum(x[1])):
        print("  %-22s n=%2d  recovery %.0f%%" % (k, nrec + ncon, 100 * nrec / (nrec + ncon)))


tab("2PM")
tab("DPM")
tab("support")

print("\n-- 2PM x DPM combined --")
groups = defaultdict(lambda: [0, 0])
for r in out:
    groups["%s / %s" % (r["2PM"], r["DPM"])][r["recovered"]] += 1
for k, (ncon, nrec) in sorted(groups.items(), key=lambda x: -sum(x[1])):
    print("  2PM %-4s DPM %-4s n=%2d  recovery %.0f%%" % (
        k.split(" / ")[0], k.split(" / ")[1], nrec + ncon, 100 * nrec / (nrec + ncon)))

print("\n-- every day: 2PM / DPM / support --")
for r in sorted(out, key=lambda x: (x["recovered"], x["date"])):
    print("  %s  %-9s 2PM %-4s DPM %-4s  support %-22s gap %s" % (
        r["date"], "RECOVERY" if r["recovered"] else "NO-RECOV", r["2PM"], r["DPM"],
        r["support"], r["sup_gap"]))
print("\ncontext -> %s" % o)
