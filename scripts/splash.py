"""Splash screen - first thing on screen when the taskbar icon is
clicked, showing the app logo and a live one-line progress feed until
the server is online (or GIVE_UP_AFTER_S elapses).

Fast-first-paint design: the window (background, title, status line,
progress bar) is drawn BEFORE any heavy work, so it appears in a few
hundred ms. The logo image is loaded in a deferred callback and the
hidden server subprocess is spawned right after the first paint - no
separate launcher script is needed.
"""
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import tkinter as tk
from tkinter import ttk

ROOT = Path(__file__).resolve().parents[1]
IMAGE_PATH = ROOT / "images" / "option_scaler.png"
# Pre-resized copy of option_scaler.png (620 px wide) for Tk's native PNG
# loader - decodes in tens of ms with NO Pillow import on the critical
# path. Regenerate after changing option_scaler.png:
#   venv\Scripts\python.exe -c "from PIL import Image; img =
#   Image.open('images/option_scaler.png').convert('RGBA'); s = 620 /
#   img.width; img.resize((620, int(img.height * s)),
#   Image.LANCZOS).save('images/option_scaler_620.png', optimize=True)"
SMALL_IMAGE_PATH = ROOT / "images" / "option_scaler_620.png"
SERVER_PORT = 5050
# server.py overwrites this file at each startup milestone; tick()
# shows its content as the status one-liner.
STATUS_PATH = ROOT / "data" / "startup_status.txt"
SPLASH_MAX_WIDTH = 620
# Fixed splash window width - matches the pre-sized 620px logo so the
# text-only first paint and the loaded logo occupy the same box.
SPLASH_WIDTH = 620
SLOW_START_AFTER_S = 60
GIVE_UP_AFTER_S = 180


def _log_crash(exc_type, exc, tb):
    import traceback
    log = ROOT / "data" / "logs" / "splash_error.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
            traceback.print_exception(exc_type, exc, tb, file=f)
            f.write("\n")
    except Exception:
        pass


sys.excepthook = _log_crash

_bg = "#0b0f14"
_fg = "#e8edf2"
_muted = "#8a97a5"


def probe_server(result_q):
    url = f"http://127.0.0.1:{SERVER_PORT}/"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    result_q.put(True)
                    return
        except Exception:
            pass
        time.sleep(0.5)


