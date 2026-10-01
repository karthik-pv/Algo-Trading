#!/bin/bash
#
# Builds and installs the "Algo Trading" macOS app bundle (Dock icon)
# into /Applications. The app's launcher starts the Flask server and
# opens the Chrome app window; closing the window (or quitting the app)
# ends the server session.
#
# Usage:  bash scripts/build_mac_app.sh
#
# Re-run any time after pulling changes - it replaces the installed
# bundle. This script is macOS-only and is never referenced by the
# Windows launchers (start_app.bat / run.ps1 / setup.ps1).

set -euo pipefail

APP_NAME="Algo Trading"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_ICON="$PROJECT_DIR/images/image.png"
CHROME_PROFILE_SUBDIR="Library/Application Support/AlgoTradingApp"

APP_ROOT="/Applications"
TARGET_APP="$APP_ROOT/$APP_NAME.app"

STAGING="$(mktemp -d)/$APP_NAME.app"
trap 'rm -rf "$(dirname "$STAGING")"' EXIT

mkdir -p "$STAGING/Contents/MacOS" "$STAGING/Contents/Resources"

# ---------------------------------------------------------------- icon
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
    sips -z $size $size "$SOURCE_ICON" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) "$SOURCE_ICON" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns -o "$STAGING/Contents/Resources/app.icns" "$ICONSET"

# ---------------------------------------------------------- Info.plist
cat > "$STAGING/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIdentifier</key><string>com.phvrao.algotrading</string>
    <key>CFBundleName</key><string>Algo Trading</string>
    <key>CFBundleDisplayName</key><string>Algo Trading</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleIconFile</key><string>app.icns</string>
    <key>CFBundleDevelopmentRegion</key><string>en</string>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.finance</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# ------------------------------------------------------------- launcher
cat > "$STAGING/Contents/MacOS/launcher" <<'LAUNCHER'
#!/bin/bash
# Algo Trading - macOS Dock launcher.
# Starts the Flask server, then waits: the server opens the Chrome app
# window itself once the port is live. Closing the app window stops
# the server (existing frontend-socket watchdog), and quitting this
# Dock app sends SIGTERM to the server. Either way this launcher then
# exits and the Dock icon stops bouncing.

PROJECT_DIR="__PROJECT_DIR__"
SERVER_PORT=5000
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE="$HOME/__CHROME_PROFILE_SUBDIR__"
LOG_DIR="$PROJECT_DIR/data/logs"
CONSOLE_LOG="$LOG_DIR/launcher_console.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

page_socket_count() {
    lsof -nP -iTCP:5001 -sTCP:ESTABLISHED 2>/dev/null | grep -c -- '->127\.0\.0\.1:5001'
}

# True when the app page is genuinely rendering: its ~15s pending-orders
# poll is current (the server's /frontend_alive answers from the page's
# own poll cadence). A closed window whose Chrome background process
# still holds the 5001 socket is NOT alive, even though lsof keeps
# reporting the connection as ESTABLISHED.
frontend_alive() {
    curl -s --max-time 2 "http://127.0.0.1:$SERVER_PORT/frontend_alive" 2>/dev/null \
        | grep -q '"alive":[[:space:]]*true'
}

app_chrome_main_pids() {
    # Main browser processes of the dedicated profile (helpers all
    # carry --type=, the main process does not).
    local pid cmd
    for pid in $(pgrep -f "user-data-dir=$CHROME_PROFILE" 2>/dev/null); do
        cmd="$(ps -o command= -p "$pid" 2>/dev/null)"
        case "$cmd" in
            *--type=*) ;;
            *"Google Chrome"*) echo "$pid" ;;
        esac
    done
}

stop_app_chrome() {
    # Gracefully stop the dedicated Chrome MAIN process and wait for a
    # clean exit (up to ~10s). Signalling the whole family at once
    # (pkill) makes Chrome mark the profile crashed, and the next
    # launch then opens a plain new-tab browser window alongside the
    # app window. A clean main-process quit keeps the profile sane.
    local pids i
    pids="$(app_chrome_main_pids)"
    [ -z "$pids" ] && return 0
    for pid in $pids; do kill -TERM "$pid" 2>/dev/null; done
    i=0
    while [ -n "$(app_chrome_main_pids)" ] && [ "$i" -lt 20 ]; do
        sleep 0.5
        i=$((i + 1))
    done
}

# Saved app-window bounds (written by the app page) so the window
# reopens at the user's last position/size.
GEOM_ARGS=()
WS_FILE="$PROJECT_DIR/data/window_state.json"
if [ -f "$WS_FILE" ]; then
    _ws_num() { grep -oE "\"$1\": *-?[0-9]+" "$WS_FILE" | grep -oE -- '-?[0-9]+' | tail -1; }
    WS_X=$(_ws_num x); WS_Y=$(_ws_num y); WS_W=$(_ws_num w); WS_H=$(_ws_num h)
    if [ -n "$WS_X" ] && [ -n "$WS_Y" ] && [ -n "$WS_W" ] && [ -n "$WS_H" ]; then
        GEOM_ARGS=("--window-position=${WS_X},${WS_Y}" "--window-size=${WS_W},${WS_H}")
    fi
fi

