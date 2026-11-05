# HANDOFF — TREPAT (Trend Price Action Tracking)

> Hand this to a new AI session to get full context.
> Last updated: 2026-11-05 (late) · Engine: `scripts/trepat_track.py` + live voice feed
> TREPAT = **T**REnd **P**rice **A**ction **T**racking — the tracking commentator
> for SPATAM-style price action, from bar exports (replay) AND live TV alerts (voice).

---

## 1. What this is

TREPAT produces **chronological tracking commentary** in SPATAM vocabulary —
S/R touches and retests, EMA/VWAP crossings, stochastic zone crosses,
PVT/MACD2 momentors, swing confirmations (`1PXD_15PM` / `1PXU_15PM`), and
bias statements — in two modes:

1. **REPLAY (NIFTY):** post-session commentary from TradingView bar exports
   (15s / 1m / 5m CSVs) via `scripts/trepat_track.py` → `tv/spatam/Tracking_<date>.log`
   + the **trepat strip** Pine viewer (`tv/spatam/trepat.pine`, hover = read).
2. **LIVE (BTC):** TradingView alerts → webhook → **spoken** through the Algo
   app's TTS queue. Went live 2026-11-05 on a BTCUSD 15s chart (SPATAM
   indicator + `strategy_tradinginvestalgo_data` v1.14, `DATA_SYM = syminfo.tickerid`).

## 2. Files

| File | Purpose |
|---|---|
| `tv/spatam/Tracking` | **The guideline — SOURCE OF TRUTH.** Every announcement type maps 1:1 to the engine. User keeps editing this; engine must be re-synced whenever it changes |
| `scripts/trepat_track.py` | Replay engine. Reads the three export CSVs → chronological log + regenerates the strip's DATA block |
| `tv/spatam/Tracking_<date>.log` | Run-time output — NEVER committed (gitignored) |
| `tv/spatam/trepat.pine` | **Strip viewer v1.1** — own pane below the 1m/15s chart; one column per bar, tooltip = that bar's log lines verbatim |
| `tv/spatam/spatam` | SPATAM indicator — the LIVE alert producer (fires the ALERTS webhook; speaks on any chart symbol) |
| `tv/tv_algo_json/strategy_tradinginvestalgo_data` | DATA/LEVELS feed producer — **v1.14: `DATA_SYM = syminfo.tickerid`** (follows chart; BTC chart → BTC data) |
| `tv/spatam/spatam.md` | Sibling: the SPATAM indicator (vocabulary: PM tags, PVT/VOC/MACD2, dashboard) |
| `handoffs/tradingview_algo_json.md` | Webhook transport / payload schema / alert runbook |

Key commits: engine+guideline `c6d8fc5`; strip `57fabc8`…`f5a23f3` (v1.1 tag
`trepat-strip-v1.1`); TV data v1.14 `c19b593` (`tv-data-v1.14`); SPATAM
alertJsonOn default true `9fe6988` (2026-11-05.38).

## 3. Replay engine — data requirements

Three TradingView chart-data exports in `~/Downloads` (names vary by suffix —
check newest by mtime, pattern `NSE_NIFTY1!, <TF>_<hash>.csv`):

- **15s** file → 15s bars + pivots/EMAs + **`MACD Line`/`Signal Line` = the 15s MACD2
  (MACD_2452), exported directly — use it, do NOT synthesize** + **`PVT` = the 1m PVT
  (PVT_1m object — constant within each minute, NOT 15s PVT)** + `K/D` = 1m Stochastic.
  **No 15s PVT and no VOC exist in this export** (verified vs the 1m file and the
  chart object tree)
- **1m** file → 1m bars + 1m `PVT`, `MACD Line` (M1), `MACD Line.1` (M2), `K/D`, `Daily VWAP` + **`PVT.1` = the 5m PVT seen on the 1m TF (developing value, updates every minute)** + 5m `K.1`
- **5m** file → 5m bars + 5m `PVT`, `MACD Line` (M1), `MACD Line.1` (M2); `PVT.1/PVT.2/PVT.3` = 30m / 2H / Daily PVT; `MACD Line.2/.3` = 30m M1/M2; `.4/.5` = 2H M1/M2; `.6/.7` = D M1/M2; `K.1/D.1` = 30m stoch

Gotchas (all confirmed the hard way):
- Export headers have **duplicate/generic names** (`MACD Line`, `Plot`, `Cross Up`) — map columns **by position**, verify TF of each block via value-change timing (HTF columns are forward-filled onto every row)
- `Cross Up/Cross Down` flag columns come out all-zero — **derive crosses from PVT sign changes / MACD2-vs-signal directly**
- Exports carry **no volume** → **VOC momentors cannot be announced** (say so in the session-open line)
- Pivot columns can be NaN on the first row of a day — take a later row (e.g. 09:20)
- **Engine paths are hardcoded in `__main__`** — update the CSV filenames each day

## 4. How to run (replay)

```bash
cd ~/Projects/Algo-Trading
./venv/bin/python scripts/trepat_track.py > tv/spatam/Tracking_<date>.log
```
One run does three things: prints the log, regenerates the strip's `=== DATA ===`
block inside `tv/spatam/trepat.pine` (paste that file into the TV editor), and
keeps guideline↔engine 1:1. Overwrite the same `Tracking_<date>.log` every
iteration — one file per day, no `_review` copies.

### Strip viewer rules (trepat.pine v1.1)
- Auto-detects chart TF: 15s chart → per-second cells, 1m chart → per-minute cells
- Keys are **date-stamped** (`yyyyMMddHHmm[ss]`, IST) — only the session in the
  data lights up; no cross-day bleed
