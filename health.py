"""
health.py  -  Wi-Fi health log: signal, link rates and latency over time.

Once a minute it records your connection (signal, rx/tx rate, channel) and,
if the "latency test" setting is on, pings your router and 1.1.1.1 three
times each. Pinging is ordinary network traffic on your own connection: it
is not a radio scan and doesn't affect the passive sensing.

Connection events (disconnect / reconnect / roaming to another access point)
are detected by the sensor every scan and logged as `health` detections.
"""

import re
import subprocess
import threading
import time

from wifi_dashboard import db_connect

INTERVAL_S = 60
INTERNET_HOST = "1.1.1.1"
SLOW_GW_MS, SLOW_NET_MS, SLOW_LOSS = 100, 300, 34
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def prepare(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wifi_health (
                id        BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts        DATETIME NOT NULL,
                ssid      VARCHAR(64),
                bssid     VARCHAR(17),
                rssi      FLOAT,
                rx        FLOAT,
                tx        FLOAT,
                channel   INT,
                gw_ms     FLOAT,
                net_ms    FLOAT,
                loss_pct  FLOAT,
                INDEX (ts)
            ) ENGINE=InnoDB
        """)
    conn.commit()


def default_gateway():
    """IPv4 default gateway of the Wi-Fi adapter (falls back to any adapter)."""
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, timeout=10, creationflags=NO_WINDOW).stdout.decode("utf-8", "replace")
    except Exception:
        return None
    section, found = "", {}
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line and not line.startswith(" "):
            section = line
        if "Default Gateway" in line:
            cands = [line.split(":", 1)[-1].strip()] + [l.strip() for l in lines[i + 1:i + 3]]
            ip = next((c for c in cands if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", c)), None)
            if ip:
                found.setdefault("wifi" if "Wireless" in section or "Wi-Fi" in section else "other", ip)
    return found.get("wifi") or found.get("other")


def ping(host, count=3):
    """-> (avg_ms or None, loss_pct)."""
    try:
        out = subprocess.run(["ping", "-n", str(count), "-w", "1000", host], creationflags=NO_WINDOW,
                             capture_output=True, timeout=count * 2 + 5).stdout.decode("utf-8", "replace")
    except Exception:
        return None, 100.0
    times = [float(t) for t in re.findall(r"time[=<](\d+(?:\.\d+)?)\s*ms", out, re.I)]
    loss = 100.0 * (count - len(times)) / count
    return (round(sum(times) / len(times), 1) if times else None), round(loss, 1)


class HealthMonitor(threading.Thread):
    def __init__(self, settings, get_link, emit):
        super().__init__(daemon=True)
        self.settings = settings
        self.get_link = get_link       # () -> latest link dict or None
        self.emit = emit               # sensor.emit
        self.latest = None
        self.last_slow = 0.0
        self.gateway = None

    def run(self):
        time.sleep(15)                 # let the sensor settle first
        conn = None
        while True:
            t0 = time.time()
            try:
                if conn is None:
                    conn = db_connect()
                self.sample(conn)
            except Exception as e:
                print(f"[!] health sample failed: {type(e).__name__}: {e}")
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            time.sleep(max(5.0, INTERVAL_S - (time.time() - t0)))

    def sample(self, conn):
        link = self.get_link()
        gw_ms = net_ms = loss = None
        if link and self.settings.get("latency_test"):
            self.gateway = default_gateway() or self.gateway
            if self.gateway:
                gw_ms, loss = ping(self.gateway)
            net_ms, net_loss = ping(INTERNET_HOST)
            loss = max(loss or 0.0, net_loss)
        row = dict(ts=time.strftime("%Y-%m-%d %H:%M:%S"),
                   ssid=(link or {}).get("ssid"), bssid=(link or {}).get("bssid"),
                   rssi=(link or {}).get("dbm"), rx=(link or {}).get("rx"), tx=(link or {}).get("tx"),
                   channel=(link or {}).get("channel"), gw_ms=gw_ms, net_ms=net_ms, loss_pct=loss)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO wifi_health (ts,ssid,bssid,rssi,rx,tx,channel,gw_ms,net_ms,loss_pct) "
                        "VALUES (%(ts)s,%(ssid)s,%(bssid)s,%(rssi)s,%(rx)s,%(tx)s,%(channel)s,%(gw_ms)s,"
                        "%(net_ms)s,%(loss_pct)s)", dict(row, ssid=(row["ssid"] or "")[:64]))
        conn.commit()
        self.latest = row
        slow = [f"router {gw_ms:.0f} ms"] if gw_ms and gw_ms > SLOW_GW_MS else []
        if net_ms and net_ms > SLOW_NET_MS:
            slow.append(f"internet {net_ms:.0f} ms")
        if loss is not None and loss >= SLOW_LOSS:
            slow.append(f"{loss:.0f}% packet loss")
        if slow and time.time() - self.last_slow > 600:
            self.last_slow = time.time()
            self.emit("health", "warning", "Wi-Fi slow: " + ", ".join(slow), title="Wi-Fi slow")


def health_data(hours):
    hours = max(1, min(24 * 30, hours))
    c = db_connect()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT ts, rssi, rx, tx, gw_ms, net_ms, loss_pct, bssid FROM wifi_health "
                        "WHERE ts >= NOW() - INTERVAL %s HOUR ORDER BY ts", (hours,))
            rows = [dict(ts=str(t), rssi=r, rx=rx, tx=tx, gw=g, net=n, loss=l, bssid=b)
                    for t, r, rx, tx, g, n, l, b in cur.fetchall()]
            cur.execute("SELECT HOUR(ts), AVG(gw_ms), AVG(net_ms), AVG(loss_pct), AVG(rx), COUNT(*) FROM wifi_health "
                        "WHERE ts >= NOW() - INTERVAL 7 DAY GROUP BY HOUR(ts)")
            by_hour = [dict(h=h, gw=_r(g), net=_r(n), loss=_r(l), rx=_r(rx), n=cnt)
                       for h, g, n, l, rx, cnt in cur.fetchall()]
            cur.execute("SELECT ts, detail FROM detections WHERE kind='health' AND ts >= NOW() - INTERVAL %s HOUR "
                        "ORDER BY ts DESC LIMIT 200", (hours,))
            events = [dict(ts=str(t), msg=m) for t, m in cur.fetchall()]
        return dict(hours=hours, rows=rows, by_hour=by_hour, events=events)
    finally:
        c.close()


def _r(v):
    return None if v is None else round(float(v), 1)
