# HANDOFF — SPATAM Algo UI Tables (1PM Momentors & Levels Ladder widget)

Created: 2026-11-01 (session "1PM Momentors & Levels Ladder")
Status: **LIVE** — read-only dashboard widget, no order-path interaction.
Last updated: 2026-11-04 (renamed per user: these four tables are the
**SPATAM Algo UI Tables**; chips now the verbatim SPATAM FAM tag, Pine v1.13).

---

## 0. Registered table names (user convention, 2026-11-01; confirmed as the
## "SPATAM Algo UI Tables", 2026-11-04)

The widget area has FOUR tables, always referred to by these names.
User's layout summary (2026-11-04): **"Topmost is BIAS table. On the left we
have Momentors, below that the SetUp table. On the right we have the Levels
Ladder."** Together these four are the **SPATAM Algo UI Tables** — the app-side
mirror of the SPATAM chart indicator (see `tv/spatam/spatam.md`).

| # | Registered name | Position | Contents |
|---|---|---|---|
| 1 | **BIAS table** | top-most, spans the two below | `PA / 5PM / CV / 3S` + set-apart `3PM`; grey caption row + colour-filled trend row (arrows per component) |
| 2 | **MOMENTORS table** | second, left | momentor rows + the TF state rows (`1/5/3/2/D`) directly beneath |
| 3 | **LEVELS LADDER table** | second, right | S/R levels ±150 pts, yellow Close row, SPATAM tag chips in 4th column |
| 4 | **SET UP table** | third, left (below MOMENTORS) | dynamic green/red component columns per TF |

---

## 1. What it is

A four-table widget in the **Trade module, below Pending Orders** (read-only)
so the user can judge 1PM Swing aggressiveness and whether to add an
**Intra** position on top of ultra-scalping. Data source: the live TradingView
webhook snapshot only (`trader._tradingView_Data`, `_tv_levels`, `_tv_htf`) —
no CSV reads, restart-safe via the persisted snapshot (§6).

## 2. MOMENTORS table — rules

Two columns: **Momentor** (first column always white text, lowercase) and
**Count** (always a colour-filled cell, fixed text colour).

- **Fresh cross, age 1–4** (standard window) → dark green `#548235` (up) /
  dark red `#C00000` (down) with the count.
- **Aged out (> window)** → the row is hidden entirely (fresh-crosses-only).
- Fixed-row cells (always present):
  - `sup` / `res` (top row): the level the price is working off — rising =
    highest level below Close (support taken), falling = highest level above
    Close (resistance). Shorthand names: `1m EMA100`→`1E100`, `5m EMA50`→`5E50`,
    `30m EMA100`→`3E100`, `2h EMA200`→`2E200`, `D EMA100`→`DE100`.
  - `5s` — the **5-minute Stochastic** (NOT 5 seconds; caption is legacy).
    Always visible, never ages out. Colour = zone × swing:
    upswing: OSZ→dark-green, MZ→light-green, OBZ→red;
    downswing: OSZ→red, MZ→light-red, OBZ→dark-red. (Dark = swinging with
    room left; plain red = exhausted extreme.) Arrow ↑/↓ = swing direction.
  - `1e50` — `UP` dark-green when 1m PVT up **and** price ≥ 1m EMA50,
    `DN` dark-red otherwise.
- **CVX rows** (Pine v1.9): `1cvx` (1m price×session-VWAP cross, fresh
  window **1–20**) and `5cvx` (5m, fresh window **1–4**). Direction purely
  from cross recency (no trend flag): green cross up / red cross down.
- Standard cross rows: `5px` `1ox` `5ox` `3px` `3m1x` `3m2x` `3ox` `2px`
  `2m1x` `2m2x` `2ox` — all lowercase, fresh 1–4, then hidden.
  **No 15S rows** — 15S data flows for Ultra Scalping but is never a row.
- Fixed-height scroll box (11 visible rows + 3 blank filler rows for height
  alignment); scrollbar appears automatically with more records.

## 3. BIAS table — rules (top-most, spans both tables)

Caption row: grey header `#4a4a4a` with `PA / 5PM / CV / 3S` and, after a
small gap, `3PM`. Below it the trend row: colour-filled cells, one arrow per
underlying component (▲ up / ▼ down / – unknown), black 1px cell borders.
Strip width hugs its content so a future table can sit to the ladder's right.

