# Script versioning convention

Every change to a TradingView Pine script gets a **new version number** — never
edit in place without bumping. This is what makes it safe to trust what's
running in TradingView.

## Rules (apply to every future change)

1. **Bump the version** in the script's header banner (`VERSION:` + a
   CHANGELOG line describing the change and its date).
2. **Snapshot a copy** in this folder before/with the change:
   `<ScriptName>_v<MAJOR.MINOR>_<YYYY-MM-DD>.pine`
3. **Tag the commit**: `git tag tv-data-v1.1` (pattern: `tv-data-v*` for
   `Strategy_TradingInvestAlgo_Data`).
4. **Commit + push** — the version in TradingView's editor should always match
   the header of the latest snapshot here.
5. **Rollback** = paste the previous snapshot from this folder back into the
   TradingView editor. No git knowledge needed.

## Current versions

| Script | Version | Date | Change |
|---|---|---|---|
| Strategy_TradingInvestAlgo_Data | v1.0 | 2026-10-30 | Baseline (DATA + LEVELS messages) |
| Strategy_TradingInvestAlgo_Data | v1.1 | 2026-10-31 | Added MSG:"OPT" ATM CE/PE indicator data (5S/15S/1M/5M) |
| Strategy_TradingInvestAlgo_Data | v1.2 | 2026-11-01 | Data symbol pinned to NSE:NIFTY1! (DATA_SYM const) — Nifty payloads from ANY chart; payload format UNCHANGED |
| Strategy_TradingInvestAlgo_Data | v1.3 | 2026-11-01 | Chart-context leaks fixed (Nifty close/round-nums/session-VWAP/weekly-VWAP/ORH/ORL via DATA_SYM security) — NSE-chart output UNCHANGED |
| Strategy_TradingInvestAlgo_Data | v1.4 | 2026-11-01 | Compact W/D/Q PM blocks (PVTTrendFlag + Trend_2452) for the ladder chips |
| Strategy_TradingInvestAlgo_Data | v1.5 | 2026-11-01 | FIX: DATA message exceeded the 4096-char alert cap after v1.4 → dropped the 5S/15S blocks (only commented-out code consumed them); DATA resumes |
| Strategy_TradingInvestAlgo_Data | v1.6 | 2026-11-01 | RESTORED 5S/15S blocks (Ultra Scalping needs them); compact W/D/Q PM states moved to the LEVELS message |
| Strategy_TradingInvestAlgo_Data | v1.7 | 2026-11-01 | FIX: alert auto-disable on 15s charts (TV 15-firings/3-min limit) → "Emit" input (ALL/DATA/LEVELS/OPT), one message per alert per bar close |
| Strategy_TradingInvestAlgo_Data | v1.8 | 2026-11-01 | LEVELS message HTF block enriched (D/W Trend_1226, K, KTrendFlag) for the SET UP table — reused existing D/W security, no extra calls |
| Strategy_TradingInvestAlgo_Data | v1.9 | 2026-11-01 | NEW 1CVX/5CVX Momentors rows: price×session-VWAP cross ages (1M/5M, tiny f_vwap_cross security pair); DATA ~3,999/4,096 chars |
| Strategy_TradingInvestAlgo_Data | v1.10 | 2026-11-02 | FIX: v1.9 dropped the comma after VOCCrossUnderIndex (1M+5M blocks) — every DATA message with an active VWAP cross was invalid JSON and the server rejected it (BIAS strip starved). Comma restored; all payloads otherwise unchanged |
| Strategy_TradingInvestAlgo_Data | v1.11 | 2026-11-04 | OPT block REMOVED (moved to the new standalone Strategy_TradingInvestAlgo_Data_OPT indicator); SYM field dropped from DATA + LEVELS (unused downstream, shredded JSON) — all other DATA/LEVELS fields byte-identical to v1.10 |
| Strategy_TradingInvestAlgo_Data | v1.12 | 2026-11-04 | NEW `FAM` in the LEVELS message: the seven SPATAM family trend tags (f_familyTag formula, order 5/3/2/D/W/M/Q, `<tag><U\|D><1\|0>` segments) for the Algo app's Levels-Ladder last column, verbatim. Two tiny M/3M security calls added; LEVELS-HTF wire keys compacted (P/M1/M2/K/KT, K @2dp) to fund it — server expands back on ingest, no downstream reader changes. LEVELS rows + DATA message UNCHANGED. Wire ~3,979/4,096 chars |
| Strategy_TradingInvestAlgo_Data | v1.13 | 2026-11-04 | FIX: v1.12 emitted FAM outside the LEVELS object (root sibling) — the app reads data.LEVELS and never saw it, tag column stayed blank. FAM now emitted INSIDE the LEVELS object (last key). Segment format + wire size UNCHANGED from v1.12 |
| Strategy_TradingInvestAlgo_Data_OPT | v1.0 | 2026-11-04 | NEW standalone indicator: MSG:"OPT" ATM CE/PE indicator blocks spun off from Strategy_TradingInvestAlgo_Data v1.10 — one chart instance, one TV alert; payload byte-identical to v1.10's OPT message minus SYM |

## How to verify what TradingView is running

The script's first lines show `VERSION: x.y` — compare against the newest
snapshot in this folder. If they differ, re-paste the latest snapshot.
