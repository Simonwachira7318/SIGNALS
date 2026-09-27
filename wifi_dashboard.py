#!/usr/bin/env python3
"""
wifi_dashboard.py  -  Passive Wi-Fi sensing dashboard for Windows.

FEATURES
--------
  * Rolling "activity level" graph (motion energy over time).
  * Radar view of nearby access points at ESTIMATED distance + coverage rings.
  * Desktop notification when motion is detected.
  * Every scan logged to a local MySQL/MariaDB database (XAMPP: root@localhost:3306).

HONEST LIMITS (read me)
-----------------------
  * Fully PASSIVE / receive-only. It never transmits and never jams anything.
  * Distance to each access point is ESTIMATED from signal strength via the
    log-distance path-loss model. Expect several metres of error; walls, bodies
    and antenna differences all skew it.
  * DIRECTION to an access point CANNOT be measured with a single laptop antenna.
    On the radar, the *distance* (radius) is estimated, but the *angle* is
    arbitrary -- APs are just spread evenly so they don't overlap. Do not read
    bearing into it.
  * It senses MOVEMENT in the RF environment, not a headcount, and it maps fixed
    access points -- not people.

REQUIREMENTS
------------
    pip install pymysql matplotlib plyer
    A running MySQL/MariaDB (XAMPP) on localhost:3306, user 'root'.

USAGE
-----
    python wifi_dashboard.py

Close the window (or Ctrl+C in the console) to stop.
"""

import re
import time
import math
import subprocess
from collections import defaultdict, deque

import pymysql
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

try:
    from plyer import notification as _plyer_notify
except Exception:
    _plyer_notify = None

# ------------------------- Config -------------------------
DB_CONFIG = dict(host="localhost", port=3306, user="root",
                 password="", charset="utf8mb4")   # XAMPP default: blank root password
DB_NAME   = "wifi_sense"

SCAN_INTERVAL   = 1.5     # seconds between scans
HISTORY_LEN     = 8       # samples per BSSID kept for variance
MOTION_STD_DB   = 4.0     # signal std-dev (%) above this => motion
BASELINE_SCANS  = 6       # warm-up scans before judging motion
ACTIVITY_WINDOW = 60      # points shown on the rolling activity graph

# RSSI -> distance model (log-distance path loss).
MEASURED_POWER_DBM = -45.0   # typical RSSI at 1 metre from an AP
PATH_LOSS_EXPONENT = 2.7     # ~2 open space, ~3 cluttered indoor
NOTIFY_COOLDOWN_S  = 8       # min seconds between desktop notifications
# ----------------------------------------------------------


def percent_to_dbm(pct):
    """Windows reports signal 0-100%. Microsoft's mapping: 0%=-100dBm, 100%=-50dBm."""
    return pct / 2.0 - 100.0


def dbm_to_distance_m(dbm):
    """Log-distance path-loss estimate. Rough -- see HONEST LIMITS above."""
    return 10 ** ((MEASURED_POWER_DBM - dbm) / (10 * PATH_LOSS_EXPONENT))


def scan_networks():
    """Return {bssid: (ssid, signal_percent)} via netsh."""
    try:
        raw = subprocess.run(
            ["netsh", "wlan", "show", "networks", "mode=bssid"],
            capture_output=True, timeout=15
        ).stdout
        out = raw.decode("utf-8", errors="replace")
        if out.count("�") > len(out) // 20:
            out = raw.decode("cp1252", errors="replace")
    except Exception as e:
        print(f"[!] scan failed: {e}")
        return {}

    results, ssid, bssid = {}, None, None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", line)
        if m:
            ssid = m.group(1).strip() or "<hidden>"; continue
        m = re.match(r"^BSSID\s+\d+\s*:\s*([0-9a-fA-F:]{17})$", line)
        if m:
            bssid = m.group(1).lower(); continue
        m = re.match(r"^Signal\s*:\s*(\d+)%$", line)
        if m and bssid:
            results[bssid] = (ssid or "<unknown>", int(m.group(1)))
    return results


