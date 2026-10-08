#!/bin/bash
#
# Builds and installs the "AlgoOptionScalper" macOS app bundle (Dock
# icon) into /Applications. The app's launcher starts the Flask
# server; the dock_shell helper owns the Dock icon and opens the
# app window as a NATIVE WebKit window - Chrome is never launched,
# so using the app never adds a "Google Chrome" instance to the
# Dock. Closing the window (or quitting the app) ends the session.
#
# Usage:  bash scripts/build_mac_app.sh
#
# Re-run any time after pulling changes - it replaces the installed
# bundle. This script is macOS-only and is never referenced by the
# Windows launchers (start_app.bat / run.ps1 / setup.ps1).

set -euo pipefail

APP_NAME="AlgoOptionScalper"
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

// Dock presence: this helper must NEVER own a Dock icon. It lives
// inside the app bundle, so touching AppKit checks it into
// LaunchServices as "AlgoOptionScalper" - and every supervisor restart
// then stacked another instance in the Dock. .prohibited makes it a
// pure background agent; CGWindowList, NSEvent and activating Chrome
// all keep working. Must run before any other AppKit call.
NSApplication.shared.setActivationPolicy(.prohibited)

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

// Topmost on-screen window owner (list is front-to-back). The FIRST
// real window belongs to the active app. CGWindowList is a direct
// window-server query - always fresh, unlike NSWorkspace
// .frontmostApplication which freezes in a bare CLI process (no
// distributed-notification runloop) and made activation a one-shot.
func isRealWindow(_ w: [String: Any]) -> Bool {
    guard let b = w[kCGWindowBounds as String] as? [String: Any],
          let wd = (b["Width"] as? NSNumber)?.doubleValue,
          let ht = (b["Height"] as? NSNumber)?.doubleValue else { return false }
    return wd > 1 && ht > 1   // skip menu-bar extras etc.
}

// Is the TOPMOST window under the mouse ours? Mouse is bottom-left
// origin; CGWindowBounds are top-left.
func topWindowUnderMouseIsOurs(_ list: [[String: Any]]) -> Bool {
    let m = NSEvent.mouseLocation                 // bottom-left origin
    guard let screen = NSScreen.main?.frame else { return false }
    let myTop = screen.height - m.y               // convert to top-left
    for w in list {
        guard isRealWindow(w) else { continue }
        let owner = w[kCGWindowOwnerPID as String] as? Int ?? -1
        guard let b = w[kCGWindowBounds as String] as? [String: Any],
              let x = (b["X"] as? NSNumber)?.doubleValue,
              let y = (b["Y"] as? NSNumber)?.doubleValue,
              let wd = (b["Width"] as? NSNumber)?.doubleValue,
              let ht = (b["Height"] as? NSNumber)?.doubleValue else { continue }
        if m.x >= x, m.x <= x + wd, myTop >= y, myTop <= y + ht {
            return owner == pid   // first (topmost) window under the mouse
        }
    }
    return false
}

log("helper start pid=\(pid) chromeHandle=\(chrome != nil ? "ok" : "nil")")

