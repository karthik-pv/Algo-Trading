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
  land in `tv/TV_Algo_JSON/incoming/FAILED_*.json` (commit `5c55659`). Also: momentors
  3O/2O rows no longer mirror VOC cross-downs — hidden while VOC < 0 (commit `27072e0`).

## 4. CHANGELOG (append-only)

- **2026-11-02** — big app session: manual-sell LIMIT parity + re-price watchdog + appconfig
  controls; limit-margin 10–100 tier 3→2%; ATM-drift watchdog; TV webhook failure dumps;
  Pine v1.10 comma fix; momentors 3O/2O down-mirror removed; TEMPORARY discipline lock
  (Buy Grid forced U/D + Positions Grid frozen); BK% click-shortcut gotcha documented with
  live evidence. Commit refs in gotchas above.
- **2026-10-31** — doc created (living-handoff restructure). No app changes this session.
- **2026-10-19** — Mac migration day: everything environmental broke and was fixed;
  architecture above locked; Kite feed + ngrok live. (archive: `2026-10-19_mac_migration.md`)
