"""2PMM (2PM Matrix) engine — builds the 3OX/5OX event matrix.

Spec (locked with the user, 2026-10-25 — see the Glossary §2PMM + handoff):

GRAIN  one row per 3OX-up event (30m VOC crossing <=0 -> >0 on a 5m close).
       A 5OX-up in the SAME 30m window fills the 5O block of that row (the
       nearest unpaired 5OX wins; max one 5O block per row). Additional
       5OX-ups get their own rows (3O block zero). A day with no event still
       gets one record (state as of 15:25, 3o=0, 5o=0). Records are ordered
       by their primary event time; record_no is 1-based within the day.

COLUMNS
    date, record_no,
    3o, 3o_time, 3o_mderun, 3o_slevels, 3o_rlevels, 3o_crossed,
    3o_synchers, 3o_run,
    gap,                                   (day-level: open vs prev close, pts)
    5o, 5o_time, 5o_mderun, 5o_slevels, 5o_rlevels, 5o_crossed,
    5o_synchers, 5o_run,
    3s, 3sz, 3sn, 3p, 3pn, 3m1, 3m2, 3ov, 3on,
                                           (30m state; 3ov = the carried 30m
                                           VOC, displayed as "3o")
    2s, 2sz, 2sn, 2p, 2pn, 2m1, 2m2, 2o, 2on,
    ds, dsz, dsn, dp, dpn, dm1, dm2, do, don

DEFINITIONS
    MDERun   MR 09:15-10:59 / DR 11:00-13:59 / ER 14:00-15:25 (event time).
    Levels   Run-derived (one run per event; see Runs). For a DOWN run:
             RLevels = the TRIGGER ZONE — the ladder rungs whose rejection
             started the fall: touched-and-rejected during the rise into it
             (high reaches the rung, candle closes below) AND within one
             event-candle-range of the rise's swing high (threshold = mean
             5m bar range of the 5 candles ending at the turn; several rungs
             can qualify), highest first. SLevels = the level that STOPPED
             the fall — the ladder rung bracketing the fall's last candle
             (low <= rung <= close there; the 5m MACD2 cross-up at that
             candle confirms the stop; '+' joins an exact confluence).
             Crossed = the rungs BELOW the trigger zone that price crossed
             down through during the fall (run low to zone bottom),
             excluding the SLevels names. UP runs mirror: SLevels = the
             support zone that triggered the rise (touched-and-rejected
             within one event-candle-range of the fall's swing low),
             RLevels = the rung holding the rise's last candle (5m MACD2
             cross-down confirms the stop), Crossed = zone-top to run high
             crossings. No completed run after the event -> all three blank.
             The ladder: 1E100, 1E200, 3E50, 3E100, 3E200, 5E50, 5E100,
             5E200, 2HE50, VWAP (at each 5m candle), Round100s, PDH/PDL/
             PDC/PDOpen (HA prev day), ORH/ORL (first 15 min raw), and the
             56 PSR series added 2026-10-26 (D/W/M/Q x
             Standard/HA x P,R1-R3,S1-S3; named D-P..Qh-S3). Pivots are
             NOT house-computed — TradingView's own plotted values are the
             pivot ladder.
             The export's 1m EMAs only populate from 2026-07-13 (TV's 1m
             history limit) — those rungs are simply absent before.
    Runs     One run per event, one signed `Run` column: positive = the move
             up (uprun), negative = the move down (downrun), blank only when
             unresolvable (no MACD2 cross after the event AND a flat close).
             Direction = the first 5m MACD2 cross after the event, else the
             net event-close -> session-close move. The confirming endpoint
             = the next cross in the same direction, else the session's last
             bar (an unconfirmed move runs to the close). Downrun measured
             from the rise's swing high to the stop level's price (Run =
             support - swing high, negative); uprun from the fall's swing
             low to the stop level's price (Run = resistance - swing low,
             positive). Signed, 1 decimal, no zero placeholders.
    Synchers cross events within +/-3 of the event on each one's own grid:
             5PX/5M1X/5M2X (+/-3 5m rows), 3PX/3M1X/3M2X (+/-3 30m buckets),
             2PX (+/-3 2H buckets), DPX (+/-3 sessions), 1PMX (+/-3 1m bars
             around the event bar's close minute; 1m data from 2026-08-03
             only), 5M1X = 12/26/9 computed from the 5m closes (no export
             carries it). Encoded "5PX-U" / "5PX-D".
    Ages     signed bucket counts, +n since an up-cross / -n since a down-
             cross, 0 = crossed in the current bucket, "" = no cross in
             history. MACD states print "X_n" (line>signal) / "Y_n" (below).
             Zones by K: OSZ/NOSZ/MZ/NOBZ/OBZ (20-pt bins). 2S/DS from the
             30m export (the 5m file carries no 2H/D Stoch); 3S from the 5m
             file. 2O/DO ages count 30m steps (their update cadence).

Data: the canonical 5m export (60c FINAL layout), the 30m export (58c, for
2S/DS), 1m.csv (for 1PMX; optional). Run:

    python -m forecasting.pmmEngine                 # full rebuild (seconds)
    python -m forecasting.pmmEngine --from 2026-01-01 --to 2026-10-23
    python -m forecasting.pmmEngine --out PATH

Writes: data/insight/csv/2pmm_matrix.csv  (+ console summary)
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from forecasting.store_builder import read_tv_csv, fnum, sgn  # noqa: E402

CSV5 = os.path.join("data", "insight", "csv", "NSE_NIFTY1!, 5_Sep 2025-Oct 2026.csv")
CSV30 = os.path.join("data", "insight", "csv", "NSE_NIFTY1!, 30_42a03.csv")
CSV1M = os.path.join("data", "insight", "csv", "NSE_NIFTY1!, 1m.csv")
OUT_DEFAULT = os.path.join("data", "insight", "csv", "2pmm_matrix.csv")

SESSION_START = "09:15"
EOD = "15:25"
PAIR_RADIUS = 3           # 5m bars each side = the same 30m window
SYNCH_BARS = 3

# 60-col 5m FINAL layout (0-indexed)
I = dict(open=1, high=2, low=3, close=4, e1_100=5, e1_200=6,
         e5_4=7, e5_9=8, e5_21=9, e5_50=10, e5_100=11, e5_200=12,
         e3_50=13, e3_100=14, e3_200=15, e2_50=16, e2_100=17, e2_200=18,
         ed_100=19, ed_200=20, vwap=21,
         m2_5=22, m2s_5=23, voc5=24, pvt5=25, k5=29, d5=30,
         voc3=31, k3=32, d3=33, m1_3=34, m1s_3=35, m2_3=36, m2s_3=37, pvt3=38,
         m1_2=42, m1s_2=43, m2_2=44, m2s_2=45, voc2=46, pvt2=47,
         m1_d=51, m1s_d=52, m2_d=53, m2s_d=54, vocd=55, pvtd=56)

HEADER = ["date", "record_no",
          "3o", "3o_time", "3o_mderun", "3o_slevels", "3o_rlevels", "3o_crossed",
          "3o_synchers", "3o_run",
          "gap",
          "5o", "5o_time", "5o_mderun", "5o_slevels", "5o_rlevels", "5o_crossed",
          "5o_synchers", "5o_run",
          "3s", "3sz", "3sn", "3p", "3pn", "3m1", "3m2", "3ov", "3on",
          "2s", "2sz", "2sn", "2p", "2pn", "2m1", "2m2", "2o", "2on",
          "ds", "dsz", "dsn", "dp", "dpn", "dm1", "dm2", "do", "don"]

EMPTY5 = {"o": 0, "_time": "", "_mderun": "", "_slevels": "", "_rlevels": "",
          "_crossed": "", "_synchers": "", "_run": ""}


def fail(msg):
    print(f"ABORT: {msg}")
    sys.exit(1)


def fnum2(v):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def f(v, nd=2):
    return "" if v is None or v == "" else f"{v:.{nd}f}"


def bucket_stamp(hhmm, step):
    if not hhmm:
        return None
    t = datetime.strptime(hhmm, "%H:%M")
    s = datetime.strptime(SESSION_START, "%H:%M")
    m = int((t - s).total_seconds() // 60)
    if m < 0:
        return None
    return (s + timedelta(minutes=(m // step) * step)).strftime("%H:%M")


def bkey(b, step):
    """Grid-bucket key: (date, stamp) so buckets never repeat across days."""
    if step == "d":
        return (b["date"],)
    st = bucket_stamp(b["hhmm"], step)
    return None if st is None else (b["date"], st)


def ema(vals, n):
    out, k, e = [], 2.0 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def zone(k):
    if k is None:
        return ""
    return ["OSZ", "NOSZ", "MZ", "NOBZ", "OBZ"][min(int(k // 20), 4)]


def mderun(hhmm):
    return "MR" if hhmm < "11:00" else ("DR" if hhmm < "14:00" else "ER")


def xyn(state, age):
    if state is None or age == "":
        return ""
    # Y already says "below zero" — the age carries no sign ("Y_5", not "Y_-5")
    return ("X" if state > 0 else "Y") + f"_{abs(age)}"


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------

def load_5m(path):
    """60-col FINAL-layout 5m export -> bar dicts with signs/states/crosses."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        r = csv.reader(fh)
        hdr = next(r)
        for i, name in ((5, "1m EMA100"), (7, "5m EMA4"), (21, "VWAP"),
                        (25, "PVT"), (38, "PVT"), (47, "PVT"), (56, "PVT")):
            if hdr[i] != name:
                fail(f"5m export layout changed: col {i} = {hdr[i]!r}, "
                     f"expected {name!r} (run a supervised session)")
        psr_off = None
        if len(hdr) >= 116:                       # 2026-10-26 PSR template
            for off, (col, _key, _name) in enumerate(TV_PSR):
                if hdr[60 + off] != col:
                    fail(f"5m export PSR layout changed: col {60 + off} = "
                         f"{hdr[60 + off]!r}, expected {col!r}")
            psr_off = 60
        elif len(hdr) != 60:
            fail(f"5m export: unexpected column count {len(hdr)}")
        raw = [row for row in r if row and row[0].strip()]

    bars = []
    for row in raw:
        b = {"date": row[0][:10], "hhmm": row[0][11:16]}
        for k, idx in I.items():
            b[k] = fnum2(row[idx])
        if psr_off is not None:
            for off, (_col, key, _name) in enumerate(TV_PSR):
                b[key] = (fnum2(row[psr_off + off])
                          if len(row) > psr_off + off else None)
        else:
            for _col, key, _name in TV_PSR:
                b[key] = None
        bars.append(b)

    # Forward-fill the sparse HTF study blocks — they print only on their own
    # update rows (30m Stoch/MACD ~13 rows/day; 2H VOC and the D MACD/VOC
    # block on theirs). PVTs, the 5m block and VWAP arrive dense — left raw.
    ff_keys = ["k3", "d3", "m1_3", "m1s_3", "m2_3", "m2s_3",
               "voc2", "m1_2", "m1s_2", "m2_2", "m2s_2",
               "vocd", "m1_d", "m1s_d", "m2_d", "m2s_d"]
    last = {k: None for k in ff_keys}
    for b in bars:
        for k in ff_keys:
            if b[k] is None:
                b[k] = last[k]
            else:
                last[k] = b[k]

    closes = [b["close"] for b in bars]
    e50 = ema(closes, 10)              # 1E50 proxy (1mE50 ~ 5mEMA10)
    m1l = ema(closes, 12)              # 5m M1 (12/26/9), computed
    m1s = ema(m1l, 9)
    # dense carried copy of the 30m VOC for DISPLAY only (3ov/3on state cells);
    # event detection keeps the raw sparse voc3 prints
    last3 = None
    for i, b in enumerate(bars):
        b["e50px"] = e50[i]
        b["m1_5"], b["m1s_5"] = m1l[i], m1s[i]
        if b["voc3"] is not None:
            last3 = b["voc3"]
        b["voc3d"] = last3
        b["vocs_3d"] = None if last3 is None else sgn(last3)
        for t in ("5", "3", "2", "d"):
            b[f"pvts_{t}"] = None if b[f"pvt{t}"] is None else sgn(b[f"pvt{t}"])
            b[f"vocs_{t}"] = None if b[f"voc{t}"] is None else sgn(b[f"voc{t}"])
        for k, (l_, s_) in {
                "m2_5": ("m2_5", "m2s_5"), "m1_5": ("m1_5", "m1s_5"),
                "m1_3": ("m1_3", "m1s_3"), "m2_3": ("m2_3", "m2s_3"),
                "m1_2": ("m1_2", "m1s_2"), "m2_2": ("m2_2", "m2s_2"),
                "m1_d": ("m1_d", "m1s_d"), "m2_d": ("m2_d", "m2s_d")}.items():
            ln, sg = b[l_], b[s_]
            b[f"{k}_st"] = None if (ln is None or sg is None) else (1 if ln > sg else -1)
        for k, (k_, d_) in {"k5": ("k5", "d5"), "k3": ("k3", "d3")}.items():
            a, s = b[k_], b[d_]
            b[f"{k}_st"] = None if (a is None or s is None) else (1 if a > s else -1)
    return bars


