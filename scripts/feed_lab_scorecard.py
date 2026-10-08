#!/usr/bin/env python3
"""Ad-hoc: compare Kite vs M.Stock feed quality from Feed Lab position CSVs.

Computes, per broker CSV: tick count, coverage span, ticks/min, mean/median
inter-tick gap, max gap, and >10s stall count, per day. Also net.csv API
latency medians for both brokers. Plain csv/math - no pandas needed.
"""
import csv, glob, os, statistics, sys
from datetime import datetime

BASE = os.path.join(os.path.dirname(__file__), "..", "data", "feed_compare")


def analyze(path):
    ts = []
    symbols = set()
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                ts.append((int(row["recv_ns"]), row["symbol"]))
                symbols.add(row["symbol"])
            except (ValueError, KeyError):
                pass
    if not ts:
        return None
    ts.sort()
    t0, t1 = ts[0][0], ts[-1][0]
    span_min = (t1 - t0) / 60e9
    gaps = [(b[0] - a[0]) / 1e9 for a, b in zip(ts, ts[1:])]
    big = [g for g in gaps if g > 10]
    return {
        "rows": len(ts),
        "symbols": len(symbols),
        "start": datetime.fromtimestamp(t0 / 1e9).strftime("%H:%M"),
        "end": datetime.fromtimestamp(t1 / 1e9).strftime("%H:%M"),
        "span_min": round(span_min, 1),
        "tpm": round(len(ts) / span_min, 1) if span_min > 0 else 0,
        "med_gap": round(statistics.median(gaps), 2) if gaps else 0,
        "mean_gap": round(statistics.mean(gaps), 2) if gaps else 0,
        "max_gap": round(max(gaps), 1) if gaps else 0,
        "stalls_gt10s": len(big),
    }


def net_latency(path):
    lat = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            url = row.get("target", "")
            key = "KITE" if "kite" in url else "MSTOCK" if "mstock" in url else None
            if key and row.get("ok") == "1":
                lat.setdefault(key, []).append(float(row["latency_ms"]))
    out = {}
    for k, v in lat.items():
        out[k] = {"probes": len(v), "p50_ms": round(statistics.median(v), 1),
                  "p95_ms": round(sorted(v)[int(0.95 * len(v)) - 1], 1),
                  "fails": sum(1 for _ in v) - len(v)}
    return out


for day in sorted(glob.glob(os.path.join(BASE, "2026-*"))):
    print(f"\n=== {os.path.basename(day)} ===")
    for broker in ("kite", "mstock"):
        files = [f for f in glob.glob(os.path.join(day, f"{broker}_*.csv"))
                 if "instruments" not in f and "master" not in f]
        if not files:
            print(f"  {broker.upper():7s} - no capture")
            continue
        for path in files:
            s = analyze(path)
            name = os.path.basename(path)
            if s:
                print(f"  {broker.upper():7s} {name:22s} ticks={s['rows']:6d} syms={s['symbols']:3d} "
                      f"window={s['start']}-{s['end']} ({s['span_min']:7.1f} min) "
                      f"ticks/min={s['tpm']:8.1f} med_gap={s['med_gap']:6.2f}s "
                      f"max_gap={s['max_gap']:7.1f}s stalls>10s={s['stalls_gt10s']}")
            else:
                print(f"  {broker.upper():7s} {name:22s} - empty")
    netp = os.path.join(day, "net.csv")
    if os.path.exists(netp):
        for k, v in net_latency(netp).items():
            print(f"  NET     {k:7s} probes={v['probes']:6d} p50={v['p50_ms']:7.1f}ms p95={v['p95_ms']:7.1f}ms")
