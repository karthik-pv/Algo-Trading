"""
TradingView webhook tunnel (ngrok) - owned by the SERVER process.

Configured by NGROK_AUTHTOKEN / NGROK_DOMAIN in the project .env (the
same file the server's load_dotenv reads). A STATIC domain keeps the
TradingView webhook URL constant across sessions:

    https://<NGROK_DOMAIN>/trading_view_webhook

HARD RULE: this module can never take trading down. Everything runs in
a daemon thread, every entry point swallows its own exceptions, the
server never waits on it, and failures surface as a UI toast + log
lines only.

Lifecycle (all paths that run server.py get this for free - the Dock
launcher, start.sh, run.ps1, start_app.bat and the silent splash):

    start_ngrok_tunnel(port)  - non-blocking; sweeps any leftover tunnel
                                holding the same domain (a free static
                                domain allows exactly ONE online
                                endpoint - a stale one from a
                                hard-killed previous session keeps the
                                domain bound to a dead port), starts
                                ngrok, verifies the PUBLIC URL answers,
                                then runs the restart watchdog.
    stop_ngrok_tunnel()       - every shutdown path: KeyboardInterrupt /
                                SIGTERM (server.py) and the watcher's
                                no-browser self-shutdown (trade_logic).
    set_notifier(fn)          - server.py registers a UI callback;
                                DOWN/UP transitions are toasted.

Missing domain, authtoken or binary only disables the feature - startup
proceeds exactly as before. A died tunnel is restarted by the watchdog
throttled to once a minute, so TradingView alerts cannot silently drop
mid-session.
"""

import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_LOG_FILE = _PROJECT_ROOT / "data" / "logs" / "ngrok.log"

_ngrok_lock = threading.Lock()
_ngrok_proc = None
_ngrok_generation = 0      # bumped per start/stop: stale watchdogs exit
_ngrok_last_start = 0.0
_restart_min_interval = 60.0

_notifier = None           # callable(down: bool, message: str) - UI toast
_last_notified_down = None  # only state TRANSITIONS are toasted


def set_notifier(fn):
    """Register the UI toast callback: fn(down: bool, message: str)."""
    global _notifier
    _notifier = fn


def _notify_transition(down, message):
    """Toast only on state CHANGES - a persistent failure must not spam
    a toast every watchdog minute, and a recovery toasts exactly once."""
    global _last_notified_down
    if _notifier is None or _last_notified_down == down:
        return
    _last_notified_down = down
    try:
        _notifier(down, message)
    except Exception:
        pass  # a broken UI callback must never touch the tunnel thread


def _read_env_value(key):
    """Value of KEY in the project .env, or '' (spaces / quotes and
    trailing comments tolerated - matches the .env's KEY = value style)."""
    try:
        with open(_ENV_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                    value = line.split("=", 1)[1]
                    value = value.split("#", 1)[0].strip().strip("\"'")
                    return value.strip()
    except FileNotFoundError:
        pass
    except Exception as exc:  # unreadable .env must never block startup
        logger.warning(f"ngrok tunnel: could not read {_ENV_FILE}: {exc}")
    return ""


def _find_ngrok_binary():
    """ngrok from PATH first, then the known install locations (the
    macOS Dock app runs with a minimal PATH, so PATH alone is not
    enough there)."""
    found = shutil.which("ngrok")
    if found:
        return found
    candidates = [
        "/opt/homebrew/bin/ngrok",
        "/usr/local/bin/ngrok",
        "/usr/bin/ngrok",
        str(Path.home() / "scoop/shims/ngrok.exe"),
        r"C:\\ProgramData\\chocolatey\\bin\\ngrok.exe",
        str(Path.home() / "AppData/Local/ngrok/ngrok.exe"),
    ]
    for cand in candidates:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return ""


def _sweep_leftover_tunnels(domain):
    """Kill tunnels the user's other projects never use: only processes
    whose command line carries THIS session's domain are stopped."""
    try:
        if os.name == "nt":
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='ngrok.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{domain}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=15,
            )
        else:
            subprocess.run(
                ["pkill", "-TERM", "-f", f"ngrok http.*{domain}"],
                capture_output=True, timeout=10,
            )
    except Exception as exc:
        logger.warning(f"ngrok tunnel: leftover sweep failed: {exc}")


def _last_ngrok_error():
    """The most recent ERROR line from ngrok's own log - the actual
    reason a tunnel failed (bad token, domain busy, no network)."""
    try:
        with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-4096:]
        matches = re.findall(r"^ERROR: (.+)$", tail, re.M)
        return matches[-1][:160] if matches else ""
    except Exception:
        return ""