while isAlive(pid) {
    if let list = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        as? [[String: Any]] {
        // Global frontmost window = first real window in the list.
        var globalTopPid = -1
        var globalTopName = "?"
        for w in list where isRealWindow(w) {
            globalTopPid = w[kCGWindowOwnerPID as String] as? Int ?? -1
            globalTopName = w[kCGWindowOwnerName as String] as? String ?? "?"
            break
        }
        let mouseOverOurs = topWindowUnderMouseIsOurs(list)
        if mouseOverOurs, globalTopPid != pid {
            if lastState != "INSIDE" {
                log("mouse over our window but \(globalTopName) is front - activating")
                lastState = "INSIDE"
            }
            // macOS 14+ throttles external activation: several calls
            // report success but are ignored - keep trying every
            // cycle until one lands.
            chrome.activate(options: [.activateIgnoringOtherApps])
        } else if lastState != "OUTSIDE" {
            if mouseOverOurs { log("window already front - idle") }
            lastState = mouseOverOurs ? "FRONT" : "OUTSIDE"
        }
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

# ------------------------------------------------------- dock_shell
# Single, stable Dock presence AND the app window itself. The bundle's
# CFBundleExecutable is a bash script and never checks into
# LaunchServices, so dock_shell is the process that shows up as
# "AlgoOptionScalper" in the Dock - and since this build it also owns
# the app window (a native WebKit window):
#   - NO Chrome is ever launched: the window belongs to this app's
#     Dock icon, so using the app never adds a "Google Chrome"
#     instance (or a second icon) to the Dock
#   - the window waits for the server port, then loads the app page
#   - window position/size restore from data/window_state.json
#   - closing the window ends the session (the page's pagehide beacon
#     stops the server; the launcher's cleanup is the backstop)
#   - right-click Quit / double-click reopen behave like a normal app
# A failed build just means no Dock icon - the app still runs.
DOCK_SRC="$(mktemp -d)/dock_shell.swift"
cat > "$DOCK_SRC" <<'SWIFT'
import AppKit
import WebKit

// args: [1] = project dir (for data/window_state.json), [2] = server port
let args = CommandLine.arguments
let projectDir = args.count > 1 ? args[1] : ""
let serverPort = args.count > 2 ? args[2] : "5050"
let appURL = URL(string: "http://127.0.0.1:\(serverPort)/")!

let tsFmt: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "HH:mm:ss.SSS"
    return f
}()

// DateFormatter is not thread-safe: the orphan guard logs from a
// background queue while the main thread may log concurrently.
let logLock = NSLock()

func log(_ s: String) {
    logLock.lock()
    defer { logLock.unlock() }
    FileHandle.standardError.write(Data(("[\(tsFmt.string(from: Date()))] " + s + "\n").utf8))
}

// Orphan guard: this shell's parent is the launcher that owns the
// session. If that launcher dies without running its cleanup (kill
// -9, crash), the shell would keep a dead "running" icon in the Dock
// - every restart then stacked another instance next to it. Poll the
// parent; launchd (pid 1) means orphaned - exit so the icon goes.
log("dock_shell start ppid=\(getppid())")
DispatchQueue.global(qos: .utility).async {
    while getppid() != 1 {
        Thread.sleep(forTimeInterval: 2)
    }
    log("launcher gone - orphaned shell exit")
    exit(0)
}

// ---------------------------------------------------- window state
func stateFileURL() -> URL? {
    guard !projectDir.isEmpty else { return nil }
    return URL(fileURLWithPath: projectDir + "/data/window_state.json")
}

func loadWindowState() -> NSRect? {
    guard let file = stateFileURL(), let data = try? Data(contentsOf: file),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let x = obj["x"] as? Int, let y = obj["y"] as? Int,
          let w = obj["w"] as? Int, let h = obj["h"] as? Int,
          w > 0, h > 0 else { return nil }
    return NSRect(x: CGFloat(x), y: CGFloat(y), width: CGFloat(w), height: CGFloat(h))
}

let stateWriteQueue = DispatchQueue(label: "window-state-writer")
var stateSaver: DispatchWorkItem?

func scheduleWindowStateSave(_ window: NSWindow) {
    let work = DispatchWorkItem {
        guard let file = stateFileURL() else { return }
        // Top-left origin (page/Cocoa top-left convention) converted
        // from the window's bottom-left frame, same shape the page's
        // own /save_window_state reporter writes.
        let frame = window.frame
        let screenH = window.screen?.frame.height ?? NSScreen.main?.frame.height ?? 0
        let topY = Int(screenH - frame.maxY)
        let obj: [String: Int] = [
            "x": Int(frame.minX), "y": topY,
            "w": Int(frame.width), "h": Int(frame.height),
        ]
        guard let data = try? JSONSerialization.data(
            withJSONObject: obj, options: [.sortedKeys])
        else { return }
        let tmp = file.path + ".tmp"
        try? data.write(to: URL(fileURLWithPath: tmp))
        _ = try? FileManager.default.replaceItemAt(file, withItemAt: URL(fileURLWithPath: tmp))
    }
    stateSaver?.cancel()
    stateSaver = work
    stateWriteQueue.asyncAfter(deadline: .now() + 0.5, execute: work)
}

// ----------------------------------------------------- app + window
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKUIDelegate,
                         WKNavigationDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var titleObservation: NSKeyValueObservation?

    // Dock right-click Quit / Cmd+Q: stop the session. SIGTERM to the
    // server wakes the launcher's wait; its cleanup then closes any
    // remaining helpers. Other stacked shells from older builds are
    // quit as well - quitting the app must end the whole app.
    // The process then exits WITHOUT letting AppKit tear the window
    // tree down (see windowShouldClose) - that teardown segfaults.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let stopServer = Process()
        stopServer.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        stopServer.arguments = ["-TERM", "-f", "python.*server\\.py"]
        try? stopServer.run()
        let stopShells = Process()
        stopShells.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        stopShells.arguments = ["-TERM", "-f", "AlgoOptionScalper\\.app/Contents/MacOS/dock_shell"]
        try? stopShells.run()
        DispatchQueue.main.async { exit(0) }
        return .terminateLater   // exit(0) above fires first
    }

    // Dock icon clicked while running: bring the app window back.
    func applicationShouldHandleReopen(_ application: NSApplication,
                                       hasVisibleWindows flag: Bool) -> Bool {
        if let w = window {
            w.deminiaturize(nil)
            w.orderFrontRegardless()
            NSApp.activate(ignoringOtherApps: true)
        }
        return true
    }

    // Session window closed (red button / Cmd+W): end the session.
    // The close is CANCELLED and the process exits instead - letting
    // AppKit tear down a window with a live WKWebView segfaults in
    // the run loop's autorelease drain (WebKit releases a freed
    // object), which surfaced as the "quit unexpectedly" popup. With
    // exit(0) the window tree is simply abandoned: status 0, no crash
    // report, the window server closes the window as the process
    // dies. The server stops via the socket disconnect (page dies
    // with the process) or the launcher's cleanup.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        log("window close requested - ending session")
        DispatchQueue.main.async { exit(0) }
        return false
    }

    func windowDidMove(_ notification: Notification) {
        if let w = window { scheduleWindowStateSave(w) }
    }

    func windowDidEndLiveResize(_ notification: Notification) {
        if let w = window { scheduleWindowStateSave(w) }
    }

    // ------------------------------------------------ JS dialogs
    private func alert(for webView: WKWebView, message: String) -> NSAlert {
        let a = NSAlert()
        a.messageText = message
        return a
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = alert(for: webView, message: message)
        a.addButton(withTitle: "OK")
        if let w = webView.window {
            a.beginSheetModal(for: w) { _ in completionHandler() }
        } else {
            a.runModal()
            completionHandler()
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = alert(for: webView, message: message)
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        if let w = webView.window {
            a.beginSheetModal(for: w) { r in
                completionHandler(r == .alertFirstButtonReturn)
            }
        } else {
            completionHandler(a.runModal() == .alertFirstButtonReturn)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let a = alert(for: webView, message: prompt)
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        input.stringValue = defaultText ?? ""
        a.accessoryView = input
        if let w = webView.window {
            a.beginSheetModal(for: w) { r in
                completionHandler(r == .alertFirstButtonReturn ? input.stringValue : nil)
            }
        } else {
            let ok = a.runModal() == .alertFirstButtonReturn
            completionHandler(ok ? input.stringValue : nil)
        }
    }

    // File uploads (contract-note CSV / PDF picker).
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        if let w = webView.window {
            panel.beginSheetModal(for: w) { r in
                completionHandler(r == .OK ? panel.urls : nil)
            }
        } else {
            completionHandler(panel.runModal() == .OK ? panel.urls : nil)
        }
    }

    // ---------------------------------------------- file downloads
    // The Orders / Audit pages download workbooks via blob anchors and
    // location navigation - WebKit surfaces both as WKDownload.
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction,
                 didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse,
                 didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let downloads = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        var target = downloads.appendingPathComponent(suggestedFilename)
        if FileManager.default.fileExists(atPath: target.path) {
            let base = target.deletingPathExtension()
            let ext = target.pathExtension
            let stamp = DateFormatter.localizedString(
                from: Date(), dateStyle: .none, timeStyle: .medium)
                    .replacingOccurrences(of: ":", with: "")
            target = downloads.appendingPathComponent(
                "\(base.lastPathComponent) \(stamp)" + (ext.isEmpty ? "" : ".\(ext)"))
        }
        log("download -> \(target.lastPathComponent)")
        completionHandler(target)
    }

    func downloadDidFinish(_ download: WKDownload) {
        log("download finished")
    }

    func download(_ download: WKDownload, didFail currentRequest: URLRequest,
                  withError error: Error, resumeData: Data?) {
        log("download failed: \(error.localizedDescription)")
    }

    // --------------------------------------------- window lifecycle
    func openMainWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()

        // Saved state is in TOP-LEFT origin (the page's screenX/Y
        // convention); NSWindow frames are BOTTOM-LEFT - convert.
        var frame = NSRect(x: 0, y: 0, width: 1280, height: 820)
        if let saved = loadWindowState() {
            let primary = NSScreen.screens.first ?? NSScreen.main
            let globalHeight = (primary?.frame.origin.y ?? 0) + (primary?.frame.height ?? 0)
            frame = NSRect(x: saved.origin.x,
                           y: globalHeight - saved.origin.y - saved.height,
                           width: saved.width,
                           height: saved.height)
        }
        let window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false
        )
        window.title = "AlgoOptionScalper"
        window.minSize = NSSize(width: 480, height: 360)
        window.delegate = self
        window.tabbingMode = .disallowed

        let web = WKWebView(frame: .zero, configuration: config)
        web.uiDelegate = self
        web.navigationDelegate = self
        window.contentView = web
        webView = web

        titleObservation = web.observe(\.title, options: [.initial, .new]) { w, _ in
            if let t = w.title, !t.isEmpty { w.window?.title = t }
        }

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        log("app window opened (native WebKit, frame \(Int(frame.minX)),\(Int(frame.minY)) \(Int(frame.width))x\(Int(frame.height)))")
        web.load(URLRequest(url: appURL))
    }
}

