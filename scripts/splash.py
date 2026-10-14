import queue
import sys
import threading
import time
import urllib.request
from pathlib import Path

import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk

ROOT = Path(__file__).resolve().parents[1]
IMAGE_PATH = ROOT / "images" / "image.png"
SERVER_PORT = 5050
SPLASH_MAX_WIDTH = 620
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


def main():
    result_q = queue.Queue()
    threading.Thread(target=probe_server, args=(result_q,), daemon=True).start()

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=_bg)

    photo = None
    if IMAGE_PATH.exists():
        img = Image.open(IMAGE_PATH).convert("RGBA")
        if img.width > SPLASH_MAX_WIDTH:
            scale = SPLASH_MAX_WIDTH / img.width
            img = img.resize(
                (SPLASH_MAX_WIDTH, int(img.height * scale)),
                Image.LANCZOS,
            )
        photo = ImageTk.PhotoImage(img)

    if photo:
        body = tk.Label(root, image=photo, bg=_bg)
        body.pack()
    else:
        body = tk.Label(
            root, text="AlgoOptionScalper", bg=_bg, fg=_fg,
            font=("Segoe UI", 22, "bold"),
        )
        body.pack(padx=60, pady=(48, 8))

    status_var = tk.StringVar(value="Starting server")
    status = tk.Label(
        root, textvariable=status_var, bg=_bg, fg=_muted,
        font=("Segoe UI", 10),
    )
    status.pack(pady=(2, 10))

    bar = ttk.Progressbar(root, mode="indeterminate", length=260)
    bar.pack(pady=(0, 24))
    bar.start(24)

    frame_w = max(root.winfo_reqwidth(), 360)
    frame_h = root.winfo_reqheight()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    x = (screen_w - frame_w) // 2
    y = (screen_h - frame_h) // 2
    root.geometry(f"+{x}+{y}")

    start_ts = time.monotonic()
    dots = 0

    def tick():
        nonlocal dots
        if not result_q.empty():
            root.destroy()
            return

        elapsed = time.monotonic() - start_ts
        if elapsed > GIVE_UP_AFTER_S:
            status_var.set(
                "Server did not come up - check the console window for errors."
            )
            bar.stop()
            return

        dots = (dots + 1) % 4
        if elapsed > SLOW_START_AFTER_S:
            status_var.set(
                "Still starting"
                + "." * dots
                + "  (first launch of the day can be slow)"
            )
        else:
            status_var.set("Starting server" + "." * dots)

        root.after(400, tick)

    tick()
    root.mainloop()
    sys.exit(0)


if __name__ == "__main__":
    main()
