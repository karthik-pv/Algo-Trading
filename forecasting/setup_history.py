"""Setup history: mine occurrence dates + intraday time ranges for each
library setup from the insight CSVs (1m file is the timeline; 30m/2H/D
files give regime signs).

Heuristic v0.1 — mirrors forecasting/setups.py thresholds. Results are
indicative for eyeballing on TradingView, not exact event logs.

Run:  python -m forecasting.setup_history [--out forecasting/setup_library.json]
                                              [--csv data/insight/csv/setup_occurrences.csv]
Effects:
  1. "history": [dates] written into each setup entry of setup_library.json
  2. one row per occurrence (date + time range) written to the CSV,
     sorted date-desc then start-desc, for Excel review.
"""

import argparse
import csv
import json
import os

from forecasting.store_builder import read_tv_csv, fnum

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_DIR = os.path.join(_REPO_ROOT, "data", "insight", "csv")
LIB_PATH = os.path.join(_REPO_ROOT, "forecasting", "setup_library.json")
CSV_OUT = os.path.join(_REPO_ROOT, "data", "insight", "csv", "setup_occurrences.csv")

FILES = {
    "1m": "NSE_NIFTY1!, 1_20adf.csv",
    "30m": "NSE_NIFTY1!, 30_42a03.csv",
    "2h": "NSE_NIFTY1!, 120_93fb3.csv",
    "D": "NSE_NIFTY1!, 1D_6b212.csv",
}

OR_MINUTES = 30
SQUEEZE_WIDTH = 0.0012
NEAR_EMA = 0.0025
VWAP_AT = 0.0008
GAP_MIN = 0.002
MAX_DATES = 8
MORNING_END_MIN = 11 * 60  # ORB/PDX/GAP windows end here


def sgn(x):
    if x is None or x == 0:
        return None
    return 1 if x > 0 else -1


def load(path):
    return read_tv_csv(os.path.join(CSV_DIR, path))


def hhmm_to_min(row):
    t = row.get("hhmm") or ""
    try:
        h, m = t.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return None


