# UI GENERAL — the living handoff (app-wide UI aspects)

> **LIVING DOCUMENT** — updated in place. Covers the app-wide UI mechanics that are not
> specific to one module (module-specific UI lives in each module's handoff). Individual
> windows/startup geometry: [`app_start_up.md`](app_start_up.md).

## 1. App page architecture

- The whole UI is ONE single-page app: `templates/app.html` rendered by `/` (Trade tab),
  `/orders` and `/appconfig` — the route only sets the initial tab; the page never
  reloads when switching tabs.
- Tabs/panels: elements `panel-<name>`; switching via the tab bar's `activate()` and the
  URL hash (`#appconfig` etc., `hashchange` listener). Initial tab: URL hash →
  `data-active-tab` hint from the server route → first panel.
- ONE shared Socket.IO connection (`window.__appSocket` → localhost:5001) serves Trade,
  Orders and App Config. It is opened on DOMContentLoaded BEFORE the panel scripts, so
  every panel can attach its handlers. The server shuts down when its LAST client
  disconnects — any open tab keeps the app alive.

## 2. App Config form contract (how to add a key — mirrors AGENTS.md rule 3)

A new config key must be registered in THREE places:

1. `APPCONFIG_GROUPS` in `server.py` — assigns the key to a logical group (the on-disk
   `config/appconfig.json` nests by these groups).
2. `APPCONFIG_DEFAULTS` in `server.py` — used when the file is missing/corrupt.
3. The form in `templates/app.html` — a `.field` block inside the relevant `card`
   section: `.field-caption` (label + `key-hint` span showing the KEY name) +
   `.input-wrap` (input/select/switch).

Reading/writing:

- Config is read via `fetch_from_json("appconfig.json", KEY)` at the point of decision —
  never cached in module globals. Note `fetch_from_json` caches the file; for keys that
  must reflect a UI save immediately, read the file fresh or use the existing
  cache-invalidation paths (`clear_json_cache`).
- Save: the UI posts a FLAT `{key: value}` map to `/save_appconfig`; `_unflatten_config`
  merges each key back into its group (keys not on the form survive a save).
- Checkbox semantics: FormData omits unchecked boxes — `collectForm()` re-adds the
  important toggles explicitly.

## 3. Dirty tracking & Save flow (App Config)

- `snapshot` = last loaded/saved form state; `isDirty()` compares `collectForm()`
  JSON. `saveBtn` is disabled unless dirty.
- `RESTART_KEYS = ["MODE", "BROKER", "UNDERLYING"]` — changing one demands a confirm
  (takes effect only after an app restart).
- Save = POST `collectForm()` to `/save_appconfig`; Cancel = `form.reset()` +
  `applyConfig(snapshot)`; Ctrl/Cmd+S saves; `beforeunload` warns on unsaved changes.
- `applyConfig(data)` populates fields by `name`; keys may be SKIPPED there when the
  field is driven by something else (e.g. the live window-size boxes, see
  [`app_start_up.md`](app_start_up.md)).

## 4. Serving & stale pages (critical for app-mode windows)

- ALL HTML responses are served `Cache-Control: no-cache, no-store, must-revalidate`
  (`no_cache_html` after_request in `server.py`) — app-mode browser windows have no
  working reload shortcut, so stale HTML would survive restarts otherwise.
- **The stale-page trap**: an open app window keeps running its OLD JavaScript until the
  app is fully quit and relaunched. If a UI change "does nothing", relaunch first, then
  debug. (Cost a full debugging session on 2026-11-06 — see the handoff changelog.)

## 5. In-app zoom

- App-mode windows do not process browser zoom shortcuts, so the page implements
  Ctrl/Cmd `+` / `-` / `0`: sets `document.documentElement.style.zoom` (clamped 0.5–2.0,
  step 0.1) and remembers the level in `localStorage` key `appUiZoom` (per dedicated
  Chrome profile).

## 6. Gotchas (accumulated, keep appending)

- **Number inputs mirror-driven by JS**: a programmatic `el.value = x` does NOT fire
  input events — dispatch a bubbling `change` event afterwards if the dirty tracker
  must react (pattern used by the window-size mirror).
- **JS syntax check without a browser**: render the template with jinja2 from the venv,
  split `<script>` blocks, compile each with
  `osascript -l JavaScript -e "try{Function(src);'OK'}catch(e){e.message}"`.
- `server.py` has a nested-quote f-string that only parses on Python 3.12+ — use the
  venv python, not the system 3.9.
- Avoid duplicate element ids: the config form uses `id` + `name` with the same value;
  mirror code looks up by `getElementById` and the form by `[name=…]` — keep both
  consistent when adding fields.

## Changelog

- **2026-11-06** — Created. Extracted the app-wide UI mechanics touched by the
  app-window-size session (page architecture, config form contract, dirty/Save flow,
  no-cache serving + stale-page trap, in-app zoom, mirror-dispatch pattern).
