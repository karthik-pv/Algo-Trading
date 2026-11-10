# Transcript — TREPAT → SPATAM single-runtime port + Tracking Log panel

> Session dates: 2026-11-09 → 2026-11-10 · Repo: Algo-Trading (main)
> Companion handoff: `handoffs/trepat.md` · Catalog: `tv/spatam/Tracking_spatam`

## Objective

Unify TREPAT (replay commentary engine) and SPATAM (live pine alerts) into one
message vocabulary, then re-architect so the **SPATAM pine is the sole
production runtime** (computes + paints the tracking log on-chart, independent
of ngrok), with the Python engine (`scripts/trepat_track.py`) demoted to a
replay test oracle.

## Background (prior sessions, summarized)

- `scripts/trepat_track.py` fully rewritten to follow the FINAL catalog
  `tv/spatam/Tracking_spatam`: template mapping (positional, descriptor-row
  based), momentor loops, 5PZ, Expect ±0.1, 1PMX condition + level
  attribution, may-cross mapping, aggressive-candle follow-through.
- Nov 6 NIFTY replay validated: 237-line log; 13:08 `Price rises above 1E100`
  reproduced; 17 follow-through events.
- Follow-through trigger evolution (do NOT regress): zone-high ref (wrong) →
  all-interaction arming (noisy) → **touch-anchored aggressive-candle (final)**.
- New 179-col 1m export (`NSE_NIFTY1!, 1_3c909.csv`, Nov 2 → Nov 8): template
  updated (Yearly pivot ladder + `1m EMA21 (seconds)`), dedup-suffix tolerant
  (`5P PVT` resolves despite `5P PVT.1`); Y ladder added to engine static
  levels; 14:01 Y P resistance captured.
- `Attention : ` prefix removed from ALL events everywhere (engine 30 +
  catalog 67 occurrences); user will reintroduce later with a better condition.

## 2026-11-09 — Pine port (SPATAM v2026-11-09.40.4)

Commits:
- `7245dca` / tag `trepat-engine-v1.1` — pending engine work: 179-col export
  template + Y ladder + Attention-prefix strip (engine + catalog + trepat.pine
  DATA block).
