# SPATT HANDOFF — Setup / Price-Action / Trade-Case module + trend insight engine

Handoff for continuing the work on another machine (Mac). Written 2026-10-17.
This complements `forecasting/HANDOFF.md` (the PAInsight forecasting-engine notes) — read that first for the underlying indicator conventions.

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
python -m forecasting.trend_insight --q23pm    # 23PM/235PM rise-origin validation
python -m forecasting.trend_insight --build is safe: writes only to data/insight/reports/.
```

## 2. Data files (NOT in git — gitignored; copy or re-export)

| File | Coverage | Role |
|---|---|---|
| `data/insight/csv/NSE_NIFTY1!, 5_Sep 2025-Mar 2026.csv` | 5m bars **2025-09-15 → 2026-10-16** (20,293 bars; despite the name it runs to Oct 16) — carries 5m/30m/2H/D PVT+MACD+Stoch+EMAs+VWAP per bar | PRIMARY source; regime + episodes + Momentors |
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

One record per **235PM uptrend phase** (2H+30m+5m PVT/MACD all up; contiguous), chronological, Sep 2025 → Oct 16 2026. Columns: `Sno, Trend, Set Up, Price Action, Tradecase, Window, Points Moved, % Move, Momentors, Note`.

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
- 235PM rise origins: shallow dips end at 1E21/1E50 (holds 63/69%); 1m close below 1E21 usually extends to the 1E50–1E100 zone (55%) before rising; 5m-MACD-cooling phases (5m PVT still +) retrace to 1E100/1E200 within 2 h (78%) and rise after (73%).

## 6. Open work (not started)

1. **Out-of-sample validation**: dev insight was built on Sep 2025–Mar 2026; **Apr–Jul 2026 is untouched**. Re-run `--build` scoped to OOS and compare tables (esp. the level/reject rates and the intraday clock).
2. **235PM Downtrend mirror**: the JSON/Trend column supports it; define the trade case (mirror of S-rows) and compute its episodes (`reg235` with all signs negative).
3. **2S/2O/DS/DO momentors** after the chart export adds those plots.
4. **Deeper 1m-EMA integration** once longer 1m history exists (TradingView hard limit ~2.5 months).
5. Momentors as a **predictor study**: does the launch code (e.g. `3PMS_X` vs `DP` vs blank) predict phase quality (Points Moved)? n=112 is enough for a first cut.

## 7. Running on the Mac

- Repo + `.venv` (or create; deps: only stdlib + openpyxl for export + Flask app deps already in requirements.txt).
- App: `start_app_silent.vbs` is Windows-only — on macOS run `python server.py` (venv) and open `http://localhost:5050`.
- Copy the two CSVs above into `data/insight/csv/` (or re-export from TradingView — same filenames).
- The playbook JSON comes from git (force-added).
- Re-run any insight command; keep the dev/OOS discipline (dev = Sep 2025–Mar 2026, OOS = Apr 2026+, 1m-verified rows = Aug 3 2026+).
