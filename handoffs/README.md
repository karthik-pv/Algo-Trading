# HANDOFFS — one living doc per domain

Each domain has exactly **ONE living handoff**, updated in place. Dated, frozen history
lives in `archive/` (read-only; each file carries a supersession banner).

| Domain | Living doc | Read it when… |
|---|---|---|
| **Forecasting** (1PME50 / 3PM / 2PM / DPM / levels, Scalping & Intra Swing styles) | [`forecasting.md`](forecasting.md) | any forecasting, insight-mining, or trading-setup session |
| **App Support** (server, brokers, feeds, ngrok, machine config) | [`app_support.md`](app_support.md) | the app breaks, feeds/tunnels, machine/broker setup |
| **Commentary** (tracking & commentary module) | [`commentary.md`](commentary.md) | tracking sessions, commentary vocabulary/style |
| **Trade Backtest** (validated studies & playbooks) | [`trade_backtest.md`](trade_backtest.md) | backtesting setups, validation runs |
| **SPATAM ↔ Levels Ladder** (FAM tag column, wire format, renderers) | [`spatam_levels_ladder_fam.md`](spatam_levels_ladder_fam.md) | the ladder tag column mismatches the chart, or the LEVELS payload changes |
| **SPATAM Algo UI Tables** (the 4 widget tables: BIAS, MOMENTORS, SET UP, LEVELS LADDER) | [`1pm_momentors_levels.md`](1pm_momentors_levels.md) | a widget table looks wrong, or table naming/layout questions come up |
| **SPATAM** (the TV indicator: tables, engine, alert families) | [`spatam.md`](spatam.md) | editing/deploying the SPATAM Pine script, alert families, security budget |
| **TradingView Algo JSON** (the DATA/LEVELS feed producer) | [`tradingview_algo_json.md`](tradingview_algo_json.md) | the JSON feed producer, payload formats, deploy runbook |
| **TREPAT** (trend price-action tracking commentator) | [`trepat.md`](trepat.md) | tracking-commentary sessions (guideline + engine) |

Vocabulary for all modules: [`forecasting/Glossary.md`](../forecasting/Glossary.md).

## Rules (the contract that makes this work)

1. **Living docs are never dated, never rotated** — updated in place; each has an
   append-only **Changelog** section (never rewrite past entries).
2. **A session touches exactly ONE living doc** (its domain). Cross-domain sessions put
   the entry in the primary domain's doc + a one-line cross-reference in the other.
3. **Archive is write-off only**: `git mv` the dated file into `archive/`, prepend a
   supersession banner pointing at the living doc + section. Never edited after.
4. **New domain → new living doc + a README table row.** The table is the router.
5. If a living doc outgrows ~800–1000 lines, split aspect files under a domain folder —
   not before.

Superseded/one-off handoffs: [`archive/`](archive/) — `2026-10-19_mac_migration.md` ·
`2026-10-20_pm_ladder.md` · `2026-10-24_data_refresh.md` · `2026-10-25_2pmm.md` ·
`2026-10-26_2pmm.md` · `2026-10-27_trepat_tracking_session.md` ·
`PAInsight_forecasting_engine.md` · `SPATT.md`.
