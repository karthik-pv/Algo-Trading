#!/usr/bin/env python
"""TREPAT — Trend Price Action Tracking.

Reads TradingView bar exports (15s / 1m / 5m — 15s and 5m OPTIONAL) for
NSE:NIFTY1! and produces chronological tracking commentary per
tv/spatam/Tracking_spatam (the FINAL message catalog — wording is 1:1).

AUTO COLUMN NAMING: TV exports carry generic names (PVT, Plot, K, D, MACD
Line...) with no timeframe. This engine auto-classifies every generic column
by (a) change-timing matched against the TF-named EMA anchor columns, and
(b) value signature (bounded -1..+1 = VOC, 0-100 = Stoch, cumulative = PVT),
renames them canonically (<TF> PVT / VOC / MACD1 / MACD2 / Stoch K / Stoch D)
and prints the mapping to stderr. Whatever TFs the export contains, the
corresponding announcements light up; missing TFs are skipped with a note.

Conventions (per user / catalog):
- Level NAMES only in announcements — no NIFTY price / level values (gap % allowed).
- 1E9 / 1E21 excluded from S/R touch identification (1E21 keeps its dedicated
  comes-below / retrace events).
- Touches are WICK-aware (bar high/low range) and classified by the APPROACH
  side (dip-and-hold = support; rise-and-reject = resistance; straddle = cross).
- "Expect 1PXD/PXU_15PM" only when 5m PVT is downtrending/uptrending.
- Stochastic zones: OBZ / MZ / OSZ — no values. 5m (and 30m) TF only.
- Flagship (1PXD_15PM) needs 15s MACD2 — only in 15s exports.

Run-time tool + output — never commit the generated log.
"""
import os
import json
import re
import sys
import pandas as pd
import numpy as np

TODAY = pd.Timestamp('2026-11-06').date()
NEAR = 15.0        # points: considered "at" a level
LADDER_PCT = 0.5   # %: nearby-level lookahead window (catalog: .5%)
RETREAT = 25.0     # points: price must move this far away to re-arm a touch

GENERIC = {'MACD Line', 'Signal Line', 'Plot', 'PVT', 'K', 'D',
           'Cross Up', 'Cross Down'}


def base_name(col):
    return re.sub(r'\.\d+$', '', col)


def is_generic(col):
    return base_name(col) in GENERIC


def change_rows(s):
    """Rows (index labels) where the value actually changes (NaN-skipped)."""
    v = s[s.notna()]
    if not len(v):
        return set()
    return set(v.index[v.diff().ne(0)])


def auto_name_columns(df):
    """Classify + rename generic columns canonically. Returns (df, notes).

    TF anchor: the TF-named EMA50 columns (1m/5m/30m/2H/D EMA50). A generic
    column's TF = the most specific anchor whose change-rows cover the
    column's change-rows.
    """
    tfs = ['1m', '5m', '15m', '30m', '2H', 'D']
    anchors = {}
    for tf in tfs:
        col = f'{tf} EMA50'
        if col in df.columns:
            cr = change_rows(df[col])
            if cr:
                # ±1-row tolerance: some TF columns stamp ON/BEFORE the close
                # row, the EMA anchors fill on the row AFTER (or vice versa)
                anchors[tf] = cr | {i + 1 for i in cr} | {i - 1 for i in cr}
    # anchor ordering: most specific (smallest change-set) first
    anchor_order = sorted(anchors, key=lambda t: len(anchors[t]))

    def infer_tf(col_changes):
        cands = [tf for tf in anchor_order
                 if col_changes and len(col_changes & anchors[tf]) / len(col_changes) >= 0.9]
        return cands[0] if cands else None

    generic_idx = [i for i, c in enumerate(df.columns) if is_generic(c)]
    notes, rename = [], {}
    macd_panes = {}   # tf -> [(idx_line, idx_sig)]  (file order: M1 then M2)

    i = 0
    while i < len(generic_idx):
        c = df.columns[generic_idx[i]]
        b = base_name(c)
        tf = infer_tf(change_rows(df[c]))
        if b in ('K', 'D'):                      # stochastic pane: K, D  -> 'S'
            j = generic_idx[i + 1] if i + 1 < len(generic_idx) and base_name(df.columns[generic_idx[i + 1]]) == 'D' else None
            if tf and j is not None:
                rename[c] = f'{tf}S K'
                rename[df.columns[j]] = f'{tf}S D'
                notes.append(f"{tf}S (stoch): cols {c}/{df.columns[j]}")
                i += 2
                continue
            i += 1
        elif b == 'MACD Line':                   # MACD pane: Line + Signal
            sig = generic_idx[i + 1] if i + 1 < len(generic_idx) and base_name(df.columns[generic_idx[i + 1]]) == 'Signal Line' else None
            if tf:
                macd_panes.setdefault(tf, []).append((generic_idx[i], sig))
            i += 2 if sig is not None else 1
        elif b == 'PVT':                         # 'P' pane — the TF's PVT
            if tf:
                rename[c] = f'{tf}P PVT'
                notes.append(f"{tf}P (pvt): col {c}")
            i += 1
        elif b == 'Plot':
            # the Plot right after this TF's MACD pane(s) = the 'o' pane (VOC);
            # a later standalone Plot = zero line
            if tf:
                prev_macd = macd_panes.get(tf)
                has_voc = f'{tf}o Plot' in rename.values()
                if prev_macd and not has_voc:
                    rename[c] = f'{tf}o Plot'
                    notes.append(f"{tf}o (voc): col {c}")
                else:
                    notes.append(f"zero-line skipped: col {c}")
            i += 1
        else:                                    # Cross Up / Cross Down flags
            i += 1

    # MACD pane naming per TF (per SPATAM tree): one pane -> M2; two panes ->
    # M1 first, M2 second (file order)
    for tf, panes in macd_panes.items():
        if len(panes) == 1:
            il, isig, = panes[0]
            rename[df.columns[il]] = f'{tf}M2 MACD Line'
            if isig is not None: rename[df.columns[isig]] = f'{tf}M2 Signal Line'
            notes.append(f"{tf}M2: cols {df.columns[il]}/{df.columns[isig] if isig else '?'}")
        else:
            for tag, (il, isig) in zip(['M1', 'M2'], panes):
                rename[df.columns[il]] = f'{tf}{tag} MACD Line'
                if isig is not None: rename[df.columns[isig]] = f'{tf}{tag} Signal Line'
                notes.append(f"{tf}{tag}: cols {df.columns[il]}/{df.columns[isig] if isig else '?'}")

    out = df.rename(columns=rename)
    return out, notes


