#!/usr/bin/env python3
"""
Ultra-scalping study v2 - CORRECTED column mapping per TV Object Tree.

Chart layout (per Object Tree): per timeframe block "MACD_2452 PVT VOC" =
  MACD Line(12/26), Signal, Hist Plot, MACD2 Line(24/52), Signal2, Plot2,
  PVT, PVT Plot, Cross Up, Cross Down.

Column mapping (verified by step cadence):
  Block A (cols 123-134): 15s TF  -> PVT_15S = col131 (steps every 15s; zero-cross
          at 09:21:15 on 2026-10-30 matches the chart's "9:21 Cross Up" marker)
  Block B (cols 135-145): 1m TF   -> PVT_1m = col142 (steps only at minute bounds)

5s MACD is computed directly from 1s closes resampled to 5s (EMA12-EMA26, sig EMA9).

Setups (long 22900 CE):
  A0: entry at 15s PVT zero-cross
  A1: A0 only if 5s MACD > signal at cross
  A2: entry at 5s MACD align (cross or late) if within 90s after a 15s PVT cross-up  ("synch" entry)
  A3: A1 + PVT thrust (dPVT/15s >= 0.05)
  A4: A2 + PVT thrust
Context filter: PVT_1m > 0 (higher-TF confirms).
"""
from pathlib import Path

import numpy as np
import pandas as pd

FUT = Path.home() / "Downloads" / "NSE_NIFTY1!, 1S_6ea4f.csv"
OPT = Path.home() / "Downloads" / "NSE_NIFTY261103C22900, 1S_f9e89.csv"
HORIZONS = [15, 30, 60, 120, 300, 600]
IST = "Asia/Kolkata"


def load():
    df = pd.read_csv(FUT, header=0, dtype=str)
    f = pd.DataFrame({
        "time": pd.to_datetime(df.iloc[:, 0], utc=True),
        "close": pd.to_numeric(df.iloc[:, 4], errors="coerce"),
        "pvt15": pd.to_numeric(df.iloc[:, 131], errors="coerce"),  # PVT_15S
        "pvt1m": pd.to_numeric(df.iloc[:, 142], errors="coerce"),  # PVT_1m
    }).dropna(subset=["time", "close"]).reset_index(drop=True)

    # 5s MACD computed from 1s closes
    s5 = f.set_index("time")["close"].resample("5s").last().dropna()
    macd = s5.ewm(span=12).mean() - s5.ewm(span=26).mean()
    sig = macd.ewm(span=9).mean()
    m5 = pd.DataFrame({"macd5": macd, "sig5": sig}).reset_index()

    f = pd.merge_asof(f, m5, on="time", direction="backward")
    f["pvt15_prev"] = f["pvt15"].shift(15)   # value 15s ago (one 15s bar)
    f["d_pvt15"] = f["pvt15"] - f["pvt15_prev"]
    f["xup"] = (f["pvt15"] > 0) & (f["pvt15"].shift(1) <= 0)
    f["macd_up"] = f["macd5"] > f["sig5"]
    f["pvt1m_pos"] = f["pvt1m"] > 0

    o = pd.read_csv(OPT, header=0, dtype=str)
    o = pd.DataFrame({
        "time": pd.to_datetime(o.iloc[:, 0], utc=True),
        "opt": pd.to_numeric(o.iloc[:, 4], errors="coerce"),
    }).dropna()
    f = pd.merge_asof(f, o, on="time", direction="backward").dropna(subset=["opt"])

    for s in HORIZONS:
        f[f"opt_r{s}"] = (f["opt"].shift(-s) / f["opt"] - 1) * 100
        f[f"fut_r{s}"] = (f["close"].shift(-s) / f["close"] - 1) * 100
    hh = f["time"].dt.tz_convert(IST).dt.time
    f = f[(hh >= pd.Timestamp("09:15").time()) & (hh <= pd.Timestamp("15:30").time())].reset_index(drop=True)
    return f


def mae(f, idx, minutes=5):
    t0 = f["time"].iloc[idx]
    seg = f.iloc[idx:idx + minutes * 60]
    if seg.empty:
        return np.nan
    r = (seg["opt"].min() / f["opt"].iloc[idx] - 1) * 100
    return r


def report(f, masks):
    rows = []
    for name, mask in masks.items():
        idxs = f.index[mask].tolist()
        if not idxs:
            continue
        row = {"Setup": name, "N": len(idxs)}
        for s in [30, 60, 120, 300, 600]:
            r = f.loc[idxs, f"opt_r{s}"].dropna()
            row[f"{s}s"] = f"{(r > 0).mean()*100:.0f}%/{r.mean():+.1f}%" if len(r) else "—"
        # avg max-adverse (5 min) for context
        maes = [mae(f, i) for i in idxs]
        row["MAE5m"] = f"{np.nanmean(maes):.1f}%"
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    f = load()
    print(f"bars {len(f)}  {f['time'].min()} -> {f['time'].max()} (UTC)")

    # synch entry: first bar where macd turns up within 90s after a pvt15 cross-up
    xup_idx = f.index[f["xup"]].tolist()
    synch = np.zeros(len(f), dtype=bool)
    for i in xup_idx:
        for j in range(i, min(i + 90, len(f))):
            if f["macd_up"].iloc[j] and (j == 0 or not f["macd_up"].iloc[j - 1]):
                synch[j] = True
                break
    f["synch"] = synch

    thrust = f["d_pvt15"] >= 0.05
    masks = {
        "A0 15s PVT xup (entry at cross)": f["xup"],
        "A1 xup + 5sMACD>sig at cross": f["xup"] & f["macd_up"],
        "A2 synch: xup then 5sMACD aligns <=90s": pd.Series(synch, f.index),
        "A3 A1 + thrust>=0.05": f["xup"] & f["macd_up"] & thrust,
        "A4 A2 + thrust>=0.05": pd.Series(synch, f.index) & thrust,
        "A5 A2 + PVT_1m>0": pd.Series(synch, f.index) & f["pvt1m_pos"],
        "A6 A2 + thrust + PVT_1m>0": pd.Series(synch, f.index) & thrust & f["pvt1m_pos"],
    }
    pd.set_option("display.width", 250)
    print("\nwin%/avg% at horizon | MAE5m = avg max adverse excursion in first 5 min")
    print(report(f, masks).to_string(index=False))

    print("\n=== All 15s PVT cross-up signals (entry-at-cross returns) ===")
    cols = ["time", "close", "opt", "pvt15", "d_pvt15", "macd_up", "pvt1m_pos",
            "opt_r30", "opt_r60", "opt_r120", "opt_r300", "opt_r600"]
    show = f[f["xup"]][cols].copy()
    show["time"] = show["time"].dt.tz_convert(IST).dt.strftime("%H:%M:%S")
    show["d_pvt15"] = show["d_pvt15"].round(3)
    for c in ["opt_r30", "opt_r60", "opt_r120", "opt_r300", "opt_r600"]:
        show[c] = show[c].round(1)
    print(show.to_string(index=False))

    f.to_csv(Path(__file__).parent.parent / "data" / "ultra_scalp_signals_v2.csv", index=False)


if __name__ == "__main__":
    main()
