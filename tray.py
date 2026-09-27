"""
tray.py  -  Run Wi-Fi Sense in the background with a system-tray icon.

    pythonw tray.py            # no console window
    WiFiSense.exe              # the packaged version (see build_exe.ps1)

Tray menu: Open dashboard (also on double-click) / Open log / Quit.
Output goes to wifi_sense.log next to the app, since there's no console.
"""

import os
import sys
import threading
import time
import webbrowser

from paths import APP_DIR

LOG_FILE = APP_DIR / "wifi_sense.log"


def _redirect_output():
    """pythonw / windowed exe have no console: send prints to a log file (kept under ~1 MB)."""
    if sys.stdout is None or not sys.stdout.isatty():
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
        f = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
        print(f"\n===== Wi-Fi Sense started {time.strftime('%Y-%m-%d %H:%M:%S')} =====")


def _icon_image():
    from PIL import Image, ImageDraw
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, s - 3, s - 3), radius=14, fill=(42, 120, 214, 255))
    c = s // 2
    d.ellipse((c - 5, c - 5, c + 5, c + 5), fill="white")
    for r in (13, 22):
        d.arc((c - r, c - r, c + r, c + r), 135, 225, fill="white", width=4)
        d.arc((c - r, c - r, c + r, c + r), -45, 45, fill="white", width=4)
    return img


def main():
    _redirect_output()
    if "--no-browser" not in sys.argv:
        sys.argv.append("--no-browser")          # the tray opens the page itself once the server is up
    import wifi_web

    url = f"http://127.0.0.1:{wifi_web.PORT}"
    server_thread = threading.Thread(target=wifi_web.main, daemon=True)
    server_thread.start()

    try:
        import pystray
    except ImportError:
        print("[!] pystray not installed; running without a tray icon (pip install pystray)")
        server_thread.join()
        return

    def open_dash(icon=None, item=None):
        webbrowser.open(url)

    def open_log(icon, item):
        os.startfile(LOG_FILE)

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)                              # stops the sensor/server threads too

    icon = pystray.Icon("wifi_sense", _icon_image(), "Wi-Fi Sense",
                        menu=pystray.Menu(pystray.MenuItem("Open dashboard", open_dash, default=True),
                                          pystray.MenuItem("Open log", open_log),
                                          pystray.Menu.SEPARATOR,
                                          pystray.MenuItem("Quit Wi-Fi Sense", quit_app)))

    def watch():
        time.sleep(4)
        if not server_thread.is_alive():         # e.g. MySQL down or already running
            icon.notify("Wi-Fi Sense could not start. See the log (right-click > Open log).", "Wi-Fi Sense")
        elif "--open" in sys.argv:
            open_dash()
    threading.Thread(target=watch, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
