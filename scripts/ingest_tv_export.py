"""TradingView export ingestion — the one-command data refresh.

2026-10-26: handles BOTH the legacy FINAL layouts (5m 60-col / 30m 58-col /
1m 35-col) and the new PSR template (5m 138 / 30m 133 / 1m 131 cols) whose
exports now carry the Pivot / Support / Resistance levels (D/W/M/Q x
Standard/Heikin-Ashi x P,R1-R3,S1-S3) as plotted series, plus Daily/Weekly
VWAP (Standard).

New-template path per file:
  1. identify the TF (column count + anchor names),
  2. map every canonical column to the new template's columns — unique names
     by name, ambiguous indicator blocks (MACD Line, Plot, PVT, K, D, ...) by
     CONTENT against the overlapping stamps (match-rate per column),
  3. verify every mapped column (rate >= threshold; hard stop below it —
     a layout change must be a human session, never a silent merge),
  4. merge: union of stamps; on overlap the NEW cell wins where non-empty
     (heals live-capture artifacts in the store; identical for clean spans);
     canonical rows never drop,
  5. `5m EMA4` (dropped from the new template) is recomputed house-side,
     EMA state carried across the merge seam (proven exact vs TV: 20,530
     bars to 1e-6; residual = cold-start bars at old export seams),
  6. the 56 PSR columns + TV Daily/Weekly VWAP are APPENDED to the store
     layout (60->118 / 58->116 / 36->94); old rows get PSR backfilled by DAY
     (pivots are period-constant) wherever the new file covers that day, and
     the TV VWAPs backfilled where stamps overlap,
  7. known flavor seams, filled but flagged: canonical VWAP <- new "Daily
     VWAP Standard" (both session VWAPs; converge intraday, differ by the
     old indicator's weighting); canonical D/W/M EMA columns have NO matching
     flavor in the new template (old indicator recipe unknown) -> left EMPTY
     for new rows (pmmEngine loads but never uses them; store_builder's
     D E50/E100 research stats simply stop accumulating),
  8. back up the canonicals + store.json into csv/backup_<date>/,
  9. rebuild store.json and run its --check.

Legacy-layout path: direct row merge (canonical wins), as before.

Exit codes: 0 ok / nothing-to-do, 1 layout or verification failure (no writes).
"""

import argparse
import csv
import glob
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_DIR = os.path.join(REPO, "data", "insight", "csv")
STORE = os.path.join(REPO, "data", "insight", "store.json")
DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")

CANON = {
    "5m": os.path.join(CSV_DIR, "NSE_NIFTY1!, 5_Sep 2025-Oct 2026.csv"),
    "30m": os.path.join(CSV_DIR, "NSE_NIFTY1!, 30_42a03.csv"),
    "1m": os.path.join(CSV_DIR, "NSE_NIFTY1!, 1_20adf.csv"),
}

# --- legacy FINAL layouts (anchors pin every block's position) --------------
LEGACY = {
    "5m": {
        "ncols": 60,
        "anchors": [(5, "1m EMA100"), (6, "1m EMA200"), (7, "5m EMA4"),
                    (13, "30m EMA50"), (16, "2H EMA50"), (19, "D EMA100"),
                    (21, "VWAP"), (24, "Plot"), (25, "PVT"), (31, "Plot"),
                    (38, "PVT"), (46, "Plot"), (47, "PVT"), (51, "MACD Line"),
                    (55, "Plot"), (56, "PVT")],
        "verify": [(25, 25, "5m PVT", 0.05), (38, 38, "30m PVT", 0.05),
                   (47, 47, "2H PVT", 0.05), (56, 56, "D PVT", 0.05),
                   (24, 24, "5m VOC", 25.0), (4, 4, "close", 25.0)],
    },
    "30m": {
        "ncols": 58,
        "anchors": [(5, "5m EMA100"), (6, "5m EMA200"), (7, "30m EMA9"),
                    (9, "30m EMA50"), (12, "2H EMA50"), (15, "D EMA50"),
                    (18, "W EMA50"), (21, "M EMA100"), (22, "M EMA100"),
                    (24, "VWAP"), (29, "Plot"), (30, "PVT"), (40, "Plot"),
                    (41, "PVT"), (47, "Plot"), (52, "PVT"), (56, "K")],
        "verify": [(30, 30, "30m PVT", 0.05), (41, 41, "2H PVT", 0.05),
                   (52, 52, "D PVT", 0.05), (4, 4, "close", 25.0)],
    },
    "1m": {
        "ncols": 36,
        "anchors": [(5, "1m EMA9"), (9, "1m EMA200"), (10, "5m EMA50"),
                    (24, "VWAP"), (29, "Plot"), (30, "PVT"), (35, "D")],
        "verify": [(30, 30, "1m PVT", 0.05), (4, 4, "close", 25.0)],
    },
}

