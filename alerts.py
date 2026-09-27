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
  * Snooze (all, or just movement / presence / devices) silences desktop +
    phone alerts until a time; detections are still recorded. Security never snoozes.
  * Grouping: alerts arriving within 60 s of the previous one are held and sent
    as ONE summary ("3 more alerts: 2× Light movement, 1× Unknown device").
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
INTERNAL = {"last_report_date", "snooze"}  # saved in settings.json, not edited via /api/settings
GROUP_WINDOW_S = 60              # alerts within this window of the last one are grouped into one summary
SNOOZE_GROUPS = {                # detection kind -> snooze group ("security" is never snoozed)
    "motion": "motion", "unusual": "motion", "room_motion": "motion", "calm": "motion",
    "occupied": "presence", "empty": "presence", "still": "presence", "blocked": "presence", "ble": "presence",
    "new_device": "devices", "arrived": "devices", "left": "devices", "health": "devices", "calibrated": "devices",
}
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
        self.last_error = None
        self.lock = threading.Lock()
        self.last_sent = 0.0             # when the last (non-grouped) alert went out
        self.batch = []                  # held alerts: (title, msg, desktop, phone)
        self.timer = None

    @property
    def phone_configured(self):
        return bool(self.token and self.chat)

    def status(self):
        return dict(phone_configured=self.phone_configured, last_error=self.last_error,
                    quiet_now=self.settings.in_quiet_hours(), snooze=self.snooze_state(),
                    held=len(self.batch))

    # ---------- snooze ----------
    def snooze_state(self):
        now = time.time()
        return {g: t for g, t in (self.settings.snapshot().get("snooze") or {}).items() if t > now}

    def is_snoozed(self, kind):
        if kind == "security":
            return False
        st = self.snooze_state()
        return "all" in st or SNOOZE_GROUPS.get(kind) in st

    def set_snooze(self, group, minutes):
        """group: all / motion / presence / devices, or 'resume' to clear everything. minutes 0 = clear group."""
        if group not in ("all", "motion", "presence", "devices", "resume"):
            raise ValueError("unknown snooze group")
        st = self.snooze_state()
        if group == "resume":
            st = {}
        elif minutes <= 0:
            st.pop(group, None)
        else:
            st[group] = time.time() + min(int(minutes), 7 * 24 * 60) * 60
        self.settings.set_internal("snooze", st)
        return st

    # ---------- routing + grouping ----------
    def handle(self, kind, severity, title, msg):
        s = self.settings.snapshot()
        desktop = s["desktop_alerts"] and kind in ("motion", "security") and \
            (kind == "security" or not self.settings.in_quiet_hours())
        phone = self.phone_configured and (kind == "security" or (s["away_mode"] and kind in AWAY_KINDS))
        if not (desktop or phone) or self.is_snoozed(kind):
            return
        now = time.time()
        with self.lock:
            if kind != "security" and now - self.last_sent < GROUP_WINDOW_S:
                self.batch.append((title, msg, desktop, phone))           # hold: sent as one summary
                if self.timer is None:
                    self.timer = threading.Timer(self.last_sent + GROUP_WINDOW_S - now, self._flush)
                    self.timer.daemon = True
                    self.timer.start()
                return
            self.last_sent = now
        self._deliver(title, msg, desktop, phone)

    def _deliver(self, title, msg, desktop, phone):
        if desktop:
            self.desktop_notify(title, msg)
        if phone:
            self.send_phone(f"{title}\n{msg}")

    def _flush(self):
        with self.lock:
            batch, self.batch, self.timer = self.batch, [], None
            if not batch:
                return
            self.last_sent = time.time()
        counts = {}
        for title, *_ in batch:
            counts[title] = counts.get(title, 0) + 1
        summary = ", ".join(f"{n}× {t}" if n > 1 else t for t, n in counts.items())
        head = f"{len(batch)} more alert{'s' if len(batch) != 1 else ''}"
        if any(d for *_, d, _p in batch):
            self.desktop_notify(head, summary)
        if any(p for *_, p in batch):
            lines = "\n".join(f"• {t}: {m}" for t, m, *_ in batch[:10])
            self.send_phone(f"{head} in the last minute:\n{lines}" + (f"\n…and {len(batch) - 10} more" if len(batch) > 10 else ""))

    def send_phone(self, text, wait=False):
        """Send a Telegram message. Runs in a thread unless wait=True."""
        def _send():
            try:
                data = urllib.parse.urlencode(dict(chat_id=self.chat, text=f"📡 WiFi Sense\n{text}")).encode()
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
