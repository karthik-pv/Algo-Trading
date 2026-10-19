#!/usr/bin/env bash
# Starts an ngrok tunnel to the local Flask server (port 5050) so
# TradingView webhooks can reach the /trading_view_webhook endpoint.
#
# Requires NGROK_AUTHTOKEN in .env:
#   https://dashboard.ngrok.org/get-started/your-authtoken
#
# Optionally set NGROK_DOMAIN in .env to your free static domain:
#   https://dashboard.ngrok.org/domains
# With a static domain the TradingView webhook URL survives restarts;
# without one the URL changes every time ngrok starts.

set -euo pipefail

cd "$(dirname "$0")/.."

# Read KEY=value from .env (tolerates spaces around '=' and quotes).
env_value() {
    grep -E "^[[:space:]]*$1[[:space:]]*=" .env 2>/dev/null \
        | head -1 | cut -d'=' -f2- \
        | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//'
}

if ! command -v ngrok >/dev/null 2>&1; then
    echo "ngrok is not installed. Install it with: brew install ngrok"
    exit 1
fi

PORT="${SERVER_PORT:-5050}"
WEBHOOK_PATH="/trading_view_webhook"
NGROK_AUTHTOKEN="${NGROK_AUTHTOKEN:-$(env_value NGROK_AUTHTOKEN)}"
NGROK_DOMAIN="${NGROK_DOMAIN:-$(env_value NGROK_DOMAIN)}"

if [[ -z "${NGROK_AUTHTOKEN}" ]]; then
    echo "NGROK_AUTHTOKEN is not set."
    echo "1. Sign up free at: https://dashboard.ngrok.org/signup"
    echo "2. Copy your authtoken: https://dashboard.ngrok.org/get-started/your-authtoken"
    echo "3. Put it in .env as:  NGROK_AUTHTOKEN = <your-token>"
    exit 1
fi

ngrok config add-authtoken "${NGROK_AUTHTOKEN}" >/dev/null

echo "Tunneling ngrok -> http://127.0.0.1:${PORT}"
if [[ -n "${NGROK_DOMAIN}" ]]; then
    echo "TradingView webhook URL: https://${NGROK_DOMAIN}${WEBHOOK_PATH}"
    exec ngrok http --url="${NGROK_DOMAIN}" "${PORT}"
else
    echo "TradingView webhook URL: https://<random>.ngrok-free.app${WEBHOOK_PATH}"
    echo "(copy the exact 'Forwarding' URL ngrok prints below)"
    exec ngrok http "${PORT}"
fi
