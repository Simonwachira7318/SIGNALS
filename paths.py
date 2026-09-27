"""
paths.py  -  Where files live, for both `python wifi_web.py` and the packaged .exe.

APP_DIR  user data: .env, settings.json, activity_model.json, floorplan files, backups/.
         The script folder, or the folder containing WiFiSense.exe when packaged.
RES_DIR  read-only resources bundled with the app: HTML pages, icons, oui_manuf.txt.
         The script folder, or PyInstaller's unpack folder when packaged.
"""

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = RES_DIR = Path(__file__).resolve().parent


def resource(name):
    """A bundled file, preferring a copy next to the app (so it can be updated in place)."""
    local = APP_DIR / name
    return local if local.exists() else RES_DIR / name