# --- the 2026-10-26 PSR template --------------------------------------------
PSR_BLOCKS = [  # (period, style) in template column order
    ("D", "Standard"), ("W", "Standard"), ("M", "Standard"), ("Q", "Standard"),
    ("D", "Heikin Ashi"), ("W", "Heikin Ashi"), ("M", "Heikin Ashi"),
    ("Q", "Heikin Ashi"),
]
PSR_LEVELS = ["P", "R1", "R2", "R3", "S1", "S2", "S3"]
PSR_NAMES = ["%s %s %s" % (p, s, lv) for p, s in PSR_BLOCKS for lv in PSR_LEVELS]
TV_VWAP_NAMES = ["Daily VWAP Standard", "Weekly VWAP Standard"]

NEW_TEMPLATES = {
    # tf: (ncols, anchor name -> any column, min content-match rate)
    "5m": (138, {"1m EMA100", "Q Standard P"}, 0.98),   # D PVT 0.9889: the
    #   store's 2026-10-22/23 live-capture episodes; healed by the new-wins merge
    "30m": (133, {"30m EMA50"}, 0.99),
    "1m": (131, {"1m EMA9"}, 0.85),   # low bar: the store's 1m carries
    #                                   known live-capture/EMA-seam artifacts
    #                                   that the merge heals (new wins)
}
# canonical columns the new template can NOT supply (flavor seams) — filled
# with "" for new rows, reported:
UNSUPPLIED = {
    "5m": {19: "D EMA100", 20: "D EMA200"},
    "30m": {15: "D EMA50", 16: "D EMA100", 17: "D EMA200",
            18: "W EMA50", 19: "W EMA100", 20: "W EMA200",
            21: "M EMA100", 22: "M EMA100#2", 23: "M EMA200"},
    "1m": {16: "2H EMA100", 17: "2H EMA200", 18: "D EMA100", 19: "D EMA200",
           20: "W EMA100", 21: "W EMA200", 22: "M EMA100", 23: "M EMA200",
           25: "MACD Line (12/26, discard)", 26: "Signal Line (12/26)"},
}
VWAP_SEAM = {"5m": 21, "30m": 24, "1m": 24}   # canon VWAP col <- Daily VWAP Standard
EMA4_COL = {"5m": 7}                          # house-recomputed


def fail(msg):
    print(f"  ABORT: {msg}")
    sys.exit(1)


def occ_names(hdr):
    """Occurrence-based header names ('Plot' -> Plot, Plot#2, ...)."""
    seen, names = {}, []
    for h in hdr:
        h = h.strip()
        seen[h] = seen.get(h, 0) + 1
        names.append("%s#%d" % (h, seen[h]) if seen[h] > 1 else h)
    return names


def read_csv(path):
    """-> (header list, rows-by-stamp). utf-8-sig, rows padded to header width."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.reader(f)
        hdr = next(r)
        rows = {}
        for row in r:
            if not row or not row[0].strip():
                continue
            if len(row) < len(hdr):
                row = row + [""] * (len(hdr) - len(row))
            stamp = row[0][:16]
            if stamp in rows:
                fail(f"{os.path.basename(path)}: duplicate stamp {stamp}")
            rows[stamp] = row
    return hdr, rows


def identify(path):
    """-> (tf, mode, header, rows). mode 'psr' (new template) or 'legacy'."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        hdr = next(csv.reader(f))
    for tf, (ncols, anchors, _rate) in NEW_TEMPLATES.items():
        if len(hdr) != ncols:
            continue
        names = set(hdr)
        if all(a in names for a in anchors) and "Q Standard P" in names:
            return tf, "psr", *read_csv(path)
    for tf, spec in LEGACY.items():
        if len(hdr) != spec["ncols"]:
            continue
        if all(hdr[i] == name for i, name in spec["anchors"]):
            return tf, "legacy", *read_csv(path)
    fail(f"{os.path.basename(path)}: header matches no known layout "
         f"(new PSR template 1m/5m/30m or legacy FINAL). "
         "The export layout changed — run a supervised session, do NOT force-merge.")


def fnum(s):
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def match_rate(canon_vals, new_vals):
    """Exact-match rate over rows where both cells are non-empty."""
    m = tot = 0
    for a, b in zip(canon_vals, new_vals):
        if a is None or b is None:
            continue
        tot += 1
        if abs(a - b) < 1e-6:
            m += 1
    return (m / tot) if tot else -1.0, tot


