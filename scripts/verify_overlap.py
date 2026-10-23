"""DEFINITIVE overlap verification: for every column of the old 30/5/1 files,
find the matching source column + stamp offset in the combined export
(Aug 3 -> Sep 25 overlap). Reports match-rate per column per candidate.
"""
import csv
import collections

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = REPO + "/data/insight/csv/"
COMB = "/Users/phvrao/Downloads/NSE_NIFTY1!, 30m 5m 1m.csv"


def load_named(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f)
        raw = [x.strip() for x in next(rd)]
        hdr, cnt = [], {}
        for h in raw:
            cnt[h] = cnt.get(h, 0) + 1
            hdr.append(h if cnt[h] == 1 else "%s#%d" % (h, cnt[h]))
        rows = []
        for r in rd:
            if len(r) < len(hdr):
                r += [""] * (len(hdr) - len(r))
            rows.append(dict(zip(hdr, r)))
    return hdr, rows


def f(x):
    try:
        return float((x or "").strip())
    except (ValueError, TypeError):
        return None


def mins(ts):
    return int(ts[11:13]) * 60 + int(ts[14:16])


hdr_c, comb = load_named(COMB)
cidx = {r["time"][:16]: r for r in comb}
cdates = {r["time"][:10] for r in comb}


def shift(ts, dm):
    """wall-clock shift in minutes (intraday, no day-crossing needed 09:15-15:44)"""
    m = mins(ts) + dm
    return ts[:11] + "%02d:%02d" % (m // 60, m % 60)


def verify(fname, off_map):
    """off_map: column-name -> list of (combined-col, offset, label)"""
    hdr_o, old = load_named(CSV + fname)
    print("\n=== %s ===" % fname)
    stats = collections.defaultdict(lambda: collections.defaultdict(int))  # col -> cand -> (match, n)
    for r in old:
        d, t = r["time"][:10], r["time"][:16]
        if d not in cdates:
            continue
        for colname in hdr_o[5:]:
            a = f(r[colname])
            if a is None:
                continue
            for ccol, off, label in off_map.get(colname, []):
                cr = cidx.get(shift(r["time"], off))
                if cr is None:
                    continue
                b = f(cr.get(ccol))
                key = (colname, ccol, off, label)
                stats[key]["n"] += 1
                if b is not None and abs(a - b) <= 1e-6 * max(1.0, abs(a)):
                    stats[key]["ok"] += 1
    # summarize: best candidate per old column
    bycol = collections.defaultdict(list)
    for (colname, ccol, off, label), s in stats.items():
        if s["n"] >= 100:
            bycol[colname].append((s["ok"] / s["n"], s["n"], ccol, off, label))
    for colname in hdr_o[5:]:
        cands = sorted(bycol.get(colname, []), reverse=True)
        if not cands:
            print("  %-16s (no candidates / n<100)" % colname)
            continue
        best = cands[0]
        rest = " | ".join("%s off=%d %.4f" % (c[2], c[3], c[0]) for c in cands[1:3])
        flag = "OK " if best[0] > 0.999 else "~  "
        print("  %s%-16s <- %-14s off=%+3d  match=%.4f (n=%d)  %s" % (
            flag, colname, best[2], best[3], best[0], best[1], rest))


# candidate sources
EMA_CARRIED = [  # carried on every 1m row -> same stamp or close-stamp
    ("1m EMA9", 0), ("1m EMA9", 4), ("1m EMA9", -1),
]
offs = {}
for name in ["1m EMA9", "1m EMA21", "1m EMA50", "1m EMA100", "1m EMA200"]:
    offs[name] = [(name, o, "cand") for o in (0, 4)]
for name in ["5m EMA50", "5m EMA100", "5m EMA200"]:
    offs[name] = [(name, o, "cand") for o in (0, 4, -1)]
for name in ["30m EMA50", "30m EMA100", "30m EMA200", "2H EMA50", "2H EMA100", "2H EMA200"]:
    offs[name] = [(name, o, "cand") for o in (0, 29, 30, -1)]
offs["VWAP"] = [("VWAP", 0, "cand"), ("VWAP", 4, "cand")]
offs["PVT"] = [("PVT#2", 0, "5mPVT"), ("PVT#2", 4, "5mPVT"), ("PVT#3", 0, "30mPVT"), ("PVT#3", 30, "30mPVT"), ("PVT", 0, "1mPVT"), ("PVT", 4, "1mPVT")]
for name, ccol in [("K", "K#2"), ("D", "D#2")]:
    offs[name] = [(ccol, 4, "5mK@close"), (ccol, 0, "5mK@open"), ("K#3", 0, "30mK"), ("K#3", 30, "30mK")]
for name, ccol in [("MACD_Line_1226", "MACD Line#2"), ("Signal_Line_1226", "Signal Line#2"),
                   ("MACD_Line_2452", "MACD Line#3"), ("Signal_Line_2452", "Signal Line#3")]:
    offs[name] = [(ccol, 4, "5m@close"), (ccol, 0, "5m@open"), (ccol, 0, "x")]

verify("NSE_NIFTY1!, 5_be22a.csv", offs)

# 30m file
offs30 = {}
for name in ["30m EMA50", "30m EMA100", "30m EMA200", "2H EMA50", "2H EMA100", "2H EMA200",
             "5m EMA100", "5m EMA200"]:
    offs30[name] = [(name, o, "cand") for o in (0, 29, 30, -29, -30)]
offs30["VWAP"] = [("VWAP", 0, "cand"), ("VWAP", 29, "cand"), ("VWAP", 30, "cand")]
offs30["PVT"] = [("PVT#3", 0, "30mPVT"), ("PVT#3", 29, "30mPVT"), ("PVT#3", 30, "30mPVT"),
                 ("PVT#2", 0, "5mPVT"), ("PVT", 0, "1mPVT")]
for name, ccol in [("K", "K#3"), ("D", "D#3")]:
    offs30[name] = [(ccol, 29, "30m@close29"), (ccol, 30, "30m@close30"), (ccol, 0, "same")]
for name, ccol in [("MACD_Line_1226", "MACD Line#4"), ("Signal_Line_1226", "Signal Line#4"),
                   ("MACD_Line_2452", "MACD Line#5"), ("Signal_Line_2452", "Signal Line#5")]:
    offs30[name] = [(ccol, 29, "@close29"), (ccol, 30, "@close30"), (ccol, 0, "same")]
verify("NSE_NIFTY1!, 30_42a03.csv", offs30)

# 1m file
offs1 = {}
for name in ["1m EMA9", "1m EMA21", "1m EMA50", "1m EMA100", "1m EMA200"]:
    offs1[name] = [(name, 0, "same")]
for name in ["5m EMA50", "5m EMA100", "5m EMA200"]:
    offs1[name] = [(name, 0, "same"), (name, -1, "prev")]
for name in ["30m EMA50", "30m EMA100", "30m EMA200", "2H EMA100", "2H EMA200"]:
    offs1[name] = [(name, 0, "same")]
offs1["VWAP"] = [("VWAP", 0, "same")]
offs1["PVT"] = [("PVT", 0, "1mPVT"), ("PVT#2", 0, "5mPVT"), ("PVT#3", 0, "30mPVT")]
for name, ccol in [("MACD_Line_1226", "MACD Line"), ("Signal_Line_1226", "Signal Line"),
                   ("MACD_Line_2452", "MACD Line"), ("Signal_Line_2452", "Signal Line")]:
    offs1[name] = [(ccol, 0, "1m-pair")]
for name, ccol in [("K", "K"), ("D", "D")]:
    offs1[name] = [(ccol, 0, "1mK")]
verify("NSE_NIFTY1!, 1_20adf.csv", offs1)
