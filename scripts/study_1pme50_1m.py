"""1PME50 insight study on the 1m export (Jul 13 .. Sep 30 mining window).

Definitions (Glossary):
- 1PM up  = 1m PVT > 0 AND 1m MACD2 (24/52) > Signal2. Mirror for down.
- 1PME50 UP trend start = within 1PM up, close crosses above 1m EMA50.
  Mirror for the downtrend (close crosses below 1E50 within 1PM down).
- 5m PVT sign = the containing 5m bar's PVT sign (last-completed semantics).
- 30m poise (rule 8): 3PVT green but retracing while 3M1 down and/or 3M2 poised
  down (diff > 0 compressing >= 3 closes, or crossed down) -> 1PME50 swing DOWN.
  Mirror for UP. The 3PVT sign alone is the weakest tell (reported separately).

Outcomes: forward move in pts from the event close at +15/+30/+60 1m bars and
day-end; P(up) and median. Conditioned on 5m PVT sign and the 30m poise state.
October is the holdout: mined stats use pre-Oct only.

Run:  python -m scripts.study_1pme50_1m
"""

import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecasting.store_builder import load_all, fnum, sgn, session_stamp

DEV_END = "2026-09-30"          # mining window: 2026-07-13 .. 2026-09-30
HOLDOUT_START = "2026-10-01"    # October = the forecast holdout
HORIZONS = (15, 30, 60)


def build_1m(bars_1m):
    """Attach 1m EMA50 and derived states to the 1m bars."""
    out = []
    for b in bars_1m:
        e50 = fnum(b["raw"], "1m EMA50")
        macd, sig = b["macd2"], b["signal2"]
        pvt = b["pvt"]
        pm = None
        if pvt is not None and macd is not None and sig is not None:
            pm = 1 if (pvt > 0 and macd > sig) else (-1 if (pvt < 0 and macd < sig) else 0)
        b["e50"] = e50
        b["pm"] = pm          # 1PM state: +1 up, -1 down, 0 mixed
        b["diff"] = macd - sig if (macd is not None and sig is not None) else None
        out.append(b)
    return out


def detect_events(bars):
    """1PME50 trend-start events on the 1m bars."""
    events = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        if b["e50"] is None or p["e50"] is None or b["close"] is None:
            continue
        if b["date"] != p["date"] or b["pm"] is None:
            continue
        c, pc, e, pe = b["close"], p["close"], b["e50"], p["e50"]
        if b["pm"] == 1 and pc <= pe and c > e:
            events.append((i, +1))
        elif b["pm"] == -1 and pc >= pe and c < e:
            events.append((i, -1))
    return events


def index_helpers(bars_1m, bars_5m, bars_30m):
    # 5m PVT sign per (date, 5m stamp)
    p5 = {}
    for b in bars_5m:
        p5[(b["date"], b["hhmm"])] = sgn(b["pvt"])
    # 30m series per date (ordered), with MACD diffs
    d30 = {}
    for b in bars_30m:
        macd, sig = b["macd2"], b["signal2"]
        m1m, m1s = fnum(b["raw"], "MACD_Line_1226"), fnum(b["raw"], "Signal_Line_1226")
        d = macd - sig if (macd is not None and sig is not None) else None
        d1 = m1m - m1s if (m1m is not None and m1s is not None) else None
        d30.setdefault(b["date"], []).append({
            "hhmm": b["hhmm"], "pvt": sgn(b["pvt"]), "pvt_raw": b["pvt"],
            "d2": d, "d1": d1,
        })
    return p5, d30


