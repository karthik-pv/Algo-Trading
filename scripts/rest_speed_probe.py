"""
REST API speed probe for the Feed Lab broker endpoints (KITE + MSTOCK),
plus the neutral internet control targets for context.

Methodology matches core/feed_capture.py's net monitor:
  - ONE keep-alive requests.Session (pooled per host) so a sample
    measures network path RTT + server time, not DNS/TCP/TLS handshake
  - ok = HTTP status < 500 (same as the app), timeout 2s
  - thresholds: broker amber >150ms / red >500ms; internet amber >80ms / red >200ms

Usage:
  ./venv/bin/python scripts/rest_speed_probe.py capture --tag baseline
  ./venv/bin/python scripts/rest_speed_probe.py capture --tag retest
  ./venv/bin/python scripts/rest_speed_probe.py compare --a <fileA.json> --b <fileB.json>

Captures are stored in data/rest_speed_probes/<ts>_<tag>.json so a later
retest can be diffed against this baseline.
"""

import argparse
import json
import os
import statistics
import time
from datetime import datetime

import requests

PROBE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "rest_speed_probes")

TARGETS = {
    "CTRL-G": "https://www.google.com/generate_204",
    "CTRL-CF": "https://1.1.1.1",
    "KITE": "https://api.kite.trade",
    "MSTOCK": "https://api.mstock.trade",
}
# Keep-alive probes: later samples per target reflect the warm network
# path; sample #1 additionally shows the cold handshake cost.
SAMPLES = 20
INTERVAL_S = 1.0
TIMEOUT_S = 2.0

THRESHOLDS = {
    "internet": {"amber": 80.0, "red": 200.0},
    "broker": {"amber": 150.0, "red": 500.0},
}


def group_of(name):
    return "broker" if name in ("KITE", "MSTOCK") else "internet"


def level_of(group, ms):
    t = THRESHOLDS[group]
    if ms is None:
        return "-"
    if ms > t["red"]:
        return "RED"
    if ms > t["amber"]:
        return "AMBER"
    return "GREEN"


def summarize(samples, group):
    oks = [s["latency_ms"] for s in samples if s["ok"]]
    fails = [s for s in samples if not s["ok"]]
    lat = sorted(oks)
    p = lambda q: round(lat[min(len(lat) - 1, int(q * (len(lat) - 1)))], 1) if lat else None  # noqa: E731
    avg = round(statistics.fmean(oks), 1) if oks else None
    return {
        "samples": len(samples),
        "ok": len(oks),
        "fails": len(fails),
        "ok_pct": round(100.0 * len(oks) / len(samples), 1),
        "min_ms": round(min(oks), 1) if oks else None,
        "avg_ms": avg,
        "p50_ms": p(0.50),
        "p95_ms": p(0.95),
        "max_ms": round(max(oks), 1) if oks else None,
        "first_sample_ms": samples[0]["latency_ms"] if samples[0]["ok"] else None,
        "level_now": level_of(group, avg),
    }


def capture(tag, samples=SAMPLES, interval=INTERVAL_S):
    os.makedirs(PROBE_DIR, exist_ok=True)
    session = requests.Session()
    raw = {name: [] for name in TARGETS}
    for i in range(samples):
        for name, url in TARGETS.items():
            started = time.time()
            ok, status = False, None
            try:
                resp = session.get(url, timeout=TIMEOUT_S)
                ok = resp.status_code < 500
                status = resp.status_code
            except requests.RequestException:
                pass
            raw[name].append({
                "seq": i + 1,
                "ts_iso": datetime.now().isoformat(),
                "ok": ok,
                "status": status,
                "latency_ms": round((time.time() - started) * 1000, 1),
            })
        if i < samples - 1:
            time.sleep(interval)

    summary = {name: summarize(s, group_of(name)) for name, s in raw.items()}
    for name, st in summary.items():
        st["level_now"] = level_of(group_of(name), st["avg_ms"])

    result = {
        "tag": tag,
        "captured_at": datetime.now().isoformat(),
        "method": {
            "keep_alive_session": True,
            "samples_per_target": samples,
            "interval_s": interval,
            "timeout_s": TIMEOUT_S,
            "targets": dict(TARGETS),
        },
        "summary": summary,
        "samples": raw,
    }
    path = os.path.join(PROBE_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{tag}.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    return path, result


def print_summary(result):
    print(f"  captured_at: {result['captured_at']}  (tag: {result['tag']})")
    for name, st in result["summary"].items():
        print(
            f"  {name:<8} ok {st['ok']}/{st['samples']} ({st['ok_pct']}%) | "
            f"first {st['first_sample_ms']}> min {st['min_ms']} | avg {st['avg_ms']} | "
            f"p50 {st['p50_ms']} | p95 {st['p95_ms']} | max {st['max_ms']} ms -> {st['level_now']}"
        )


def load(path):
    with open(path) as f:
        return json.load(f)


def compare(path_a, path_b):
    a, b = load(path_a), load(path_b)
    print(f"  A: {a['tag']} @ {a['captured_at']}")
    print(f"  B: {b['tag']} @ {b['captured_at']}")
    print(f"  {'target':<10} {'avg A->B':>16} {'p50 A->B':>16} {'p95 A->B':>16} {'max A->B':>16}  ok A->B")
    for name in TARGETS:
        sa, sb = a["summary"][name], b["summary"][name]

        def fmt(sa, sb, key):
            va, vb = sa[key], sb[key]
            if va is None or vb is None:
                return f"{va} -> {vb}"
            delta = vb - va
            pct = (delta / va * 100) if va else float("inf")
            return f"{va:>6} -> {vb:<6} ({pct:+.0f}%)"

        print(
            f"  {name:<10} {fmt(sa, sb, 'avg_ms'):>16} {fmt(sa, sb, 'p50_ms'):>16} "
            f"{fmt(sa, sb, 'p95_ms'):>16} {fmt(sa, sb, 'max_ms'):>16}  "
            f"{sa['ok']}/{sa['samples']} -> {sb['ok']}/{sb['samples']}"
        )


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--tag", default="run")
    c.add_argument("--samples", type=int, default=SAMPLES)
    c.add_argument("--interval", type=float, default=INTERVAL_S)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--a", required=True)
    cmp_.add_argument("--b", required=True)
    args = ap.parse_args()

    if args.cmd == "capture":
        path, result = capture(args.tag, samples=args.samples, interval=args.interval)
        print(f"REST speed probe saved -> {path}")
        print_summary(result)
    else:
        compare(args.a, args.b)


if __name__ == "__main__":
    main()
