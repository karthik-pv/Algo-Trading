> **[SUPERSEDED 2026-10-31]** This file remains the canonical VOCABULARY only. The
> operational handoff (current rules, mined insights, data state) is now
> `handoffs/FORECASTING.md` — start there; the insight sections below are mirrored
> there with `[STATS]` tags.

# Glossary — shared nomenclature

Scope: the vocabulary for the **Forecasting**, **Tracking** and **Commentary** modules.
Derived from the **Forecast** sessions ("Forecasting trading setups for scalping",
"Forecasting good 1PM Swing conditions") + `handoffs/archive/PAInsight_forecasting_engine.md` +
`handoffs/archive/SPATT.md`.
This is the canonical vocabulary for all Tracking / Commentary entries. If a term is used
differently anywhere, this file wins.

---

## Trend (market state — one per window)

| Term | Meaning | xlsx fill |
|---|---|---|
| `UT` | Uptrend | dark green `#548235` |
| `DT` | Downtrend | dark red `#C00000` |
| `RT_UP` | Retracement up | light green `#C6EFCE` |
| `RT_DOWN` | Retracement down | light red `#FFC7CE` |

**Precedence when assigning trend:** `3S_5PM_CV` → `3PMX` → `3PM`
(3S_5PM_CV wins even if 3PM points the other way; 3PMX wins whenever the cross index is 1–13 same-day.)

## Setup acronyms — Library v1 (setup_library.json)

| Acronym | Setup Name | Trend fit |
|---|---|---|
| TDY | Trend Day Ride | UT / DT |
| TCP | Trend Continuation Pullback | UT / DT |
| VWR | VWAP Reject Fade | RT (in DT) |
| VWH | VWAP Hold Bounce | RD (in UT) |
| ORB | Opening Range Breakout | UT / DT |
| PDX | PDH/PDL Sweep & Reclaim | RT / RD |
| SQZ | Squeeze / Compression Breakout | UT / DT |
| TRP | Failed Breakout / Trap Reversal | RD / RT |
| RNG | Range Fade | — (no trend) |
| GAP | Gap Fill | UT / DT |

## Setup acronyms — v2 trend-state classes (setup_occurrences_v2)

| Acronym | Rule | Trend |
|---|---|---|
| `3S_5PM_CV` | 3S retracing up (30m Stoch rising, 30m PVT still red) + 5PM up + close above VWAP → RT_UP; mirrored → RT_DOWN | RT_UP / RT_DOWN |
| `3S` | 30m Stoch turning, 30m PVT still red (stoch leads) | RT_UP / RT_DOWN |
| `3SP` | 30m Stoch AND 30m PVT both turning (confirmed) | UT / DT |
| `3PMX` | 3P/3M1/3M2 crossed up/down, index 1–13 candles since cross, **same-day credit only** (next day degrades to 3PM if P/M2 still trending) | UT / DT |
| `3PM` | 3P and 3M2 both up/down — no cross credit | UT / DT |

## Set Ups — momentor states (per TF)

State setups: name = `<TF><indicator>`. **Direction is not in the name** — it lives in the Trend column (UT/DT); the same setup name serves both directions. "Uptrending" defaults to the payload's Trend flags (Downtrending = the −1 mirror):

| Set Up (5m) | Uptrending state |
|---|---|
| `5P` | 5m PVT uptrending — above zero (`PVTTrendFlag` +1) |
| `5M1` | 5m MACD1 (12/26) UT — above signal (`Trend_1226` +1) |
| `5M2` | 5m MACD2 (24/52) UT — above signal (`Trend_2452` +1) |
| `5S` | 5m Stochastic UT — K>D (`KTrendFlag` +1) and rising (`StochPoiseFlag` +1) |
| `5SPM` | **5S + 5P + 5M1 + 5M2 all uptrending** (full 5m alignment) |
| `5O` | 5m VOC above the zero line — **no Downtrend mirror** (VOC-below-zero is not a setup) |

Same pattern per TF:

- **1m:** `1P` / `1M` / `1S` / `1SPM` / `1O` — **1M is always the 24/52 pair; 1M1/1M2 are never distinguished** → `1SPM` = 1m Stoch/PVT/MACD2 all uptrending
- **2H:** `2P` / `2M1` / `2M2` / `2S` / `2SPM` / `2O`
- **D:** `DP` / `DM1` / `DM2` / `DS` / `DSPM` / `DO`
- **W:** `WP` / `WM1` / `WM2` / `WS` / `WSPM` / `WO`
- **30m:** keeps its Forecast-session family (`3P` / `3S` / `3SP` / `3M1` / `3M2` / `3PM` / `3PMX`)

These are **states** (no cross required) — the cross events (`5PX`, `3M2X`, `3MX` = both together, …) live in Cross-event naming above. Multi-TF playbook setups (23PM_5PX etc.) = logical ANDs of these states + a trigger cross.

Data availability: `1O`/`5O`/`3OX`/`2OX` tracked (VOC in exports) · `DS` from the D file · `2S`/`DO` pending export plots · `WS` limited. The `30m 5m 1m` export adds `1S`/`5S`/`5M1` (map below).

## Indicator shorthand

