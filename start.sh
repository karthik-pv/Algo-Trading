#!/bin/bash
clear

echo "Activating virtual environment..."

source venv/bin/activate

# Raise the soft open-file limit - the launcher app inherits the
# system default of 256 and a descriptor leak crashes the server with
# Errno 24 within hours (2026-11-05). The hard limit is unlimited, so
# this always succeeds.
if [ "$(ulimit -n)" -lt 10240 ] 2>/dev/null; then
    ulimit -n 10240
    echo "Raised open-file soft limit to $(ulimit -n)."
fi

echo "Starting server... Press Ctrl+C to stop."

python3 server.py
