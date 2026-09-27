#!/usr/bin/env python3
"""
wifi_web.py  -  Browser dashboard (SVG icons) for passive Wi-Fi sensing.

Reuses the distance model, MySQL setup and notifications from wifi_dashboard.py
and adds a richer scanner + detection engine, served as a local web page.

DETECTIONS
----------
  motion      signal wobble on one or more paths above that path's own
              (self-learned) threshold; graded light / strong. One event per episode.
  calm        the motion episode ended (with its duration).
  blocked     a router's signal dropped sharply below its learned baseline.
  still       a router's signal sits steadily off its baseline without
              wobbling (possible stationary person/object on that path).
  new_device  a Wi-Fi source appeared (e.g. a phone hotspot switched on).
  left        a Wi-Fi source disappeared.
  security    possible "evil twin" (your network name from another device) or
              a known network's security type changed.
  ble         more Bluetooth devices came close (phones, watches...).
  calibrated  a router's 1-metre signal level was measured.
  occupied /  fused room state (movement, still presence, AI model, Bluetooth)
  empty       with a hold time, so it doesn't flicker (presence.py).
  arrived     a *known* (named/trusted) device came back; unknown ones raise new_device.

FEEDBACK
--------
  Each detection has 👍/👎 buttons. False alarms raise that path's threshold,
  confirmations lower it slightly (devices.py). 👎 on a device event mutes that
  device; 👎 on a security alert marks it trusted.

EXTRAS
------
  * Live link sampled ~10x/second via the Windows Native Wi-Fi API (wlan_native.py).
  * Per-path adaptive thresholds learned from each path's own noise.
  * Per-router distance calibration ("stand next to it" button).
  * Trainable activity classifier (classifier.py): label, train, predict.
  * MAC vendor lookup (oui.py), passive Bluetooth counting (ble_scan.py).
  * Desktop + Telegram phone alerts, away mode, quiet hours (alerts.py).
  * History page, CSV export, automatic clean-up of old scan rows.
  * Devices page: name, trust or mute every device ever seen, 7-day presence.
  * Daily summary to Telegram (report.py). Config/secrets from .env (envfile.py).

HONEST LIMITS
-------------
Passive / receive-only. Distances are estimates. Direction is NOT measurable
with one antenna (radar angle is arbitrary). A person icon marks a disturbed
signal PATH, not a person's position. It can't count people.

USAGE
-----
    $env:WIFI_SENSE_DB_PASSWORD = 'yourpassword'   # if MySQL root has one
    python wifi_web.py
Opens http://127.0.0.1:8765 in your browser. Ctrl+C to stop.
"""

import os
import re
import base64
import hmac
import socket
import sys
import csv
import io
import json
import time
import statistics
import threading
import subprocess
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import envfile  # noqa: F401  (loads .env before anything reads os.environ)
from wifi_dashboard import (
    DB_NAME, SCAN_INTERVAL, HISTORY_LEN, MOTION_STD_DB, BASELINE_SCANS,
    ACTIVITY_WINDOW, MEASURED_POWER_DBM, PATH_LOSS_EXPONENT,
    percent_to_dbm, db_connect, log_scan, log_motion, notify,
)
import oui
from alerts import Settings, Alerter
from classifier import ActivityModel
from ble_scan import BLEPresence
from devices import DeviceRegistry, prepare as devices_prepare, presence_grid
from presence import PresenceFusion
from report import build_summary
from analysis import rhythm, rate_features
import health
import rollup
from anomaly import UnusualDetector
import replay
import insights
from rules import RuleEngine, prepare as rules_prepare

try:
    from wlan_native import LinkRSSI
except Exception:
    LinkRSSI = None

from paths import APP_DIR, resource
HOST = os.environ.get("WIFI_SENSE_BIND", "127.0.0.1")    # 0.0.0.0 = reachable on your network (needs a token)
PORT = int(os.environ.get("WIFI_SENSE_PORT", "8765"))
ACCESS_TOKEN = os.environ.get("WIFI_SENSE_ACCESS_TOKEN", "").strip()
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
FLOORPLAN_JSON = APP_DIR / "floorplan.json"
NODE_OFFLINE_S = 20
PAGES = {"/": "wifi_web.html", "/index.html": "wifi_web.html",
         "/history": "history.html", "/history.html": "history.html",
         "/devices": "devices.html", "/devices.html": "devices.html",
         "/health": "health.html", "/floorplan": "floorplan.html", "/rules": "rules.html",
         "/tuning": "tuning.html", "/kiosk": "kiosk.html"}
BRAND_FILES = {"logo_mark.png": "image/png", "logo_full.png": "image/png", "favicon.ico": "image/x-icon",
               "favicon-32.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png",
               "apple-touch-icon.png": "image/png"}
RECENT_SCANS = 2400                # ~60 min of per-scan path ratios kept in memory (why-drilldown, heat trail)
STATIC = {"/nav.js": ("nav.js", "text/javascript; charset=utf-8"),
          "/nav.css": ("nav.css", "text/css; charset=utf-8"),
          "/manifest.json": ("manifest.json", "application/manifest+json"),
          "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
          "/icon.svg": ("icon.svg", "image/svg+xml")}

SPARK_LEN        = 40     # samples kept per router for sparklines
BLOCK_DROP_PCT   = 15     # drop below baseline => "blocked"
STILL_DEV_PCT    = 8      # steady offset from baseline => "still"
STILL_SCANS      = 6      # ...held this many scans
BLOCK_SCANS      = 2
LEFT_AFTER_SCANS = 12     # unseen this long => "left"
OCCUPANCY_WINDOW = 200    # scans (~5 min) for room-activity %
DB_STATS_EVERY   = 10     # scans between MySQL stats queries
SHADOW_DB        = 6.0    # typical indoor spread (dB) around the path-loss model
SHADOW_DB_CAL    = 4.0    # ...after calibrating that router

# adaptive thresholds: threshold = clamp(NOISE_MULT * learned_noise + margin, lo, hi)
NOISE_MULT       = 3.0
AP_TH_RANGE      = (2.0, 8.0)     # % units (Windows signal quality)
LINK_TH_RANGE    = (1.0, 6.0)     # dB units (native RSSI)
LINK_TH_DEFAULT  = 2.0
LINK_HZ          = 10             # live link samples per second
LINK_WINDOW_S    = 3.0            # wobble window for the live link
CALIB_SCANS      = 8              # scans averaged when calibrating a router
RETENTION_EVERY  = 3600           # seconds between clean-up runs
BLE_JUMP         = 2              # near-device increase that triggers an event
ROLLUP_EVERY     = 300            # seconds between hourly-rollup checks
LINK_MISS_SCANS  = 2              # consecutive scans without a link before "disconnected"

FEATURES = ["idx_all", "mean_ratio", "link_ratio", "frac_moving",
            "frac_off", "mean_absdev", "link_absdev", "ble_near",
            "gait_share", "rhythm_std", "dom_hz", "rate_instability", "rate_drop"]
RHYTHM_WINDOW_S = 12.8            # seconds of live-link samples per rhythm analysis

HOTSPOT_RE = re.compile(
    r"iphone|android|galaxy|redmi|xiaomi|infinix|tecno|itel|oppo|vivo|huawei|"
    r"nokia|pixel|oneplus|samsung|hotspot|'s phone|mobile", re.I)


# ------------------------- Scanning -------------------------
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flashes when run from the tray


def _netsh(*args):
    raw = subprocess.run(["netsh", "wlan", *args], capture_output=True, timeout=15,
                         creationflags=NO_WINDOW).stdout
    out = raw.decode("utf-8", errors="replace")
    if out.count("�") > len(out) // 20:
        out = raw.decode("cp1252", errors="replace")
    return out


def _kv(line):
    k, sep, v = line.partition(" : ")
    return (k.strip(), v.strip()) if sep else (None, None)


def scan_full():
    """{bssid: dict(ssid, pct, auth, radio, band, channel)} from the scan list."""
    nets, ssid, auth, cur = {}, None, None, None
    for line in _netsh("show", "networks", "mode=bssid").splitlines():
        k, v = _kv(line.strip())
        if not k:
            continue
        if re.match(r"^SSID \d+$", k):
            ssid, auth, cur = v or "<hidden>", None, None
        elif k == "Authentication":
            auth = v
        elif re.match(r"^BSSID \d+$", k) and re.fullmatch(r"[0-9a-fA-F:]{17}", v):
            cur = nets[v.lower()] = dict(ssid=ssid or "<unknown>", pct=0, auth=auth,
                                         radio=None, band=None, channel=None)
        elif cur is not None:
            if k == "Signal":
                cur["pct"] = int(v.rstrip("%") or 0)
            elif k == "Radio type":
                cur["radio"] = v
            elif k == "Band":
                cur["band"] = v
            elif k == "Channel":
                cur["channel"] = int(v) if v.isdigit() else None
    return nets


