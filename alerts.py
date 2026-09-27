"""
alerts.py  -  Settings + alert routing (desktop and Telegram phone alerts).

Settings live in settings.json next to this script and are edited from the
dashboard. Secrets are NOT stored there: the Telegram bot token and chat id
come from environment variables:

    $env:WIFI_SENSE_TELEGRAM_TOKEN = '123456:ABC...'   # from @BotFather
    $env:WIFI_SENSE_TELEGRAM_CHAT  = '987654321'       # your chat id

Routing rules
-------------
  * Away mode ON  -> phone alert for motion, presence, room occupied,
                     unknown devices and security.
  * Away mode OFF -> phone alert only for security events.
  * Desktop notifications: motion + security, muted during quiet hours
    (security still shows).
"""

import os
import json
import time
import threading
import urllib.parse
import urllib.request

import envfile  # noqa: F401  (loads .env)

from paths import APP_DIR
SETTINGS_FILE = APP_DIR / "settings.json"
DEFAULTS = dict(
    away_mode=False,
    quiet_enabled=True, quiet_start="23:00", quiet_end="06:00",
    desktop_alerts=True,
    retention_days=7,            # delete `scans` rows older than this
    adaptive_thresholds=True,
    presence_hold_min=5,         # keep "occupied" this long after the last evidence
    daily_report_enabled=True,
    daily_report_time="08:00",
    motion_threshold_scale=1.0,  # global sensitivity (>1 = less sensitive); set from the Tuning lab
    episode_gap_s=30,            # quiet this long before a motion episode ends
    latency_test=True,           # ping router + internet once a minute for the Wi-Fi health log
    unusual_alerts=True,         # alert on movement at unusual times (learned per hour of week)
)
RANGES = {"presence_hold_min": (1, 60), "episode_gap_s": (5, 600), "retention_days": (1, 365),
          "motion_threshold_scale": (0.3, 4.0)}
INTERNAL = {"last_report_date"}  # saved in settings.json, not editable from the UI
PHONE_COOLDOWN_S = 60
AWAY_KINDS = {"motion", "still", "blocked", "new_device", "security", "ble", "occupied",
              "unusual", "room_motion"}


def _minutes(hhmm):
    h, m = hhmm.split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time {hhmm!r}")
    return h * 60 + m


class Settings:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        if SETTINGS_FILE.exists():
            try:
                self.data.update(json.loads(SETTINGS_FILE.read_text()))
            except Exception as e:
                print(f"[!] could not read {SETTINGS_FILE.name}: {e}")

    def get(self, key):
        with self.lock:
            return self.data[key]

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def update(self, changes):
        """Apply validated changes from the UI; unknown keys are ignored."""
        clean = {}
        for k, v in changes.items():
            if k not in DEFAULTS:
                continue
            lo, hi = RANGES.get(k, (1, 365))
            if isinstance(DEFAULTS[k], bool):
                clean[k] = bool(v)
            elif isinstance(DEFAULTS[k], float):
                clean[k] = round(max(lo, min(hi, float(v))), 3)
            elif isinstance(DEFAULTS[k], int):
                clean[k] = max(lo, min(hi, int(v)))
            elif k in ("quiet_start", "quiet_end", "daily_report_time"):
                _minutes(str(v))                      # raises on bad input
                clean[k] = str(v)[:5]
        with self.lock:
            self.data.update(clean)
            SETTINGS_FILE.write_text(json.dumps(self.data, indent=1))
        return self.snapshot()

    def set_internal(self, key, value):
        assert key in INTERNAL
        with self.lock:
            self.data[key] = value
            SETTINGS_FILE.write_text(json.dumps(self.data, indent=1))

    def in_quiet_hours(self):
        s = self.snapshot()
        if not s["quiet_enabled"]:
            return False
        now = time.localtime()
        cur = now.tm_hour * 60 + now.tm_min
        a, b = _minutes(s["quiet_start"]), _minutes(s["quiet_end"])
        return a <= cur < b if a <= b else cur >= a or cur < b


class Alerter:
    def __init__(self, settings, desktop_notify):
        self.settings = settings
        self.desktop_notify = desktop_notify
        self.token = os.environ.get("WIFI_SENSE_TELEGRAM_TOKEN", "").strip()
        self.chat = os.environ.get("WIFI_SENSE_TELEGRAM_CHAT", "").strip()
        self.last_phone = {}
        self.last_desktop = 0.0
        self.last_error = None

    @property
    def phone_configured(self):
        return bool(self.token and self.chat)

    def status(self):
        return dict(phone_configured=self.phone_configured, last_error=self.last_error,
                    quiet_now=self.settings.in_quiet_hours())

    def handle(self, kind, severity, title, msg):
        s = self.settings.snapshot()
        now = time.time()
        # desktop
        if s["desktop_alerts"] and kind in ("motion", "security"):
            if (kind == "security" or not self.settings.in_quiet_hours()) and now - self.last_desktop > 8:
                self.last_desktop = now
                self.desktop_notify(title, msg)
        # phone
        wants_phone = kind == "security" or (s["away_mode"] and kind in AWAY_KINDS)
        if wants_phone and self.phone_configured and now - self.last_phone.get(kind, 0) > PHONE_COOLDOWN_S:
            self.last_phone[kind] = now
            self.send_phone(f"{title}\n{msg}")

    def send_phone(self, text, wait=False):
        """Send a Telegram message. Runs in a thread unless wait=True."""
        def _send():
            try:
                data = urllib.parse.urlencode(dict(chat_id=self.chat, text=f"📡 Wi-Fi Sense\n{text}")).encode()
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                with urllib.request.urlopen(url, data=data, timeout=20) as r:
                    r.read()
                self.last_error = None
                return True
            except Exception as e:
                # never echo the URL: it contains the bot token
                self.last_error = f"{type(e).__name__}: {getattr(e, 'reason', '') or getattr(e, 'code', '')}"
                print(f"[!] phone alert failed: {self.last_error}")
                return False
        if not self.phone_configured:
            self.last_error = "Telegram not configured (set WIFI_SENSE_TELEGRAM_TOKEN and _CHAT)"
            return False
        if wait:
            return _send()
        threading.Thread(target=_send, daemon=True).start()
        return True