// Wait until the web server actually serves the app page (HTTP 200 on
// / - a bare TCP connect would accept squatters too), then open the
// window. Same check the server's own browser-opener thread uses.
func waitForServer(deadline: TimeInterval) -> Bool {
    let start = Date()
    while Date().timeIntervalSince(start) < deadline {
        var req = URLRequest(url: appURL)
        req.timeoutInterval = 2
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 { ok = true }
            sem.signal()
        }.resume()
        sem.wait()
        if ok { return true }
        Thread.sleep(forTimeInterval: 0.5)
    }
    return false
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate

// Lifetime anchors: NSApplication.delegate (and the window/webview
// it holds) are UNRETAINED by AppKit, and top-level locals can be
// released early under -O once the closures that captured them have
// run - leaving NSApp.delegate dangling, which crashed the run loop's
// autorelease drain (SIGSEGV in objc_release, the "quit unexpectedly"
// popup). Anchor the delegate for the whole process lifetime.
let gDelegateHolder = [delegate]
withExtendedLifetime((app, delegate)) {
    // Minimal app menu: activating the icon must not show a blank menu bar.
    let mainMenu = NSMenu()
    let appMenuItem = NSMenuItem()
    mainMenu.addItem(appMenuItem)
    let appMenu = NSMenu()
    appMenu.addItem(NSMenuItem(title: "Quit AlgoOptionScalper",
                               action: #selector(NSApplication.terminate(_:)),
                               keyEquivalent: "q"))
    appMenuItem.submenu = appMenu
    app.mainMenu = mainMenu

    app.setActivationPolicy(.regular)

    // Opt out of external Accessibility entirely: AX queries (System
    // Events, utilities) against a bare script-built AppKit binary
    // crashed the run loop in HIServices autorelease handling, and
    // nothing legitimate needs AX here - the focus helper reads the
    // window server directly (CGWindowList) and activates via
    // NSRunningApplication, neither of which uses this AX tree.
    app.setAccessibilityEnabled(false)

    // Open the app window once the server is up (background wait, main
    // thread for window work - the Dock icon stays responsive meanwhile).
    DispatchQueue.main.async {
        DispatchQueue.global(qos: .userInitiated).async {
            let ready = waitForServer(deadline: 90)
            DispatchQueue.main.async {
                if ready {
                    delegate.openMainWindow()
                } else {
                    log("server port never came up - window not opened")
                    exit(1)
                }
            }
        }
    }

    app.run()
}
SWIFT
DOCK_BIN="$(mktemp -d)/dock_shell"
# -Onone deliberately: the shell is trivial and -O's aggressive early
# release of top-level references produced dangling-object SIGSEGVs
# (the "quit unexpectedly" popup) in the run loop's autorelease drain.
if swiftc -Onone "$DOCK_SRC" -o "$DOCK_BIN"; then
    cp "$DOCK_BIN" "$STAGING/Contents/MacOS/dock_shell"
    chmod 755 "$STAGING/Contents/MacOS/dock_shell"
