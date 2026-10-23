"""PM-ladder validation (user model, 2026-10-20).

The ladder (each line mirrored for downtrending):

    D  PM uptrending -> price keeps rising from 3E50/100/200 with 30m PM cross up and swing
    2  PM uptrending -> price keeps rising from 5E50/100/200 with 5m  PM cross up and swing
    3  PM uptrending -> price keeps rising from 1E100/200    with 5m  PM cross up and swing
    5  PM uptrending -> price keeps rising from 1E9/21/50    with 1m  PM cross up and swing

Trade cases: Intra = 5PM swing, Scalp = 1PM swing, Ultra = 15PM swing (15s).

Conventions (locked, per handoffs + today's answers):
  - PM pair per TF = PVT sign AND MACD_2452 vs Signal direction; "uptrending" = both up.
  - Cross trigger = PVT zero-cross AND MACD_2452 signal-cross, same direction.
    strict  = both cross on the same effective trigger-TF bar;
    relaxed = both cross within +/-3 TRIGGER-TF bars (either order), entry at the
              later cross; min gap 4 trigger-TF bars between entries. Same-day
              contiguous rows only, stamps <= 15:25. Entry = trigger-TF close
              (no-repaint).
    For the D layer the relaxation unit is the 30m bar (6 rows of the 5m grid):
    +/-3 30m bars = +/-17 rows, because the two 30m oscillators cross on
    different 30m bars in general.
  - Carried-TF timing (verified 2026-10-20 on the 5m export): a 30m PVT sign
    flip prints on the first row of the new 30m window (:15/:45 stamps) while
    the same completed window's MACD prints one row earlier (:10/:40 stamps),
    so the D layer pairs the MACD flip at row+1 (state_lag=1). Native-TF
    triggers (5m/1m/15s) pair same-row (state_lag=0).
  - RUNG ANCHOR (the ladder's actual trade): the cross must fire with price IN
    the rung zone - a rung tagged (low <= L <= high) within the last Z trigger-
    TF bars before/at the entry. Reported as the "_at_rungs" variants next to
    the raw crosses.
  - Rung touch/hold tables use the locked definitions on the rung's own grid:
    support touch = open > L and low <= L, hold = close > L (up context);
    resistance touch = open < L and high >= L, reject = close < L (down
    context). 30m rungs: synthetic 30m grid (30m OHLC from the 5m rows; the
    carried EMA on the window's rows = last completed value, no-repaint).
  - Exits per episode: "swing_end" = first later trigger-TF row where the PM
    pair breaks (PVT sign flips against OR MACD state flips against), EOD flat
    15:25 if unbroken; "ema" = family exit (30m trigger -> 5m EMA21, 5m trigger
    -> 5m EMA4, 1m trigger -> 1m EMA9). pts = move in the trend direction.
  - Fixed horizons (same-session only) reported alongside per layer.

Data windows (tiered):
  - 5m export (canonical, 2025-09-15 .. 2026-10-16): D / 2 / 3 layers' triggers
    and the 2-layer rungs on the full year.
  - 1m exports: 1_20adf (2026-07-13 .. 2026-10-19) for the 3-layer rungs + the
    1m rung tags; 1m.csv (2026-08-03 .. 2026-10-16, carries the 5m state
    columns) for the 5 layer.
  - 15S export (2026-10-14 .. 2026-10-16, ~2.5 sessions): ultra tier - tiny n.

Run:
    python -m forecasting.pm_ladder            # full run -> report + json
    python -m forecasting.pm_ladder --check    # cross-file consistency checks only

Writes: data/insight/reports/pm_ladder.json / pm_ladder.md
"""

import argparse
import csv
import json
import os
from datetime import datetime

from forecasting.store_builder import read_tv_csv, fnum, sgn
from forecasting.trend_insight import (load_bars, session_stamp, contiguous_runs,
                                       med)

CSV5 = os.path.join("data", "insight", "csv", "NSE_NIFTY1!, 5_Sep 2025-Oct 2026.csv")
CSV1A = os.path.join("data", "insight", "csv", "NSE_NIFTY1!, 1_20adf.csv")
CSV1B = os.path.join("data", "insight", "csv", "NSE_NIFTY1!, 1m.csv")
CSV15 = os.path.join("data", "insight", "csv", "NSE_NIFTY1!, 15S_3c57c.csv")
OUT_DIR = os.path.join("data", "insight", "reports")

SESSION_FIRST = "09:15"
SESSION_LAST = "15:25"          # last tradeable stamp (EOD flat)
DEV_END = "2026-03-31"          # dev window ends; OOS = 2026-04-01+

RELAX_TF = 3                    # +/-3 trigger-TF bars for the relaxed trigger
DEDUPE_TF = 4                   # min trigger-TF bars between relaxed entries

