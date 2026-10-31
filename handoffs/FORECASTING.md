# FORECASTING — the living handoff (single source of truth)

> **LIVING DOCUMENT** — updated in place by every forecasting session. Never dated, never
> rotated. History lives in `handoffs/archive/` (read-only); this file holds CURRENT TRUTH.
> Vocabulary: `forecasting/Glossary.md` (terms win over usage elsewhere).

Tag discipline: **[LOCKED]** = user-defined rule, no-repaint, don't reinterpret ·
**[STATS]** = mined edge (must carry n, window, mining date) · **[HYPOTHESIS]** = working
belief, needs validation before trading on it.

---

## 0. READ-ME-FIRST

- **The forecast target**: the `1PME50` swing — the 1m trend-start call [LOCKED]:
  within a 1PM uptrend (1m PVT + MACD2 24/52 both up), the moment price crosses above
  **1m EMA50** confirms the start. Mirror for down. Until the 1E50 cross, a 1PX flip is
  only a candidate.
- **The cascade** (how the layers stack, top-down):
  `DPM (daily) → 2PM (2H) → 3PM poise (30m, rule 8) → 5m PVT alignment → 1PME50 trigger`.
  Higher layers condition the lower ones; the bare 1PME50 trigger is weak (see §2.1).
- **Session protocol**: read this file → check the Changelog's latest entry → work →
  append a dated changelog entry (append-only, never rewrite past entries).
- **Current state (2026-10-31)**: 5m + 30m canonicals current through **Oct 30** (psr2
  layout); 1m canonical stale at Oct 26 (psr2 for 1m not yet implemented). October
  2026 = the holdout for forecasting; mining window = pre-Oct 2026.

## 1. THE LADDER (the PM layers)

| Layer | Definition | Source (authoritative) |
|---|---|---|
| `DPM` | D PVT sign + DM2 (24/52) state | 30m file D block (dense since 2020) |
| `2PM` | 2H PVT + 2M2 | 30m file 2H block (dense since 2020) |
| `3PM` / 3PMX | 30m PVT + 3M2 (+ same-day cross credit) | 5m/30m files |
| `5PM` | 5m PVT + 5M2 | 5m file |
| `1PM` / `1PME50` | 1m PVT + 1m MACD2 → the 1E50 cross | 1m file |

Regime notation `D−|2H+|30−` = PVT signs per TF (Glossary §Regime). Mixed PVT/MACD =
"Retracing". Momentum (M1/M2 poise) leads, PVT confirms [LOCKED, user rule 8, §2.3].

## 2. ASPECTS

### 2.1 1PME50 — the 1m trend-start swing (flagship aspect)

**Definition [LOCKED]**: event = 1PM state (1m PVT & MACD2 24/52 aligned) + close
crossing 1m EMA50. Study: `scripts/study_1pme50_1m.py` →
`data/insight/reports/1pme50_1m.md` (dev 2026-07-13..09-30, 630 events; Oct holdout 168).

**Mined edges [STATS]** (dev window unless noted; numbers = median fwd pts / P(up)):

- **Bare 1PME50 starts are weak**: eod +1.0 / 51% (n=630). UP starts eod −1.2/48% —
  the raw call is NOT tradeable alone.
- **The 5m PVT alignment is the filter**: UP start with 5m PVT+ = eod +10.2/58% (n=57);
  UP start with 5m PVT− = **fade bait** (dev n=270; Oct holdout n=70 → +3.0/56% —
  weaker than dev, treat as no-trend, not a fade).
- **3PM alignment is the swing context**: UP start inside 3PM-up = dev +3.8/65% @15min;
  UP start inside 3PM-down = −3.2/44% eod (n=189, fade bait). **Trade 1PME50 WITH 3PM
  only; counter-3PM starts = fade signals; 3PM-mixed = no-call.**
- **Rule-8 poise stack (strict)**: poise-UP strict eod +8.6/66% (n=68); loose +9.5/68%
  (n=91); neutral poise eod −2.6/46% (n=440).
- **October holdout highlights**: DOWN + 5m PVT− = eod **+100.8 / 85%** (n=13, small n
  but the best cell anywhere); poise-UP strict events eod +13.1/77% (n=31).
- **Instability warning**: the dev "before-10:00 fade" pattern (−6.7/37% @15, n=35)
  **flipped** in Oct (+62.8/91% @15, n=11). Time-of-day priors are fragile — re-verify
  before trusting any TOD slice.
- **Rule 8 as a standalone day-direction forecast is weak** (poise-UP calls hit 38% vs
  40% baseline dev) — it is a *conditioning* layer, not a forecast by itself.