else
    echo "WARNING: swiftc failed - no Dock icon; the app still runs from the launcher"
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
    <key>CFBundleName</key><string>AlgoOptionScalper</string>
    <key>CFBundleDisplayName</key><string>AlgoOptionScalper</string>
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
# AlgoOptionScalper - macOS Dock launcher.
# Starts the Flask server, then waits: the dock_shell helper (which
# owns the Dock icon) waits for the port and opens the native app
# window. Closing the app window stops the server (pagehide beacon +
# frontend-socket watchdog), and quitting this Dock app sends SIGTERM
# to the server. Either way this launcher then exits and the Dock
# icon stops bouncing. Chrome is never launched.

PROJECT_DIR="__PROJECT_DIR__"
# Own port: 5050, not 5000 - macOS AirPlay Receiver (ControlCenter)
# squats on 5000, which makes the Flask bind fail and the Dock icon
# appear dead. Keep in sync with SERVER_PORT in server.py.
SERVER_PORT=5050
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE="$HOME/__CHROME_PROFILE_SUBDIR__"
LOG_DIR="$PROJECT_DIR/data/logs"
CONSOLE_LOG="$LOG_DIR/launcher_console.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

# Own pid, exported: the focus-supervisor fork checks this to know
# when the launcher died (its own $PPID is not reliable).
export LAUNCHER_PID="$$"

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