def load(path):
    raw = pd.read_csv(path, header=None, low_memory=False)
    # Descriptor row support: row 0 = per-column indicator names (e.g. '1P',
    # '5M2', '3o'), row 1 = the real TV header. Canonical name = '<desc> <hdr>'.
    has_desc = len(raw) > 1 and str(raw.iloc[0, 0]) in ('nan', 'None', 'None ', '') \
        and str(raw.iloc[1, 0]) == 'time'
    if has_desc:
        desc = ['' if pd.isna(v) else str(v).strip() for v in raw.iloc[0]]
        header = [str(v) for v in raw.iloc[1]]
        names = [f"{d} {h}" if d else h for d, h in zip(desc, header)]
        df = pd.DataFrame(raw.values[2:], columns=names)
    else:
        df = pd.DataFrame(raw.values[1:], columns=raw.iloc[0].astype(str).tolist())
    for pos in range(df.shape[1]):
        if df.columns[pos] != 'time':
            df.isetitem(pos, pd.to_numeric(df.iloc[:, pos], errors='coerce'))
    df['time'] = pd.to_datetime(df['time'])
    return df.reset_index(drop=True), has_desc


def pm_tag(pvt, m1=None, m2=None):
    """SPATAM family trend: UT / DT / PM Uptrend / PM Downtrend / PM2 ..."""
    if pvt is None or pd.isna(pvt):
        return None
    if m1 is None or pd.isna(m1) or m2 is None or pd.isna(m2):
        return 'UT' if pvt > 0 else 'DT'
    if np.sign(m1) == np.sign(m2) == np.sign(pvt):
        return 'PM Uptrend' if pvt > 0 else 'PM Downtrend'
    if np.sign(m2) == np.sign(pvt):
        return 'PM2 ' + ('UT' if pvt > 0 else 'DT')
    return 'UT' if pvt > 0 else 'DT'


def trend_word(tag):
    return 'Uptrend' if tag and ('UT' in tag or 'Uptrend' in tag) else 'Downtrend'


