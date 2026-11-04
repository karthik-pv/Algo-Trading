# APP SUPPORT — the living handoff (app / infra / broker feed)

> **LIVING DOCUMENT** — updated in place by every app-support session. Archive:
> `handoffs/archive/`. Primary history: `archive/2026-10-19_mac_migration.md`.

## 1. Architecture (the user's model — do not re-learn the hard way)

- **No machine owns any broker.** Both machines run all five modes (MSTOCK / KITE /
  PAPER / SIMULATOR / PLAYBACK) from this one repo.
- **Mac Mini = the home workstation**, currently primary, runs **KITE** (24/7 feed —
  MStock's WS rejects off-hours). **DELL = the on-the-move laptop**, boots **MSTOCK**
  defaults, needs no `.env`.
- **Config layering**: tracked `config/appconfig.json` = shared DELL-compatible defaults
  → gitignored `.env` = machine overrides (`BROKER`, `AUTO_SHUTDOWN_NO_BROWSER`,
  `NGROK_*`) and **wins**.
- **One broker account = one live feed session.** Don't run the same broker mode on both
  machines at once — the newer login kicks the older socket (proven 2026-10-19 07:00).

## 2. Server & tunnel

- `server.py` is the FULL trading app (broker watchers, ngrok, app-window launch on
  start) — do not start it just to eyeball a tab; the user runs it. **SERVER_PORT =
  5050** (not 5059). Tabs include the 2PMM Grid at `/2pmm`.
- ngrok tunnel (2026-10-19 instance): `https://kendra-preantepenultimate-complementarily.ngrok-free.dev`
  → 5050; BTCUSD test alert = the integration vehicle; at 09:15 the NIFTY alert takes
  over (15s cadence) — verify its webhook URL points at the current tunnel.
- Auto-shutdown: OFF on the Mac; stop = Ctrl-C in the console.

## 3. Gotchas (accumulated, keep appending)

- `server.py` line ~2001 has a nested-quote f-string that only parses on **Python
  3.12+** — pre-existing in HEAD, fine under the venv; system python3 is 3.9 and CANNOT
  parse server.py (engines themselves are 3.9-compatible).
- `scripts/smoke_test_app_page.py` fails pre-existing (expects 3 tabs; 2PMM made it 4).
- JS syntax check without a browser: render the template with jinja2 from the venv,
  split `<script>` blocks, compile each with
  `osascript -l JavaScript -e "try{Function(src);'OK'}catch(e){e.message}"`.
- Orders/charges: known review topics — orders-tab vs MStock contract-amount mismatch
  (session 2026-10, unresolved at the time; re-verify against current broker docs).
- **Manual-sell fills read "worse" than the grid price by design** (2026-11-02, measured):
  manual sells are marketable SELL LIMITs at `LTP − compute_limit_margin` (tiered 5/3/2/1%,
  cap 3 pts, floor 0.05 — `LIMIT_MARGIN_PCT_*` in App Config, live-applying). Audit-trail
  attribution (`data/audit/audit_<date>.json`, `sell_trigger_source`) shows the discount IS
  the protection margin, not slippage: 2026-11-02 net manual-sell gap vs grid LTP was
  −₹448 over 4 clicks; the re-price watchdog never fired once. 10–100 tier trimmed 3→2%
  (commit `3953f40`).
- **BK% box click shortcuts cut both ways** (2026-11-02): the Positions Grid BK% box has
  left-click +3% / right-click −3% shortcuts, and every edit re-prices the leg's live
  resting SELL LIMIT in place (`_sync_bp_resting_exit`). On a Mac a two-finger tap IS a
  right-click → accidental −3% re-prices the exit DOWN and it fills within ~1s. Seen live:
  7→4→1% edits, each followed by an instant sell at the lowered price. Separately, a BP
  edit cannot outrun a fill: once LTP touches the resting limit the broker executes it
  (edit lost by 58 ms at 09:30:50 — `position flat or unverifiable` is the signature).
- **TEMPORARY discipline lock in the grids** (2026-11-02, commits `83301bd`/`386cc01`/
  `ff0390e`/`c197225`, user self-discipline aid): Buy Grid rows are FORCED to
  ULTRA_SCALPING + D sell mode with all six radios disabled; Positions Grid radios are
  disabled (existing values kept, no forcing). Single marked blocks in `templates/app.html`
  ("TEMPORARY DISCIPLINE LOCK" / "PART 2") — delete block + the `${disciplineDisable}` /
  `${posDisciplineDisable}` references to undo.
- **ATM-drift watchdog** (2026-11-02, commit `ee00585`): the WOC window's ATM is computed
  once at build time; a pre-market build from a stale cached FUT quote left ATM stuck at
  23100 vs true 23300. New watchdog thread (`_atm_drift_watchdog_core`, ~60s, LIVE + market
  hours) rebuilds the window via `setup_woc_subscriptions()` when the round-half-up ATM of
  the live near-month FUT differs from the window's, rate-limited by
  `ATM_DRIFT_MIN_REBUILD_SECS` (600). Keys: `ATM_DRIFT_ENABLED/_CHECK_SECS/_MIN_REBUILD_SECS`.
- **TV MSG:DATA v1.10** (2026-11-02, tag `tv-data-v1.10`): v1.9 dropped a comma after
  `VOCCrossUnderIndex` (1M+5M blocks) — every DATA alert with an active VWAP cross was
  invalid JSON and the server rejected it (BIAS strip starved). Webhook failure dumps now
  land in `tv/tv_algo_json/incoming/FAILED_*.json` (commit `5c55659`). Also: momentors
  3O/2O rows no longer mirror VOC cross-downs — hidden while VOC < 0 (commit `27072e0`).
- **A dead broker login must never block app startup** (2026-11-04, commit `9636050`):
  `broker = BROKER_MAP[CURRENT_BROKER]()` at import time (server.py ~line 438) used to
  crash the app on a missing/invalid `KITE_API_KEY`, and a failed `broker.prod_start()`
  killed startup too (the big try only caught KeyboardInterrupt). Both now log and
  continue: constructor failure falls back to the SIMULATOR adapter (web UI always
  loads → the user can switch BROKER to MSTOCK in the config UI); `prod_start()` failure
  logs and loads degraded. NOTE: broker API keys live in the gitignored `.env`, not
  appconfig.
- **Momentors widget can freeze silently on stale data — check the snapshot date first**
  (2026-11-04 session): the widget hydrates from `data/tv_last_snapshot.json` at startup.
  Symptom: Momentors rows showing yesterday's crosses (e.g. `5px green 2` while the chart
  crossed down). Diagnosis recipe: check the `ts`/mtime of `data/tv_last_snapshot.json`
  + `data/logs/latest_DATA.json`; grep the launcher log for `TV webhook MSG=DATA` (last
  clean one). On 2026-11-04 the root cause was TV-side: DATA + LEVELS alerts not firing
  at all (only SPATAM ALERTS arrived) — the TV alert dialogs needed re-arming; the
  frozen snapshot's Close (23763 = NIFTY level) is the user's DELIBERATE reference chart
  for SENSEX, not a bug.