def link_info():
    """The connected link's metadata (SSID, rates, channel, security)."""
    info = {}
    for line in _netsh("show", "interfaces").splitlines():
        k, v = _kv(line.strip())
        if not k:
            continue
        if k in ("SSID", "State", "Band", "Radio type", "Authentication"):
            info[k.lower().replace(" ", "_")] = v
        elif k in ("AP BSSID", "BSSID") and re.fullmatch(r"[0-9a-fA-F:]{17}", v):
            info["bssid"] = v.lower()
        elif k == "Signal":
            info["pct"] = int(v.rstrip("%") or 0)
        elif k == "Channel" and v.isdigit():
            info["channel"] = int(v)
        elif k.startswith("Receive rate"):
            info["rx"] = float(v or 0)
        elif k.startswith("Transmit rate"):
            info["tx"] = float(v or 0)
    return info if info.get("bssid") else None


def device_type(ssid, bssid):
    if ssid == "<hidden>":
        return "hidden"
    if ssid.startswith("DIRECT-"):
        return "direct"                       # printer / TV / Wi-Fi Direct
    if HOTSPOT_RE.search(ssid):
        return "phone"
    if int(bssid[:2], 16) & 0x02:             # locally administered MAC
        return "virtual"                      # often a phone hotspot or extra SSID
    return "router"


def same_hardware_family(a, b):
    """True if two BSSIDs look like radios of the same device/vendor (mesh, dual-band)."""
    ma, mb = bytes.fromhex(a.replace(":", "")), bytes.fromhex(b.replace(":", ""))
    if (ma[0] & ~0x02) == (mb[0] & ~0x02) and ma[1:3] == mb[1:3]:
        return True
    va, vb = oui.vendor(a), oui.vendor(b)
    return bool(va and va == vb and va != "Randomized MAC")


def _std(h):
    n = len(h); mean = sum(h) / n
    return (sum((s - mean) ** 2 for s in h) / n) ** 0.5


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ------------------------- Database extras -------------------------
def db_prepare(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS detections (
                id        BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts        DATETIME NOT NULL,
                kind      VARCHAR(16) NOT NULL,
                severity  VARCHAR(10) NOT NULL,
                bssid     VARCHAR(17),
                ssid      VARCHAR(64),
                detail    VARCHAR(255),
                INDEX (ts), INDEX (kind)
            ) ENGINE=InnoDB
        """)
        cur.execute("SHOW COLUMNS FROM detections LIKE 'confidence'")
        if not cur.fetchone():   # added after the first release
            cur.execute("ALTER TABLE detections ADD COLUMN confidence TINYINT UNSIGNED")
        for col, ddl in (("feedback", "TINYINT(1) NULL"), ("paths", "VARCHAR(200) NULL")):
            cur.execute(f"SHOW COLUMNS FROM detections LIKE '{col}'")
            if not cur.fetchone():
                cur.execute(f"ALTER TABLE detections ADD COLUMN {col} {ddl}")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS calibration (
                bssid          VARCHAR(17) PRIMARY KEY,
                ssid           VARCHAR(64),
                measured_power FLOAT NOT NULL,
                updated        DATETIME NOT NULL
            ) ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS training_samples (
                id        BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts        DATETIME NOT NULL,
                label     VARCHAR(32) NOT NULL,
                features  VARCHAR(512) NOT NULL,
                INDEX (label)
            ) ENGINE=InnoDB
        """)
    conn.commit()
    devices_prepare(conn)
    health.prepare(conn)
    rollup.prepare(conn)
    rules_prepare(conn)


# ------------------------- Accuracy -------------------------
def distance_m(dbm, measured_power):
    return 10 ** ((measured_power - dbm) / (10 * PATH_LOSS_EXPONENT))


def distance_range(dbm, wobble_db, measured_power, calibrated):
    """(distance, low_m, high_m, level): estimate with its likely range.

    Indoor signal strength varies ~6 dB around the path-loss model (walls,
    bodies, antenna angle; ~4 dB once calibrated); current wobble adds to that.
    """
    shadow = SHADOW_DB_CAL if calibrated else SHADOW_DB
    sigma = (shadow ** 2 + wobble_db ** 2) ** 0.5
    d = distance_m(dbm, measured_power)
    f = 10 ** (sigma / (10 * PATH_LOSS_EXPONENT))
    lo, hi = d / f, d * f
    span = hi - lo
    level = "high" if span < 3 else "medium" if span < 8 else "low"
    return round(d, 2), round(lo, 1), round(hi, 1), level


def system_confidence(n_sources, link_live, fast_link, settled, n_calibrated):
    """0-100: how much to trust detections right now, with the reasons."""
    src = min(1.0, n_sources / 4)
    score = 100 * (0.30 * src + 0.20 * link_live + 0.10 * fast_link
                   + 0.25 * settled + 0.15 * min(1.0, n_calibrated))
    reasons = [
        dict(ok=src >= 0.75, text=f"{n_sources} signal path{'s' if n_sources != 1 else ''} visible"
             + ("" if src >= 0.75 else " (4+ is better)")),
        dict(ok=bool(link_live), text="Live link to your router" if link_live
             else "Not connected: no live link"),
        dict(ok=bool(fast_link), text=f"Fast link sampling ({LINK_HZ}/s)" if fast_link
             else "Slow link sampling (native API unavailable)"),
        dict(ok=settled >= 1.0, text="Noise levels learned" if settled >= 1.0
             else f"Learning noise levels ({int(settled * 100)}%)"),
        dict(ok=n_calibrated > 0, text=f"{n_calibrated} router{'s' if n_calibrated != 1 else ''} calibrated"
             if n_calibrated else "No router calibrated yet"),
    ]
    level = "high" if score >= 70 else "medium" if score >= 40 else "low"
    return round(score), level, reasons


# ------------------------- Live link sampler -------------------------
class LinkSampler(threading.Thread):
    """Samples the connected link's RSSI (dBm) ~10x/second via wlanapi."""

    def __init__(self):
        super().__init__(daemon=True)
        self.samples = deque(maxlen=LINK_HZ * 60)     # (t, dbm), last minute
        self.lock = threading.Lock()
        try:
            self.api = LinkRSSI() if LinkRSSI else None
        except OSError:
            self.api = None

    @property
    def fast(self):
        return self.api is not None

    def run(self):
        if not self.api:
            return
        while True:
            v = self.api.rssi()
            if v is not None and -100 < v < 0:
                with self.lock:
                    self.samples.append((time.time(), v))
            time.sleep(1 / LINK_HZ)

    def add_slow(self, dbm):
        """Fallback when the native API is unavailable: one sample per scan."""
        if not self.api:
            with self.lock:
                self.samples.append((time.time(), dbm))

    def window(self, seconds):
        cutoff = time.time() - seconds
        with self.lock:
            return [v for t, v in self.samples if t >= cutoff]


# ------------------------- Sensor -------------------------
class AP:
    def __init__(self, bssid, info, scan_no):
        self.bssid = bssid
        self.info = info
        self.first_seen = scan_no
        self.last_seen = scan_no
        self.wob = deque(maxlen=HISTORY_LEN)
        self.spark = deque(maxlen=SPARK_LEN)
        self.baseline = None
        self.noise = MOTION_STD_DB / NOISE_MULT      # learned quiet-time wobble
        self.state = "stable"
        self.off_scans = 0
        self.gone = False
        self.vendor = oui.vendor(bssid)

    def threshold(self, adaptive, factor=1.0):
        base = MOTION_STD_DB if not adaptive else _clamp(NOISE_MULT * self.noise + 1.0, *AP_TH_RANGE)
        return base * factor