def load_30m_stoch(path):
    """30m export (58c) -> {(date, hhmm): (2S-K, 2S-D, DS-K, DS-D)} carried."""
    out = {}
    for r in read_tv_csv(path):
        out[(r["date"], r["hhmm"])] = (fnum(r, "K#2"), fnum(r, "D#2"),
                                       fnum(r, "K#3"), fnum(r, "D#3"))
    return out


def attach_htf_stoch(bars, st30):
    """Forward-fill 2S (K#2/D#2) and DS (K#3/D#3) onto the 5m bars: a 5m row
    uses the latest 30m-export stamp <= its own time (last completed value)."""
    by_date = {}
    for (d, hhmm), v in st30.items():
        by_date.setdefault(d, []).append((hhmm, v))
    for d in by_date:
        by_date[d].sort()
    last = None
    for b in bars:
        for hhmm, v in reversed(by_date.get(b["date"], [])):
            if hhmm <= b["hhmm"]:
                last = v
                break
        k2, d2, k3, d3 = last if last else (None, None, None, None)
        b["k2"], b["d2"], b["kd"], b["dd"] = k2, d2, k3, d3
        b["k2_st"] = None if (k2 is None or d2 is None) else (1 if k2 > d2 else -1)
        b["kd_st"] = None if (k3 is None or d3 is None) else (1 if k3 > d3 else -1)