HORIZONS = {                    # layer key -> horizon minutes (same-session only)
    "D": [30, 60, 120],
    "2": [15, 30, 60],
    "3": [15, 30, 60],
    "5": [5, 15, 30],
    "15s": [5, 15],
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def fv(v):
    if isinstance(v, str):
        v = v.strip()
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def macd_state(macd, signal):
    """True = MACD above signal (up), False = below, None = unknown."""
    if macd is None or signal is None:
        return None
    return macd > signal


def ff_macd_state(bars, macd_key, signal_key, out_key):
    """Forward-fill an HTF MACD state (populated only on TF-closing rows)."""
    last = None
    for b in bars:
        st = macd_state(b.get(macd_key), b.get(signal_key))
        if st is not None:
            last = st
        b[out_key] = last


def flip_events(bars, sign_key, state_key, direction):
    """Row indices where the PVT sign flips and where the MACD state flips.

    direction 'up': sign -1 -> +1 and state False -> True; 'down' mirrored.
    """
    s = 1 if direction == "up" else -1
    want = direction == "up"
    pvt_idx, macd_idx = [], []
    for i in range(1, len(bars)):
        p, b = bars[i - 1], bars[i]
        ps, bs = p.get(sign_key), b.get(sign_key)
        if ps is not None and bs is not None and ps == -s and bs == s:
            pvt_idx.append(i)
        st_p, st_b = p.get(state_key), b.get(state_key)
        if st_p is not None and st_b is not None and st_p is not want and st_b is want:
            macd_idx.append(i)
    return pvt_idx, macd_idx


def armed_entries(pvt_idx, macd_idx, relax_rows, dedupe_rows, state_lag=0):
    """Merge PVT + MACD cross indices into entry indices (within one day-run).

    Row units are trigger-grid rows. state_lag=1 for carried TFs: the window's
    MACD prints one row before the window's PVT, so the MACD flip pairs at
    row+1. strict = same effective row; relaxed = within +/-relax_rows, entry
    at the later cross; dedupe entries closer than dedupe_rows.
    """
    macd_shift = [j + state_lag for j in macd_idx] if state_lag else list(macd_idx)
    strict = sorted(set(pvt_idx) & set(macd_shift))
    cand = set(strict)
    for i in pvt_idx:
        for j in macd_shift:
            if abs(i - j) <= relax_rows:
                cand.add(max(i, j))
    relaxed = sorted(cand - set(strict))
    dedup, last_e = [], None
    for e in relaxed:
        if last_e is None or e - last_e >= dedupe_rows:
            dedup.append(e)
            last_e = e
    return strict, dedup


def tag_rungs(run, rung_keys, direction):
    """Mark rows whose bar-range crosses any rung (price is AT the zone).

    up: low <= L <= high for some rung L; down mirrored is the same test.
    """
    for b in run:
        hit = False
        o, h, l = b.get("open"), b.get("high"), b.get("low")
        if None not in (h, l):
            for k in rung_keys:
                lv = b.get(k)
                if lv is None:
                    continue
                if l <= lv <= h:
                    hit = True
                    break
        b["_rung_tag"] = hit


def set_rung_anchor(run, z_rows, tag_key="_rung_tag"):
    """b['rung_anchor'] = a rung was tagged within the last z_rows (incl. this)."""
    for i, b in enumerate(run):
        b["rung_anchor"] = any(run[j].get(tag_key) for j in range(max(0, i - z_rows), i + 1))


def episode_stats(bars, entries, direction, ctx_fn, exit_mode, horizons_min,
                  step_min, require_anchor=False):
    """Measure episodes at the given entry row indices (one day-run).

    exit_mode 'swing_end' uses the pre-set flags exit_sign_again/exit_state_again;
    'ema' uses the pre-set exit_ema. Returns a list of episode dicts.
    """
    s = 1 if direction == "up" else -1
    eps = []
    n_bars = len(bars)
    for i in entries:
        if i >= n_bars:
            continue  # entry shifted past the end of the day-run
        b = bars[i]
        if require_anchor and not b.get("rung_anchor"):
            continue
        if not ctx_fn(b):
            continue
        if b["hhmm"] > SESSION_LAST or b.get("close") is None:
            continue
        entry = b["close"]
        end_bar, eod = None, True
        for x in bars[i + 1:]:
            if x["hhmm"] > SESSION_LAST:
                break
            if exit_mode == "swing_end":
                broke = bool(x.get("exit_sign_again")) or bool(x.get("exit_state_again"))
            else:
                lv, c = x.get("exit_ema"), x.get("close")
                broke = (c < lv) if (lv is not None and c is not None and direction == "up") \
                    else (c > lv if (lv is not None and c is not None) else False)
            if broke:
                end_bar, eod = x, False
                break
        if end_bar is None:
            end_bar = bars[-1]
            eod = True
        if end_bar.get("close") is None:
            continue
        pts_exit = (end_bar["close"] - entry) * s
        hor = {}
        for hm in horizons_min:
            k = int(round(hm / float(step_min)))
            j = i + k
            if j < n_bars and bars[j]["hhmm"] <= SESSION_LAST \
                    and bars[j].get("close") is not None:
                r = (bars[j]["close"] - entry) * s
                hor["%dm" % hm] = {"pts": round(r, 1), "pos": r > 0}
            else:
                hor["%dm" % hm] = None
        eps.append({
            "date": b["date"], "from": b["hhmm"], "to": end_bar["hhmm"],
            "entry": round(entry, 2), "exit": round(end_bar["close"], 2),
            "eod": eod, "pts": round(pts_exit, 1), "hor": hor,
        })
    return eps


def summarize(eps):
    if not eps:
        return {"n": 0}
    pts = [e["pts"] for e in eps]
    out = {
        "n": len(eps),
        "n_days": len(set(e["date"] for e in eps)),
        "n_pos": sum(1 for p in pts if p > 0),
        "pos_pct": round(100.0 * sum(1 for p in pts if p > 0) / len(pts), 1),
        "n_eod_flat": sum(1 for e in eps if e["eod"]),
        "total_pts": round(sum(pts), 1),
        "mean_pts": round(sum(pts) / len(pts), 2),
        "median_pts": med(pts),
    }
    for h in ("5m", "15m", "30m", "60m", "120m"):
        vals = [e["hor"][h] for e in eps if e["hor"].get(h)]
        if vals:
            out["h%s" % h] = {
                "n": len(vals),
                "pos_pct": round(100.0 * sum(1 for v in vals if v["pos"]) / len(vals), 1),
                "mean_pts": round(sum(v["pts"] for v in vals) / len(vals), 2),
            }
    return out


def split_period(eps):
    dev = [e for e in eps if e["date"] <= DEV_END]
    oos = [e for e in eps if e["date"] > DEV_END]
    return {"dev_upto_%s" % DEV_END: summarize(dev),
            "oos_after_%s" % DEV_END: summarize(oos)}


def touch_hold(bars, rungs, direction):
    """Locked touch/hold definitions per rung, on the given bar list."""
    out = {}
    for name, key in rungs:
        hd = [0, 0]
        rj = [0, 0]
        for b in bars:
            lv, o, h, l, c = b.get(key), b.get("open"), b.get("high"), b.get("low"), b.get("close")
            if None in (lv, o, h, l, c):
                continue
            if direction == "up":
                if o > lv and l <= lv:
                    hd[0] += 1
                    if c > lv:
                        hd[1] += 1
            else:
                if o < lv and h >= lv:
                    rj[0] += 1
                    if c < lv:
                        rj[1] += 1
        if direction == "up":
            out[name] = {"touch": hd[0],
                         "hold_pct": round(100.0 * hd[1] / hd[0], 1) if hd[0] else None}
        else:
            out[name] = {"touch": rj[0],
                         "reject_pct": round(100.0 * rj[1] / rj[0], 1) if rj[0] else None}
    return out


def day_runs(bars, step_min):
    """Same-day contiguous runs (contiguity checked at step_min)."""
    return contiguous_runs(bars, step_min=step_min)


def add_swing_exit_flags(run, sign_key, state_key, direction):
    """Per row: does the PM pair break AGAINST the direction on this row?

    First row after entry where the sign is against or the state is against =
    the flip row (at entry the pair just aligned, so the first against row is
    the break)."""
    want = direction == "up"
    s = 1 if direction == "up" else -1
    for b in run:
        sg, st = b.get(sign_key), b.get(state_key)
        b["exit_sign_again"] = (sg is not None and sg == -s)
        b["exit_state_again"] = (st is not None and st is not want)


def set_prevalence(out, bars, up_key, dn_key):
    n = len(bars)
    out["prevalence"] = {
        "bars": n,
        "up_pct": round(100.0 * sum(1 for b in bars if b[up_key]) / n, 1) if n else None,
        "down_pct": round(100.0 * sum(1 for b in bars if b[dn_key]) / n, 1) if n else None,
    }


def collect_layer_stats(runs, direction, cfg):
    """Run one layer x direction over day-runs; return the variant summaries.

    cfg keys: sign_key, state_key, state_lag, relax_rows, dedupe_rows,
    ctx_key, anchor_z (None = no anchor), rung_keys (anchor tagging on the
    trigger grid), exit_ema_key (or None), horizons, step_min.
    """
    relax_rows = cfg["relax_rows"]
    strict_all, strict_anch, relax_all, relax_anch, relax_anch_ema = [], [], [], [], []
    pvt_anch = []
    for run in runs:
        add_swing_exit_flags(run, cfg["sign_key"], cfg["state_key"], direction)
        if cfg.get("rung_keys"):
            tag_rungs(run, cfg["rung_keys"], direction)
        pvt_i, macd_i = flip_events(run, cfg["sign_key"], cfg["state_key"], direction)
        strict, relax = armed_entries(pvt_i, macd_i, relax_rows, cfg["dedupe_rows"],
                                      state_lag=cfg.get("state_lag", 0))

        def ctx_fn(b, _k=cfg["ctx_key"]):
            return b[_k]

        hz, sm = cfg["horizons"], cfg["step_min"]
        anchored = cfg.get("anchor_z") is not None
        strict_all += episode_stats(run, strict, direction, ctx_fn, "swing_end", hz, sm)
        relax_all += episode_stats(run, relax, direction, ctx_fn, "swing_end", hz, sm)
        if anchored:
            set_rung_anchor(run, cfg["anchor_z"])
            strict_anch += episode_stats(run, strict, direction, ctx_fn, "swing_end",
                                         hz, sm, require_anchor=True)
            relax_anch += episode_stats(run, relax, direction, ctx_fn, "swing_end",
                                        hz, sm, require_anchor=True)
            if cfg.get("exit_ema_key"):
                for b in run:
                    b["exit_ema"] = b.get(cfg["exit_ema_key"])
                relax_anch_ema += episode_stats(run, relax, direction, ctx_fn, "ema",
                                                hz, sm, require_anchor=True)
            # head-to-head: the established PVT-only cross (5PX/3PX convention),
            # same context + rung anchor - deduped with the same gap
            pvt_only, last_e = [], None
            for e in sorted(pvt_i):
                if last_e is None or e - last_e >= cfg["dedupe_rows"]:
                    pvt_only.append(e)
                    last_e = e
            pvt_anch += episode_stats(run, pvt_only, direction, ctx_fn, "swing_end",
                                      hz, sm, require_anchor=True)
    out = {
        "strict_same_bar": summarize(strict_all),
        "strict_by_period": split_period(strict_all),
        "relaxed_pm3tf": summarize(relax_all),
    }
    if cfg.get("anchor_z") is not None:
        out["strict_same_bar_at_rungs"] = summarize(strict_anch)
        out["relaxed_pm3tf_at_rungs"] = summarize(relax_anch)
        out["relaxed_pm3tf_at_rungs_by_period"] = split_period(relax_anch)
        if cfg.get("exit_ema_key"):
            out["relaxed_pm3tf_at_rungs_ema_exit"] = summarize(relax_anch_ema)
        out["pvt_only_at_rungs"] = summarize(pvt_anch)
        out["pvt_only_at_rungs_by_period"] = split_period(pvt_anch)
        out["episodes_relaxed_at_rungs"] = relax_anch
    else:
        out["episodes_relaxed"] = relax_all
    return out


# ---------------------------------------------------------------------------
# loaders beyond the shared ones
# ---------------------------------------------------------------------------

def load_1m_20adf(csv_path):
    """NSE_NIFTY1!, 1_20adf.csv - 1m export, generic repeated headers (36 cols).

    read_tv_csv names the two MACD pairs _1226 (first, DISCARD) / _2452 (second,
    USE) - identity re-verified numerically in run_checks().
    """
    rows = read_tv_csv(csv_path)
    bars = []
    for r in rows:
        if not r.get("date") or not r.get("hhmm"):
            continue
        b = {
            "date": r["date"], "hhmm": r["hhmm"],
            "open": fnum(r, "open"), "high": fnum(r, "high"),
            "low": fnum(r, "low"), "close": fnum(r, "close"),
            "vwap": fnum(r, "VWAP"),
            "pvt": fnum(r, "PVT"), "pvt_sign": None,
            "macd": fnum(r, "MACD_Line_2452"), "signal": fnum(r, "Signal_Line_2452"),
        }
        for e in ("1m EMA9", "1m EMA21", "1m EMA50", "1m EMA100", "1m EMA200"):
            b[e] = fnum(r, e)
        b["pvt_sign"] = sgn(b["pvt"])
        b["state"] = macd_state(b["macd"], b["signal"])
        bars.append(b)
    return bars


def load_1m_full(csv_path):
    """NSE_NIFTY1!, 1m.csv - 1m export WITH the 5m state columns (57 cols).

    1m block: '1m MACD Line'/'1m Signal Line'/'1m PVT' (named). 5m block:
    'PVT' (5m PVT, carried; flips at 5m-window-open rows) + '5m MACD Line'/
    '5m Signal Line' (close-aligned rows :19/:24/... -> ff).
    """
    rows = read_tv_csv(csv_path)
    bars = []
    for r in rows:
        if not r.get("date") or not r.get("hhmm"):
            continue
        b = {
            "date": r["date"], "hhmm": r["hhmm"],
            "open": fnum(r, "open"), "high": fnum(r, "high"),
            "low": fnum(r, "low"), "close": fnum(r, "close"),
            "vwap": fnum(r, "VWAP"),
            "pvt": fnum(r, "1m PVT"), "pvt_sign": None,
            "macd": fnum(r, "1m MACD Line"), "signal": fnum(r, "1m Signal Line"),
            "5m_pvt": fnum(r, "PVT"), "5m_macd": fnum(r, "5m MACD Line"),
            "5m_signal": fnum(r, "5m Signal Line"),
        }
        for e in ("1m EMA9", "1m EMA21", "1m EMA50", "1m EMA100", "1m EMA200"):
            b[e] = fnum(r, e)
        b["pvt_sign"] = sgn(b["pvt"])
        b["5m_pvt_sign"] = sgn(b["5m_pvt"])
        b["state"] = macd_state(b["macd"], b["signal"])
        bars.append(b)
    return bars


def load_15s(csv_path):
    """NSE_NIFTY1!, 15S_3c57c.csv - 45-col 15s export (positional blocks).

    Verified 2026-10-20: cols 28-29 = 15s MACD 24/52 (r=1.0 vs self-computed,
    every row); 30-31 = the 1m MACD (1m-close-aligned rows, r=1.0 vs 1m.csv);
    33 = 15s PVT (every row); 39-40/41 = a higher-TF block (not used).
    """
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader)]
        raw = [r for r in reader if r and r[0].strip()]

    def expect(idx, name):
        got = header[idx] if idx < len(header) else None
        if got != name:
            raise ValueError("unexpected 15s layout: col %d = %r, expected %r"
                             % (idx, got, name))

    for idx, name in ((5, "15s EMA9"), (6, "15s EMA21"), (27, "VWAP"),
                      (33, "PVT"), (41, "PVT")):
        expect(idx, name)

    bars = []
    for r in raw:
        if len(r) < len(header):
            r = r + [""] * (len(header) - len(r))
        ts = r[0].strip()
        b = {
            "date": ts[:10], "hhmm": ts[11:19] if len(ts) > 18 else "",
            "open": fv(r[1]), "high": fv(r[2]), "low": fv(r[3]), "close": fv(r[4]),
            "vwap": fv(r[27]),
            "pvt": fv(r[33]), "pvt_sign": None,
            "macd": fv(r[28]), "signal": fv(r[29]),
            "15s EMA9": fv(r[5]), "15s EMA21": fv(r[6]),
        }
        b["pvt_sign"] = sgn(b["pvt"])
        b["state"] = macd_state(b["macd"], b["signal"])
        bars.append(b)
    return bars