- **PA** — green when price takes support & rises **with the 1PM trend
  started** (the `1e50` rule); red when facing resistance & falling. Arrow ▲/▼.
- **5PM** — background = 5m **PVT vs zero** (green >0 / red <0); arrows =
  M1 (MACD 12/26) then M2 (MACD 24/52) on 5m.
- **CV** — Close vs session VWAP: above green, below red. Arrow ▲/▼.
- **3S** — 30m Stoch. Arrow = K-trend + zone text (`▼ OSZ`). Colour =
  **2PM direction × zone**: 2PM up → OSZ/MZ dark-green, OBZ red;
  2PM down (mirror) → OSZ/MZ dark-red, OBZ green; 2PM mixed → grey.
- **3PM** — background = 30m **PVT vs zero** (green >0 / red <0); arrows =
  M1 then M2 on 30m (e.g. `▼▲` = M1 retracing down, M2 still up).

## 4. SET UP table — rules (third, left)

Rows are timeframes `1 / 5 / 3 / 2 / D`. Columns are FIXED: **left always
green, right always red**. Every component of the row is evaluated
individually — up-components occupy the left cell, retracing/downtrending
(or unknown) ones the right cell. Same-TF components concatenate
(`5P`+`5M1`→`5PM1`), cross-TF join with `_` (`5M2`+`3S`→`5M2_3S`).

| Row | Components (in join order) |
|---|---|
| 1 | `1P` (1m PVT) · `1M1` (1m MACD 12/26) · `1M2` (1m MACD 24/52) · `1E50` (price ≥ 1m EMA50) · `5S` (5m Stoch K-trend) |
| 5 | `5P` · `5S` (5m Stoch) · `5M1` · `5V` (5m VOC) · `5M2` (5m MACD 24/52) · `3S` (30m Stoch) |
| 3 | `3P` · `3S` (30m Stoch) · `3M1` · `3M2` · `2S` (2h Stoch) |
| 2 | `2P` · `2S` (2h Stoch) · `2M1` (2h MACD 12/26) · `2M2` (2h MACD 24/52) · `DS` (Daily Stoch) |
| D | `DP` (Daily PVT) · `DS` (Daily Stoch) · `DM1` (Daily MACD 12/26) · `DM2` (Daily MACD 24/52) · `WS` (Weekly Stoch) |

## 5. LEVELS LADDER table — rules

