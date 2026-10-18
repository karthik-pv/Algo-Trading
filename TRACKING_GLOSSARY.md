# Tracking Glossary — shared nomenclature

Derived from the **Forecast** sessions ("Forecasting trading setups for scalping",
"Forecasting good 1PM Swing conditions") + `forecasting/HANDOFF.md` + `SPATT_HANDOFF.md`.
This is the canonical vocabulary for all Tracking / Commentary entries. If a term is used
differently anywhere, this file wins.

---

## Trend (market state — one per window)

| Term | Meaning | xlsx fill |
|---|---|---|
| `UT` | Uptrend | dark green `#548235` |
| `DT` | Downtrend | dark red `#C00000` |
| `RT_UP` | Retracement up | light green `#C6EFCE` |
| `RT_DOWN` | Retracement down | light red `#FFC7CE` |

**Precedence when assigning trend:** `3S_5PM_CV` → `3PMX` → `3PM`
(3S_5PM_CV wins even if 3PM points the other way; 3PMX wins whenever the cross index is 1–13 same-day.)

## Setup acronyms — Library v1 (setup_library.json)

| Acronym | Setup Name | Trend fit |
|---|---|---|
| TDY | Trend Day Ride | UT / DT |
| TCP | Trend Continuation Pullback | UT / DT |
| VWR | VWAP Reject Fade | RT (in DT) |
| VWH | VWAP Hold Bounce | RD (in UT) |
| ORB | Opening Range Breakout | UT / DT |
| PDX | PDH/PDL Sweep & Reclaim | RT / RD |
| SQZ | Squeeze / Compression Breakout | UT / DT |
| TRP | Failed Breakout / Trap Reversal | RD / RT |
| RNG | Range Fade | — (no trend) |
| GAP | Gap Fill | UT / DT |

## Setup acronyms — v2 trend-state classes (setup_occurrences_v2)

| Acronym | Rule | Trend |
|---|---|---|
| `3S_5PM_CV` | 3S retracing up (30m Stoch rising, 30m PVT still red) + 5PM up + close above VWAP → RT_UP; mirrored → RT_DOWN | RT_UP / RT_DOWN |
| `3S` | 30m Stoch turning, 30m PVT still red (stoch leads) | RT_UP / RT_DOWN |
| `3SP` | 30m Stoch AND 30m PVT both turning (confirmed) | UT / DT |
| `3PMX` | 3P/3M1/3M2 crossed up/down, index 1–13 candles since cross, **same-day credit only** (next day degrades to 3PM if P/M2 still trending) | UT / DT |
| `3PM` | 3P and 3M2 both up/down — no cross credit | UT / DT |

## Indicator shorthand

- **PVT/MACD state per TF:** `DPM` (Daily) · `2PM` (2H) · `3PM` (30m) · `5PM` (5m) · `1PM` (1m) · `WPM` (Weekly)
- **Stoch per TF:** `DS` (Daily) · `2S` (2H) · `3S` (30m) · `WS` (Weekly)
- **CV** = Close vs VWAP · **MACD_2452** = the 24/52 pair (USE) · **M1** = 12/26 pair (DISCARD) · **M2** = 24/52
- **3M1/3M2, 2M1/2M2, DM1/DM2** = MACD pairs per TF (Numbers-doc vocabulary)
- **OSZ** = Oversold Zone (30m Stoch K<25) · **Cross credit / index** = bars since MACD cross (1–13)
- **IS1** (execution layer) = 5PM · CV · 3S · 3PM · 2S
- **IS2** (context layer) = 2PM · DS · DPM · WS · WPM

## Regime & levels

- Regime notation: `D−|2H+|30−` — PVT signs per TF; **D = previous day's D-PVT sign**
- **PDH / PDL / PDC / PDOpen** = prior-day high / low / close / open · **Round100** = 100-pt NIFTY multiples (23000, 23100, …)
- EMA rungs: `3E50` = 30m EMA50, `5E200` = 5m EMA200, `1E100` = 1m EMA100, `D E100` = Daily EMA100
- Major rung set for Price Action labels: 1E100, 1E200, 3E50, 3E100, 5E50, 5E100, 5E200
- **Touch** = high ≥ level · **Reject** = close back below · **Hold** = close back above · **FT** = follow-through (Y/N, next-30m-bar)
- Window format: `Date | Start→End` in 30m steps; same-class windows <10 min apart merge

