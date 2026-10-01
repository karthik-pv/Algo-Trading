# HANDOFF — PAInsight Forecasting Engine (for OptionScalper)

Context transfer from the PAInsight research session (2026-09-25). This document contains
everything needed to implement the forecasting engine inside this repo (Algo-Trading /
"OptionScalper") without re-doing the research. The engine forecasts **rest-of-day price
action** (bias, ranked S/R levels with confidence factors) from the TradingView webhook
snapshot that already arrives every 15s.

---

## 1. Source data (insight store input)

Five TradingView CSV exports live in **`data/insight/csv/`** (copied from the original
research folder `/Users/phvrao/Projects/PAInsight/` — refresh by re-copying newer exports
over them). `data/` is gitignored; store_builder.py should take the CSV dir as a
configurable argument, defaulting to `data/insight/csv/`:

| File | TF | Range | Notes |
|---|---|---|---|
| `NSE_NIFTY1!, 1_20adf.csv` | 1m | 2026-07-13 → 2026-09-25 | has 1m EMA9/21/50/100/200 + 5m EMA50/100/200 |
| `NSE_NIFTY1!, 5_be22a.csv` | 5m | 2025-08-25 → | also carries 1m EMA columns |
| `NSE_NIFTY1!, 30_42a03.csv` | 30m | 2021-11-18 → | primary intraday stats source |
| `NSE_NIFTY1!, 120_93fb3.csv` | 2H | 2007-10-26 → | 2H bars at 09:15/11:15/13:15 |
| `NSE_NIFTY1!, 1D_6b212.csv` | 1D | 2007-10-26 → | daily OHLC + D/W/M EMAs |

**Column conventions (locate by header name, NOT fixed index):**
- Two `MACD Line` / `Signal Line` pairs per file. **Numerically verified (2026-09-25):**
  first pair = MACD 12/26 → **DISCARD** (payload's `_1226`); second pair = MACD 24/52 → **USE** (payload's `_2452`).