- `indicator(..., max_labels_count=500)` — Pine's default label cap is 50 (cost
  us the "only after 13:03" bug); tooltips live on LABELS only (plot/box have
  none); the DATA block uses `~` entry separators + lambda re.sub (literal `\n`
  escapes must survive — `re.sub` replacement strings eat them, and adjacent
  string literals need an explicit separator)

## 5. The rules (short form — the guideline file is authoritative)

1. Session open: gap up/down in % (the only numeric value ever announced)
2. Level **names only** — never Nifty price / level values
3. DPM = Daily PVT/MACD2 (nothing to do with Daily VWAP). DPM uptrend →
   30m EMA50/100/200 act as SUPPORT; downtrend → RESISTANCE
4. `Expect 1PXD/PXU_15PM` only when 5m PVT is downtrending/uptrending (point-in-time)
5. Stochastic: 5m only, zones only (OBZ/MZ/OSZ), no values
6. Retests count only after price moved ~25 pts away and returned; 3rd+ touch = "...again"
7. VWAP proximity warning within ~10 pts, once per direction per episode;
   direction = the side price is APPROACHING from (previous bar's position —
   current-side flipped the call at the cross bar itself)
8. **15s Candle crossed above/below VWAP** announced standalone (added 2026-11-05 —
   the actual cross events; 1m/5m candle VWAP crosses only fire on TF closes)
9. S/R touches announced for ALL EMAs (1m/5m/30m, **excluding EMA9**), the FULL
   pivot ladder (D/W/M/Q × P/R1–R3/S1–S3, dedup within 5 pts), VWAP
10. Same-tick touches are GROUPED into one line; levels in a continuing touch
    episode ride along in the grouped line when a fresh touch fires same tick
11. `5PX about to cross down/up`: 5m PVT **read on the 1m TF**, above zero within ±0.1 (down)
    / below zero within ±0.1 (up), 1m PVT<0 & MACD2 falling (down) / 1m PVT>0 & MACD2
    rising (up). Once per episode

`1PXD_15PM Swing Down` = 1m PVT cross down **accompanied by** a 15s MACD2 cross
down within ±2 15s-bars (5-min dedup). The "15s PVT" branch is impossible —
no such column in the export (the old check compared 1m PVT to itself). Mirror for up.

## 6. Live mode (BTC voice) — what's wired

Chain: TV alert → `POST /trading_view_webhook` (server.py:1467) →
`MSG:"ALERTS"` → `trader.process_spatam_alerts()` (trade_logic.py:8578) →
`queue_spatam_speech()` → voice loop (`say` on macOS / SAPI on Windows).
Persisted to `data/logs/SPATAM/SPATAM_Alerts_<date>.txt`.

- **No symbol filter exists in the voice path** — any symbol's ALERTS payload
  is spoken. BTC went live 2026-11-05 (SYM=BTCUSD in SPATAM's JSON).
- **Critical input:** SPATAM's `alertJsonOn` ("Send alert flush as JSON") must be
  **ON** — default flipped to true in 2026-11-05.38; OFF = plain-text body the
  app silently drops (no voice, no log — cost us an hour on 2026-11-05).
- One live SPATAM alert at a time (NIFTY and BTC both active = double-fire).
- Payloads carry no `SYM` on DATA/LEVELS (only SPATAM's ALERTS do); BTC vs
  NIFTY is not machine-distinguishable on DATA — if both feeds ever run
  simultaneously, add SYM to the v1.14 script (would be v1.15).
- Voice UI toggle: muted-at-startup is normal (enabled from the app checkbox);
  messages arriving while muted are DROPPED, not queued.

## 7. Session workflow (replay day)

1. User downloads the 15s / 1m / 5m exports into `~/Downloads`
2. Update `__main__` paths, run the engine, present the log
3. User reviews commentary vs the guideline, edits the `Tracking` guideline directly
4. Re-sync engine ↔ guideline, regenerate log + strip, iterate
5. End of day ("check in"): commit engine + guideline + strip (`TREPAT: ...`
   message style), push to `main` (+ git tag for pine changes), never the log

## 8. Open items

- **VOC momentors (replay)** — blocked: CSV exports carry no volume. LIVE is
  unblocked (BTC chart has volume — SPATAM can fire VOC)
- **Cancellation/retraction** — idea floated: if 1m trend flips while 5m PVT is
  still in the ±0.1 band, consider retracting the "5PX about to cross" call. Not implemented
- **Auto-file-discovery** — engine still hardcodes download filenames
- **Event-stamp semantics** — TF-bar events are stamped at bar OPEN (the bar
  completes later; e.g. a 5m verdict at 12:15 really completes at 12:20). User
  accepts current log timing; revisit only if they ask
- **Live TREPAT for NIFTY** — replay engine could be ported to consume the live
  DATA feed the way BTC voice does today
- **BTC voice = SPATAM vocabulary**, not TREPAT-replay vocabulary — the two are
  close but not line-identical; unification not started

## 9. Validation history

- **2026-11-04 (NIFTY replay):** gap -0.01%; bias 5PM Uptrend all day;
  `5PX about to cross down` 10:15 → 5PXD 10:20 (5-min lead); 15:25 → 15:35.
  Key levels: R2 D, R1 D, Daily VWAP, 1E50, R2 M. Noise 376 → ~339 lines.
- **2026-11-05 (BTC live):** v1.14 deployed, alerts recreated, first BTC
  DATA/LEVELS ~06:17 IST (~80,615 BTC scale). Voice proven by injected test
  payload (06:17, dropped while muted) + heard after alertJsonOn fix.
  Alignment bugs fixed: pivot ladder completeness (M R2 missing at 12:15),
  co-touch grouping, 15s VWAP crosses, VWAP direction-by-approach.
