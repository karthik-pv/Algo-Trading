#!/usr/bin/env bash
# Mac launcher: starts the app AND opens the app page in the browser.
# The browser client cancels the no-browser auto-shutdown timer - without
# it the app shuts itself down ~20s after start (the Windows launcher's
# splash/page flow does this automatically; this is its Mac equivalent).

set -u
cd "$(dirname "$0")/.."

if [ -d .venv ]; then PY=.venv/bin/python
elif [ -d venv ]; then PY=venv/bin/python
else PY=python3; fi

# machine-local settings (BROKER / AUTO_SHUTDOWN_NO_BROWSER / NGROK_*) -
# .env is gitignored, so the Dell and the Mac can differ from one repo
if [ -f .env ]; then set -a; . ./.env; set +a; fi

"$PY" server.py &
APP_PID=$!

wait $APP_PID