- **Repaired webhooks now persist too** (2026-11-04, commit `0be8e82`): the broken-SYM
  repair path used to skip MSG-logging / incoming-dump / `_tv_persist_snapshot`, so a
  day of broken-SYM messages left the persisted snapshot (and the widget's restart
  hydration) frozen. The dump/persist block now runs for BOTH clean and repaired
  payloads. Symptom before the fix: widget fine in-session, stale after every restart.
- **WOC ATM-drift toast now says WHY** (2026-11-04, commit `487d73b`): drift rebuilds
  toast "ATM Strike Price moved up/down from X to Y - option window rebuilt"; the
  generic "Option chain updated - new WOC factors applied" toast is suppressed for 10s
  after a drift toast (never both). Rebuild mechanics untouched.
- **OPT data split out of the main Pine indicator** (2026-11-04, tags `tv-data-v1.11` /
  `tv-data-opt-v1.0`): `strategy_tradinginvestalgo_data` v1.11 no longer sends
  MSG:"OPT" (opt_calc + 8 CE/PE security calls + the OPT message + ATM-Options inputs
  removed); the SYM field is dropped from DATA + LEVELS (unused downstream — it arrived
  as a study-object dump with unescaped quotes, shredding EVERY message on 2026-11-04,
  177/177 broken). New standalone `strategy_tradinginvestalgo_data_opt` v1.0 sends the
  byte-identical OPT payload (minus SYM) from its own chart instance + one TV alert.
  DATA/LEVELS fields are byte-identical to v1.10. Rollback: git tag `tv-data-v1.10`
  (no snapshot copies — TV keeps version history). TV-side setup: main indicator
  instances (DATA + LEVELS alerts) +
  the new OPT indicator instance (one alert) — the old third instance (Emit=OPT) is
  retired.