class Sensor(threading.Thread):
    def __init__(self, conn, settings, alerter, link_sampler, ble):
        super().__init__(daemon=True)
        self.conn = conn
        self.settings = settings
        self.alerter = alerter
        self.link = link_sampler
        self.ble = ble
        self.model = ActivityModel()
        self.aps = {}
        self.scan_no = 0
        self.activity = deque(maxlen=ACTIVITY_WINDOW)      # dict(t, v, link)
        self.occupancy = deque(maxlen=OCCUPANCY_WINDOW)
        self.feed = deque(maxlen=50)
        self.motion_on = False
        self.motion_start = 0.0
        self.last_motion = None
        self.db_stats = dict(today=0, hourly=[0] * 24, train_counts={})
        self.started = time.time()
        self.sys_conf = 0
        self.link_noise = LINK_TH_DEFAULT / NOISE_MULT
        self.link_baseline = None
        self.known_auth = {}                 # bssid -> auth
        self.ble_near_hist = deque(maxlen=80)
        self.last_ble_event = 0.0
        self.ml_probs = deque(maxlen=3)
        self.calibration = {}                # bssid -> measured power
        self.calib = None                    # active calibration session
        self.labeling = None                 # label being recorded
        self.labeled_now = 0
        self.last_cleanup = 0.0
        self.devices = DeviceRegistry(conn)
        self.presence = PresenceFusion()
        self.last_report_check = 0.0
        self.last_report = None              # dict(text, generated, sent)
        self.rx_hist = deque(maxlen=30)      # link receive rate (Mbps) per scan
        self.unusual = UnusualDetector()
        self.latest_link = None              # read by the health monitor thread
        self.health = None                   # HealthMonitor, set by main()
        self.prev_link = None
        self.link_miss = 0
        self.last_rollup = 0.0
        self.pending = deque()               # events posted by other threads, emitted in step()
        self.rules = None                    # RuleEngine, set by main()
        self.nodes = {}                      # name -> latest report from a remote node
        self.recent = deque(maxlen=RECENT_SCANS)  # (epoch, idx_all, link_ratio, {bssid: (ratio, state)})
        self.cond = threading.Condition()    # notified after every scan (live push to browsers)
        self.cur_thresholds = {}             # bssid -> threshold (%) used on the last scan
        self.lock = threading.Lock()
        self.state = {}
        self._load()

    # ---- helpers ----
    def _load(self):
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT id, ts, kind, severity, bssid, ssid, detail, confidence, feedback "
                            "FROM detections ORDER BY id DESC LIMIT 25")
                for i, ts, kind, sev, b, ssid, detail, conf, fb in reversed(cur.fetchall()):
                    self.feed.appendleft(dict(id=i, ts=str(ts), kind=kind, severity=sev, bssid=b,
                                              ssid=ssid, msg=detail, conf=conf, past=True,
                                              feedback=None if fb is None else ("correct" if fb else "false")))
                cur.execute("SELECT bssid, measured_power FROM calibration")
                self.calibration = {b: mp for b, mp in cur.fetchall()}
        except Exception as e:
            print(f"[!] could not load saved data: {e}")

    def emit(self, kind, severity, msg, ap=None, conf=None, title=None, paths=None):
        """Record a detection. conf = 0-1 confidence (scaled by system confidence).
        paths = BSSIDs involved (used to tune thresholds from feedback)."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        bssid = ap.bssid if ap else None
        ssid = ap.info["ssid"] if ap else None
        if conf is not None:
            conf = round(100 * _clamp(conf, 0.05, 1.0) * (0.6 + 0.4 * self.sys_conf / 100))
        paths = paths or ([bssid] if bssid else [])
        item = dict(id=None, ts=ts, kind=kind, severity=severity, bssid=bssid, ssid=ssid,
                    msg=msg, conf=conf, feedback=None)
        try:
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO detections (ts,kind,severity,bssid,ssid,detail,confidence,paths) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (ts, kind, severity, bssid, (ssid or "")[:64], msg[:255], conf,
                             ",".join(paths)[:200] or None))
                item["id"] = cur.lastrowid
            self.conn.commit()
        except Exception as e:
            print(f"[!] DB write failed: {e}")
        self.feed.appendleft(item)
        self.alerter.handle(kind, severity, title or kind.replace("_", " ").title(), msg)
        if self.rules:
            try:
                self.rules.on_event(dict(kind=kind, severity=severity, msg=msg, ts=ts,
                                         title=title or kind.replace("_", " ").title()))
            except Exception as e:
                print(f"[!] rules failed: {e}")

    def node_report(self, rep_, ip):
        """Store a report from a remote node (HTTP thread)."""
        with self.lock:
            prev = self.nodes.get(rep_["name"], {})
            self.nodes[rep_["name"]] = dict(prev, **rep_, ip=ip, received=time.time())

    def _process_nodes(self, now):
        """Per-room motion episodes from node reports; returns JSON-ready node list."""
        gap = self.settings.get("episode_gap_s")
        out = []
        with self.lock:
            items = list(self.nodes.items())
        for name, n in items:
            online = now - n.get("received", 0) < NODE_OFFLINE_S
            if online and n.get("motion") and not n.get("warming"):
                if not n.get("motion_on"):
                    n["motion_on"] = True
                    moving = [p for p in n["paths"] if p["state"] == "moving"]
                    self.emit("room_motion", "serious",
                              f"Movement in {name} ({len(moving)} signal path{'s' if len(moving) != 1 else ''})",
                              conf=min(1.0, 0.4 + 0.3 * max(0.0, n.get("idx", 1) - 1) + 0.1 * len(moving)),
                              title=f"Movement in {name}", paths=[p["bssid"] for p in moving])
                n["last_motion"] = now
            elif n.get("motion_on") and now - n.get("last_motion", now) > gap:
                n["motion_on"] = False
            out.append(dict(name=name, online=online, motion=bool(online and n.get("motion")),
                            idx=n.get("idx"), ip=n.get("ip"), last=n.get("ts"),
                            ago=round(now - n.get("received", now)), paths=n.get("paths", []),
                            last_motion=n.get("last_motion")))
        return sorted(out, key=lambda x: x["name"])

    def post_event(self, kind, severity, msg, title=None):
        """Thread-safe: queue an event from another thread; emitted on the sensor thread."""
        self.pending.append((kind, severity, msg, title))

    def refresh_db_stats(self):
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM detections "
                            "WHERE kind='motion' AND ts >= CURDATE()")
                today = cur.fetchone()[0]
                cur.execute("SELECT HOUR(ts), COUNT(*) FROM detections WHERE kind='motion' "
                            "AND ts >= NOW() - INTERVAL 23 HOUR - INTERVAL MINUTE(NOW()) MINUTE "
                            "GROUP BY HOUR(ts)")
                hourly = [0] * 24
                for h, c in cur.fetchall():
                    hourly[h] = c
                cur.execute("SELECT label, COUNT(*) FROM training_samples GROUP BY label")
                train_counts = dict(cur.fetchall())
            self.db_stats = dict(today=today, hourly=hourly, train_counts=train_counts)
        except Exception as e:
            print(f"[!] DB stats failed: {e}")

    def cleanup(self):
        """Delete scan rows older than the retention period (batched)."""
        days = self.settings.get("retention_days")
        try:
            with self.conn.cursor() as cur:
                for table in ("scans", "motion_events"):
                    while cur.execute(f"DELETE FROM {table} WHERE ts < NOW() - INTERVAL %s DAY "
                                      "LIMIT 20000", (days,)) == 20000:
                        self.conn.commit()
            self.conn.commit()
        except Exception as e:
            print(f"[!] clean-up failed: {e}")

    # ---- commands from the UI (called from HTTP threads) ----
    def start_calibration(self, bssid):
        with self.lock:
            if bssid is None:
                self.calib = None
            elif bssid in self.aps and not self.aps[bssid].gone:
                self.calib = dict(bssid=bssid, ssid=self.aps[bssid].info["ssid"], samples=[])
            else:
                return False
        return True

    def clear_calibration(self, bssid):
        with self.lock:
            self.calibration.pop(bssid, None)
        c = db_connect()
        try:
            with c.cursor() as cur:
                cur.execute("DELETE FROM calibration WHERE bssid=%s", (bssid,))
            c.commit()
        finally:
            c.close()

    def set_label(self, label):
        with self.lock:
            self.labeling = (label or "").strip()[:32] or None
            self.labeled_now = 0

    def feedback(self, det_id, verdict):
        """👍 ('correct') / 👎 ('false') on a detection. Returns a message for the UI."""
        if verdict not in ("correct", "false"):
            raise ValueError("verdict must be 'correct' or 'false'")
        c = db_connect()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT kind, bssid, ssid, paths, feedback FROM detections WHERE id=%s", (det_id,))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"detection {det_id} not found")
                kind, bssid, ssid, paths, old = row
                if old is not None:
                    return "Feedback already recorded for this detection."
                cur.execute("UPDATE detections SET feedback=%s WHERE id=%s",
                            (1 if verdict == "correct" else 0, det_id))
            c.commit()
        finally:
            c.close()
        for item in self.feed:
            if item.get("id") == det_id:
                item["feedback"] = verdict
        paths = [p for p in (paths or bssid or "").split(",") if p]
        name = self.devices.display(bssid, ssid) if bssid else None
        if kind == "motion":
            changed = self.devices.apply_feedback(paths, "motion_factor", verdict)
            if verdict == "false":
                return "False alarm noted. Motion threshold raised on " + ", ".join(
                    f"{self._name_of(p)} (×{f})" for p, f in changed) + "."
            return "Confirmed. Those paths get slightly more sensitive."
        if kind in ("blocked", "still"):
            changed = self.devices.apply_feedback(paths, "dev_factor", verdict)
            return (f"Noted. Presence threshold on {name} is now ×{changed[0][1]}." if changed
                    else "Noted.")
        if kind in ("new_device", "left", "arrived") and verdict == "false" and bssid:
            self.devices.update(bssid, muted=True)
            return f"Muted {name}: no more appear/leave events for it (unmute on the Devices page)."
        if kind == "security" and verdict == "false" and bssid:
            self.devices.update(bssid, trusted=True)
            return f"Marked {name} as trusted: no more security alerts for it."
        if kind == "empty" and verdict == "false":
            hold = min(60, self.settings.get("presence_hold_min") + 2)
            self.settings.update({"presence_hold_min": hold})
            return f"Noted. Presence hold time raised to {hold} min so the room isn't marked empty so soon."
        if kind == "occupied" and verdict == "false":
            hold = max(1, self.settings.get("presence_hold_min") - 1)
            self.settings.update({"presence_hold_min": hold})
            return f"Noted. Presence hold time lowered to {hold} min."
        return "Thanks, feedback recorded."

    def _name_of(self, bssid):
        ap = self.aps.get(bssid)
        return self.devices.display(bssid, ap.info["ssid"] if ap else bssid)

    def make_report(self, send):
        pres = self.presence.state
        r = build_summary(24, pres)
        r["sent"] = False
        if send:
            r["sent"] = self.alerter.send_phone(r["text"], wait=True)
            if not self.alerter.phone_configured:
                notify("Wi-Fi Sense daily summary",
                       f"{r['motion']} motion episodes, occupied {r['occupied_h']} h (open the dashboard for more)")
        self.last_report = dict(text=r["text"], generated=r["generated"], sent=r["sent"])
        return r

    def _maybe_daily_report(self):
        s = self.settings.snapshot()
        if not s["daily_report_enabled"]:
            return
        today = time.strftime("%Y-%m-%d")
        if not s.get("last_report_date"):            # first run: start the schedule tomorrow, don't send now
            self.settings.set_internal("last_report_date", today)
            return
        if s.get("last_report_date") == today or time.strftime("%H:%M") < s["daily_report_time"]:
            return
        self.settings.set_internal("last_report_date", today)
        try:
            self.make_report(send=True)
        except Exception as e:
            print(f"[!] daily report failed: {e}")
        try:
            rollup.backup()
        except Exception as e:
            print(f"[!] daily backup failed: {e}")

    # ---- main loop ----
    def run(self):
        self.refresh_db_stats()
        while True:
            t0 = time.time()
            try:
                self.step()
                self.publish()
            except Exception as e:
                print(f"[!] scan cycle failed: {type(e).__name__}: {e}")
            if time.time() - self.last_rollup > ROLLUP_EVERY:
                self.last_rollup = time.time()
                try:
                    rollup.rollup(self.conn)             # summarise hours before raw rows expire
                    self.unusual.refresh(self.conn)
                except Exception as e:
                    print(f"[!] rollup failed: {e}")
            if time.time() - self.last_cleanup > RETENTION_EVERY:
                self.last_cleanup = time.time()
                self.cleanup()
            if time.time() - self.last_report_check > 30:
                self.last_report_check = time.time()
                self._maybe_daily_report()
            time.sleep(max(0.0, SCAN_INTERVAL - (time.time() - t0)))

    def step(self):
        try:
            self.conn.ping()
        except Exception:                        # survive a MySQL restart
            try:
                self.conn = db_connect()
                print("[i] reconnected to MySQL")
            except Exception as e:
                print(f"[!] MySQL unreachable ({type(e).__name__}); retrying next scan")
        nets = scan_full()
        link = link_info()
        self.scan_no += 1
        now = time.time()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        warming = self.scan_no <= BASELINE_SCANS
        adaptive = self.settings.get("adaptive_thresholds")
        scale = self.settings.get("motion_threshold_scale")
        settled = min(1.0, max(0, self.scan_no - BASELINE_SCANS) / 40)

        # ---- live link (fast native samples, else one netsh sample per scan) ----
        link_bssid = link["bssid"] if link else None
        if link and link.get("pct") is not None:
            if link_bssid in nets:
                nets[link_bssid]["pct"] = link["pct"]    # scan-list value is stale
            self.link.add_slow(percent_to_dbm(link["pct"]))
        lw = self.link.window(LINK_WINDOW_S) if link else []
        link_dbm = lw[-1] if lw else None
        link_std = _std(lw) if len(lw) >= 3 else 0.0
        link_th = (_clamp(NOISE_MULT * self.link_noise + 0.5, *LINK_TH_RANGE) if adaptive else LINK_TH_DEFAULT) \
            * (self.devices.factor(link_bssid, "motion_factor") if link_bssid else 1.0) * scale
        link_ratio = link_std / link_th if link else 0.0
        if link_dbm is not None:
            self.link_baseline = link_dbm if self.link_baseline is None else self.link_baseline
        link_dev = (link_dbm - self.link_baseline) if link_dbm is not None else 0.0

        # ---- events from other threads (health monitor) ----
        while self.pending:
            k, sev, msg, title = self.pending.popleft()
            self.emit(k, sev, msg, title=title)

        # ---- connection events: disconnect / reconnect / roaming ----
        self.latest_link = dict(link, dbm=link_dbm) if link else None
        if link:
            prev = self.prev_link
            if self.link_miss >= LINK_MISS_SCANS and prev:
                self.emit("health", "good", f"Reconnected to {link.get('ssid')}", title="Wi-Fi reconnected")
            elif prev and prev.get("bssid") != link["bssid"] and prev.get("ssid") == link.get("ssid"):
                self.emit("health", "info", f"Roamed to another access point of {link.get('ssid')} "
                          f"({prev.get('bssid')} → {link['bssid']})", title="Wi-Fi roamed")
            self.prev_link, self.link_miss = link, 0
        else:
            self.link_miss += 1
            if self.link_miss == LINK_MISS_SCANS and self.prev_link:
                self.emit("health", "warning", f"Wi-Fi disconnected from {self.prev_link.get('ssid')}",
                          title="Wi-Fi disconnected")

        n_cal = sum(1 for b in nets if b in self.calibration)
        self.sys_conf, conf_level, conf_reasons = system_confidence(
            len(nets), bool(link), self.link.fast and bool(lw), settled, n_cal)

        # ---- per-router update + appear events ----
        for b, info in nets.items():
            ap = self.aps.get(b)
            if ap is None or ap.gone:
                returning = ap is not None
                ap = self.aps[b] = AP(b, info, self.scan_no)
                if not warming:
                    self._on_new_source(ap, returning, link)
            # security: auth type changed on a known BSSID
            old_auth = self.known_auth.get(b)
            if old_auth and info["auth"] and info["auth"] != old_auth and not warming \
                    and not (self.devices.get(b) or {}).get("trusted"):
                self.emit("security", "critical",
                          f"Security on {self.devices.display(b, info['ssid'])} changed: "
                          f"{old_auth} → {info['auth']}", ap, conf=0.8, title="Security changed")
            if info["auth"]:
                self.known_auth[b] = info["auth"]
            ap.info, ap.last_seen = info, self.scan_no
            self.devices.touch(b, info["ssid"], ap.vendor, device_type(info["ssid"], b))
            ap.wob.append(info["pct"])
            ap.spark.append(info["pct"])
            ap.baseline = info["pct"] if ap.baseline is None else ap.baseline

        # ---- sources that went quiet ----
        for ap in self.aps.values():
            if not ap.gone and self.scan_no - ap.last_seen >= LEFT_AFTER_SCANS:
                ap.gone = True
                dev = self.devices.get(ap.bssid) or {}
                if not dev.get("muted"):
                    self.emit("left", "neutral",
                              f"{self.devices.display(ap.bssid, ap.info['ssid'])} went out of range / switched off",
                              ap, conf=0.4 + 0.5 * min(1.0, ap.info["pct"] / 60), title="Device left")

        # ---- calibration session ----
        calib_view = None
        with self.lock:
            calib = self.calib
        if calib:
            ap = self.aps.get(calib["bssid"])
            if ap and ap.last_seen == self.scan_no:
                dbm = link_dbm if calib["bssid"] == link_bssid and link_dbm is not None \
                    else percent_to_dbm(ap.info["pct"])
                calib["samples"].append(dbm)
            if len(calib["samples"]) >= CALIB_SCANS:
                mp = round(statistics.median(calib["samples"]), 1)
                self._save_calibration(calib["bssid"], calib["ssid"], mp)
                with self.lock:
                    self.calib = None
            else:
                calib_view = dict(bssid=calib["bssid"], ssid=calib["ssid"],
                                  n=len(calib["samples"]), needed=CALIB_SCANS)

        # ---- per-path detection ----
        rows = []
        for ap in self.aps.values():
            if ap.gone or ap.last_seen != self.scan_no:
                continue
            pct = ap.info["pct"]
            connected = ap.bssid == link_bssid
            wobble = _std(ap.wob) if len(ap.wob) >= 3 else 0.0
            mf = self.devices.factor(ap.bssid, "motion_factor")
            df = self.devices.factor(ap.bssid, "dev_factor")
            th = ap.threshold(adaptive, mf * scale)
            ratio = wobble / th
            unit = "%"
            if connected and lw:                 # the live link is the better sensor
                wobble, th, ratio, unit = link_std, link_th, link_ratio, "dB"
            dev = pct - ap.baseline
            prev = ap.state

            if warming:
                state = "calibrating"
            elif ratio >= 1.0:
                state = "moving"
            elif dev <= -BLOCK_DROP_PCT * df:
                ap.off_scans += 1
                state = "blocked" if ap.off_scans >= BLOCK_SCANS else "stable"
            elif abs(dev) >= STILL_DEV_PCT * df:
                ap.off_scans += 1
                state = "still" if ap.off_scans >= STILL_SCANS else "stable"
            else:
                ap.off_scans = 0
                state = "stable"
            ap.state = state

            if state != prev:
                if state == "blocked":
                    self.emit("blocked", "warning",
                              f"Signal to {self.devices.display(ap.bssid, ap.info['ssid'])} dropped {-dev:.0f}% below normal: "
                              f"something may be blocking that path",
                              ap, conf=0.45 + 0.4 * min(1.0, (-dev - BLOCK_DROP_PCT) / 15),
                              title="Path blocked")
                elif state == "still":
                    self.emit("still", "warning",
                              f"Steady change on path to {self.devices.display(ap.bssid, ap.info['ssid'])} ({dev:+.0f}%): "
                              f"possible stationary person/object",
                              ap, conf=0.3 + 0.3 * min(1.0, (abs(dev) - STILL_DEV_PCT) / 8),
                              title="Possible still presence")

            # learn: baseline fast while steady, slowly otherwise; noise only while steady
            alpha = 0.3 if warming else (0.08 if abs(dev) < 5 and state == "stable" else 0.01)
            ap.baseline += alpha * (pct - ap.baseline)
            if state in ("stable", "calibrating") and len(ap.wob) >= 3:
                ap.noise += (0.2 if warming else 0.02) * (_std(ap.wob) - ap.noise)

            # distance (connected link uses the precise native dBm)
            dbm = link_dbm if connected and link_dbm is not None else percent_to_dbm(pct)
            calibrated = ap.bssid in self.calibration
            mp = self.calibration.get(ap.bssid, MEASURED_POWER_DBM)
            wobble_db = wobble if unit == "dB" else wobble / 2.0      # 1% ≈ 0.5 dB
            dist, lo, hi, acc = distance_range(dbm, wobble_db, mp, calibrated)
            rows.append(dict(
                bssid=ap.bssid, ssid=ap.info["ssid"], pct=pct, dbm=round(dbm, 1),
                dist=dist, dist_lo=lo, dist_hi=hi, acc=acc, calibrated=calibrated,
                measured_power=mp, wobble=round(wobble, 2), th=round(th, 2),
                ratio=round(ratio, 2), unit=unit,
                baseline=round(ap.baseline, 1), dev=round(dev, 1), state=state,
                intensity=("strong" if ratio >= 2 else "light") if state == "moving" else None,
                spark=list(ap.spark), auth=ap.info["auth"], radio=ap.info["radio"],
                band=ap.info["band"], channel=ap.info["channel"],
                type=device_type(ap.info["ssid"], ap.bssid), vendor=ap.vendor,
                connected=connected, new=not warming and self.scan_no - ap.first_seen < 40,
                name=(self.devices.get(ap.bssid) or {}).get("name"),
                known=self.devices.is_known(ap.bssid),
                trusted=bool((self.devices.get(ap.bssid) or {}).get("trusted")),
                muted=bool((self.devices.get(ap.bssid) or {}).get("muted")),
                first_seen=(self.devices.get(ap.bssid) or {}).get("first_seen"),
                tuned=dict(motion=round(mf, 2), dev=round(df, 2)),
            ))

        # link noise + baseline learning
        if link and lw and link_ratio < 1.0:
            self.link_noise += (0.2 if warming else 0.02) * (link_std - self.link_noise)
            if link_dbm is not None:
                self.link_baseline += (0.3 if warming else 0.05) * (link_dbm - self.link_baseline)

        # ---- global motion episode ----
        moving = [r for r in rows if r["state"] == "moving"]
        idx_all = max((r["ratio"] for r in rows), default=0.0)
        motion = bool(moving)
        self.activity.append(dict(t=ts[11:], v=round(idx_all, 2), link=round(link_ratio, 2)))
        self.recent.append((now, round(idx_all, 3), round(link_ratio, 3),
                            {r["bssid"]: (r["ratio"], r["state"]) for r in rows}))
        self.cur_thresholds = {r["bssid"]: r["th"] for r in rows if r["unit"] == "%"}
        if not warming:
            self.occupancy.append(1 if motion else 0)

        if motion:
            self.last_motion = now
            if not self.motion_on:
                self.motion_on, self.motion_start = True, now
                strong = idx_all >= 2
                names = ", ".join(r["name"] or r["ssid"] for r in moving[:3])
                # stronger margin over threshold + more paths agreeing = more confident
                conf = (0.35 + 0.35 * min(1.0, idx_all - 1)
                        + 0.15 * min(1.0, (len(moving) - 1) / 2)
                        + 0.15 * (link_ratio >= 1.0))
                self.emit("motion", "critical" if strong else "serious",
                          f"{'Strong' if strong else 'Light'} movement on {len(moving)} "
                          f"signal path{'s' if len(moving) > 1 else ''} ({names})",
                          conf=conf, title=f"{'Strong' if strong else 'Light'} movement detected",
                          paths=[r["bssid"] for r in moving])
                self.db_stats["today"] += 1
                self.db_stats["hourly"][time.localtime().tm_hour] += 1
                odd = self.unusual.check() if self.settings.get("unusual_alerts") else None
                if odd:
                    self.emit("unusual", "serious", odd[0], conf=odd[1], title="Unusual activity",
                              paths=[r["bssid"] for r in moving])
        elif self.motion_on and now - (self.last_motion or now) > self.settings.get("episode_gap_s"):
            # bursts separated by short pauses are one episode, not dozens
            self.motion_on = False
            self.emit("calm", "good",
                      f"Movement stopped after {self.last_motion - self.motion_start + SCAN_INTERVAL:.0f} s",
                      title="Movement stopped")

        # ---- Bluetooth presence ----
        ble = self.ble.snapshot()
        ble_jump = False
        if ble["running"]:
            self.ble_near_hist.append(ble["near"])
            low = min(self.ble_near_hist)
            if (not warming and ble["near"] - low >= BLE_JUMP
                    and now - self.last_ble_event > 60 and len(self.ble_near_hist) > 20):
                self.last_ble_event = now
                ble_jump = True
                self.emit("ble", "info",
                          f"{ble['near'] - low} more Bluetooth devices came close "
                          f"({ble['near']} nearby now)", conf=0.5, title="Bluetooth devices nearby")

        # ---- movement rhythm + link-rate features ----
        rhy = rhythm(self.link.window(RHYTHM_WINDOW_S)) if link else dict(ok=False, reason="not connected")
        if link and link.get("rx"):
            self.rx_hist.append(link["rx"])
        rate_inst, rate_drop = rate_features(list(self.rx_hist))

        # ---- activity classifier ----
        feats = self._features(rows, idx_all, link_ratio, link_dev, ble, rhy, rate_inst, rate_drop)
        with self.lock:
            label = self.labeling
        if label and not warming:
            try:
                with self.conn.cursor() as cur:
                    cur.execute("INSERT INTO training_samples (ts,label,features) VALUES (%s,%s,%s)",
                                (ts, label, json.dumps(feats)))
                self.conn.commit()
                self.labeled_now += 1
                tc = self.db_stats["train_counts"]
                tc[label] = tc.get(label, 0) + 1
            except Exception as e:
                print(f"[!] could not save training sample: {e}")
        ml = None
        pred = self.model.predict(feats) if not warming else None
        if pred:
            self.ml_probs.append(pred[2])
            avg = {l: sum(p[l] for p in self.ml_probs) / len(self.ml_probs) for l in pred[2]}
            best = max(avg, key=avg.get)
            ml = dict(label=best, prob=round(avg[best], 3),
                      probs={l: round(v, 3) for l, v in sorted(avg.items(), key=lambda kv: -kv[1])})

        # ---- presence fusion: occupied / empty ----
        hold_s = self.settings.get("presence_hold_min") * 60
        if not warming:
            n_still = sum(r["state"] in ("still", "blocked") for r in rows)
            change = self.presence.update(now, hold_s, motion, idx_all, len(moving), n_still, ble_jump, ml)
            if change:
                new_state, p, prev_dur = change
                if new_state == "occupied":
                    self.emit("occupied", "info",
                              f"Room occupied ({self.presence.last_reason}); it was empty for {prev_dur / 60:.0f} min",
                              conf=p, title="Room occupied")
                else:
                    self.emit("empty", "good",
                              f"Room looks empty; it was occupied for {prev_dur / 60:.0f} min",
                              conf=1 - p, title="Room empty")
        if self.scan_no % DB_STATS_EVERY == 0:
            try:
                self.devices.flush(self.conn)
            except Exception as e:
                print(f"[!] device save failed: {e}")

        # ---- persistence (existing tables) ----
        try:
            log_scan(self.conn, ts, {b: (i["ssid"], i["pct"]) for b, i in nets.items()})
            if motion:
                top = max(moving, key=lambda r: r["ratio"])
                log_motion(self.conn, ts, top["wobble"], top["bssid"], len(nets))
        except Exception as e:
            print(f"[!] DB write failed: {e}")
        if self.scan_no % DB_STATS_EVERY == 0:
            self.refresh_db_stats()

        # ---- channel congestion ----
        channels = {}
        for r in rows:
            if r["channel"]:
                channels.setdefault(str(r["channel"]), []).append(dict(ssid=r["ssid"], pct=r["pct"]))
        congestion = None
        if link and link.get("channel"):
            ch = link["channel"]
            if ch <= 14:   # 2.4 GHz channels overlap within +/-4
                near = [r for r in rows if r["channel"] and r["channel"] <= 14
                        and abs(r["channel"] - ch) <= 4 and not r["connected"]]
            else:
                near = [r for r in rows if r["channel"] == ch and not r["connected"]]
            cscore = sum(r["pct"] for r in near) / 100
            congestion = dict(channel=ch, overlapping=len(near), score=round(cscore, 2),
                              level="busy" if cscore >= 2 else "moderate" if cscore >= 0.8 else "clear",
                              best=self._best_channel(rows, link_bssid) if ch <= 14 else None)

        occ = (sum(self.occupancy) / len(self.occupancy) * 100) if self.occupancy else 0.0
        score = min(100.0, idx_all / 3 * 100)
        live = self.link.window(15) if link else []

        nodes_view = self._process_nodes(now)      # takes self.lock itself, and may emit
        with self.lock:
            self.state = dict(
                ts=ts, now=now, started=self.started, scan_count=self.scan_no,
                warming=warming, baseline_scans=BASELINE_SCANS,
                motion=motion, interval=SCAN_INTERVAL, adaptive=adaptive,
                score=round(score, 1),
                confidence=dict(score=self.sys_conf, level=conf_level, reasons=conf_reasons),
                level="strong" if idx_all >= 2 else "light" if motion else "calm",
                occupancy=round(occ, 1), last_motion=self.last_motion,
                aps=sorted(rows, key=lambda r: r["bssid"]),   # stable order -> stable radar angles
                link=dict(link, dbm=link_dbm, wobble=round(link_std, 2), th=round(link_th, 2),
                          ratio=round(link_ratio, 2), fast=self.link.fast,
                          live=live[::2][-75:]) if link else None,
                activity=list(self.activity), feed=list(self.feed),
                today=self.db_stats["today"], hourly=self.db_stats["hourly"],
                hour_now=time.localtime().tm_hour,
                channels=channels, congestion=congestion, db=DB_NAME,
                ble=ble, calib=calib_view,
                ml=dict(ready=self.model.ready, result=ml, labels=self.model.labels,
                        trained_at=self.model.trained_at, labeling=self.labeling,
                        labeled_now=self.labeled_now, counts=self.db_stats["train_counts"]),
                settings=self.settings.snapshot(), alerts=self.alerter.status(),
                oui=oui.available(),
                presence=self.presence.view(now, hold_s) if not warming else None,
                tuning=self.devices.tuning_summary(), report=self.last_report,
                devices_total=len(self.devices.devs), recent_minutes=round(len(self.recent) * SCAN_INTERVAL / 60),
                rhythm=rhy, rate=dict(instability=rate_inst, drop=rate_drop, rx=list(self.rx_hist)),
                unusual=self.unusual.slot_view(),
                nodes=nodes_view,
                health=self.health.latest if self.health else None,
            )

    # ---- internals ----
    def _on_new_source(self, ap, returning, link):
        info, b = ap.info, ap.bssid
        kind = device_type(info["ssid"], b)
        what = {"phone": "Phone hotspot", "direct": "Wi-Fi Direct device",
                "virtual": "Hotspot / virtual AP", "hidden": "Hidden network"}.get(kind, "Router")
        vend = f" [{ap.vendor}]" if ap.vendor and ap.vendor != "Randomized MAC" else ""
        open_net = not info["auth"] or "open" in info["auth"].lower()
        dev = self.devices.get(b) or {}
        if dev.get("muted"):
            pass
        elif dev.get("name") or dev.get("trusted"):
            self.emit("arrived", "info",
                      f"{dev.get('name') or info['ssid']} {'is back' if returning else 'arrived'}",
                      ap, conf=0.9, title="Known device arrived")
        else:
            self.emit("new_device", "info",
                      f"Unknown {what.lower()}{vend} {'back in range' if returning else 'appeared'}: "
                      f"{info['ssid']}{' (OPEN, unencrypted)' if open_net else ''}",
                      ap, conf=0.9, title="Unknown Wi-Fi device")
        # evil twin: your network's name broadcast by different hardware
        if link and info["ssid"] == link.get("ssid") and b != link["bssid"] and not dev.get("trusted") \
                and not same_hardware_family(b, link["bssid"]):
            mismatch = info["auth"] and link.get("authentication") and info["auth"] != link["authentication"]
            self.emit("security", "critical" if mismatch or open_net else "serious",
                      f"Another device is broadcasting your network name '{info['ssid']}'"
                      f"{' with different security (' + str(info['auth']) + ')' if mismatch else ''}"
                      f" from {ap.vendor or 'unknown hardware'}. Could be a mesh node or an evil twin.",
                      ap, conf=0.7 if mismatch or open_net else 0.45, title="Possible evil twin")

    def _save_calibration(self, bssid, ssid, mp):
        try:
            with self.conn.cursor() as cur:
                cur.execute("REPLACE INTO calibration (bssid, ssid, measured_power, updated) "
                            "VALUES (%s,%s,%s,NOW())", (bssid, ssid[:64], mp))
            self.conn.commit()
        except Exception as e:
            print(f"[!] could not save calibration: {e}")
        with self.lock:
            self.calibration[bssid] = mp
        self.emit("calibrated", "good", f"Calibrated {ssid}: {mp} dBm at 1 m", self.aps.get(bssid),
                  title="Router calibrated")

    @staticmethod
    def _features(rows, idx_all, link_ratio, link_dev, ble, rhy, rate_inst, rate_drop):
        n = max(1, len(rows))
        ok = rhy.get("ok")
        return [
            round(idx_all, 3),
            round(sum(r["ratio"] for r in rows) / n, 3),
            round(link_ratio, 3),
            round(sum(r["state"] == "moving" for r in rows) / n, 3),
            round(sum(abs(r["dev"]) >= STILL_DEV_PCT for r in rows) / n, 3),
            round(sum(abs(r["dev"]) for r in rows) / n / 10, 3),
            round(abs(link_dev), 3),
            float(ble["near"]) if ble["running"] else 0.0,
            rhy["bands"]["gait"] if ok else 0.0,
            rhy["std_db"] if ok else 0.0,
            rhy["dom_hz"] if ok and rhy["std_db"] >= 0.8 else 0.0,
            rate_inst,
            rate_drop,
        ]

    @staticmethod
    def _best_channel(rows, link_bssid):
        """Least-interfered of 1/6/11 (the non-overlapping 2.4 GHz channels)."""
        def load(ch):
            return sum(r["pct"] / 100 * max(0, 1 - abs(r["channel"] - ch) / 5)
                       for r in rows if r["channel"] and r["channel"] <= 14 and r["bssid"] != link_bssid)
        return min((1, 6, 11), key=load)

    def publish(self):
        with self.cond:
            self.cond.notify_all()

    def names(self):
        return {b: (d.get("name") or d.get("ssid")) for b, d in self.devices.all().items()}

    def snapshot(self):
        with self.lock:
            return json.dumps(self.state, default=str)


# ------------------------- History / export (own DB connection) -------------------------
def history_data(days, kind):
    days = _clamp(days, 1, 90)
    c = db_connect()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT DATE(ts), HOUR(ts), COUNT(*) FROM detections WHERE kind='motion' "
                        "AND ts >= CURDATE() - INTERVAL %s DAY GROUP BY DATE(ts), HOUR(ts)", (days - 1,))
            heat = [dict(d=str(d), h=h, n=n) for d, h, n in cur.fetchall()]
            cur.execute("SELECT kind, COUNT(*) FROM detections WHERE ts >= CURDATE() - INTERVAL %s DAY "
                        "GROUP BY kind", (days - 1,))
            kinds = dict(cur.fetchall())
            q = ("SELECT ts, kind, severity, ssid, detail, confidence FROM detections "
                 "WHERE ts >= CURDATE() - INTERVAL %s DAY")
            args = [days - 1]
            if kind:
                q += " AND kind=%s"
                args.append(kind)
            cur.execute(q + " ORDER BY id DESC LIMIT 500", args)
            dets = [dict(ts=str(t), kind=k, severity=s, ssid=ss, msg=m, conf=cf)
                    for t, k, s, ss, m, cf in cur.fetchall()]
            cur.execute("SELECT COUNT(*), MIN(ts) FROM scans")
            n_scans, first = cur.fetchone()
        return dict(days=days, heat=heat, kinds=kinds, detections=dets,
                    scans_rows=n_scans, scans_since=str(first) if first else None)
    finally:
        c.close()


def export_csv(what, qs):
    c = db_connect()
    try:
        buf = io.StringIO()
        w = csv.writer(buf)
        with c.cursor() as cur:
            if what == "detections":
                days = _clamp(int(qs.get("days", ["30"])[0]), 1, 365)
                cur.execute("SELECT ts, kind, severity, bssid, ssid, detail, confidence FROM detections "
                            "WHERE ts >= NOW() - INTERVAL %s DAY ORDER BY id", (days,))
                w.writerow(["ts", "kind", "severity", "bssid", "ssid", "detail", "confidence"])
            else:
                hours = _clamp(int(qs.get("hours", ["24"])[0]), 1, 168)
                cur.execute("SELECT ts, bssid, ssid, signal_pct, est_dist_m FROM scans "
                            "WHERE ts >= NOW() - INTERVAL %s HOUR ORDER BY id", (hours,))
                w.writerow(["ts", "bssid", "ssid", "signal_pct", "est_dist_m"])
            for row in cur:
                w.writerow(row)
        return buf.getvalue().encode("utf-8-sig")
    finally:
        c.close()


def train_model(sensor):
    c = db_connect()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT label, features FROM training_samples")
            samples = [(l, json.loads(f)) for l, f in cur.fetchall()]
    finally:
        c.close()
    samples = [(l, x) for l, x in samples if len(x) == len(FEATURES)]
    return sensor.model.train(samples, FEATURES, time.strftime("%Y-%m-%d %H:%M"))


def reset_training(sensor, delete_samples):
    sensor.model.reset()
    if delete_samples:
        c = db_connect()
        try:
            with c.cursor() as cur:
                cur.execute("DELETE FROM training_samples")
            c.commit()
        finally:
            c.close()
        sensor.db_stats["train_counts"] = {}


# ------------------------- Floor plan storage -------------------------
def floorplan_load():
    try:
        d = json.loads(FLOORPLAN_JSON.read_text())
    except Exception:
        d = {}
    return dict(items=d.get("items", {}), width_m=d.get("width_m", 10), image=d.get("image"))


def floorplan_save(d):
    FLOORPLAN_JSON.write_text(json.dumps(d, indent=1))


def floorplan_set_layout(body):
    d = floorplan_load()
    items = {}
    for k, v in (body.get("items") or {}).items():
        if len(items) >= 100:
            break
        items[str(k)[:64]] = dict(x=_clamp(float(v["x"]), 0, 1), y=_clamp(float(v["y"]), 0, 1),
                                  label=str(v.get("label") or "")[:64])
    d.update(items=items, width_m=_clamp(float(body.get("width_m") or 10), 2, 200))
    floorplan_save(d)
    return d


def floorplan_set_image(data_url):
    head, _, b64 = str(data_url).partition(",")
    raw = base64.b64decode(b64, validate=True)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("image larger than 4 MB")
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif raw[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    else:
        raise ValueError("only PNG or JPEG images")
    for old in APP_DIR.glob("floorplan_image.*"):
        old.unlink()
    (APP_DIR / f"floorplan_image.{ext}").write_bytes(raw)
    d = floorplan_load()
    d["image"] = f"floorplan_image.{ext}#{int(time.time())}"
    floorplan_save(d)
    return d["image"]


def floorplan_clear_image():
    for old in APP_DIR.glob("floorplan_image.*"):
        old.unlink()
    d = floorplan_load()
    d["image"] = None
    floorplan_save(d)


def clean_node_report(b):
    name = re.sub(r"[^\w .-]", "", str(b.get("name") or ""))[:32].strip() or "node"
    paths = []
    for p in (b.get("paths") or [])[:64]:
        bssid = str(p.get("bssid", "")).lower()
        if re.fullmatch(r"[0-9a-f:]{17}", bssid):
            paths.append(dict(bssid=bssid, ssid=str(p.get("ssid") or "")[:64], pct=int(p.get("pct") or 0),
                              ratio=float(p.get("ratio") or 0),
                              state=p.get("state") if p.get("state") in ("moving", "stable", "calibrating") else "stable"))
    return dict(name=name, ts=str(b.get("ts") or "")[:19], warming=bool(b.get("warming")),
                motion=bool(b.get("motion")), idx=float(b.get("idx") or 0), paths=paths)


LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wi-Fi Sense</title><body style="font:15px system-ui;background:#0f0f0e;color:#fff;display:grid;place-items:center;height:100vh;margin:0">
<form method="get" style="background:#1a1a19;border:1px solid #2d2d2a;border-radius:14px;padding:24px;width:min(340px,90vw)">
<h2 style="margin:0 0 6px">Wi-Fi Sense</h2><p style="color:#8d8c85;margin:0 0 14px">Enter the access token from the hub's .env</p>
<input name="token" type="password" autofocus style="width:100%;height:38px;border-radius:8px;border:1px solid #2d2d2a;background:#0f0f0e;color:#fff;padding:0 10px">
<button style="margin-top:12px;width:100%;height:38px;border:0;border-radius:8px;background:#3987e5;color:#fff;font-weight:600">Open dashboard</button>
</form></body>"""