## Commentary vocabulary (1PM Swing session)

- Afternoon classification: **SWING** / **STOCH-CHOP** / **DEAD** (base rates 40/43/17%)
- **1PM Swing** = net 13:15→close move ≥0.25% (~60 pts), one-sided ≥45% efficiency (**eff**)
- **1m Stoch Swing day** = chop day; scalp 1m stoch crosses instead of holding a directional swing
- Tension (regime flip / PVT curl against trend) → SWING; full alignment by 13:00 → CHOP/DEAD
- **Fell from** = nearest level above window close that the window high touched

## Playbook setups (SPATT, setup_pa_tradecase.json — 593 rows)

| Setup | Trigger | Exit | Tradecase |
|---|---|---|---|
| 23PM_5PM | 2PM+3PM+5m PVT/MACD all aligned (hand-marked phases) | — | SCALP_1SPX_15PM |
| 23PM_5PX | 2PM up + 3PM up, 5m PVT cross-up | first 5m close below own **EMA4** | SCALP_23PM_5PX |
| 2PM_3PX | 2PM up at a 30m PVT cross-up | first 5m close below own **EMA21** | INTRA_5PM |
| ↓ mirrors | PVT/MACD all down, cross-down trigger | 5m close above own EMA4/EMA21 | same per family |

All exits EOD-flat at 15:25. "Points Moved" = move in trend direction (positive = worked).

## Execution horizons

`ultra-scalp` / `scalp` / `intraday` — the three TradeCase boxes per setup.

---

## 15s export column map (`NSE_NIFTY1!, 15S_*.csv` — 45 cols, verified vs 1m.csv 2026-10-18)

