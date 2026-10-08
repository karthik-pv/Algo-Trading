#!/usr/bin/env python3
"""Ad-hoc: per-held-position tick quality, Kite vs M.Stock, head-to-head.

Uses data/audit/audit_*.json holding intervals (buy fill -> sell fill) and
measures each broker's position-capture tick cadence for that exact symbol
within that exact window. Only intervals >=2 min are scored (shorter legs
have too little signal). Compares only when BOTH brokers captured.
"""
import csv, glob, json, os, statistics
from datetime import datetime

BASE = os.path.join(os.path.dirname(__file__), "..", "data")

def load_ticks(path, sym):
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for row in csv.DictReader(f):
            if row.get("symbol") != sym:
                continue
            try:
                out.append(int(row["recv_ns"]))
            except (ValueError, KeyError):
                pass
    out.sort()
    return out

def seg_stats(ns, lo, hi):
    seg = [t for t in ns if lo <= t <= hi]
    if len(seg) < 2:
        return None
    mins = (hi - lo) / 60e9
    gaps = [(b - a) / 1e9 for a, b in zip(seg, seg[1:])]
    return {
        "ticks": len(seg), "per_min": len(seg) / mins,
        "med": statistics.median(gaps), "max": max(gaps),
        "stalls": sum(1 for g in gaps if g > 10),
    }

for apath in sorted(glob.glob(os.path.join(BASE, "audit", "audit_*.json"))):
    recs = json.load(open(apath))
    rows = []
    for r in recs:
        if not (r.get("buy_exec_ts") and r.get("sell_exec_ts") and r.get("sell_success")):
            continue
        lo, hi = r["buy_exec_ts"], r["sell_exec_ts"]
        if hi - lo < 120:
            continue
        sym = r["symbol"]  # kite-style name (e.g. SENSEX26O0872700CE)
        day = r["date"]
        kite = load_ticks(os.path.join(BASE, "feed_compare", day, "kite_position.csv"), sym)
        ms = load_ticks(os.path.join(BASE, "feed_compare", day, "mstock_position.csv"), sym)
        lo_ns, hi_ns = int(lo * 1e9), int(hi * 1e9)
        ks, ms_s = seg_stats(kite, lo_ns, hi_ns), seg_stats(ms, lo_ns, hi_ns)
        rows.append((sym, lo, hi, ks, ms_s))
    if not rows:
        continue
    print(f"\n=== {os.path.basename(apath)} (held legs >=2 min) ===")
    for sym, lo, hi, ks, ms_s in rows:
        t = f"{datetime.fromtimestamp(lo):%H:%M}-{datetime.fromtimestamp(hi):%H:%M}"
        print(f"  {sym}  {t} ({(hi-lo)/60:4.0f} min)")
        for label, s in (("KITE  ", ks), ("MSTOCK", ms_s)):
            if s:
                print(f"    {label}: {s['ticks']:5d} ticks | {s['per_min']:6.1f}/min | "
                      f"med {s['med']:5.2f}s | max {s['max']:6.1f}s | stalls {s['stalls']}")
            else:
                print(f"    {label}: no ticks captured")
