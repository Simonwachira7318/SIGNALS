"""
report.py  -  Daily summary (sent to Telegram each morning, or on demand).

Covers the last 24 hours: motion episodes and the busiest hour, time the
room was occupied, unknown devices, security events, and how much feedback
you've given.
"""

import time
from datetime import datetime, timedelta

from wifi_dashboard import db_connect


def _occupied_seconds(events, start, end, state_at_start):
    """Sum occupied time from ordered (ts, kind) occupied/empty events."""
    total, cur, t0 = 0.0, state_at_start, start
    for ts, kind in events:
        if cur == "occupied":
            total += (ts - t0).total_seconds()
        cur, t0 = ("occupied" if kind == "occupied" else "empty"), ts
    if cur == "occupied":
        total += (end - t0).total_seconds()
    return total


def build_summary(hours=24, presence_now=None):
    end = datetime.now()
    start = end - timedelta(hours=hours)
    c = db_connect()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT kind, COUNT(*) FROM detections WHERE ts >= %s GROUP BY kind", (start,))
            kinds = dict(cur.fetchall())
            cur.execute("SELECT HOUR(ts), COUNT(*) c FROM detections WHERE kind='motion' AND ts >= %s "
                        "GROUP BY HOUR(ts) ORDER BY c DESC LIMIT 1", (start,))
            busiest = cur.fetchone()
            cur.execute("SELECT ts, kind FROM detections WHERE kind IN ('occupied','empty') AND ts >= %s "
                        "ORDER BY ts", (start,))
            pres = list(cur.fetchall())
            cur.execute("SELECT kind FROM detections WHERE kind IN ('occupied','empty') AND ts < %s "
                        "ORDER BY ts DESC LIMIT 1", (start,))
            before = cur.fetchone()
            cur.execute("SELECT detail FROM detections WHERE kind='new_device' AND ts >= %s "
                        "ORDER BY ts DESC LIMIT 5", (start,))
            unknown = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT detail FROM detections WHERE kind='security' AND ts >= %s "
                        "ORDER BY ts DESC LIMIT 3", (start,))
            security = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT feedback, COUNT(*) FROM detections WHERE feedback IS NOT NULL AND ts >= %s "
                        "GROUP BY feedback", (start,))
            fb = dict(cur.fetchall())
            cur.execute("SELECT COUNT(*) FROM devices WHERE last_seen >= %s", (start,))
            devices_seen = cur.fetchone()[0]
    finally:
        c.close()

    state0 = before[0] if before else ("occupied" if (not pres and presence_now == "occupied") else "empty")
    occ_s = _occupied_seconds(pres, start, end, state0)
    motion = kinds.get("motion", 0)

    lines = [f"📊 Daily summary · last {hours} h (to {end:%a %d %b %H:%M})", ""]
    lines.append(f"🏃 Motion episodes: {motion}"
                 + (f" · busiest hour {busiest[0]:02d}:00 ({busiest[1]})" if busiest and motion else ""))
    lines.append(f"🏠 Room occupied: {occ_s / 3600:.1f} h ({occ_s / (hours * 36):.0f}% of the time)"
                 + (f" · now {presence_now}" if presence_now else ""))
    if kinds.get("blocked") or kinds.get("still"):
        lines.append(f"🧍 Presence hints: {kinds.get('still', 0)} still, {kinds.get('blocked', 0)} blocked path")
    lines.append(f"📡 Devices seen: {devices_seen} · unknown arrivals: {kinds.get('new_device', 0)}"
                 f" · known arrivals: {kinds.get('arrived', 0)}")
    for u in unknown:
        lines.append(f"   • {u}")
    if security:
        lines.append(f"🛡️ Security alerts: {kinds.get('security', 0)}")
        for s in security:
            lines.append(f"   • {s}")
    else:
        lines.append("🛡️ No security alerts")
    if fb:
        lines.append(f"👍 Feedback: {fb.get(1, 0)} confirmed · 👎 {fb.get(0, 0)} false alarms corrected")
    text = "\n".join(lines)
    return dict(text=text, motion=motion, occupied_h=round(occ_s / 3600, 2), kinds=kinds,
                generated=time.strftime("%Y-%m-%d %H:%M"))