- **PVT/MACD state per TF:** `DPM` (Daily) · `2PM` (2H) · `3PM` (30m) · `5PM` (5m) · `1PM` (1m) · `WPM` (Weekly)
- **Stoch per TF:** `DS` (Daily) · `2S` (2H) · `3S` (30m) · `WS` (Weekly)
- **CV** = Close vs VWAP · **MACD_2452** = the 24/52 pair (USE) · **M1** = 12/26 pair (DISCARD) · **M2** = 24/52
- **3M1/3M2, 2M1/2M2, DM1/DM2** = MACD pairs per TF (Numbers-doc vocabulary)
- **OSZ** = Oversold Zone (30m Stoch K<25) · **Cross credit / index** = bars since MACD cross (1–13)
- **IS1** (execution layer) = 5PM · CV · 3S · 3PM · 2S
- **IS2** (context layer) = 2PM · DS · DPM · WS · WPM

## Regime & levels

- Regime notation: `D−|2H+|30−` — PVT signs per TF; **D = previous day's D-PVT sign**
- **PDH / PDL / PDC / PDOpen** = prior-day high / low / close / open · **Round100** = 100-pt NIFTY multiples (23000, 23100, …)
- EMA rungs: `3E50` = 30m EMA50, `5E200` = 5m EMA200, `2HE50` = 2H EMA50, `1E100` = 1m EMA100, `D E100` = Daily EMA100
- Major rung set for Price Action labels: 1E100, 1E200, 3E50, 3E100, 5E50, 5E100, 5E200
- **Touch** = high ≥ level · **Reject** = close back below · **Hold** = close back above · **FT** = follow-through (Y/N, next-30m-bar)
- In the 2PMM, `RLevels` and `SLevels` are RUN-DERIVED (one run per event). For a DOWN
  run: `RLevels` = the TRIGGER ZONE — rungs whose rejection started the fall (touched
  and rejected during the rise into it) AND within one event-candle-range of the rise's
  swing high (threshold = mean 5m bar range of the 5 candles ending at the turn; several
  rungs can qualify), highest first. `SLevels` = the rung that STOPPED the fall —
  bracketing the fall's last candle (low ≤ rung ≤ close there; the 5m MACD2 cross-up at
  that candle confirms; `+` joins an exact confluence). UP runs mirror: `SLevels` =
  support zone that triggered the rise (within one event-candle-range of the fall's
  swing low), `RLevels` = the rung holding the rise's last candle (MACD2 cross-down
  confirms).
- 2PMM runs are ONE signed `Run` column per event: positive = uprun, negative =
  downrun (1 decimal). Direction = the first 5m MACD2 cross after the event, else the
  net event-close → session-close move; the confirming endpoint = the next cross in the
  same direction, else the session's last bar (unconfirmed moves run to the close).
  DRun = held support − swing high (negative); URun = held resistance − swing low.
  `Levels Crossed` = for a down run, the rungs BELOW the trigger zone price crossed down
  through during the fall (run low → zone bottom); mirrored for up runs; excluding
  levels named in SLevels/RLevels.
- 2PMM ladder (2026-10-26): base rungs + the 56 TradingView PSR series — `D-P…Q-S3`
  (Standard) and `Dh-P…Qh-S3` (Heikin Ashi), from the canonical's appended
  columns. Pivots are NOT house-computed — TradingView's own plotted values are
  the pivot ladder (the `TV-` prefix marks them; the old house `D-P…M-S4` set is
  retired, including its R4/S4 depth which TV does not plot).
- Window format: `Date | Start→End` in 30m steps; same-class windows <10 min apart merge

## Commentary vocabulary (1PM Swing session)

- Afternoon classification: **SWING** / **STOCH-CHOP** / **DEAD** (base rates 40/43/17%)
- **1PM Swing** = net 13:15→close move ≥0.25% (~60 pts), one-sided ≥45% efficiency (**eff**)
- **1m Stoch Swing day** = chop day; scalp 1m stoch crosses instead of holding a directional swing
- Tension (regime flip / PVT curl against trend) → SWING; full alignment by 13:00 → CHOP/DEAD
- **Fell from** = nearest level above window close that the window high touched

## Playbook setups (SPATT, setup_pa_tradecase.json — 593 rows)

| Setup | Trigger | Exit | Tradecase |
|---|---|---|---|
| 23PM_5PM | 2PM+3PM+5m PVT/MACD all aligned (hand-marked phases) | — | SCALP_1SPX_15PM |
| 23PM_5PX | 2PM up + 3PM up, 5m PVT cross-up | first 5m close below own **EMA4** | SCALP_23PM_5PX |
| 2PM_3PX | 2PM up at a 30m PVT cross-up | first 5m close below own **EMA21** | INTRA_5PM |
| ↓ mirrors | PVT/MACD all down, cross-down trigger | 5m close above own EMA4/EMA21 | same per family |

All exits EOD-flat at 15:25. "Points Moved" = move in trend direction (positive = worked).

## Execution horizons

`ultra-scalp` / `scalp` / `intraday` — the three TradeCase boxes per setup.

---

## 15s export column map (`NSE_NIFTY1!, 15S_*.csv` — 45 cols, verified vs 1m.csv 2026-10-18)

