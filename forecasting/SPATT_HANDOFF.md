# SPATT HANDOFF — Setup / Price-Action / Trade-Case module + trend insight engine

Handoff for continuing the work on another machine (Mac). Written 2026-10-17.
This complements `forecasting/HANDOFF.md` (the PAInsight forecasting-engine notes) — read that first for the underlying indicator conventions.

---

## 0. 2026-10-18 UPDATE — new 5m export + 23PM_5PX setup

### New canonical 5m file: `data/insight/csv/NSE_NIFTY1!, 5_Sep 2025-Oct 2026.csv`
- Replaces `NSE_NIFTY1!, 5_Sep 2025-Mar 2026.csv` (same 20,293 bars, 2025-09-15 → 2026-10-16; OHLC and all shared indicator columns verified **identical** to the old file — r=1.0 / max diff ≤5e-11 — pure column additions).
- **Unprefixed positional layout** (59 cols, repeated generic header names) — `load_bars()` auto-detects both layouts (`_load_bars_positional` anchors blocks by index with strict header assertions; fails loudly if the layout shifts again).
- Per HTF block **two MACD pairs**: M1 = 12/26 (DISCARD), M2 = 24/52 (USE). `*_macd`/`*_signal` keys come from M2.
- **New columns**: `5m EMA4` (full range; tight proxy for 1m EMA21 — median 0.8 pts vs the real 1m EMA21 at 5m stamps), `1m EMA100`/`1m EMA200` (Aug 3+ only, lower-TF buffer), **`2H VOC`**.
- **`2H VOC` is a 30m-computed VOC with periods ×4 (36/84) = the 2H equivalent** (user-confirmed). It updates on **30m boundaries** (13–14 bars/day), NOT on 2H closes — momentor ages off it accrue per 30m step. Not a duplicate of 30m VOC (r=0.47). Range −29.5..+34.1, full-range coverage.
- Header vocabulary from the user's Numbers doc (`Nifty Data.numbers`): `3M1/3M2`, `2M1/2M2`, `DM1/DM2` = MACD pairs; `5m Stock D`/`30m Stock D` = Stoch D (typo normalized in code). Still absent: 2H Stoch, D Stoch, D VOC → `2SX`/`DSX`/`DOX` momentors remain pending (`2O` is now unblocked).

### New setup 23PM_5PX (`--q23pm5px`)
- **Definition (user, 2026-10-18)**: 2PM uptrending (2H PVT>0 & 2H MACD_2452 up) + 3PM uptrending (30m PVT>0 & 30m MACD_2452 up), carried no-repaint values, sign-based — same convention as 23PM_5PM. M1 (12/26) pairs are NOT used by current setups — reserved for a future setup. Trigger = **5m PVT cross-up** (−1→+1, same-day contiguous, stamps ≤15:25).
- **Exit (user, 2026-10-18, replaced the original 1m-MACD-cross-down rule)**: **first 5m close below its own 5m EMA4** — price loses the micro-mean (5m EMA4 ≈ 1m EMA21, median 0.8 pts apart). EOD flat at 15:25 if unbroken. The 1m MACD_2452 cross-down is logged as a comparison note (`macd_xdn_note`), not an exit. Rationale: measured head-to-head on the first 17 episodes, the EMA4 exit captured +116.2 pts vs +63.9 for the MACD exit (better in 13/17; the 1m MACD lags and confirms after the fade). No trigger bar ever closed below its own EMA4 (no born-dead edge case).
- Implemented: `q_23pm_5px()` + `--q23pm5px` in `forecasting/trend_insight.py`; entry = trigger 5m-bar close.
- **113 episodes** (full 5m range Sep 2025 → Oct 2026 — the EMA4 exit needs no 1m data): **60 positive (53%), 20 EOD flats, total +1,148.6 pts, mean +10.2, median +1.6, best +295.7, worst −39.2**. All 113 appended to `setup_pa_tradecase.json` (Sno 113–225, Trend=Uptrend, Set Up=23PM_5PX, Tradecase=SCALP_23PM_5PX — draft text, user to refine).
- Deliberately **excluded** example: 2026-10-14 15:20 cross-up — 3PM was down (30m PVT negative since 13:10, 30m MACD below signal since 14:40); the user's sighting was a "3PM *turning*" entry — candidate for a future sibling setup, not 23PM_5PX.

