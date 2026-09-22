"""
Find the top-5 highest premium-velocity spikes recorded in the per-tick
P/L logs, plus the top-5 largest absolute moves within <=600ms (the
ultra-scalping exit scale).
"""
import re
import os
import glob
from datetime import datetime

LOG_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "logs")

ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):(\d{3}) \|")
tick_re = re.compile(r"For (\S+) : Buy Price ([0-9.]+) , LTP ([0-9.]+)")

files = glob.glob(os.path.join(LOG_ROOT, "**", "*.log"), recursive=True)
files.sort()

last_tick = {}          # token -> (ts, ltp, file)
top_velocity = []       # (pts_per_sec, ts, token, from, to, dt, file)
top_move_fast = []      # (move, ts, token, from, to, dt, file)


def keep_top(lst, item, key_idx=0, size=5):
    lst.append(item)
    lst.sort(key=lambda x: x[key_idx], reverse=True)
    del lst[size:]


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

                m = tick_re.search(line)
                if not m:
                    continue

                token = m.group(1)
                ltp = float(m.group(3))
                prev = last_tick.get(token)
                last_tick[token] = (ts, ltp, os.path.basename(path))

                if not prev:
                    continue

                dt = (ts - prev[0]).total_seconds()
                if not (0.05 <= dt <= 5.0):
                    continue

                move = abs(ltp - prev[1])
                if move < 0.05:   # ignore zero-flat ticks
                    continue

                velocity = move / dt
                rec = (velocity, ts, token, prev[1], ltp, dt, os.path.basename(path))
                keep_top(top_velocity, rec)

                if dt <= 0.6:
                    rec2 = (move, ts, token, prev[1], ltp, dt, os.path.basename(path))
                    keep_top(top_move_fast, rec2)
    except Exception as e:
        print(f"skipping {os.path.basename(path)}: {e}")

print("=== TOP 5 SPIKE VELOCITY (pts/sec, any window 50ms-5s) ===")
for v, ts, token, frm, to, dt, fname in top_velocity:
    sign = "+" if to > frm else "-"
    print(
        f"{v:>10.0f} pts/sec | {ts} | {token}\n"
        f"            {frm:.2f} -> {to:.2f} ({sign}{abs(to-frm):.2f} pts in {dt*1000:.0f} ms) | {fname}"
    )

print()
print("=== TOP 5 ABSOLUTE MOVES within <=600ms (ultra-scalping exit scale) ===")
for mv, ts, token, frm, to, dt, fname in top_move_fast:
    sign = "+" if to > frm else "-"
    print(
        f"{mv:>8.2f} pts in {dt*1000:.0f} ms ({mv/dt:>8.0f} pts/sec) | {ts} | {token}\n"
        f"            {frm:.2f} -> {to:.2f} | {fname}"
    )
