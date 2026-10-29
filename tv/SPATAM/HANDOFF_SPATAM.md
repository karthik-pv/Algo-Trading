# HANDOFF — SPATAM (TradingView Indicator)

> Hand this to a new AI session (or to the Algo-app developer) to get full context.
> Last updated: 2026-10-23 · Script version: **2026-10-23.22** · ~2,070 lines
> SPATAM = **S**etUp · **P**rice Action · **T**rend · **A**lerts · **M**omentum
> (This replaces `Lookout/HANDOFF_Lookouts.md` — SPATAM supersedes Lookouts + the announcer.)

---

## 1. What this is

`SPATAM` is a TradingView **Pine v5 indicator** (overlay, `scale.none`) for NSE:NIFTY charts
(seconds/minutes timeframes). It renders two visual tables and **fires lookout alerts** through
one combined `alert()` → webhook → **the Algo app** (webhook receiver) which **speaks** the
alerts (its existing time/PnL TTS queue) and **persists** them to a file with timestamps.

The **announcer** (`announcer-71bb.onrender.com`) is **retired** — SPATAM no longer points there.

---

## 2. Files

| File | Purpose |
|---|---|
| `TradingView/Levels/SPATAM` | **The live script** (paste into TV Pine Editor). Line 4 has `// VERSION: YYYY-MM-DD.N` — always verify after copying |
| `TradingView/Levels/HANDOFF_SPATAM.md` | This document |
| `TradingView/TV_Algo_JSON/HANDOFF_TradingView_Algo_JSON.md` | Sibling producer: the `DATA`/`LEVELS` JSON feed (`Strategy_TradingInvestAlgo_Data`) → same Algo app |
| `TradingView/Lookout/Lookouts` | Legacy alert engine (announcer era) — reference only; its lookout families were ported into SPATAM (see §5) |

---

## 3. Tables (2, both always live while shown)

### 3.1 Levels table — LEFT, full display height (`position.top_left`, 4 cols)
Header bar (yellow) shows the selected view: **CW-CD / CW-ND / NW** (input `Levels Table`).

| Col | Content |
|---|---|
| 0 | Level value (leading zeros stripped) |
| 1 | Level name — `5m EMA200`, `R1 D`, `W S2`, `VWAP D`, `PDH`, `Cam R4`, `Fib 61.8 D`, … |
| 2 | Distance from Close (points) |
| 3 | **Family trend tag** — `2P / 2PM2 / 2PM`, `3P…`, `5P…`, `DP…`, `WP…`, `MP…`, `QP…` |

- Rows: all EMA levels, D/W/M/Q floor pivots (+R4/S4, optional R5/S5), next-day & next-week
  projected pivots, PDH/PDL/PDC/D Open, Camarilla, VWAP D/W, PWH/PWL/PWC, PMH/PML, round
  numbers, opening range, prev-day fibs — sorted descending, **Close pinned mid-table** (gray band).
- **Trend tag logic** (Lookouts hand-off mapping + user extension): 5m trend plays at 1m EMA50 →
  tag `5P…`; 30m at 1m EMA100/200 → `3P…`; 2H at 5m EMA50/100/200 → `2P…`; Daily at 30m EMAs →
  `DP…`; Weekly at 2h EMAs → `WP…`; **Monthly at D EMA100/200** → `MP…`; **Quarterly at W
  EMA100/200** → `QP…`. Text: PVT alone → `2P`; MACD2 joins → `2PM2`; M1+M2 both → `2PM`.
  Shade: **dark** red `#f91c06` / green `#02580b` when MACD2 agrees with PVT; **light** red
  `#fab9b1` / green `#c5fab7` when MACD2 is retracing against it.
- Views: **CW-CD** = current week + current day (live) · **CW-ND** = current week + projected
  next day · **NW** = projected next week + projected next day (weekend view).
- Table is padded with empty rows down to `Dashboard height` so the left side spans the display.

### 3.2 Right dashboard (`position.top_right`, 6 cols × `Dashboard height` rows)
Top → bottom (row split derived from `Dashboard height`, exact 10/30/20/40 at 73 rows):

1. **Digest (~10%)** — `3S` (+30m Stoch zone OSZ/NOSZ/MZ/NOBZ/OBZ) · `5PM` (M1+M2 arrows) ·
   `CV` (close vs session VWAP) · `3PM` (M1+M2 arrows). Backgrounds green/red by PVT sign & VWAP side.
2. **Set Ups (~20%)** — one row per TF (`1`,`5`,`3`,`2`,`D`): Uptrend string (green) ·
   Downtrend string (red) · spacer · Cross-up code (green when fired) · Cross-down code (red).
   Strings like `1PME50_5S`, `5PSMV_3S`, `2PSM_DS`, `DPSM_WS`; cross codes `5POX`, `3SPM oX`, `1VX`.