# ---------------------------------------------------------------------------
# verification (cross-file identity checks - fail loudly)
# ---------------------------------------------------------------------------

def corr(a, b):
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    n = len(pairs)
    if n < 100:
        return None, n
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    cov = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    vx = sum((p[0] - mx) ** 2 for p in pairs)
    vy = sum((p[1] - my) ** 2 for p in pairs)
    return (round(cov / ((vx * vy) ** 0.5), 4) if vx > 0 and vy > 0 else None), n


def ema_series(values, n_):
    alpha = 2.0 / (n_ + 1.0)
    out = [None] * len(values)
    e = None
    for i, v in enumerate(values):
        if v is None:
            out[i] = e
            continue
        e = v if e is None else alpha * v + (1 - alpha) * e
        out[i] = e
    return out


def check_macd_variants(bars, macd_key, label):
    """Correlate a CSV MACD pair against self-computed 12/26 and 24/52."""
    closes = [b["close"] for b in bars]
    col = [b.get(macd_key) for b in bars]
    res = {}
    for name, n1, n2 in (("12/26", 12, 26), ("24/52", 24, 52)):
        e1 = ema_series(closes, n1)
        e2 = ema_series(closes, n2)
        macd = [None if (x is None or y is None) else x - y for x, y in zip(e1, e2)]
        r, n = corr(macd[300:], col[300:])
        res[name] = {"r": r, "n": n}
    ok = res["24/52"]["r"] is not None and res["24/52"]["r"] > 0.99
    print("  [%s] %s vs 24/52: r=%s (n=%s) | vs 12/26: r=%s -> %s"
          % (label, macd_key, res["24/52"]["r"], res["24/52"]["n"],
             res["12/26"]["r"], "OK" if ok else "MISMATCH - DO NOT USE"))
    return ok


