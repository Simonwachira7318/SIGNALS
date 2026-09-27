"""
devices.py  -  Known-device registry and per-path sensitivity tuning.

DeviceRegistry
    Every Wi-Fi source ever seen, with an optional friendly name, a "trusted"
    flag (never raise security alerts for it) and a "muted" flag (no
    appear/leave events). Named or trusted devices count as *known*: they
    produce quiet "arrived" events instead of "unknown device" alerts.

Tuning
    Per-path multipliers learned from 👍/👎 feedback on detections:
      motion_factor  scales the path's motion threshold
      dev_factor     scales its blocked/still deviation thresholds
"""

import threading
import time

import oui
from wifi_dashboard import db_connect

FACTOR_UP, FACTOR_DOWN = 1.15, 0.97      # false alarm / confirmed detection
FACTOR_RANGE = (0.6, 2.5)


def prepare(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                bssid      VARCHAR(17) PRIMARY KEY,
                ssid       VARCHAR(64),
                name       VARCHAR(64),
                trusted    TINYINT(1) NOT NULL DEFAULT 0,
                muted      TINYINT(1) NOT NULL DEFAULT 0,
                vendor     VARCHAR(96),
                dtype      VARCHAR(12),
                first_seen DATETIME NOT NULL,
                last_seen  DATETIME NOT NULL
            ) ENGINE=InnoDB
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS path_tuning (
                path          VARCHAR(17) PRIMARY KEY,
                motion_factor FLOAT NOT NULL DEFAULT 1,
                dev_factor    FLOAT NOT NULL DEFAULT 1,
                confirmed     INT NOT NULL DEFAULT 0,
                false_alarms  INT NOT NULL DEFAULT 0
            ) ENGINE=InnoDB
        """)
    conn.commit()


class DeviceRegistry:
    def __init__(self, conn):
        self.lock = threading.Lock()
        self.devs = {}                 # bssid -> dict
        self.dirty = set()             # bssids whose last_seen needs saving
        self.tuning = {}               # path -> dict(motion_factor, dev_factor, confirmed, false_alarms)
        self._seed_from_scans(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT bssid, ssid, name, trusted, muted, vendor, dtype, first_seen, last_seen FROM devices")
            for b, ssid, name, tr, mu, vendor, dtype, fs, ls in cur.fetchall():
                self.devs[b] = dict(ssid=ssid, name=name, trusted=bool(tr), muted=bool(mu),
                                    vendor=vendor, dtype=dtype, first_seen=str(fs), last_seen=str(ls))
            cur.execute("SELECT path, motion_factor, dev_factor, confirmed, false_alarms FROM path_tuning")
            for p, mf, df, c, f in cur.fetchall():
                self.tuning[p] = dict(motion_factor=mf, dev_factor=df, confirmed=c, false_alarms=f)

    @staticmethod
    def _seed_from_scans(conn):
        """First run: import every source already in the scans table."""
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM devices")
            if cur.fetchone()[0]:
                return
            cur.execute("SELECT bssid, MAX(ssid), MIN(ts), MAX(ts) FROM scans GROUP BY bssid")
            rows = [(b, (ssid or "")[:64], oui.vendor(b), fs, ls) for b, ssid, fs, ls in cur.fetchall()]
            if rows:
                cur.executemany("INSERT IGNORE INTO devices (bssid, ssid, vendor, first_seen, last_seen) "
                                "VALUES (%s,%s,%s,%s,%s)", rows)
        conn.commit()

    # ---------- devices ----------
    def get(self, bssid):
        with self.lock:
            d = self.devs.get(bssid)
            return dict(d) if d else None

    def is_known(self, bssid):
        d = self.get(bssid)
        return bool(d and (d["name"] or d["trusted"]))

    def display(self, bssid, ssid):
        d = self.get(bssid)
        return d["name"] if d and d["name"] else ssid

    def touch(self, bssid, ssid, vendor, dtype):
        """Mark a device as seen now (saved in batches by flush())."""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            d = self.devs.get(bssid)
            if d is None:
                d = self.devs[bssid] = dict(ssid=ssid, name=None, trusted=False, muted=False,
                                            vendor=vendor, dtype=dtype, first_seen=now, last_seen=now)
            d.update(ssid=ssid, last_seen=now)
            if not d.get("vendor"):
                d["vendor"] = vendor
            if not d.get("dtype"):
                d["dtype"] = dtype
            self.dirty.add(bssid)

    def flush(self, conn):
        with self.lock:
            rows = [(b, self.devs[b]["ssid"][:64], self.devs[b]["vendor"], self.devs[b]["dtype"],
                     self.devs[b]["first_seen"], self.devs[b]["last_seen"]) for b in self.dirty]
            self.dirty.clear()
        if not rows:
            return
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO devices (bssid, ssid, vendor, dtype, first_seen, last_seen) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE ssid=VALUES(ssid), last_seen=VALUES(last_seen), "
                "vendor=COALESCE(vendor, VALUES(vendor)), dtype=COALESCE(dtype, VALUES(dtype))", rows)
        conn.commit()

    def update(self, bssid, name=None, trusted=None, muted=None):
        """Edit from the UI (HTTP thread; uses its own DB connection)."""
        with self.lock:
            d = self.devs.get(bssid)
            if d is None:
                return None
            if name is not None:
                d["name"] = name.strip()[:64] or None
            if trusted is not None:
                d["trusted"] = bool(trusted)
            if muted is not None:
                d["muted"] = bool(muted)
            snap = dict(d)
            self.dirty.add(bssid)          # make sure the row exists before the UPDATE below
        c = db_connect()
        try:
            with c.cursor() as cur:
                cur.execute("INSERT INTO devices (bssid, ssid, vendor, dtype, first_seen, last_seen) "
                            "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE bssid=bssid",
                            (bssid, (snap["ssid"] or "")[:64], snap["vendor"], snap["dtype"],
                             snap["first_seen"], snap["last_seen"]))
                cur.execute("UPDATE devices SET name=%s, trusted=%s, muted=%s WHERE bssid=%s",
                            (snap["name"], int(snap["trusted"]), int(snap["muted"]), bssid))
            c.commit()
        finally:
            c.close()
        return snap

    def all(self):
        with self.lock:
            return {b: dict(d) for b, d in self.devs.items()}

    # ---------- tuning ----------
    def factor(self, path, which):
        t = self.tuning.get(path)
        return t[which] if t else 1.0

    def apply_feedback(self, paths, which, verdict):
        """Nudge factors for the given paths. Returns [(path, new_factor)]."""
        out = []
        c = db_connect()
        try:
            for p in paths:
                with self.lock:
                    t = self.tuning.setdefault(p, dict(motion_factor=1.0, dev_factor=1.0,
                                                       confirmed=0, false_alarms=0))
                    if verdict == "false":
                        t[which] = min(FACTOR_RANGE[1], t[which] * FACTOR_UP)
                        t["false_alarms"] += 1
                    else:
                        t[which] = max(FACTOR_RANGE[0], t[which] * FACTOR_DOWN)
                        t["confirmed"] += 1
                    row = (p, t["motion_factor"], t["dev_factor"], t["confirmed"], t["false_alarms"])
                with c.cursor() as cur:
                    cur.execute("REPLACE INTO path_tuning (path, motion_factor, dev_factor, confirmed, "
                                "false_alarms) VALUES (%s,%s,%s,%s,%s)", row)
                out.append((p, round(row[1 if which == "motion_factor" else 2], 2)))
            c.commit()
        finally:
            c.close()
        return out

    def reset_tuning(self):
        with self.lock:
            self.tuning.clear()
        c = db_connect()
        try:
            with c.cursor() as cur:
                cur.execute("DELETE FROM path_tuning")
            c.commit()
        finally:
            c.close()

    def tuning_summary(self):
        with self.lock:
            return dict(paths=sum(1 for t in self.tuning.values()
                                  if abs(t["motion_factor"] - 1) > 1e-6 or abs(t["dev_factor"] - 1) > 1e-6),
                        confirmed=sum(t["confirmed"] for t in self.tuning.values()),
                        false_alarms=sum(t["false_alarms"] for t in self.tuning.values()))


def presence_grid(days=7):
    """{bssid: [[0/1]*24]*days} from the scans table (hours in which each device was seen)."""
    c = db_connect()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT bssid, DATEDIFF(CURDATE(), DATE(ts)), HOUR(ts), COUNT(*) FROM scans "
                        "WHERE ts >= CURDATE() - INTERVAL %s DAY GROUP BY bssid, DATE(ts), HOUR(ts)",
                        (days - 1,))
            grid = {}
            for b, ago, h, _n in cur.fetchall():
                g = grid.setdefault(b, [[0] * 24 for _ in range(days)])
                row = days - 1 - ago
                if 0 <= row < days:
                    g[row][h] = 1
        return grid
    finally:
        c.close()
