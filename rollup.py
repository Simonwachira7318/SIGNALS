"""
rollup.py  -  Keep history forever (cheaply) + database backups.

Hourly rollups
    Before raw `scans` rows are deleted by the retention setting, each full
    hour is summarised into `hourly_stats` (kept forever, one row per hour):
    minutes the sensor ran, sources seen, motion episodes, minutes occupied,
    average link signal and latency. The History page's long-term charts and
    the unusual-activity detector read from it.

Backups
    `backup()` runs XAMPP's mysqldump into ./backups (newest 14 kept). The
    huge raw `scans` / `motion_events` tables are skipped by default since the
    rollups preserve their history; pass full=True to include them.
"""

import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta

from wifi_dashboard import DB_CONFIG, DB_NAME, SCAN_INTERVAL

from paths import APP_DIR
BACKUP_DIR = APP_DIR / "backups"
KEEP_BACKUPS = 14
MYSQLDUMP_CANDIDATES = [r"C:\xampp\mysql\bin\mysqldump.exe", r"C:\xampp\mysql\bin\mariadb-dump.exe"]


def prepare(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hourly_stats (
                hour            DATETIME PRIMARY KEY,
                running_min     FLOAT NOT NULL DEFAULT 0,
                sources         INT NOT NULL DEFAULT 0,
                motion_episodes INT NOT NULL DEFAULT 0,
                occupied_min    FLOAT NOT NULL DEFAULT 0,
                avg_link_rssi   FLOAT NULL,
                avg_gw_ms       FLOAT NULL,
                avg_net_ms      FLOAT NULL
            ) ENGINE=InnoDB
        """)
    conn.commit()


def _occupied_minutes(cur, start, end):
    cur.execute("SELECT kind FROM detections WHERE kind IN ('occupied','empty') AND ts < %s "
                "ORDER BY ts DESC LIMIT 1", (start,))
    r = cur.fetchone()
    state = r[0] if r else "empty"
    cur.execute("SELECT ts, kind FROM detections WHERE kind IN ('occupied','empty') AND ts >= %s AND ts < %s "
                "ORDER BY ts", (start, end))
    total, t0 = 0.0, start
    for ts, kind in cur.fetchall():
        if state == "occupied":
            total += (ts - t0).total_seconds()
        state, t0 = kind, ts
    if state == "occupied":
        total += (end - t0).total_seconds()
    return total / 60


def rollup(conn):
    """Summarise every complete hour not yet in hourly_stats. Returns hours written."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(hour) FROM hourly_stats")
        last = cur.fetchone()[0]
        if last is None:
            cur.execute("SELECT MIN(ts) FROM scans")
            first = cur.fetchone()[0]
            if first is None:
                return 0
            start = first.replace(minute=0, second=0, microsecond=0)
        else:
            start = last + timedelta(hours=1)
        end_all = datetime.now().replace(minute=0, second=0, microsecond=0)
        written = 0
        h = start
        while h < end_all:
            nxt = h + timedelta(hours=1)
            cur.execute("SELECT COUNT(DISTINCT ts), COUNT(DISTINCT bssid) FROM scans WHERE ts >= %s AND ts < %s",
                        (h, nxt))
            n_ts, sources = cur.fetchone()
            running = min(60.0, n_ts * SCAN_INTERVAL / 60)
            cur.execute("SELECT COUNT(*) FROM detections WHERE kind='motion' AND ts >= %s AND ts < %s", (h, nxt))
            episodes = cur.fetchone()[0]
            occ = _occupied_minutes(cur, h, nxt) if running else 0.0
            cur.execute("SELECT AVG(rssi), AVG(gw_ms), AVG(net_ms) FROM wifi_health WHERE ts >= %s AND ts < %s",
                        (h, nxt))
            rssi, gw, net = cur.fetchone()
            cur.execute("REPLACE INTO hourly_stats (hour, running_min, sources, motion_episodes, occupied_min, "
                        "avg_link_rssi, avg_gw_ms, avg_net_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (h, round(running, 1), sources, episodes, round(occ, 1),
                         None if rssi is None else round(float(rssi), 1),
                         None if gw is None else round(float(gw), 1),
                         None if net is None else round(float(net), 1)))
            written += 1
            h = nxt
    conn.commit()
    return written


def daily_series(conn, days):
    with conn.cursor() as cur:
        cur.execute("SELECT DATE(hour), SUM(running_min), SUM(motion_episodes), SUM(occupied_min), "
                    "AVG(avg_net_ms) FROM hourly_stats WHERE hour >= CURDATE() - INTERVAL %s DAY "
                    "GROUP BY DATE(hour) ORDER BY DATE(hour)", (days - 1,))
        return [dict(d=str(d), running_h=round(float(r or 0) / 60, 1), episodes=int(e or 0),
                     occupied_h=round(float(o or 0) / 60, 1), net_ms=None if n is None else round(float(n)))
                for d, r, e, o, n in cur.fetchall()]


# ------------------------- backups -------------------------
def _mysqldump():
    for c in MYSQLDUMP_CANDIDATES:
        if os.path.exists(c):
            return c
    return shutil.which("mysqldump") or shutil.which("mariadb-dump")


def backup(full=False):
    exe = _mysqldump()
    if not exe:
        raise FileNotFoundError("mysqldump not found (expected in C:\\xampp\\mysql\\bin)")
    BACKUP_DIR.mkdir(exist_ok=True)
    out = BACKUP_DIR / f"wifi_sense_{time.strftime('%Y%m%d_%H%M%S')}{'_full' if full else ''}.sql"
    args = [exe, f"--host={DB_CONFIG['host']}", f"--port={DB_CONFIG['port']}", f"--user={DB_CONFIG['user']}",
            "--single-transaction", "--routines", "--default-character-set=utf8mb4"]
    if not full:
        args += [f"--ignore-table={DB_NAME}.scans", f"--ignore-table={DB_NAME}.motion_events"]
    args.append(DB_NAME)
    env = dict(os.environ, MYSQL_PWD=DB_CONFIG["password"] or "")   # keeps the password off the command line
    with open(out, "wb") as f:
        r = subprocess.run(args, stdout=f, stderr=subprocess.PIPE, env=env, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[:300])
    for old in sorted(BACKUP_DIR.glob("wifi_sense_*.sql"))[:-KEEP_BACKUPS]:
        old.unlink()
    return dict(file=out.name, size_kb=round(out.stat().st_size / 1024))


def list_backups():
    if not BACKUP_DIR.exists():
        return []
    return [dict(file=p.name, size_kb=round(p.stat().st_size / 1024),
                 ts=time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)))
            for p in sorted(BACKUP_DIR.glob("wifi_sense_*.sql"), reverse=True)]
