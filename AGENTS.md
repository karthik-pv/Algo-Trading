# AGENTS.md — project conventions (read me first)

Standing rules for every session working in this repo. These are persistent
user preferences — do not ask for confirmation on them each time.

## 1. Versioning of TradingView Pine scripts (mandatory)

The user must be able to trust what is running in TradingView at all times.
Every change to a Pine script in `tv/` (e.g.
`tv/tv_algo_json/strategy_tradinginvestalgo_data`) MUST:

1. **Bump the version** in the script's header banner (`VERSION: x.y`) and add
   a `CHANGELOG` line with the date + one-line description of the change,
   including what is guaranteed UNCHANGED (payload messages etc.).
   For `tv/spatam/spatam` the version is NEVER added to the indicator title —
   it lives as a `// VERSION: …` comment on the line right below the
   `indicator(...)` call.
2. **Tag the commit**: `git tag <script>-vX.Y` (e.g. `tv-data-v1.1`) and push
   the tag.
3. **Commit + push** — commit messages follow the existing style
   (`Area: what changed`), one logical change per commit.

No physical snapshot copies in the repo — TradingView keeps the version
history; the git tag is the rollback point.

## 2. Runtime files are never committed

Logs/state such as `tv/tv_algo_json/alerts_log.jsonl`,
`tv/tv_algo_json/state_latest.json`, `data/`, tokens, dumps — keep them out of
git (already in `.gitignore`).

## 3. Config changes apply live

Strategy parameters live in `config/appconfig.json`, are read via
`fetch_from_json("appconfig.json", KEY)` at the point of decision (never
cached in globals), and any new key must be registered in
`APPCONFIG_GROUPS` + `APPCONFIG_DEFAULTS` in `server.py` and given a field in
the AppConfig form in `templates/app.html` so the user can edit it on screen.

## 4. Git workflow

- User wants changes committed with meaningful labels, **separately** per
  logical change, and pushed to `main` after each completed unit of work.
- Do not commit runtime/log files.
