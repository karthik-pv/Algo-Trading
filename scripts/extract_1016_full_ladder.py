"""The FULL level ladder for the framework commentary:
  1m EMAs (9/21/50/100/200) · 5m EMAs (50/100/200 carried) · 30m EMAs (50/100/200
  carried) · 2H EMAs (50/100/200 carried) · D/W/M EMAs (static per period, from
  the 1D file) · VWAP · the 50-pt round ladder · the daily floor pivots P/R1-R4/
  S1-S4 · the Camarilla · the Fib 38.2/50/61.8 · PDH/PDL/PDC/PDOpen — all HA-based
  (the glossary formulas).

Touch semantics (per 1m bar, per level):
  ST = the support taken (the prior close above, the low dips, the close holds above)
  SB = the support broken
  RF = the resistance faced (the prior close below, the high pops, the close holds below)
  RB = the resistance broken (the reclaim)
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


r1 = load_named(CSV + "NSE_NIFTY1!, 1_20adf.csv")
rd_ = load_named(CSV + "NSE_NIFTY1!, 1D_6b212.csv")

rows1 = sorted([r for r in r1 if r.get("time") and r["time"][:10] == DAY], key=lambda r: r["time"])
drows = [r for r in rd_ if r.get("time") and r["time"]]
di = next(i for i, r in enumerate(drows) if r["time"][:10] == DAY)
pd_row = drows[di - 1]                      # the previous day (the HA values)

# ---- the static EMA levels (the previous day / week / month finals) ----
import datetime
def week_key(s):
    d = datetime.date.fromisoformat(s)
    return d.isocalendar()[0], d.isocalendar()[1]

wk = week_key(DAY)
mi = DAY[:7]
prev_day_row = pd_row
prev_week_row = next((r for r in reversed(drows[:di]) if week_key(r["time"][:10]) != wk), None)
prev_month_row = next((r for r in reversed(drows[:di]) if r["time"][:7] != mi), None)

STATIC = {}
for nm_label, col_prefix, r in (("DE", "D", prev_day_row), ("WE", "W", prev_week_row), ("ME", "M", prev_month_row)):
    if not r:
        continue
    for p in (50, 100, 200):
        v = f(r.get("%s EMA%d" % (col_prefix, p)))
        if v is not None:
            STATIC["%s%d" % (nm_label, p)] = v

# ---- the pivots (the previous day's HA-OHLC) ----
pdO, pdH = f(pd_row["open"]), f(pd_row["high"])
pdL, pdC = f(pd_row["low"]), f(pd_row["close"])
P = (pdH + pdL + pdC) / 3.0
rng = pdH - pdL
PIV = {
    "D-P": P, "D-R1": 2 * P - pdL, "D-S1": 2 * P - pdH,
    "D-R2": P + rng, "D-S2": P - rng,
    "D-R3": 2 * P + pdH - 2 * pdL, "D-S3": 2 * P - (2 * pdH - pdL),
    "D-R4": 3 * P + pdH - 3 * pdL, "D-S4": 3 * P - (3 * pdH - pdL),
    "PDH": pdH, "PDL": pdL, "PDC": pdC, "PDOpen": pdO,
    "Fib38.2": pdL + rng * 0.382, "Fib50": pdL + rng * 0.5, "Fib61.8": pdL + rng * 0.618,
}
for k, mult in ((1, 12), (2, 6), (3, 4), (4, 2)):
    PIV["Cam-R%d" % k] = pdC + rng * 1.1 / mult
    PIV["Cam-S%d" % k] = pdC - rng * 1.1 / mult

print("STATIC EMAs:", {k: round(v, 1) for k, v in sorted(STATIC.items())})
print("PIVOTS:", {k: round(v, 1) for k, v in sorted(PIV.items())})

# ---- the per-bar event loop ----
RUNGS = [("1E9", "1m EMA9"), ("1E21", "1m EMA21"), ("1E50", "1m EMA50"),
         ("1E100", "1m EMA100"), ("1E200", "1m EMA200"),
         ("5E50", "5m EMA50"), ("5E100", "5m EMA100"), ("5E200", "5m EMA200"),
         ("3E50", "30m EMA50"), ("3E100", "30m EMA100"), ("3E200", "30m EMA200"),
         ("2E50", "2H EMA50"), ("2E100", "2H EMA100"), ("2E200", "2H EMA200")]

counts = defaultdict(int)
families = defaultdict(list)
prev_c = None
for r in rows1:
    c = f(r["close"])
    if c is None:
        continue
    hi, lo = f(r["high"]), f(r["low"])
    tags = []
    levels = {}
    for nm, key in RUNGS:
        if key not in r:
            continue
        v = f(r[key])
        if v is not None:
            levels[nm] = v
    levels["VWAP"] = f(r["VWAP"])
    levels.update(STATIC)
    levels.update(PIV)
    rn_dn = int(c // 50) * 50
    levels["RN50"] = rn_dn
    for nm, lv in levels.items():
        if lv is None or prev_c is None or hi is None or lo is None:
            continue
        if prev_c > lv and lo <= lv and c > lv:
            tags.append("ST-%s" % nm)
        elif prev_c > lv and c < lv:
            tags.append("SB-%s" % nm)
        elif prev_c < lv and hi >= lv and c < lv:
            tags.append("RF-%s" % nm)
        elif prev_c < lv and c > lv:
            tags.append("RB-%s" % nm)
    if tags:
        print("  %s C=%-8s %s" % (r["time"][11:16], "%.1f" % c, " ".join(tags)))
        for t in tags:
            counts[t.split("-", 1)[0]] += 1
            families[t.split("-", 1)[1]].append(r["time"][11:16])
    prev_c = c

print("\n--- event counts by kind ---")
for k in sorted(counts):
    print("  %-4s %d" % (k, counts[k]))
print("\n--- touch counts by level family ---")
fam_counts = defaultdict(int)
for lv, times in families.items():
    if lv.startswith("1E"):
        fam_counts["1m rungs"] += len(times)
    elif lv.startswith("5E"):
        fam_counts["5m rungs"] += len(times)
    elif lv.startswith("3E"):
        fam_counts["30m rungs"] += len(times)
    elif lv.startswith("2E"):
        fam_counts["2H rungs"] += len(times)
    elif lv.startswith("DE"):
        fam_counts["D EMAs"] += len(times)
    elif lv.startswith(("WE", "ME")):
        fam_counts["W/M EMAs"] += len(times)
    elif lv.startswith("Cam"):
        fam_counts["Camarilla"] += len(times)
    elif lv.startswith(("D-R", "D-S", "D-P")):
        fam_counts["floor pivots"] += len(times)
    elif lv.startswith(("Fib", "PD")):
        fam_counts["PDH/L/C + Fib"] += len(times)
    elif lv == "VWAP":
        fam_counts["VWAP"] += len(times)
    elif lv == "RN50":
        fam_counts["round-50s"] += len(times)
for k in sorted(fam_counts):
    print("  %-14s %d" % (k, fam_counts[k]))
