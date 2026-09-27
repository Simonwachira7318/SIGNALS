"""
envfile.py  -  Minimal .env loader (no extra packages).

Reads KEY=VALUE lines from .env next to this script into os.environ.
Variables already set (non-empty) in the real environment win over the
file, so a one-off `$env:X = ...` still overrides it.

    # comment
    WIFI_SENSE_DB_PASSWORD=root
    WIFI_SENSE_TELEGRAM_TOKEN="123:abc"
"""

import os

from paths import APP_DIR
ENV_FILE = APP_DIR / ".env"
_loaded = False


def load_env(path=ENV_FILE):
    global _loaded
    if _loaded or not path.exists():
        return
    _loaded = True
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        elif " #" in val:                       # trailing comment on unquoted value
            val = val.split(" #", 1)[0].rstrip()
        if key and not os.environ.get(key):      # set-but-empty counts as unset
            os.environ[key] = val


load_env()
