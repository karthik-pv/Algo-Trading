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
# Focus-watch Swift helper: compile once per build; the launcher runs
# it for focus-follows-mouse. A failed build disables the feature
# gracefully (the launcher skips it when the binary is absent).
SWIFT_SRC="$(mktemp -d)/focus_watch.swift"
cat > "$SWIFT_SRC" <<'SWIFT'
import AppKit

// Focus-follows-mouse: activates the Chrome app window the moment the
// mouse hovers a VISIBLE part of it. Uses CGWindowList (a direct
// window-server query - always fresh, no permissions, no AppKit
// notification runloop needed). NSWorkspace.frontmostApplication is
// intentionally avoided: in a bare CLI process it caches the startup
// value (no distributed-notification runloop) and never updates.
//
// args: [1] = Chrome main pid

let args = CommandLine.arguments
guard args.count >= 2, let pid = Int32(args[1]) else { exit(1) }
guard let chrome = NSRunningApplication(processIdentifier: pid) else {
    // Not a GUI app (supervisor matched a transient process) - exit
    // so the supervisor re-resolves the real Chrome pid.
    FileHandle.standardError.write(("pid \(pid) is not a GUI app - exit\n").data(using: .utf8)!)
    exit(1)
}

var lastState = ""
var throttleLogged = false
var verifiedLogged = false

let tsFmt: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "HH:mm:ss.SSS"
    return f
}()

func log(_ s: String) {
    FileHandle.standardError.write(Data(("[\(tsFmt.string(from: Date()))] " + s + "\n").utf8))
}

func isAlive(_ p: Int32) -> Bool { kill(p, 0) == 0 }

// Returns (ownerPid, ownerName, bounds) of the TOPMOST on-screen
// window under the mouse.
func topWindowUnderMouse() -> (pid: Int, name: String, bounds: CGRect)? {
    guard let list = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        as? [[String: Any]] else { return nil }
    let m = NSEvent.mouseLocation                 // bottom-left origin
    guard let screen = NSScreen.main?.frame else { return nil }
    let myTop = screen.height - m.y               // convert to top-left
    for w in list {
        guard let owner = w[kCGWindowOwnerPID as String] as? Int,
              let b = w[kCGWindowBounds as String] as? [String: Any],
              let x = (b["X"] as? NSNumber)?.doubleValue,
              let y = (b["Y"] as? NSNumber)?.doubleValue,
              let wd = (b["Width"] as? NSNumber)?.doubleValue,
              let ht = (b["Height"] as? NSNumber)?.doubleValue else { continue }
        if wd <= 1 || ht <= 1 { continue }        // menu-bar extras etc.
        if m.x >= x, m.x <= x + wd, myTop >= y, myTop <= y + ht {
            let name = w[kCGWindowOwnerName as String] as? String ?? "?"
            return (owner, name, CGRect(x: x, y: y, width: wd, height: ht))
        }
    }
    return nil
}

func frontPid() -> Int {
    Int(NSWorkspace.shared.frontmostApplication?.processIdentifier ?? -1)
}

log("helper start pid=\(pid) chromeHandle=ok")

while isAlive(pid) {
    if let top = topWindowUnderMouse(), top.pid == pid {
        if lastState != "INSIDE" {
            log("mouse over our topmost window (name=\(top.name) bounds=\(Int(top.bounds.minX)),\(Int(top.bounds.minY)) \(Int(top.bounds.width))x\(Int(top.bounds.height))) front=\(frontPid()) - activating")
            lastState = "INSIDE"
            throttleLogged = false
            verifiedLogged = false
        }
        if frontPid() != pid {
            // macOS 14+ throttles external activation - the first
            // several calls report success but are ignored; keep
            // trying until one lands (log once per episode).
            _ = chrome.activate(options: [.activateIgnoringOtherApps])
            let checkDeadline = Date().addingTimeInterval(0.12)
            while Date() < checkDeadline { usleep(10_000) }
            if frontPid() != pid {
                if !throttleLogged {
                    log("activation throttled by macOS - retrying until it lands")
                    throttleLogged = true
                }
            } else if !verifiedLogged {
                log("activation verified - chrome is front")
                verifiedLogged = true
            }
        }
    } else {
        if lastState != "OUTSIDE" { lastState = "OUTSIDE" }
        throttleLogged = false
        verifiedLogged = false
    }
    usleep(10_000)  // 10ms - effectively instant, ~0 CPU
}
log("chrome gone - helper exit")
SWIFT
FOCUS_BIN="$(mktemp -d)/focus_watch"
if swiftc -O "$SWIFT_SRC" -o "$FOCUS_BIN"; then
    cp "$FOCUS_BIN" "$STAGING/Contents/MacOS/focus_watch"
    chmod 755 "$STAGING/Contents/MacOS/focus_watch"