def run_checks(bars_1a, bars_1b, bars_15):
    print("== pm_ladder cross-file checks ==")
    ok = check_macd_variants(bars_1a, "macd", "1_20adf 1m pair2")
    ok &= check_macd_variants(bars_15, "macd", "15S 15s MACD (cols 28-29)")
    # 1_20adf PVT vs 1m.csv '1m PVT' on the overlap - signs only (the two exports
    # normalize PVT over different loaded histories; values are NOT comparable)
    key = {(b["date"], b["hhmm"]): b["pvt"] for b in bars_1b}
    a = [b for b in bars_1a if (b["date"], b["hhmm"]) in key and b["date"] >= "2026-08-03"]
    pairs = [(b["pvt"], key[(b["date"], b["hhmm"])])
             for b in a if b["pvt"] is not None and key[(b["date"], b["hhmm"])] is not None]
    agree = sum(1 for x, y in pairs if (x > 0) == (y > 0))
    pct = round(100.0 * agree / len(pairs), 3) if pairs else None
    print("  [1_20adf PVT vs 1m.csv 1m PVT] sign agreement: %s%% (n=%d) -> %s"
          % (pct, len(pairs), "OK" if pct is not None and pct > 99.0 else "CHECK"))
    return ok


# ---------------------------------------------------------------------------
# layer validation
# ---------------------------------------------------------------------------