- **Momentors sup/res row = the leg's LAUNCH rung** (2026-11-04, commit `ffac0ff`):
  user rule change — the row anchors on the 1m-PVT leg's origin (rung closest above the
  leg's swing low rising / below its swing high falling, window = 1M
  PVTCrossOverIndex/UnderIndex minutes), NOT the highest level below price (which kept
  advancing to every newly reclaimed rung: "rose from 5E100, why CAM R3?"). Needs the
  server's rolling price memory (`_TV_PRICE_MEMORY`, ~3h @ 15s, in-memory only, fed by
  every LEVELS webhook Close). Fallback to the old rule when no memory (fresh restart,
  leg older than ~3h, no cross age).

## 4. CHANGELOG (append-only)

- **2026-11-04** — session "Appsupport": dead-broker-login no longer blocks app startup
  (SIMULATOR fallback, `9636050`); diagnosed the frozen Momentors widget (TV DATA/LEVELS
  alerts not firing; only SPATAM arrived — TV alerts re-armed by the user; NIFTY
  reference chart confirmed intentional); repaired webhooks now also dump+persist
  (`0be8e82`); WOC split — Pine v1.11 (OPT removed, SYM dropped) + new standalone
  `strategy_tradinginvestalgo_data_opt` v1.0 (`tv-data-v1.11` / `tv-data-opt-v1.0`);
  ATM-drift toast now names the strike move, generic WOC toast suppressed for the same
  rebuild (`487d73b`); Momentors sup/res row re-anchored on the 1m-PVT leg's launch rung
  with a server-side rolling price memory (`ffac0ff`). OPEN: the ATM-drift dead-band
  idea (rebuild every ~10 min on choppy days — LTP hovering at strike midpoints) was
  discussed, NOT implemented; interim = raise `ATM_DRIFT_MIN_REBUILD_SECS` in the
  config UI or switch `ATM_DRIFT_ENABLED` off.
- **2026-11-02** — big app session: manual-sell LIMIT parity + re-price watchdog + appconfig
  controls; limit-margin 10–100 tier 3→2%; ATM-drift watchdog; TV webhook failure dumps;
  Pine v1.10 comma fix; momentors 3O/2O down-mirror removed; TEMPORARY discipline lock
  (Buy Grid forced U/D + Positions Grid frozen); BK% click-shortcut gotcha documented with
  live evidence. Commit refs in gotchas above. Evening feed session: LTP freshness guard
  (`POSITION_LTP_STALE_SECS`, REST re-seed, commit `3b6f66b`); MStock WS close-code
  forensics (`9cb4870`); Feed Lab Kite capture fails loudly on expired daily token
  (`e77f2ef` — 2026-11-02 capture died on 1006s, stale token). Feed verdict: Kite ~2×
  tick rate, 30–70× fewer dead intervals; MStock ~1.6s throttle is broker-side.
- **2026-10-31** — doc created (living-handoff restructure). No app changes this session.
- **2026-10-19** — Mac migration day: everything environmental broke and was fixed;
  architecture above locked; Kite feed + ngrok live. (archive: `2026-10-19_mac_migration.md`)
