"""Score logged forecasts against actual bars from the PAInsight CSVs.

Usage:
    python -m forecasting.score data/forecasts/2026-09-25.jsonl [--csv-dir DIR]

For each forecast record in the jsonl log:
  - bias: rest-of-day move (last 1m close vs forecast price) vs bias_label
  - levels: for each emitted level, did price touch it afterwards, and did the
    touch reject (resistance: close back below on the touch bar) / hold
    (support: close back above)? Empirical rates vs average forecast confidence.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

from forecasting.store_builder import read_tv_csv, fnum

DEFAULT_CSV_DIR = os.path.join("data", "insight", "csv")
# family name in forecast -> CSV column
CSV_COL = {
    "VWAP": "VWAP", "PDH": "PDH", "PDC": "PDC", "PDL": "PDL", "PDOpen": "PDOpen",
    "Round100": "Round100",
    "30m E50": "30m EMA50", "30m E100": "30m EMA100", "30m E200": "30m EMA200",
    "2H E50": "2H EMA50", "2H E100": "2H EMA100", "2H E200": "2H EMA200",
    "D E50": "D EMA50", "D E100": "D EMA100",
    "1m E50": "1m EMA50", "1m E100": "1m EMA100", "1m E200": "1m EMA200",
    "5m E50": "5m EMA50", "5m E100": "5m EMA100", "5m E200": "5m EMA200",
}


def load_bars(csv_dir, tf_file, date_str):
    path = os.path.join(csv_dir, tf_file)
    bars = []
    for r in read_tv_csv(path):
        if r["date"] != date_str or not r["hhmm"]:
            continue
        bars.append({
            "hhmm": r["hhmm"],
            "open": fnum(r, "open"), "high": fnum(r, "high"),
            "low": fnum(r, "low"), "close": fnum(r, "close"),
            "pvt": fnum(r, "PVT"),
        })
    bars.sort(key=lambda b: b["hhmm"])
    return bars


def score_record(rec, bars_1m, bars_30m=None):
    fc = rec.get("forecast")
    if not fc:
        return None
    snap_hhmm = rec["ts"][11:16]
    price = fc.get("price")
    out = {"bias_hit": None, "levels": []}

    future_1m = [b for b in bars_1m if b["hhmm"] > snap_hhmm and b["close"] is not None]
    if future_1m and price:
        eod = future_1m[-1]["close"]
        move = (eod / price - 1.0) * 100.0
        out["move_pct"] = round(move, 3)
        label = fc.get("bias_label")
        if label == "bullish":
            out["bias_hit"] = move > 0.05
        elif label == "bearish":
            out["bias_hit"] = move < -0.05
        else:
            out["bias_hit"] = abs(move) <= 0.10

    for side in ("resistance", "support"):
        for lv in fc.get(side) or []:
            col = CSV_COL.get(lv["type"])
            entry = {
                "side": side, "type": lv["type"], "level": lv["level"],
                "confidence": lv["confidence"], "touched": False,
                "resolved": None, "follow_through": None,
            }
            if not future_1m:
                out["levels"].append(entry)
                continue
            # find first 1m bar that touches the level
            touch_bar = None
            for b in future_1m:
                if b["high"] is None or b["low"] is None:
                    continue
                if side == "resistance" and b["high"] >= lv["level"]:
                    touch_bar = b
                    break
                if side == "support" and b["low"] <= lv["level"]:
                    touch_bar = b
                    break
            if touch_bar is None:
                out["levels"].append(entry)
                continue
            entry["touched"] = True
            touch_hhmm = touch_bar["hhmm"]
            idx = next(i for i, b in enumerate(future_1m) if b["hhmm"] == touch_hhmm)
            nxt = future_1m[idx + 1] if idx + 1 < len(future_1m) else None
            if side == "resistance":
                entry["resolved"] = touch_bar["close"] < lv["level"]
                if entry["resolved"] and nxt is not None:
                    entry["follow_through"] = nxt["close"] < touch_bar["close"]
            else:
                entry["resolved"] = touch_bar["close"] > lv["level"]
                if entry["resolved"] and nxt is not None:
                    entry["follow_through"] = nxt["close"] > touch_bar["close"]
            out["levels"].append(entry)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score forecasts vs actual bars")
    ap.add_argument("jsonl", help="path to data/forecasts/YYYY-MM-DD.jsonl")
    ap.add_argument("--csv-dir", default=DEFAULT_CSV_DIR)
    args = ap.parse_args(argv)

    date_str = os.path.basename(args.jsonl).split(".")[0]
    bars_1m = load_bars(args.csv_dir, "NSE_NIFTY1!, 1_20adf.csv", date_str)
    if not bars_1m:
        print("No 1m bars for %s in %s" % (date_str, args.csv_dir), file=sys.stderr)
        sys.exit(1)

    n = 0
    bias_hits = 0
    bias_n = 0
    moves = []
    lvl = {}  # (side, type) -> {n_emitted, conf_sum, n_touched, n_resolved, n_ft}
    for line in open(args.jsonl):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        res = score_record(rec, bars_1m)
        if res is None:
            continue
        n += 1
        if res["bias_hit"] is not None:
            bias_n += 1
            if res["bias_hit"]:
                bias_hits += 1
            moves.append((res.get("move_pct"), rec["forecast"].get("bias_label")))
        for e in res["levels"]:
            c = lvl.setdefault(e["side"] + "|" + e["type"],
                               {"n_emitted": 0, "conf": 0.0, "n_touched": 0,
                                "n_resolved": 0, "n_ft": 0, "n_ft_known": 0})
            c["n_emitted"] += 1
            c["conf"] += e["confidence"]
            if e["touched"]:
                c["n_touched"] += 1
                if e["resolved"] is not None:
                    c["n_resolved"] += 1
                    if e["follow_through"]:
                        c["n_ft"] += 1

    print("Forecasts scored: %d  (date %s)" % (n, date_str))
    if bias_n:
        print("Bias accuracy: %d/%d = %.0f%%" % (bias_hits, bias_n, 100.0 * bias_hits / bias_n))
        avg_move = sum(m for m, _ in moves) / float(len(moves)) if moves else 0
        print("Avg rest-of-day move: %.3f%%" % avg_move)
    print()
    print("%-38s %6s %8s %9s %10s" % ("level", "emitted", "touched", "res/hold", "conf(avg)"))
    for k in sorted(lvl):
        c = lvl[k]
        rh = "%d/%d" % (c["n_resolved"], c["n_touched"]) if c["n_touched"] else "-"
        print("%-38s %6d %8d %9s %9.1f%%" % (k, c["n_emitted"], c["n_touched"], rh,
                                             c["conf"] / max(1, c["n_emitted"])))


if __name__ == "__main__":
    main()
