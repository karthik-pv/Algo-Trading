# HANDOFF — TREPAT (Trend Price Action Tracking)

> Hand this to a new AI session to get full context.
> Last updated: 2026-11-05 · Engine: `scripts/trepat_track.py` (first version)
> TREPAT = **T**REnd **P**rice **A**ction **T**racking — the tracking commentator
> for SPATAM-style price action, run from bar-export data.

---

## 1. What this is

TREPAT produces a **chronological tracking commentary** for a trading session
(NSE:NIFTY1!) from TradingView bar exports (15s / 1m / 5m). It announces — with
timestamps, in SPATAM vocabulary — the swings as they unfold: S/R touches and
retests, EMA/VWAP crossings, stochastic zone crosses, PVT/MACD2 momentors,
swing confirmations (`1PXD_15PM` / `1PXU_15PM`), and bias statements.

It is a **post-session / replay tool** (reads downloaded CSVs), not live.

## 2. Files

| File | Purpose |
|---|---|
| `tv/SPATAM/Tracking` | **The guideline — SOURCE OF TRUTH.** Every announcement type maps 1:1 to the engine. User keeps editing this; engine must be re-synced whenever it changes |
| `scripts/trepat_track.py` | The engine. Reads the three export CSVs, writes the chronological log |
| `tv/SPATAM/Tracking_<date>.log` | Run-time output — NEVER committed (gitignored) |
| `tv/SPATAM/spatam.md` | Sibling: the SPATAM indicator (vocabulary definitions: PM tags, PVT/VOC/MACD2, dashboard) |

Commit: `TREPAT: tracking engine + guideline (source of truth)` → `c6d8fc5` (2026-11-05).

## 3. Data requirements

Three TradingView chart-data exports in `~/Downloads` (names vary by suffix —
check newest by mtime, pattern `NSE_NIFTY1!, <TF>_<hash>.csv`):

- **15s** file → 15s bars + `PVT` + `MACD Line` (15s M1) + `1m K/D` (stoch) + full pivot/EMA ladder
- **1m** file → 1m bars + 1m `PVT`, `MACD Line` (M1), `MACD Line.1` (M2), `K/D`, `Daily VWAP` + **`PVT.1` = the 5m PVT seen on the 1m TF (developing value, updates every minute)** + 5m `K.1`
- **5m** file → 5m bars + 5m `PVT`, `MACD Line` (M1), `MACD Line.1` (M2); `PVT.1/PVT.2/PVT.3` = 30m / 2H / Daily PVT; `MACD Line.2/.3` = 30m M1/M2; `.4/.5` = 2H M1/M2; `.6/.7` = D M1/M2; `K.1/D.1` = 30m stoch

Gotchas (all confirmed the hard way):
- Export headers have **duplicate/generic names** (`MACD Line`, `Plot`, `Cross Up`) — map columns **by position**, verify TF of each block via value-change timing (HTF columns are forward-filled onto every row)
- `Cross Up/Cross Down` flag columns come out all-zero — **derive crosses from PVT sign changes / MACD2-vs-signal directly**
- Exports carry **no volume** → **VOC momentors cannot be announced** (the guideline has them pending; say so in the session-open line)
- 15s **MACD2 is not exported** — engine synthesizes MACD(24,52,9) from 15s closes
- Pivot columns can be NaN on the first row of a day — take a later row (e.g. 09:20)

## 4. How to run

```bash
cd ~/Projects/Algo-Trading
./venv/bin/python scripts/trepat_track.py > tv/SPATAM/Tracking_<date>.log
```
Paths of the three CSVs are hardcoded in `__main__` — update the filenames for
the new day (or accept args; not yet implemented). Overwrite the same
`Tracking_<date>.log` on every iteration — one file per day, no `_review` copies.

## 5. The rules (short form — the guideline file is authoritative)

1. Session open: gap up/down in % (the only numeric value ever announced)
2. Level **names only** — never Nifty price / level values
3. DPM = Daily PVT/MACD2 (nothing to do with Daily VWAP). DPM uptrend →
   30m EMA50/100/200 act as SUPPORT; downtrend → RESISTANCE
4. `Expect 1PXD/PXU_15PM` only when 5m PVT is downtrending/uptrending (point-in-time)
5. Stochastic: 5m only, zones only (OBZ/MZ/OSZ), no values
6. Retests count only after price moved ~25 pts away and returned; 3rd+ touch = "...again"
7. VWAP proximity warning within ~10 pts, once per direction per episode
8. 15s PVT/MACD2 never announced standalone — only inside 1PXD/PXU_15PM confirmation
9. S/R touches announced for ALL EMAs (1m/5m/30m, **excluding EMA9**), all pivotal levels, VWAP
10. Same-tick touches are GROUPED into one line (`Price retested support 1E100 and 1E200 again`)
11. `5PX about to cross down/up`: 5m PVT **read on the 1m TF**, above zero within ±0.1 (down)
    / below zero within ±0.1 (up), 1m PVT<0 & MACD2 falling (down) / 1m PVT>0 & MACD2
    rising (up). Once per episode

`1PXD_15PM Swing Down` = 1m PVT cross down **accompanied by** a 15s PVT or
15s MACD2 cross down within ±2 15s-bars (5-min dedup). Mirror for up.

## 6. Session workflow (what the user does / expects)

1. User downloads the 15s / 1m / 5m exports for the day into `~/Downloads`
2. Point the engine at the new files, generate the log, present it
3. User reviews commentary vs the guideline, suggests wording/rule changes,
   edits the `Tracking` guideline directly
4. Re-sync engine ↔ guideline, regenerate log, iterate
5. End of day (or when user says "check in"): commit engine + guideline
   (`TREPAT: ...` message style), push to `main`, never the log

## 7. Open items

- **VOC momentors** — blocked on volume in the export; guideline already lists them
- **Cancellation/retraction** — idea floated: if 1m trend flips while 5m PVT is still
  in the ±0.1 band, consider retracting the "5PX about to cross" call. Not implemented
- **Auto-file-discovery** — engine still hardcodes download filenames
- **Live mode** — currently replay-only; could later be wired to the Algo app's
  TTS queue like SPATAM alerts (see spatam.md §6)

## 8. First-version validation (2026-11-04 session)

- Gap call: -0.01% (flat open), prev close from last 5m bar of Nov 3
- 12× `1PXD_15PM` and 13× `1PXU_15PM` confirmations; 5PXU 12:25; 5PXD 15:35
- Bias stayed in 5PM Uptrend all day (Strong Uptrend 09:20 → Uptrend 12:30)
- `5PX about to cross down` 10:15 → 5PXD 10:20 (5-min lead, the case that drove
  the 1m-TF reading rule); 15:25 → 15:35 (10-min lead)
- Key levels: R2 D (resistance, retested 5+), R1 D, Daily VWAP, 1E50
- Noise trajectory: 376 → 286 → 331/335 lines as rules tightened (grouping,
  band ±0.1, momentor cuts)