def synth_30m(sess):
    """Synthetic 30m grid for rung touch/hold (windows stamped at their open;
    carried EMA = last completed value, no-repaint). Includes the EOD stub."""
    g, order = {}, []
    for b in sess:
        st = session_stamp(b["hhmm"], 30)
        k = (b["date"], st)
        if k not in g:
            g[k] = {"date": b["date"], "hhmm": st, "open": b["open"], "high": b["high"],
                    "low": b["low"], "close": b["close"], "_n": 0}
            order.append(k)
        w = g[k]
        if b["high"] is not None and w["high"] is not None:
            w["high"] = max(w["high"], b["high"])
        if b["low"] is not None and w["low"] is not None:
            w["low"] = min(w["low"], b["low"])
        if b["close"] is not None:
            w["close"] = b["close"]
        for k30 in ("30m_ema50", "30m_ema100", "30m_ema200"):
            v = b.get(k30)
            if v is not None:
                w[k30] = v
        w["_n"] += 1
    g30 = [g[k] for k in order if g[k]["_n"] >= 3]
    dmap = {(b["date"], b["hhmm"]): b for b in sess}
    return g30, dmap


def layer_D(bars5):
    """D PM layer - full 5m export window.

    Context: prev-day D PVT sign (carried) + ff D MACD state (prints 15:25 rows).
    Trigger = 30m PVT sign flip + ff 30m MACD state flip on the 5m row grid;
    state_lag=1 (MACD prints at :10/:40, PVT flips at :15/:45 - same completed
    window); relax = +/-3 30m bars (= 17 rows), dedupe 4 30m bars (= 24 rows).
    Rung anchor = 3E rungs tagged on the 5m rows within the last 24 rows (2h).
    Rung touch/hold on the synthetic 30m grid.
    """
    out = {"layer": "D", "claim": "D PM up -> rises from 3E50/100/200, 30m PM cross up"}

    ff_macd_state(bars5, "1d_macd", "1d_signal", "1d_state_ff")
    ff_macd_state(bars5, "30m_macd", "30m_signal", "30m_state_ff")
    for b in bars5:
        b["D_pm_up"] = (b.get("1d_pvt_sign") == 1 and b.get("1d_state_ff") is True)
        b["D_pm_dn"] = (b.get("1d_pvt_sign") == -1 and b.get("1d_state_ff") is False)
    sess = [b for b in bars5 if SESSION_FIRST <= b["hhmm"] <= SESSION_LAST]
    set_prevalence(out, sess, "D_pm_up", "D_pm_dn")

    g30, dmap = synth_30m(sess)
    for w in g30:
        b5 = dmap.get((w["date"], w["hhmm"]), {})
        w["D_pm_up"] = b5.get("D_pm_up", False)
        w["D_pm_dn"] = b5.get("D_pm_dn", False)

    cfg = {"sign_key": "30m_pvt_sign", "state_key": "30m_state_ff", "state_lag": 1,
           "relax_rows": RELAX_TF * 6 - 1, "dedupe_rows": DEDUPE_TF * 6,
           "anchor_z": 24, "rung_keys": ["30m_ema50", "30m_ema100", "30m_ema200"],
           "exit_ema_key": "5m_ema21", "horizons": HORIZONS["D"], "step_min": 5}
    for direction in ("up", "down"):
        ck = "D_pm_up" if direction == "up" else "D_pm_dn"
        out["rungs_%s" % direction] = touch_hold(
            [w for w in g30 if w[ck]],
            [("3E50", "30m_ema50"), ("3E100", "30m_ema100"), ("3E200", "30m_ema200")],
            direction)
        cfg["ctx_key"] = ck
        out["trigger_%s" % direction] = collect_layer_stats(day_runs(sess, 5), direction, cfg)
    return out


