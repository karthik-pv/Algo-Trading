#!/usr/bin/env python
"""TREPAT — Trend Price Action Tracking.

Reads TradingView bar exports (15s / 1m / 5m) for NSE:NIFTY1! and produces a
chronological tracking commentary per tv/SPATAM/Tracking templates.

Conventions (per user):
- Level NAMES only in announcements — no NIFTY price / level values (gap % allowed).
- DPM = Daily PVT/MACD2. When DPM is uptrending, 30m EMA50/100/200 act as
  SUPPORT; when downtrending, they act as RESISTANCE (cited as the reason).
- "Expect 1PXU_15PM Swing Up(s)" only when 5m PVT has started uptrending
  (5m PVT > 0); mirrored for the down side.
- Stochastic zones: OBZ / MZ / OSZ — no values.
- VOC moments unavailable (export carries no volume).

Run-time tool + output — never commit the generated log.
"""
import pandas as pd
import numpy as np

TODAY = pd.Timestamp('2026-11-04').date()
NEAR = 15.0        # points: considered "at" a level
LADDER = 50.0      # points: nearby-level ladder window

def load(path):
    df = pd.read_csv(path)
    df['time'] = pd.to_datetime(df['time'])
    return df[df['time'].dt.date == TODAY].reset_index(drop=True)

