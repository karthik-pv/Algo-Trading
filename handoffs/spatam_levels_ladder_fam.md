# HANDOFF — SPATAM & the Levels-Ladder tag column (FAM)

Created: 2026-11-04 (session "Levels Ladder")
Status: **LIVE** — verified end-to-end against the chart on 2026-11-04.
Domain: TradingView SPATAM indicator ↔ Algo app Levels-Ladder rendering.

---

## 1. What this covers

The **Levels Ladder** tables in the Algo app show the same rows as the
SPATAM chart's levels table (CW-CD / CW-ND / NW-ND). As of Pine
`strategy_tradinginvestalgo_data` **v1.12/v1.13**, the chart's **4th column
— the family trend tag — is delivered IN the webhook JSON** and printed by
the app **verbatim**: TradingView is the single source of truth; the app
never recalculates it.

The tag = SPATAM's `f_familyTag` output per level family, e.g. `5P` →
`5PM2` → `5PM` (text grows as MACD1/MACD2 join the PVT trend), shaded
dark green/red when MACD2 agrees with the PVT direction, light when it
retraces against it.

## 2. Level → family mapping (SPATAM `f_levelTag`, mirrored everywhere)

| Level name | Family (FAM segment index) |
|---|---|
| `1m EMA50` | 5m (0) |
| `1m EMA100`, `1m EMA200` | 30m (1) |
| `5m EMA50/100/200` | 2h (2) |
| `30m EMA50/100/200` | D (3) |
| `2h EMA50/100/200` | W (4) |
| `D EMA100/200` | M (5) |
| `W EMA100/200` | Q (6) |

All other levels (pivots, Camarilla, PDH/PDL, VWAP, round numbers, Fib, OR)
have **no tag** (blank cell on the chart, blank cell in the app).

## 3. Wire format — `LEVELS.FAM`

The MSG:"LEVELS" webhook carries, **inside the `LEVELS` object** (last key,
after `NW_ND` — v1.13 moved it there, see §7):

```
"FAM": "5PMU1,3PM2U1,2PMU1,DPMU1,WPU0,MPMD1,QPM2U1"
```

Seven comma-joined segments, **fixed family order 5, 3, 2, D, W, M, Q**
(mapping table above). Each segment is `<tag><U|D><1|0>`:

- `<tag>` — the tag text exactly as the chart prints it (never empty:
  always `<pfx>P` + optional `M2`/`M1`/`M` suffix);
- `U|D` — PVT direction (green/red on the chart);
- `1|0` — dark shade flag (1 = MACD2 agrees with the PVT trend = dark,
  0 = light shade).

Colors (same values as SPATAM's table):
dark green `#02580B` / light green `#C5FAB7` / dark red `#F91C06` /
light red `#FAB9B1`; text white on dark, black on light.

## 4. Why the HTF block keys are compact (P/M1/M2/K/KT)

The LEVELS message rides close to TradingView's **4096-char alert cap**
(~3,979 after v1.13). To fund FAM's ~55 chars, the `LEVELS.HTF` block keys
are compact **on the wire only**: `P`=PVTTrendFlag, `M1`=Trend_1226,
`M2`=Trend_2452, `KT`=KTrendFlag, `K` stays (2dp). `core/utils.expand_htf()`
expands them back to the full names at ingest (both in
`trade_logic.process_TradingView_Data` and `server._tv_persist_snapshot`),
so every downstream reader sees the original field names — **idempotent**,
old full-name messages (Pine ≤ v1.11, MSG:"HTF") pass through untouched.

## 5. Who renders what

| UI surface | Renderer | Last column |
|---|---|---|
| **PA panel → "Levels Ladder" section** (yellow CW-CD/CW-ND/NW-ND tables) | `applyPaLevels` in `templates/app.html` (~line 3841), fed by the `update_trading_view_data` socket event / `GET /tv/last_data` on page load | FAM tag cell, colors decoded from `<U|D><1|0>`; points-from-close kept as a hover tooltip on the price cell |
| **"1PM Momentors & Levels Ladder" widget → LEVELS LADDER table** (top strip, `ml-ladder-table`) | `build_ladder` in `forecasting/momentors_widget.py` → `/momentor/data` → `renderMomentors` in `app.html` | 4th column header **SPATAM**: `chip` = tag text verbatim, `chip_color` = `spatam-dark/light-green/red` (added to `ML_COLORS`) |

Both fall back gracefully: no `FAM` (pre-v1.13 webhook) → the PA ladder
shows blank tag cells, the widget falls back to the old endorsing-PM chip.

## 6. M / Q family state (new in v1.12)

The five intraday families were already fetched by the full f_calc_all
security calls. M and Q were not — v1.12 added one tiny helper
`f_fam_state()` (PVT sign + MACD1 12/26 + MACD2 24/52) with two
`request.security` calls (`"M"` and `"3M"`, same resolutions SPATAM uses).
Security-call count: 31 of TV's 40.

## 7. Version history

| Version | What |
|---|---|
| `tv-data-v1.12` (14:12) | FAM added — **but emitted at the message ROOT**, sibling of `LEVELS`; the app reads `data.LEVELS` and never saw it (tag column blank). Kept for the record; superseded within minutes. |
| `tv-data-v1.13` (14:19) | **FIX: FAM emitted INSIDE the LEVELS object.** This is the version to run. Verified live 2026-11-04 14:22+: `FAM: 5PMU1,3PM2U1,2PMU1,DPMU1,WPU0,MPMD1,QPM2U1`. |
| `cdbdff4` (14:13) | Algo side: `expand_htf` + PA-ladder tag column. |
| `1c9d8df` (14:28) | Widget ladder (ml-ladder) State → SPATAM tag column. |

Rollback = `git checkout <tag> -- tv/tv_algo_json/strategy_tradinginvestalgo_data`
(tag `tv-data-vX.Y`) and paste that copy into the TV Pine editor (app side
tolerates both formats).

## 8. Verification checklist

1. TV editor header: `VERSION: 1.13` (script
   `strategy_tradinginvestalgo_data`; git tag `tv-data-v1.13`).
2. `tv/tv_algo_json/incoming/latest_LEVELS.json` → `LEVELS.FAM` present,
   7 segments; `LEVELS.HTF` keys compact (`P/M1/M2/K/KT`).
3. `python -c "from core.utils import expand_htf"` → full names restored.
4. App: both ladder tables show tag cells matching the SPATAM chart table,
   same text AND same dark/light shading, on every tick.
5. Wire size sanity: `LEVELS` message < 4,096 chars (~3,979 typical) — a
   rejected alert starves BOTH ladders silently; if both go stale at once,
   check this first.

## 9. Cross-references

- Payload shape + HTF semantics: `tv/tv_algo_json/tradingview_algo_json.md`
- Widget tables & momentor rules: `handoffs/1pm_momentors_levels.md` (the
  four **SPATAM Algo UI Tables**: BIAS, MOMENTORS, SET UP, LEVELS LADDER)
- SPATAM indicator itself: `tv/spatam/spatam.md`
- The four widget tables (BIAS / MOMENTORS / SET UP / LEVELS LADDER — the
  "SPATAM Algo UI Tables"): `handoffs/1pm_momentors_levels.md`
- Versioning convention: AGENTS.md §1 (version bump + changelog + git tag; no snapshot copies)

## Changelog (append-only)

- 2026-11-04 — created; FAM live end-to-end (Pine v1.13, commits `1308b5f`,
  `cdbdff4`, `9b264e1`, `1c9d8df`).