server_pid_age() {
    # Uptime of a pid in seconds (ps etime format "[[dd-]hh:]mm:ss").
    ps -o etime= -p "$1" 2>/dev/null | awk '{
        n = split($1, p, /[:-]/)
        if (n == 4)      print p[1]*86400 + p[2]*3600 + p[3]*60 + p[4]
        else if (n == 3) print p[1]*3600 + p[2]*60 + p[3]
        else if (n == 2) print p[1]*60 + p[2]
        else if (n == 1) print p[1] + 0
        else             print 0
    }'
}

reap_orphan_helpers() {
    # Stacked Dock icons: a dock_shell whose launcher died without
    # cleanup (kill -9, crash) keeps a dead "running" icon - the dot -
    # alive in the Dock, and every new session stacks another one next
    # to it. Shells from this build self-exit when orphaned (see the
    # dock_shell source); this reaper clears leftovers from older
    # builds. A helper is orphaned exactly when it has been reparented
    # to launchd (ppid 1); a live session's helpers always still have
    # their launcher as parent, so they are never touched.
    local pid ppid
    for pid in $(pgrep -f "AlgoOptionScalper.app/Contents/MacOS/dock_shell" 2>/dev/null); do
        ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
        [ "$ppid" = "1" ] && kill -TERM "$pid" 2>/dev/null
    done
    for pid in $(pgrep -f "AlgoOptionScalper.app/Contents/MacOS/focus_watch" 2>/dev/null); do
        ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
        [ "$ppid" = "1" ] && kill -TERM "$pid" 2>/dev/null
    done
}