def layer_2_3(bars5, which, tags_1m=None):
    """2 PM layer (rungs 5E50/100/200) and 3 PM layer - both trigger on the
    NATIVE 5m PM cross (PVT sign flip + MACD_2452 state flip, same row strict).
    The 3-layer rung anchor = 1m tags (1E100/200) mapped to 5m stamps."""
    name = {"2": "2H", "3": "30m"}[which]
    out = {"layer": which,
           "claim": "%s PM up -> rises from %s with 5m PM cross up" % (
               which, "5E50/100/200" if which == "2" else "1E100/200 (1m tags)")}
    lname = name.lower()
    sign_key = "%s_pvt_sign" % lname
    ff_macd_state(bars5, "%s_macd" % lname, "%s_signal" % lname, "%s_state_ff" % lname)
    up_key, dn_key = "%s_pm_up" % which, "%s_pm_dn" % which
    for b in bars5:
        b[up_key] = (b.get(sign_key) == 1 and b.get("%s_state_ff" % lname) is True)
        b[dn_key] = (b.get(sign_key) == -1 and b.get("%s_state_ff" % lname) is False)
    sess = [b for b in bars5 if SESSION_FIRST <= b["hhmm"] <= SESSION_LAST]
    set_prevalence(out, sess, up_key, dn_key)
    for b in sess:
        b["5m_state"] = macd_state(b.get("5m_macd"), b.get("5m_signal"))

    cfg = {"sign_key": "5m_pvt_sign", "state_key": "5m_state", "state_lag": 0,
           "relax_rows": RELAX_TF, "dedupe_rows": DEDUPE_TF,
           "exit_ema_key": "5m_ema4", "horizons": HORIZONS[which], "step_min": 5}
    if which == "2":
        cfg["anchor_z"] = 12  # 1h of 5m rows
        cfg["rung_keys"] = ["5m_ema50", "5m_ema100", "5m_ema200"]
    else:
        cfg["anchor_z"] = 12
        cfg["rung_keys"] = None  # anchor comes from the 1m tag map
    for direction in ("up", "down"):
        ck = up_key if direction == "up" else dn_key
        if which == "2":
            out["rungs_%s" % direction] = touch_hold(
                [b for b in sess if b[ck]],
                [("5E50", "5m_ema50"), ("5E100", "5m_ema100"), ("5E200", "5m_ema200")],
                direction)
        cfg["ctx_key"] = ck
        if which == "3" and tags_1m is not None:
            for run in day_runs(sess, 5):
                for b in run:
                    b["_rung_tag"] = (b["date"], b["hhmm"]) in tags_1m
        out["trigger_%s" % direction] = collect_layer_stats(day_runs(sess, 5), direction, cfg)
    return out


def layer_3_rungs(bars5, bars_1a):
    """3-layer rung touch/hold (1E100/200) on the 1m grid (1_20adf window),
    context = the containing 5m row's 3-PM state. Also returns the set of
    (date, 5m stamp) where a 1m bar tagged a rung - the 3-layer anchor map."""
    out = {"layer": "3-rungs",
           "window": [bars_1a[0]["date"], bars_1a[-1]["date"]] if bars_1a else None,
           "note": "1m grid; 3-PM context mapped from the containing 5m row"}
    reg = {(b["date"], b["hhmm"]): b for b in bars5}
    joined = []
    tags = set()
    for b in bars_1a:
        if not (SESSION_FIRST <= b["hhmm"] <= SESSION_LAST):
            continue
        st5 = session_stamp(b["hhmm"], 5)
        b5 = reg.get((b["date"], st5))
        if b5 is None:
            continue
        tagged = False
        for e in ("1m EMA100", "1m EMA200"):
            lv = b.get(e)
            if lv is not None and b.get("low") is not None and b.get("high") is not None \
                    and b["low"] <= lv <= b["high"]:
                tagged = True
                break
        if tagged:
            tags.add((b["date"], st5))
        if b5.get("3_pm_up"):
            b["ctx"] = "up"
        elif b5.get("3_pm_dn"):
            b["ctx"] = "down"
        else:
            b["ctx"] = None
        joined.append(b)
    for direction in ("up", "down"):
        out["rungs_%s" % direction] = touch_hold(
            [b for b in joined if b["ctx"] == direction],
            [("1E100", "1m EMA100"), ("1E200", "1m EMA200")], direction)
    return out, tags