- Source: `LEVELS.CW_CD` of the webhook LEVELS message (`[Name, Price]`
  pairs, HA-based per Glossary §"LEVELS from the TradingView webhook
  payload"). Parsed and re-served by `forecasting/momentors_widget.py`.
- Filter: rows within **±150 pts** of the Close; sorted price-descending;
  **Close row on a bright yellow background** (`#ffd54d`, bold dark text,
  `<<<` marker) — always prominent mid-table.
- `Pts` = |level − close| (headroom).
- **Chips** (4th column, header **SPATAM**, styled like the MOMENTORS 2nd
  column: colour-filled cell, fixed text colour) — since Pine v1.12/v1.13 the
  chip is the **SPATAM family trend tag, VERBATIM from the webhook**
  (`LEVELS.FAM`, decoded by `build_ladder` in `forecasting/momentors_widget.py` —
  see `spatam_levels_ladder_fam.md`). TradingView is the single source of
  truth; the app never recalculates it. Dark green/red when MACD2 agrees with
  the PVT direction, light shades when it retraces against it. Example chips
  (verified 2026-11-04): `5PM2` `3PM2` `2PM` `MPM` `QPM2`. A higher-TF PM
  uptrend endorses its lower-TF EMA rungs as supports (green chip); downtrend
  makes them resistances (red). Historic (pre-v1.12) fixed chip map:

  | Chip | Endorsing state | Rungs |
  |---|---|---|
  | `3PM2` | 30M PVT+ & Trend_2452+ | 1m EMA100, 1m EMA200 |
  | `2PM` | 2H PVT+ & Trend_2452+ | 5m EMA50, 5m EMA100, 5m EMA200 |
  | `WPM` | W PVT+ & Trend_2452+ (HTF block) | 2h EMA200 |
  | `MPM` | D PVT+ & Trend_2452+ (HTF block) | D EMA100, D EMA200 |
  | `QPM2` | W PVT+ (HTF block) | W EMA100 |

- Scroll container ~411px (≈18 rows visible), border wraps the scrollbar.

## 6. TradingView architecture (Pine v1.9) — CRITICAL

**The 15-firings-per-3-minutes rule**: TradingView auto-disables any alert
that fires more than 15 times in 3 minutes. ALL `alert()` calls in one
script instance count as ONE alert. Therefore:

- The script runs on the chart **once per message type** ("Emit" input:
  `ALL` / `DATA` / `LEVELS` / `OPT`), each instance with **its own TV
  alert**, firing exactly ONE message per bar close (12/3min on a 15s
  chart — safely under the limit).
- `ALL` (default) fires all messages from one alert — **5m+ charts only**.
- Live setup (BTCUSD weekend test): three 15s-chart instances —
  `DATA` / `LEVELS` / `OPT` — one alert each, same webhook URL.
- **Message budget**: TV silently drops any `alert()` message over
  **4096 chars**. DATA is ~3,999/4,096 after v1.9 — **do not add payload
  fields without re-checking the length** (see `incoming/latest_DATA.json`).
- Messages per bar close: `DATA` (INDICATORS 5S–2H + SETUPS),
  `LEVELS` (+ `HTF` block: W/D/Q PM states, v1.8 D/W Trend_1226/K/KTrendFlag),
  `OPT` (ATM CE/PE).
- v1.2 pins ALL data to `NSE:NIFTY1!` (`DATA_SYM`) — genuine Nifty data from
  any chart (BTCUSD used for weekend testing). Caveat: `ORH`/`ORL` and
  session stamps key off the chart's session — not NSE-meaningful on a 24/7
  chart.
- Version ceremony: bump VERSION + CHANGELOG, dated snapshot in
  git tag `tv-data-vX.Y` (no physical snapshot copies; TV keeps version history).

## 7. App-side implementation

| Piece | Where |
|---|---|
| Pure builders (`build_topstrip`, `build_momentors`, `build_states`, `build_ladder`, `CHIP_MAP`) | `forecasting/momentors_widget.py` |
| Endpoint (read-only, 1s TTL cache, fail-safe → HTTP 200 + `error`) | `server.py` `GET /momentor/data` |
| Widget UI + 2s poller (IIFE, try/catch) | `templates/app.html` (`#ml-widget-container`, below Pending Orders) |
| Webhook envelopes (LEVELS/HTF stored, never clobber the snapshot or run trade logic) | `core/trade_logic.py` `process_TradingView_Data` — **the envelope checks MUST run before `_tradingView_Data = data`** (a v1.7 regression made the widget blank between DATA messages) |
| Raw webhook dump (debug aid) | `server.py` `_tv_dump_incoming` → `tv/tv_algo_json/incoming/` (gitignored, retention-capped ~300 files) |

## 8. Persistence (restart-safe)

DATA / LEVELS / HTF payloads are merged into `data/tv_last_snapshot.json`
(gitignored; atomic write, one write per 5s). `/momentor/data` falls back to
it when the in-memory snapshot or levels are empty (`"source": "persisted"`).

## 9. Safety guarantees

- **Read-only widget**: no writes, no config keys, no order-path interaction.
- The only trade_logic touch is the envelope early-returns (additive; they
  return `[]` before any trade logic runs).
- Fail-safe endpoint + isolated render — a widget failure cannot break the
  order ticket or pending-orders grid.

## 10. Known limitations / next steps

1. DATA message at ~3,999/4,096 chars — next field addition needs trimming
   (candidates: `PVTPoiseFlag`/`StochPoiseFlag`, low-value decimals).
2. 15s-fresh entries driven by a fast-path message would be a deliberate
   order-path change — deferred (display-only architecture today).
3. Only ONE live alert per message type; recreating an alert requires
   re-selecting the matching Emit instance.
4. BTCUSD test alerts fire 24/7 — disable before NSE open; the NSE-chart
   alert set (same 3-instance pattern) is the market-hours source.