3. **PM/S mini-grid** — `5PM | 3PM | 2PM | DPM` and `3S | 2S | DS | WS` headers (alternating
   black / `#798095`) with trend arrows on PVT-sign backgrounds.
4. **Alert Log (~40%)** — `Time` (IST HH:MM:SS) + `Alert` message; messages **merged across cols
   1–5** (so long texts can't stretch the shared columns); row color by family (green UT/Support/
   Crossing up · red DT/Resistance/Crossing down/ends · yellow Pz/M2z/Momentum-lost · white
   deploy marker); newest first; 30-entry rolling capacity.

The old large Momentum grid was **removed** (helpers `f_momRow/f_arrow/f_xs/f_stoch_bg/f_pvt_bg/
f_voc_bg/f_zone_stoch` remain defined if ever re-added).

---

## 4. Engine (per-TF stats via `request.security`)

- **Stochastic (S)** — RSI-stoch 14, K smoothed; SPATAM uses K-smoothing **3** for 5m/30m/2H/D/W
- **PVT (P)** — normalized to [−1,1]; value, trend (vs 0), poise (rising), cross indices
- **MACD** — M1 = 12/26/9, M2 = 24/52/9; trends, poise, **z-cross flag** (±1/±2/±3), X-indices
- **VOC (o)** — `100·(EMA9(vol)−EMA21(vol))/EMA21(vol)`; TFs 1m/5m/30m/2H/D/W
- **EMA levels** — EMA50/100/200 per TF (1m/5m/30m/2h) + D/W EMA100/200, `lookahead_on`
- **Pivots** — prev completed D/W/M/Q floor pivots (P/R1–R5/S1–S5 + prev H/L/C in ONE pull per
  period); next-day/next-week projections computed **locally** (`f_devPivots`) from developing
  D/W OHLC (single `dev_ohlc()` pull per TF)
- **CV / VW** — close vs session VWAP, computed locally (no pull)

### Security budget — currently **39 of 40**
The counter = unique (function-expression × resolution) pairs. Breakdown:
Stoch 5 · VOC 6 (1m/5m/30m/2H/D/W) · MACD 8 · PVT 8 · Prev-period pivots+HLC 4 (D/W/M/3M) ·
EMA triples 6 (1/5/30/120/D/W) · dev OHLC 2 (D/W) = **39**. One slot free — **next data pull
needs a consolidation** (e.g. merge MACD+PVT into one engine tuple per TF, saving 8).

---

## 5. Alert families wired (fire on CONFIRMED bars → `f_alert`)

| # | Family | Codes / examples | Trigger |
|---|---|---|---|
| 1 | **S/R + crossings at EMA levels** | `Support @ 3E1` · `Resistance @ 5E2` · `Crossing up @ 1E50` · `Crossing down @ 2E2` | touch+close-back (retests only — close-through takes **precedence**, so S/R and crossings never fire together); **same-type events on one bar are grouped**: `Crossing down @ 1E50 and 1E1`, `Support @ 1E2, 5E50 and 5E1` |
| 2 | **HTF S/M1/M2 crosses** | `3SX UT` · `5M1X DT` · `2M2X UT` · `3M2z DT` (M2 curling, zCross flag ±2) | cross on the just-closed bar (index == 1) |
| 3 | **PVT crosses, volume-confirmed** | `1PX UT` · `5PX1o UT` / plain `5PX UT` · `3PX5o` · `2PX35o/5o/3o` · `DPX32o/3o/2o` · `WPXDo` | PVT cross on the bar; `o` suffix = lower-TF VOC > 0 |
| 4 | **PVT pre-cross (Pz)** | `5Pz UT` · `3Pz DT` · `2Pz…` · `DPz…` · `WPz…` | PVT in (−0.1, 0] (up) / (0, +0.1) (down) **and lower-TF PVT already turned that way** (chain confirm) |
| 5 | **VOC states** | `5oz → 5ox → 5o → 5o ends` (+1o/3o/2o/Do/Wo) | state change, once per episode; `oz` (−0.1…0, rising) needs lower-TF VOC > 0; `o` = > 0.1 |
| 6 | **Tag changes** (SPATAM-native) | `2PM2 DT formed` · `3PM UT formed` | family trend tag flips |
| 7 | **5m PVT × MACD2 sync** | `Attention 5PM Cross Up Sync` / `…Cross Down Sync` | both cross same way on the same bar or ±2 bars; once per episode, re-arms when indices age out (> 3) |
| 8 | **30m VOC × 5PM down sync** | `Attention : 5PM_3ox` | 30m VOC crossed up within the window of a 5m PM cross-down sync |
| 9 | **1PME50 run vs 5m PVT divergence** | `1PME50 downtrend starts..But 5m PVT is still at 0.26..Watch out..` | 1PME50 run **starts** (1m PVT+MACD2+E50 aligned) while 5m PVT is far on the opposite side (±0.1); once per run |

