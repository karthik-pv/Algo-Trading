# HANDOFFS — one living doc per domain

Each domain has exactly **ONE living handoff**, updated in place. Dated, frozen history
lives in `archive/` (read-only; each file carries a supersession banner).

| Domain | Living doc | Read it when… |
|---|---|---|
| **Forecasting** (1PME50 / 3PM / 2PM / DPM / levels, Scalping & Intra Swing styles) | [`FORECASTING.md`](FORECASTING.md) | any forecasting, insight-mining, or trading-setup session |
| **App Support** (server, brokers, feeds, ngrok, machine config) | [`APP_SUPPORT.md`](APP_SUPPORT.md) | the app breaks, feeds/tunnels, machine/broker setup |
| **Commentary** (tracking & commentary module) | [`COMMENTARY.md`](COMMENTARY.md) | tracking sessions, commentary vocabulary/style |
| **Trade Backtest** (validated studies & playbooks) | [`TRADE_BACKTEST.md`](TRADE_BACKTEST.md) | backtesting setups, validation runs |

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
`2026-10-26_2pmm.md` · `PAInsight_forecasting_engine.md` · `SPATT.md`.