def load_1pmx(path):
    """1m.csv -> {date: [(close_minute, 'U'|'D'), ...]} for 1PMX events
    (1m PVT sign AND 1m MACD(24/52) direction crossing the SAME direction on
    the same 1m close)."""
    if not os.path.exists(path):
        return {}
    out, prev_p, prev_m = {}, None, None
    for r in read_tv_csv(path):
        pvt = fnum(r, "1m PVT")
        ln, sg = fnum(r, "1m MACD Line"), fnum(r, "1m Signal Line")
        if pvt is None or ln is None or sg is None:
            prev_p = prev_m = None
            continue
        p, m = sgn(pvt), (1 if ln > sg else -1)
        if prev_p is not None and prev_m is not None:
            if p != prev_p and m != prev_m and p == m and p != 0:
                out.setdefault(r["date"], []).append(
                    (minute_int(r["hhmm"]) + 1, "U" if p > 0 else "D"))
        prev_p, prev_m = p, m
    return out


def minute_int(hhmm):
    t = datetime.strptime(hhmm, "%H:%M")
    return t.hour * 60 + t.minute


# ---------------------------------------------------------------------------
# ages on TF bucket grids
# ---------------------------------------------------------------------------

def bucket_ages(bars, state_key, step):
    """Per-bar signed age (in grid buckets) of a +/-1 carried state.
    The cross fires on the print row — the row where the carried state
    actually changes (30m studies print on :10/:40 rows inside the closing
    window's bucket; D studies on the day's first row). Age = grid buckets
    between the flip's containing bucket and now: +n since an up-cross,
    -n since a down-cross, 0 = crossed in the current bucket, '' = never."""
    ordi, seq = {}, []
    for b in bars:
        bk = bkey(b, step)
        seq.append(bk)
        if bk is not None and bk not in ordi:
            ordi[bk] = len(ordi)
    ages, flip, prev_st = [], None, None
    for b, bk in zip(bars, seq):
        st = b[state_key]
        if st is not None and prev_st is not None and st != prev_st:
            flip = (bk, st)
        if st is not None:
            prev_st = st
        if st is None or flip is None or bk is None:
            ages.append("")
        else:
            fb, fd = flip
            ages.append(fd * (ordi[bk] - ordi[fb]))
    return ages