def layer_5(bars1b):
    """5 PM layer on the 1m grid (1m.csv window): context 5m PM state mapped
    from the 1m.csv 5m columns; rungs 1E9/21/50; trigger = native 1m PM cross."""
    out = {"layer": "5", "claim": "5 PM up -> rises from 1E9/21/50, 1m PM cross up",
           "window": [bars1b[0]["date"], bars1b[-1]["date"]] if bars1b else None}
    ff_macd_state(bars1b, "5m_macd", "5m_signal", "5m_state_ff")
    for b in bars1b:
        b["5_pm_up"] = (b.get("5m_pvt_sign") == 1 and b.get("5m_state_ff") is True)
        b["5_pm_dn"] = (b.get("5m_pvt_sign") == -1 and b.get("5m_state_ff") is False)
    sess = [b for b in bars1b if SESSION_FIRST <= b["hhmm"] <= SESSION_LAST]
    set_prevalence(out, sess, "5_pm_up", "5_pm_dn")

    cfg = {"sign_key": "pvt_sign", "state_key": "state", "state_lag": 0,
           "relax_rows": RELAX_TF, "dedupe_rows": DEDUPE_TF, "anchor_z": 15,
           "rung_keys": ["1m EMA9", "1m EMA21", "1m EMA50"],
           "exit_ema_key": "1m EMA9", "horizons": HORIZONS["5"], "step_min": 1}
    for direction in ("up", "down"):
        ck = "5_pm_up" if direction == "up" else "5_pm_dn"
        out["rungs_%s" % direction] = touch_hold(
            [b for b in sess if b[ck]],
            [("1E9", "1m EMA9"), ("1E21", "1m EMA21"), ("1E50", "1m EMA50")],
            direction)
        cfg["ctx_key"] = ck
        out["trigger_%s" % direction] = collect_layer_stats(day_runs(sess, 1), direction, cfg)
    return out


def layer_15s(bars15, bars1b):
    """Ultra tier: 15s PM cross trigger; two contexts reported - 15 PM (self)
    and 5 PM (mapped from 1m.csv). No rung anchor yet (rung set undefined).
    Tiny window - flagged."""
    out = {"layer": "15s", "claim": "Ultra: 15PM swing (15s PM cross up)",
           "window": [bars15[0]["date"], bars15[-1]["date"]] if bars15 else None,
           "caveat": "~2.5 sessions of 15S data - first cut only"}
    sess = [b for b in bars15 if SESSION_FIRST <= b["hhmm"][:5] <= SESSION_LAST]
    m5 = {}
    for b in bars1b:
        st = session_stamp(b["hhmm"], 5)
        m5[(b["date"], st)] = (b.get("5m_pvt_sign"), b.get("5m_state_ff"))
    for b in sess:
        s5 = m5.get((b["date"], session_stamp(b["hhmm"][:5], 5)), (None, None))
        b["5_pm_up"] = (s5[0] == 1 and s5[1] is True)
        b["15_pm_up"] = (b.get("pvt_sign") == 1 and b.get("state") is True)
        b["15_pm_dn"] = (b.get("pvt_sign") == -1 and b.get("state") is False)
    out["prevalence_15pm"] = {
        "up_pct": round(100.0 * sum(1 for b in sess if b["15_pm_up"]) / len(sess), 1) if sess else None,
        "down_pct": round(100.0 * sum(1 for b in sess if b["15_pm_dn"]) / len(sess), 1) if sess else None,
    }
    # day runs (15s stamps carry seconds - group by date, no contiguity check)
    runs, cur, cur_date = [], [], None
    for b in sess:
        if b["date"] != cur_date:
            if cur:
                runs.append(cur)
            cur, cur_date = [], b["date"]
        cur.append(b)
    if cur:
        runs.append(cur)

    cfg = {"sign_key": "pvt_sign", "state_key": "state", "state_lag": 0,
           "relax_rows": RELAX_TF, "dedupe_rows": DEDUPE_TF, "anchor_z": None,
           "rung_keys": None, "exit_ema_key": None,
           "horizons": HORIZONS["15s"], "step_min": 0.25}
    for direction in ("up", "down"):
        res = {}
        for cname, ck in (("context_15pm", "15_pm_up" if direction == "up" else "15_pm_dn"),
                          ("context_5pm", "5_pm_up")):
            cfg["ctx_key"] = ck
            res[cname] = collect_layer_stats(runs, direction, cfg)
        out["trigger_%s" % direction] = res
    return out


# ---------------------------------------------------------------------------
# report rendering
# ---------------------------------------------------------------------------

def fmt_sum(s):
    if not s or s.get("n", 0) == 0:
        return "n=0"
    bits = ["n=%d" % s["n"], "%s%% pos" % s["pos_pct"],
            "mean %+.1f" % s["mean_pts"], "med %+.1f" % s["median_pts"]]
    if s.get("n_eod_flat"):
        bits.append("eod=%d" % s["n_eod_flat"])
    for h in ("h5m", "h15m", "h30m", "h60m", "h120m"):
        if s.get(h):
            bits.append("%s %s%%/%+.1f" % (h[1:], s[h]["pos_pct"], s[h]["mean_pts"]))
    return " | ".join(bits)