**Conditioning layers in progress [HYPOTHESIS]**: 2PM1M2 and DPM1M2 states as extra
gates. Data unlocked 2026-10-31: the psr2 30m file carries **dense 2H/D MACD pairs
(2M1/2M2/DM1/DM2, 100% populated back to 2020)** — the 30m canonical is the
authoritative source (the 5m file's HTF blocks are sparse prints).

**Open**: (1) ingest the psr2 1m download (`1_f434f.csv`, 160-col — needs a psr2:1m
template) → 1m current + D-pivot cross-TF backfill; (2) wire 2PM/DPM layers into
`study_1pme50_1m.py`, measure incremental value vs rule 8; (3) the October day-by-day
forecast protocol (cascade §1, one call per day, holdout discipline).

### 2.2 3PM (30m) — poise & fade playbooks

- **Rule 8 [LOCKED, user 2026-10-19]**: 3PVT green-but-retracing with 3M1 down / 3M2
  poised down (diff >0 compressing ≥3 closes, or crossed down) ⇒ the 1PME50 swing
  direction is DOWN; the day resolves down after the morning consolidation; 1m UP
  attempts are fades; once 3PX confirms, no looking up; 5m PVT stays red. Mirror for up.
- **3PM-DT fade playbook [STATS]** (`scripts/validate_3pm_dt_fades.py`, 273 legs/43
  days, Jul–Oct 2026): triggers in a 3PM-down window — 15s-MACD2-proxy X-DN = the
  workhorse (n=174, win 70–80%, fall med 15.7–19.2); 1m PVT+MACD2 X-DN together = the
  quality trigger (76% in the 5M2-retrace-up phase, fall med 20.8). Fades die at
  1E9/1E21 mostly (win 74–81%); expectancy +5..+13, adverse med 14–19 = stop budget.
- **3M-crack morning** (`scripts/scan_morning_setup.py`): the intraday bell-state
  formation = same-day fade tell (rest-of-day negative 64/98, med −16); the bell-carried
  version was a bull-era continuation only (pre-2025 regime).

### 2.3 2PM (2H) & 2PMM

- 2PM up + 3PM up + 5m PVT cross-up = the `23PM_5PX` scalp (exit: first 5m close below
  own EMA4); 2PM up at a 30m PVT cross-up = `2PM_3PX` INTRA_5PM (exit 5m EMA21)
  [LOCKED, SPATT playbook].
- **2PMM module**: `forecasting/pmmEngine.py` — mines 3OX-up (30m VOC ×0) with full
  state/level/run context; matrix `data/insight/csv/2pmm_matrix.csv` (1,154 × 47);
  viewer at `/2pmm`. Run semantics: one signed `Run` column, run-derived SL/RL, TV-only
  pivot ladder (56 PSR series, no house pivots). Spec: the engine docstring (fullest)
  + Glossary §2PMM.
- **Sparse-block rule**: the 5m file's HTF blocks print only on update rows (30m on
  :10/:40, inside the closing bucket) — forward-fill before computing states; flip/age
  logic fires on the print row.
- Next 2PMM stages: correlations (state/level/syncher vs the signed Run) → pattern
  repetition → forecasting.

### 2.4 DPM (daily) & the VOC layer

- D block (PVT, DM1, DM2, DS, DO) rides the 30m file — dense since 2020. **DM1 (12/26)
  is KEPT, not discarded** [LOCKED, 2026-10-24 convention change]; M2 = 24/52 = the used
  pair everywhere else.
- **VOC flood semantics [STATS]** (strongest edge mined 2026-10-19): 1m VOC > +40 on a
  DOWN bar with 5m PVT− ⇒ distribution, continuation is the base case (fwd-10 −4.3/39%,
  n=285); the same flood with 5m PVT+ ⇒ absorption, the flush fails (+0.8/52%).
- 3OX-up cadence ≈ 1.27/day; 5OX-up ≈ 3.34/day.

### 2.5 Levels & PSR

- Pivot ladder = the 56 TV PSR series (`D-P…Q-S3` Standard + `Dh-*` HA), **no house
  pivots** [LOCKED 2026-10-26]. Name-only level tokens on the Grid.
- All OHLC-derived level families (PDH/PDL/PDC, W/M prior periods) = **Heikin-Ashi**
  values [LOCKED]. House-computed still: `5m EMA4`, `1E50` proxy, `5M1`, PDH/PDL/PDC/
  PDOpen (HA), ORH/ORL, R100.
- Level interactions: approach (Δ≤~12 pts) · crossed up/down · took support / faced
  resistance; touch = high ≥ level, reject = close back below, hold = close back above.
- Confluence gradient (VWAP reject-from-below by count): 10/33/50/73% — intact.

### 2.6 Style playbooks (the "how do I use this" view)