def macd_cells(bars, key, step):
    """Per-bar 'X_n'/'Y_n' cells for a MACD pair (state + bucket age)."""
    ages = bucket_ages(bars, f"{key}_st", step)
    return [xyn(b[f"{key}_st"], a) for b, a in zip(bars, ages)]


# ---------------------------------------------------------------------------
# per-day level book
# ---------------------------------------------------------------------------

def ha_chain(daily):
    """daily: [(date, o, h, l, c)] -> HA chain (house formulas)."""
    out, po, pc = [], None, None
    for d, o, h, l, c in daily:
        hac = (o + h + l + c) / 4.0
        hao = (o + c) / 2.0 if po is None else (po + pc) / 2.0
        hah, hal = max(h, hao, hac), min(l, hao, hac)
        out.append((d, hao, hah, hal, hac))
        po, pc = hao, hac
    return out


def build_levels(bars_by_date, order):
    """-> {date: {'static': [(name, level)], 'gap': pts}}. PDH/PDL/PDC/PDOpen
    = the HA previous day; ORH/ORL from the first 15 minutes (raw).
    Pivots are NOT house-computed any more: the canonical carries TV's own
    56 PSR series (D-P…Qh-S3) as per-bar rungs (see TV_PSR/DYN_LV)."""
    daily = [(d, bs[0]["open"], max(b["high"] for b in bs),
              min(b["low"] for b in bs), bs[-1]["close"])
             for d, bs in ((d, bars_by_date[d]) for d in order)]
    ha = ha_chain(daily)
    ha_by = {x[0]: x[1:] for x in ha}

    out = {}
    for i, d in enumerate(order):
        lv = []
        if i > 0:
            pd = order[i - 1]
            po, ph, pl, pc = ha_by[pd]
            lv += [("PDH", ph), ("PDL", pl), ("PDC", pc), ("PDOpen", po)]
        bs = bars_by_date[d]
        first3 = [b for b in bs if b["hhmm"] <= "09:30"]
        if first3:
            lv.append(("ORH", max(b["high"] for b in first3)))
            lv.append(("ORL", min(b["low"] for b in first3)))
        out[d] = {"static": lv,
                  "gap": None if i == 0 else bs[0]["open"] - daily[i - 1][4]}
    return out


DYN_LV = [("1E100", "e1_100"), ("1E200", "e1_200"), ("3E50", "e3_50"),
          ("3E100", "e3_100"), ("3E200", "e3_200"), ("5E50", "e5_50"),
          ("5E100", "e5_100"), ("5E200", "e5_200"), ("2HE50", "e2_50"),
          ("VWAP", "vwap")]

