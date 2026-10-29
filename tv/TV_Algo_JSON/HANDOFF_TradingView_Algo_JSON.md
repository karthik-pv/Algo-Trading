# Handoff — TradingView → Algo App JSON Feed

**Producer:** `Strategy_TradingInvestAlgo_Data` (Pine v5 indicator)
**Location:** `/Users/phvrao/Projects/TradingView/Strategy_TradingInvestAlgo_Data`
**Consumer:** the "forecasting" algo app (webhook receiver)
**Status:** LEVELS section (S/R levels from `Strategy_EMAs_OCHL_cwcd v1.1`) added 2026-10-18. Same day, payload was **split into two alert messages** (`DATA` / `LEVELS`) to respect TradingView's **4096-char per alert message** limit; level rows are compact `[Name, Value]` pairs.

---

## 1. Transport

- The script is an **indicator** (not a strategy). It calls `alert(jsonStr, alert.freq_once_per_bar_close)` only when `barstate.isrealtime and barstate.isconfirmed`.
- To activate: create **one alert** in TradingView on the chart → condition = the indicator → *"Any alert() function call"* → **Webhook URL** = the app endpoint. The URL currently noted at the bottom of the script is an ngrok tunnel; whatever URL is entered in the alert dialog is what gets used.
- **Cadence:** **two messages** per **confirmed realtime bar** of the chart timeframe — first `MSG:"DATA"`, then `MSG:"LEVELS"`. No historical/backfill messages on load — the first pair arrives at the next bar close.
- **Payload:** each JSON string is the raw POST body (TradingView sends it as the request body; do not expect form fields).
- **Size:** TradingView caps one `alert()` message at **4096 characters** (observed limit — the combined payload of ~7.2K chars is rejected). The two messages are sized to stay under it: `DATA` ≈ 3.5–4 KB, `LEVELS` ≈ 3–3.8 KB. Don't add fields without re-checking message length.

## 2. Root schema

Two messages arrive per bar close, both carrying `MSG` (type) and `SYM` (instrument):

```json
// message 1 — MSG:"DATA"
{
  "MSG": "DATA",
  "SYM": "NSE:NIFTY261006C22700",
  "INDICATORS": { "5S": {}, "15S": {}, "1M": {}, "5M": {}, "30M": {}, "2H": {} },
  "SETUPS":     { "DASHBOARD": {}, "1M": {}, "5M": {}, "30M": {}, "2H": {}, "D": {} }
}

// message 2 — MSG:"LEVELS"
{
  "MSG": "LEVELS",
  "SYM": "NSE:NIFTY261006C22700",
  "LEVELS": { "CW_CD": [], "CW_ND": [], "NW_ND": [] }
}
```

Notes:
- Route every POST by `MSG`; key instruments by `SYM` = `syminfo.tickerid` (`EXCHANGE:TICKER`, e.g. `NSE:NIFTY261006C22700`).
- `INDICATORS` has **no** `"D"` / `"W"` blocks (they are computed in the script but currently commented out of the JSON). Daily/weekly context still arrives via `SETUPS.DASHBOARD` and `LEVELS` (D/W EMAs, pivots, PDH/PWH, VWAP W, …).
- All multi-timeframe values are fetched with `request.security(..., lookahead_on)`; on realtime bars the higher-timeframe fields reflect the **developing** (live) bar of that timeframe.
- The payload contains **no timestamp** — stamp messages with receiver-arrival time. `SYM` identifies the instrument.

## 3. `INDICATORS.<TF>`

TF keys: `5S`, `15S`, `1M`, `5M`, `30M`, `2H`. All TFs are fetched explicitly; the chart TF only sets the send cadence.