**Gates (inputs → Alerts group):** master switch · per-family toggles · `Fire Daily (D)` and
`Fire Weekly (W)` checkboxes (**default OFF**) gate all D/W/M/Q announcements.

**Not ported from Lookouts (available on request):** HTF cross reminders, forecasts, 3-nearest
ladder, falls-from, test counting, consolidation watch, session zones, 15s tracker, intraday
trend judge.

---

## 6. Alert → Algo app contract

**Pipeline:** event → `f_alert` (stamps Alert Log + queues with fire-time) → **one combined
`alert()` per flush** (default 15 s, bar-based throttle, queue cap 20, dedup) → webhook → Algo app.

With **"Send alert flush as JSON" ON** (Settings → Alerts):

```json
{
  "MSG": "ALERTS",
  "SYM": "NSE:NIFTY",
  "ALERTS": [
    {"T": "09:15:21", "TXT": "3SX DT"},
    {"T": "09:15:22", "TXT": "Crossing down @ 1E50 and 1E1"},
    {"T": "09:15:23", "TXT": "Support @ 1E2, 5E50 and 5E1"},
    {"T": "09:15:23", "TXT": "1PME50 downtrend starts..But 5m PVT is still at 0.26..Watch out.."}
  ]
}
```

- `MSG` = `"ALERTS"` — route beside the feed's `DATA` / `LEVELS` (same app, same endpoint)
- `SYM` = `syminfo.tickerid` (`EXCHANGE:TICKER`)
- `T` = **exact per-event IST fire time**, `HH:MM:SS`, **no date** (captured when the event
  queued, not at flush); records share a flush only when events landed in the same window
- `TXT` = message exactly as the on-chart Alert Log shows it; `VWAP` → `V WAP` substitution applied
- Messages never contain double quotes → JSON-safe; max 20 records per flush (~1.2 KB — far
  under the 4096-char alert cap); typical 1–4 records

**Suggested app handling:** iterate `ALERTS` → speak `TXT`s (join with spaces through the
existing time/PnL TTS queue) → append each `T | TXT` line to a per-day alert file (one batched
write per flush). Text mode (JSON toggle OFF) sends the old single-line, double-space-joined
payload — kept for compatibility.

---

## 7. Inputs (groups)

- **Extra S/R Levels** — toggles for PDH/PDL, Camarilla, VWAP, PWH/PWL, PMH/PML, round numbers,
  opening range, fibs, R4/S4, R5/S5 + steps
- **Table Layout** — `Levels shown above/below Close` (15) · `Levels Table` (CW-CD / CW-ND / NW)
- **Dashboard** — `Show right dashboard` (ON) · `Dashboard height (rows)` (73 — tune to fill the
  display; the Levels table pads to the same height) · `Alert flush cadence` (15 s)
- **Alerts** — master, per-family toggles, D/W gates, JSON mode toggle

## 8. Pine gotchas (each has bitten — do not regress)

1. **Pine `and`/`or` are NOT lazy** — guard with `if`, never inline `size>0 and array.get(a,0)`
2. **`var` state rolls back intrabar** — event logic gates on `barstate.isconfirmed`; throttles are bar-based
3. **`alert()` = once per bar per call site** — the flush is the single call site
4. **`alert()` does NOT fire in Bar Replay** (log still updates); **never fires on historical bars**
5. **`request.security` ≤ 40** — counted as unique (function-expression × resolution) pairs,
   **39/40** (§4). Consolidate before adding any pull
6. **Table columns size to their widest cell** — log messages are merged across cols 1–5 so they
   can't stretch shared columns (broke alignment once already)
7. **Functions can only reference globals declared earlier in the script**
8. **EMA levels use `lookahead_on`** — HTF fields reflect the developing bar on realtime ticks
9. **Argument-count drift** — grep all call sites when changing a function's params
   (missed `_pfx` in `f_familyTag` broke the build once)

## 9. Deploy runbook

1. Copy `Levels/SPATAM` — verify the VERSION stamp on line 4
2. Pine Editor → select all → paste → **Save**
3. Remove old instance from chart → **Add to chart** (fresh inputs)
4. Settings → Alerts → **"Send alert flush as JSON"** → ON
5. Create one alert: condition **SPATAM → Any alert() function call** → webhook = **Algo app
   endpoint** → notifications on
6. Verify: Alert Log seeds `SPATAM deployed — listening` (only when the log is empty), first
   flush carries recent confirmed-bar events
