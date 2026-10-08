#!/usr/bin/env python3
"""Ad-hoc: head-to-head Kite vs M.Stock POSITION tick quality.

For each day, computes each broker's position-capture coverage, then
re-analyzes both brokers restricted to the common overlap window so the
comparison is like-for-like (they subscribe their own position sets).
Per broker: ticks, ticks/min, median gap, max gap, >10s stalls within
the overlap only. Falls back to solo stats when there is no overlap.
"""
import csv, glob, os, statistics
from datetime import datetime

BASE = os.path.join(os.path.dirname(__file__), "..", "data", "feed_compare")


def load(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                out.append((int(row["recv_ns"]), row["symbol"]))
            except (ValueError, KeyError):
                pass
    out.sort()
    return out


def stats(ts, label):
    if len(ts) < 2:
        return f"{label}: insufficient ticks ({len(ts)})"
    t0, t1 = ts[0][0], ts[-1][0]
    span = (t1 - t0) / 60e9
    gaps = [(b[0] - a[0]) / 1e9 for a, b in zip(ts, ts[1:])]
    return (
        f"{label}: {len(ts):6d} ticks over {span:7.1f} min | "
        f"{len(ts)/span:7.1f}/min | med_gap={statistics.median(gaps):5.2f}s | "
        f"max_gap={max(gaps):7.1f}s | stalls>10s={sum(1 for g in gaps if g>10)}"
    )


for day in sorted(glob.glob(os.path.join(BASE, "2026-*"))):
    kt = load(os.path.join(day, "kite_position.csv"))
    mt = load(os.path.join(day, "mstock_position.csv"))
    if not kt and not mt:
        continue
    print(f"\n=== {os.path.basename(day)} ===")
    if kt: print(" ", stats(kt, "KITE   full"))
    if mt: print(" ", stats(mt, "MSTOCK full"))
    if kt and mt:
        lo = max(kt[0][0], mt[0][0])
        hi = min(kt[-1][0], mt[-1][0])
        if hi > lo:
            ko = [t for t in kt if lo <= t[0] <= hi]
            mo = [t for t in mt if lo <= t[0] <= hi]
            mins = (hi - lo) / 60e9
            print(f"  overlap {datetime.fromtimestamp(lo/1e9):%H:%M}-{datetime.fromtimestamp(hi/1e9):%H:%M} ({mins:.0f} min):")
            print("   ", stats(ko, "KITE   "))
            print("   ", stats(mo, "MSTOCK "))
            ks = {s for _, s in ko}
            ms = {s for _, s in mo}
            common = ks & ms
            if common:
                kc = [t for t in ko if t[1] in common]
                mc = [t for t in mo if t[1] in common]
                print(f"    same {len(common)} symbol(s) {sorted(common)}:")
                print("     ", stats(kc, "KITE   "))
                print("     ", stats(mc, "MSTOCK "))
        else:
            print("  no overlapping capture window")