other_launcher_running() {
    # True when a launcher OTHER than this one is alive. Used to make
    # the exit cleanup race-safe: with a rapid close-and-relaunch the
    # dying session's cleanup can run up to ~5s after its server died
    # (watchdog sleep) - by then the next session's Chrome window may
    # already be open, and killing chrome here would murder it. The
    # next session sweeps leftover chrome itself before opening its
    # own window, so skipping is always safe.
    local pid ppid
    for pid in $(pgrep -f "AlgoOptionScalper.app/Contents/MacOS/launcher" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
        [ "$ppid" = "$$" ] && continue   # our own focus-supervisor fork
        return 0
    done
    return 1
}

# (Window position/size restore moved into dock_shell - it reads
# data/window_state.json itself when it opens the native app window.)

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
        # Orphan guard: this loop is a fork of the launcher; if the
        # launcher dies without cleanup (kill -9, crash) the fork
        # would keep restarting focus helpers forever - exit once the
        # launcher is gone. LAUNCHER_PID is exported by the launcher
        # itself ($$) - $PPID is unreliable here (a subshell of a
        # launchd-parented launcher reports PPID=1).
        if [ -n "${LAUNCHER_PID:-}" ] && ! kill -0 "$LAUNCHER_PID" 2>/dev/null; then
            return 0
        fi
        # The watched app is the dock_shell itself: the app window is
        # its native WebKit window (no Chrome in the session anymore).
        # start_dock_shell runs BEFORE this fork, so the pid is the
        # exact inherited one - no process-pattern matching races.
        local app_pid="$DOCK_SHELL_PID"
        if [ -n "$app_pid" ]; then
            # Blocks while the shell lives; returns if it restarts.
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

# Single stable Dock icon: this bash script never registers with
# LaunchServices, so the icon must come from a helper. dock_shell
# registers ONCE per session (no more stacked instances from helper
# restarts), translates Dock-Quit into a server SIGTERM, and owns the
# native app window (no Chrome involved in the session at all).
DOCK_SHELL_PID=""
start_dock_shell() {
    local helper="$(dirname "$0")/dock_shell"
    [ -x "$helper" ] || return 0
    "$helper" "$PROJECT_DIR" "$SERVER_PORT" >>"$LOG_DIR/dock_shell.log" 2>&1 &
    DOCK_SHELL_PID=$!
    disown
}

# Server already running (icon double-clicked): nothing to do here -
# the Dock shell raises its own native window on the reopen event.
# Clear any stacked dead icons from older builds and exit - no second
# server. The response must be 200 text/html so only our Flask app
# (not some other squatter on the port - e.g. AirPlay on 5000 answers
# 403) counts as a live session.
RESP="$(curl -s --max-time 2 -o /dev/null -w '%{http_code} %{content_type}' "http://127.0.0.1:$SERVER_PORT/" 2>/dev/null || true)"
if [[ "$RESP" == "200 text/html"* ]]; then
    reap_orphan_helpers
    exit 0
fi

# A server process exists but the port is not serving: either it is
# still starting up (first launch - the window opens when the port
# comes live) or it is a ZOMBIE: alive for minutes without ever
# serving (hung broker login, dead network at startup). A zombie used
# to block every Dock click until every app instance in the Dock was
# quit manually - now this launch stops it and takes over the session.
SERVER_STARTUP_GRACE=300
stale_pids=""
for pid in $(pgrep -fi 'python.*[[:space:]/]server\.py$' 2>/dev/null); do
    age="$(server_pid_age "$pid")"
    if [ "${age:-0}" -ge "$SERVER_STARTUP_GRACE" ]; then
        stale_pids="$stale_pids $pid"
    fi
done

if [ -n "$stale_pids" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] stopping stuck session (pids:$stale_pids)" \
        >>"$LOG_DIR/launcher.log" 2>/dev/null
    osascript -e 'display notification "Previous session is stuck - stopping it and starting fresh." with title "AlgoOptionScalper"' \
        >/dev/null 2>&1 || true
    for pid in $stale_pids; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    i=0
    while [ -n "$(pgrep -fi 'python.*[[:space:]/]server\.py$' 2>/dev/null)" ] && [ "$i" -lt 20 ]; do
        sleep 0.5
        i=$((i + 1))
    done
    # Anything that ignored the graceful stop is force-stopped.
    pkill -KILL -fi 'python.*[[:space:]/]server\.py$' 2>/dev/null || true
    sleep 1
fi

# Young server still starting: the first instance will open the app
# window itself once ready - exit instead of starting a second server.
if pgrep -fi 'python.*[[:space:]/]server\.py$' >/dev/null 2>&1; then
    exit 0
fi

# No server running: clear any leftover app window from a dead
# session of an OLDER build (which used Chrome app-mode windows) so
# nothing lingers, then start. This build never launches Chrome.
stop_app_chrome

# APP_WINDOW_OWNER tells server.py that this Dock session opens its
# own native window (dock_shell) - the server then skips its own
# Chrome browser-opener thread.
APP_WINDOW_OWNER=dock_shell "$PROJECT_DIR/venv/bin/python3" server.py >>"$CONSOLE_LOG" 2>&1 &
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
    if [ -n "${DOCK_SHELL_PID:-}" ]; then
        kill "$DOCK_SHELL_PID" 2>/dev/null
    fi
    if [ -n "${SERVER_PID:-}" ]; then
        if server_alive; then
            kill -TERM "$SERVER_PID" 2>/dev/null
            wait "$SERVER_PID" 2>/dev/null
        fi
        # Only sweep the dedicated Chrome when no newer session is
        # starting: with a rapid close-and-relaunch, a late cleanup
        # here would otherwise murder the next session's freshly
        # opened window. The next session's own startup sweep clears
        # any leftover chrome instead (see stop_app_chrome in the
        # fresh-start path and _stop_leftover_app_chrome in server.py).
        if ! other_launcher_running; then
            stop_app_chrome
        fi
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Hover-focus tracking: activates the app window when the mouse
# dwells inside it. dock_shell must start FIRST so the supervisor
# fork inherits its pid.
start_dock_shell

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
    osascript -e "display notification \"Server exited (code $STATUS). See $CONSOLE_LOG\" with title \"AlgoOptionScalper\"" >/dev/null 2>&1
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
fi
# The launcher's cleanup normally stops its dock_shell/focus helpers;
# these catch orphans whose launcher was killed outright, so a
# reinstall never leaves stacked Dock icons behind.
pkill -f "$TARGET_APP/Contents/MacOS/dock_shell" 2>/dev/null || true
pkill -f "$TARGET_APP/Contents/MacOS/focus_watch" 2>/dev/null || true
sleep 2

if [ -d "$TARGET_APP" ]; then
    rm -rf "$TARGET_APP"
fi
ditto "$STAGING" "$TARGET_APP"
touch "$TARGET_APP"

echo ""
echo "Installed: $TARGET_APP"
echo "Next: drag it into the Dock, then click the icon to launch."
echo "Console output (no VSCode window) is appended to: data/logs/launcher_console.log"