def main(paths):
    d1full, had_desc = load(paths['1m'])
    d15 = (load(paths['15s'])[0] if paths.get('15s') and os.path.exists(paths['15s']) else None)

    if had_desc:
        print("[map] file carries a descriptor row — using its column names as-is",
              file=sys.stderr)
    else:
        # try the saved TV-export template (scripts/tv_export_columns.json):
        # if the header layout matches, apply its canonical descriptor names
        tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'tv_export_columns.json')
        applied = False
        if os.path.exists(tpl_path):
            tpl = json.load(open(tpl_path))
            header = [str(v) for v in d1full.columns]
            strip = lambda c: re.sub(r'\.\d+$', '', c)   # dedup-suffix tolerant
            if [strip(c) for c in header] == [strip(c) for c in tpl['headers']]:
                new_names = [f"{d} {h}" if d else h
                             for h, d in zip(tpl['headers'], tpl['descriptors'])]
                d1full.columns = new_names
                applied = True
                print(f"[map] header matches saved template — applied "
                      f"{sum(1 for d in tpl['descriptors'] if d)} canonical names",
                      file=sys.stderr)
        if not applied:
            d1full, notes = auto_name_columns(d1full)
            for n in notes:
                print(f"[map] {n}", file=sys.stderr)

    d1 = d1full[d1full['time'].dt.date == TODAY].reset_index(drop=True)
    events = []  # (time, order, text)

    def ev(t, text):
        events.append((t, len(events), text))

    # ---------- previous close & gap ----------
    prev_days = d1full[d1full['time'].dt.date < TODAY]
    prev_close = prev_days[prev_days['time'].dt.date == prev_days['time'].dt.date.max()]['close'].iloc[-1]
    open_px = d1['open'].iloc[0]
    gap_pct = (open_px - prev_close) / prev_close * 100

    # ---------- static pivotal levels (names only) ----------
    row = d1[d1['D P'].notna()]
    lv = row.iloc[0] if len(row) else d1.iloc[0]
    levels = {}
    for c, name in [('D P', 'PD P'), ('D R1', 'R1 D'), ('D R2', 'R2 D'), ('D R3', 'R3 D'),
                    ('D S1', 'S1 D'), ('D S2', 'S2 D'), ('D S3', 'S3 D'),
                    ('W P', 'P W'), ('W R1', 'R1 W'), ('W R2', 'R2 W'), ('W R3', 'R3 W'),
                    ('W S1', 'S1 W'), ('W S2', 'S2 W'), ('W S3', 'S3 W'),
                    ('M P', 'P M'), ('M R1', 'R1 M'), ('M R2', 'R2 M'), ('M R3', 'R3 M'),
                    ('M S1', 'S1 M'), ('M S2', 'S2 M'), ('M S3', 'S3 M'),
                    ('Q P', 'P Q'), ('Q R1', 'R1 Q'), ('Q R2', 'R2 Q'), ('Q R3', 'R3 Q'),
                    ('Q S1', 'S1 Q'), ('Q S2', 'S2 Q'), ('Q S3', 'S3 Q'),
                    ('Y P', 'P Y'), ('Y R1', 'R1 Y'), ('Y R2', 'R2 Y'), ('Y R3', 'R3 Y'),
                    ('Y S1', 'S1 Y'), ('Y S2', 'S2 Y'), ('Y S3', 'S3 Y'),
                    ('Daily VWAP', 'Daily VWAP')]:
        v = lv.get(c)
        if pd.notna(v):
            if any(abs(float(v) - u) <= 5 for u in levels.values()):
                continue
            levels[name] = float(v)

    # ---------- canonical columns ----------
    # Canonical names: '<pane><TF> <plot>' — e.g. '1P PVT' (1m PVT),
    # '1o Plot' (1m VOC), '1M2 MACD Line', '5M1 MACD Line', '5S K', '3P PVT'.
    # Present either from the file's descriptor row or the auto-classifier.
    def col(name):
        return d1[name] if name in d1.columns else pd.Series(np.nan, index=d1.index)

    c1 = d1['close'].values
    lo1 = d1['low'].values
    hi1 = d1['high'].values
    op1 = d1['open'].values
    e21 = d1['1m EMA21'].values
    e50 = d1['1m EMA50'].values
    vwap1 = d1['Daily VWAP'].values
    pvt1 = col('1P PVT').values                 # 1m PVT (1P)
    m2_1 = col('1M2 MACD Line').values          # 1m MACD2 (1M2)
    sig_1 = col('1M2 Signal Line').values
    pvt5 = col('5P PVT')                        # 5m PVT (5P) — optional
    has_pvt5 = pvt5.notna().any()
    pvt5v = pvt5.values
    m15 = col('5M1 MACD Line').values           # 5m MACD1 (5M1)
    m25 = col('5M2 MACD Line').values           # 5m MACD2 (5M2)
    k5 = col('5S K').values
    dd5 = col('5S D').values

    # momentor families — pane-based names from the catalog:
    #   PX  = <TF> PVT zero-cross        (1PX 5PX 3PX 2PX DPX)
    #   MX  = <pane> MACD/Signal cross   (1M2X 5M1X 5M2X 3M2X 2M1X 2M2X DM1X DM2X)
    #   ox  = VOC cross-UP only          (1ox 5ox 3ox 2ox Dox — no UT/DT)
    macd_pairs = []                              # (momentor, line, signal)
    for pane in ['1M2', '5M1', '5M2', '3M2', '2M1', '2M2', 'DM1', 'DM2']:
        ln, sg = col(f'{pane} MACD Line'), col(f'{pane} Signal Line')
        if ln.notna().any() and sg.notna().any():
            macd_pairs.append((f'{pane}X', ln.values, sg.values))
    pvt_panes = []                               # (momentor, values)
    for nm, series in [('1PX', col('1P PVT')), ('5PX', col('5P PVT')),
                       ('3PX', col('3P PVT')), ('2PX', col('2P PVT')),
                       ('DPX', col('DP PVT'))]:
        if series.notna().any():
            pvt_panes.append((nm, series.values))
    voc_panes = []                               # (momentor, values) — cross-UP only
    for nm in ['1o', '5o', '3o', '2o', 'Do']:
        c = col(f'{nm} Plot')
        if c.notna().any():
            voc_panes.append((f'{nm}x', c.values))

    pvt30 = col('3P PVT')                       # 30m PVT (3P) — optional
    has_pvt30 = pvt30.notna().any()
    m230 = col('3M2 MACD Line').values          # 30m MACD2 (3M2)
    sig30 = col('3M2 Signal Line').values
    is5m = ~np.isnan(m25) if '5M2 MACD Line' in d1.columns else np.zeros(len(d1), bool)

    # full EMA grid as touch/call levels: every TF's EMA9/21/50/100/200 that the
    # export carries. 1E9 / 1E21 excluded (dedicated events only).
    TOUCH_EMAS = []
    for pfx, tf in [('1', '1m'), ('5', '5m'), ('30', '30m'), ('2', '2H'),
                    ('D', 'D'), ('W', 'W'), ('M', 'M')]:
        for n in [9, 21, 50, 100, 200]:
            cn = f'{tf} EMA{n}'
            nm = f'{pfx}E{n}'
            if cn in d1.columns and nm not in ('1E9', '1E21'):
                TOUCH_EMAS.append((cn, nm))

    # forward-filled 5m values: the stamps land one row before the 5m boundary,
    # so at boundary time the latest stamp is already the current reading
    m15f = d1['5M1 MACD Line'].ffill().values if '5M1 MACD Line' in d1.columns else m15
    m25f = d1['5M2 MACD Line'].ffill().values if '5M2 MACD Line' in d1.columns else m25
    # higher-TF panes for the 3P/2P may-cross note
    m31f = d1['3M1 MACD Line'].ffill().values if '3M1 MACD Line' in d1.columns else None
    m32f = d1['3M2 MACD Line'].ffill().values if '3M2 MACD Line' in d1.columns else None
    m21f = d1['2M1 MACD Line'].ffill().values if '2M1 MACD Line' in d1.columns else None
    m22f = d1['2M2 MACD Line'].ffill().values if '2M2 MACD Line' in d1.columns else None
    pvt2 = col('2P PVT')                        # 2h PVT (2P) — optional
    pvtD = col('DP PVT')                        # Daily PVT (DP) — optional
    mD1f = d1['DM1 MACD Line'].ffill().values if 'DM1 MACD Line' in d1.columns else None
    mD2f = d1['DM2 MACD Line'].ffill().values if 'DM2 MACD Line' in d1.columns else None
    t1 = d1['time']

    # HTF pane tags at time t — latest non-NaN stamp of the TF's PVT/MACD lines
    def htf_tag(pvt_series, m1f, m2f, t):
        if not pvt_series.notna().any():
            return None
        idx = d1.index[(d1['time'] <= t) & pvt_series.notna()]
        if not len(idx):
            return None
        j = idx[-1]
        return pm_tag(pvt_series.iloc[j], m1f[j], m2f[j])

    def tag30(t):
        return htf_tag(pvt30, m31f, m32f, t)

    def tag2h(t):
        return htf_tag(pvt2, m21f, m22f, t)

    def tagD(t):
        return htf_tag(pvtD, mD1f, mD2f, t)

    # may-cross mapping: each higher-TF PVT governs the next-lower-TF's
    # EMA50/100/200 cluster (per user): 3P->1E100/200, 2P->5E50/100/200,
    # DP->30E50/100/200, WP->2E50/100/200, MP->DE50/100/200.
    # WP / MP activate only when their PVT panes exist in the export.
    MAY_CROSS_MAP = [
        ('3P', ('1E100', '1E200'), tag30),
        ('2P', ('5E50', '5E100', '5E200'), tag2h),
        ('DP', ('30E50', '30E100', '30E200'), tagD),
        ('WP', ('2E50', '2E100', '2E200'), None),
        ('MP', ('DE50', 'DE100', 'DE200'), None),
    ]

    # ---------- 1m-level events ----------
    below21 = False
    above50 = False
    e21_res = e21_sup = e50_sup = e50_res = False
    pmx_state = 0   # 1PMX pair-alignment state: -1 down, +1 up, 0 = open of day
    pm_e50_said = False  # "Attention: 1PME50 UT/DT Started ..." once per episode
    ret_time = {}
    touches, away = {}, {}
    # price-action follow-through applies to the major EMA clusters:
    # 1E50/100/200, 5E50/100/200, 30E50/100/200, 2E50/100/200, DE50/100/200...
    MAJOR_EMAS = {f'{p}E{n}' for p, tf in [('1', '1m'), ('5', '5m'), ('30', '30m'),
                                           ('2', '2H'), ('D', 'D'), ('W', 'W'), ('M', 'M')]
                  for n in (50, 100, 200)}
    last_touch = {}                 # major-EMA name -> bar index of last touch tick
    pend_fire = {}                  # major-EMA name -> (is_res, armed_at_bar)
    for i in range(1, len(d1)):
        t = t1.iloc[i]
        px = c1[i]
        # retrace to 1E21 / 1E50 that holds (no Attention prefix per catalog)
        if below21 and pd.notna(e21[i]) and abs(px - e21[i]) <= NEAR and px < e21[i]:
            if not e21_res:
                e21_res = True
                ret_time['1E21'] = t
                ev(t, "Price has retraced up to and faced resistance at 1E21..")
        elif pd.notna(e21[i]) and e21[i] - px > RETREAT:
            e21_res = False
        if (not below21) and pd.notna(e21[i]) and abs(px - e21[i]) <= NEAR and px > e21[i]:
            if not e21_sup:
                e21_sup = True
                ret_time['1E21'] = t
                ev(t, "Price has retraced down to and taken support of 1E21..")
        elif pd.notna(e21[i]) and px - e21[i] > RETREAT:
            e21_sup = False
        # 1E21 crossings (state only — feeds the 1E21 retrace lines)
        if not below21 and pd.notna(e21[i-1]) and c1[i-1] > e21[i-1] and px < e21[i]:
            below21 = True
        elif below21 and pd.notna(e21[i-1]) and c1[i-1] < e21[i-1] and px > e21[i]:
            below21 = False
        # 1PMX swing start — stateful pair alignment (2026-11-10 rule):
        # Down state = 1m PVT < 0 AND MACD2 below signal; Up state = PVT > 0
        # AND MACD2 above signal. Announcement fires on the FIRST bar the pair
        # holds while the opposite state was in force (same bar or a few bars
        # apart — no same-bar cross requirement, no DT-over-UT precedence).
        pvt_up_now = pd.notna(pvt1[i]) and pvt1[i] > 0
        pvt_dn_now = pd.notna(pvt1[i]) and pvt1[i] < 0
        m2_up_now = (pd.notna(sig_1[i]) and pd.notna(m2_1[i]) and m2_1[i] > sig_1[i])
        m2_dn_now = (pd.notna(sig_1[i]) and pd.notna(m2_1[i]) and m2_1[i] < sig_1[i])
        down_now = pvt_dn_now and m2_dn_now
        up_now = pvt_up_now and m2_up_now
        if down_now:
            if pmx_state != -1:
                lvl = getattr(main, '_last_res', None)
                ev(t, "1PMX DT" + (f" Resistance @ {lvl}" if lvl else " Resistance"))
                pm_e50_said = False
            pmx_state = -1
        elif up_now:
            if pmx_state != 1:
                lvl = getattr(main, '_last_sup', None)
                ev(t, "1PMX UT" + (f" Support @ {lvl}" if lvl else " Support"))
                pm_e50_said = False
            pmx_state = 1
        # 1E50 cross — Attention prefix per catalog; only said when 1m PVT
        # agrees with the cross (above-cross needs 1P > 0, below-cross 1P < 0)
        if pd.notna(e50[i-1]) and c1[i-1] > e50[i-1] and px < e50[i]:
            above50 = False
            if pd.notna(pvt1[i]) and pvt1[i] < 0:
                ev(t, "Price has crossed below 1E50..")
            if pmx_state == -1 and not pm_e50_said:
                pm_e50_said = True
                lvl = getattr(main, '_last_res', None)
                ev(t, "Attention: 1PME50 DT Started Resistance" + (f" @ {lvl}" if lvl else ""))
        elif pd.notna(e50[i-1]) and c1[i-1] < e50[i-1] and px > e50[i]:
            above50 = True
            if pd.notna(pvt1[i]) and pvt1[i] > 0:
                ev(t, "Price has crossed above 1E50..")
            if pmx_state == 1 and not pm_e50_said:
                pm_e50_said = True
                lvl = getattr(main, '_last_sup', None)
                ev(t, "Attention: 1PME50 UT Started Support" + (f" @ {lvl}" if lvl else ""))
        # 1E50 retrace-that-holds
        if above50 and pd.notna(e50[i]) and abs(px - e50[i]) <= NEAR and px > e50[i]:
            if not e50_sup:
                e50_sup = True
                ret_time['1E50'] = t
                ev(t, "Price has retraced down to and taken support of 1E50..")
        elif pd.notna(e50[i]) and px - e50[i] > RETREAT:
            e50_sup = False
        if (not above50) and pd.notna(e50[i]) and abs(px - e50[i]) <= NEAR and px < e50[i]:
            if not e50_res:
                e50_res = True
                ret_time['1E50'] = t
                ev(t, "Price has retraced up to and faced resistance at 1E50..")
        elif pd.notna(e50[i]) and e50[i] - px > RETREAT:
            e50_res = False
        # 1m candle VWAP cross
        if pd.notna(vwap1[i-1]) and c1[i-1] > vwap1[i-1] and px < vwap1[i]:
            ev(t, "Price crossed down VWAP (1m)..")
        elif pd.notna(vwap1[i-1]) and c1[i-1] < vwap1[i-1] and px > vwap1[i]:
            ev(t, "Price crossed up VWAP (1m)..")
        # 5PZ — 5m PVT about to cross (pre-call), +/-0.1 band, once per episode
        if has_pvt5:
            v5 = pvt5v[i]
            m2_fall = pd.notna(m2_1[i-1]) and m2_1[i] < m2_1[i-1]
            m2_rise = pd.notna(m2_1[i-1]) and m2_1[i] > m2_1[i-1]
            if pd.notna(v5) and 0 < v5 <= 0.1 and pvt1[i] < 0 and m2_fall \
                    and not getattr(main, '_pzc_dn', False):
                main._pzc_dn = True
                main._pzc_up = False
                ev(t, "5PZ DT")
            elif pd.notna(v5) and -0.1 <= v5 < 0 and pvt1[i] > 0 and m2_rise \
                    and not getattr(main, '_pzc_up', False):
                main._pzc_up = True
                main._pzc_dn = False
                ev(t, "5PZ UT")
            elif pd.notna(v5) and abs(v5) > 0.1:
                main._pzc_dn = False
                main._pzc_up = False
        # ---------- momentor crosses (every 1m row, latest values) ----------
        # PX: <TF> PVT zero-cross — value updates on consecutive rows
        for nm, pv in pvt_panes:
            if i > 0 and pd.notna(pv[i - 1]) and pd.notna(pv[i]):
                if pv[i - 1] > 0 >= pv[i]:
                    ev(t, f"{nm} DT")
                elif pv[i - 1] < 0 <= pv[i]:
                    ev(t, f"{nm} UT")
        # MX: <pane> MACD Line vs its own Signal Line; HTF panes stamp on
        # boundary rows, so compare with the previous non-NaN row
        for nm, ln, sg in macd_pairs:
            j = i - 1
            while j >= 0 and (np.isnan(ln[j]) or np.isnan(sg[j])):
                j -= 1
            if j >= 0:
                if ln[j] > sg[j] and ln[i] <= sg[i]:
                    ev(t, f"{nm} DT")
                elif ln[j] < sg[j] and ln[i] >= sg[i]:
                    ev(t, f"{nm} UT")
        # ox: VOC cross-UP only (no UT/DT, cross-downs not announced)
        for nm, vv in voc_panes:
            if i > 0 and pd.notna(vv[i - 1]) and pd.notna(vv[i]):
                if vv[i - 1] <= 0 < vv[i]:
                    ev(t, f"{nm}")

        # price-action follow-through on major EMAs — "rises above" / "falls
        # below": an AGGRESSIVE candle (body >= 1.5x avg of the previous 5)
        # closing beyond a level that was just ANNOUNCED as touched/retested.
        # One fire per announced episode, next bar onward, 15-bar validity.
        for nm, (is_res, ib) in list(pend_fire.items()):
            if i <= ib or i - ib > 15:
                if i - ib > 15:
                    del pend_fire[nm]
                continue
            if nm not in cur_levels:
                continue
            val = cur_levels[nm]
            body = abs(px - op1[i])
            avg5 = np.nanmean(np.abs(c1[max(0, i - 5):i] - op1[max(0, i - 5):i]))
            if not np.isfinite(avg5) or avg5 == 0:
                continue
            if (not is_res) and px > val and px > op1[i] and body >= 1.5 * avg5:
                ev(t, f"Price rises above {nm}")
                del pend_fire[nm]
            elif is_res and px < val and px < op1[i] and body >= 1.5 * avg5:
                ev(t, f"Price falls below {nm}")
                del pend_fire[nm]

        # ---------- level touches (bar RANGE, wick-aware, approach-side) ----------
        lo_px, hi_px = lo1[i], hi1[i]
        cur_levels = dict(levels)
        # 1E9 / 1E21 excluded from S/R identification (per user) — 1E21 keeps
        # its dedicated events (comes below 1E21 / retrace lines) only.
        for cn, nm in TOUCH_EMAS:
            v = d1[cn].iloc[i] if cn in d1.columns else np.nan
            if pd.notna(v):
                cur_levels[nm] = float(v)
        tick_res, tick_sup = [], []
        cont_res, cont_sup = [], []

        for name, val in cur_levels.items():
            in_band = (lo_px - val) <= NEAR and (hi_px - val) >= -NEAR  # range overlaps band
            if in_band:
                if not away.get(name, True):
                    (cont_res if px < val else cont_sup).append(name)
                    continue
                first = name not in touches
                touches[name] = touches.get(name, 0) + 1
                away[name] = False
                if name in MAJOR_EMAS:
                    last_touch[name] = i
                if name in ('1E21', '1E50') and ret_time.get(name) \
                        and (t - ret_time[name]).total_seconds() < 120:
                    continue
                again = (not first) and touches[name] >= 3
                # Support vs resistance by the APPROACH side, not the close:
                # bar dipped into the level and closed above it -> SUPPORT;
                # bar rose into it and closed below -> RESISTANCE. A bar
                # straddling the level (open/close on opposite sides) is a
                # cross, not a touch — skip.
                if op1[i] >= val and px >= val:
                    tick_sup.append((name, first, again))
                elif op1[i] <= val and px <= val:
                    tick_res.append((name, first, again))
            elif min(abs(lo_px - val), abs(hi_px - val)) > RETREAT and name in touches:
                away[name] = True
        fresh_res, fresh_sup = len(tick_res), len(tick_sup)
        if fresh_res:
            tick_res += [(n, False, False) for n in cont_res if n not in [x[0] for x in tick_res]]
        if fresh_sup:
            tick_sup += [(n, False, False) for n in cont_sup if n not in [x[0] for x in tick_sup]]

        def group(names):
            return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]

        def emit_group(items, is_res):
            if not items:
                return
            names = [n for n, _, _ in items]
            any_first = any(f for _, f, _ in items)
            any_again = any(a for _, _, a in items)
            # remember the acting level for the 1PMX swing-start lines
            if any_first:
                setattr(main, '_last_res' if is_res else '_last_sup',
                        ', '.join(names))
            # arm the price-action follow-through for announced major EMAs
            for nm in names:
                if nm in MAJOR_EMAS:
                    pend_fire[nm] = (is_res, i)
            if is_res:
                base = (f"Price facing resistance at {group(names)}.." if any_first
                        else f"Price retested the level {group(names)}"
                             + (" again.." if any_again else ".."))
            else:
                base = (f"Price takes support of {group(names)}.." if any_first
                        else f"Price retested the level {group(names)}"
                             + (" again.." if any_again else ".."))
            # may-cross note — each PVT pane only speaks for ITS mapped EMA
            # cluster (MAY_CROSS_MAP): 3P for 1E100/200, 2P for 5E50/100/200,
            # DP for 30E50/100/200, WP for 2E50/100/200, MP for DE50/100/200.
            may = ""
            if any_first:
                res_panes = [p for p, lvls, tg in MAY_CROSS_MAP
                             if tg and trend_word(tg(t)) == 'Uptrend'
                             and any(n in lvls for n in names)]
                sup_panes = [p for p, lvls, tg in MAY_CROSS_MAP
                             if tg and trend_word(tg(t)) == 'Downtrend'
                             and any(n in lvls for n in names)]
                if is_res and res_panes:
                    may = (f" May cross up since {'/'.join(res_panes)} "
                           + ("are" if len(res_panes) > 1 else "is") + " uptrending..")
                elif not is_res and sup_panes:
                    may = (f" May cross down since {'/'.join(sup_panes)} "
                           + ("are" if len(sup_panes) > 1 else "is") + " downtrending..")
            ev(t, base + may)
            # Expect pre-call: only when 1m PVT is trending INTO the level —
            # swing Down only if 1m PVT is uptrending (facing resistance),
            # swing Up only if 1m PVT is downtrending (facing support) — AND
            # 1m PVT must be within the +/-0.1 zero-line threshold (about to
            # cross); outside that band the Expect call is not said.
            gate = (has_pvt5 and pd.notna(pvt1[i]) and abs(pvt1[i]) <= 0.1
                    and (pvt1[i] > 0 if is_res else pvt1[i] < 0))
            expect = "Expect 1PX_15PM DT" if is_res else "Expect 1PX_15PM UT"
            if gate:
                ev(t, expect)

        emit_group(tick_res, True)
        emit_group(tick_sup, False)

        # ---------- 5PM trend start / bias: every 1m row, latest 5m values ----------
        if has_pvt5 and pd.notna(pvt5v[i]) and pd.notna(m15f[i]) and pd.notna(m25f[i]):
            tag = pm_tag(pvt5v[i], m15f[i], m25f[i])
            ptag = getattr(main, '_tag5', None)
            above_vwap = pd.notna(vwap1[i]) and c1[i] > vwap1[i]
            below_vwap = pd.notna(vwap1[i]) and c1[i] < vwap1[i]
            if tag and 'PM Uptrend' in str(tag) and 'PM Uptrend' not in str(ptag):
                pfx = "" if (pd.notna(vwap1[i-1]) and c1[i-1] < vwap1[i-1] and px > vwap1[i]) else ""
                ev(t, pfx + "5PM Uptrend started..")
                ev(t, "Bias is " + ("Strong Uptrend" if above_vwap else "Uptrend") +
                    " as we have 5PM Uptrend" + (" above VWAP" if above_vwap else ""))
            if tag and 'PM Downtrend' in str(tag) and 'PM Downtrend' not in str(ptag):
                pfx = "" if (pd.notna(vwap1[i-1]) and c1[i-1] > vwap1[i-1] and px < vwap1[i]) else ""
                ev(t, pfx + "5PM Downtrend started..")
                ev(t, "Bias is " + ("Strong Downtrend" if below_vwap else "Downtrend") +
                    " as we have 5PM Downtrend" + (" below VWAP" if below_vwap else ""))
            main._tag5 = tag

        # ---------- 5m rows: stoch crosses (stamp-row events) ----------
        if is5m[i] and i > 0:
            jp = next((k for k in range(i - 1, -1, -1) if is5m[k]), None)
            if jp is not None:
                if pd.notna(k5[jp]) and pd.notna(dd5[jp]) and pd.notna(k5[i]) and pd.notna(dd5[i]):
                    if k5[jp] > dd5[jp] and k5[i] <= dd5[i]:
                        zone = 'OBZ' if k5[jp] >= 80 else 'OSZ' if k5[jp] <= 20 else 'MZ'
                        ev(t, f"5m Stochastic cross down from {zone}..")
                    elif k5[jp] < dd5[jp] and k5[i] >= dd5[i]:
                        zone = 'OSZ' if k5[jp] <= 20 else 'OBZ' if k5[jp] >= 80 else 'MZ'
                        ev(t, f"5m Stochastic cross up from {zone}..")

    events.sort(key=lambda x: (x[0], x[1]))

    # ---------- opening call ----------
    # levels = static pivot ladder + Daily VWAP + ALL dynamic EMAs (excluding
    # 1E9/1E21) as of the first bar — the EMAs hug the open and must be announced.
    open_row = d1.iloc[0]
    open_levels = dict(levels)
    for cn, nm in TOUCH_EMAS:
        v = open_row.get(cn)
        if pd.notna(v):
            if any(abs(float(v) - u) <= 5 for u in open_levels.values()):
                continue
            open_levels[nm] = float(v)
    above = [n for n, v in open_levels.items() if 0 < v - open_px <= open_px * LADDER_PCT / 100]
    below = [n for n, v in open_levels.items() if 0 < open_px - v <= open_px * LADDER_PCT / 100]
    gap_word = 'Gap up' if gap_pct >= 0 else 'Gap down'
    ev(d1['time'].iloc[0],
       f"Session open. {gap_word} {gap_pct:+.2f}% from previous close.")
    ev(d1['time'].iloc[0],
       f"Resistance within .5% above: {', '.join(above) or 'none'}")
    ev(d1['time'].iloc[0],
       f"Support within .5% below: {', '.join(below) or 'none'}")
    # session-open VOC status (whatever 'o' panes the export contains)
    vocs = []
    for pfx in ['1o', '5o', '3o', '2o', 'Do']:
        c = f'{pfx} Plot'
        if c in d1.columns and pd.notna(d1[c].iloc[0]) and d1[c].iloc[0] > 0:
            vocs.append(pfx)
    ev(d1['time'].iloc[0],
       "VOC at open: " + (", ".join(vocs) or "none above 0"))
    if d15 is None:
        ev(d1['time'].iloc[0],
           "(15s data not provided — flagship 1PXD/PXU_15PM confirmation and 15s VWAP crosses unavailable)")
    if not has_pvt5:
        ev(d1['time'].iloc[0],
           "(5m PVT not in this export — 5PX / 5PM / Bias / Expect pre-calls unavailable)")
    events.sort(key=lambda x: (x[0], x[1]))

    events.sort(key=lambda x: (x[0], x[1]))
    print('\n'.join(f"{t.strftime('%H:%M:%S')}  {text}" for t, _, text in events))

    # ---------- embed the same messages into the TREPAT strip Pine file ----------
    pine_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'tv', 'spatam', 'trepat.pine')
    by_min = {}
    for t, _, text in events:
        by_min.setdefault(t.strftime('%Y%m%d%H%M'), []).append(text)
    chunks = []
    for k in sorted(by_min):
        esc = '\\n'.join(s.replace('\\', '\\\\').replace('"', '\\"') for s in by_min[k])
        chunks.append(f'"{k}|{esc}~"')
    by_sec = {}
    for t, _, text in events:
        by_sec.setdefault(t.strftime('%Y%m%d%H%M%S'), []).append(text)
    for k in sorted(by_sec):
        esc = '\\n'.join(s.replace('\\', '\\\\').replace('"', '\\"') for s in by_sec[k])
        chunks.append(f'"{k}|{esc}~"')
    data_line = ' +\n  '.join(chunks) if chunks else '""'
    block = (f'// === DATA BEGIN (auto-generated by scripts/trepat_track.py — do not edit) ===\n'
             f'var string TREPAT_DATA = {data_line}\n'
             f'// === DATA END ===')
    try:
        src = open(pine_path, encoding='utf-8').read()
        src = re.sub(r'// === DATA BEGIN.*?// === DATA END ===',
                     lambda m: block, src, flags=re.S)
        open(pine_path, 'w', encoding='utf-8').write(src)
        print(f"[strip] trepat.pine updated with {len(by_min)} minute cells — "
              f"paste it into the TradingView Pine editor", file=sys.stderr)
    except OSError as e:
        print(f"[strip] could not update {pine_path}: {e}", file=sys.stderr)
    return events


if __name__ == '__main__':
    dl = os.path.expanduser('~/Downloads')
    main({
        '1m': f"{dl}/NSE_NIFTY1!, 1_3c909.csv",
        '15s': None,
        '5m': None,
    })