else
    echo "WARNING: swiftc failed - focus-follows-mouse will be disabled"
fi

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
    # Main browser process of the dedicated profile ONLY: it carries
    # the --app= flag (helpers/crashpad never do). A looser match once
    # returned a transient non-GUI process and the focus watcher
    # monitored a dead pid.
    local pid cmd
    for pid in $(pgrep -f "user-data-dir=$CHROME_PROFILE" 2>/dev/null); do
        cmd="$(ps -o command= -p "$pid" 2>/dev/null)"
        case "$cmd" in
            *--type=*|*--crashpad*|*--utility*|*--renderer*|*--gpu*) ;;
            *"Google Chrome"*--app=*) echo "$pid" ;;
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
_ws_num() { grep -oE "\"$1\": *-?[0-9]+" "$WS_FILE" 2>/dev/null | grep -oE -- '-?[0-9]+' | tail -1; }
GEOM_ARGS=()
WS_FILE="$PROJECT_DIR/data/window_state.json"
if [ -f "$WS_FILE" ]; then
    WS_X=$(_ws_num x); WS_Y=$(_ws_num y); WS_W=$(_ws_num w); WS_H=$(_ws_num h)
    if [ -n "$WS_X" ] && [ -n "$WS_Y" ] && [ -n "$WS_W" ] && [ -n "$WS_H" ]; then
        GEOM_ARGS=("--window-position=${WS_X},${WS_Y}" "--window-size=${WS_W},${WS_H}")
    fi
fi

# ----------------------------------------------------------------
# Focus-follows-mouse for the app window. On macOS an INACTIVE
# window shows no hover cursor and swallows the first click (the
# click only activates the window) - after working in TradingView
# the first app click always went to focusing the window instead
# of the button.
#
# A tiny Swift helper (compiled at build time, below) polls the
# mouse IN-PROCESS every 10ms and activates Chrome the instant the
# cursor enters the window bounds - effectively zero delay, ~0.1%
# CPU, no Accessibility permission. Bounds come from the page's own
# window_state.json. The bash supervisor restarts the helper when
# Chrome restarts (new pid).
# ----------------------------------------------------------------
focus_supervisor_loop() {
    local helper="$(dirname "$0")/focus_watch"
    [ -x "$helper" ] || return 0
    while server_alive; do
        local app_pid
        app_pid="$(app_chrome_main_pids | head -1)"
        if [ -n "$app_pid" ]; then
            # Blocks while Chrome lives; returns when it restarts.
            "$helper" "$app_pid" "$PROJECT_DIR/data/window_state.json" \
                >>"$LOG_DIR/focus_watch.log" 2>&1
            sleep 1
        else
            sleep 1
        fi
    done
}

FOCUS_WATCH_PID=""
start_focus_watch() {
    focus_supervisor_loop &
    FOCUS_WATCH_PID=$!
    disown
}

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
    if [ -n "${FOCUS_WATCH_PID:-}" ]; then
        kill "$FOCUS_WATCH_PID" 2>/dev/null
    fi
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

# Hover-focus tracking: activates the app window when the mouse
# dwells inside it (self-guards while Chrome is not open yet).
start_focus_watch

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
