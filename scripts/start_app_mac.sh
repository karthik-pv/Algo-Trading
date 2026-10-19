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

"$PY" server.py &
APP_PID=$!

# open the page once the server answers (cancels the shutdown timer)
(
    for i in $(seq 1 30); do
        if curl -s -o /dev/null --max-time 1 http://localhost:5050/; then
            open "http://localhost:5050"
            break
        fi
        sleep 1
    done
) &

wait $APP_PID