| Cols | Content |
|---|---|
| 1–5 | time (15s stamps), OHLC |
| 6–7 | 15s EMA9 / EMA21 |
| 8–12 | 1m EMA9/21/50/100/200 |
| 13–15 | 5m EMA50/100/200 |
| 16–18 | 30m EMA50/100/200 |
| 19–21 | 2H EMA50/100/200 |
| 22–27 | D/W/M EMA100/200 |
| 28 | VWAP |
| 29–30 | **15s MACD** (pair 1 — the fast pair) |
| 31–32 | **1m MACD** (pair 2, 24/52; carried within the 1m bar) |
| 33 | **1m VOC** (header says "Plot"; 15s-resolution, runs wider than 1m.csv's) |
| 34–37 | **1m PVT** + Plot + Cross Up/Down markers |
| 38–39 | **1m Stoch K/D** |
| 40–41 | **5m MACD** (fills on 5m closes, carried) |
| 42–45 | **5m PVT** + Plot + Cross Up/Down markers |

User's pane scan order (read right→left): 5m PVT → 5m MACD → 1m Stoch → 1m PVT → 1m VOC → 1m MACD → 15s MACD.
No 5m Stoch / 5m VOC in this export. File lives in `data/insight/csv/` (copied from Downloads 2026-10-18).

## `30m 5m 1m` export column map (`NSE_NIFTY1!, 30m 5m 1m.csv` — 57 cols, verified 2026-10-19)

1m bars 2026-08-03 → present. User's pane scan order (right→left): **30m VOC (in 5m mode) ·
30m MACD2 / MACD1 / Stoch / PVT · 5m MACD2 / MACD1 / Stoch / PVT / VOC · 1m Stoch / PVT / VOC /
MACD2 · chart (EMAs + VWAP + levels)**.

| Cols | Content |
|---|---|
| 1–5 | time (1m stamps), OHLC |
| 6–18 | 1m EMA9/21/50/100/200 · 5m/30m/2H EMA50/100/200 |
| 19–24 | D/W/M EMA100/200 — **EMPTY in this export** (empty-column trap applies) |
| 25 | VWAP (session) |
| 26–27 | **1m MACD2** (24/52) — no 1m M1 in this export |
| 28 | 1m VOC |
| 29–32 | 1m PVT + dead Plot (const 0) + PVT Cross Up/Down markers |
| 33–34 | 1m Stoch K/D |
| 35 | 5m VOC |
| 36–39 | 5m PVT + dead Plot + markers — carried on EVERY 1m bar (×5 repeats, no-repaint) |
| 40–41 | 5m Stoch K/D (5m-close stamps `:19/:24/…`) |
| 42–45 | 5m MACD1 (12/26) then MACD2 (24/52) |
| 46–49 | 30m PVT + dead Plot + markers — carried on every 1m bar (×30 repeats) |
| 50–55 | 30m Stoch K/D · 30m MACD1 (12/26) · MACD2 (24/52) — populated ONLY at 30m closes (`:44/:14` stamps, 13/day) |
| 56 | **30m VOC "in 5m mode"** = 5m-computed with periods ×6 (the 2H-VOC pattern, user-confirmed), updating on **every 5m close** (range seen −32…+40) |

Verified semantics (2026-10-19): M1 = 12/26 / M2 = 24/52 by the median-|MACD| ratio check —
same convention as the 5m file (M1 discard, M2 use) · PVT Cross Up/Down markers = PVT zero-cross
events (`0` on event bars; a stale carry marker can ride the 09:15 row — verify via sign flips)
· 5m/30m PVT columns mix raw normalized values with ±1 saturation flags — signs & zero-crosses
only (the handoff rule).

**Adds vs the 5m file:** 1S (1m Stoch), 5S, 5M1 (5M1X now callable), 1m-resolution no-repaint
carries of 5m/30m PVT. **Still missing:** 2H Stoch/VOC, D Stoch/VOC, D/W/M EMAs, 2H/D PVT —
`2SX`/`DSX`/`DOX` stay pending and the regime's D/2H components must come from the 5m file or logs.

## Cross-event naming — the full momentor net (confirmed against `MOMENTOR_COLS`, trend_insight.py)

| Momentor | Meaning | Tracked from |
|---|---|---|
| `3PX` / `3SX` / `3OX` | 30m PVT sign · Stoch K vs D · VOC sign | 5m file, true 30m closes only (`:40/:10` stamps; `:15` readings are intrabar recomputes — never call a cross off them) |
| `3M1X` | 30m MACD **12/26** cross alone | 5m file (M1 pair) |
| `3M2X` | 30m MACD **24/52** cross alone | 5m file (M2 pair) |
| `3MX` | **both 3M1X and 3M2X happening together** (same close) — user definition, 2026-10-18. Never use `3MX` for a single-pair cross — spell out `3M1X` or `3M2X` | 5m file |
| `2PX` / `2M1X` / `2M2X` / `2MX` / `2OX` | 2H same family (2MX = both together); 2H VOC updates per 30m step (not per 2H close) | 5m file (2SX pending — no 2H Stoch in export) |
| `DPX` / `DM1X` / `DM2X` / `DMX` | Daily same family (DMX = both together; D-PVT carried = previous day's final) | 5m file D block (DSX/DOX pending) |
| `1PX` / `1MX` / `1m VOCross` / `1SX` | 1m micro layer (1MX here = the 1m 24/52 cross — tracking only, not a SPATT momentor) | 1m.csv |
| `5PX` / `5MX` / `5m VOCross` | 5m layer (5MX = the 5m 24/52 cross — no 5m M1 in any export) | 1m.csv carried / 5m file |

- `3PMX` (the v2 setup acronym, credit 1–13) is a **state** triggered by a same-day `3M2X` UP — never call the composite without naming the trigger event.
- Ages: 30m counts the cross candle as 1 (valid 1–13); 2H/D count it as 0 (valid 0–4); outside → NA.
- Not callable: 1M1X (no 1m M1 in any export) · 2SX / DSX / DOX (plots not yet added to the TradingView export). 5M1X is callable from the `30m 5m 1m` export only.
- **Module note:** the SPATT engine's `MX` momentor column (`compute_momentor_ages`) tracks the M2-pair state/age — in commentary the user's combined definition above wins; a log line meaning the M2-pair cross alone says `3M2X`.
- **Empty-column trap:** HTF MACD/Stoch/VOC are populated ONLY at the TF's closing bars — before the day's first 2H close (11:15) those columns are empty; never read "+/−" from empty (the `sgn(False)==0` trap) — carry the previous day's final TF state instead.

**Level interactions — all five called, all levels:** **approaching** (Δ ≤ ~12 pts, 1m EMAs excluded — price hugs them) · **crossed above** / **crossed below** / **took support at** / **faced resistance at**, plus the 1m EMA21 retrace-support (wick touch). Ladder: 1E9/1E21/1E50/1E100/1E200 · 5E4/5E9/5E21/5E50/5E100/5E200 · 3E50/3E100/3E200 · 2E50/2E100/2E200 · DE100/DE200 (+ W/M if touched) · VWAP · Round100s · PDH/PDL/PDC/PDOpen. Tight clusters print as one line; same level+event deduped 15 min.

## LEVELS from the TradingView webhook payload (new 2026-10-18 — see `HANDOFF_TradingView_Algo_JSON.md` §5)

**ALL OHLC-derived level families are computed on HEIKIN-ASHI OHLC of the reference period** (user-confirmed 2026-10-18) — not raw OHLC. This includes PDH/PDL/PDC: those names mean the HA-previous-day values, not the printed highs/lows. HA chain per TF: HA-C = (O+H+L+C)/4; HA-O = (prev HA-O + prev HA-C)/2; HA-H = max(H, HA-O, HA-C); HA-L = min(L, HA-O, HA-C). In a falling period the HA-O rides above the raw highs — HA-H = that HA-O ceiling (e.g. W rows read PWH 23129 vs raw 22830).

Live days: parse `LEVELS.CW_CD` (main table) + `CW_ND`/`NW_ND` (projections) directly — sorted desc, split at the `Close` row for nearest resistance/support; `Distance` field = pts away. Backfills: computed from HA chains (verified vs the live table to within ~6–18 pts; the residual = chain history depth — TradingView chains from full history, backfill seeds Aug 3).

| Family | Names | Formula (all on **HA-OCHL**) |
|---|---|---|
| Floor pivots (prev completed period) | `D P`, `R1–R4 D`, `S1–S4 D` · `W P`, `W R1–R3`, `W S1–S3` · `M P`… · `Q P`… | P=(HA-H+HA-L+HA-C)/3; R1=2P−HA-L; S1=2P−HA-H; R2=P+(HA-H−HA-L); S2=P−(HA-H−HA-L); R3=2P+HA-H−2·HA-L; S3=2P−(2·HA-H−HA-L); R4=3P+HA-H−3·HA-L; S4=3P−(3·HA-H−HA-L) |
| Next-period projections | `D P`, `D R1–R3`, `D S1–S3` (CW_ND) · `W P`… (NW_ND) | same formula on the **developing** period's HA |
| Camarilla (prev day) | `Cam R1–R4`, `Cam S1–S4` | HA-PDC ± (HA-PDH−HA-PDL)×1.1/{12,6,4,2} |
| Fib (prev day range) | `Fib 38.2/50/61.8 D` | HA-PDL + HA-range×{0.382, 0.5, 0.618} (low-anchored) |
| Round numbers | `RN −` / `RN +` | floor/ceil(close/50)×50 — price-based, no HA (tracked as the 50-pt ladder) |
| Opening range | `ORH` / `ORL` | first 15 min H/L (today's raw range) |
| VWAP | `VWAP D`, `VWAP W` | volume-based — NOT HA (VWAP W payload-only in backfills) |
| Prior periods | `PWH/PWL/PWC` · `PMH/PML` | prev week / month **HA** H·L·C (PWH = the HA-high ceiling, not the printed high!) |
| EMAs | `1m/5m/30m/2h EMA50/100/200`, `D EMA100/200`, `W EMA100/200` | the chart's close-based EMAs (same values as the exports) |

R5/S5 default off. Unknown `Name`s: index by name gracefully (inputs change the mix). Q-pivots in backfills approximate (July 1m data missing).

## Scalping model — the 5m PVT switch (user, 2026-10-18)

**5m PVT > 0 = upmove for scalping** (mirror for < 0). The long frame, in order:

1. **Entry look:** price rising **from 1E21 / 1E50**, with **1m Stoch / 1m PVT cross up**, and the **15m PVT/MACD swing** pointing with you (15m MACD = computable from 1m closes; 15m PVT = volume-based, payload-only).
2. **Volatile variant:** price may slice **below 1E21/1E50 → 1E100 → 1E200 + any other level nearby** (payload levels in confluence — RN, PDH/PDC, pivots).
3. **The reclaim rule:** if **5m PVT is still >> 0** (a marginal dip, not a collapse), price **comes back up and goes above 1E50** — the dip is a scalp entry, not a regime change. If the 5m PVT flips decisively negative, the upmove frame is off.

Dip depth ∝ 5m PVT state: marginal flip → the full ladder sweep to 1E200 + nearby-level confluence; 5m PVT positive → 1E21/1E50 hold and reclaim.

## Replay insights — 1m×5m×30m correlations (mined 2026-10-19, full Aug 3→Oct 19 sample of the `30m 5m 1m` export)

1. **Bare 1PX/1MX flips are noise** — median adverse excursion ~4–5 pts (30-bar window), 80 %+ stay within 15 pts, ~⅔ of 1m PVT flips reclaim within 10 bars, regardless of 5m PVT sign. Never a trend start alone (the 1PME50 gate stands).
2. **1PME50 UP against 5m PVT − = fade bait** — forward 30 bars: median −6.2 pts, P(up) 40 % (n=227). With 5m PVT +: ≈flat (−0.7, 47 %, n=38). Only the 5m-aligned version pays.
3. **VOC flood semantics — the strongest edge found**: 1m VOC > +40 on a DOWN bar with 5m PVT − → **distribution, continuation is the base case** (fwd-10 median −4.3, P(up) 39 %, n=285). The same flood with 5m PVT + → absorption, the flush fails (+0.8, 52 %). Floods on up-bars ≈ neutral.
4. **1m Stoch extremes are 5-minute scalps only** — OS & 3M2+ → median +2.1 in 5 bars (57 % up); the edge is gone by 15 bars. OB fades work mildly under 3M2 − (45 % up at 15 bars).
5. **1SX UP pays only with 5m PVT +** (fwd-10 +0.8, 52 %) vs flat-to-negative against it (−0.3, 49 %).
6. **5m-PVT+ 1m dips slice the ladder deep but reclaim** — 85 % pierce 1E50, 62 % pierce 1E200 before the 1m PVT flips back; still ⅔ reclaim ≤10 bars. The dip = scalp-entry rule, quantified.
7. **VWAP dips reclaim fast by default** — median ~4 bars below, only 6–8 % still below after 30 min. A CV-below persisting >30 min is the distribution-day marker (Oct 19 qualified at 09:56).
8. **The 30m M1/M2 poise rule (user, 2026-10-19) — the primary input for the 1PME50 swing-direction forecast**: when 3PVT is green but *retracing* while **3M1 is down and/or 3M2 is poised down** (diff still >0 but compressing ≥3 closes) or crossed down, the 1PME50 swing direction is **DOWN** — the day resolves down after the morning consolidation: 1m UP attempts are fades, and once 3PX confirms, no looking up; 5m PVT then stays red for an extended run. **Mirror for 3M1 up / 3M2 poised up** (even under a red-but-curling 3PVT): the same 1m structures become buyable. The 3PVT sign alone is the *weakest* of the three tells — momentum (M1/M2 poise) leads, PVT confirms.

## 1m micro calls (tracking layer, user 2026-10-18)

| Call | Definition |
|---|---|
| `5m PVT Cross Down` | the 5m PVT cross-down event (= `5PX has happened — DOWN`) |
| `1PX Up` / `1PX Down` | 1m PVT cross up / down (the bare PVT flip alone is just this event — **not** a trend start) |
| `1PMX Up` / `1PMX Down` | **1m PVT + 1m MACD crossing together** (same close) — the 1m composite cross |
| `1PME50 Trend Starts` | **the trend-start call**: within a 1PM uptrend (1m PVT/MACD up), the moment **price goes above 1E50** — the 1E50 cross is what confirms the trend start. Until price takes 1E50, a 1PX Up is only a candidate. Mirror for the downtrend (price below 1E50). |

Tracking consequence: the old "1PM uptrend starts" line fires on the PVT flip alone — from now on the trend-start label is `1PME50 Trend Starts` and requires the 1E50 cross on top of the 1PM state. The nearby-levels lookahead (style rule 4) attaches to the `1PME50` call.

## Commentary style rules (Tracking session, user-specified 2026-10-18)

1. **Track the setup states by name** — 2PM / 3PM / 5PM (+1PM, DPM) with sign and M2 status (`3P+ 3M2↓`), carried no-repaint.
2. **Every level interaction is called out explicitly**: "crossed the level — PDC (22994) up/down", "price hits VWAP — acting as resistance", "price retraced to 1E21 and took support".
3. **Cross events get their own lines**: "1PM uptrend starts…", "5PX has happened — UP/DOWN", "5m VOCross up/down", "1M/5M M2 cross up/down".
4. **At every 1PM / 15PM trend start, list the nearby levels it could cross** (Round100s, PDH/PDL/PDC, VWAP, EMA rungs within ~120 pts) — proactive, before price gets there.
5. **Don't repeat the Nifty print unnecessarily** — level values and key extremes only (day high/low, closes at section boundaries); never a price on every line.
6. **Role reversals are always flagged** (morning resistance → afternoon support).
7. 15PM: not present in CSV exports — only trackable live from the 15s webhook feed.

## 5m export 2026-10-20 (`NSE_NIFTY1!, 5_c8600.csv` — 59 cols) — EXPORT TRAP + the 30/5/1 update

**The trap:** this 5m-chart export's MACD/PVT/Stoch panes are **D-resolved** — all four
indicator blocks show identical values (e.g. D MACD −388.7/−382.7), populating only at
day boundaries (MACD at the last row, PVT at 09:15). Useless for 5m/30m indicator columns.
Probably the wrong chart template / pane resolutions when exporting. Verify a new export's
blocks by change-frequency before trusting it (`scripts/verify2.py` pattern).

**What it IS good for (verified against the old files on the Aug 3→Sep 25 overlap):**
TRUE 5m HA OHLC (Oct 9→19) · 5m EMA4/9/21 (new) · 1m/5m/30m/2H EMA carries (= the containing
bar's final value, 1.0000) · D EMA100/200 + 2H carries = **frozen at the day's first minute**
(the chart-displayed state; the implied-close check proves it: Oct 19's carry implies the
first-minute print 22889, not the 22897 close) · session VWAP (5m-based).

**The 30/5/1 update (Sep 28 → Oct 19, `scripts/update_nifty_store.py --write`,
backups in `csv/backup_2026-10-20/`):** sources = the combined `30m 5m 1m` export
(verified blocks: the 5m MACD/Stoch/PVT populate on the 5m close-minute rows +4, the 30m on
+29; the old M1 = 12/26 discard, M2 = 24/52 use — the 1m pairs recompute EXACTLY from the
HA closes) + the 5m export (the true 5m bars, the D/2H EMA carries). Appended: 1m +5775,
5m +1147, 30m +195, 1D +15; `store.json` + `setup_occurrences_v2.csv` rebuilt.
**Known gaps in the appended rows:** 5m 15:30 bars missing Sep 28→Oct 8 (no 1m source rows
for 15:30-15:34); D EMA50 / W/M EMA columns empty (no source anywhere); 1D PVT/K/D empty
(volume / unresolved Stoch convention); the 1D close + 30m OHLC = 1m-bucket approximations
(~1 pt intraday, worse on gap opens); the 2H EMA200 in the combined export = warmup-divergent
→ continued from the old chain (validated 0.5 pt vs the 5m export). The 2H file still ends
Sep 25, the 15S file Oct 16.

**The "3M-crack morning" scan (`scripts/scan_morning_setup.py`,
`csv/morning_setup_30m_occurrences.csv`):** the user's 20 Oct morning state =
30m MACD1 < signal · MACD2 > signal but falling + the gap shrinking (2 bars) · PVT > 0 but
falling (2 bars, unsaturated). Calibrated: Oct 16 15:15 bar = the exact bell state; the
cross fired at the 09:45-stamped bar (closes 10:14:59) = the logged "3M2X DOWN 10:14".
Found: 34 bell-carried days (next day down 13/34; but Sep 2025+ regime: 6/9 down) and 98
intraday formations (rest-of-day negative 64/98, median −16 pts) — the intraday version is
the same-day fade tell; the bell version alone was a bull-era continuation signal until the
2025 regime change.

## The 5PM-pullback-1E50 validation (user insight, 20 Oct 2026 — `scripts/validate_1e_pullback.py`)

Setup: 30m PVT sign (last completed 30m bar) + 5PM (5m PVT sign + 5M2 vs signal) +
the touch-and-reclaim of the 1m-EMA rungs. Window 2025-10-20 → 2026-10-19, 5m bars;
the 1m-EMA rungs = span-matched EMAs over the 5m closes (1mE50→5mEMA10, 1mE100→5mEMA20,
1mE200→5mEMA40 — the decay-exact match; median error 1.2–1.7 pts vs the carried values —
the carried 1m EMA columns in the 5m file only populate from 2026-07-13, TradingView's
1m-history limit). Events: 3,737 (`csv/validate_1e_pullback_events.csv`).

**Validated:** (1) the 1E50 bounce rarely holds as touched — the strict 15-min hold 16–48%,
the soft (the close 3 bars beyond) 55–74%. (2) **The depth clause**: with 5P up but 5M2
retracing, the 1E50 strict-hold collapses 48%→19% (bull) and the touch extends to the
deeper rung (84% of the 1E100 touches happen in the 5M2-retracing state). (3) The BEAR
side end-to-end: the 1E50 rejection with 5M2 curling up → the EOD median **−12.3** (the
best cell); the 1E100/1E200 + the 5M2-curl-up entry → the next-30-min down 53%/58%,
EOD −8.6/−4.7.

**Not validated:** the bull-side deep-rung auto-bounce — the 1E100+retrace = a coin flip
(48% up, med −1.7, R/R 16.2 fav vs 20.2 adv); the **1E200 touch with 5M2 retracing =
the trend-fail warning** (EOD −11.5, max-adv 25.2 > max-fav 16.8), not an entry. The
bull's clean cell = the 1E50 touch with 5M2 still RISING (the soft-hold 74%, EOD +2.4).
The year = a bear year (26.4k→22.9k) — the bull cells are counter-trend; re-test by regime.

## Capture test — the 1E100/200 rise with 1PM / 15SW triggers (`scripts/validate_capture_1e100.py`, 20 Oct)

Question: with **3PM still up** (30m PVT>0 + 30m M2>signal, completed-bar carry) and
**5m MACD2 retracing** (still above signal but falling/gap-shrinking, last completed 5m
bar), can the rise off the 1E100/200 touch be captured with a 1PM (1m PVT>0 + 1m M2>signal
flip-on) or a 15m-MACD-swing trigger? Window = the 1m era (Jul 13 → Oct 19; the 1m PVT
exists only here), 1m resolution, exact carried 1m EMAs. Events: 112 × 1E100, 41 × 1E200
(one per pullback leg until the reclaim). 15m MACD = 24/52 over the 15m-close proxies
(the last 1m HA-close of the 15m window); **the 15m PVT = payload-only, not testable**.

| 1E100 entry | n | 30min up | 30min med | EOD med |
|---|---|---|---|---|
| T0 (the touch close) | 112 | 48% | −0.7 | −5.6 |
| 1PM flip-on | 68 | 53% | +0.7 | −10.5 |
| **15m swing (line>sig & rising)** | 82 | **57%** | **+3.7** | −1.3 |

1E200: too rare (41/21/16) — T0 EOD +17.4 (the deep-flush recovery), but the 15SW entry =
25% up, max-adv **44.7** — the swing confirmation arrives late at the deepest rung and
enters INTO the continuation. **Verdict: capture = a 30-min SCALP at 1E100 with the 15m
swing trigger (57%/+3.7); the 1PM adds little (53%); do NOT hold (the EOD ≈ flat/negative);
the 1E200 + the 15m-swing = the danger cell.** The EOD bleed (−1.3…−10.5) says the 3PM-up
+ 5M2-retrace context is itself fragile this era — the bounce = the exit, not a hold.

## Scalp refinement — the swing-FIRST entry (`scripts/validate_scalp_1pvt_15s.py`, 20 Oct)

The trigger-order test on the same event set (the 3PM up + the 5m M2 retracing + the
1E100/200 touch, Jul 13 → Oct 19, 1m): **the 15s-MACD swing = the EARLY trigger, the 1m
PVT cross = the LATE one** — the swing fires ~2 min after the touch, the PVT cross ~11 min.

| 1E100 entry | n | 30min up | 30min med | fav/adv | EOD med |
|---|---|---|---|---|---|
| T0 the touch close | 112 | 48% | −0.7 | 13.2/17.2 | −5.6 |
| **SWING alone (15s-proxy, ~2 min)** | 110 | **53%** | **+1.9** | **14.9/16.2** | −3.4 |
| XPVT (the 1m PVT cross, ~11 min) | 83 | 48% | −0.1 | 13.8/18.2 | −7.6 |
| PVT + swing together | 83 | 45% | −0.7 | 13.5/18.2 | −7.6 |

1E200 (n=41): the SWING entry = 46%, the fav/adv 14.6/13.6 (the R/R>1), the EOD +18.0
(the deep-flush recovery); waiting for the PVT = the worst cell (37%, −7.4).
**Verdict: the entry = the 15s-MACD swing (the timing tool); the 1m PVT cross = the
confirmation, not the trigger (waiting for it costs the edge). The 15s MACD itself =
validated only on Oct 14-16 (the one textbook leg: the swing +0.2-2.8 min → the +58-64
runs, the max-adv 1.8-19) — collect the 15s exports daily to build the sample.** The
proxy = MACD(3,6)+sig2 of the 1m closes (the span-match for the 15s 12/26/9).

## The 3PM-DT fade playbook — validated across days (`scripts/validate_3pm_dt_fades.py`, 20 Oct)

The Oct-19 frame (the 3PM downtrend → the retrace-ups die at the 1m rungs → the fall
triggers) tested on every 3PM-down window (the 30m PVT<0 + the 30m M2<signal, completed-bar
carry) in the 1m era (Jul 13 → Oct 19): **273 legs on 43 of 69 days**
(`csv/threepm_dt_fade_events.csv`).

- **The triggers (the fall ≥10 pts = win):** ① the 15s-MACD2-proxy cross-down alone = the
  workhorse (n=174, win 80%/70% by phase, the fall med 19.2/15.7) · ③ the 1m PVT X-DN +
  the 1m MACD2 X-DN together = the quality trigger (n=83, win 67% in the 5PM-down, **76% in
  the 5M2-retrace-up phase, the fall med 20.8**) · ② the 1m PVT + the proxy = n=16 (80% in
  the retrace-up phase, small n).
- **The phase-2 claim validated and stronger:** with the 5m M2 retracing up (the 5P red),
  the rallies STILL die — the T3 = 76% win / the fall med 20.8 — "the 5M2 retrace-up = still
  a sell" confirmed in the 3PM-down context.
- **The rung ladder:** the fades mostly die at the 1E9/1E21 already (n=180, the win
  74–81%, the 1E9 = the R/R 21.5/14.5); the 1E50+ = rarer (n=93) but 65–67%.
- **The expectancy** (the win% × the fall med − the loss% × the adv med): the T3-retrace-up
  ≈ +12, the T1-5PM-down ≈ +12, the T3-5PM-down ≈ +5, the 1E9 fades ≈ +13 — all positive,
  the adverse med 14–19 = the stop budget.
- **The day spread:** 36/43 days = the win-rate ≥50%; the best = the breakdown days
  (Oct 8: 12 legs 92%, the med 43.2 · Sep 16: 68.9 · Oct 1: 60.4); the chop-day failures =
  Aug 17, Oct 13 (0%, the med <8).

## FINAL export layout (2026-10-25 — the go-forward format for future downloads)

The user locked the chart layouts; future 5m/30m exports match byte-for-byte. Verified
2026-10-25 by value-identity on the overlap (PVTs bit-exact; D block bit-exact; the rest
float-noise from re-export).

**5m export — 60 cols** (canonical `5_Sep 2025-Oct 2026.csv`, 20,601 rows,
2025-09-15 → 2026-10-23): 0-4 time/OHLC · 5-20 EMAs (1m E100/E200, 5m E4/9/21/50/100/200,
30m/2H/D) · 21 VWAP · **5m block** MACD 22-23, VOC 24, PVT 25, markers 26-28, Stoch 29-30 ·
**30m block** VOC 31, Stoch 32-33, M1 34-35, M2 36-37, PVT 38, markers 39-41 ·
**2H block** M1 42-43, M2 44-45, VOC 46, PVT 47, markers 48-50 ·
**D block** M1 51-52, M2 53-54, **VOC 55 (the DO — new; updates per 30m step)**,
PVT 56, markers 57-59. Rows before 2025-09-22 carry D VOC empty (TV's export window
starts there; prepended from the 59-col layout). The 2026-10-22 14:40 5m bar (missing
from the old export) is healed.

**30m export — 58 cols** (canonical `30_42a03.csv`, 21,910 rows, 2020-01-01 → 2026-10-23):
blocks standardized to `M1, M2, Plot, PVT, dead-Plot, XU, XD, K, D` per TF —
**30m** @26-36 · **2H** @37-47 (Stoch moved AFTER the PVT block) ·
**D** @48-58 (**VOC 48 = the DO, 30m-computed** — new; M1 49-50, M2 51-52, PVT 53,
Stoch 57-58 moved to the end). read_tv_csv occurrence naming unchanged:
MACD_Line_1226/2452 = 3M1/3M2, _3/_4 = 2M1/2M2, _5/_6 = DM1/DM2; PVT/#2/#3 =
30m/2H/D; K/D, K#2/D#2, K#3/D#3 = 3S/2S/**DS (now confirmed live since 2020 —
the "DS absent" note above is superseded)**.

**VOC cadences (verified):** 5m VOC = every row (~3.3 zero-X/day) · 30m VOC = every 5m
close (the ×6 pattern) · 2H VOC = per 30m step · D VOC = per 30m step in both files
(scales differ per file — the volume series differs, the 30m file tracks the other
price/volume series; signs & zero-crosses only, the house rule).

**Note:** the 2026-10-25 downloads still end 2026-10-23 (a layout refresh, not a data
refresh) — Friday 2026-10-24's session arrives with the next export. Backups:
`csv/backup_2026-10-25/`.

## 2PMM — 2PM Matrix (module registered 2026-10-25)

The user's new module (session "2PMM"). **Ultimate goal:** mine the pattern of **when 3O
comes up — DEFINED = the 30m VOC crossing above the zero line (`3OX up` event)** — and its
price-moving direction; forecast from that pattern; capture the swings as Swing / Intra /
Scalp. Sequence: historical matrix → indicator correlations → pattern repetition (does
price action repeat when the trend setup repeats?) → forecasting. The full O-ladder
(5O/3O/2O/DO) + PM states + the 2S/DS Stoch layer feed the matrix; 3OX-up is the
centerpiece trigger.

### 2PMM level semantics (2026-10-25, revised 2026-10-26 — hybrid)

- **SL Levels (both event blocks)** — RUN-DERIVED (2026-10-26 semantics): for a down run,
  the ladder rung that STOPPED the fall — bracketing the fall's last candle (low ≤ rung ≤
  close there), the 5m MACD2 cross-up at that candle confirming the stop; '+' joins labels
  on an exact confluence (e.g. `PDH+VWAP`). Mirrored for up runs: the support ZONE that
  triggered the rise (touched-and-rejected rungs within one event-candle-range of the
  fall's swing low).
- **RL Levels (both event blocks)** — RUN-DERIVED trigger zone: for a down run, the rungs
  whose rejection started the fall — touched and rejected during the rise into it AND
  within one event-candle-range of the rise's swing high (threshold = mean 5m bar range
  of the 5 candles ending at the turn; several rungs can qualify), highest first. Mirrored
  for up runs: the single rung holding the rise's last candle (MACD2 cross-down confirms).
  Oct 23 09:30 now lists `ORH,3E200,R1-of-Q,2HE50,3E100,3E50` (see the matrix).
- **Levels Crossed (`<prefix>o_crossed`)** — for a down run, the rungs BELOW the trigger
  zone price crossed down through during the fall (run low → zone bottom); mirrored for
  up runs (zone top → run high); excluding levels named in SLevels/RLevels. The run = one
  direction per event, the first 5m MACD2 cross after the event.
  Ladder = PDH/PDL/PDC/PDOpen, ORH/ORL,
  1E/3E/5E rungs, VWAP, Round-100s, and the 56 TradingView PSR rungs (`D-P…Qh-S3`).
  Matrix is now **48 columns** (`3ov`/`3on` = the
  carried 30m VOC value + age, displayed `3o`/`3on`).
- **2PMM state cells** — MACD cells `X_n`/`Y_n`, n unsigned (Y = below zero, the sign
  is implicit); PVT (3p/2p/dp) 1 decimal; ages signed (+ = since up-cross, − = since
  down-cross, 0 = crossed this bucket).
- **2PMM level cells** — NAME-ONLY tokens, comma separated (`PDH+ORH`,
  `D-S2,R100|VWAP`): no Nifty price, no time; R100 dropped its price suffix
  (`R100-25300` → `R100`) and repeated tokens dedupe across the whole cell.
- **2PMM merged view** — the grid renders the two event blocks as one set of
  `ev_*` columns; each row shows its own event (3O row or 5O-only row). The `O`
  column filters **3o / 5o / All** (default 3o). Paired rows display as 3O rows;
  their 5O rider stays in the CSV only.