- `PVT` column: values are small/normalized (±1-scale) in CSVs vs raw in payload — **use signs and zero-crosses only**.
- `Cross Up` / `Cross Down` columns = MACD_2452 signal-cross markers (mostly empty; `0` on event bars).
- `K`, `D` = Stochastic.
- Intraday files have `VWAP` (session VWAP).
- D-file date format `2026-09-25`; intraday `2026-09-25T13:15:00+05:30` (strip tz on parse).
- Environment: `/usr/bin/python3` (3.x, **no pandas** — use csv module; the app's venv has more).

## 2. Insight findings (verified numbers — bake into store.json)

### 2.1 Regime prevalence (30m bars since 2025-01-01, 8 regimes)
`D{+/-}|2H{+/-}|30{+/-}` from PVT signs (D = previous day's D-PVT sign):

```
D+|2H+|30+ 19%   D+|2H-|30+  4%   D-|2H+|30+ 18%   D-|2H-|30+ 11%
D+|2H+|30-  9%   D+|2H-|30- 17%   D-|2H+|30-  3%   D-|2H-|30- 19%
```

### 2.2 Resistance: reject % [n] — approach from below, same-bar close back below
30m bars since 2025, per regime:

```
Level          D+|2H+|30+     D+|2H+|30-     D+|2H-|30+     D+|2H-|30-     D-|2H+|30+     D-|2H+|30-     D-|2H-|30+     D-|2H-|30-
PDH               44%[50]       73%[15]       48%[23]       69%[39]       30%[64]       100%[2]       62%[50]       69%[36]
PDC                50%[4]        33%[3]         0%[4]       51%[55]        67%[3]             -        7%[14]       61%[75]
PDL               40%[221]        86%[7]       47%[17]       100%[2]      38%[152]             -       66%[44]       100%[8]
VWAP              29%[394]       54%[82]       22%[60]      52%[129]      21%[228]       50%[16]      24%[165]      52%[238]
Round100          35%[450]       71%[66]       32%[56]      55%[146]      36%[395]       60%[20]      40%[258]      56%[288]
30m E50           29%[48]        53%[32]       64%[53]       65%[51]       20%[79]        75%[8]      60%[140]       72%[50]
30m E100          27%[33]        29%[7]        50%[26]       67%[58]       34%[119]       50%[12]      61%[54]       73%[22]
2H E50            28%[18]        56%[9]        26%[23]       60%[40]       41%[104]       62%[8]       47%[17]        67%[9]
2H E100           25%[16]        38%[8]        40%[5]        59%[17]       30%[56]             -       20%[10]        62%[13]
D E50             51%[45]       100%[4]        50%[10]       62%[16]       41%[32]       100%[1]        12%[8]        56%[9]
```

### 2.3 Support: hold % [n] — approach from above, same-bar close back above
```
Level          D+|2H+|30+     D+|2H+|30-     D+|2H-|30+     D+|2H-|30-     D-|2H+|30+     D-|2H+|30-     D-|2H-|30+     D-|2H-|30-
PDH               69%[36]       60%[68]        75%[4]       38%[74]       85%[26]       65%[17]       62%[13]       36%[69]
PDC                75%[4]        73%[74]        50%[4]      40%[187]             -        88%[8]       100%[6]      45%[246]
PDL               65%[79]        35%[17]        75%[4]             -        76%[45]        33%[6]        29%[7]         0%[1]
VWAP              60%[255]      19%[147]        49%[39]      26%[259]      61%[152]       20%[41]      58%[103]      26%[428]
Round100          (not computed in research — builder MUST compute support-side round 100s too)
30m E50           63%[43]       54%[139]        33%[12]      43%[110]       77%[62]       38%[39]       67%[27]       20%[44]
30m E100          53%[15]        45%[33]        67%[12]      41%[111]       67%[58]       39%[33]       42%[12]       23%[22]
2H E50            67%[15]        45%[11]        38%[16]      43%[122]       79%[33]       40%[20]        50%[2]        47%[19]
2H E100           50%[8]         43%[28]             -        46%[74]       60%[10]        50%[8]        78%[9]        47%[34]
D E50             57%[21]        33%[15]       100%[1]        45%[40]        33%[9]       100%[1]        50%[2]        64%[47]
```

Key reads: 30m E50/E100 rejection probability roughly doubles bull→bear; VWAP flips from
support (60% hold in full bull) to wall (52% reject in full bear); PDH/PDL alone are weak — regime is what matters.

### 2.4 Confluence curves (VWAP, 30m bars since 2025) — the confidence backbone
Bearish factors = [30m PVT<0, MACD_2452 < Signal_2452, Stoch K < D]:

| # factors | Reject from below | Hold from above |
|---|---|---|
| 0 | 10% (35/338) | 75% (79/106) |
| 1 | 33% (157/482) | 54% (231/424) |
| 2 | 50% (198/400) | 34% (180/531) |
| 3 | 73% (67/92) | 13% (48/363) |

≈ +20pp rejection per bearish factor from below; mirrored decay on the support side.
Multi-TF PVT alignment (count of {30m, 2H, D-prev} PVT<0) → VWAP reject from below:
0: 29%, 1: 28%, 2: 37%, 3: 52%.

### 2.5 Micro layer (last 2 months Jul 25–Sep 25; 44 trading days; 32 days with any 30m PVT<0; 11 fully negative)
Rejections while 30m PVT<0 (8,940 1m bars / 1,788 5m bars):

| Level | Rejections | Episodes* | Days | ~Per day | Next-bar confirmed |
|---|---|---|---|---|---|
| 1m EMA50 | 747 | 402 | 32/32 | 12.6 | 349 |
| 1m EMA100 | 455 | 255 | 31/32 | 8.0 | 205 |
| 1m EMA200 | 284 | 158 | 28/32 | 4.9 | 136 |
| 5m EMA50 | 143 | 80 | 26/32 | 2.5 | 69 |
| 5m EMA100 | 63 | 30 | 15/32 | 0.9 | 25 |
| 5m EMA200 | 36 | 24 | 10/32 | 0.8 | 13 |

*Episode = consecutive bars rejected at the same EMA counted once.

**Setup A/B (timing triggers; 30m PVT<0 regime):**
- Setup = 1m PVT zero-cross down + 1m MACD_2452 cross-down.
  - **A**: 5m PVT stays green (≥0). Strict same-bar: 15 signals; relaxed ±3 bars: 59.
  - **B**: 5m PVT crosses down too. Strict: 4 signals; relaxed: 12.

| Level | A strict | A relaxed | B strict | B relaxed |
|---|---|---|---|---|
| 1m EMA50 | 11/15 (73%) | 35/59 (59%) | 4/4 (100%) | 10/12 (83%) |
| 1m EMA100 | 6/15 (40%) | 25/59 (42%) | 3/4 (75%) | 8/12 (67%) |
| 1m EMA200 | 4/15 (27%) | 15/59 (25%) | 1/4 | 5/12 (42%) |
| 5m EMA50 | 6/15 (40%) | 24/59 (41%) | 2/4 | 6/12 (50%) |
| 5m EMA100 | 2/15 | 8/59 (14%) | 2/4 | 5/12 (42%) |
| 5m EMA200 | 0/15 | 4/59 (7%) | 1/4 | 3/12 (25%) |

B is the high-conviction variant (higher rejection at every level, ~80% next-bar follow-through).
Rejection chain: **1m E50 (noise) → 5m E50 (workhorse) → 5m E100 (highest per-touch conviction)
→ 30m E21/E9+VWAP cluster → round 100s → PDH/PDC**.

### 2.6 Daily layer (D file, 2023+)
| Prev-day D-PVT | n | Next day up | Median move | PDH touched&rejected | PDL touched&held |
|---|---|---|---|---|---|
| D+ | 491 | 54% | +0.05% | 25% | 19% |
| D- | 434 | 49% | −0.03% | 23% | 24% |

## 3. Webhook payload schema (locked — from the Pine alert script)

Every 15s (realtime confirmed 15s bars), POST to `/trading_view_webhook`
(server.py:926; currently via ngrok tunnel). Shape:

```
{ "INDICATORS": { "5S":{...}, "15S":{...}, "1M":{...}, "5M":{...}, "30M":{...}, "2H":{...} },
  "SETUPS": { "DASHBOARD":{...}, "1M":{...}, "5M":{...}, "30M":{...}, "2H":{...}, "D":{...} } }
```

Each INDICATORS TF block (all six TFs share the shape):
`PVT, PVTPoiseFlag, PVTTrendFlag, PVTCrossOverIndex, PVTCrossUnderIndex,
Trend_1226, X_1226, Y_1226, MACD_1226, Signal_1226, Hist_1226,
Trend_2452, X_2452, Y_2452, MACD_2452, Signal_2452, Hist_2452,
ZCross, K, KTrendFlag, StochPoiseFlag, KCrossoverIndex, KCrossUnderIndex,
EMA9, EMA21, EMA50, EMA100, EMA200, VWAP,
VOC, VOCTrendFlag, VOCCrossOverIndex, VOCCrossUnderIndex`

**The D and W INDICATORS sections are commented out in the Pine script — NOT in the payload.**
Daily/weekly context comes from `SETUPS.DASHBOARD` + the D-CSV loaded at startup.

`SETUPS.DASHBOARD`: `DPM`(str)+`DPMPVT`, `WS`(str)+`WSPVT`, `2PM`(str)+`2PMPVT`,
`DS`(str)+`DSPVT`, `3PM`(str)+`3PMPVT`, `2S`(str)+`2SPVT`.
`SETUPS` per TF (1M/5M/30M/2H/D): `Uptrend`, `Downtrend`, `CrossUptrend`, `CrossDowntrend` (strings).

### Locked field semantics (confirmed by user 2026-09-25)
- `PVTTrendFlag`: **+1 if PVT>0, −1 if PVT<0** → regime sign per TF.
- `PVTPoiseFlag`: PVT momentum building vs fading (exact value range TBD — grab from first live payload).
- `_1226` fields: MACD 12/26 trend/cross markers → **DISCARD entirely**.
- `_2452` fields: MACD 24/52 → **USE** (this is "MACD2"; matches CSV pair 2).
- DASHBOARD: **DPM** = Daily PVT/MACD, **2PM** = 2H PVT/MACD, **3PM** = 30m PVT/MACD,
  **WS** = Weekly Stochastic, **DS** = Daily Stochastic, **2S** = 2H Stochastic.
- `VOC` = Volume Oscillator reading (log-only initially; model later).
- Existing code already reads `data["INDICATORS"]["2H"]["PVTTrendFlag"]`, `["Trend_2452"]`, `["30M"][...]` — see `core/trade_logic.py:4674 process_TradingView_Data`.

## 4. Engine design & integration

### Module layout (build here)
```
forecasting/
  HANDOFF.md         # this file
  __init__.py
  store_builder.py   # offline: parse 5 CSVs -> data/insight/store.json
  regime.py          # snapshot INDICATORS -> regime vector
  levels.py          # level map builder
  engine.py          # ForecastEngine singleton; on_snapshot(data) -> forecast dict
  cli.py             # python -m forecasting.cli snapshot.json  (blind tests)
  score.py           # score forecasts jsonl vs actual bars from CSVs
data/insight/store.json      # generated (regime×level tables, confluence curves, setups, daily, meta)
data/forecasts/YYYY-MM-DD.jsonl  # runtime log: {snapshot, forecast} per tick
```

### store.json contents
- `levels`: per level family {VWAP, PDH, PDC, PDL, Round100, 30m E50/100/200, 2H E50/100/200, D E50/100, + 5m/1m EMA families} × 8 regimes × side(resist/support): `{n_touch, n_rej, rej_rate, next_bar_conf}`.
- `confluence`: factor-count curves (per level family where n allows; VWAP numbers in §2.4).
- `setups`: Setup A/B tables (§2.5) at 1m/5m granularity.
- `daily`: next-day tables (§2.6).
- `meta`: build ts, source file paths, row counts, MACD pair mapping note.

### regime.py mapping
- Per TF (5S,15S,1M,5M,30M,2H): sign = `PVTTrendFlag`; momentum modifier = `PVTPoiseFlag`; MACD2 state = `MACD_2452` vs `Signal_2452` (or `Trend_2452` flag); stoch = `K` vs `D` (or `KTrendFlag`).
- Daily regime: `SETUPS.DASHBOARD` DPM/DPMPVT + DS (fallback: previous-day D-PVT sign from D-CSV).
- Regime key for tables: `D{±}|2H{±}|30{±}`.

### levels.py mapping
- From snapshot: VWAP, all EMA stacks (1m/5m/30m/2H; D-EMAs from D-CSV), price.
- Computed: Round100 (nearest 100 above price = resistance, below = support — **both sides**),
  PDH/PDL/PDC + prior-week H/L (from D-CSV loaded at engine init).
- Rank by proximity to price; attach store probabilities.

### engine.py behavior
- Singleton (mirror `Trader_Singleton` pattern), lazy init on first webhook, **day-rollover reset**.
- `on_snapshot(data)`:
  1. Parse INDICATORS + SETUPS; update per-TF state (PVT sign flips = cross events; use `PVTCrossOver/UnderIndex` if they prove to be cross bar-indices).
  2. Detect Setup A / B (1M PVT cross-down + MACD_2452 cross-down within ±3 bars; 5M green vs 5M cross-down).
  3. Build level map; look up (level, regime, side) base rates; adjust by confluence count (≈+20pp/factor, capped, shrink toward base when n small); apply Poise modifier.
  4. Emit forecast dict: `bias (+%)`, `regime`, `resistance[]`, `support[]` (level, type, confidence %, basis), `expected_shape`, `invalidation`.
- Publish: return dict for `json_data["Forecast"]` + append `{ts, snapshot, forecast}` to `data/forecasts/YYYY-MM-DD.jsonl`.

### Integration point (minimal, non-invasive)
`core/trade_logic.py` → `process_TradingView_Data` (line ~4674):
```python
from forecasting.engine import forecast_engine   # lazy singleton
...
forecast = forecast_engine.on_snapshot(data)      # after trade_setups built
json_data["Forecast"] = forecast                  # rides existing Socket.IO emit (~line 4732)
```
No new transport, no server changes. Frontend may later render a panel from the same
`update_trading_view_data` event (deferred).

### Blind-test protocol
1. User pastes a past day's snapshot JSON (no date revealed) → `cli.py` → rest-of-day forecast.
2. User reveals date → `score.py` pulls actual 30m/1m bars from PAInsight CSVs → hit/miss report per level and bias.
3. Feed results back into store rebuilds.

## 5. Open items
1. `PVTPoiseFlag` / `ZCross` / `X_2452` / `Y_2452` / `PVTCrossOverIndex|UnderIndex` exact value ranges — infer from first live payloads (engine logs everything, so this is low-risk).
2. Round-100 support-side stats missing in research — compute in store_builder.
3. 5S/15S have no historical stats — accumulate from jsonl logs, fold into future rebuilds.
4. VOC: log-only until enough data.
5. UI panel: deferred.
6. PVT scale mismatch CSV (±1) vs payload (raw cumulative) — signs/crosses only; never compare magnitudes across sources.

## 6. Build order (suggested)
1. `store_builder.py` + store.json → sanity-check against §2 numbers.
2. `regime.py`, `levels.py`, `engine.py` (+ unit smoke test with a hand-built snapshot from today's CSV values).
3. Integration into `process_TradingView_Data` + jsonl logging.
4. `cli.py` + `score.py`; run one blind test.

---

## 7. BUILD STATUS (2026-09-25) — all four steps implemented

1. **store_builder.py** — done. `python -m forecasting.store_builder --check` → 112/112
   reference checks pass. `data/insight/store.json` generated (gitignored; regenerable).
2. **regime.py / levels.py / engine.py** — done. `python -m forecasting.smoke_test` passes
   (hand-built snapshot from latest CSV values). Engine = lazy singleton
   (`forecast_engine`), day-rollover reset, jsonl logging to `data/forecasts/`.
3. **Integration** — done. `core/trade_logic.py → process_TradingView_Data` sets
   `json_data["Forecast"] = forecast_engine.on_snapshot(data)` (failure-contained try/except);
   rides the existing `update_trading_view_data` Socket.IO emit.
4. **cli.py + score.py** — done. `python -m forecasting.cli snap.json [--date YYYY-MM-DD]`
   for blind tests (set_day pins daily context + disables logging);
   `python -m forecasting.score data/forecasts/DATE.jsonl` scores bias + per-level
   touch/reject/hold vs actual 1m bars. End-to-end replay of 2026-09-24
   (26 synthetic snapshots from CSVs) scored: bias 23/26, per-level report sane.

### Research corrections discovered during the build (verified by exact count matches)
- **§2.2/§2.3 PDH/PDC/PDL rows were MISLABELED.** Their "PDH" = previous-day **OPEN**,
  their "PDC" = previous-day **LOW**, their "PDL" = previous-day **HIGH** (exact touch-count
  matches in every regime, e.g. 44%[50]/30%[64] reproduced by prev-day-open). The store uses
  correct labels and adds a `PDOpen` family; `PDC` (prev close) is new (no research row).
- **§2.1 regime prevalence** could not be reproduced exactly with the (touch-verified)
  regime recipe (D=prev-day D-PVT, 2H=containing bar, 30=bar). Store recomputes prevalence
  with the verified recipe; treat §2.1 as approximate.
- **§2.5**: "next-bar confirmed" = next 1m/5m bar closes beyond the REJECTED bar's own close
  (matches 349/205/136/69/25/13 within ±2); `per_day` = episodes/days; "relaxed" counts are
  ALL signals (superset of strict): A 15/59 ✓ exact; B 4/12 vs our consistent 4/16
  (research B counting was internally inconsistent — documented in store meta).
- Definitions that reproduce the research EXACTLY (touch tables, confluence, daily n/up/median):
  resistance touch = open < L and high >= L, reject = close < L; support touch = open > L and
  low <= L, hold = close > L; regime 2H sign = the 2H bar CONTAINING the 30m bar (look-ahead
  within the 2H bar — matches research); Round100 = next 100-multiple beyond the bar open.

### Runtime notes
- `PVTPoiseFlag`/`ZCross`/`VOC`: logged in the jsonl snapshots, no model yet (open items 1/4).
- Store paths default to `data/insight/store.json` + `data/insight/csv/` (configurable).
- Python: stdlib-only; runs on both system 3.9 and app venv 3.12.