def min_to_hhmm(m):
    return "%02d:%02d" % (m // 60, m % 60)


def group_by_date(rows):
    out = {}
    for r in rows:
        if r.get("date"):
            out.setdefault(r["date"], []).append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=LIB_PATH)
    ap.add_argument("--csv", default=CSV_OUT)
    args = ap.parse_args()

    print("loading CSVs...")
    days_1m = group_by_date(load(FILES["1m"]))
    pvt_30 = {r["date"] + "|" + (r.get("hhmm") or ""): sgn(fnum(r, "PVT"))
              for r in load(FILES["30m"])}
    pvt_2h = {r["date"] + "|" + (r.get("hhmm") or ""): sgn(fnum(r, "PVT"))
              for r in load(FILES["2h"])}
    d_rows = [r for r in load(FILES["D"]) if r.get("date")]
    d_by_date = {r["date"]: r for r in d_rows}
    d_dates = sorted(d_by_date.keys())

    with open(args.out) as f:
        lib = json.load(f)
    meta = {s["id"]: s for s in lib["setups"]}

    occ = []  # (date, start_min, end_min, sid, detail)

    for date in sorted(days_1m.keys()):
        bars = days_1m[date]
        if len(bars) < 60:
            continue
        d_i = next((i for i, dd in enumerate(d_dates) if dd >= date), None)
        if not d_i:
            continue
        pd = d_by_date[d_dates[d_i - 1]] if d_i > 0 else None
        if pd is None:
            continue
        pdh, pdl = fnum(pd, "high"), fnum(pd, "low")
        pdc, pdo = fnum(pd, "close"), fnum(pd, "open")
        d_pvt = sgn(fnum(pd, "PVT"))
        if pdc is None:
            continue

        def m30_sign_at(hhmm):
            m = int(hhmm[:2]) * 60 + int(hhmm[3:5])
            slot = ((m - 555) // 30) * 30 + 555
            return pvt_30.get("%s|%02d:%02d" % (date, slot // 60, slot % 60))

        def h2_sign_at(hhmm):
            m = int(hhmm[:2]) * 60 + int(hhmm[3:5])
            slot = ((m - 555) // 120) * 120 + 555
            return pvt_2h.get("%s|%02d:%02d" % (date, slot // 60, slot % 60))

        o = fnum(bars[0], "open")
        hi = max(fnum(r, "high") or 0 for r in bars)
        lo = min(fnum(r, "low") or fnum(bars[0], "close") for r in bars)
        cl = fnum(bars[-1], "close")
        gap_pct = (o - pdc) / pdc if o else 0
        rng = hi - lo if hi and lo else 0

        # ---- day gates -------------------------------------------------
        tdy_day = False
        or_hi = or_lo = None
        orb_up = orb_dn = trp = False
        orb_t = trp_t = None
        swept_hi = swept_lo = False
        pdx_sweep = None      # ("PDH"|"PDL", sweep_t)
        fill_t = None

        # pre-pass for event-style setups (ORB/TRP/PDX/GAP-fill)
        for r in bars:
            t = hhmm_to_min(r)
            if t is None:
                continue
            c = fnum(r, "close")
            h_, l_ = fnum(r, "high") or 0, fnum(r, "low") or 1e18
            m2, s2 = fnum(r, "MACD_Line_2452"), fnum(r, "Signal_Line_2452")
            macd2_bear = (m2 < s2) if (m2 is not None and s2 is not None) else None
            if t < 9 * 60 + 45:
                or_hi = max(or_hi or 0, h_)
                or_lo = min(or_lo or 1e18, l_)
                continue
            if t <= MORNING_END_MIN and or_hi and or_lo and c is not None:
                if not orb_up and c > or_hi and macd2_bear is False:
                    orb_up, orb_t = True, t
                if not orb_dn and c < or_lo and macd2_bear is True:
                    orb_dn, orb_t = True, t
                if orb_up and c < or_hi:
                    trp, trp_t = True, t
                if orb_dn and c > or_lo:
                    trp, trp_t = True, t
            if pdh and not swept_hi and h_ >= pdh:
                swept_hi = True
                pdx_sweep = ("PDH", t)
            if pdl and not swept_lo and l_ <= pdl:
                swept_lo = True
                pdx_sweep = ("PDL", t)
            if gap_t_fill(pdc, o, h_, l_) and fill_t is None:
                fill_t = t

        if or_hi and or_lo and (or_hi - or_lo) / cl > 0.0005 and orb_t:
            occ.append((date, orb_t, MORNING_END_MIN, "ORB", "break of OR"))
        if trp and trp_t:
            occ.append((date, trp_t, trp_t + 15, "TRP", "failed break, back inside"))
        if gap_pct and abs(gap_pct) >= GAP_MIN:
            end = fill_t if fill_t else hhmm_to_min(bars[-1])
            occ.append((date, hhmm_to_min(bars[0]), end, "GAP",
                        "gap %+.2f%%, fill at %s" % (gap_pct * 100, "touch" if fill_t else "never")))
        if pdx_sweep:
            side, sw_t = pdx_sweep
            lvl = pdh if side == "PDH" else pdl
            reclaimed = (cl < lvl) if side == "PDH" else (cl > lvl)
            if reclaimed:
                occ.append((date, sw_t, hhmm_to_min(bars[-1]), "PDX",
                            "%s swept %.1f, closed back inside" % (side, lvl)))

        # ---- bar-walking episodes ---------------------------------------
        open_ep = {}  # sid -> start_min
        ses_sqz_break = False
        for r in bars:
            t = hhmm_to_min(r)
            if t is None:
                continue
            c = fnum(r, "close")
            vwap = fnum(r, "VWAP")
            e = {k: fnum(r, "1m EMA%d" % k) for k in (9, 21, 50, 100, 200)}
            e5 = {k: fnum(r, "5m EMA%d" % k) for k in (50, 100, 200)}
            if c is None or vwap is None or any(v is None for v in e.values()):
                continue
            s30, s2h = m30_sign_at(r.get("hhmm") or ""), h2_sign_at(r.get("hhmm") or "")
            pvt1 = sgn(fnum(r, "PVT"))
            m2, s2 = fnum(r, "MACD_Line_2452"), fnum(r, "Signal_Line_2452")
            macd2_bear = (m2 < s2) if (m2 is not None and s2 is not None) else None

            conds = {}

            # TDY (day gate: close in trend quartile)
            if s30 and s2h and d_pvt and s30 == s2h == d_pvt:
                bull = s30 > 0
                vals = [e[k] for k in (9, 21, 50, 100, 200)]
                diffs = [vals[i] - vals[i + 1] for i in range(4)]
                stack = all(dd >= 0 for dd in diffs) if bull else all(dd <= 0 for dd in diffs)
                side = c > vwap if bull else c < vwap
                onside = (cl >= hi - 0.3 * rng) if bull else (cl <= lo + 0.3 * rng)
                if stack and side and onside:
                    tdy_day = True
                    conds["TDY"] = True

            # TCP: 2/3 aligned + price at 5m EMA cluster
            sigs = [s for s in (d_pvt, s2h, s30) if s]
            if len(sigs) >= 2 and len(set(sigs)) == 1:
                bull = sigs[0] > 0
                for v in e5.values():
                    if v is None:
                        continue
                    d_ = (c - v) * (1 if bull else -1)
                    if 0 <= d_ / c <= NEAR_EMA:
                        conds["TCP"] = True
                        break

            # VWR / VWH
            if abs(c - vwap) / c <= VWAP_AT and pvt1 is not None and macd2_bear is not None:
                if c <= vwap and pvt1 < 0 and macd2_bear and (s30 == -1 or d_pvt == -1):
                    conds["VWR"] = True
                if c >= vwap and pvt1 > 0 and not macd2_bear and (s30 == 1 or d_pvt == 1):
                    conds["VWH"] = True

            # SQZ: coil bars; break marks the day
            w = (max(e.values()) - min(e.values())) / c
            if t >= 9 * 60 + 45:
                if w < SQUEEZE_WIDTH:
                    conds["SQZ"] = True

            # RNG episodes (day gate: no gap + mid-close)
            if s2h and s30 and s2h != s30 and rng > 0 and abs(gap_pct) < GAP_MIN:
                mid = (cl - lo) / rng
                if 0.40 <= mid <= 0.60:
                    conds["RNG"] = True

            for sid, on in conds.items():
                if on and sid not in open_ep:
                    open_ep[sid] = t
            for sid in list(open_ep):
                if sid not in conds:
                    occ.append((date, open_ep.pop(sid), t, sid, "conditions active"))

        # TDY quartile gate: only keep episodes if day actually trended
        if not tdy_day:
            occ = [x for x in occ if not (x[0] == date and x[3] == "TDY")]
        # close any still-open episodes at day end
        last_t = hhmm_to_min(bars[-1])
        for sid, t0 in open_ep.items():
            occ.append((date, t0, last_t, sid, "conditions active to close"))

    # ---- merge close episodes of same setup/day --------------------------
    by_key = {}
    for date, t0, t1, sid, det in occ:
        by_key.setdefault((date, sid), []).append((t0, t1, det))
    merged = []
    for (date, sid), eps in by_key.items():
        eps.sort()
        cur0, cur1, det = eps[0]
        for t0, t1, _ in eps[1:]:
            if t0 - cur1 <= 10:
                cur1 = max(cur1, t1)
            else:
                merged.append((date, cur0, cur1, sid, det))
                cur0, cur1 = t0, t1
        merged.append((date, cur0, cur1, sid, det))
    occ = merged

    # ---- outputs ---------------------------------------------------------
    occ.sort(key=lambda x: (x[0], x[1]), reverse=True)

    dates_by_sid = {}
    for date, t0, t1, sid, _ in occ:
        dates_by_sid.setdefault(sid, []).append(date)
    for s in lib["setups"]:
        s["history"] = dates_by_sid.get(s["id"], [])[-MAX_DATES:]
        s["history_note"] = ("heuristic v0.1 occurrences mined from data/insight/csv (1m file span); "
                             "eyeball these dates on TradingView to verify the signature")
    with open(args.out, "w") as f:
        json.dump(lib, f, indent=2)

    with open(args.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Start", "End", "Trend", "Acronym", "Setup Name",
                    "Liner", "Indicator SetUp 1", "Indicator SetUp 2"])
        for date, t0, t1, sid, _ in occ:
            m = meta[sid]
            w.writerow([date, min_to_hhmm(t0), min_to_hhmm(t1), m.get("trend", ""),
                        sid, m.get("name", ""), m.get("liner", ""),
                        m.get("indicator_setup_1", ""), m.get("indicator_setup_2", "")])

    counts = {}
    for _, _, _, sid, _ in occ:
        counts[sid] = counts.get(sid, 0) + 1
    print("occurrences: %d rows" % len(occ))
    for sid in sorted(counts):
        print("  %-4s %3d" % (sid, counts[sid]))
    print("wrote %s" % args.csv)
    print("wrote %s" % args.out)


def gap_t_fill(pdc, o, h_, l_):
    """True if this bar touches PDC (fill)."""
    if pdc is None:
        return False
    return l_ <= pdc <= h_


if __name__ == "__main__":
    main()