def _public_url_answers(domain, timeout=4):
    """True when the app answers through the tunnel (proves the whole
    edge -> tunnel -> local-server path, not just the tunnel)."""
    try:
        req = urllib.request.Request(f"https://{domain}/", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_ngrok_tunnel(port):
    """Non-blocking: sweep + start + verify + watchdog all run in a
    daemon thread - server startup is never blocked or delayed."""
    global _ngrok_generation
    with _ngrok_lock:
        _ngrok_generation += 1
        generation = _ngrok_generation
    threading.Thread(
        target=_start_ngrok_tunnel_sync, args=(port, generation),
        daemon=True, name="ngrok-tunnel",
    ).start()


def _start_ngrok_tunnel_sync(port, generation):
    global _ngrok_proc, _ngrok_last_start
    try:
        domain = _read_env_value("NGROK_DOMAIN")
        if not domain:
            return  # feature disabled - silent (deliberate no-config case)
        ngrok_bin = _find_ngrok_binary()
        if not ngrok_bin:
            logger.warning(
                "ngrok tunnel skipped - NGROK_DOMAIN is configured but "
                "the ngrok binary was not found"
            )
            _notify_transition(
                True,
                "TradingView webhook tunnel DOWN (ngrok not installed) - "
                "alerts will not arrive. Manual trading is NOT affected."
            )
            return
        authtoken = _read_env_value("NGROK_AUTHTOKEN")

        with _ngrok_lock:
            _sweep_leftover_tunnels(domain)
            time.sleep(1.0)  # let a just-stopped old tunnel release it

            _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(_LOG_FILE, "ab")
            args = [ngrok_bin, "http", f"--url={domain}", "--inspect=false"]
            if authtoken:
                args.append(f"--authtoken={authtoken}")
            args.append(str(port))

            _ngrok_last_start = time.time()
            logger.info(
                f"starting ngrok tunnel {domain} -> 127.0.0.1:{port}"
            )
            try:
                _ngrok_proc = subprocess.Popen(
                    args, stdout=log_fh, stderr=subprocess.STDOUT
                )
            except Exception as exc:
                logger.error(f"ngrok tunnel failed to start: {exc}")
                _notify_transition(
                    True,
                    f"TradingView webhook tunnel DOWN ({exc}) - alerts "
                    "will not arrive. Manual trading is NOT affected."
                )
                return

        # verify + watchdog run OUTSIDE the lock (an old bug held the
        # lock through the infinite watchdog loop - every shutdown and
        # every restart deadlocked on it)
        _verify_and_watch(domain, port, generation)
    except Exception as exc:  # trading must go on - no matter what
        logger.error(f"ngrok tunnel thread error: {exc}")


def _verify_and_watch(domain, port, generation):
    """Verify the public URL (the server binds seconds after this runs,
    so early non-200s are 'not ready yet', not failures), then watchdog
    the process and restart it if it dies. Exits quietly when a newer
    generation starts or the tunnel is stopped."""
    for _ in range(30):
        if _ngrok_generation != generation:
            return
        if _ngrok_proc is not None and _ngrok_proc.poll() is not None:
            reason = _last_ngrok_error() or "ngrok exited"
            logger.warning(
                f"ngrok tunnel process exited during startup ({reason}) "
                f"- see {_LOG_FILE}"
            )
            _notify_transition(
                True,
                f"TradingView webhook tunnel DOWN ({reason}) - alerts "
                "will not arrive. Manual trading is NOT affected."
            )
            return
        if _public_url_answers(domain):
            logger.info(
                f"ngrok tunnel up: https://{domain} "
                "(webhook path: /trading_view_webhook)"
            )
            _notify_transition(
                False,
                f"TradingView webhook tunnel UP - alerts flowing to "
                f"https://{domain}/trading_view_webhook"
            )
            break
        time.sleep(1)
    else:
        reason = _last_ngrok_error() or "no response within 30s"
        logger.warning(
            f"ngrok tunnel did not confirm within 30s ({reason}) - "
            f"see {_LOG_FILE}"
        )
        _notify_transition(
            True,
            f"TradingView webhook tunnel DOWN ({reason}) - alerts will "
            "not arrive. Manual trading is NOT affected."
        )
        return

    while True:
        time.sleep(15)
        if _ngrok_generation != generation:
            return  # stopped, or a newer tunnel took over
        proc = _ngrok_proc
        if proc is None or proc.poll() is None:
            continue  # alive
        if time.time() - _ngrok_last_start < _restart_min_interval:
            continue
        logger.warning("ngrok tunnel died - restarting it")
        start_ngrok_tunnel(port)  # the fresh start owns verify + watchdog
        return


def stop_ngrok_tunnel():
    """Teardown for every shutdown path. Never raises - a shutdown must
    not hang because the tunnel refused to die."""
    global _ngrok_generation, _ngrok_proc
    try:
        with _ngrok_lock:
            _ngrok_generation += 1  # any running watchdog exits quietly
            proc = _ngrok_proc
            _ngrok_proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
        domain = _read_env_value("NGROK_DOMAIN")
        if domain:
            _sweep_leftover_tunnels(domain)
    except Exception as exc:
        logger.warning(f"ngrok tunnel teardown issue (ignored): {exc}")
