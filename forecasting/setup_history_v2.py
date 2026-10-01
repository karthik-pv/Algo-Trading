"""Setup occurrences v2 — Trend (UT/RT_UP/DT/RT_DOWN) + Acronym classes.

Per 30m window, classify the market into the most specific class:

  RT_UP   3S_5PM_CV : 3S uptrending (rising) + 3P (30m PVT) still red
                      + 5PM confirmation (5m PVT up OR 5m M2 up; occasional
                      5m PVT dips forgiven while M2 holds) + close above VWAP
  RT_DOWN 3S_5PM_CV : mirror — 3S downtrending (falling) + 3P still green
                      + 5PM confirmation down + price traded below VWAP in the window
  UT      3PMX      : 3P/3M1/3M2 crossed up, 1-13 candles since the cross,
                      same day only (next day it becomes 3PM if P/M2 still up)
  DT      3PMX      : same, crossed down
  UT      3PM       : 3P (30m PVT) AND 3M2 (MACD_2452) both uptrending
  DT      3PM       : both downtrending

PRECEDENCE (user-locked): 3S_5PM_CV overrides 3PM/3PMX; 3PMX overrides 3PM
even when 3P/3M2 are already trending (cross index 1-13 same day wins).
Priority RT_UP > RT_DOWN > 3PMX > 3PM. Unclassified windows get no row.
Consecutive same-(trend, acronym) windows merge into one row.

Run:  python -m forecasting.setup_history_v2
Out:  data/insight/csv/setup_occurrences_v2.csv (date-desc)
"""

import csv
import os

from forecasting.setup_history import load, hhmm_to_min, min_to_hhmm, group_by_date, sgn, fnum, FILES

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DEFAULT = os.path.join(_REPO_ROOT, "data", "insight", "csv", "setup_occurrences_v2.csv")

NAMES = {
    "3PM": "30m PVT + MACD Trend",
    "3PMX": "30m Cross Credit (1-13, same day)",
    "3S_5PM_CV": "Stoch Retrace + 5PM + VWAP",
}

LINERS = {
    ("UT", "3PM"): "3P and 3M2 both uptrending — confirmed uptrend",
    ("DT", "3PM"): "3P and 3M2 both downtrending — confirmed downtrend",
    ("UT", "3PMX"): "3P/3M1/3M2 crossed up, 1-13 candles since cross (same-day credit; becomes 3PM next day if P/M2 still up)",
    ("DT", "3PMX"): "3P/3M1/3M2 crossed down, 1-13 candles since cross (same-day credit; becomes 3PM next day if P/M2 still down)",
    ("RT_UP", "3S_5PM_CV"): "3S uptrending while 3P still red + 5PM up + close above VWAP — RTU even if 3PM is downtrending",
    ("RT_DOWN", "3S_5PM_CV"): "3S downtrending while 3P still green + 5PM down + close below VWAP — RTD even if 3PM is uptrending",
}


def slot_ranges():
    windows = []
    t0 = 9 * 60 + 15
    while t0 < 15 * 60 + 45:
        windows.append((t0, t0 + 30))
        t0 += 30
    return windows


