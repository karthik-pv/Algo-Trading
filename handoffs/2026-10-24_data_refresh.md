# HANDOFF — 2026-10-24 — Data refresh to Oct 23, DM1 convention change, store rebuilt

Session: all analysis CSVs refreshed to **Fri 2026-10-23**, the 30m engine source replaced
with a full-history export, the DM1 pair promoted to *kept* status, and store.json rebuilt.
Reads after `2026-10-20_pm_ladder.md`.

---

## 1. Data state after this session (all in `data/insight/csv/`)

| File | Coverage after merge | How |
|---|---|---|
| `5_Sep 2025-Oct 2026.csv` (canonical 5m) | 2025-09-15 → **2026-10-23** | positional append from `5_7f9e6.csv` (header byte-identical) |
| `1m.csv` (canonical 1m) | 2026-08-03 → **2026-10-23** | value-mapped append from `1_8be94.csv` |
| `1_20adf.csv` (engine 1m) | 2026-08-03 → **2026-10-23** | mapped append; 12/26 pair + D/W/M EMAs empty (empty before too) |
| `5_be22a.csv` (engine 5m) | 2026-08-25 → **2026-10-23** | mapped append; 1m EMA9/21/50 cross-filled from the 1m file (exact) |
| `30_42a03.csv` (engine 30m) | **2020-01-01 → 2026-10-23 (full replace)** | replaced by `30_8cadf.csv` (full-history export, 57 cols) |

**2026-10-20 = trading holiday** (no bars — 3 sessions added, not 4).

## 2. CONVENTION CHANGE — DM1 is now KEPT (user, 2026-10-24)

The old rule was "first MACD pair (12/26) = DISCARD". **The D-timeframe DM1 (12/26) is
now KEPT and will be used.** In the 30m export the D block reads [D-PVT, DM1, DM2];
`read_tv_csv` now names MACD pairs occurrence-based so nothing collides or gets dropped:
pair 1 = `MACD_Line_1226`, pair 2 = `MACD_Line_2452` (unchanged for 2-pair files),
pairs 3+ = `MACD_Line_3..6`. In the 30m file:
- `MACD_Line_1226/2452` = 3M1/3M2 (30m; 2452 = the used 30m pair — verified r=1.000 vs
  self-computed)
- `MACD_Line_3/4` = 2M1/2M2, **`MACD_Line_5` = DM1, `MACD_Line_6` = DM2** (pair identity
  verified by correlation vs daily-computed 12/26 and 24/52: r=0.999/0.998)
M1 pairs in the 1m/5m exports remain unpopulated-by-chart (the 1m chart no longer plots
1M1 at all) — but the discard convention is lifted: if an export carries an M1, store it.

## 3. The 30m export now carries the full daily/2H layer (verified)

New 30m file blocks, all sign/value-verified against the 5m file and the 1D file:
- **PVT@30 = 30m PVT, PVT#2(@43) = 2H PVT, PVT#3(@49) = D PVT** (each sign-agreement
  1.000 vs the 5m file's carried blocks; the D-PVT also 1.000 vs the 1D file's PVT over
  472 days)
- **2H EMA50 / D EMA50 restored** (fully carried every bar; 2H E50 value-exact vs the 5m
  file), **W EMA50** from ~2021 (weekly convergence)
- DM1/DM2 as above. D Stoch (DS) still absent.
- **VWAP = true session VWAP again** — the old 30m file's "VWAP" column had silently
  become a ±offset study (chart change before the Oct 20 refresh), which broke the
  store's VWAP touch semantics. The new export fixes it.

## 4. Series note (pre-existing, now quantified — do not "fix" casually)

All 30m exports (old and new) track a slightly different price series than the 5m/1m
exports (daily closes differ ±46 pts, varying — spot-vs-futures-like). Each file is
internally self-consistent (price, EMAs, VWAP, PVT from one series), so the store's
touch/hold stats are valid. The rebuilt store's per-regime VWAP/level tables reproduce
the research references — confirmed end-to-end.

## 5. store.json REBUILT (2026-10-24)

- `python -m forecasting.store_builder` → new store (meta ts 2026-10-24T01:46Z).
- Old store backed up: `csv/backup_2026-10-24/store.json.pre_rebuild`.
- `--check`: **99/112 pass**; the 13 fails are pure data-growth drift (micro window grew
  32→44 days → Setup A/B counts scaled; daily table +2 rows). No level-table or VWAP
  failures. VWAP confluence gradient intact: reject-from-below 10.2%→33.0%→49.8%→72.5%
  (research: 10/33/50/73).
- `read_tv_csv` in `forecasting/store_builder.py` was updated for occurrence-based MACD
  pair naming (see §2) — required before any rebuild on the 6-pair 30m file.

## 6. Backups

`data/insight/csv/backup_2026-10-24/` — all five pre-merge CSVs + the pre-rebuild store.

## 7. Still open

1. **1D file** (`1D_6b212.csv`) — stale AND its O/C are Heikin-Ashi synthetic values
   (discovered 2026-10-24; H/L are true). PVT column stops 2026-09-25. The new 30m file
   now carries D-PVT/D-EMAs and can aggregate true daily OHLC — the daily layer can be
   re-sourced from the 30m when the builder is next touched (user decision pending on
   true-O/C vs HA-anchored PDC/PDOpen semantics).
2. **2H bars** (`120_93fb3.csv`, since Sep 25) and **15S** (since Oct 17) — not refreshed.
3. 15s tier validation, OOS window Apr–Jul 2026, momentor-predictor study — unchanged
   from the 2026-10-20 handoff.
4. BTC phase — unchanged.
