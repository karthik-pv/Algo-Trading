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

## How to verify what TradingView is running

The script's first lines show `VERSION: x.y` — compare against the newest
snapshot in this folder. If they differ, re-paste the latest snapshot.
