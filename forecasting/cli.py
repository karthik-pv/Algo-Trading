"""Blind-test CLI: run the forecast engine on a pasted snapshot JSON.

Usage:
    python -m forecasting.cli snapshot.json [--date YYYY-MM-DD] [--out out.json]

Protocol (HANDOFF 4): paste a past day's snapshot WITHOUT the date for a
blind forecast; reveal the date afterwards and re-run with --date to score
(score.py). --date injects the correct daily context (PDH/PDL/D-EMAs) for
that day and disables jsonl logging.
"""

import argparse
import json
import sys

from forecasting.engine import ForecastEngine


def main(argv=None):
    ap = argparse.ArgumentParser(description="Blind-test the forecasting engine")
    ap.add_argument("snapshot", help="path to a webhook snapshot JSON file (or '-' for stdin)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD of the snapshot (reveals context; disables logging)")
    ap.add_argument("--store", default=None, help="override store.json path")
    ap.add_argument("--out", default=None, help="write forecast JSON to this file too")
    args = ap.parse_args(argv)

    if args.snapshot == "-":
        data = json.load(sys.stdin)
    else:
        with open(args.snapshot) as f:
            data = json.load(f)

    kwargs = {}
    if args.store:
        kwargs["store_path"] = args.store
    eng = ForecastEngine(**kwargs)

    if args.date:
        eng.set_day(args.date)
    forecast = eng.on_snapshot(data, log=False)

    if forecast is None:
        print("Forecast failed (see logs)", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(forecast, indent=1))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(forecast, f, indent=1)
    return forecast


if __name__ == "__main__":
    main()
