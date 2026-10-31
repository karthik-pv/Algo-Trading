> **[SUPERSEDED 2026-10-31 — ARCHIVE]** Current truth for app/infra state and machine architecture lives in
> `handoffs/FORECASTING.md, APP_SUPPORT.md`. This file is frozen provenance — read for history, do not update.

# HANDOFF — 2026-10-19 — Mac migration day (the trading machine moved)

**Read this first, then `handoffs/SPATT.md`, then `handoffs/PAInsight_forecasting_engine.md`.**
Session: the Mac went live as the trading machine; everything that broke, broke for
environmental reasons and is now fixed. Written 2026-10-19 ~07:50, pre-market.

---

## 1. The architecture now (the user's model — do not re-learn the hard way)

- **No machine owns any broker.** Both machines run all five modes (MSTOCK / KITE /
  PAPER / SIMULATOR / PLAYBACK) from this one repo.
- **Mac Mini = the home workstation.** Currently the primary (their words: "Mac is the
  way forward"). Running **KITE** (24/7 feed — MStock's WS rejects off-hours).
- **DELL = the on-the-move trading laptop.** Pulls the same repo; boots **MSTOCK**
  defaults; needs no `.env`.
- **Config layering**: tracked `config/appconfig.json` = the shared defaults (the
  DELL-compatible: MSTOCK, auto-shutdown ON) → the gitignored `.env` = the machine's
  overrides (`BROKER`, `AUTO_SHUTDOWN_NO_BROWSER`, `NGROK_*`) and **wins**.
- **One broker account = one live feed session.** Don't run the same broker mode on
  both machines at once — the newer login kicks the older socket (proven 07:00 today).

## 2. Current live state (as of writing)

- The app: **RUNNING** (Kite, 10 weekly contracts 19Oct2026 built, webhooks flowing,
  the Buy grid populated, the page = the Chrome window titled "OptionScalper").
- The ngrok tunnel: `https://kendra-preantepenultimate-complementarily.ngrok-free.dev`
  → 5050. The BTCUSD test alert feeds it (the integration vehicle).
- **AT 09:15**: the NIFTY alert takes over (15s cadence, clean SYM). Verify its webhook
  URL points at the tunnel above.
- The auto-shutdown = OFF on the Mac; stopping = Ctrl-C in the terminal.

## 3. Today's fixes (the chronology, each committed)

1. **The webhook hardening** (`server.py`): `request.json` → `get_json(force=True,
   silent=True)`; a broken body = logged (the first 500 chars, pre-formatted!) + 400,
   never a crash. **The app logger does NOT interpolate %-args — f-strings only** (a
   latent bug fixed in 3 call sites).
2. **The SYM repair**: the alert's SYM field can arrive as a study-object dump with
   unescaped quotes (the BTCUSD chart), shredding the body; the route strips SYM
   (the regex to the next top-level key) and re-parses. MSG:LEVELS messages flow to
   process_TradingView_Data (stored as `_tv_levels`, trade logic skipped).
3. **The duplicate-`hasData` JS bug** (mine, via the CW-CD insert): killed the entire
   115KB main script → the Trade tab dark while SPATT (a separate block) lived.
   Fixed; **verified with a headless browser** (playwright in the temp dir — the
   page errors = 0, the ladder renders, the Buy grid paints).
4. **`AUTO_SHUTDOWN_NO_BROWSER`** (the appconfig flag + the env override): the Mac =
   false (the app stays up; the stop = manual). The Windows console-cleanup behavior
   was self-killing the app every time a tab closed.
5. **The KITE switch**: `session.BROKER = KITE` (the Mac's `.env`) — the MStock WS's
   feed-window gate bypassed; the contracts build instantly, any hour.
6. **`start_app_mac.sh`** (the launcher): sources `.env`, starts the server, NO second
   browser-open (the server's own `webbrowser.open` = the one window). **The app has
   no dock icon — the Chrome window IS the app.** Pin the tab.
7. **`start_ngrok.sh`** committed (the tunnel starter; the static-domain support).

## 4. The setup families (the playbook = 643 rows, all machine-reproducible)

| Set Up | Uptrend | Downtrend | Trigger | Exit |
|---|---|---|---|---|
| 23PM_5PM | 112 (hand-marked!) | 174 (machine phases) | phase / PVT cross-down | phase end / EMA4-bridge |
| 23PM_5PX | 113 | 154 | 5m PVT cross | 5m close vs EMA4 |
| 2PM_3PX | 17 | 23 | 30m PVT cross | 5m close vs EMA21 |
| 2PX_3PM (+M1/M2/O variants) | 11 | 12 | 2H PVT cross, 2H MACDs pre-cross | event rows |
| 2PM_3PX_5PM | 10 | 17 | 30m PVT cross in confirmed 2H | 5m MACD2 cross against |

**The conventions (locked by the user — never undo without asking):**
- **Price Action** = the majors **the move reclaimed** (from the 1m-cross leg extreme
  to the farthest print of the containing sign-run), read at the **current 1m PVT
  cross**; grouped in the hierarchy order (3E → 5E → 1E), digits compacted
  (3E50+3E100 → `3E5100`; 50+100+200 → `51200`); fallback = "1E50, 1E21". Rung set:
  1E100, 1E200, 3E50, 3E100, 3E200 (added today), 5E50, 5E100, 5E200.
- **Momentors** = 30m-and-above ONLY (no 1m/5m elements — the trailing _X removed!);
  letter sequence **S-P-M-O**; ages: 30m = 1..13, 2H/D = 0..4.
- **Points Moved** = integers, in TREND direction (positive = the setup worked).
- **Duration** = "2h 30m" format; the grid filters are ladder-aware ("5E50" finds
  "5E51200"); the Change columns = green/red by sign.
- The 23PM_5PM phase rows' PA = the related 5PX's label (the launching cross of the
  containing run, cross-day included).

## 5. The queued work (the next session's menu)

1. **09:15 today**: the NIFTY alert = the live feed; verify the tunnel URL; watch the
   first real session on the Mac (the final integration exam).
2. **The 2O momentor**: unblocked (the 2H VOC = in the export — a 30m-computed ×4-
   period VOC updating on 30m boundaries); needs the age-semantics decision (30m
   steps vs the collapsed 2H bars).
3. **2SX / DSX / DOX momentors**: await a chart export with 2H Stoch + D Stoch/VOC.
4. **The "3PM-turning" sibling setup** (the 14 Oct 15:20-style early entry — the user's
   chart read; deliberately excluded from 23PM_5PX).
5. **The OOS validation**: Apr–Jul 2026 untouched; re-run the insight builds scoped
   OOS (the SPATT.md §6.1).
6. **The momentor-predictor study**: the launch codes → the phase quality (the
   SPATT.md §6.5; n large enough now).
7. **The insight-app split** (the architecture, agreed): the cheap-tier stays
   in-process (the fuse enforces it); the heavy jobs (builds/backtests/ML) = a second
   lightweight app on another port, the files as the contract — when the features need it.
8. **The ngrok static domain**: the `.env`'s NGROK_AUTHTOKEN/NGROK_DOMAIN are EMPTY —
   the user fills them (the dashboard) → `start_ngrok.sh` → the URL never changes again.

## 6. The gotchas (the do-not-relearn list)

- The logger: **f-strings only** (the %-args = silently dropped).
- The system python = 3.9 (can't parse the server's 3.12 f-strings!) — **always use
  the venv** for the app-side syntax checks.
- esprima (the JS check) = false-positives on `?.`/`??` (the ES2020) — not real errors.
- `screencapture` = no screen permission; the headless playwright lives in the temp
  dir (`/private/var/folders/.../T/opencode/pylibs` + `pwbrowsers`) — reinstall if the
  temp is cleared.
- The single-instance guard = the UI port 5001 — the second start exits(1) fast.
- The MStock WS = the session-gated (the single login per account); the Kite = 24/7.
- The data/ = gitignored = the machine-local artifacts (the tokens, the forecasts,
  the persisted prices) — the playbook/JSON = the tracked exceptions (the force-added).
- The user's notation: 1E/3E/5E/2E = the EMA rungs by TF; P/M/S/O = PVT/MACD/Stoch/VOC;
  X = a cross trigger; the "PM" = the PVT+MACD state pair.