def _server_alive():
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{SERVER_PORT}/", timeout=1
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _spawn_server():
    """Start the hidden server (pythonw, no console). Windows only -
    the macOS/Linux launchers (start.sh) run server.py themselves."""
    if sys.platform != "win32":
        return
    pythonw = ROOT / "venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        return
    subprocess.Popen(
        [str(pythonw), "server.py"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _open_app_window():
    """(Re)open the Chrome/Edge app-mode window for a server that is
    already running headless. Mirrors server.py's _open_app_window_to:
    when the splash is launched while an instance is already up,
    nothing else re-opens the window (the old side effect of the
    second server instance hitting the single-instance guard no
    longer happens, since the splash does not spawn a rival server)."""
    url = f"http://127.0.0.1:{SERVER_PORT}"
    for candidate in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ):
        if os.path.exists(candidate):
            subprocess.Popen([candidate, f"--app={url}"])
            return
    import webbrowser
    webbrowser.open(url)


def main():
    result_q = queue.Queue()
    threading.Thread(target=probe_server, args=(result_q,), daemon=True).start()

    # Wall-clock launch time. The status file must be written no
    # earlier than this to be shown - a leftover file from a previous
    # run (yesterday's last line) is stale and ignored.
    launch_ts = time.time()

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=_bg)

    # Fixed-size splash: the logo area is exactly the pre-sized logo's
    # 620x620 box from the FIRST paint, so the text-only stand-in and
    # the loaded logo fill the same window - no resize jump when the
    # image arrives - and the status line and progress bar sit in the
    # same spot underneath the logo for the whole startup.
    logo_area = tk.Frame(
        root, width=SPLASH_WIDTH, height=SPLASH_WIDTH, bg=_bg,
        highlightthickness=1, highlightbackground="#22303f",
    )
    logo_area.pack_propagate(False)  # keep the box sized before the logo exists
    logo_area.pack(pady=(16, 0))

    # First paint is text-only on purpose: no image decode, no heavy
    # imports - the window is on screen as fast as Tk can manage.
    body = tk.Label(
        logo_area, text="OptionScalper", bg=_bg, fg=_fg,
        font=("Segoe UI", 26, "bold"),
    )
    body.place(relx=0.5, rely=0.5, anchor="center")

    status_var = tk.StringVar(value="Starting")
    status = tk.Label(
        root, textvariable=status_var, bg=_bg, fg=_muted,
        font=("Segoe UI", 11),
    )
    status.pack(pady=(8, 10))

    bar = ttk.Progressbar(root, mode="indeterminate", length=260)
    bar.pack(pady=(0, 20))
    bar.start(24)

    # Borderless (overrideredirect) windows have no close box - without
    # this the splash lingers with no way to dismiss it.
    close_btn = tk.Label(
        root, text="\u2715", bg=_bg, fg=_muted, font=("Segoe UI", 10)
    )
    close_btn.place(relx=1.0, x=-10, y=8, anchor="ne")
    close_btn.bind("<Button-1>", lambda e: root.destroy())

    def center():
        frame_w = max(root.winfo_reqwidth(), 360)
        frame_h = root.winfo_reqheight()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = (screen_w - frame_w) // 2
        y = (screen_h - frame_h) // 2
        root.geometry(f"+{x}+{y}")

    center()
    # First paint happens HERE - after this the window is on screen.
    root.update_idletasks()
    root.update()

    def load_logo():
        """Deferred logo load - runs once the window is already
        visible. Tk's native PhotoImage handles the pre-sized PNG;
        Pillow (full-res option_scaler.png + resize) is only a fallback."""
        photo = None
        if SMALL_IMAGE_PATH.exists():
            try:
                photo = tk.PhotoImage(file=str(SMALL_IMAGE_PATH))
            except Exception:
                photo = None
        if photo is None and IMAGE_PATH.exists():
            try:
                from PIL import Image, ImageTk
                img = Image.open(IMAGE_PATH).convert("RGBA")
                if img.width > SPLASH_MAX_WIDTH:
                    scale = SPLASH_MAX_WIDTH / img.width
                    img = img.resize(
                        (SPLASH_MAX_WIDTH, int(img.height * scale)),
                        Image.LANCZOS,
                    )
                photo = ImageTk.PhotoImage(img)
            except Exception:
                photo = None
        if photo is not None:
            body.configure(image=photo, text="", padx=0, pady=0)
            body.image = photo  # keep a reference, else GC blanks it
            center()

    root.after(20, load_logo)

    # Window is visible - now bring up the server (unless one is
    # already running, e.g. a second click or start_app.bat session).
    if _server_alive():
        # Headless server already up: its startup opened the app
        # window once; clicking the icon again must bring that
        # window back instead of flashing the splash and doing
        # nothing.
        _open_app_window()
        root.destroy()
        return
    _spawn_server()

    start_ts = time.monotonic()
    dots = 0
    shown_status = "Starting server"

    def _poll_server_status():
        """Latest server-published startup line, or None. The file is
        only trusted when it was written after this splash launched."""
        try:
            if STATUS_PATH.exists() and STATUS_PATH.stat().st_mtime >= launch_ts - 1:
                line = STATUS_PATH.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                if line:
                    return line
        except OSError:
            pass
        return None

    def tick():
        nonlocal dots, shown_status
        if not result_q.empty():
            root.destroy()
            return

        elapsed = time.monotonic() - start_ts
        if elapsed > GIVE_UP_AFTER_S:
            status_var.set(
                "Server did not come up - check data\\logs for errors."
            )
            bar.stop()
            # Auto-dismiss so a failed start cannot leave the splash
            # sitting on screen forever.
            root.after(15000, root.destroy)
            return

        line = _poll_server_status()
        if line:
            shown_status = line

        dots = (dots + 1) % 4
        if elapsed > SLOW_START_AFTER_S:
            status_var.set(
                "Still starting"
                + "." * dots
                + "  (first launch of the day can be slow)"
            )
        else:
            status_var.set(shown_status + "." * dots)

        root.after(400, tick)

    tick()
    root.mainloop()
    sys.exit(0)


if __name__ == "__main__":
    main()
