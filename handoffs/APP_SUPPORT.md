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

## 4. CHANGELOG (append-only)

- **2026-10-31** — doc created (living-handoff restructure). No app changes this session.
- **2026-10-19** — Mac migration day: everything environmental broke and was fixed;
  architecture above locked; Kite feed + ngrok live. (archive: `2026-10-19_mac_migration.md`)