| Key | Meaning |
|---|---|
| `PVT` | Normalized PVT oscillator (2dp, roughly −1…+1) |
| `PVTPoiseFlag` | 1 = PVT rising vs previous bar, else −1 |
| `PVTTrendFlag` | 1 = PVT above 0, else −1 |
| `PVTCrossOverIndex` | Bars since PVT crossed **above** 0, +1 (1 = on this bar) |
| `PVTCrossUnderIndex` | Bars since PVT crossed **below** 0, +1 |
| `Trend_1226` / `Trend_2452` | MACD line vs signal: 1 = above (bull), −1 = below, for MACD(12,26,9) / MACD(24,52,9) |
| `X_1226` / `Y_1226` | Bars since 12/26 MACD crossover / crossunder, +1 |
| `MACD_1226`, `Signal_1226`, `Hist_1226` | MACD(12,26,9) values (2dp); `*_2452` for MACD(24,52,9) |
| `ZCross` | Early-reversal marker for MACD crosses: 1 = 12/26 cross **up while still below** signal, 2 = 24/52 same, 3 = both; −1/−2/−3 = same but cross **down while still above**; 0 = none |
| `K` | Stochastic %K (RSI-stoch 14, K smoothed over 6), 2dp |
| `KTrendFlag` | 1 = K above D, −1 = below |
| `StochPoiseFlag` | 1 = K rising vs previous bar, else −1 |
| `KCrossoverIndex` / `KCrossUnderIndex` | Bars since K crossed D up / down, +1 |
| `EMA9`, `EMA21`, `EMA50`, `EMA100`, `EMA200` | That TF's EMAs of close, rounded to whole points |
| `VWAP` | Session-anchored VWAP of hlc3 (2dp) |
| `VOC` | Volume oscillator: 100·(EMA(vol,9)−EMA(vol,21))/EMA(vol,21), 2dp |
| `VOCTrendFlag` | 1 = VOC rising, −1 = falling |
| `VOCCrossOverIndex` / `VOCCrossUnderIndex` | Bars since VOC crossed 0 up / down, +1 |

All "*Index" fields: **1 = cross on the just-closed bar**, larger = older. See edge case in §6 about `NaN`.

## 4. `SETUPS`

### `SETUPS.DASHBOARD`
Higher-TF trend board:

| Key | Meaning |
|---|---|
| `DPM`, `2PM`, `3PM` | Two-letter MACD combo for **D**, **2h**, **30m**: first letter = 12/26 MACD, second = 24/52; letters `U`/`D` → e.g. `"UU"`, `"DU"` |
| `DPMPVT`, `2PMPVT`, `3PMPVT` | PVT sign (1/−1) for the same TFs |
| `WS`, `DS`, `2S` | Stochastic K>D trend for **W**, **D**, **2h**: `"U"`/`"D"` |
| `WSPVT`, `DSPVT`, `2SPVT` | PVT sign (1/−1) for W / D / 2h |

### `SETUPS.<TF>` — `1M`, `5M`, `30M`, `2H`, `D`
Each block: `Uptrend`, `Downtrend`, `CrossUptrend`, `CrossDowntrend` — short code strings (empty string = not active). Token legend:

- Leading TF prefix: `1`, `5`, `3`, `2`, `d`, `D`
- `p` PVT aligned · `s` stochastic aligned · `m` both MACDs aligned · `m1` 12/26 aligned · `m2` 24/52 aligned
- `e50` close vs EMA50 (1M block) · `v` close vs VWAP (5M block)
- Cross tables: `vX` VWAP cross (1M), `p`/`o`/`O` fresh PVT/VOC crosses, trailing `X` marker
- Suffix `_5s` / `_3s` / `_2s` / `_ds` = higher-TF stochastic also aligned

(Origin: the `Strategy_SetUps_Label` tables — treat the codes as opaque, display-oriented strings.)

## 5. `LEVELS` — S/R levels (new)

Three arrays, mirroring the `Strategy_EMAs_OCHL_cwcd v1.1` tables:

| Key | Table |
|---|---|
| `CW_CD` | Current week / current day — the main live S/R table |
| `CW_ND` | Current week / next day (adds next-day pivot projections) |
| `NW_ND` | Next week / next day |

Each element is a compact pair — `["PDH", 25123]`:

- `[0]` **Name** — row label (same names as the on-chart table)
- `[1]` **Value** — the level, rounded to whole points (same rounding as the table)
- `["Close", <close>]` is included in every array as the reference point. Distance is **not** pre-computed (it was dropped to stay under the 4096-char cap) — derive it as `abs(Value − Close value)` on your side.
- **Ordering:** sorted by `Value` **descending** (highest resistance first). The `Close` row sits mid-array; entries above it are resistances, below it supports.