# ------------------------- Database -------------------------
def db_connect():
    """Connect, creating the database + tables on first run."""
    conn = pymysql.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME} "
                    "CHARACTER SET utf8mb4")
    conn.select_db(DB_NAME)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id           BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts           DATETIME NOT NULL,
                bssid        VARCHAR(17) NOT NULL,
                ssid         VARCHAR(64),
                signal_pct   INT,
                est_dist_m   FLOAT,
                INDEX (ts), INDEX (bssid)
            ) ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS motion_events (
                id            BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts            DATETIME NOT NULL,
                activity      FLOAT,
                trigger_bssid VARCHAR(17),
                aps_visible   INT,
                INDEX (ts)
            ) ENGINE=InnoDB
        """)
    conn.commit()
    return conn


def log_scan(conn, ts, nets):
    rows = [(ts, b, ssid, pct, round(dbm_to_distance_m(percent_to_dbm(pct)), 2))
            for b, (ssid, pct) in nets.items()]
    if rows:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO scans (ts,bssid,ssid,signal_pct,est_dist_m) "
                "VALUES (%s,%s,%s,%s,%s)", rows)
        conn.commit()


def log_motion(conn, ts, activity, bssid, aps):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO motion_events (ts,activity,trigger_bssid,aps_visible)"
                    " VALUES (%s,%s,%s,%s)", (ts, activity, bssid, aps))
    conn.commit()


# ------------------------- Notification -------------------------
def notify(title, msg):
    if _plyer_notify:
        try:
            _plyer_notify.notify(title=title, message=msg,
                                 app_name="WiFi Sense", timeout=5)
            return
        except Exception:
            pass
    # Fallback: audible beep + console line, no external dependency.
    try:
        import ctypes; ctypes.windll.user32.MessageBeep(0xFFFFFFFF)
    except Exception:
        pass
    print(f"\a[NOTIFY] {title}: {msg}")


# ------------------------- Main app -------------------------
class Dashboard:
    def __init__(self, conn):
        self.conn = conn
        self.history = defaultdict(lambda: deque(maxlen=HISTORY_LEN))
        self.activity = deque([0] * ACTIVITY_WINDOW, maxlen=ACTIVITY_WINDOW)
        self.scan_count = 0
        self.last_notify = 0.0

        self.fig = plt.figure(figsize=(11, 5))
        self.fig.canvas.manager.set_window_title("Wi-Fi Sensing Dashboard (passive)")
        self.ax_act = self.fig.add_subplot(1, 2, 1)
        self.ax_rad = self.fig.add_subplot(1, 2, 2, projection="polar")

    def step(self):
        """One scan cycle. Returns (nets, activity, worst_ap, motion)."""
        nets = scan_networks()
        self.scan_count += 1
        ts = time.strftime("%Y-%m-%d %H:%M:%S")

        for b, (_, pct) in nets.items():
            self.history[b].append(pct)

        worst_std, worst_ap = 0.0, None
        for b, h in self.history.items():
            if len(h) >= 3:
                n = len(h); mean = sum(h) / n
                std = (sum((s - mean) ** 2 for s in h) / n) ** 0.5
                if std > worst_std:
                    worst_std, worst_ap = std, b

        self.activity.append(worst_std)
        motion = self.scan_count > BASELINE_SCANS and worst_std >= MOTION_STD_DB

        # Persist
        try:
            log_scan(self.conn, ts, nets)
            if motion:
                log_motion(self.conn, ts, round(worst_std, 2), worst_ap, len(nets))
        except Exception as e:
            print(f"[!] DB write failed: {e}")

        # Notify (rate-limited)
        if motion and time.time() - self.last_notify > NOTIFY_COOLDOWN_S:
            self.last_notify = time.time()
            notify("Motion detected",
                   f"Signal wobble {worst_std:.1f}% on AP ...{(worst_ap or '')[-8:]}")

        return nets, worst_std, worst_ap, motion

    def draw(self, _frame):
        nets, activity, worst_ap, motion = self.step()

        # ---- Activity graph ----
        self.ax_act.clear()
        self.ax_act.plot(list(self.activity), color="tab:red" if motion else "tab:blue")
        self.ax_act.axhline(MOTION_STD_DB, ls="--", color="gray", lw=1)
        self.ax_act.set_ylim(0, max(MOTION_STD_DB * 2, max(self.activity) + 1))
        self.ax_act.set_title("Activity level (signal wobble %)")
        self.ax_act.set_xlabel("time  ->")
        state = "MOTION" if motion else ("warming up" if self.scan_count <= BASELINE_SCANS else "still")
        self.ax_act.text(0.02, 0.92, state, transform=self.ax_act.transAxes,
                         color="red" if motion else "green", fontweight="bold")

        # ---- Radar of access points ----
        self.ax_rad.clear()
        self.ax_rad.set_title("Access points  (radius = est. distance;\nangle is arbitrary, NOT direction)",
                              fontsize=9)
        self.ax_rad.set_theta_zero_location("N")
        max_d = 1.0
        items = sorted(nets.items(), key=lambda kv: -kv[1][1])  # strongest first
        for i, (b, (ssid, pct)) in enumerate(items):
            d = dbm_to_distance_m(percent_to_dbm(pct))
            max_d = max(max_d, d)
            ang = 2 * math.pi * i / max(1, len(items))
            self.ax_rad.plot(ang, d, "o", markersize=9)
            self.ax_rad.annotate(f"{ssid[:12]}\n{d:.1f} m",
                                 xy=(ang, d), fontsize=7, ha="center")
        self.ax_rad.set_ylim(0, max_d * 1.15)
        # "you are here" at centre
        self.ax_rad.plot(0, 0, "k*", markersize=14)

        self.fig.tight_layout()

    def run(self):
        # interval in ms; matplotlib drives the scan loop
        self.anim = FuncAnimation(self.fig, self.draw,
                                  interval=int(SCAN_INTERVAL * 1000),
                                  cache_frame_data=False)
        plt.show()


def main():
    print("Connecting to MySQL (root@localhost:3306)...")
    try:
        conn = db_connect()
    except Exception as e:
        print(f"[!] Could not connect to MySQL: {e}\n"
              f"    Make sure XAMPP's MySQL is started, then retry.")
        return
    print(f"OK. Logging to database '{DB_NAME}'. Opening dashboard window...")
    Dashboard(conn).run()
    conn.close()
    print("Stopped. Nothing was ever transmitted.")


if __name__ == "__main__":
    main()