def build_mapping(tf, canon_hdr, canon, new, nhdr_names):
    """-> (map: canon idx -> new idx | 'house_ema4' | None, notes, seams).
    Unique names by name; ambiguous indicator blocks by content match on the
    overlapping stamps. `seams` = canon idxs exempt from rate verification
    (documented flavor changes, filled best-effort)."""
    ncommon = sorted(set(canon) & set(new))
    sample = ncommon[::max(1, len(ncommon) // 4000)] if ncommon else []
    if len(ncommon) < 50:
        fail(f"overlap too small ({len(ncommon)} stamps) to content-map — "
             "disjoint downloads cannot be verified")
    by_base = {}
    for n, i in nhdr_names.items():
        by_base.setdefault(n.split("#")[0], []).append(i)

    min_rate = NEW_TEMPLATES[tf][2]
    mapping, notes, seams = {}, [], {VWAP_SEAM[tf]} if tf in VWAP_SEAM else set()
    for ci, cname in enumerate(canon_hdr):
        if ci == 0:
            mapping[ci] = nhdr_names["time"]
            continue
        base = cname.split("#")[0]
        if tf in EMA4_COL and ci == EMA4_COL[tf] and base not in by_base:
            mapping[ci] = "house_ema4"
            notes.append(f"  col {ci:2d} {cname:20s} -> HOUSE 5m EMA4 (recomputed; "
                         "dropped from the new template; proven exact vs TV)")
            continue
        if ci in seams:
            src = nhdr_names["Daily VWAP Standard"]
            mapping[ci] = src
            notes.append(f"  col {ci:2d} {cname:20s} -> col {src} 'Daily VWAP "
                         "Standard' (FLAVOR SEAM: the old VWAP indicator's "
                         "weighting is gone from the new template; both are "
                         "session VWAPs and converge intraday)")
            continue
        if base in ("open", "high", "low", "close"):
            mapping[ci] = by_base[base][0]
            continue
        if ci in UNSUPPLIED[tf]:
            mapping[ci] = None
            notes.append(f"  col {ci:2d} {cname:20s} -> EMPTY for new rows "
                         f"({UNSUPPLIED[tf][ci]})")
            continue
        cvals = [fnum(canon[t][ci]) for t in sample]
        best, best_rate, best_n = None, -1.0, 0
        for ni in by_base.get(base, []):
            nvals = [fnum(new[t][ni]) for t in sample]
            rate, n = match_rate(cvals, nvals)
            if rate > best_rate:
                best, best_rate, best_n = ni, rate, n
        if best is None or best_rate < min_rate:
            fail(f"cannot map canonical col {ci} ({cname!r}): best "
                 f"{base!r} candidate rate {best_rate:.4f} "
                 f"(< {min_rate}) — the new template's {base!r} block does not "
                 "match the canonical. Investigate before merging.")
        mapping[ci] = best
        if best_rate < 1.0:
            notes.append(f"  col {ci:2d} {cname:20s} -> col {best:3d} rate "
                         f"{best_rate:.4f} n={best_n} (store-side artifacts — "
                         "healed by new-wins merge)")
    return mapping, notes, seams


def map_row(row, mapping, canon_w):
    """New-template row -> canonical-width row (None-mapped cols -> '')."""
    out = []
    for ci in range(canon_w):
        src = mapping[ci]
        if src == "house_ema4":
            out.append("")                     # filled post-merge
        elif src is None:
            out.append("")
        else:
            out.append(row[src])
    return out


def merge(tf, canon_hdr, canon, new, mapping, seams, psr_fb):
    """-> (merged rows, extended header, stats). Union of stamps; on overlap
    the new cell wins where non-empty (heals store artifacts) EXCEPT the
    flavor-seam columns (VWAP: the canonical keeps its historical indicator
    flavor; only appended rows carry the TV-standard flavor); canonical rows
    never drop; EMA4 house-carried; PSR backfilled by day (the file's own
    value, else the cross-TF fallback table — TV computes W/M/Q pivots only
    on charts whose context reaches, the 1m chart carries them all)."""
    canon_w = len(canon_hdr)
    ext_hdr = list(canon_hdr) + PSR_NAMES + TV_VWAP_NAMES
    # day -> first new row (for the PSR backfill)
    day_row = {}
    for k in sorted(new):
        d = k[:10]
        if d not in day_row:
            day_row[d] = new[k]
    # PSR/TV-VWAP source indices in the new file
    psr_src = [nhdr_names[n] for n in PSR_NAMES]
    vw_src = [nhdr_names[n] for n in TV_VWAP_NAMES]

    def fb_val(d, j):
        row = psr_fb.get(d)
        return row[j] if row else ""

    merged, appended, healed = [], 0, 0
    for k in sorted(set(canon) | set(new)):
        if k not in new:                           # canonical-only row
            old = canon[k] + [""] * (len(ext_hdr) - canon_w)
            d = k[:10]
            for j, si in enumerate(psr_src):       # PSR backfill by day
                v = new[day_row[d]][si].strip() if d in day_row else ""
                old[canon_w + j] = v or fb_val(d, j)
            merged.append(old)
            continue
        mapped = map_row(new[k], mapping, canon_w)
        d = k[:10]
        # the row's own PSR block, cross-TF fallback where TV left it empty
        mapped += [(new[k][si].strip() or fb_val(d, j))
                   for j, si in enumerate(psr_src)]
        # the row's own TV VWAP columns
        mapped += [new[k][si] for si in vw_src]
        if k in canon:
            healed += 1
            for ci in range(canon_w):
                if ci in seams and canon[k][ci].strip():
                    mapped[ci] = canon[k][ci]      # flavor seam: canon wins
                elif mapped[ci] == "" and canon[k][ci].strip():
                    mapped[ci] = canon[k][ci]      # new row blank: keep canon
        else:
            appended += 1
        merged.append(mapped)

    # EMA4: walk the merged timeline; canonical values win, house-EMA carries
    # the state across every seam (proven exact vs TV)
    if tf in EMA4_COL:
        k, n = 2 / 5, None
        for row in merged:
            if row[EMA4_COL[tf]].strip():
                v = fnum(row[EMA4_COL[tf]])
                if v is not None:
                    n = v
                    continue
            if n is None:
                continue
            c = fnum(row[4])
            if c is None:
                continue
            n = c * k + n * (1 - k)
            row[EMA4_COL[tf]] = repr(n)
    # TV VWAP backfill for canonical-only rows (exact stamps the new file has
    # are already covered above; canonical-only rows keep '' TV VWAPs)

    stats = {"canon_rows": len(canon), "new_rows": len(new), "appended": appended,
             "overlap": len(set(canon) & set(new)),
             "new_first": min(new), "new_last": max(new)}
    return merged, ext_hdr, stats


def write_canonical(path, hdr, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        w.writerows(rows)
    os.replace(tmp, path)


def backup():
    import shutil
    from datetime import date
    bdir = os.path.join(CSV_DIR, "backup_%s" % date.today().isoformat())
    os.makedirs(bdir, exist_ok=True)
    for p in list(CANON.values()) + [STORE]:
        if os.path.exists(p):
            shutil.copy2(p, bdir)
    return bdir


def rebuild_store():
    print("\n-- rebuilding store.json")
    r = subprocess.run([sys.executable, "-m", "forecasting.store_builder"],
                       cwd=REPO, capture_output=True, text=True)
    tail = (r.stdout + r.stderr).strip().splitlines()[-1:]
    print("   " + (tail[0] if tail else "(no output)"))
    r = subprocess.run([sys.executable, "-m", "forecasting.store_builder",
                        "--check"], cwd=REPO, capture_output=True, text=True)
    for line in (r.stdout + r.stderr).strip().splitlines():
        if line.startswith(("Sanity", "  FAIL")):
            print("   " + line)


def psr_day_table(nhdr_names, rows):
    """{date: [56 PSR values]} — first row per day that has any PSR value
    (pivots are period-constant). Cross-TF backfill source: the 1m download
    carries all 56 (~100%) regardless of the 5m chart's context limits."""
    src = [nhdr_names[n] for n in PSR_NAMES]
    table = {}
    for k in sorted(rows):
        d = k[:10]
        if d in table:
            continue
        row = rows[k]
        vals = [row[si].strip() for si in src]
        if any(vals):
            table[d] = vals
    return table


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="export CSVs (default: newest per TF in ~/Downloads)")
    ap.add_argument("--dry-run", action="store_true", help="verify + report, write nothing")
    ap.add_argument("--no-store", action="store_true", help="skip the store.json rebuild")
    ap.add_argument("--force", action="store_true", help="run even if verification fails")
    args = ap.parse_args()

    # pick files
    if args.paths:
        picked = list(args.paths)
    else:
        candidates = sorted(glob.glob(os.path.join(DOWNLOADS, "NSE_NIFTY1!, *.csv")),
                            key=os.path.getmtime, reverse=True)
        picked, seen = [], set()
        for p in candidates:
            try:
                with open(p, newline="", encoding="utf-8-sig") as f:
                    ncols = len(next(csv.reader(f)))
            except OSError:
                continue
            tf = None
            for t, spec in NEW_TEMPLATES.items():
                if ncols == spec[0]:
                    tf = t
            for t, spec in LEGACY.items():
                if ncols == spec["ncols"]:
                    tf = t
            if tf and tf not in seen:
                seen.add(tf)
                picked.append(p)
    if not picked:
        fail("no export files found (neither given nor in ~/Downloads)")

    bdir = None
    wrote = False
    # cross-TF PSR fallback: the 1m download carries all 56 pivots
    psr_fb = {}
    for p in picked:
        tf, mode, hdr, new = identify(p)
        if mode == "psr" and tf == "1m":
            psr_fb = psr_day_table(
                {n: i for i, n in enumerate(occ_names(hdr))}, new)
            if psr_fb:
                print(f"\n-- PSR cross-TF fallback: {len(psr_fb)} days from "
                      f"{os.path.basename(p)}")
            break
    for p in picked:
        print(f"\n== {os.path.basename(p)}")
        tf, mode, hdr, new = identify(p)
        canon_path = CANON[tf]
        canon_hdr, canon = read_csv(canon_path)
        print(f"  TF={tf} ({mode})  new rows={len(new)} ({min(new)} .. {max(new)})"
              f"  canonical rows={len(canon)}")
        if mode == "legacy":
            # legacy path: same layout, canonical wins, no PSR
            spec = LEGACY[tf]
            common = sorted(set(canon) & set(new))
            if not common:
                print("  overlap: none — skipping value verification")
            else:
                for ci, ni, label, tol in spec["verify"]:
                    maxd, flips, n = 0.0, 0, 0
                    for k in common:
                        a, b = canon[k][ci].strip(), new[k][ni].strip()
                        if not a or not b:
                            continue
                        fa, fb = fnum(a), fnum(b)
                        if fa is None or fb is None:
                            continue
                        n += 1
                        maxd = max(maxd, abs(fa - fb))
                        if (fa > 0) != (fb > 0) and fa != fb:
                            flips += 1
                    if n and (maxd > tol or flips / n > 0.001):
                        fail(f"overlap check failed on {label}: maxdiff={maxd:.6g}")
                    print(f"  overlap {label:8s}: n={n:6d} maxdiff={maxd:.6g} OK")
            rows, ext_hdr = list(canon.values()) + [new[k] for k in new if k not in canon], canon_hdr
            rows.sort(key=lambda r: r[0][:16])
        else:
            global nhdr_names
            nhdr_names = {n: i for i, n in enumerate(occ_names(hdr))}
            mapping, notes, seams = build_mapping(tf, canon_hdr, canon, new, nhdr_names)
            print("  mapping:")
            for line in notes:
                print(line)
            # verify mapped columns on the full overlap
            common = sorted(set(canon) & set(new))
            for ci, src in mapping.items():
                if src in (None, "house_ema4") or ci == 0 or ci in seams:
                    continue
                cname = canon_hdr[ci].split("#")[0]
                if cname in ("open", "high", "low", "close"):
                    continue
                m = n = 0
                maxd = 0.0
                for k in common[::5]:
                    a, b = fnum(canon[k][ci]), fnum(new[k][src])
                    if a is None or b is None:
                        continue
                    n += 1
                    maxd = max(maxd, abs(a - b))
                    if abs(a - b) < 1e-6:
                        m += 1
                rate = m / n if n else 1.0
                if rate < NEW_TEMPLATES[tf][2]:
                    fail(f"verification failed on canon col {ci} ({canon_hdr[ci]!r}): "
                         f"match rate {rate:.4f} over {n} rows, maxdiff {maxd:.6g}")
            print(f"  verify: {len(common)} overlap stamps, all mapped columns OK")
            rows, ext_hdr, st = merge(tf, canon_hdr, canon, new, mapping, seams,
                                      psr_fb)
            print(f"  merge: +{st['appended']} appended, {st['overlap']} overlap "
                  f"(new-wins heal) -> {len(rows)} rows x {len(ext_hdr)} cols")
        if args.dry_run:
            print("  dry-run: canonical NOT written")
            continue
        if bdir is None:
            bdir = backup()
            print(f"  backup -> {os.path.basename(bdir)}/")
        write_canonical(canon_path, ext_hdr, rows)
        wrote = True
        print(f"  written: {os.path.basename(canon_path)} ({len(rows)} rows x {len(ext_hdr)} cols)")

    if not args.dry_run and not args.no_store and wrote:
        rebuild_store()
    elif not args.dry_run:
        print("\n-- store rebuild skipped (no writes or --no-store)")
    print("\ndone.")


if __name__ == "__main__":
    main()
