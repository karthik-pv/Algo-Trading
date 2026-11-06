# APP START-UP WINDOW — the living handoff (window geometry / App Config)

> **LIVING DOCUMENT** — updated in place. Covers how the app window opens on every
> platform (macOS Dock app, macOS manual launch, Windows), and the App Config
> `APP_WINDOW_WIDTH` / `APP_WINDOW_HEIGHT` feature (2026-11-06, session with the user).
> General UI mechanics (config form contract, dirty/Save flow, no-cache serving) live in
> [`ui_general.md`](ui_general.md).

## 1. What the feature does

App Config → **App Window** card (`APP_WINDOW_WIDTH` / `APP_WINDOW_HEIGHT`, group
`"window"` in appconfig):

- **Both empty (default)** — the window reopens at the size/position the page saves to
  `data/window_state.json` (re-anchored top-right if it would land off-screen).
- **Either set** — that dimension is PINNED: every start the window opens anchored to the
  **RIGHT-TOP corner of the main display** at the configured size (y = top bar offset).
  A `0`/empty value counts as "not set" for that dimension; the other dimension falls
  back to the remembered state.
- Takes effect at the **next app start** (read fresh from disk at window-open time, not
  the `fetch_from_json` cache).

### In-page behaviours (all platforms, served by `templates/app.html`)

- The two boxes **mirror the live window size** — filled on page load, updated on every
  resize, plus a 1.5s interval safety net.
- **Dragging the window lights up the App Config Save button** — the mirror dispatches a
  bubbling `change` when the value actually changes (dirty tracking picks it up).
- **Typing in the boxes resizes the window live** (debounced 300ms; both dims must be
  ≥ 200 to apply).
- **0×0 guard**: minimized/hidden windows report 0 in some engines — sizes < 200 are
  never mirrored into the boxes nor persisted to `window_state.json`.

## 2. Per-platform startup paths (know which one you are in)

| Platform | Window type | Geometry source |
|---|---|---|
| **macOS Dock icon** (`AlgoOptionScalper.app`) | **native WebKit** (`dock_shell` binary owns a WKWebView) | `dock_shell` reads `data/window_state.json` when the port comes live. With config pinned, `server.py` **pre-writes** the right-top anchored frame into that file BEFORE the Flask port binds (`_apply_config_size_to_native_window`) — no AppleScript needed. |
| **macOS manual** (`venv/bin/python3 server.py`) | Chrome `--app` window, dedicated profile (`~/Library/Application Support/AlgoTradingApp`) | `_load_saved_window_geometry()` → config override or remembered state; passed as `--window-position/--window-size`. Leftover Chrome is stopped gracefully first (`_stop_leftover_app_chrome`). |
| **Windows** (Dell) | Chrome or Edge `--app` | Same `_load_saved_window_geometry()` logic; flags passed on both launch paths (`_open_app_window_when_ready` and the reopen path `_open_app_window_to`). Work area = primary monitor minus taskbar. |

`_desktop_bounds()` returns the main display in points: **CoreGraphics via ctypes**
(macOS, no permissions) → **ctypes user32 SPI_GETWORKAREA** (Windows) → osascript Finder
query (fallback). Do NOT rely on the Finder query alone — it is DENIED in the
launchd/Dock context ("no desktop" failures).

### Resizing from the boxes, per engine

- **Chrome `--app`** (macOS manual + Windows): `window.resizeTo(w,h)` works.
- **Native WebKit** (Dock): `window.resizeTo` is a NO-OP and `window.outerWidth` reports
  **0**. The page detects `outerWidth < 200`, uses `innerWidth/innerHeight` for display,
  and POSTs `/resize_app_window {w,h}` — the server resizes the `dock_shell` window via
  System Events AppleScript (needs Accessibility permission for the app context; worked
  live 2026-11-06).

## 3. Key code locations

- `server.py`: `APPCONFIG_GROUPS["window"]`, `APPCONFIG_DEFAULTS["window"]`,
  `_configured_window_size()`, `_load_saved_window_geometry()`, `_desktop_bounds()`,
  `_apply_config_size_to_native_window()`, `_resize_native_app_window()`,
  `/resize_app_window`, `/save_window_state` (rejects 0×0 states), launch decision at
  `APP_WINDOW_OWNER != "dock_shell"`.
- `templates/app.html`: "App Window" card in the config form; window-state reporter
  (bottom `mirrorToConfigInputs` / `liveWindowSize` / `reportState`); config-panel
  independent mirror + resize-on-type block (after `saveBtn` listener).
- `config/appconfig.json` → `"window"` group (the file also carries live user edits —
  never commit it blindly).

## 4. Gotchas (accumulated, keep appending)

- **App-mode Chrome has no working reload** — after editing `app.html` the open window
  keeps the old JS; quit and relaunch (the `/appconfig` HTML is served `no-cache`, so a
  real relaunch always gets the current page).
- **dock_shell reads `window_state.json` only once**, when the port comes up — the
  config frame must be pre-written before Flask binds (it is; see startup block).
- **The stale-page trap**: if the boxes misbehave, first check WHICH window you have —
  a window from before a code change can survive for hours. Symptom history
  (2026-11-06): "0/0 in the boxes" = native WebKit window + old page/outerWidth=0.
- **System Events needs the window enumerable**: `set size of window 1 of process
  "dock_shell"` fails with "Invalid index" if the window is closed or the app is
  hidden on another Space — the endpoint reports `ok:false`, the page keeps the typed
  value, retry once the window is visible.
- **`window_state.json` is runtime state** — gitignored; never commit it.
- Windows top offset is 0 (taskbar at the bottom); macOS uses 31 (below the menu bar).

## Changelog

- **2026-11-07** — Added cross-reference banner to [`ui_general.md`](ui_general.md)
  (general UI mechanics moved there; this doc stays window/startup-specific).
- **2026-11-06** — Feature created: `APP_WINDOW_WIDTH`/`APP_WINDOW_HEIGHT` App Config
  keys (group `window`), App Window card in the config form, live mirror + Save-enable
  on drag, resize-on-type (resizeTo on Chrome, `/resize_app_window` + System Events on
  native WebKit), 0×0 guards, config-anchored startup on all three platform paths
  (CoreGraphics/user32 desktop bounds; dock_shell frame pre-write). Verified live on
  macOS Dock (native WebKit) and macOS Chrome; Windows path wired, awaiting first run
  on the DELL.