| Cols | Content |
|---|---|
| 1–5 | time (15s stamps), OHLC |
| 6–7 | 15s EMA9 / EMA21 |
| 8–12 | 1m EMA9/21/50/100/200 |
| 13–15 | 5m EMA50/100/200 |
| 16–18 | 30m EMA50/100/200 |
| 19–21 | 2H EMA50/100/200 |
| 22–27 | D/W/M EMA100/200 |
| 28 | VWAP |
| 29–30 | **15s MACD** (pair 1 — the fast pair) |
| 31–32 | **1m MACD** (pair 2, 24/52; carried within the 1m bar) |
| 33 | **1m VOC** (header says "Plot"; 15s-resolution, runs wider than 1m.csv's) |
| 34–37 | **1m PVT** + Plot + Cross Up/Down markers |
| 38–39 | **1m Stoch K/D** |
| 40–41 | **5m MACD** (fills on 5m closes, carried) |
| 42–45 | **5m PVT** + Plot + Cross Up/Down markers |

User's pane scan order (read right→left): 5m PVT → 5m MACD → 1m Stoch → 1m PVT → 1m VOC → 1m MACD → 15s MACD.
No 5m Stoch / 5m VOC in this export. File lives in `data/insight/csv/` (copied from Downloads 2026-10-18).

## Cross-event naming — the full momentor net (confirmed against `MOMENTOR_COLS`, trend_insight.py)

| Momentor | Meaning | Tracked from |
|---|---|---|
| `3PX` / `3MX` / `3M1X` / `3SX` / `3OX` | 30m PVT sign · MACD M2 (24/52) · MACD M1 (12/26) · Stoch K vs D · VOC sign | 5m file, true 30m closes only (`:40/:10` stamps; `:15` readings are intrabar recomputes — never call a cross off them) |
| `2PX` / `2MX` / `2M1X` / `2OX` | 2H same family; 2H VOC updates per 30m step (not per 2H close) | 5m file (2SX pending — no 2H Stoch in export) |
| `DPX` / `DMX` / `DM1X` | Daily same family (D-PVT carried = previous day's final) | 5m file D block (DSX/DOX pending) |
| `1PX` / `1MX` / `1m VOCross` / `1SX` | 1m micro layer (tracking only — not SPATT momentors) | 1m.csv |
| `5PX` / `5MX` / `5m VOCross` | 5m layer | 1m.csv carried / 5m file |

- `3PMX` (the v2 setup acronym, credit 1–13) is a **state** triggered by a same-day `3MX` UP — never call the composite without naming the trigger event.
- Ages: 30m counts the cross candle as 1 (valid 1–13); 2H/D count it as 0 (valid 0–4); outside → NA.
- Not callable: 1M1X / 5M1X (no M1 columns for 1m/5m in any export) · 2SX / DSX / DOX (plots not yet added to the TradingView export).

**Level interactions — all five called, all levels:** **approaching** (Δ ≤ ~12 pts, 1m EMAs excluded — price hugs them) · **crossed above** / **crossed below** / **took support at** / **faced resistance at**, plus the 1m EMA21 retrace-support (wick touch). Ladder: 1E9/1E21/1E50/1E100/1E200 · 5E4/5E9/5E21/5E50/5E100/5E200 · 3E50/3E100/3E200 · 2E50/2E100/2E200 · DE100/DE200 (+ W/M if touched) · VWAP · Round100s · PDH/PDL/PDC/PDOpen. Tight clusters print as one line; same level+event deduped 15 min.

## LEVELS from the TradingView webhook payload (new 2026-10-18 — see `HANDOFF_TradingView_Algo_JSON.md` §5)

Live days: parse `LEVELS.CW_CD` (main table) + `CW_ND`/`NW_ND` (projections) directly — sorted desc, split at the `Close` row for nearest resistance/support; `Distance` field = pts away. Backfills: computed from the same formulas (verified against the Pine source):

| Family | Names | Formula |
|---|---|---|
| Floor pivots (prev completed period) | `D P`, `R1–R4 D`, `S1–S4 D` · `W P`, `W R1–R3`, `W S1–S3` · `M P`… · `Q P`… | P=(H+L+C)/3; R1=2P−L; S1=2P−H; R2=P+(H−L); S2=P−(H−L); R3=2P+(H−2L); S3=2P−(2H−L); R4=3P+(H−3L); S4=3P−(3H−L) |
| Next-period projections | `D P`, `D R1–R3`, `D S1–S3` (CW_ND) · `W P`… (NW_ND) | same formula on the **developing** period OHLC |
| Camarilla (prev day) | `Cam R1–R4`, `Cam S1–S4` | PDC ± (PDH−PDL)×1.1/{12,6,4,2} |
| Fib (prev day range) | `Fib 38.2/50/61.8 D` | PDL + rng×{0.382, 0.5, 0.618} (low-anchored) |
| Round numbers | `RN −` / `RN +` | floor/ceil(close/50)×50 — tracked as the 50-pt ladder (`RN22550`-style) |
| Opening range | `ORH` / `ORL` | first 15 min H/L |
| VWAP | `VWAP D`, `VWAP W` | session VWAP (have it); weekly VWAP needs volume — payload-only |
| Prior periods | `PWH/PWL/PWC` · `PMH/PML` | prev week / month H·L·C |
| EMAs | `1m/5m/30m/2h EMA50/100/200`, `D EMA100/200`, `W EMA100/200` | same ladder as above |

R5/S5 default off. Unknown `Name`s: index by name gracefully (inputs change the mix). Q-pivots in backfills approximate (July 1m data missing).

## Commentary style rules (Tracking session, user-specified 2026-10-18)

1. **Track the setup states by name** — 2PM / 3PM / 5PM (+1PM, DPM) with sign and M2 status (`3P+ 3M2↓`), carried no-repaint.
2. **Every level interaction is called out explicitly**: "crossed the level — PDC (22994) up/down", "price hits VWAP — acting as resistance", "price retraced to 1E21 and took support".
3. **Cross events get their own lines**: "1PM uptrend starts…", "5PX has happened — UP/DOWN", "5m VOCross up/down", "1M/5M M2 cross up/down".
4. **At every 1PM / 15PM trend start, list the nearby levels it could cross** (Round100s, PDH/PDL/PDC, VWAP, EMA rungs within ~120 pts) — proactive, before price gets there.
5. **Don't repeat the Nifty print unnecessarily** — level values and key extremes only (day high/low, closes at section boundaries); never a price on every line.
6. **Role reversals are always flagged** (morning resistance → afternoon support).
7. 15PM: not present in CSV exports — only trackable live from the 15s webhook feed.
