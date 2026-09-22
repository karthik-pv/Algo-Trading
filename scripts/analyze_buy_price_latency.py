"""
Measure, from historical logs:
  1. D-path cost: BUY placement -> executed buy price (fetch_order_executed_price)
  2. D-path degradations: order not yet traded -> falls back to LTP anyway
  3. BUY HTTP round-trip times
  4. Premium velocity from per-tick P/L logs -> points risked during the D penalty
"""
import re
import os
import sys
import glob
from datetime import datetime

LOG_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "logs")

ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):(\d{3}) \|")
fetch_start_re = re.compile(r"Fetching executed price for order id (\S+)")
fetch_ok_re = re.compile(r"Executed average price for order (\S+): ([0-9.]+)")
not_traded_re = re.compile(r"Order (\S+) not yet traded")
degraded_re = re.compile(r"Executed price unavailable for order (\S+)")
tick_re = re.compile(r"For (\S+) : Buy Price ([0-9.]+) , LTP ([0-9.]+)")
buy_timing_re = re.compile(
    r"M.STOCK BUY HTTP TIMING \| request_to_send=([0-9.]+)s "
    r"response_wait=([0-9.]+)s total=([0-9.]+)s"
)

files = glob.glob(os.path.join(LOG_ROOT, "**", "*.log"), recursive=True)
files.sort()  # chronological by filename (timestamped names)

fetch_starts = {}          # orderid -> ts
fetch_latencies = []       # s
not_traded = 0
degraded = 0
buy_totals = []            # s

# velocity stats
vel_samples = 0
vel_sum = 0.0
vel_max = 0.0
# per 300ms / 600ms windows (the D penalty scale)
pts_at_risk_300 = []
pts_at_risk_600 = []
last_tick = {}             # token -> (ts, ltp)

parsed = 0
for path in files:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = ts_re.match(line)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d %H:%M:%S%f")
                except ValueError:
                    continue
                parsed += 1

                m = fetch_start_re.search(line)
                if m:
                    fetch_starts[m.group(1)] = ts
                    continue

                m = fetch_ok_re.search(line)
                if m:
                    start = fetch_starts.pop(m.group(1), None)
                    if start:
                        fetch_latencies.append((ts - start).total_seconds())
                    continue

                if not_traded_re.search(line):
                    not_traded += 1
                    continue

                if degraded_re.search(line):
                    degraded += 1
                    continue

                m = buy_timing_re.search(line)
                if m:
                    buy_totals.append(float(m.group(3)))
                    continue

                m = tick_re.search(line)
                if m:
                    token, _bp, ltp = m.group(1), m.group(2), float(m.group(3))
                    prev = last_tick.get(token)
                    last_tick[token] = (ts, ltp)
                    if prev:
                        dt = (ts - prev[0]).total_seconds()
                        if 0 < dt <= 5.0:
                            move = abs(ltp - prev[1])
                            v = move / dt
                            vel_samples += 1
                            vel_sum += v
                            if v > vel_max:
                                vel_max = v
                        if 0.15 <= dt <= 0.6:
                            pts_at_risk_300.append(move)
    except Exception as e:
        print(f"skipping {os.path.basename(path)}: {e}")

print(f"log lines parsed: {parsed} from {len(files)} files")
print()

print("=== D-path: BUY -> executed buy price available ===")
if fetch_latencies:
    fetch_latencies.sort()
    n = len(fetch_latencies)
    def pct(p):
        return fetch_latencies[min(n - 1, int(p * n))]
    print(f"samples: {n}")
    print(f"  min    : {fetch_latencies[0]*1000:.0f} ms")
    print(f"  median : {pct(0.5)*1000:.0f} ms")
    print(f"  p75    : {pct(0.75)*1000:.0f} ms")
    print(f"  p90    : {pct(0.90)*1000:.0f} ms")
    print(f"  max    : {fetch_latencies[-1]*1000:.0f} ms")
    print(f"  mean   : {(sum(fetch_latencies)/n)*1000:.0f} ms")
else:
    print("no completed fetch pairs found")
print(f"fetch attempts that found 'not yet traded' (D degrades to LTP): {not_traded}")
print(f"'executed price unavailable' fallbacks (D silently becomes U): {degraded}")
print()

print("=== BUY order HTTP round trip (both U and D pay this) ===")
if buy_totals:
    buy_totals.sort()
    n = len(buy_totals)
    print(f"samples: {n} | median {buy_totals[n//2]*1000:.0f} ms | "
          f"p90 {buy_totals[min(n-1, int(0.9*n))]*1000:.0f} ms | "
          f"max {buy_totals[-1]*1000:.0f} ms")
print()

print("=== Premium velocity (per-tick P/L logs) ===")
if vel_samples:
    mean_v = vel_sum / vel_samples
    print(f"consecutive tick pairs (dt<=5s): {vel_samples}")
    print(f"  mean |move|  : {mean_v:.2f} pts/sec")
    print(f"  max |move|   : {vel_max:.2f} pts/sec")
    if pts_at_risk_300:
        pts_at_risk_300.sort()
        n3 = len(pts_at_risk_300)
        print(f"  |move| over ~200-350ms windows (the D-fetch scale): "
              f"median {pts_at_risk_300[n3//2]:.2f} pts | "
              f"p90 {pts_at_risk_300[min(n3-1, int(0.9*n3))]:.2f} pts | "
              f"max {pts_at_risk_300[-1]:.2f} pts | samples {n3}")
else:
    print("no tick pairs found")
