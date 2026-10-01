"""Falls-from analysis: when 2PM is downtrending (2H PVT<0 AND 2H MACD_2452
below signal) and 3S (30m Stoch) is retracing up red (K<D and rising) —
which levels does price fall from?

For each qualifying 30m window (last 3 months of the 1m data):
  - window high/close from the 1m bars
  - level map: VWAP, Round100 multiples, PDH/PDOpen/PDC/PDL, 5m/30m/2H EMAs,
    D EMAs (prev day)
  - "fell from" = nearest level above close that the window high touched
    (touch = high >= L, reject = close < L; store definitions)
  - follow-through = next 30m close below this window's close

Run:  python -m forecasting.falls_from
Out:  data/insight/csv/falls_from_levels.csv (+ .xlsx), console summary.
"""

import csv
import os

from forecasting.setup_history import load, hhmm_to_min, min_to_hhmm, group_by_date, sgn, fnum, FILES
from forecasting.setup_xlsx import convert as xlsx_convert

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DEFAULT = os.path.join(_REPO_ROOT, "data", "insight", "csv", "falls_from_levels.csv")

SINCE = None  # e.g. "2026-06-25"; None = full 1m span


def main(out_path=OUT_DEFAULT):
    days_1m = group_by_date(load(FILES["1m"]))
    r30 = load(FILES["30m"])
    r2h = load(FILES["2h"])
    d_rows = [r for r in load(FILES["D"]) if r.get("date")]
    d_by_date = {r["date"]: r for r in d_rows}
    d_dates = sorted(d_by_date.keys())

    m30 = {}
    for r in r30:
        if r.get("date"):
            m30[(r["date"], (r.get("hhmm") or "")[:5])] = {
                "k": fnum(r, "K"), "d": fnum(r, "D"),
                "close": fnum(r, "close"), "pvt": sgn(fnum(r, "PVT"))}
    m2h = {}
    for r in r2h:
        if r.get("date"):
            mac2, sig2 = fnum(r, "MACD_Line_2452"), fnum(r, "Signal_Line_2452")
            m2h[(r["date"], (r.get("hhmm") or "")[:5])] = {
                "pvt": sgn(fnum(r, "PVT")),
                "m2_down": (mac2 < sig2) if (mac2 is not None and sig2 is not None) else None}

    def level_map(date, w0, day_hi):
        """Nearest candidate levels above price for this window (from 1m bars)."""
        return None  # placeholder, real build per-window in loop below

    rows = []
    dates_all = sorted(days_1m.keys())
    for date in dates_all:
        if SINCE and date < SINCE:
            continue
        bars = [b for b in days_1m[date] if b.get("hhmm")]
        if len(bars) < 60:
            continue
        d_i = d_dates.index(date) if date in d_dates else None
        if not d_i or d_i == 0:
            continue
        pd = d_by_date[d_dates[d_i - 1]]
        pdh, pdl = fnum(pd, "high"), fnum(pd, "low")
        pdc, pdo = fnum(pd, "close"), fnum(pd, "open")
        d_e100, d_e200 = fnum(pd, "D EMA100"), fnum(pd, "D EMA200")

        # per-1m-bar lookups
        bars_by_min = {}
        for b in bars:
            t = hhmm_to_min(b)
            if t is not None:
                bars_by_min[t] = b

        # day-level: walk the 13 half-hour windows
        # 3S retracing up = 30m Stoch rising (K > K_prev, prev-day fallback)
        #                  AND 30m PVT still below zero (user-locked def)
        prev_date = d_dates[d_i - 1]
        prev_k = m30.get((prev_date, "15:15"), {}).get("k")
        day_rows = []
        for w0 in range(9 * 60 + 15, 15 * 60 + 45, 30):
            w1 = w0 + 30
            key = (date, min_to_hhmm(w0))
            st = m30.get(key)
            if not st or st["k"] is None or st["d"] is None:
                continue
            retracing_up = prev_k is not None and st["k"] > prev_k and st["pvt"] == -1
            prev_k = st["k"]

            h2 = None
            m2h_slot = ((w0 - 555) // 120) * 120 + 555
            for hh in (m2h_slot, m2h_slot - 120, m2h_slot - 240):
                h2 = m2h.get((date, min_to_hhmm(hh)))
                if h2:
                    break
            if not (h2 and h2["pvt"] == -1 and h2["m2_down"] is True):
                continue
            if not retracing_up:
                continue

            # window OHLC from 1m bars
            wb = [bars_by_min[t] for t in sorted(bars_by_min) if w0 <= t < w1]
            if not wb:
                continue
            whi = max(fnum(b, "high") or 0 for b in wb)
            wcl = fnum(wb[-1], "close")
            vwap = next((fnum(b, "VWAP") for b in reversed(wb) if fnum(b, "VWAP")), None)
            last_bar = wb[-1]
            e5 = {k: fnum(last_bar, "5m EMA%d" % k) for k in (50, 100, 200)}
            e30 = {k: fnum(last_bar, "30m EMA%d" % k) for k in (50, 100, 200)}
            e2h = {k: fnum(last_bar, "2H EMA%d" % k) for k in (100, 200)}

            # candidate levels above window close
            cands = [
                ("VWAP", vwap), ("Round100", None),
                ("PDH", pdh), ("PDOpen", pdo), ("PDC", pdc), ("PDL", pdl),
                ("5m E50", e5[50]), ("5m E100", e5[100]), ("5m E200", e5[200]),
                ("30m E50", e30[50]), ("30m E100", e30[100]), ("30m E200", e30[200]),
                ("2H E100", e2h[100]), ("2H E200", e2h[200]),
                ("D E100", d_e100), ("D E200", d_e200),
            ]
            touched = []
            for fam, val in cands:
                if val is None:
                    continue
                if fam == "Round100":
                    continue
                if whi >= val > wcl:
                    touched.append((val, fam))
            r100s = []
            m = (int(wcl) // 100 + 1) * 100
            while m <= whi:
                r100s.append((float(m), "Round100"))
                m += 100
            touched += r100s
            if not touched:
                continue
            touched.sort(reverse=True)
            fell_from, fam = touched[0]  # nearest above close that was touched

            nxt = m30.get((date, min_to_hhmm(w1)))
            ft = (nxt["close"] < wcl) if (nxt and nxt["close"] is not None) else None
            day_rows.append({
                "date": date, "start": min_to_hhmm(w0), "end": min_to_hhmm(w1),
                "win_high": round(whi, 1), "win_close": round(wcl, 1),
                "fell_from": fam, "level": round(fell_from, 1),
                "gap_pts": round(fell_from - wcl, 1),
                "follow_through": ft,
                "k": round(st["k"], 1), "d": round(st["d"], 1),
                "all_touched": "; ".join("%s@%.0f" % (f_, v_) for v_, f_ in touched),
            })
        rows.extend(day_rows)

    rows.sort(key=lambda r: r["date"], reverse=True)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Window", "Fell From", "Level", "Win High", "Win Close",
                    "Gap pts", "Follow-Through", "Stoch K", "Stoch D", "All Touched Levels"])
        for r in rows:
            w.writerow([r["date"], "%s-%s" % (r["start"], r["end"]), r["fell_from"],
                        r["level"], r["win_high"], r["win_close"], r["gap_pts"],
                        "" if r["follow_through"] is None else ("Y" if r["follow_through"] else "N"),
                        r["k"], r["d"], r["all_touched"]])

    # summary
    fams = {}
    for r in rows:
        f_ = fams.setdefault(r["fell_from"], {"n": 0, "ft": 0})
        f_["n"] += 1
        if r["follow_through"]:
            f_["ft"] += 1
    print("2PM-down + 3S-red-rising windows with a touched-and-rejected level: %d" % len(rows))
    print("%-10s %5s %8s" % ("Level", "n", "FT%"))
    for fam, f_ in sorted(fams.items(), key=lambda kv: -kv[1]["n"]):
        print("%-10s %5d %7.0f%%" % (fam, f_["n"], 100.0 * f_["ft"] / f_["n"]))

    try:
        xp, _ = xlsx_convert(out_path)
        print("wrote %s" % xp)
    except Exception as e:
        print("xlsx skipped: %s" % e)
    print("wrote %s" % out_path)


if __name__ == "__main__":
    main()