def poise30(state_hist, strict=True):
    """Rule-8 30m state from the 30m bars completed so far.

    Returns +1 (poised UP), -1 (poised DOWN) or 0 (neutral/no signal).
    strict=True requires the 3PVT retrace (green but falling / red but rising);
    strict=False needs only the M1/M2 tells against/for the PVT sign.
    """
    if len(state_hist) < 4:
        return 0
    pvt_sign = state_hist[-1]["pvt"]
    if not pvt_sign:
        return 0

    def compressing(diff_key, sign):
        """diff > 0 (for sign>0 framing) and lower on each of the last 3 closes,
        or already crossed (diff <= 0)."""
        diffs = [h[diff_key] for h in state_hist[-4:]]
        if any(d is None for d in diffs):
            return False
        if diffs[-1] * sign <= 0:
            return True  # crossed down (relative to the poise direction)
        decs = sum(1 for a, b in zip(diffs, diffs[1:]) if b * sign < a * sign)
        return decs >= 3

    m2_p = compressing("d2", pvt_sign)
    m1_against = (state_hist[-1]["d1"] is not None
                  and state_hist[-1]["d1"] * pvt_sign < 0)

    if not (m2_p or m1_against):
        return 0
    if strict:
        # 3PVT retracing: last completed PVT moved against its sign vs the prior bar
        prev = state_hist[-2]["pvt_raw"]
        cur = state_hist[-1]["pvt_raw"]
        if prev is None or cur is None:
            return 0
        if not (cur - prev) * pvt_sign < 0:
            return 0
    return pvt_sign if pvt_sign == -1 else pvt_sign  # -1 poise DOWN, +1 poise UP


def day_end_idx(bars, i):
    d = bars[i]["date"]
    j = i
    while j + 1 < len(bars) and bars[j + 1]["date"] == d:
        j += 1
    return j


def fwd_stats(bars, events, p5, d30, dev_only=True):
    rows = []
    for i, direction in events:
        b = bars[i]
        date = b["date"]
        if dev_only and date > DEV_END:
            continue
        st5 = session_stamp(b["hhmm"], 5)
        five = p5.get((date, st5))
        hist = [h for h in d30.get(date, []) if h["hhmm"] < b["hhmm"]]
        poi_strict = poise30(hist, strict=True)
        poi_loose = poise30(hist, strict=False)
        entry = b["close"]
        outs = {}
        for h in HORIZONS:
            j = min(i + h, day_end_idx(bars, i))
            outs[h] = bars[j]["close"] - entry
        je = day_end_idx(bars, i)
        outs["eod"] = bars[je]["close"] - entry
        rows.append({
            "date": date, "hhmm": b["hhmm"], "dir": direction, "n5": five,
            "poi_s": poi_strict, "poi_l": poi_loose, **outs,
        })
    return rows


def summarize(rows, key_filter, label):
    sel = [r for r in rows if key_filter(r)]
    if not sel:
        return None
    out = {"label": label, "n": len(sel)}
    for h in list(HORIZONS) + ["eod"]:
        vals = [r[h] * r["dir"] for r in sel]  # signed with-trend
        up = sum(1 for v in vals if v > 0)
        out[h] = (st.median(vals), round(100.0 * up / len(vals)), )
    return out


def fmt(s):
    if not s:
        return "| %s | 0 |" % s["label"] if s else ""
    parts = []
    for h in list(HORIZONS) + ["eod"]:
        med, p = s[h]
        parts.append("%s: %+.1f / %d%%" % (h, med, p))
    return "| %s | %d | %s |" % (s["label"], s["n"], " · ".join(parts))


