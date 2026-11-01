#!/usr/bin/env python3
"""
Option elasticity study: When Nifty/Sensex (futures proxy) moves +/-0.1%,
how much does the ATM weekly option (CE/PE) move, bucketed by
weekday-distance to expiry.

  NIFTY  (Tuesday expiry):   Expiry(Tue), -1(Mon), -4(Fri), -5(Thu), -6(Wed)
  SENSEX (Thursday expiry):  Expiry(Thu), -1(Wed), -2(Tue), -3(Mon), -4(Fri)

Data source: data/feed_compare/<date>/{kite,mstock}_chain_<IDX>.csv
             data/feed_compare/<date>/session_<IDX>.json
"""
import json
import re
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "feed_compare"
THRESH = 0.001  # 0.1%

WEEKDAY = dict(Mon=0, Tue=1, Wed=2, Thu=3, Fri=4)
EXPIRY_WD = {"NIFTY": WEEKDAY["Tue"], "SENSEX": WEEKDAY["Thu"]}

# bucket label by weekday-distance from expiry weekday
LABELS = {
    "NIFTY": {0: "Expiry", 1: "Expiry-1", 4: "Expiry-4", 5: "Expiry-5", 6: "Expiry-6"},
    "SENSEX": {0: "Expiry", 1: "Expiry-1", 2: "Expiry-2", 3: "Expiry-3", 6: "Expiry-6"},
}

OPT_RE = {
    "NIFTY": re.compile(r"^NIFTY\d{2}[A-Z]{1,3}\d*(\d{5})(CE|PE)$"),
    "SENSEX": re.compile(r"^SENSEX\d{2}[A-Z]{1,3}\d*(\d{5})(CE|PE)$"),
}


def load_day(day_dir: Path, idx: str):
    frames = []
    sj = day_dir / f"session_{idx}.json"
    for broker in ("kite", "mstock"):
        csv = day_dir / f"{broker}_chain_{idx}.csv"
        if csv.exists():
            frames.append(pd.read_csv(csv, usecols=["recv_iso", "symbol", "ltp"], dtype={"symbol": str}))
    if not frames or not sj.exists():
        return None
    try:
        meta = json.loads(sj.read_text())
    except Exception:
        return None
    atm = int(meta["atm_strike"])

    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["recv_iso"], format="ISO8601")
    df = df[(df["ts"].dt.time >= datetime.strptime("09:15", "%H:%M").time())
            & (df["ts"].dt.time <= datetime.strptime("15:15", "%H:%M").time())]
    df = df.dropna(subset=["ltp"])
    df = df[df["ltp"] > 0]

    fut = df[df["symbol"].str.contains("FUT")].sort_values("ts")
    if fut.empty:
        return None
    exp_wd = date.fromisoformat(meta["expiry"]).weekday()
    wd = fut["ts"].iloc[0].weekday()
    dist = (exp_wd - wd) % 7  # backward calendar-day distance to expiry weekday
    label = LABELS[idx].get(dist)
    if label is None:
        return None  # holiday-shifted expiry week (non-standard weekday)

    rec = OPT_RE[idx]
    ce_df = df[df["symbol"].str.match(rec.pattern.replace(r"(\d{5})", str(atm)).replace("(CE|PE)", "CE"))].sort_values("ts")
    pe_df = df[df["symbol"].str.match(rec.pattern.replace(r"(\d{5})", str(atm)).replace("(CE|PE)", "PE"))].sort_values("ts")

    f = fut[["ts", "ltp"]].rename(columns={"ltp": "fut"}).reset_index(drop=True)
    f["ce"] = _ffill_join(f["ts"], ce_df)
    f["pe"] = _ffill_join(f["ts"], pe_df)
    f = f.dropna(subset=["ce", "pe"])
    if f.empty:
        return None
    return f, label