### New setup 2PM_3PX (`--q2pm3px`)
- **Definition (user, 2026-10-18)**: 2PM uptrending (2H PVT>0 & 2H MACD_2452 up, carried no-repaint) at a **30m PVT cross-up** (−1→+1 on carried values — prints at the 5m bar carrying the 30m close value; entry = that bar's close, the earliest no-repaint entry). **No 3PM condition** (unlike 23PM_5PX). Trigger stamps land on :15/:45 (the 30m boundaries of the 09:15 session).
- **Exit**: first 5m close below its own **5m EMA21**; EOD flat at 15:25 if unbroken (same close-basis convention as 23PM_5PX's EMA4 exit).
- Implemented: `q_2pm_3px()` + `--q2pm3px` in `forecasting/trend_insight.py`.
- **17 episodes** (full range Oct 2025 → Aug 2026; no 1m dependency): **11 positive (65%), 6 EOD flats, total +559.3 pts, mean +32.9, median +14.6** — deeper/rarer trigger + wider exit than 23PM_5PX (53% positive, mean +10.2). Best +162.3 (07 Apr 26), worst −38.0 (06 Feb 26). Rows **226–242** appended to `setup_pa_tradecase.json` (Trend=Uptrend, Set Up=2PM_3PX, **Tradecase=INTRA_5PM** — user-specified).

### Downtrend mirrors (user, 2026-10-18) — all three setups
- Every setup gained a **Downtrend** mirror (same Set Up name, direction lives in the Trend column). All legs are exact mirrors: regime = PVT sign −1 AND MACD_2452 down (carried); trigger = PVT cross-**down** (+1→−1); exit = first 5m close **above** its own EMA4/EMA21; EOD flat at 15:25. **Points Moved = move in trend direction** (positive = down-move captured) — keeps "positive = setup worked" true for both colors.
- Implemented as a `direction="up"|"down"` parameter on `q_23pm_5px()` / `q_2pm_3px()` (uptrend re-run identical after refactor — clean no-op) plus `q_23pm_5pm_down()` — machine mirror of the hand-marked phases: maximal same-day contiguous runs of 2H+30m+5m PVT/MACD all DOWN; rows carry close-to-close move + MFE (deepest low below phase start), matching the hand-marked note style. Flags: `--q23pm5pxd`, `--q2pm3pxd`, `--q23pm5pmd`.
- Results: 23PM_5PX↓ 154 eps (51% pos, +2,548.6 pts, mean +16.6); 2PM_3PX↓ 23 eps (65% pos, +501.4, mean +21.8, best +197.5 = 15 Sep 26 310-min EOD runner); 23PM_5PM↓ 174 phases (56% pos, +3,388.6, mean +19.5; only 10 single-bar phases — flicker not inflating counts). 2025's bear window carries the richer tails (64–71% positive), matching the dev-window regime finding.
- Playbook now **593 rows**: Uptrend 112/113/17 + Downtrend 174/154/23 (families appended in mirror order: 23PM_5PM↓ Sno 243–416, 23PM_5PX↓ 417–570, 2PM_3PX↓ 571–593; chronological within each (Trend, family) group; Sno sequence verified). Downtrend Tradecase mirrors the family: SCALP_1SPX_15PM / SCALP_23PM_5PX / INTRA_5PM.

### Machine-setup row convention in the playbook (23PM_5PX / 2PM_3PX)
- **Price Action (user rule, 2026-10-18)** = the major level taken at the **CURRENT 1m PVT cross** belonging to the trigger (latest 1m PVT cross-up at/before entry for longs; cross-down for shorts — `px_current_1m_cross`, scans back ≤10 days of 1m coverage). Label = every **major rung the move has already reclaimed** between the cross-leg extreme (dip low since the prior 1m cross-down / rally high since the prior cross-up) and the cross-bar close, **in crossing order** (`px_support_label`): e.g. "Rise from 5E50, 1E200, 1E100". Major rungs = 1E100, 1E200, 5E50, 5E100, 5E200, DE100, DE200 — the minor rungs (1E50/1E21/1E9) are never listed except the **fallback**: no major reclaimed → "Rise/Fall from 1E50, 1E21". Basis: 1m cross bar where 1m coverage exists (Aug 3 2026+), else the trigger 5m bar (5m/DE rungs only). Verified against the user's chart call: 16 Oct 26 15:15 5PX = "Rise from 5E50" (1m PVT cross-up 15:12, price leaning on the rising 5m EMA50).
- Entry-position band (old derivation) retained as reference field `price_action_band` in the query output (not shown in the grid). `Window` = `dd Mon yy-HH:MM-HH:MM`; `Note` documents entry/exit prices, swing minutes, EOD flag, the 1m MACD cross comparison where covered, and marks the row machine-detected. Machine rows are appended after the hand-marked family (Sno continues), chronological within each (Trend, family) group; filter by Set Up in the grid.

---

## 1. What was built

### 1.1 SPATT module (in the Flask app)
- **Tab "SPATT"** in the app's tab bar (`templates/base.html`) → route `/spatt` (`server.py`) renders `templates/app.html` panel `#panel-spatt`.
- **Grid** (client, inline script in `app.html`): 13 columns, server-fed via `/spatt/data` (JSON), with:
  - per-column "contains" text filters (instant, case-insensitive)
  - click-to-sort on EVERY column (asc/desc toggle, type-aware: Sno numeric, Date parses `dd Mon yy`, times/durations as minutes, Momentors NA-last)
  - auto-refresh 10s (toggle) + manual Refresh — **filter text and sort state survive refreshes** (header builds once: `headBuilt` flag)
  - group headers: **Window** (Date | From Time | To Time | Duration) and **Change** (Points | %); Trend/Set Up colored green (Uptrend) / red (Downtrend)
  - sticky all three header rows: **requires `border-collapse:separate; border-spacing:0`** (collapse silently breaks sticky in Chromium)
- **Excel export**: `/spatt/export` → styled .xlsx download (bold+filled two-row header with merged group cells, column widths, wrap text, auto-filter dropdowns on the sub-header row, colored Trend/Set Up). Filename stamped with data date.
- Data source of truth: **`data/insight/setups/setup_pa_tradecase.json`** (NOT Excel — Excel is export-only). Server caches by mtime; Excel-lock issues eliminated.
- Helper: `scripts/view_setup_table.py` — standalone auto-refreshing browser view (port 8917); superseded by the SPATT tab, kept for reference.

### 1.2 Trend insight engine (`forecasting/trend_insight.py`)
Standalone analysis module over the 5m TradingView export (which carries the full multi-TF indicator state per 5m bar). Commands:
```
python -m forecasting.trend_insight --verify   # data semantics checks
python -m forecasting.trend_insight --build    # 5-table insight pack -> data/insight/reports/trend_insight.md/.json
python -m forecasting.trend_insight --q2h      # 2H uptrend survival by D-context
python -m forecasting.trend_insight --q5mxc    # 5m PVT cross-up context & follow-through
python -m forecasting.trend_insight --q1m      # 1m EMA price action x HTF regime
python -m forecasting.trend_insight --q23pm    # 23PM/23PM_5PM rise-origin validation
python -m forecasting.trend_insight --build is safe: writes only to data/insight/reports/.
```

## 2. Data files (NOT in git — gitignored; copy or re-export)

| File | Coverage | Role |
|---|---|---|
| `data/insight/csv/NSE_NIFTY1!, 5_Sep 2025-Oct 2026.csv` | 5m bars **2025-09-15 → 2026-10-16** (20,293 bars) — carries 5m/30m/2H/D PVT + dual MACD pairs (M1 12/26 discarded, M2 24/52 used) + Stoch + VOC (5m/30m/2H) + EMAs (incl. 5m EMA4, 1m E100/E200) + VWAP per bar | PRIMARY source; regime + episodes + Momentors + 23PM_5PX |
| `data/insight/csv/NSE_NIFTY1!, 1m.csv` | 1m bars **2026-08-03 → 2026-10-16** (53 days, 20,405 bars) — true 1m Stoch/PVT/MACD + 1m EMA ladder | 1m-EMA context + 1SPX_15PM trigger detection |
| `data/insight/setups/setup_pa_tradecase.json` | the playbook (112 records) | **force-added to git** (`git add -f`), so it travels with the repo |
| `data/insight/reports/*` | insight pack + query outputs | regenerable via the commands above |
| `data/insight/setups/setup_pa_tradecase_backup_20261017.xlsx` | the original hand-styled Excel | archive; superseded by export route |

TradingView limits to know: 1m history depth ≈ 2.5 months (verified twice) — deeper 1m re-exports are impossible; the 5m export's lower-TF indicator columns only populate ~2.5 months back from the export date.

## 3. Data semantics (verified — do not re-learn the hard way)

- **MACD pairs are the 24/52 variant** (correlation 1.0 with self-computed 24/52; the 12/26 pairs are discarded).
- **HTF columns behave differently**: PVT (30m/2H/D) is carried on EVERY 5m bar = last COMPLETED TF value (no repaint). MACD/Signal/Stoch/VOC for 30m/2H/D are populated ONLY at the TF window's closing bar (2H: 11:10/13:10/15:10/15:25; D: 15:25; 30m: :40/:10 closes) → forward-fill to use. `1m EMA200` was all-null in the 5m export (lower-TF buffer).
- **D-PVT carried on day T = previous day's final PVT** (matches the regime convention in HANDOFF.md).
- **Python bool trap**: `sgn(False) == 0` — never pass booleans into `sgn`; convert to ±1 explicitly first (this silently killed the VOC momentor once).
- Session: 75 5m bars 09:15–15:25; 1m file also prints 15:30–15:39 tail bars (post-close; not tradeable).

## 4. The playbook (112 records)

One record per **23PM_5PM uptrend phase** (2H+30m+5m PVT/MACD all up; contiguous), chronological, Sep 2025 → Oct 16 2026. Columns: `Sno, Trend, Set Up, Price Action, Tradecase, Window, Points Moved, % Move, Momentors, Note`.

- **Momentors** = cross-up ages at phase start: per TF a token `3`/`2`/`D` + active oscillator letters (P/M/S/O), joined by `_`; trailing `X` when the 1SPX_15PM entry trigger fired. Print windows: **30m ages 1–13** (cross candle = 1); **2H/D ages 0–4** (cross candle = 0); anything else → NA. Examples: `3PM_2P_DP_X`, `3O_2P_X`, `3PMS`.
- Engine: `compute_momentor_ages()` in `forecasting/trend_insight.py`; `MOMENTOR_RULES = {"3": (1,1,13), "2": (0,0,4), "D": (0,0,4)}`.
- **Pending**: `2SX/2OX/DSX/DOX` need 2H Stoch/VOC + D Stoch/VOC plots added to the TradingView export (code ready). 2O had zero records because 2H VOC isn't exported (also the `sgn(False)` bug was fixed — 3O now works).

## 5. Key insight findings (dev window Sep 2025–Mar 2026, n=134 days)

Full tables: `data/insight/reports/trend_insight.md`. Highlights:
- Regime prevalence: D-|2H-|30- 27.7%, D+|2H+|30+ 25.5% (window net-bearish). D-PVT sign persists next day 90.2%.
- Persistence ladder: 5m PVT ~45 min; 30m ~6 h; 2H 12.5 h up / 18.8 h down; D 90% next-day.
- Pullbacks: once 5m closes beyond EMA21/VWAP against an aligned trend, only ~30% snap back within 30 min.
- Intraday clock: BULL gains front-load 09:15–11:15; midday fade even in bull; BEAR accelerates into close (−2.33 pts/5m bar 14:15–15:30).
- Levels: VWAP reject 33% (BULL) vs 53% (BEAR); PDL rejects 68% in BEAR; PDO holds 78% in BULL vs 28% in BEAR.
- 5m PVT cross-up (1SPX-class trigger): with 2H+30m PVT+MACD aligned → 71% up +15 min (median +5.5 pts), edge dead by +60 min. Without MACD confirm → no edge (39% @30 m). No alignment → trap (−9.8 mean EOD).
- 23PM_5PM rise origins: shallow dips end at 1E21/1E50 (holds 63/69%); 1m close below 1E21 usually extends to the 1E50–1E100 zone (55%) before rising; 5m-MACD-cooling phases (5m PVT still +) retrace to 1E100/1E200 within 2 h (78%) and rise after (73%).

## 6. Open work (not started)

1. **Out-of-sample validation**: dev insight was built on Sep 2025–Mar 2026; **Apr–Jul 2026 is untouched**. Re-run `--build` scoped to OOS and compare tables (esp. the level/reject rates and the intraday clock).
2. **23PM_5PM Downtrend mirror**: the JSON/Trend column supports it; define the trade case (mirror of S-rows) and compute its episodes (`reg235` with all signs negative).
3. **2S/2O/DS/DO momentors** after the chart export adds those plots.
4. **Deeper 1m-EMA integration** once longer 1m history exists (TradingView hard limit ~2.5 months).
5. Momentors as a **predictor study**: does the launch code (e.g. `3PMS_X` vs `DP` vs blank) predict phase quality (Points Moved)? n=112 is enough for a first cut.

## 7. Running on the Mac

- Repo + `.venv` (or create; deps: only stdlib + openpyxl for export + Flask app deps already in requirements.txt).
- App: `start_app_silent.vbs` is Windows-only — on macOS run `python server.py` (venv) and open `http://localhost:5050`.
- Copy the two CSVs above into `data/insight/csv/` (or re-export from TradingView — same filenames).
- The playbook JSON comes from git (force-added).
- Re-run any insight command; keep the dev/OOS discipline (dev = Sep 2025–Mar 2026, OOS = Apr 2026+, 1m-verified rows = Aug 3 2026+).
