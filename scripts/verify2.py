"""Verify the combined export against the three old files on the overlap."""
import csv
import collections

REPO = "/Users/phvrao/Projects/Algo-Trading"
CSV = REPO + "/data/insight/csv/"
COMB = "/Users/phvrao/Downloads/NSE_NIFTY1!, 30m 5m 1m.csv"


def load_named(path):
    """Parse with duplicate headers suffixed #2/#3 (store_builder style,
    but MACD pairs keep their generic names until identified)."""
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


hdr_c, comb = load_named(COMB)
print("-- combined columns (name: populated / changed) --")
pop = collections.Counter()
chg = collections.Counter()
prev = None
for r in comb:
    for h in hdr_c[5:]:
        v = (r.get(h) or "").strip()
        if v:
            pop[h] += 1
        if prev is not None and v and v != (prev.get(h) or "").strip():
            chg[h] += 1
    prev = r
for h in hdr_c[5:]:
    print("  %-14s pop=%-6d chg=%d" % (h, pop[h], chg[h]))