def render_md(res):
    L = []
    L.append("# PM LADDER VALIDATION (user model, 2026-10-20)\n")
    L.append("Trigger = PVT zero-cross + MACD_2452 signal-cross, same direction. "
             "strict = same effective bar (carried 30m: MACD prints one row before "
             "the PVT flip - same completed window); relaxed = within +/-3 TRIGGER-TF "
             "bars, entry at the later cross, min gap 4 trigger-TF bars. "
             "**at_rungs** = a rung of the layer's zone tagged within the anchor "
             "window before the entry (the ladder's actual trade). Exit swing_end = "
             "trigger-TF PM pair breaks (PVT sign flips against OR MACD flips "
             "against), EOD flat 15:25; family exits (5m EMA21 / 5m EMA4 / 1m EMA9) "
             "as variants. pts = move in trend direction. Dev = up to %s, OOS = after.\n" % DEV_END)
    for key in ("D", "2", "3", "3_rungs", "5", "15s"):
        r = res[key]
        L.append("\n## Layer %s\n" % r["layer"])
        if r.get("claim"):
            L.append("%s\n" % r["claim"])
        if r.get("window"):
            L.append("Window: %s .. %s. %s" % (r["window"][0], r["window"][1],
                                               r.get("caveat", "")))
        if r.get("prevalence"):
            L.append("Context prevalence: up %s%% / down %s%% (of %s session bars)"
                     % (r["prevalence"]["up_pct"], r["prevalence"]["down_pct"],
                        r["prevalence"]["bars"]))
        if r.get("prevalence_15pm"):
            L.append("15-PM context prevalence: up %s%% / down %s%%"
                     % (r["prevalence_15pm"]["up_pct"], r["prevalence_15pm"]["down_pct"]))
        for direction in ("up", "down"):
            if ("rungs_%s" % direction) in r and r["rungs_%s" % direction]:
                L.append("\n### Rungs (%s)\n" % direction)
                L.append("| rung | touches | hold/reject % |")
                L.append("|---|---|---|")
                for nm, v in r["rungs_%s" % direction].items():
                    rate = v.get("hold_pct", v.get("reject_pct"))
                    L.append("| %s | %d | %s |" % (nm, v["touch"], rate))
            t = r.get("trigger_%s" % direction)
            if t:
                L.append("\n### Trigger (%s)\n" % direction)
                for variant, s in t.items():
                    if variant.endswith("_by_period") or variant.startswith("episodes"):
                        continue
                    if isinstance(s, dict) and "n" in s:
                        L.append("- **%s**: %s" % (variant, fmt_sum(s)))
                # nested contexts (15s layer only - variants named context_*)
                for variant, s in t.items():
                    if variant.startswith("context_") and isinstance(s, dict) and "n" not in s:
                        for v2, s2 in s.items():
                            if isinstance(s2, dict) and "n" in s2:
                                L.append("- **%s / %s**: %s" % (variant, v2, fmt_sum(s2)))
                for variant, s in t.items():
                    if variant.endswith("_by_period") and isinstance(s, dict):
                        for p, s2 in s.items():
                            L.append("  - %s: %s" % (variant.replace("_by_period", ""), fmt_sum(s2)))
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="PM ladder validation")
    ap.add_argument("--csv5", default=CSV5)
    ap.add_argument("--csv-1m-a", default=CSV1A)
    ap.add_argument("--csv-1m-b", default=CSV1B)
    ap.add_argument("--csv-15s", default=CSV15)
    ap.add_argument("--check", action="store_true", help="cross-file checks only")
    args = ap.parse_args(argv)

    for p in (args.csv5, args.csv_1m_a, args.csv_1m_b, args.csv_15s):
        if not os.path.exists(p):
            raise SystemExit("Missing CSV: %s" % p)

    print("Loading %s ..." % args.csv5)
    bars5 = load_bars(args.csv5)
    print("Loading %s ..." % args.csv_1m_a)
    bars_1a = load_1m_20adf(args.csv_1m_a)
    print("Loading %s ..." % args.csv_1m_b)
    bars_1b = load_1m_full(args.csv_1m_b)
    print("Loading %s ..." % args.csv_15s)
    bars_15 = load_15s(args.csv_15s)
    print("bars: 5m=%d 1m_20adf=%d 1m_full=%d 15s=%d"
          % (len(bars5), len(bars_1a), len(bars_1b), len(bars_15)))

    ok = run_checks(bars_1a, bars_1b, bars_15)
    if args.check:
        return
    if not ok:
        print("WARNING: a MACD-variant check did not confirm 24/52 - verify before trusting triggers")

    res = {}
    print("Layer D ...")
    res["D"] = layer_D(bars5)
    print("Layer 2 ...")
    res["2"] = layer_2_3(bars5, "2")
    print("Layer 3 (trigger full year; rungs on the 1m tier) ...")
    # the 3-layer needs its context flags first (layer_2_3 sets them), then the
    # 1m rung tags; run the context pass, the rung pass, then the trigger pass
    tmp = layer_2_3(bars5, "3")
    l3r, tags_1m = layer_3_rungs(bars5, bars_1a)
    res["3_rungs"] = l3r
    res["3"] = layer_2_3(bars5, "3", tags_1m=tags_1m)
    print("Layer 5 (1m tier) ...")
    res["5"] = layer_5(bars_1b)
    print("Layer 15s (ultra tier) ...")
    res["15s"] = layer_15s(bars_15, bars_1b)

    out = {"generated": datetime.now().isoformat(timespec="seconds"),
           "definition": __doc__.strip(), "layers": res}

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "pm_ladder.json"), "w") as f:
        json.dump(out, f, indent=1, default=str)
    md = render_md(res)
    with open(os.path.join(OUT_DIR, "pm_ladder.md"), "w") as f:
        f.write(md)
    print("\n%s" % md)
    print("Wrote %s and pm_ladder.json" % os.path.join(OUT_DIR, "pm_ladder.md"))


if __name__ == "__main__":
    main()