- `e1a2cd8` / tag `spatam-v2026-11-09.39` — **the port**: the old LOOKOUT
  FAMILIES section of `tv/spatam/spatam` replaced with a TREPAT block ported
  1:1 from the engine:
  - level-touch engine: 69-entry level universe (33 EMAs incl. EMA21/M-EMAs,
    35 pivots D/W/M/Q/**Y** + Daily VWAP frozen at day start, dedup 5 pts),
    wick-aware range overlap (NEAR=15), approach side, touch/retest/again
    groups, ride-along merging, RETREAT=25 re-arm
  - may-cross notes (3P→1E100/200, 2P→5E50/100/200, DP→3E50/100/200,
    WP→2E50/100/200, MP→DE50/100/200; uptrend = pane PVT > 0)
  - `Expect 1PX_15PM DT/UT` (1m PVT trending into the level, |PVT| ≤ 0.1)
  - aggressive-candle follow-through (body ≥ 1.5× prev-5 avg, next bar onward,
    15-bar validity, one fire per episode, armed on announced major-EMA touch)
  - `1PMX UT/DT` with `Support/Resistance @ <group>` attribution
  - 1E50 cross gated on 1m PVT; 1E50/1E21 retrace-that-holds (120 s touch
    suppression after retrace lines)
  - 1m candle VWAP cross; 5PZ once-per-episode (poise1 = MACD2 falling/rising)
  - session open: gap %, resistance/support within 0.5 %, VOC at open
  - 5PM-start + Bias lines (Strong when above/below VWAP; direction from PVT
    sign since the f_familyTag "5PM" tag does not encode direction)
  - momentors: PX/MX/ox only, catalog names (1M2X; 5M1X+5M2X; 3M2X;
    2M1X+2M2X; DM1X+DM2X); 5m stoch zone crosses (OBZ/MZ/OSZ)
  - REMOVED (not aliased): SX/M1X/M2X/M2z/Pz/oz codes, BIAS + Momentors
    heartbeats, `Attention : ` prefix
  - 1m layer gated on `new1m` (once per 1m bar, security-pulled 1m OHLC +
    prev-5 body avg); HTF momentors stamp on own TF boundaries
- `2dd01eb` — handoff updated.

Pine fix train (user pasted into TradingView; errors came back live):
- `7276064` / `.40` — "Script requests too many securities: 41 > 40" →
  EMA21 folded into the existing per-TF EMA grid pull (tuple extended to 5
  values; 7 standalone EMA21 requests removed).
- `9e661e8` / `.40.1` — "'o1m' is already defined": stray duplicated
  1m-bar security pull removed.
- `91cfee4` / `.40.2` — runtime RE10045 (array.get index 0, size 0, bar 4):
  `var lvName = str.split(...)` init unreliable → replaced with explicit
  `array.from(...)`.
- `f09ebe8` / `.40.3` — same RE10045 elsewhere: Pine runs a
  `for i = 0 to size-1` body even on empty arrays → all possibly-empty loops
  converted to `for x in arr`; array ternaries (`hasRes ? tickRes : tickSup`)
  replaced with explicit if/else assignments.
- `694558b` — version added to the indicator title… **reverted by user**:
  version must NEVER be in the indicator title; it lives as a `// VERSION:`
  comment on the line right below the `indicator(...)` call
  (`f03d679` / `.40.4`; AGENTS.md updated to record this rule).

## 2026-11-10 — Algo app Tracking Log panel

Commits:
- `11459de` — new panel: `process_spatam_alerts()` now ALSO keeps a SYM-tagged
  300-record ring buffer + emits the `spatam_alerts` socket event
  (persistence/speech untouched); `GET /tv/spatam_alerts` hydrates on page
  load; "Tracking Log" section added to the PA widget grid.
- `ae65664` — "table not coming up": the PA widget is hidden for the session
  (`PA_WIDGET_HIDDEN = true`); panel moved into the **ml-widget** grid, right
  of the Levels Ladder the user actually sees.
- `7501bcb` — was wrapping to a second row: `#ml-grid` set `nowrap`, panel
  trimmed to 300 px.
- "Alerts are not coming" — diagnosed: alerts WERE arriving (persisted
  08:43–08:56) but every line was filtered out — the BTCUSD test chart sends
  SYM as a study-object dump, the server's JSON repair renames it `REPAIRED`,
  and the client filter required `BTC`. Fix `2d725ce`: accept `REPAIRED`.
  Verified end-to-end via `curl localhost:5050/tv/spatam_alerts`
  (08:55 stoch cross, 08:56 VWAP cross in the buffer).
- `d7b5dcf` — **permanent switch to NIFTY**: filter = SYM contains `NIFTY`
  (+ `REPAIRED` fallback), title "Tracking Log".
- `6d44c38` — layout per user: Tracking Log column starts at the very top of
  the widget (right below Pending Orders); top strip width JS-synced.
- `1f1eb80` — top strip widened to the full Momentors + Ladder span (up to
  the ladder's right edge); sync JS removed, strip `width:100%`.

## Final state

- **Runtime:** SPATAM pine (sole commentary engine, 15s NIFTY chart) →
  ALERTS flush → voice + daily file + Tracking Log panel.
- **Oracle:** `scripts/trepat_track.py` regenerates the reference log for
  regression diffs; not yet regression-diffed against the pine.
- **Catalog:** `tv/spatam/Tracking_spatam` remains the single vocabulary.

## Open items (carried)

- Regression-diff pine Alert Log vs engine log on a replay day.
- 15s-flagship `1PX_15PM` confirmation exists only as the `Expect` pre-call
  (pine has 15s natively; engine stays 1m-only).
- WP/MP may-cross live-only until exports carry those panes.
- Auto-file-discovery for the engine; 5PZ cancellation idea; old
  `tv/spatam/Tracking` fold-and-drop decision; broken Pine SYM on BTCUSD.
