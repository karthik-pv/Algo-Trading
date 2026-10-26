# HANDOFFS — read in this order

1. **`2026-10-25_2pmm.md`** — **THE LATEST — START HERE**: the **2PMM module** built —
   the event engine (`forecasting/pmmEngine.py`, run it yourself, zero tokens), the
   1,151-record matrix (`data/insight/csv/2pmm_matrix.csv`), and the **2PMM Grid** tab
   (`/2pmm`). Also: the sparse-HTF-block + :10/:40 print-timing discoveries.
2. `2026-10-24_data_refresh.md` — all CSVs refreshed to
   Oct 23, the full-history 30m replacement (D-PVT/2H-PVT/DM1/DM2/E50s now in-file),
   **DM1 convention change (kept, not discarded)**, the VWAP-column fix, store.json
   rebuilt (99/112; fails = data growth only).
3. `2026-10-20_pm_ladder.md` — the PM-ladder model (D/2/3/5/15s rung zones + trade
   cases), the 1-year tiered validation (`forecasting/pm_ladder.py`), and the PM Ladder
   Table indicator.
4. `2026-10-19_mac_migration.md` — the Mac as the trading machine, today's fixes,
   the setup families, the queued work, the gotchas.
5. `SPATT.md` — the SPATT module + the trend-insight engine + the setup families'
   full derivations (the conventions live here too, updated 2026-10-19).
6. `PAInsight_forecasting_engine.md` — the forecasting engine's research + the build
   notes (the indicator conventions, the store, the webhook schema).

Rule: a new session ends with a new dated handoff in this folder, and the newest one
sits at the top of this list.