### Name catalog
- **EMAs:** `1m EMA50/100/200`, `5m EMA50/100/200`, `30m EMA50/100/200`, `2h EMA50/100/200`, `D EMA100/200`, `W EMA100/200` (no EMA9/21, no 5S/15S EMAs — those are only in `INDICATORS`)
- **Floor pivots, previous completed period:** `P D`, `R1 D`, `R2 D`, `R3 D`, `S1 D`–`S3 D` · `W P`, `W R1`–`W R3`, `W S1`–`W S3` · `M P`, `M R1`–`M R3`, `M S1`–`M S3` · `Q P`, `Q R1`–`Q R3`, `Q S1`–`Q S3`
- **Next-period projections:** `D P`, `D R1`–`D R3`, `D S1`–`D S3` (in CW_ND, NW_ND) · `W P`, `W R1`–`W R3`, `W S1`–`W S3` (in NW_ND)
- **R4/S4 & R5/S5:** `R4 D`, `S4 D`, `R4 W`, `S4 W`, `R4 M`, `S4 M`, `R4 Q`, `S4 Q` (and `R5…`/`S5…` only if the input is enabled — default **off**)
- **Extras:** `PDH`, `PDL`, `PDC`, `D Open` · `Cam R1`–`Cam R4`, `Cam S1`–`Cam S4` · `VWAP D`, `VWAP W` · `PWH`, `PWL`, `PWC` · `PMH`, `PML` · `RN -`, `RN +` (nearest round numbers, 50-pt step input) · `ORH`, `ORL` (opening range, first 15 min by default) · `Fib 38.2 D`, `Fib 50 D`, `Fib 61.8 D` (prev-day range)

Each family can be switched off via the indicator's "Extra S/R Levels" inputs; disabled families simply don't appear in the arrays. `ORH`/`ORL` only appear once the opening range has formed.

## 6. Consumer gotchas / edge cases

1. **Rare `NaN` indices:** the "*Index" fields are `bars-since-cross + 1`. If no cross exists in loaded history, Pine yields `na` and the JSON may contain a bare `NaN` (invalid per strict JSON spec). Parse tolerantly (e.g., Python `json.loads(..., parse_constant=lambda c: None)`) or patch the script later to emit `null`.
2. **No timestamp in payload** — stamp messages with receiver-arrival time. Instrument is identified by `SYM` (`EXCHANGE:TICKER`); multiple charts/alerts can feed one endpoint and be demultiplexed by that field.
3. Values are already rounded (EMAs/levels whole points, oscillators 2dp) — don't expect tick precision.
4. `LEVELS.Value` for the `Close` row equals chart close at bar close — useful as the "current price" anchor if you don't get price elsewhere.
5. If the indicator settings (inputs) are changed, the level mix in `LEVELS` changes accordingly; treat unknown `Name` values gracefully and just index by name.
6. The alert only fires in **realtime** (never on historical bars) — so backfilling requires letting it run, or reading stored messages.
7. **Two messages per bar** (`MSG:"DATA"` then `MSG:"LEVELS"`) exist because TradingView rejects a single `alert()` message over 4096 chars. Never assume one POST contains both halves; route on `MSG`.
8. Enabling the `R5 / S5` input adds 24 rows to the LEVELS message — if it ever trips the 4096 cap, turn R5/S5 off (default is off) or drop `CW_ND`/`NW_ND` on the receiver.

## 7. Suggested receiver handling (forecasting session)

- Endpoint: accept POST, parse body as JSON (tolerant, per §6.1), route by `MSG` (`DATA` vs `LEVELS`), key by `SYM`, store raw message + received-at timestamp.
- For "distance to nearest S/R" logic: take `LEVELS.CW_CD`, find the `["Close", …]` pair → nearest resistance = first pair above it, nearest support = first pair below it; distance = `|value − close|`. Arrays are sorted, so a simple scan suffices.
- For regime/trend logic: prefer `SETUPS.DASHBOARD` (higher-TF MACD/stoch/PVT state) over per-bar indicator sniffing.
- Keep a small ring buffer of the last N messages per chart if you want poise/momentum deltas (X rising/falling, flags flipping).

## 8. Pointers back to source

- Producer script: `/Users/phvrao/Projects/TradingView/Strategy_TradingInvestAlgo_Data`
- Levels reference (visual table version): `/Users/phvrao/Projects/TradingView/Strategy_EMAs_OCHL_cwcd v1.1`
- SetUps code origin: `/Users/phvrao/Projects/TradingView/Strategy_SetUps_Label`