def main():
    bars_30m, bars_5m, bars_1m, _, _, meta = load_all(
        os.path.join("data", "insight", "csv"))
    bars_1m = build_1m(bars_1m)
    p5, d30 = index_helpers(bars_1m, bars_5m, bars_30m)

    events = detect_events(bars_1m)
    dev_events = [e for e in events if e and bars_1m[e[0]]["date"] <= DEV_END]
    oct_events = [e for e in events if bars_1m[e[0]]["date"] >= HOLDOUT_START]
    print("1PME50 trend-start events: dev(Jul13-Sep30)=%d  Oct-holdout=%d"
          % (len(dev_events), len(oct_events)))

    rows = fwd_stats(bars_1m, events, p5, d30, dev_only=True)
    rows_oct = fwd_stats(bars_1m, events, p5, d30, dev_only=False)
    rows_oct = [r for r in rows_oct if r["date"] >= HOLDOUT_START]

    lines = []
    ap = lines.append
    ap("# 1PME50 1m study — mining window 2026-07-13 .. 2026-09-30")
    ap("")
    ap("## Unconditional baselines (dev window)")
    ap("")
    dev_bars = [b for b in bars_1m if b["date"] <= DEV_END and b["close"]]
    n_up = sum(1 for b in dev_bars if b["pm"] == 1)
    ap("- 1PM-up share of 1m bars: %.0f%% (n=%d)" % (100.0 * n_up / len(dev_bars), len(dev_bars)))
    dates = sorted({b["date"] for b in dev_bars})
    day_up, day_n, rng = 0, 0, []
    for date in dates:
        db = [b for b in dev_bars if b["date"] == date]
        if len(db) < 300:
            continue
        day_n += 1
        day_up += int(db[-1]["close"] > db[0]["open"])
        rng.append(abs(db[-1]["close"] - db[0]["open"]))
    ap("- day closes above open: %d/%d (%.0f%%)" % (day_up, day_n, 100.0 * day_up / max(1, day_n)))
    ap("- median |close-open| drift: %.0f pts" % st.median(rng))
    ap("")
    ap("Event = 1PM state (1m PVT & MACD2 24/52 aligned) + close crossing 1m EMA50.")
    ap("Numbers = forward move WITH the event direction (median pts / P(ptr up)).")
    ap("eod = to day-end close. October (n=%d events) is holdout — excluded." % len(oct_events))
    ap("")
    ap("| Slice | n | +15 | +30 | +60 | eod |")
    ap("|---|---|---|---|---|---|")
    ap(fmt(summarize(rows, lambda r: True, "ALL 1PME50 starts")))
    ap(fmt(summarize(rows, lambda r: r["dir"] == 1, "UP starts")))
    ap(fmt(summarize(rows, lambda r: r["dir"] == -1, "DOWN starts")))
    ap("")
    ap("## By 5m PVT sign at the event")
    ap("")
    ap("| Slice | n | +15 | +30 | +60 | eod |")
    ap("|---|---|---|---|---|---|")
    ap(fmt(summarize(rows, lambda r: r["n5"] == 1 and r["dir"] == 1, "UP, 5m PVT +")))
    ap(fmt(summarize(rows, lambda r: r["n5"] == -1 and r["dir"] == 1, "UP, 5m PVT −")))
    ap(fmt(summarize(rows, lambda r: r["n5"] == -1 and r["dir"] == -1, "DOWN, 5m PVT −")))
    ap(fmt(summarize(rows, lambda r: r["n5"] == 1 and r["dir"] == -1, "DOWN, 5m PVT +")))
    ap("")
    ap("## By rule-8 30m poise at the event")
    ap("")
    ap("| Slice | n | +15 | +30 | +60 | eod |")
    ap("|---|---|---|---|---|---|")
    ap(fmt(summarize(rows, lambda r: r["poi_s"] == 1, "poise-UP strict")))
    ap(fmt(summarize(rows, lambda r: r["poi_s"] == -1, "poise-DOWN strict")))
    ap(fmt(summarize(rows, lambda r: r["poi_s"] == 0, "poise neutral (strict)")))
    ap(fmt(summarize(rows, lambda r: r["poi_l"] == 1, "poise-UP loose")))
    ap(fmt(summarize(rows, lambda r: r["poi_l"] == -1, "poise-DOWN loose")))
    ap("")
    ap("## Cross: event direction vs 30m poise (strict)")
    ap("")
    ap("| Slice | n | +15 | +30 | +60 | eod |")
    ap("|---|---|---|---|---|---|")
    ap(fmt(summarize(rows, lambda r: r["dir"] == 1 and r["poi_s"] == 1, "UP start, poise UP")))
    ap(fmt(summarize(rows, lambda r: r["dir"] == 1 and r["poi_s"] == -1, "UP start, poise DOWN (fade bait?)")))
    ap(fmt(summarize(rows, lambda r: r["dir"] == -1 and r["poi_s"] == -1, "DOWN start, poise DOWN")))
    ap(fmt(summarize(rows, lambda r: r["dir"] == -1 and r["poi_s"] == 1, "DOWN start, poise UP (fade bait?)")))
    ap("")
    ap("## Time-of-day")
    ap("")
    ap("| Slice | n | +15 | +30 | +60 | eod |")
    ap("|---|---|---|---|---|---|")
    ap(fmt(summarize(rows, lambda r: r["hhmm"] < "10:00", "events before 10:00")))
    ap(fmt(summarize(rows, lambda r: "10:00" <= r["hhmm"] < "12:00", "10:00-12:00")))
    ap(fmt(summarize(rows, lambda r: "12:00" <= r["hhmm"] < "14:00", "12:00-14:00")))
    ap(fmt(summarize(rows, lambda r: r["hhmm"] >= "14:00", "after 14:00")))

    # Rule-8 as a daily swing forecast: at each 30m close from 10:00, state -> rest-of-day
    ap("")
    ap("## Rule-8 as the 1PME50 swing-direction forecast (30m closes 10:00+)")
    ap("")
    hits = {"up": [0, 0], "down": [0, 0]}
    hits_first = {"up": [0, 0], "down": [0, 0]}
    for date, hist in sorted(d30.items()):
        if date > DEV_END:
            continue
        day_bars = [b for b in bars_1m if b["date"] == date]
        if len(day_bars) < 300:
            continue
        eod = day_bars[-1]["close"]
        first_called = False
        for k, h30 in enumerate(hist):
            if h30["hhmm"] < "10:00":
                continue
            poi = poise30(hist[:k + 1], strict=True)
            if poi == 0:
                continue
            # the 1m bar at that stamp
            tb = next((b for b in day_bars if b["hhmm"] == h30["hhmm"]), None)
            if tb is None or tb["close"] is None:
                continue
            up = eod > tb["close"]
            key = "up" if poi == 1 else "down"
            hits[key][0] += int((up == (poi == 1)))
            hits[key][1] += 1
            if not first_called:
                first_called = True
                hits_first[key][0] += int((up == (poi == 1)))
                hits_first[key][1] += 1
    ap("- poise-UP calls: %d/%d hit (%.0f%%) · first-call-of-day: %d/%d (%.0f%%)" % (
        hits["up"][0], hits["up"][1], 100.0 * hits["up"][0] / max(1, hits["up"][1]),
        hits_first["up"][0], hits_first["up"][1], 100.0 * hits_first["up"][0] / max(1, hits_first["up"][1])))
    ap("- poise-DOWN calls: %d/%d hit (%.0f%%) · first-call-of-day: %d/%d (%.0f%%)" % (
        hits["down"][0], hits["down"][1], 100.0 * hits["down"][0] / max(1, hits["down"][1]),
        hits_first["down"][0], hits_first["down"][1], 100.0 * hits_first["down"][0] / max(1, hits_first["down"][1])))

    ap("")
    ap("## HOLDOUT — October 2026 (n=%d events), same slices" % len(oct_events))
    ap("")
    ap("| Slice | n | +15 | +30 | +60 | eod |")
    ap("|---|---|---|---|---|---|")
    ap(fmt(summarize(rows_oct, lambda r: True, "ALL 1PME50 starts")))
    ap(fmt(summarize(rows_oct, lambda r: r["dir"] == 1 and r["n5"] == 1, "UP, 5m PVT +")))
    ap(fmt(summarize(rows_oct, lambda r: r["dir"] == 1 and r["n5"] == -1, "UP, 5m PVT − (fade bait)")))
    ap(fmt(summarize(rows_oct, lambda r: r["dir"] == -1 and r["n5"] == -1, "DOWN, 5m PVT −")))
    ap(fmt(summarize(rows_oct, lambda r: r["dir"] == -1 and r["n5"] == 1, "DOWN, 5m PVT +")))
    ap(fmt(summarize(rows_oct, lambda r: r["poi_s"] == 1, "poise-UP strict")))
    ap(fmt(summarize(rows_oct, lambda r: r["poi_s"] == -1, "poise-DOWN strict")))
    ap(fmt(summarize(rows_oct, lambda r: r["hhmm"] < "10:00", "before 10:00")))

    report = "\n".join(lines)
    out_path = os.path.join("data", "insight", "reports", "1pme50_1m.md")
    with open(out_path, "w") as f:
        f.write(report + "\n")
    print(report)
    print("\nwritten:", out_path)


if __name__ == "__main__":
    main()