# The 56 TV PSR series appended to the 5m canonical on 2026-10-26 (cols
# 60..115; the TV VWAPs at 116/117 stay unloaded — the house VWAP column is
# canonical). Ladder names TV<period>[h]-<level>; 'h' = Heikin Ashi style.
# Ladder names <period>[h]-<level> (D-P, Dh-P, … Q-S3): 'h' = Heikin Ashi
# style. No TV prefix on the Grid — these ARE TradingView's plotted values.
TV_PSR = []                                   # (canonical col, bar key, ladder name)
for _style, _tag in (("Standard", ""), ("Heikin Ashi", "h")):
    for _p in "DWMQ":
        for _lv in ("P", "R1", "R2", "R3", "S1", "S2", "S3"):
            TV_PSR.append((f"{_p} {_style} {_lv}",
                           f"tv{_p.lower()}{_tag}_{_lv.lower()}",
                           f"{_p}{_tag}-{_lv}"))
DYN_LV += [(name, key) for _, key, name in TV_PSR]


def bar_candidates(b, static_lv):
    """Return static and carried dynamic ladder values for one 5m bar."""
    out = list(static_lv)
    out.extend((name, b[key]) for name, key in DYN_LV
               if b[key] is not None)
    low, high = b["low"], b["high"]
    if low is not None and high is not None:
        first = int(low // 100) * 100
        last = int(high // 100) * 100
        out.extend(("R100", float(level))
                   for level in range(first, last + 1, 100))
    return out


def zone_threshold(bs, i):
    """Event-candle-range threshold: mean 5m bar range over the 5 candles
    ending at i (the turn candle of the leg)."""
    rng = [b["high"] - b["low"] for b in bs[max(1, i - 4):i + 1]
           if b["high"] is not None and b["low"] is not None]
    return sum(rng) / len(rng) if rng else 0.0


def trigger_zone(bs, start, end, static_lv, direction):
    """The rungs that TRIGGERED the leg: touched-and-rejected during
    start..end AND within one event-candle-range of the leg's extreme
    (several rungs can qualify). Down leg: rejections under the swing high
    (resistance zone, highest first). Up leg: rejections over the swing low
    (support zone, lowest first). -> ([(name, value)], extreme, zone_edge)."""
    rng = bs[start:end + 1]
    thr = zone_threshold(bs, end)
    if direction == "D":
        ext = max(b["high"] for b in rng if b["high"] is not None)
        cand = {}
        for b in rng:
            close, high = b["close"], b["high"]
            if close is None or high is None:
                continue
            for name, value in bar_candidates(b, static_lv):
                if close < value <= high and value > ext - thr:
                    cand[name] = max(cand.get(name, value), value)
        zone = sorted(cand.items(), key=lambda it: (-it[1], it[0]))
        return zone, ext, ext - thr
    ext = min(b["low"] for b in rng if b["low"] is not None)
    cand = {}
    for b in rng:
        close, low = b["close"], b["low"]
        if close is None or low is None:
            continue
        for name, value in bar_candidates(b, static_lv):
            if close > value >= low and value < ext + thr:
                cand[name] = min(cand.get(name, value), value)
    zone = sorted(cand.items(), key=lambda it: (it[1], it[0]))
    return zone, ext, ext + thr


def rejection_level(b, static_lv):
    """Mirror of support_level: nearest ladder resistance held on a turn
    candle (close <= rung <= high), plus its actual price."""
    close, high = b["close"], b["high"]
    if close is None or high is None:
        return "", None
    res = [(value, name) for name, value in bar_candidates(b, static_lv)
           if close <= value <= high]
    if not res:
        return "HIGH", high
    value = min(v for v, _ in res)
    names = sorted(name for v, name in res if v == value)
    return "+".join(names), value


def support_level(b, static_lv):
    """Nearest ladder support held on a turn candle, plus its actual price."""
    close, low = b["close"], b["low"]
    if close is None or low is None:
        return "", None
    supports = [(value, name) for name, value in bar_candidates(b, static_lv)
                if low <= value <= close]
    if not supports:
        return "LOW", low
    value = max(v for v, _ in supports)
    names = sorted(name for v, name in supports if v == value)
    return "+".join(names), value


def crossed_levels(bs, start, end, low_value, high_value, direction,
                   excluded, static_lv):
    """Distinct ladder names crossed between endpoints in the run direction."""
    crossed, seen = [], set(excluded)
    for i in range(max(start + 1, 1), end + 1):
        prev, cur = bs[i - 1], bs[i]
        if prev["close"] is None or cur["close"] is None:
            continue
        prev_values = dict(bar_candidates(prev, static_lv))
        for name, value in bar_candidates(cur, static_lv):
            if not low_value < value < high_value or name in seen:
                continue
            prev_value = prev_values.get(name, value)
            if direction == "D":
                hit = prev["close"] > prev_value and cur["close"] <= value
            else:
                hit = prev["close"] < prev_value and cur["close"] >= value
            if hit:
                seen.add(name)
                crossed.append(name)
    return ",".join(crossed)


def macd2_cycles(bs):
    """Completed 5m MACD2 up/down/up cycles, indexed locally."""
    flips, previous = [], None
    for i, b in enumerate(bs):
        state = b["m2_5_st"]
        if state is None:
            continue
        if previous is not None and state != previous:
            flips.append((i, "U" if state > previous else "D"))
        previous = state
    first_state = next((b["m2_5_st"] for b in bs
                        if b["m2_5_st"] is not None), None)
    starts = [idx for idx, direction in flips if direction == "U"]
    if first_state == 1:
        starts.insert(0, 0)
    cycles = []
    for start in starts:
        down = next((idx for idx, direction in flips
                     if idx > start and direction == "D"), None)
        if down is None:
            continue
        finish = next((idx for idx, direction in flips
                       if idx > down and direction == "U"), None)
        if finish is not None:
            cycles.append((start, down, finish))
    return cycles


def event_run_levels(bs, event_i, static_lv):
    """Return (SLevels, RLevels, Crossed, Run) — run-derived, Run signed
    (+ up / - down, blank when unresolvable).

    Direction = the first 5m MACD2 cross after the event; with no cross at
    all, the net event-close -> session-close move. The move's confirming
    endpoint = the next cross in the same direction, else the session's
    last bar (an unconfirmed move runs to the close — it still counts).
    Down run: RLevels = trigger zone (rejections within one event-candle-
    range of the rise's swing high, the rise starting at the previous
    up-cross or the session open); SLevels = the rung holding the last
    candle of the fall (MACD2 cross-up confirms); Crossed = rungs below the
    zone crossed down during the fall; Run = support - swing high (<= 0).
    Up run mirrored: SLevels = support zone at the swing low that triggered
    the rise; RLevels = the rung holding the rise's last candle (MACD2
    cross-down confirms); Crossed = zone top to run high; Run = resistance
    - swing low (>= 0)."""
    flips = []
    previous = None
    for idx, bar in enumerate(bs):
        state = bar["m2_5_st"]
        if state is None:
            continue
        if previous is not None and state != previous:
            flips.append((idx, "U" if state > previous else "D"))
        previous = state
    after = [(idx, d) for idx, d in flips if idx > event_i]
    last_i = len(bs) - 1
    if after:
        turn, direction = after[0]
    else:
        c0, cl = bs[event_i]["close"], bs[last_i]["close"]
        if c0 is None or cl is None or cl == c0:
            return "", "", "", ""
        direction = "U" if cl > c0 else "D"
        turn = event_i
    if direction == "D":
        leg_start = max((idx for idx, d in flips if d == "U" and idx < turn),
                        default=0)
        finish = next((idx for idx, d in after[1:] if d == "U"), last_i)
        zone, swing_high, zone_bottom = trigger_zone(bs, leg_start, turn,
                                                     static_lv, "D")
        rl = ",".join(name for name, _ in zone)
        sl, support = support_level(bs[finish], static_lv)
        if support is None:
            return sl, rl, "", ""
        run_low = min((b["low"] for b in bs[turn:finish + 1]
                       if b["low"] is not None), default=support)
        excluded = set(rl.split(",")) | set(sl.replace("+", ",").split(","))
        crossed = crossed_levels(bs, turn, finish, run_low, zone_bottom,
                                 "D", excluded, static_lv) if zone else ""
        return sl, rl, crossed, support - swing_high
    leg_start = max((idx for idx, d in flips if d == "D" and idx < turn),
                    default=0)
    finish = next((idx for idx, d in after[1:] if d == "D"), last_i)
    zone, swing_low, zone_top = trigger_zone(bs, leg_start, turn,
                                             static_lv, "U")
    sl = ",".join(name for name, _ in zone)
    rl, resistance = rejection_level(bs[finish], static_lv)
    if resistance is None:
        return sl, rl, "", ""
    run_high = max((b["high"] for b in bs[turn:finish + 1]
                    if b["high"] is not None), default=resistance)
    excluded = set(rl.split(",")) | set(sl.replace("+", ",").split(","))
    crossed = crossed_levels(bs, turn, finish, zone_top, run_high,
                             "U", excluded, static_lv) if zone else ""
    return sl, rl, crossed, resistance - swing_low


def flip_events(bars, state_key, step):
    """{global_idx: 'U'|'D'} — the grid's own state flips (not the carried
    state changes). `step` only tags the grid; the bucket-distance
    filtering happens in synchers_at."""
    evs, prev_st = {}, None
    for gi, b in enumerate(bars):
        st = b[state_key]
        if st is None:
            continue
        if prev_st is not None and st != prev_st:
            evs[gi] = "U" if st > prev_st else "D"
        prev_st = st
    return evs


def value_crosses(bars, key, up_only=False):
    """{global_idx: 'U'|'D'} — raw oscillator zero-crosses (<=0 -> >0 = U).
    up_only=True keeps just the up-crosses (the matrix records 3OX/5OX up)."""
    evs, prev = {}, None
    for gi, b in enumerate(bars):
        v = b[key]
        if v is None:
            continue
        if prev is not None:
            if prev <= 0 < v:
                evs[gi] = "U"
            elif not up_only and prev >= 0 > v:
                evs[gi] = "D"
        prev = v
    return evs


def make_ordinal(bars, step):
    """{bucket_key: ordinal} for a grid + the keys in bar order."""
    seq, seen = [], set()
    for b in bars:
        bk = bkey(b, step)
        if bk is not None and bk not in seen:
            seen.add(bk)
            seq.append(bk)
    return {bk: i for i, bk in enumerate(seq)}


def synchers_at(bars, g, ctx):
    """'5PX-U,3M2X-D,...' — the cross events synching with global bar g."""
    out = []
    for label, evs, kind, step in ctx["synch_specs"]:
        hit = None
        if kind == "row":
            for j, dd in evs.items():
                if abs(j - g) <= SYNCH_BARS:
                    hit = dd
                    break
        elif kind == "1pm":
            hit = evs.get(g)
        elif kind == "bucket":
            ordi = ctx["ordi"][step]
            bk = bkey(bars[g], step)
            if bk in ordi:
                my = ordi[bk]
                for j, dd in evs.items():
                    bj = bkey(bars[j], step)
                    if bj in ordi and abs(ordi[bj] - my) <= SYNCH_BARS:
                        hit = dd
                        break
        else:  # session grid
            dates = ctx["dates"]
            di = dates.index(bars[g]["date"])
            for j, dd in evs.items():
                if abs(dates.index(bars[j]["date"]) - di) <= SYNCH_BARS:
                    hit = dd
                    break
        if hit:
            out.append(f"{label}-{hit}")
    return ",".join(out)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def build(from_date=None, to_date=None, out_path=OUT_DEFAULT):
    bars = load_5m(CSV5)
    attach_htf_stoch(bars, load_30m_stoch(CSV30))
    ev_1pm = load_1pmx(CSV1M)

    for gi, b in enumerate(bars):
        b["_gi"] = gi
    bars_by_date, order = {}, []
    for b in bars:
        bars_by_date.setdefault(b["date"], []).append(b)
        if not order or order[-1] != b["date"]:
            order.append(b["date"])
    for d in order:
        for li, b in enumerate(bars_by_date[d]):
            b["_li"] = li
    lv_book = build_levels(bars_by_date, order)

    # the synch context: event maps + bucket ordinals
    ctx = {
        "dates": order,
        "ordi": {st: make_ordinal(bars, st) for st in (30, 120)},
        "synch_specs": [
            ("5PX", flip_events(bars, "pvts_5", "5m"), "row", None),
            ("5M1X", flip_events(bars, "m1_5_st", "5m"), "row", None),
            ("5M2X", flip_events(bars, "m2_5_st", "5m"), "row", None),
            ("3PX", flip_events(bars, "pvts_3", "30m"), "bucket", 30),
            ("3M1X", flip_events(bars, "m1_3_st", "30m"), "bucket", 30),
            ("3M2X", flip_events(bars, "m2_3_st", "30m"), "bucket", 30),
            ("2PX", flip_events(bars, "pvts_2", "2h"), "bucket", 120),
            ("DPX", flip_events(bars, "pvts_d", "d"), "session", None),
            ("1PMX", {}, "1pm", None),
        ],
    }
    # 1PMX: pre-match 1m events to 5m rows (event minute within +/-3 of the
    # 5m bar's close minute)
    ev1pm_rows = {}
    for gi, b in enumerate(bars):
        close_min = minute_int(b["hhmm"]) + 4
        for m, direction in ev_1pm.get(b["date"], []):
            if abs(m - close_min) <= SYNCH_BARS:
                ev1pm_rows[gi] = direction
                break
    ctx["synch_specs"][8] = ("1PMX", ev1pm_rows, "1pm", None)

    # age tables (global)
    a_k3 = bucket_ages(bars, "k3_st", 30)
    a_k2 = bucket_ages(bars, "k2_st", 120)
    a_kd = bucket_ages(bars, "kd_st", "d")
    a_p3 = bucket_ages(bars, "pvts_3", 30)
    a_p2 = bucket_ages(bars, "pvts_2", 120)
    a_pd = bucket_ages(bars, "pvts_d", "d")
    a_v2 = bucket_ages(bars, "vocs_2", 30)     # 2O updates per 30m step
    a_vd = bucket_ages(bars, "vocs_d", 30)     # DO updates per 30m step
    a_v3 = bucket_ages(bars, "vocs_3d", 30)    # 3O (display) per 30m step
    c_m1_3 = macd_cells(bars, "m1_3", 30)
    c_m2_3 = macd_cells(bars, "m2_3", 30)
    c_m1_2 = macd_cells(bars, "m1_2", 120)
    c_m2_2 = macd_cells(bars, "m2_2", 120)
    c_m1_d = macd_cells(bars, "m1_d", "d")
    c_m2_d = macd_cells(bars, "m2_d", "d")

    def state_row(b):
        g = b["_gi"]
        return {
            "3s": f(b["k3"], 1), "3sz": zone(b["k3"]), "3sn": str(a_k3[g]),
            "3p": f(b["pvt3"], 1), "3pn": str(a_p3[g]),
            "3m1": c_m1_3[g], "3m2": c_m2_3[g],
            "3ov": f(b["voc3d"], 2), "3on": str(a_v3[g]),
            "2s": f(b["k2"], 1), "2sz": zone(b["k2"]), "2sn": str(a_k2[g]),
            "2p": f(b["pvt2"], 1), "2pn": str(a_p2[g]),
            "2m1": c_m1_2[g], "2m2": c_m2_2[g],
            "2o": f(b["voc2"], 2), "2on": str(a_v2[g]),
            "ds": f(b["kd"], 1), "dsz": zone(b["kd"]), "dsn": str(a_kd[g]),
            "dp": f(b["pvtd"], 1), "dpn": str(a_pd[g]),
            "dm1": c_m1_d[g], "dm2": c_m2_d[g],
            "do": f(b["vocd"], 2), "don": str(a_vd[g]),
        }

    def event_blk(bs, i, prefix):
        b = bs[i]
        static = lv_book[b["date"]]["static"]
        sl, rl, cr, run = event_run_levels(bs, i, static)  # run-derived
        return {f"{prefix}o": 1, f"{prefix}o_time": b["hhmm"],
                f"{prefix}o_mderun": mderun(b["hhmm"]),
                f"{prefix}o_slevels": sl, f"{prefix}o_rlevels": rl,
                f"{prefix}o_crossed": cr,
                f"{prefix}o_synchers": synchers_at(bars, b["_gi"], ctx),
                f"{prefix}o_run": f(run, 1)}

    def zero_blk(prefix):
        return {f"{prefix}o": 0, f"{prefix}o_time": "", f"{prefix}o_mderun": "",
                f"{prefix}o_slevels": "", f"{prefix}o_rlevels": "",
                f"{prefix}o_crossed": "",
                f"{prefix}o_synchers": "", f"{prefix}o_run": ""}

    ev3 = value_crosses(bars, "voc3", up_only=True)
    ev5 = value_crosses(bars, "voc5", up_only=True)

    rows = []
    n3 = n5 = npair = noev = 0
    for d in order:
        if from_date and d < from_date:
            continue
        if to_date and d > to_date:
            continue
        bs = bars_by_date[d]
        evs3 = sorted(b["_li"] for b in bs if b["_gi"] in ev3)
        evs5 = sorted(b["_li"] for b in bs if b["_gi"] in ev5)
        used, day_recs = set(), []

        for i in evs3:                                   # one row per 3OX
            n3 += 1
            bk = bucket_stamp(bs[i]["hhmm"], 30)
            j5 = next((j for j in evs5 if j not in used
                       and bucket_stamp(bs[j]["hhmm"], 30) == bk), None)
            if j5 is not None:
                used.add(j5)
                npair += 1
            row = {"date": d, "record_no": None}
            row.update(event_blk(bs, i, "3"))
            row["gap"] = f(lv_book[d]["gap"], 1)
            row.update(event_blk(bs, j5, "5") if j5 is not None
                       else zero_blk("5"))
            day_recs.append((bs[i]["hhmm"], row))

        for j in evs5:                                   # unpaired 5OXs
            if j in used:
                continue
            n5 += 1
            row = {"date": d, "record_no": None}
            row.update(zero_blk("3"))
            row["gap"] = f(lv_book[d]["gap"], 1)
            row.update(event_blk(bs, j, "5"))
            day_recs.append((bs[j]["hhmm"], row))

        if not day_recs:                                 # no-event day
            noev += 1
            ieod = max((i for i, b in enumerate(bs) if b["hhmm"] <= EOD),
                       default=len(bs) - 1)
            row = {"date": d, "record_no": None}
            row.update(zero_blk("3"))
            row["gap"] = f(lv_book[d]["gap"], 1)
            row.update(zero_blk("5"))
            day_recs.append((bs[ieod]["hhmm"], row))

        day_recs.sort(key=lambda x: x[0])                # chronological
        for no, (_, row) in enumerate(day_recs, 1):
            i_state = _state_index(bs, row)
            row["record_no"] = no
            row.update(state_row(bs[i_state]))
            rows.append(row)

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)

    print(f"2PMM matrix -> {out_path}")
    print(f"  sessions {order[0]} .. {order[-1]} ({len(order)} days)")
    print(f"  3OX-up {n3} | unpaired 5OX-up {n5} | paired 5O blocks {npair}"
          f" | no-event days {noev} | records {len(rows)}")


def _state_index(bs, row):
    """The row whose state block a record reports: the 3OX bar if present,
    else the 5OX bar, else the EOD bar (no-event days)."""
    if row["3o"] == 1:
        return next(i for i, b in enumerate(bs) if b["hhmm"] == row["3o_time"])
    if row["5o"] == 1:
        return next(i for i, b in enumerate(bs) if b["hhmm"] == row["5o_time"])
    return max((i for i, b in enumerate(bs) if b["hhmm"] <= EOD),
               default=len(bs) - 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="2PMM matrix engine")
    ap.add_argument("--from", dest="from_date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="to_date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--out", default=OUT_DEFAULT)
    a = ap.parse_args(argv)
    build(a.from_date, a.to_date, a.out)


if __name__ == "__main__":
    main()