- **Scalping** (1m/5m execution): the 5m PVT switch [LOCKED] — 5m PVT>0 = upmove;
  entries off 1E21/1E50 holds with 1m Stoch/PVT cross up; the reclaim rule (marginal
  5m-PVT dip = scalp entry, decisive flip = frame off). Trigger order [STATS]: the
  15s-MACD swing is the EARLY trigger (~2 min), the 1m PVT cross the LATE confirmation
  (~11 min) — don't wait for PVT. 1m Stoch extremes = 5-minute scalps only. Deep-rung
  auto-bounce NOT validated (1E100+retrace = coin flip; 1E200 + 5M2 retrace = trend-fail
  warning, not entry).
- **Intra Swing** (30m/2H): 3PM/2PM poise sets direction; 1PME50 WITH-3PM = the swing
  entry; counter-3PM = fades (§2.2). Day-type frame: SWING/STOCH-CHOP/DEAD 40/43/17%;
  1PM Swing = 13:15→close ≥0.25%, ≥45% efficiency.

## 3. DATA & TOOLING

- **Canonicals** (`data/insight/csv/`, all self-consistent per-file; the 30m file tracks
  a slightly different price series — ±46 pts on closes; never cross-compare levels):
  - 5m `5_Sep 2025-Oct 2026.csv` — **20,986 × 121**, 2025-09-15 → 2026-10-30
  - 30m `30_42a03.csv` — **21,975 × 123**, 2020-01-01 → 2026-10-30
  - 1m `1_20adf.csv` — 27,955 × 94, stale at 2026-10-26 (**psr2:1m not yet implemented**;
    the new 1m download `1_f434f.csv` is 160-col psr2)
- **Ingestion**: `scripts/ingest_tv_export.py` — psr2 mode (2026-10-31 template, 5m 160 /
  30m 155 cols) with explicit per-TF block tables + content verification (hard-abort
  below the per-TF rate gate). psr2 quirks encoded: the 30m template hangs the NEXT TF's
  Stoch at each block's end (2H K/D inside the 30m block, D K/D inside the 2H block);
  W pivots = plain `W P` on the 30m chart, `(low TF)` on the 5m chart; D/W/M EMAs are a
  different TV flavor (kept as `(TV2)` extras; canonical columns stop accumulating);
  D Standard/HA pivots empty in the 30m export (refilled from the 1m's cross-TF fallback
  once psr2:1m runs). Idempotent only against the pre-ingest layout — restore from
  backup before a re-run.
- **MACD pair convention**: occurrence-based in `store_builder.read_tv_csv`
  (`MACD_Line_1226/2452` = 3M1/3M2, `_3/_4` = 2M1/2M2, `_5/_6` = DM1/DM2). PVT values =
  normalized — **signs and zero-crosses only** [LOCKED].
- **Store**: `python -m forecasting.store_builder` (+ `--check`). Sanity 99/112; the 13
  fails = stale REF constants (benign; re-pinning is a deliberate TODO).
- **Studies** (re-runnable, conclusions live in §2): `study_1pme50_1m.py` ·
  `pm_ladder.py` · `validate_3pm_dt_fades.py` · `validate_1e_pullback.py` ·
  `validate_capture_1e100.py` · `validate_scalp_1pvt_15s.py` · `scan_morning_setup.py`.
- **Holdout discipline**: pre-Oct-2026 = mining; October 2026 = forecast holdout. Never
  re-tune on October until the day-by-day protocol is committed.

## 4. CHANGELOG (append-only)

- **2026-10-31** — psr2 ingestion extended to 30m (`30_42a03.csv` → 21,975 × 123, through
  Oct 30; store rebuilt, smoke OK). 5m done earlier same day (20,986 × 121). 1PME50 study
  written (630 dev events + Oct holdout). Discovered the 30m psr2 block quirk (next-TF
  Stoch at block ends) and dense 2H/D MACD pairs → 2PM/DPM layers unblocked. Next: psr2:1m,
  2PM/DPM conditioning, October forecast protocol. Hub created (this file).
- **2026-10-26** — psr2 template first ingested (5m/30m/1m), run-derived SL/RL + single
  signed Run in 2PMM, TV-only pivot ladder, no house pivots. (archive:
  `2026-10-26_2pmm.md`)
- **2026-10-25** — 2PMM built (engine + matrix + Grid). (archive: `2026-10-25_2pmm.md`)
- **2026-10-24** — data refresh to Oct 23, full-history 30m, DM1 kept. (archive:
  `2026-10-24_data_refresh.md`)
- **2026-10-20** — PM ladder model + tiered validation. (archive: `2026-10-20_pm_ladder.md`)
- **2026-10-19** — replay insights mined (rules 1–8), 1PME50 gate defined. (archive:
  `SPATT.md` §insights, `forecasting/Glossary.md`)
- **2026-09-25/27** — engine research, store conventions, webhook schema. (archive:
  `PAInsight_forecasting_engine.md`, SPATT sessions)
