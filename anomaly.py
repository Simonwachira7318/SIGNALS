"""
anomaly.py  -  "Movement at an unusual time" detector.

Learns, for each hour of the week (Mon 00:00 ... Sun 23:00), how often motion
episodes happen per hour the sensor was running, from `hourly_stats`
(up to 8 weeks). When a new motion episode starts in a slot that is normally
quiet, it raises an `unusual` alert.

It needs at least MIN_DAYS days of history before it alerts at all, and at
least MIN_SLOT_HOURS monitored hours in that particular slot, so it won't
cry wolf while it's still learning.
"""

import time
from datetime import datetime

MIN_DAYS = 7
MIN_SLOT_HOURS = 2.0
QUIET_RATE = 0.25            # < 1 episode per 4 monitored hours = "usually quiet"
BUSY_RATE = 2.0
REFRESH_S = 1800
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class UnusualDetector:
    def __init__(self):
        self.slots = {}          # (weekday, hour) -> dict(hours, episodes)
        self.days = 0
        self.loaded_at = 0.0

    def refresh(self, conn, force=False):
        if not force and time.time() - self.loaded_at < REFRESH_S:
            return
        self.loaded_at = time.time()
        with conn.cursor() as cur:
            # WEEKDAY(): 0 = Monday. Today's partial day is excluded so it can't mask itself.
            cur.execute("SELECT WEEKDAY(hour), HOUR(hour), SUM(running_min)/60, SUM(motion_episodes) "
                        "FROM hourly_stats WHERE hour >= CURDATE() - INTERVAL 56 DAY AND hour < CURDATE() "
                        "AND running_min > 0 GROUP BY WEEKDAY(hour), HOUR(hour)")
            self.slots = {(int(d), int(h)): dict(hours=float(rh or 0), episodes=int(e or 0))
                          for d, h, rh, e in cur.fetchall()}
            cur.execute("SELECT COUNT(DISTINCT DATE(hour)) FROM hourly_stats "
                        "WHERE running_min > 0 AND hour < CURDATE()")
            self.days = int(cur.fetchone()[0] or 0)

    def slot_view(self, when=None):
        when = when or datetime.now()
        s = self.slots.get((when.weekday(), when.hour))
        if self.days < MIN_DAYS:
            return dict(ready=False, days=self.days, need=MIN_DAYS,
                        text=f"Learning normal patterns ({self.days}/{MIN_DAYS} days)")
        if not s or s["hours"] < MIN_SLOT_HOURS:
            return dict(ready=True, known=False, text=f"Not enough history for {DAYS[when.weekday()]} {when.hour:02d}:00 yet")
        rate = s["episodes"] / s["hours"]
        label = "usually quiet" if rate < QUIET_RATE else "usually busy" if rate >= BUSY_RATE else "usually some activity"
        return dict(ready=True, known=True, rate=round(rate, 2), hours=round(s["hours"], 1),
                    text=f"{DAYS[when.weekday()]} {when.hour:02d}:00 is {label} "
                         f"({s['episodes']} episodes in {s['hours']:.0f} h monitored)")

    def check(self, when=None):
        """Called when a motion episode starts. -> (message, confidence) or None."""
        when = when or datetime.now()
        v = self.slot_view(when)
        if not v.get("known") or v["rate"] >= QUIET_RATE:
            return None
        conf = min(0.9, 0.4 + 0.08 * v["hours"]) * (1 - v["rate"] / QUIET_RATE * 0.5)
        return (f"Movement at {when:%H:%M} on a {when:%A}: this time is usually quiet "
                f"({v['rate']:.2f} episodes/hour over {v['hours']:.0f} monitored hours)", conf)
