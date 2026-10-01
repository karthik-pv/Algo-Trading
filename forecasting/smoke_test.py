"""Smoke test: hand-built snapshot from the latest CSV values -> forecast.

Run:  python -m forecasting.smoke_test
Builds a synthetic webhook payload from the most recent bars in the CSV
exports, feeds it through ForecastEngine, and checks the forecast shape.
"""

import json

from forecasting.store_builder import read_tv_csv, fnum
from forecasting.engine import ForecastEngine, DEFAULT_CSV

CSV_MAP = {
    "1M": "NSE_NIFTY1!, 1_20adf.csv",
    "5M": "NSE_NIFTY1!, 5_be22a.csv",
    "30M": "NSE_NIFTY1!, 30_42a03.csv",
    "2H": "NSE_NIFTY1!, 120_93fb3.csv",
}


def build_snapshot(csv_dir=DEFAULT_CSV):
    ind = {}
    for tf, fname in CSV_MAP.items():
        rows = read_tv_csv("%s/%s" % (csv_dir, fname))
        last = rows[-1]
        ind[tf] = {
            "PVT": fnum(last, "PVT"),
            "PVTTrendFlag": 1 if (fnum(last, "PVT") or 0) > 0 else -1,
            "PVTPoiseFlag": None,
            "Trend_2452": None,
            "MACD_2452": fnum(last, "MACD_Line_2452"),
            "Signal_2452": fnum(last, "Signal_Line_2452"),
            "K": fnum(last, "K"),
            "D": fnum(last, "D"),
            "EMA9": fnum(last, "%s EMA9" % tf.lower().replace("30m", "30m").replace("2h", "2H")),
            "EMA50": fnum(last, "%s EMA50" % _col_tf(tf)),
            "EMA100": fnum(last, "%s EMA100" % _col_tf(tf)),
            "EMA200": fnum(last, "%s EMA200" % _col_tf(tf)),
            "VWAP": fnum(last, "VWAP"),
            "CLOSE": fnum(last, "close"),
        }
        if tf == "1M":
            # 1m file carries 1m EMA9/21/50/100/200 directly
            ind[tf]["EMA50"] = fnum(last, "1m EMA50")
            ind[tf]["EMA100"] = fnum(last, "1m EMA100")
            ind[tf]["EMA200"] = fnum(last, "1m EMA200")
    ind["5S"] = {"PVT": ind["1M"]["PVT"], "PVTTrendFlag": ind["1M"]["PVTTrendFlag"]}
    ind["15S"] = dict(ind["5S"])

    return {
        "INDICATORS": ind,
        "SETUPS": {
            "DASHBOARD": {"DPM": "Bearish", "DPMPVT": "Negative", "WS": "Bearish",
                          "DS": "Bearish", "2PM": "Bearish", "3PM": "Bearish"},
            "1M": {"Uptrend": "no", "Downtrend": "yes"},
        },
    }


def _col_tf(tf):
    return {"1M": "1m", "5M": "5m", "30M": "30m", "2H": "2H"}[tf]


def main():
    snap = build_snapshot()
    eng = ForecastEngine()
    fc = eng.on_snapshot(snap)
    assert fc is not None, "forecast is None"
    print(json.dumps(fc, indent=1))

    # structural assertions
    assert fc["regime"] and fc["regime"].startswith("D"), fc["regime"]
    assert fc["price"] and fc["price"] > 0
    for side in ("resistance", "support"):
        for lv in fc[side]:
            assert lv["level"] > 0
            assert 5.0 <= lv["confidence"] <= 95.0, lv
            assert lv["basis"]
    for lv in fc["resistance"]:
        assert lv["level"] > fc["price"], "resistance below price"
    for lv in fc["support"]:
        assert lv["level"] < fc["price"], "support above price"
    assert fc["expected_shape"]
    assert fc["bias_label"] in ("bullish", "bearish", "neutral")
    print("\nSmoke test OK — regime=%s bias=%s (%.2f%%)" % (
        fc["regime"], fc["bias_label"], fc["bias_pct"] * 100))


if __name__ == "__main__":
    main()
