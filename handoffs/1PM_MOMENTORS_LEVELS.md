# HANDOFF — 1PM Momentors & Levels Ladder widget

Created: 2026-11-01 (session "1PM Momentors & Levels Ladder")
Status: **LIVE v1** — read-only dashboard widget, no order-path interaction.

---

## 1. What it is

A two-table widget in the **Trade module, below Pending Orders**:

1. **1PM Momentors** (left) — the momentor state net, so the user can see how
   aggressive the 1PM Swing is right now and judge whether to add an
   **Intra** position on top of ultra-scalping.
2. **Levels Ladder** (right) — the S/R levels within **±150 pts** of the
   Nifty Futures close, with headroom in points and the endorsing
   higher-TF PM state ("chip") per rung.

Decision question it answers: enough headroom above → hold the swing;
stacked overhead levels → prefer Intra/ultra-scalp positioning.

## 2. Momentors table — rules

Two columns: **Momentor** and **Count** (cross age 1–4).

- **Fresh cross, age 1–4** → shown as the uppercase X-form with the count,
  **dark** green `#548235` (up) / dark red `#C00000` (down).
- **Aged out (age > 4)** → the row **degrades to its lowercase/state form**,
  **light** green `#C6EFCE` / light red `#FFC7CE`, no count.
  (e.g. `3ox` age 5+ → shows `3o`; `3M1x` beyond 4 → `3M1` uptrending.)
- **1o / 5o below zero** → grey (VOC has no downtrend setup — Glossary §Set Ups).
- **Row 1 — `5SXTOZ`**: 5m Stochastic cross + the zone it is crossing /
  trending FROM: `OSZ` (K<25, favorable, dark green), `MZ` (25–75, good,
  amber), `OBZ` (K>75, unfavorable, dark red). Arrow ↑/↓ = last cross
  direction (KCrossover vs KCrossUnder index). **Nothing to do with volume.**
- **Row 2 — `1PME50`**: dark green when the 1PM state (1m PVT + 1m MACD2
  24/52) is UP **and** price ≥ 1m EMA50; dark red otherwise (the LOCKED
  trend-start trigger, FORECASTING §2.1).

Row order (sheet casing, exact): `5SXTOZ` · `1PME50` · `5px` · `1ox`/`1o` ·
`5ox`/`5o` · `3PX`/`3p` · `3M1X`/`3M1` · `3M2X`/`3M2` · `3OX`/`3o` ·
`2PX`/`2p` · `2M1X`/`2M1` · `2M2X`/`2M2` · `2OX`/`2o` · `Dp` · `DM1` ·
`DM2` · `Do`.

**Data source — the live webhook only.** Each `INDICATORS` TF block of
`tv/TV_Algo_JSON/Strategy_TradingInvestAlgo_Data` already carries
`PVTCrossOverIndex/UnderIndex`, `X_1226/Y_1226`, `X_2452/Y_2452`,
`KCrossoverIndex/KCrossUnderIndex`, `VOCCrossOverIndex/UnderIndex`
(all `barssince(cross)+1`) → ages come straight off the snapshot; no CSV
reads, no history keeping, restart-safe.

**Daily rows are STATE-ONLY** (`Dp`/`DM1`/`DM2`, light colors, no counts):
the Pine payload's `D`/`W` INDICATORS blocks are commented out
(strategy script lines ~1188–1261); D states come from
`SETUPS.DASHBOARD` (`DPMPVT` sign; `DPM` = 2-letter combo, first letter
M1 12/26, second letter M2 24/52). `Do` is grey — D VOC not in the payload.
**Update (2026-11-01, Pine v1.4):** the payload now carries compact
`W`/`D`/`Q` INDICATORS blocks (`PVTTrendFlag` + `Trend_2452` only, ~150
chars each), so the previously-pending chips are live: `WPM` → 2h EMA200,
`MPM` → D EMA100/200, `QPM2` → W EMA100. D-row momentor cross ages and the
full (non-compact) W/D blocks remain future work.

## 3. Levels Ladder — rules