# Server already running (icon double-clicked): bring the app window
# back and exit this extra instance - no second server. The response
# must be 200 text/html: macOS AirPlay (ControlCenter) also listens
# on port 5000 but answers 403 with no content type.
RESP="$(curl -s --max-time 2 -o /dev/null -w '%{http_code} %{content_type}' "http://127.0.0.1:$SERVER_PORT/" 2>/dev/null || true)"
if [[ "$RESP" == "200 text/html"* ]]; then
    if [ -x "$CHROME" ]; then
        if frontend_alive; then
            # App window is really open (the page polls). Minimized or
            # behind other windows: unminimize + focus its Chrome
            # process by PID. System Events needs assistive access -
            # failures are harmless, the window simply stays where it
            # is. Never fall back to "tell application Google Chrome":
            # with the user's own Chrome running that can raise the
            # WRONG instance.
            APP_PID="$(app_chrome_main_pids | head -1)"
            if [ -n "$APP_PID" ]; then
                osascript -e "tell application \"System Events\" to set frontmost of (first process whose unix id is $APP_PID) to true" >/dev/null 2>&1 || true
                osascript -e "tell application \"System Events\" to tell (first process whose unix id is $APP_PID) to perform action \"AXRaise\" of window 1" >/dev/null 2>&1 || true
            fi
        else
            # Zombie state: Chrome background process still holds the
            # 5001 socket (lsof says ESTABLISHED) but the page is gone
            # - no poll for 90s. Stop the leftover instance cleanly so
            # the relaunch below creates a proper app window (and does
            # not hand off to the running instance as a plain tab).
            stop_app_chrome
            "$CHROME" "--app=http://127.0.0.1:$SERVER_PORT" \
                "--user-data-dir=$CHROME_PROFILE" \
                --no-first-run --no-default-browser-check \
                --hide-crash-restore-bubble \
                "${GEOM_ARGS[@]}" >/dev/null 2>&1 &
        fi
    else
        open "http://127.0.0.1:$SERVER_PORT"
    fi
    exit 0
fi

# A server process exists but the port is not serving yet (still
# starting up): the first instance will open the app window itself
# once ready - exit instead of starting a second server.
if pgrep -fi "python.*server\.py" >/dev/null 2>&1; then
    exit 0
fi

"$PROJECT_DIR/venv/bin/python3" server.py >>"$CONSOLE_LOG" 2>&1 &
SERVER_PID=$!

server_alive() { kill -0 "$SERVER_PID" 2>/dev/null; }

# This instance owns the session (SERVER_PID set): stop the server,
# then make sure the dedicated Chrome instance is gone too - on
# Dock-quit this also closes the app window; after a window-close it
# just clears any residue. Guard-path instances never set SERVER_PID
# and so never touch the session.
cleanup() {
    if [ -n "${SERVER_PID:-}" ]; then
        if server_alive; then
            kill -TERM "$SERVER_PID" 2>/dev/null
            wait "$SERVER_PID" 2>/dev/null
        fi
        stop_app_chrome
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Watchdog: the server normally stops itself when the app window
# closes (frontend socket disconnect with a short grace period). This
# is the backstop for what the server cannot see itself: the app page
# holds a live ESTABLISHED connection to the frontend socket server
# (port 5001), and that connection dies with the page even though
# Chrome may keep a background process alive after the window closes
# - so the socket is the reliable signal, not the process.
PAGE_SEEN=0
PAGE_GONE_POLLS=0
STALE_POLLS=0
while server_alive; do
    if [ "$(page_socket_count)" -gt 0 ]; then
        PAGE_SEEN=1
        PAGE_GONE_POLLS=0
        # The socket is up - but Chrome background processes can hold
        # it long after the window is gone. The page's poll cadence is
        # the real signal: a dead page never refreshes the server's
        # activity timestamp, so give it a few minutes and shut down
        # (minimized windows poll at worst once a minute and stay
        # under the 90s liveness threshold, so they are never killed).
        if frontend_alive; then
            STALE_POLLS=0
        else
            STALE_POLLS=$((STALE_POLLS + 1))
            if [ "$STALE_POLLS" -ge 60 ]; then
                kill -TERM "$SERVER_PID" 2>/dev/null
                break
            fi
        fi
    elif [ "$PAGE_SEEN" -eq 1 ]; then
        PAGE_GONE_POLLS=$((PAGE_GONE_POLLS + 1))
        if [ "$PAGE_GONE_POLLS" -ge 6 ]; then
            kill -TERM "$SERVER_PID" 2>/dev/null
            break
        fi
    fi
    sleep 5
done

wait "$SERVER_PID"
STATUS=$?

# 0 = clean shutdown (app window closed). 130/143 = terminated by our
# own traps (Dock quit / watchdog). Anything else = crash: surface it.
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 130 ] && [ "$STATUS" -ne 143 ]; then
    osascript -e "display notification \"Server exited (code $STATUS). See $CONSOLE_LOG\" with title \"Algo Trading\"" >/dev/null 2>&1
fi

exit "$STATUS"
LAUNCHER

sed -i '' "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$STAGING/Contents/MacOS/launcher"
sed -i '' "s|__CHROME_PROFILE_SUBDIR__|$CHROME_PROFILE_SUBDIR|g" "$STAGING/Contents/MacOS/launcher"
chmod 755 "$STAGING/Contents/MacOS/launcher"

# ------------------------------------------------------------- install
if pgrep -f "$TARGET_APP/Contents/MacOS/launcher" >/dev/null 2>&1; then
    echo "Quitting the running $APP_NAME app..."
    pkill -f "$TARGET_APP/Contents/MacOS/launcher" 2>/dev/null || true
    sleep 2
fi

if [ -d "$TARGET_APP" ]; then
    rm -rf "$TARGET_APP"
fi
ditto "$STAGING" "$TARGET_APP"
touch "$TARGET_APP"

echo ""
echo "Installed: $TARGET_APP"
echo "Next: drag it into the Dock, then click the icon to launch."
echo "Console output (no VSCode window) is appended to: data/logs/launcher_console.log"
