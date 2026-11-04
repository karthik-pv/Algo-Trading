# TRADE BACKTEST — the living handoff (validated studies on my trades/setups)

> **LIVING DOCUMENT** — updated in place. Every study here is re-runnable (the doc holds
> conclusions + the command; full tables in `data/insight/reports/` and the CSVs).
> Archive: `handoffs/archive/`.

Tag discipline: **[STATS]** = measured with n/window · **[HYPOTHESIS]** = not yet
validated. Holdout discipline: October 2026 = holdout — don't re-tune on it.

## 1. Validated studies (reverse-chronological)

### 1PME50 study (2026-10-31) — `scripts/study_1pme50_1m.py`
Dev 2026-07-13..09-30 (630 events) + Oct holdout (168). The bare call is weak; the
5m-PVT and 3PM filters are the tradeable edges. Full numbers: FORECASTING §2.1 and
`data/insight/reports/1pme50_1m.md`.

### 3PM-DT fade playbook (2026-10-20) — `scripts/validate_3pm_dt_fades.py`
273 legs / 43 of 69 days (1m era). Triggers: 15s-MACD2-proxy X-DN (win 70–80%),
1m PVT+MACD2 X-DN together (76% in the 5M2-retrace-up phase). Positive expectancy in
every phase (+5..+13), adverse med 14–19 = stop budget. Best on breakdown days;
fails on chop (Aug 17, Oct 13: 0%).

### Scalp trigger order (2026-10-20) — `scripts/validate_scalp_1pvt_15s.py`
The 15s-MACD swing = EARLY trigger (~2 min post-touch); the 1m PVT cross = LATE
confirmation (~11 min) — waiting for PVT costs the edge (37–48% vs 53% at 1E100).
The 15s MACD itself validated on Oct 14–16 only — **collect 15s exports daily**.

### Capture test at 1E100/200 (2026-10-20) — `scripts/validate_capture_1e100.py`
With 3PM up + 5m M2 retracing: 1E100 + 15m-swing trigger = 30-min scalp (57% up,
+3.7); 1PM trigger adds little; do NOT hold (EOD flat/negative). 1E200 + 15m swing =
the danger cell (25% up, max-adv 44.7).

### 5PM-pullback-1E50 validation (2026-10-20) — `scripts/validate_1e_pullback.py`
3,737 events, 1 year of 5m. The depth clause: 5P up + 5M2 retracing ⇒ 1E50 strict-hold
collapses 48→19% and the touch extends to 1E100 (84% of touches). Bear best cell: the
1E50 rejection with 5M2 curling up → EOD med −12.3. 1E200 + 5M2 retrace = trend-fail
warning. The year was bear — bull cells are counter-trend; **re-test by regime**.

### PM Ladder (2026-10-20) — `forecasting/pm_ladder.py`
The user's PM-ladder model: per-HTF PM regime → rung sets (D: 3E50/100/200; 2H/3PM:
5E50/100/200 and 1E100/200; 5PM: 1E9/21/50; 15PM: 15s PVT cross) with PVT zero-cross
as the trigger (MACD2 state = context, `✓M` tick within ±3 bars). Tiered validation:
full year for 5m layers, ~3 months 1m, ~2.5 sessions 15s.

### 3M-crack morning scan (2026-10-20) — `scripts/scan_morning_setup.py`
Intraday bell-state formation (3M1<signal · 3M2>signal falling · gap shrinking · PVT>0
falling) = same-day fade tell (rest-of-day negative 64/98, med −16). Bell-carried
version = bull-era continuation only (pre-2025 regime; 6/9 down since Sep 2025).

## 2. Playbook setups (SPATT, `setup_pa_tradecase.json` — 593 rows)

| Setup | Trigger | Exit | Tradecase |
|---|---|---|---|
| 23PM_5PM | 2PM+3PM+5m PVT/MACD all aligned | — | SCALP_1SPX_15PM |
| 23PM_5PX | 2PM up + 3PM up, 5m PVT cross-up | first 5m close below own EMA4 | SCALP_23PM_5PX |
| 2PM_3PX | 2PM up at a 30m PVT cross-up | first 5m close below own EMA21 | INTRA_5PM |
| ↓ mirrors | mirrored down | 5m close above EMA4/EMA21 | same per family |

All exits EOD-flat at 15:25. "Points Moved" = move in trend direction.

## 3. Not validated / killed (do not re-litigate without new data)

- The joint PVT+MACD cross as the ladder trigger (killed by the tiered validation —
  PVT zero-cross won).
- The withdrawn 1E50-proxy run_points model (applied to the replaced run semantics only).
- The positional-SL / rejection-window-RL level model (superseded 2026-10-26 by the
  run-derived hybrid — see FORECASTING §2.3).

## 4. CHANGELOG (append-only)

- **2026-10-31** — doc created (living-handoff restructure); the six validated studies
  migrated from the Glossary/SPATT handoffs. No new backtests this session.