def pm_tag(pvt, m1, m2):
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
    d15 = load(paths['15s'])
    d1 = load(paths['1m'])
    d5 = load(paths['5m'])

    events = []  # (time, order, text)

    def ev(t, text):
        events.append((t, len(events), text))

    # ---------- previous close & gap ----------
    df5_full = pd.read_csv(paths['5m'])
    df5_full['time'] = pd.to_datetime(df5_full['time'])
    prev_days = df5_full[df5_full['time'].dt.date < TODAY]
    prev_close = prev_days[prev_days['time'].dt.date == prev_days['time'].dt.date.max()]['close'].iloc[-1]
    open_px = d5['open'].iloc[0]
    gap_pct = (open_px - prev_close) / prev_close * 100

    # ---------- static pivotal levels (names only) ----------
    row = d5[d5['D P'].notna()]
    lv = row.iloc[0] if len(row) else d5.iloc[0]
    levels = {}
    for c, name in [('D P','PD P'), ('D R1','R1 D'), ('D R2','R2 D'), ('D R3','R3 D'),
                    ('D S1','S1 D'), ('D S2','S2 D'), ('D S3','S3 D'),
                    ('W P','P W'), ('W R1','R1 W'), ('W S1','S1 W'),
                    ('M P','P M'), ('M R1','R1 M'), ('M S1','S1 M'),
                    ('Q P','P Q'), ('Daily VWAP','Daily VWAP')]:
        v = lv.get(c)
        if pd.notna(v):
            levels[name] = float(v)
    # DPM-driven dynamic levels: 30m EMA50/100/200
    dyn = {'30m EMA50': '30m EMA50', '30m EMA100': '30m EMA100', '30m EMA200': '30m EMA200'}

    # ---------- family trends at any 5m row ----------
    def fam_tag(i):
        r = d5.iloc[i]
        tags = {}
        t3 = pm_tag(r['PVT.1'], r['MACD Line.2'], r['MACD Line.3'])
        if t3: tags['3PM'] = t3
        t2 = pm_tag(r['PVT.2'], r['MACD Line.4'], r['MACD Line.5'])
        if t2: tags['2PM'] = t2
        td = pm_tag(r['PVT.3'], r['MACD Line.6'], r['MACD Line.7'])
        if td: tags['DPM'] = td
        return tags

    def dpm_state(t):
        """DPM trend word at time t."""
        idx = d5.index[d5['time'] <= t]
        if not len(idx):
            return 'Uptrend'
        return trend_word(fam_tag(idx[-1]).get('DPM'))

    def d5_state(t):
        idx = d5.index[d5['time'] <= t]
        return idx[-1] if len(idx) else 0

    # ---------- 1m-level events ----------
    c1 = d1['close'].values
    e21 = d1['1m EMA21'].values
    e50 = d1['1m EMA50'].values
    vwap1 = d1['Daily VWAP'].values
    pvt1 = d1['PVT'].values
    m11 = d1['MACD Line'].values
    m21 = d1['MACD Line.1'].values
    t1 = d1['time']

    below21 = False
    above50 = False   # for "retraced down to and taken support of 1E50"
    for i in range(1, len(d1)):
        t = t1.iloc[i]
        px = c1[i]
        # pullback to 1E21 that holds
        if below21 and pd.notna(e21[i]) and abs(px - e21[i]) <= NEAR and px < e21[i]:
            if not getattr(main, '_e21_res', False):
                main._e21_res = True
                main.__dict__['_ret_1E21'] = t
                ev(t, "Price has retraced up to and faced resistance at 1E21")
        elif pd.notna(e21[i]) and e21[i] - px > 25:
            main._e21_res = False
        if (not below21) and pd.notna(e21[i]) and abs(px - e21[i]) <= NEAR and px > e21[i]:
            if not getattr(main, '_e21_sup', False):
                main._e21_sup = True
                main.__dict__['_ret_1E21'] = t
                ev(t, "Price has retraced down to and taken support at 1E21")
        elif pd.notna(e21[i]) and px - e21[i] > 25:
            main._e21_sup = False
        if not below21 and pd.notna(e21[i-1]) and c1[i-1] > e21[i-1] and px < e21[i]:
            below21 = True
            bel = [n for n, v in levels.items() if 0 < px - v <= LADDER]
            emas = []
            for cn, nm in [('1m EMA50','1E50'), ('1m EMA100','1E100'), ('1m EMA200','1E200'),
                           ('5m EMA9','5E9'), ('5m EMA21','5E21'), ('5m EMA50','5E50')]:
                v = d1[cn].iloc[i]
                if pd.notna(v) and 0 < v - e21[i] <= LADDER:
                    emas.append(nm)
            ev(t, "Price comes below 1E21")
            # DPM-driven EMA supports/resistances
            dpm = dpm_state(t)
            role = 'support' if dpm == 'Uptrend' else 'resistance'
            ev(t, "1PM Swing Down started")
            if bel:
                ev(t, f"Nearby below Levels — Pivotal: {', '.join(bel)}")
            if emas:
                ev(t, f"EMAs above 1E21 (within 50 pts): {', '.join(emas)}")
            ev(t, f"30m EMA50/100/200 acting as {role} (DPM {dpm})")
        elif below21 and c1[i-1] < e21[i-1] and px > e21[i]:
            below21 = False
            ev(t, "Price back above 1E21")
        if pd.notna(e50[i-1]) and c1[i-1] > e50[i-1] and px < e50[i]:
            above50 = False
            ev(t, "Price has crossed below 1E50")
        elif pd.notna(e50[i-1]) and c1[i-1] < e50[i-1] and px > e50[i]:
            above50 = True
            ev(t, "Price has gone above 1E50")
        # pullback to 1E50 that holds (upswing mirror: faced resistance at 1E50)
        if above50 and pd.notna(e50[i]) and abs(px - e50[i]) <= NEAR and px > e50[i]:
            if not getattr(main, '_e50_sup', False):
                main._e50_sup = True
                main.__dict__['_ret_1E50'] = t
                ev(t, "Price has retraced down to and taken support of 1E50")
        elif pd.notna(e50[i]) and px - e50[i] > 25:
            main._e50_sup = False
        if (not above50) and pd.notna(e50[i]) and abs(px - e50[i]) <= NEAR and px < e50[i]:
            if not getattr(main, '_e50_res', False):
                main._e50_res = True
                main.__dict__['_ret_1E50'] = t
                ev(t, "Price has retraced up to and faced resistance at 1E50")
        elif pd.notna(e50[i]) and e50[i] - px > 25:
            main._e50_res = False
        if pd.notna(vwap1[i-1]) and c1[i-1] > vwap1[i-1] and px < vwap1[i]:
            ev(t, "1m Candle crossed below VWAP")
        elif pd.notna(vwap1[i-1]) and c1[i-1] < vwap1[i-1] and px > vwap1[i]:
            ev(t, "1m Candle crossed above VWAP")
        # 5PX about to cross down/up — 5m PVT read on the 1m TF (updates every
        # minute): value above zero but within +/-0.1 of the line + 1m PVT and
        # 1m MACD2 trending the same way. Once per episode.
        v5 = d1['PVT.1'].iloc[i]
        m2_fall = pd.notna(m21[i-1]) and m21[i] < m21[i-1]
        m2_rise = pd.notna(m21[i-1]) and m21[i] > m21[i-1]
        if pd.notna(v5) and 0 < v5 <= 0.1 and pvt1[i] < 0 and m2_fall \
                and not getattr(main, '_pzc_dn', False):
            main._pzc_dn = True
            main._pzc_up = False
            ev(t, "5PX about to cross down")
        elif pd.notna(v5) and -0.1 <= v5 < 0 and pvt1[i] > 0 and m2_rise \
                and not getattr(main, '_pzc_up', False):
            main._pzc_up = True
            main._pzc_dn = False
            ev(t, "5PX about to cross up")
        elif pd.notna(v5) and abs(v5) > 0.1:
            main._pzc_dn = False
            main._pzc_up = False
        if pd.notna(pvt1[i-1]) and pd.notna(pvt1[i]):
            if pvt1[i-1] > 0 >= pvt1[i]:
                ev(t, "Attention : 1PX Cross Down")
            elif pvt1[i-1] < 0 <= pvt1[i]:
                ev(t, "Attention : 1PX Cross Up")
        if pd.notna(m21[i-1]) and pd.notna(m21[i]):
            if m21[i-1] > m11[i-1] and m21[i] <= m11[i]:
                ev(t, "Attention : 1M2X Cross Down")
            elif m21[i-1] < m11[i-1] and m21[i] >= m11[i]:
                ev(t, "Attention : 1M2X Cross Up")

    # ---------- 5m-level events ----------
    pvt5 = d5['PVT'].values
    m15 = d5['MACD Line'].values
    m25 = d5['MACD Line.1'].values
    k5, dd5 = d5['K'].values, d5['D'].values
    cl5, vw5 = d5['close'].values, d5['Daily VWAP'].values
    t5 = d5['time']
    ut5 = dt5 = False
    pvt5_up = pvt5_dn = False   # 5m PVT trend gates the "Expect ..." lines
    for i in range(1, len(d5)):
        t = t5.iloc[i]
        if pd.notna(pvt5[i-1]) and pd.notna(pvt5[i]):
            if pvt5[i-1] > 0 >= pvt5[i]:
                pvt5_dn = True; pvt5_up = False
                ev(t, "5PXD — 5m PVT Cross Down")
                ev(t, "Attention : 5PX Cross Down")
            elif pvt5[i-1] < 0 <= pvt5[i]:
                pvt5_up = True; pvt5_dn = False
                ev(t, "5PXU — 5m PVT Cross Up")
                ev(t, "Attention : 5PX Cross Up")
        if pd.notna(m25[i-1]) and pd.notna(m25[i]):
            if m25[i-1] > m15[i-1] and m25[i] <= m15[i]:
                ev(t, "5M2XD — 5m MACD2 Cross Down")
                ev(t, "Attention : 5M2X Cross Down")
            elif m25[i-1] < m15[i-1] and m25[i] >= m15[i]:
                ev(t, "5M2XU — 5m MACD2 Cross Up")
                ev(t, "Attention : 5M2X Cross Up")
        was_ut, was_dt = ut5, dt5
        ut5 = pvt5[i] > 0 and m15[i] > 0 and m25[i] > 0
        dt5 = pvt5[i] < 0 and m15[i] < 0 and m25[i] < 0
        if dt5 and not was_dt:
            ev(t, "5PM Downtrend started — Expect 1PXD_15PM Swing Downs")
            ev(t, "Currently the Bias is " +
                ("Strong Downtrend" if cl5[i] < vw5[i] else "Downtrend") +
                (" as we have 5PM Downtrend below VWAP" if cl5[i] < vw5[i] else " as we have 5PM Downtrend"))
        if ut5 and not was_ut:
            ev(t, "5PM Uptrend started — Expect 1PXU_15PM Swing Ups")
            ev(t, "Currently the Bias is " +
                ("Strong Uptrend" if cl5[i] > vw5[i] else "Uptrend") +
                (" as we have 5PM Uptrend above VWAP" if cl5[i] > vw5[i] else " as we have 5PM Uptrend"))
        if pd.notna(k5[i-1]) and pd.notna(dd5[i]):
            if k5[i-1] > dd5[i-1] and k5[i] <= dd5[i]:
                zone = 'OBZ' if k5[i-1] >= 80 else 'OSZ' if k5[i-1] <= 20 else 'MZ'
                ev(t, f"5m Stochastic cross down from {zone}")
            elif k5[i-1] < dd5[i-1] and k5[i] >= dd5[i]:
                zone = 'OSZ' if k5[i-1] <= 20 else 'OBZ' if k5[i-1] >= 80 else 'MZ'
                ev(t, f"5m Stochastic cross up from {zone}")
        if pd.notna(vw5[i-1]) and cl5[i-1] > vw5[i-1] and cl5[i] < vw5[i]:
            ev(t, "5m Candle crossed below VWAP")
        elif pd.notna(vw5[i-1]) and cl5[i-1] < vw5[i-1] and cl5[i] > vw5[i]:
            ev(t, "5m Candle crossed above VWAP")

    # ---------- 15s-level events ----------
    c15 = d15['close'].values
    pvt15 = d15['PVT'].values
    ema = lambda x, n: pd.Series(x).ewm(span=n, adjust=False).mean().values
    m2s_line = ema(c15, 24) - ema(c15, 52)          # 15s MACD2 not exported — synthesized
    m2s_sig = pd.Series(m2s_line).ewm(span=9, adjust=False).mean().values
    x15 = []
    for i in range(1, len(c15)):
        if pd.notna(pvt15[i-1]) and pd.notna(pvt15[i]):
            if pvt15[i-1] > 0 >= pvt15[i]:
                x15.append((i, 'D'))
            elif pvt15[i-1] < 0 <= pvt15[i]:
                x15.append((i, 'U'))
    x15m2 = [(i, 'D' if m2s_line[i-1] > m2s_sig[i-1] and m2s_line[i] <= m2s_sig[i] else 'U')
             for i in range(1, len(c15))
             if (m2s_line[i-1] > m2s_sig[i-1] and m2s_line[i] <= m2s_sig[i])
             or (m2s_line[i-1] < m2s_sig[i-1] and m2s_line[i] >= m2s_sig[i])]
    xd15 = {d15['time'][i] for i, d in x15 if d == 'D'}
    xu15 = {d15['time'][i] for i, d in x15 if d == 'U'}
    xd15m2 = {d15['time'][i] for i, d in x15m2 if d == 'D'}
    xu15m2 = {d15['time'][i] for i, d in x15m2 if d == 'U'}
    fired_xd = fired_xu = None
    last15 = {}
    touches = {}   # level name -> touch count
    away = {}      # level name -> True once price moved >= 25 pts away
    for i in range(1, len(d15)):
        t = d15['time'].iloc[i]
        px = c15[i]
        # 15s momentors — not announced standalone (guideline); 15s only
        # confirms 1PXD_15PM / 1PXU_15PM below.
        # 1PXD_15PM / 1PXU_15PM confirmations
        j1 = d1.index[d1['time'] == t.floor('min')]
        # VWAP proximity warning (pre-cross)
        r5 = d5.iloc[d5_state(t)]
        vw_now = r5.get('Daily VWAP')
        if pd.notna(vw_now):
            vwap_key = 'vwappro'
            if abs(px - vw_now) <= 10:
                side = 'down' if px > vw_now else 'up'
                key = vwap_key + side
                if last15.get(key) is None or (t - last15[key]).total_seconds() >= 600:
                    last15[key] = t
                    ev(t, f"Price is close to VWAP. About to cross {side}")
        if len(j1):
            ji = j1[0]
            prev = d1['PVT'].iloc[ji-1] if ji > 0 else np.nan
            curr = d1['PVT'].iloc[ji]
            lo, hi = t - pd.Timedelta(seconds=30), t + pd.Timedelta(seconds=30)
            if pd.notna(prev) and prev > 0 >= curr:
                acc = any(lo <= x <= hi for x in xd15) or any(lo <= x <= hi for x in xd15m2)
                if acc and (fired_xd is None or (t - fired_xd).total_seconds() > 300):
                    fired_xd = t
                    ev(t, "1PXD_15PM Swing Down — 1m PXD with 15s PVT/M2XD confirmation")
            if pd.notna(prev) and prev < 0 <= curr:
                acc = any(lo <= x <= hi for x in xu15) or any(lo <= x <= hi for x in xu15m2)
                if acc and (fired_xu is None or (t - fired_xu).total_seconds() > 300):
                    fired_xu = t
                    ev(t, "1PXU_15PM Swing Up — 1m PXU with 15s PVT/M2XU confirmation")
        # level touches — retest only after price moved >= 25 pts away
        # Dynamic EMA ladder (avoid EMA9): 1m/5m/30m EMA21+ — values track the live bars
        j1l = d1.index[d1['time'] <= t]
        r1l = d1.iloc[j1l[-1]] if len(j1l) else None
        cur_levels = dict(levels)
        for cn, nm in dyn.items():
            v = r5.get(cn)
            if pd.notna(v):
                cur_levels[nm] = float(v)
        if r1l is not None:
            for cn, nm in [('1m EMA21','1E21'), ('1m EMA50','1E50'), ('1m EMA100','1E100'), ('1m EMA200','1E200'),
                           ('5m EMA21','5E21'), ('5m EMA50','5E50'), ('5m EMA100','5E100'), ('5m EMA200','5E200')]:
                v = r1l.get(cn)
                if pd.notna(v):
                    cur_levels[nm] = float(v)
        dpm = dpm_state(t)
        r5_pvt = r5['PVT']
        pvt5_up_now = pd.notna(r5_pvt) and r5_pvt > 0    # 5m PVT uptrending (point-in-time)
        pvt5_dn_now = pd.notna(r5_pvt) and r5_pvt < 0    # 5m PVT downtrending
        # collect same-tick touches across levels, then announce grouped
        tick_res, tick_sup = [], []   # (name, first_touch, again)
        for name, val in cur_levels.items():
            if abs(px - val) <= NEAR:
                if not away.get(name, True):
                    continue
                first = name not in touches
                if first:
                    touches[name] = 1
                    away[name] = False
                else:
                    touches[name] += 1
                    away[name] = False
                # suppress a generic 1E21/1E50 touch right after its retrace line
                if name in ('1E21', '1E50') and main.__dict__.get(f'_ret_{name}') \
                        and (t - main.__dict__[f'_ret_{name}']).total_seconds() < 120:
                    continue
                again = (not first) and touches[name] >= 3
                if px < val:
                    tick_res.append((name, first, again))
                else:
                    tick_sup.append((name, first, again))
            elif abs(px - val) > 25 and name in touches:
                away[name] = True

        def group(names):
            return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]

        def emit_group(items, is_res):
            if not items:
                return
            names = [n for n, _, _ in items]
            any_first = any(f for _, f, _ in items)
            any_again = any(a for _, _, a in items)
            e30 = [n for n in names if n in dyn.values()]
            reason = f" because of DPM {dpm}" if e30 and len(e30) == len(names) else ""
            gate = pvt5_dn_now if is_res else pvt5_up_now
            expect = "Expect 1PXD_15PM Swing Down" if is_res else "Expect 1PXU_15PM Swing Up"
            if is_res:
                ev(t, f"Price facing resistance at level {group(names)}{reason}" if any_first
                       else f"Price retested the level {group(names)}" + (" again" if any_again else ""))
            else:
                ev(t, f"Price takes support of level {group(names)}{reason}" if any_first
                       else f"Price retested support {group(names)}" + (" again" if any_again else ""))
            if gate:
                ev(t, expect)

        emit_group(tick_res, True)
        emit_group(tick_sup, False)

    events.sort(key=lambda x: (x[0], x[1]))

    # opening call as first event
    open_px = d1['close'].iloc[0]
    above = [n for n, v in levels.items() if 0 < v - open_px <= LADDER]
    below = [n for n, v in levels.items() if 0 < open_px - v <= LADDER]
    gap_word = 'Gap up' if gap_pct >= 0 else 'Gap down'
    ev(d1['time'].iloc[0],
       f"Session open. {gap_word} {gap_pct:+.2f}% from previous close. "
       f"Resistance within 50 pts above: {', '.join(above) or 'none'}. "
       f"Support within 50 pts below: {', '.join(below) or 'none'}. "
       f"(VOC announcements unavailable — no volume in export)")
    events.sort(key=lambda x: (x[0], x[1]))
    print('\n'.join(f"{t.strftime('%H:%M:%S')}  {text}" for t, _, text in events))
    return events

if __name__ == '__main__':
    import os
    dl = os.path.expanduser('~/Downloads')
    main({
        '15s': f"{dl}/NSE_NIFTY1!, 15S_f3fe0.csv",
        '1m':  f"{dl}/NSE_NIFTY1!, 1_4b5a1.csv",
        '5m':  f"{dl}/NSE_NIFTY1!, 5_b6473.csv",
    })
