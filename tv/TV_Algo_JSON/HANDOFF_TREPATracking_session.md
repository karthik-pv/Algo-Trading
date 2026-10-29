# Handoff — TREPA Tracking Session

**Session:** `ses_e84cd6526ffeq86sJAeY6s59JM` (OpenCode, titled "TREPATracking")
**Date:** 2026-10-27 (Tue), set up pre-market ~08:00–08:20 IST
**Purpose:** Running trend + price-action commentary desk for NIFTY Futures intra / scalping / ultra-scalping, driven by the live TradingView → Algo app JSON feed.

---

## 1. What the session is for

User (phvrao) trades NIFTY Futures intra-day in scalping / ultra-scalping styles. The agent tracks trend and price action from the live TradingView alert feed and gives continuous commentary:

- Trend stack read (D → 2H → 30M → 5M → 1M)
- Price action vs VWAP / EMAs / live S/R ladder
- Momentum state (PVT poise, fresh crosses, VOC)
- Alerts on predefined setups (2PM UT/DT, 5PXU/5PXD, CE/PE gate flips, SPATAM)
- Forecasting commentary encouraged — "without fear of being wrong"

## 2. The data feed (verified end-to-end)

```
TradingView alert (JSON, TV throttles ≤1 msg / 15 s per alert; bursts expected)
  → ngrok tunnel: kendra-preantepenultimate-complementarily.ngrok-free.dev
      (ngrok PID 16588, started 07:03 IST, forwards to localhost:5050)
  → Algo-Trading app server.py  [PID 16472, cwd /Users/phvrao/Projects/Algo-Trading]
      POST /trading_view_webhook
  → trade_logic.process_TradingView_Data  (core/trade_logic.py:7668)
      - stores _tradingView_Data in memory, emits to frontend via Socket.IO
  → forecasting/engine.py  on_snapshot()
      - LOGS EVERY SNAPSHOT + FORECAST TO:
        /Users/phvrao/Projects/Algo-Trading/data/forecasts/<YYYY-MM-DD>.jsonl
        each line: {"ts": iso, "snapshot": <DATA msg>, "forecast": {...}}
```

**Reading point for the agent:** tail `data/forecasts/<today>.jsonl`. Poll, dedupe/merge
15-second bursts into one coherent state before commenting.

**Message envelope** (two POSTs per confirmed chart bar — route on `MSG`):
- `MSG:"DATA"` — `SYM` + `INDICATORS{5S,15S,1M,5M,30M,2H}` + `SETUPS{DASHBOARD,1M,5M,30M,2H,D}`
- `MSG:"LEVELS"` — `SYM` + `LEVELS{CW_CD,CW_ND,NW_ND}` (S/R ladder, sorted desc, `["Close",v]` anchor)

**Full schema doc:** `/Users/phvrao/Projects/TradingView/TV_Algo_JSON/HANDOFF_TradingView_Algo_JSON.md`
(§6 consumer gotchas: bare NaN → parse tolerantly; no timestamp in payload — stamp on arrival;
TV 4096-char cap is why DATA/LEVELS are split; R5/S5 default off.)

## 3. What was built this session

- **`TV_Algo_JSON/trepa_receiver.py`** — standalone backup webhook receiver (port 8080).
  - Currently RUNNING in background (OpenCode shell `sh_17b375aa40016kFVVvtv5HLae8`), NOT in the alert path.
  - Writes `alerts_log.jsonl` + rolling `state_latest.json` next to itself.
  - Use it only if the main ngrok→5050 path is taken down (e.g., to run commentary without the algo app).
- **My ngrok attempt failed by design:** free plan has one pinned domain, already owned by the algo app's agent (ERR_NGROK_334). Left the existing tunnel untouched — do NOT kill PID 16588 while the algo app is in use.

## 4. Key state at handoff

- ~08:15 IST, pre-market. No `2026-10-27.jsonl` yet (feed only writes on live bars; last file was 2026-10-19 — **verify at 9:15 open that a new file appears**; if not, check that the forecast engine is active in trade_logic).
- NSE data files present: `NSE_NIFTY261006C22700/...P22700` 1s CSVs (weekly expiry 06-Nov-2026 chain).

## 5. Instrument / symbols

- Primary: **NIFTY Near-Month Future** `NSE:NIFTY1!` (producer script input).
- Feed SYM may arrive as option tickers (e.g., `NSE:NIFTY261006C22700`) when alerts come off the ATM-option scripts (`NMF_ATM_OPTIONS_1PX_15O_5sPM_Alert` etc.).

## 6. Open items / next steps for the new session

1. After 9:15 IST: confirm `data/forecasts/2026-10-27.jsonl` is growing; if stale since 10-19, debug engine activation.
2. Agree commentary cadence with user (offered: fixed 15-min checkpoints vs. on-ping + setup alerts only — unanswered).
3. Set up burst-merge logic (multiple msgs ≤15 s apart → single commentary state).
4. Track forecast calls vs. outcomes through the day to score the "fearless forecasts."

## 7. Relevant paths

| What | Path |
|---|---|
| This project (Pine scripts, alerts, receiver) | `/Users/phvrao/Projects/TradingView` |
| Backup receiver | `/Users/phvrao/Projects/TradingView/TV_Algo_JSON/trepa_receiver.py` |
| Feed schema doc | `/Users/phvrao/Projects/TradingView/TV_Algo_JSON/HANDOFF_TradingView_Algo_JSON.md` |
| Algo app (feed consumer) | `/Users/phvrao/Projects/Algo-Trading` |
| **Live snapshot+forecast log** | `/Users/phvrao/Projects/Algo-Trading/data/forecasts/<date>.jsonl` |
| Insight store / PA tradecase | `/Users/phvrao/Projects/Algo-Trading/data/insight/` (store.json, setups/setup_pa_tradecase.json) |
| App logs | `/Users/phvrao/Projects/Algo-Trading/data/logs/<date>/app_*.log` |
| Producer Pine (source of feed) | `/Users/phvrao/Projects/TradingView/Strategy_TradingInvestAlgo_Data` |
| Setup-label token legend | `/Users/phvrao/Projects/TradingView/Strategy_SetUps_Label` |

---
*The session can be continued by passing `sessionID: ses_e84cd6526ffeq86sJAeY6s59JM` back to the subagent, or simply by following §2/§6 above with fresh context.*