- Source: `LEVELS.CW_CD` of the webhook LEVELS message (`[Name, Price]`
  pairs, HA-based per Glossary §"LEVELS from the TradingView webhook
  payload"). Parsed and re-served by `forecasting/momentors_widget.py`.
- Filter: rows within **±150 pts** of the Close (the `["Close", v]` anchor
  row supplies the reference price).
- Sort: price descending → **Close row highlighted mid-table with `<<<`**;
  resistances above it, supports below. In an upswing the support the price
  rose from stays at the bottom of the ladder, in a downswing the resistance
  it fell from at the top — the ±150 filter + sort produce this naturally
  as price moves.
- `Pts` = |level − close| (headroom).
- **Chips** (State column) — user rule: a higher-TF PM uptrend endorses its
  lower-TF EMA rungs as **supports** (dark green chip); a downtrend makes
  them **resistances** (dark red chip). PM = PVT sign + MACD2 24/52 aligned.

  | Chip | Endorsing state | Rungs |
  |---|---|---|
  | `3PM2` | 30M PVT+ & Trend_2452+ | 1m EMA100, 1m EMA200 |
  | `2PM` | 2H PVT+ & Trend_2452+ | 5m EMA50, 5m EMA100, 5m EMA200 |
  | `WPM` → 2h EMA200 · `MPM` → D EMA100/200 · `QPM2` → W EMA100 | **pending** — W/M/Q states are not in the webhook payload yet | |

## 4. Implementation map

| Piece | Where |
|---|---|
| Pure builders (momentors + ladder) | `forecasting/momentors_widget.py` |
| Endpoint (read-only, 1s TTL cache, fail-safe → HTTP 200 + `error`) | `server.py` `GET /momentor/data` |
| Widget UI + 2s poller (self-contained IIFE, `try/catch` around render) | `templates/app.html` (`#ml-widget-container`, below `#pending-widget-container`) |

## 5. Safety guarantees (market-day constraint, 2026-11-01)

- **Read-only**: the endpoint only reads `trader._tradingView_Data` and
  `trader._tv_levels` (already in memory from the webhook). It writes
  nothing — no files, no DB, no state.
- **No order-path edits**: `core/trade_logic.py`, the order/position
  workflow, and appconfig defaults are untouched. No new config keys.
- **No Pine changes** → no new alerts, no extra webhook traffic.
- **Fail-safe**: endpoint catches everything → `{"error": ...}` with 200;
  the widget JS isolates its render in try/catch — a widget failure can
  never break the pending-orders grid or order ticket.
- **Stampede-safe**: 1s server cache; worst case with many tabs = one
  builder pass per second over an in-memory dict.

## 6. Persistence (added 2026-11-01, after first market-off test)

The widget initially stayed blank after an app restart while the market was
off — `trader._tradingView_Data` / `_tv_levels` are memory-only. Fix:

- Every DATA / LEVELS webhook payload is now merged into
  `data/tv_last_snapshot.json` (gitignored runtime file; atomic write,
  throttled to one write per 5s, ALERTS/OPT messages skipped).
- `/momentor/data` falls back to that file when the in-memory snapshot or
  levels are empty and marks the payload `"source": "persisted"`; the widget
  header then shows "last TV data: <ts>" in amber so stale data is never
  mistaken for live.
- Trade logic untouched — the persist hook is additive inside the webhook
  route, wrapped in try/except, and cannot affect order flow.

## 7. Testing the widget while NSE is closed (v1.2 data pin, 2026-11-01)

The indicator `Strategy_TradingInvestAlgo_Data` **v1.2** pins its data symbol
to `NSE:NIFTY1!` (one `DATA_SYM` const; all `request.security` calls + the
`SYM` fields use it). Placed on a 24/7 chart (e.g. **BTCUSD**) it computes
and alerts **genuine Nifty data** while NSE is closed — the full path
(alert → webhook → persist → widget) can be tested any day. Payload format
is byte-identical to a NIFTY1!-chart alert.

**IMPORTANT (v1.5, 2026-11-01):** TradingView silently DROPS an alert message
over 4096 chars. v1.4 pushed the DATA message over the cap and it stopped
arriving (LEVELS/OPT kept coming). v1.5 dropped the 5S/15S INDICATORS blocks
to get back under the cap. **Do not add payload fields without checking the
message length budget** (DATA ≈ INDICATORS+SETUPS must stay < 4096).

**Rules:**

- **Exactly ONE live alert at a time.** A BTCUSD-chart alert and a
  NIFTY1!-chart alert both active would double-fire every bar close —
  double Spatam TTS + duplicate webhook processing into the trade path.
- A BTCUSD-chart alert fires **24/7** (every 5m bar close, all weekend) —
  disable it after testing / before the NSE open and keep the NSE-chart
  alert as the market-hours source.
- Caveat: `ORH`/`ORL` and session stamps key off the **chart's** session —
  on a BTCUSD chart those two values are not NSE-meaningful. All other
  payload values are pure Nifty. The NSE-chart alert remains authoritative.

## 8. Known limitations / next steps

**Widget UX update (2026-11-01, same day):** the Momentors table shows **fresh
crosses only** (age 1–4, dark colors) — aged-out state rows are hidden;
`Do` stays pinned at the bottom; captions above the two tables were removed
and rows compacted ~30%. Pine **v1.3** additionally fixed chart-context leaks
(Nifty close, Round Numbers, session/weekly VWAP, ORH/ORL now all computed
from `DATA_SYM` via `request.security`) — on a NIFTY1! chart the payload is
unchanged; on a 24/7 test chart no BTC values leak into LEVELS/DATA.

1. `WPM`/`MPM`/`QPM2` chips and D-row cross ages need the D/W INDICATORS
   blocks enabled in the Pine payload (version-bump ceremony) or another
   W/M/Q state source.
2. The `Close` anchor is the chart close at webhook time; between LEVELS
   messages (one per confirmed bar) headroom is against that, not the
   broker tick.
3. Intra-vs-swing verdict gauge (SWING / STOCH-CHOP / DEAD + headroom
   summary) deliberately deferred — two tables only in v1.
