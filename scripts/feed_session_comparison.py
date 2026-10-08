"""
Compare Feed Lab internet health + feed signal strength across the
last few captured market sessions (data/feed_compare/<date>/).

Reuses core/feed_capture.py analytics so the numbers match the app's
own daily report (same market window, same attribution rules).

Standalone read-only: loads CSVs, prints a per-day comparison table.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.feed_capture import (  # noqa: E402
    _INTERNET_TARGETS,
    _BROKER_TARGETS,
    _market_window,
    _filter_market_hours,
    _internet_down_windows,
    _rest_anomalies,
    _broker_stats,
    _net_stats,
    _ns_runs,
    _day_dir,
)

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "feed_compare")


def down_minutes(windows):
    return round(sum((e - s) / 60e9 for s, e in windows), 1)


def analyze_day(day):
    day_dir = os.path.join(DATA, day)
    out = {"day": day, "note": []}

    # ---- session metadata
    sess_path = os.path.join(day_dir, "session.json")
    if os.path.exists(sess_path):
        import json
        with open(sess_path) as f:
            s = json.load(f)
        out["underlying"] = s.get("underlying")
    else:
        out["underlying"] = None

    net_path = os.path.join(day_dir, "net.csv")
    if not os.path.exists(net_path):
        out["note"].append("no net.csv (net monitor not running)")
        out["net"] = None
        out["internet_down_min"] = None
        out["rest"] = None
    else:
        raw = pd.read_csv(net_path)
        window = _market_window(out["underlying"])
        net_df = _filter_market_hours(raw, "ts_ns", window)
        if net_df.empty:
            out["note"].append("net.csv has no rows inside market window")
        out["window_label"] = f"{window[0][0]:02d}:{window[0][1]:02d}-{window[1][0]:02d}:{window[1][1]:02d}"
        out["net"] = net_df
        iw = _internet_down_windows(net_df)
        out["internet_down_min"] = down_minutes(iw)
        out["internet_down_windows"] = len(iw)
        out["rest"] = _rest_anomalies(net_df, iw)

        # full-day (not just market hours) coverage for context
        ist = pd.to_datetime(raw["ts_ns"], unit="ns", utc=True).dt.tz_convert("Asia/Kolkata")
        out["capture_span"] = f"{ist.min():%H:%M}-{ist.max():%H:%M}"

    # ---- feed (signal strength) captures
    feeds = {}
    for broker in ("kite", "mstock"):
        matches = [
            f for f in os.listdir(day_dir)
            if f.startswith(broker + "_") and f.endswith(".csv") and "instruments" not in f and "master" not in f
        ] if os.path.isdir(day_dir) else []
        if not matches:
            feeds[broker.upper()] = None
            continue
        df = pd.read_csv(os.path.join(day_dir, matches[0]))
        window = _market_window(out["underlying"])
        feeds[broker.upper()] = _filter_market_hours(df, "recv_ns", window)
    out["feeds"] = feeds

    # cross-broker runs for stall attribution
    runs = {
        b: (_ns_runs(df["recv_ns"]) if df is not None and not df.empty else [])
        for b, df in feeds.items()
    }
    out["per_broker"] = {}
    internet_windows = (
        _internet_down_windows(out["net"]) if out["net"] is not None and not out["net"].empty else []
    )
    for b, df in feeds.items():
        if df is None or df.empty:
            out["per_broker"][b] = None
            continue
        other = runs["MSTOCK" if b == "KITE" else "KITE"]
        out["per_broker"][b] = _broker_stats(
            df, internet_windows=internet_windows, other_runs=other
        )

    return out


def lat_stats(net_df, targets):
    if net_df is None or net_df.empty:
        return None
    sel = net_df[net_df["target"].isin(targets)]
    if sel.empty:
        return None
    ok = sel["ok"].astype(bool)
    lat = sel.loc[ok, "latency_ms"].astype(float)
    return {
        "samples": int(len(sel)),
        "ok_pct": round(100.0 * float(ok.mean()), 2) if len(sel) else None,
        "avg_ms": round(float(lat.mean()), 1) if len(lat) else None,
        "p95_ms": round(float(lat.quantile(0.95)), 1) if len(lat) else None,
        "max_ms": round(float(lat.max()), 1) if len(lat) else None,
        "fails": int((~ok).sum()),
    }


def main():
    days = sorted(d for d in os.listdir(DATA) if os.path.isdir(os.path.join(DATA, d)))
    results = [analyze_day(d) for d in days]

    for r in results:
        print("=" * 78)
        extra = f" ({'; '.join(r['note'])})" if r["note"] else ""
        print(f"SESSION {r['day']}  underlying={r['underlying'] or '-'}{extra}")
        if r.get("capture_span"):
            print(f"  net-monitor coverage (full day, IST): {r['capture_span']}")

        # Internet health
        inet = lat_stats(r["net"], _INTERNET_TARGETS)
        if inet:
            print(f"  INTERNET probes : {inet['samples']:>6} samples | ok {inet['ok_pct']}% | "
                  f"avg {inet['avg_ms']}ms | p95 {inet['p95_ms']}ms | max {inet['max_ms']}ms | fails {inet['fails']}")
            print(f"  internet DOWN   : {r['internet_down_min']} min across {r['internet_down_windows']} window(s) (market hours)")
        else:
            print("  INTERNET probes : none inside market window")

        # Broker API health
        for bname, url in _BROKER_TARGETS.items():
            bs = lat_stats(r["net"], [url])
            if bs:
                rest_anom = (r["rest"] or {}).get(bname, 0)
                breaches = (r["rest"] or {}).get("latency_breaches", {}).get(bname, 0)
                print(f"  {bname:<6} API probe: {bs['samples']:>6} samples | ok {bs['ok_pct']}% | "
                      f"avg {bs['avg_ms']}ms | p95 {bs['p95_ms']}ms | fails {bs['fails']} | "
                      f"REST anomalies {rest_anom} | red-latency breaches {breaches}")

        # Signal strength (feed quality)
        for bname in ("KITE", "MSTOCK"):
            st = r["per_broker"].get(bname)
            if not st:
                print(f"  {bname:<6} feed    : no capture inside market window")
                continue
            inst = st.get("instruments", {})
            worst_sym = max(inst.items(), key=lambda kv: kv[1]["max_gap_s"]) if inst else ("-", {"max_gap_s": 0})
            print(f"  {bname:<6} feed    : {st['ticks']:>8} ticks | {st['ticks_per_min']:>7.1f}/min | "
                  f"runs {st['runs']} | active {st['span_min']}min | max gap {st['max_gap_s']}s | "
                  f"stalls>5s {st['stalls']} (internet-explained {st['stalls_internet_explained']}) | "
                  f"app-closure {st['app_closure_gaps']}x/{st['app_closure_min']}min")
            if inst:
                for sym, s in inst.items():
                    print(f"           {sym or '-':<28} p50 {s['p50_gap_ms']:>6}ms | p95 {s['p95_gap_ms']:>7}ms | "
                          f"p99 {s['p99_gap_ms']:>7}ms | max {s['max_gap_s']:>6}s | silent {s['silent_seconds_pct']:>5}%")

    print("=" * 78)
    print("NOTE: 'today' = last captured day above. Anything after that has no Feed Lab data.")


if __name__ == "__main__":
    main()