def main(out_path=OUT_DEFAULT):
    days_1m = group_by_date(load(FILES["1m"]))
    r30 = load(FILES["30m"])
    r5m = load({"5m": "NSE_NIFTY1!, 5_be22a.csv"}["5m"])
    pvt_5m = {(r["date"] + "|" + (r.get("hhmm") or "")): sgn(fnum(r, "PVT"))
              for r in r5m}
    m2_5m = {}
    for r in r5m:
        mac2, sig2 = fnum(r, "MACD_Line_2452"), fnum(r, "Signal_Line_2452")
        if mac2 is not None and sig2 is not None:
            m2_5m[r["date"] + "|" + (r.get("hhmm") or "")] = mac2 > sig2
    d_rows = [r for r in load(FILES["D"]) if r.get("date")]
    d_by_date = {r["date"]: r for r in d_rows}
    d_dates = sorted(d_by_date.keys())

    # 30m state per (date, hhmm)
    m30 = {}
    for r in r30:
        if not r.get("date"):
            continue
        key = (r["date"], (r.get("hhmm") or "")[:5])
        mac1, sig1 = fnum(r, "MACD_Line_1226"), fnum(r, "Signal_Line_1226")
        mac2, sig2 = fnum(r, "MACD_Line_2452"), fnum(r, "Signal_Line_2452")
        m30[key] = {
            "k": fnum(r, "K"), "d": fnum(r, "D"),
            "pvt": sgn(fnum(r, "PVT")),
            "m1_up": (mac1 > sig1) if (mac1 is not None and sig1 is not None) else None,
            "m2_up": (mac2 > sig2) if (mac2 is not None and sig2 is not None) else None,
        }

    def cross_dir(key_prev, key_cur):
        """Return 'UT'/'DT'/None: any of 3P/3M1/3M2 crossing between two bars."""
        p, c = m30.get(key_prev), m30.get(key_cur)
        if not p or not c:
            return None
        # 3P: PVT zero-cross
        if p["pvt"] == -1 and c["pvt"] == 1:
            return "UT"
        if p["pvt"] == 1 and c["pvt"] == -1:
            return "DT"
        # 3M1 / 3M2: signal crosses
        for up, dn in ((p["m1_up"], c["m1_up"]), (p["m2_up"], c["m2_up"])):
            if up is False and dn is True:
                return "UT"
            if up is True and dn is False:
                return "DT"
        return None

    rows = []
    for date in sorted(days_1m.keys()):
        bars = [b for b in days_1m[date] if b.get("hhmm")]
        if len(bars) < 60:
            continue
        d_i = next((i for i, dd in enumerate(d_dates) if dd >= date), None)
        if not d_i or d_i == 0:
            continue
        pd = d_by_date[d_dates[d_i - 1]]
        dpm = sgn(fnum(pd, "PVT"))
        ds = None
        k, dd_ = fnum(pd, "K"), fnum(pd, "D")
        if k is not None and dd_ is not None:
            ds = 1 if k > dd_ else -1

        closes, vwaps = {}, {}
        for b in bars:
            t = hhmm_to_min(b)
            if t is None:
                continue
            if fnum(b, "close") is not None:
                closes[t] = fnum(b, "close")
            if fnum(b, "VWAP") is not None:
                vwaps[t] = fnum(b, "VWAP")

        day_windows = []
        for w0, w1 in slot_ranges():
            cur = m30.get((date, min_to_hhmm(w0)))
            if not cur or cur["k"] is None or cur["d"] is None:
                continue
            prev = m30.get((date, min_to_hhmm(w0 - 30)))

            ts = [t for t in closes if w0 <= t < w1]
            c = closes[max(ts)] if ts else None
            v = vwaps.get(max(ts)) if ts else None
            # window-level CV: price traded above/below VWAP at ANY 1m bar
            cv_above = any(closes[t] > vwaps[t] for t in ts if t in vwaps)
            cv_below = any(closes[t] < vwaps[t] for t in ts if t in vwaps)

            def p5_ok(want_up):
                """5PM confirmation: 5m PVT or 5m M2 in direction; checked at
                window-start and mid slots (PVT dips forgiven while M2 holds)."""
                for off in (0, 15):
                    m5 = 555 + ((w0 + off - 555) // 5) * 5
                    key = "%s|%02d:%02d" % (date, m5 // 60, m5 % 60)
                    if want_up and (pvt_5m.get(key) == 1 or m2_5m.get(key) is True):
                        return True
                    if not want_up and (pvt_5m.get(key) == -1 or m2_5m.get(key) is False):
                        return True
                return False

            pk = (date, min_to_hhmm(w0 - 30))
            if pk not in m30 and d_i > 1:
                pk = (d_dates[d_i - 2] if d_i - 2 >= 0 else d_dates[d_i - 1], "15:15")
            k_prev = m30[pk]["k"] if pk in m30 else None
            k_rising = cur["k"] > k_prev if k_prev is not None else None
            k_falling = cur["k"] < k_prev if k_prev is not None else None
            green, red = cur["k"] > cur["d"], cur["k"] < cur["d"]

            trend = cls = None
            # Precedence: 3S_5PM_CV > 3PMX > 3PM.
            # 1. RT classes — 3S rising (any color; "Red" = 3P still red)
            #    with 5PM/CV confirmations; overrides 3PM/3PMX states.
            if k_rising and cur["pvt"] == -1 and p5_ok(True) and cv_above:
                trend, cls = "RT_UP", "3S_5PM_CV"
            elif k_falling and cur["pvt"] == 1 and p5_ok(False) and cv_below:
                trend, cls = "RT_DOWN", "3S_5PM_CV"
            else:
                # 2. 3PMX — overrides 3PM even when 3P/3M2 are already
                #    trending: if the cross index is 1-13 same day, show 3PMX.
                for n in range(1, 14):
                    kc = (date, min_to_hhmm(w0 - 30 * (n - 1)))
                    kp = (date, min_to_hhmm(w0 - 30 * n))
                    if kp not in m30:
                        break  # crossed yesterday -> credit expired
                    xdir = cross_dir(kp, kc)
                    if xdir:
                        trend, cls = xdir, "3PMX"
                        break
                # 3. 3PM: 3P and 3M2 aligned (only when no cross credit active)
                if not cls:
                    if cur["pvt"] == 1 and cur["m2_up"] is True:
                        trend, cls = "UT", "3PM"
                    elif cur["pvt"] == -1 and cur["m2_up"] is False:
                        trend, cls = "DT", "3PM"
            if cls:
                ctx = "DPM%s" % ("+" if dpm == 1 else ("-" if dpm == -1 else "?"))
                ctx += " 3P%s" % ("+" if cur["pvt"] == 1 else ("-" if cur["pvt"] == -1 else "?"))
                if cur["m2_up"] is not None:
                    ctx += " 3M2%s" % ("+" if cur["m2_up"] else "-")
                if ds:
                    ctx += " DS%s" % ("+" if ds == 1 else "-")
                day_windows.append([w0, w1, trend, cls, ctx])

        merged = []
        for w0, w1, trend, cls, ctx in day_windows:
            if merged and merged[-1][2] == trend and merged[-1][3] == cls \
                    and merged[-1][1] == w0:
                merged[-1][1] = w1
            else:
                merged.append([w0, w1, trend, cls, ctx])
        for w0, w1, trend, cls, ctx in merged:
            rows.append((date, w0, w1, trend, cls, ctx))

    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)

    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Start", "End", "Trend", "Acronym", "Setup Name",
                    "Liner", "Indicator SetUp 1", "Indicator SetUp 2"])
        for date, w0, w1, trend, cls, ctx in rows:
            w.writerow([date, min_to_hhmm(w0), min_to_hhmm(w1), trend, cls,
                        NAMES[cls], LINERS[(trend, cls)],
                        "%s: %s" % (trend, LINERS[(trend, cls)]), ctx])

    counts = {}
    for _, _, _, trend, cls, _ in rows:
        counts[(trend, cls)] = counts.get((trend, cls), 0) + 1
    print("v2 rows: %d -> %s" % (len(rows), out_path))
    for (trend, cls), n in sorted(counts.items()):
        print("  %-8s %-10s %3d" % (trend, cls, n))


if __name__ == "__main__":
    main()