def _ffill_join(ts, opt_df):
    if opt_df.empty:
        return pd.Series(np.nan, index=ts.index)
    merged = pd.merge_asof(pd.DataFrame({"ts": ts}), opt_df[["ts", "ltp"]], on="ts", direction="backward")
    return merged["ltp"].to_numpy()


def events(f: pd.DataFrame):
    out = []
    fut = f["fut"].to_numpy()
    ce = f["ce"].to_numpy()
    pe = f["pe"].to_numpy()
    a_f, a_c, a_p = fut[0], ce[0], pe[0]
    for i in range(1, len(fut)):
        r = fut[i] / a_f - 1
        if r >= THRESH or r <= -THRESH:
            direction = "UP" if r >= THRESH else "DOWN"
            if a_c > 5 and a_p > 5:
                out.append((direction, (ce[i] / a_c - 1) * 100, (pe[i] / a_p - 1) * 100))
            a_f, a_c, a_p = fut[i], ce[i], pe[i]
    return out


def collect(idx: str) -> pd.DataFrame:
    rows = []
    for day_dir in sorted(DATA.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            res = load_day(day_dir, idx)
        except Exception as e:
            print(f"  skip {idx} {day_dir.name}: {e}")
            continue
        if res is None:
            continue
        f, label = res
        for direction, ce_m, pe_m in events(f):
            rows.append(dict(day=day_dir.name, dte=label, direction=direction, ce=ce_m, pe=pe_m))
        print(f"  {idx} {day_dir.name}: {label}, {len(events(f))} events")
    return pd.DataFrame(rows)


def main():
    print("Collecting NIFTY ...")
    nifty = collect("NIFTY")
    print("Collecting SENSEX ...")
    sensex = collect("SENSEX")

    nifty.to_csv(ROOT / "data" / "option_elasticity_events_nifty.csv", index=False)
    sensex.to_csv(ROOT / "data" / "option_elasticity_events_sensex.csv", index=False)

    pd.set_option("display.width", 200)

    def fmt(df):
        g = df.groupby(["dte", "direction"]).agg(
            ce_mean=("ce", "mean"), ce_med=("ce", "median"),
            pe_mean=("pe", "mean"), pe_med=("pe", "median"), n=("ce", "size"),
        ).reset_index()
        return g

    gn, gs = fmt(nifty), fmt(sensex)
    order = ["Expiry", "Expiry-1", "Expiry-2", "Expiry-3", "Expiry-4", "Expiry-5", "Expiry-6"]
    order = [d for d in order if d in set(nifty.dte) | set(sensex.dte)]

    for direction, title in [("UP", "RISE  (index +0.1%)"), ("DOWN", "FALL  (index -0.1%)")]:
        print(f"\n=== {title} ===")
        rows = []
        for dte in order:
            a = gn[(gn.dte == dte) & (gn.direction == direction)]
            b = gs[(gs.dte == dte) & (gs.direction == direction)]
            if a.empty and b.empty:
                continue
            rows.append({
                "DTE": dte,
                "NIFTY CE": f"{a.ce_mean.iloc[0]:+.1f}% / {a.ce_med.iloc[0]:+.1f}%" if len(a) else "—",
                "NIFTY PE": f"{a.pe_mean.iloc[0]:+.1f}% / {a.pe_med.iloc[0]:+.1f}%" if len(a) else "—",
                "NIFTY n": int(a.n.iloc[0]) if len(a) else 0,
                "SENSEX CE": f"{b.ce_mean.iloc[0]:+.1f}% / {b.ce_med.iloc[0]:+.1f}%" if len(b) else "—",
                "SENSEX PE": f"{b.pe_mean.iloc[0]:+.1f}% / {b.pe_med.iloc[0]:+.1f}%" if len(b) else "—",
                "SENSEX n": int(b.n.iloc[0]) if len(b) else 0,
            })
        print(pd.DataFrame(rows).to_string(index=False))

    print("\nFormat: mean% / median% of option price move")


if __name__ == "__main__":
    main()