# ------------------------- HTTP -------------------------
class Server(ThreadingHTTPServer):
    """Refuses to share the port: on Windows the stdlib default (SO_REUSEADDR)
    lets a second copy bind the same port and silently run a duplicate sensor."""
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 64          # default 5 drops bursts (page load + live stream + app pre-cache)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_handler(sensor):
    allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, default=str).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _stream(self):
            """Server-Sent Events: push the full state after every scan (heartbeat every 15 s)."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = None
            try:
                while True:
                    snap = sensor.snapshot()
                    if snap != last and snap != "{}":
                        self.wfile.write(b"data: " + snap.encode() + b"\n\n")
                        last = snap
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    with sensor.cond:
                        sensor.cond.wait(timeout=15)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return                                   # browser closed the page

        def _token_from_request(self):
            tok = self.headers.get("X-Access-Token", "")
            if not tok:
                for part in self.headers.get("Cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "ws_token":
                        tok = v
            return tok

        def _host_ok(self):
            """Local browser on 127.0.0.1/localhost: allowed (Host check blocks DNS rebinding).
            Anything else (phone, node, Tailscale/HTTPS proxy): needs the access token."""
            host = self.headers.get("Host", "")
            if self.client_address[0] in LOOPBACK and host in allowed_hosts:
                return True
            tok = self._token_from_request()
            return bool(ACCESS_TOKEN and tok and hmac.compare_digest(tok.encode(), ACCESS_TOKEN.encode()))

        def _login_redirect(self, u, qs):
            """?token=... on a page: set an HttpOnly cookie and redirect to the clean URL."""
            tok = qs.get("token", [""])[0]
            if ACCESS_TOKEN and tok and hmac.compare_digest(tok.encode(), ACCESS_TOKEN.encode()):
                self.send_response(302)
                self.send_header("Set-Cookie", f"ws_token={ACCESS_TOKEN}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
                self.send_header("Location", u.path or "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            return False

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if not self._host_ok():
                if "token" in qs and self._login_redirect(u, qs):
                    return
                if u.path in PAGES and ACCESS_TOKEN:
                    return self._send(401, LOGIN_PAGE, "text/html; charset=utf-8")
                return self._send(403, {"error": "forbidden"})
            try:
                if u.path == "/api/state":
                    return self._send(200, sensor.snapshot())
                if u.path == "/api/summary":        # compact, for Home Assistant etc.
                    s = json.loads(sensor.snapshot() or "{}")
                    ml = (s.get("ml") or {}).get("result") or {}
                    return self._send(200, dict(
                        motion=s.get("motion"), level=s.get("level"), score=s.get("score"),
                        occupancy=s.get("occupancy"), confidence=(s.get("confidence") or {}).get("score"),
                        activity=ml.get("label"), activity_prob=ml.get("prob"),
                        ble_near=(s.get("ble") or {}).get("near"), sources=len(s.get("aps") or []),
                        away_mode=(s.get("settings") or {}).get("away_mode"),
                        occupied=((s.get("presence") or {}).get("state") == "occupied"),
                        occupied_prob=(s.get("presence") or {}).get("p"), ts=s.get("ts")))
                if u.path == "/api/history":
                    return self._send(200, history_data(int(qs.get("days", ["14"])[0]),
                                                        qs.get("kind", [""])[0] or None))
                if u.path in ("/api/export/detections.csv", "/api/export/scans.csv"):
                    what = "detections" if "detections" in u.path else "scans"
                    return self._send(200, export_csv(what, qs), "text/csv; charset=utf-8",
                                      {"Content-Disposition":
                                       f'attachment; filename="wifi_sense_{what}_{time.strftime("%Y%m%d")}.csv"'})
                if u.path == "/api/devices":
                    grid = presence_grid(7)
                    present = {r["bssid"] for r in json.loads(sensor.snapshot() or "{}").get("aps", [])}
                    devs = [dict(d, bssid=b, present=b in present, grid=grid.get(b))
                            for b, d in sensor.devices.all().items()]
                    devs.sort(key=lambda d: d["last_seen"] or "", reverse=True)
                    devs.sort(key=lambda d: not d["present"])      # stable: present first, then most recent
                    return self._send(200, dict(devices=devs, tuning=sensor.devices.tuning))
                if u.path == "/api/stream":
                    return self._stream()
                if u.path == "/api/snooze":
                    return self._send(200, {"snooze": sensor.alerter.snooze_state()})
                if u.path == "/api/why":
                    return self._send(200, insights.why(int(qs["id"][0]), list(sensor.recent), sensor.names(),
                                                        dict(sensor.cur_thresholds)))
                if u.path == "/api/day":
                    return self._send(200, insights.day(qs.get("date", [time.strftime("%Y-%m-%d")])[0]))
                if u.path == "/api/at":
                    return self._send(200, insights.at(float(qs["t"][0]), dict(sensor.calibration), sensor.names(),
                                                       dict(sensor.cur_thresholds), distance_range, device_type))
                if u.path == "/api/trail":
                    return self._send(200, {"trail": insights.trail(list(sensor.recent),
                                                                    _clamp(int(qs.get("minutes", ["10"])[0]), 1, 60))})
                if u.path == "/favicon.ico" or u.path.startswith("/brand/"):
                    name = "favicon.ico" if u.path == "/favicon.ico" else u.path[len("/brand/"):]
                    if name not in BRAND_FILES:
                        return self.send_error(404)
                    return self._send(200, resource("brand/" + name).read_bytes(), BRAND_FILES[name],
                                      {"Cache-Control": "max-age=86400"})
                if u.path in STATIC:
                    fname, ctype = STATIC[u.path]
                    return self._send(200, resource(fname).read_bytes(), ctype)
                if u.path == "/floorplan-image":
                    img = next(iter(APP_DIR.glob("floorplan_image.*")), None)
                    if not img:
                        return self.send_error(404)
                    return self._send(200, img.read_bytes(), "image/png" if img.suffix == ".png" else "image/jpeg")
                if u.path == "/api/health":
                    return self._send(200, health.health_data(int(qs.get("hours", ["24"])[0])))
                if u.path == "/api/replay":
                    return self._send(200, replay.run(int(qs.get("hours", ["24"])[0]),
                                                      int(qs.get("gap", ["30"])[0]),
                                                      _clamp(float(qs.get("scale", ["1"])[0]), 0.3, 4.0),
                                                      sensor.settings.get("adaptive_thresholds")))
                if u.path == "/api/rules":
                    return self._send(200, {"rules": sensor.rules.list()})
                if u.path == "/api/floorplan":
                    return self._send(200, floorplan_load())
                if u.path == "/api/backups":
                    return self._send(200, {"backups": rollup.list_backups()})
                if u.path == "/api/longterm":
                    c = db_connect()
                    try:
                        return self._send(200, {"days": rollup.daily_series(c, int(qs.get("days", ["90"])[0]))})
                    finally:
                        c.close()
                if u.path == "/api/report":
                    return self._send(200, sensor.make_report(send=False))
                if u.path in PAGES:
                    return self._send(200, resource(PAGES[u.path]).read_bytes(), "text/html; charset=utf-8")
                self.send_error(404)
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            # JSON-only: browsers can't send this cross-site without a CORS preflight we never allow
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._send(415, {"error": "JSON required"})
            n = int(self.headers.get("Content-Length") or 0)
            p0 = urlparse(self.path).path
            limit = 6_000_000 if p0 == "/api/floorplan/image" else 64_000 if p0 == "/api/node/report" else 10_000
            if n > limit:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                p = urlparse(self.path).path
                if p == "/api/calibrate":
                    if body.get("clear"):
                        sensor.clear_calibration(body["clear"])
                        return self._send(200, {"ok": True})
                    ok = sensor.start_calibration(body.get("bssid"))
                    return self._send(200 if ok else 404, {"ok": ok})
                if p == "/api/label":
                    sensor.set_label(body.get("label"))
                    return self._send(200, {"ok": True})
                if p == "/api/train":
                    ok, msg = train_model(sensor)
                    return self._send(200, {"ok": ok, "message": msg})
                if p == "/api/train/reset":
                    reset_training(sensor, bool(body.get("delete_samples")))
                    return self._send(200, {"ok": True})
                if p == "/api/settings":
                    return self._send(200, sensor.settings.update(body))
                if p == "/api/test-alert":
                    notify("Wi-Fi Sense test", "Desktop notifications work.")
                    ok = sensor.alerter.send_phone("Test alert: phone notifications work.", wait=True)
                    return self._send(200, {"ok": True, "phone": ok, "error": sensor.alerter.last_error})
                if p == "/api/feedback":
                    return self._send(200, {"ok": True, "message":
                                            sensor.feedback(int(body["id"]), body.get("verdict"))})
                if p == "/api/device":
                    d = sensor.devices.update(str(body["bssid"]).lower(), name=body.get("name"),
                                              trusted=body.get("trusted"), muted=body.get("muted"))
                    return self._send(200 if d else 404, {"ok": bool(d), "device": d})
                if p == "/api/tuning/reset":
                    sensor.devices.reset_tuning()
                    return self._send(200, {"ok": True})
                if p == "/api/report/send":
                    r = sensor.make_report(send=True)
                    return self._send(200, dict(r, error=sensor.alerter.last_error))
                if p == "/api/snooze":
                    return self._send(200, {"snooze": sensor.alerter.set_snooze(str(body.get("group")),
                                                                                int(body.get("minutes") or 0))})
                if p == "/api/node/report":
                    sensor.node_report(clean_node_report(body), self.client_address[0])
                    return self._send(200, {"ok": True})
                if p == "/api/rules":
                    sensor.rules.save(body)
                    return self._send(200, {"ok": True})
                if p == "/api/rules/delete":
                    sensor.rules.delete(body["id"])
                    return self._send(200, {"ok": True})
                if p == "/api/rules/test":
                    return self._send(200, sensor.rules.test(body["id"]))
                if p == "/api/floorplan":
                    return self._send(200, floorplan_set_layout(body))
                if p == "/api/floorplan/image":
                    return self._send(200, {"ok": True, "image": floorplan_set_image(body.get("data", ""))})
                if p == "/api/floorplan/image/clear":
                    floorplan_clear_image()
                    return self._send(200, {"ok": True})
                if p == "/api/backup":
                    return self._send(200, dict(rollup.backup(bool(body.get("full"))), ok=True))
                if p == "/api/cleanup":
                    sensor.last_cleanup = 0          # run on next scan cycle
                    return self._send(200, {"ok": True})
                self.send_error(404)
            except (ValueError, KeyError) as e:
                self._send(400, {"error": f"bad request: {e}"})
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, *args):   # keep the console quiet
            pass
    return Handler


def main():
    print("Connecting to MySQL (root@localhost:3306)...", flush=True)
    try:
        conn = db_connect()
        db_prepare(conn)
    except Exception as e:
        print(f"[!] Could not connect to MySQL: {e}\n"
              f"    Start XAMPP's MySQL, and check WIFI_SENSE_DB_PASSWORD in .env\n"
              f"    (copy .env.example to .env if you don't have one).")
        return
    if HOST not in LOOPBACK and len(ACCESS_TOKEN) < 16:
        print("[!] WIFI_SENSE_BIND makes the dashboard reachable from other devices, so a\n"
              "    WIFI_SENSE_ACCESS_TOKEN of at least 16 characters is required in .env. Not starting.")
        conn.close()
        return
    url = f"http://{'127.0.0.1' if HOST in ('0.0.0.0', '::') else HOST}:{PORT}"
    try:
        server = Server((HOST, PORT), None)          # claim the port first
    except OSError:
        print(f"[!] Port {PORT} is already in use: Wi-Fi Sense is probably already running.\n"
              f"    Open {url} or stop the other copy (or set WIFI_SENSE_PORT in .env).")
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        conn.close()
        return
    settings = Settings()
    alerter = Alerter(settings, notify)
    link = LinkSampler(); link.start()
    ble = BLEPresence(); ble.start()
    sensor = Sensor(conn, settings, alerter, link, ble)
    sensor.rules = RuleEngine(settings, alerter)
    sensor.health = health.HealthMonitor(settings, lambda: sensor.latest_link, sensor.post_event)
    sensor.start()
    sensor.health.start()

    print(f"    live link sampling : {'native API, %d/s' % LINK_HZ if link.fast else 'netsh fallback'}")
    print(f"    MAC vendor lookup  : {'on' if oui.available() else 'off (run: python oui.py --update)'}")
    print(f"    Bluetooth          : {'on' if ble.available else 'off (pip install bleak)'}")
    print(f"    phone alerts       : {'Telegram configured' if alerter.phone_configured else 'off'}")
    if HOST not in LOOPBACK:
        try:
            s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s_.connect(("8.8.8.8", 80))
            lan = s_.getsockname()[0]; s_.close()
        except OSError:
            lan = "<this-pc-ip>"
        print(f"    network access     : ON  -> http://{lan}:{PORT}/?token=<your token>  (phone / nodes)")

    server.RequestHandlerClass = make_handler(sensor)
    print(f"OK. Logging to '{DB_NAME}'. Dashboard at {url}  (Ctrl+C to stop)", flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        conn.close()
        print("Stopped. Nothing was ever transmitted.")


if __name__ == "__main__":
    main()
