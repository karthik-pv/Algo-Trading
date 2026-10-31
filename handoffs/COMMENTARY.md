# COMMENTARY — the living handoff (tracking & commentary module)

> **LIVING DOCUMENT** — updated in place. Vocabulary: `forecasting/Glossary.md` (wins over
> usage elsewhere). Archive: `handoffs/archive/`.

## 1. What commentary tracks

State calls (no-repaint, carried once stated) + level interactions + cross events, per
the user's style rules [LOCKED, 2026-10-18]:

1. **Track the setup states by name** — 2PM / 3PM / 5PM (+1PM, DPM) with sign and M2
   status (`3P+ 3M2↓`).
2. **Every level interaction is called out explicitly**: "crossed the level — PDC
   (22994) up/down", "price hits VWAP — acting as resistance", "retraced to 1E21 and
   took support".
3. **Cross events get their own lines**: "1PME50 trend starts…", "5PX has happened —
   UP/DOWN", "5m VOCross up/down", "3M2X down".
4. **At every 1PM/15PM trend start, list the nearby levels** it could cross (Round100s,
   PDH/PDL/PDC, VWAP, EMA rungs within ~120 pts) — proactive, before price gets there.
5. **No unnecessary Nifty prints** — level values and key extremes only.
6. **Role reversals always flagged** (morning resistance → afternoon support).
7. 15PM: only trackable live from the 15s webhook feed (not in CSV exports).

## 2. Micro-call vocabulary (tracking layer)

| Call | Definition |
|---|---|
| `5m PVT Cross Down` | the 5m PVT cross-down event (= `5PX has happened — DOWN`) |
| `1PX Up/Down` | bare 1m PVT flip — an event, **not** a trend start |
| `1PMX Up/Down` | 1m PVT + 1m MACD crossing together — the 1m composite cross |
| `1PME50 Trend Starts` | **the trend-start call**: 1PM state + the 1m EMA50 cross (see FORECASTING §2.1) |

## 3. Day-type classification (afternoons)

- **SWING / STOCH-CHOP / DEAD** — base rates 40/43/17%.
- **1PM Swing** = net 13:15→close move ≥0.25% (~60 pts), one-sided ≥45% efficiency.
- **1m Stoch Swing day** = chop day; scalp 1m stoch crosses instead of holding a swing.
- Tension (regime flip / PVT curl against trend) → SWING; full alignment by 13:00 →
  CHOP/DEAD.
- **Fell from** = nearest level above window close that the window high touched.

## 4. Trend assignment

- Precedence: `3S_5PM_CV` → `3PMX` → `3PM` (3S_5PM_CV wins even against 3PM; 3PMX wins
  whenever the cross index is 1–13 same-day).
- Trend states: `UT / DT / RT_UP / RT_DOWN` (Glossary §Trend).

## 5. CHANGELOG (append-only)

- **2026-10-31** — doc created (living-handoff restructure); content migrated from
  Glossary §Commentary + SPATT tracking conventions. No rule changes.
