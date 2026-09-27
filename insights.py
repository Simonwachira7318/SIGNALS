"""
insights.py  -  Data for the "Why did it fire?" drill-down, the "Your day"
timeline with replay, and the floor-plan heat trail.

why(det_id)      signals around one detection (45 s before to 30 s after):
                 per-path signal %, rolling wobble, and, when the event is
                 recent enough to still be in memory, the exact threshold
                 ratios the live detector used. Plus a plain-language reason.
day(date)        per-minute lanes for one day: sensor running, movement,
                 room occupied, and event markers.
at(ts)           what the signal map looked like at a past moment (replay).
"""

import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta

from wifi_dashboard import db_connect, HISTORY_LEN, MEASURED_POWER_DBM, percent_to_dbm

BEFORE_S, AFTER_S = 45, 30
EVENT_KINDS = ("occupied", "empty", "new_device", "arrived", "left", "health", "unusual",
               "security", "room_motion", "motion", "blocked", "still", "ble")


def _ep(dt):
    return dt.timestamp()


def _wobble_series(points):
    """points [(epoch, pct)] -> [(epoch, rolling std over HISTORY_LEN)]."""
    out, win = [], []
    for t, v in points:
        win.append(v)
        win = win[-HISTORY_LEN:]
        if len(win) >= 3:
            out.append((t, round(statistics.pstdev(win), 2)))
    return out


# ------------------------- why -------------------------
def why(det_id, recent, names, thresholds):
    """recent: list of (epoch, idx_all, link_ratio, {bssid: (ratio, state)}) from the sensor.
    names: bssid -> display name. thresholds: bssid -> current threshold (%)."""
    c = db_connect()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT id, ts, kind, severity, bssid, ssid, detail, confidence, paths, feedback "
                        "FROM detections WHERE id=%s", (det_id,))
            r = cur.fetchone()
            if not r:
                raise KeyError(f"detection {det_id} not found")
            _, ts, kind, sev, bssid, ssid, detail, conf, paths, fb = r
            start, end = ts - timedelta(seconds=BEFORE_S + 12), ts + timedelta(seconds=AFTER_S)
            cur.execute("SELECT ts, bssid, ssid, signal_pct FROM scans WHERE ts BETWEEN %s AND %s ORDER BY ts, id",
                        (start, end))
            rows = cur.fetchall()
    finally:
        c.close()

    wanted = [p for p in (paths or bssid or "").split(",") if p]
    by = defaultdict(list)
    ssids = {}
    for t, b, s, pct in rows:
        by[b].append((_ep(t), pct))
        ssids[b] = s
    if not wanted:                                   # e.g. room occupied: show the liveliest paths
        wanted = sorted(by, key=lambda b: -statistics.pstdev([v for _, v in by[b]]) if len(by[b]) > 2 else 0)[:4]
    t0, t_ev = _ep(start) + 12, _ep(ts)

    mem = [e for e in recent if t0 <= e[0] <= _ep(end)]
    series = []
    for b in wanted:
        pts = by.get(b, [])
        ratio = [(t, rr[b][0]) for t, _, _, rr in mem if b in rr]
        series.append(dict(bssid=b, name=names.get(b) or ssids.get(b) or b,
                           pct=[(t, v) for t, v in pts if t >= t0],
                           wobble=[(t, v) for t, v in _wobble_series(pts) if t >= t0],
                           ratio=ratio, threshold=thresholds.get(b)))
    idx = [(t, i) for t, i, _, _ in mem]
    link = [(t, l) for t, _, l, _ in mem]

    # plain-language reason
    explain = detail or ""
    if kind == "motion":
        near = lambda pts: [p for p in pts if abs(p[0] - t_ev) <= 8]      # around the moment it fired
        peaks = []
        for s_ in series:
            if near(s_["ratio"]):
                t, v = max(near(s_["ratio"]), key=lambda x: x[1])
                peaks.append((v, s_["name"], t, "×"))
            elif near(s_["wobble"]):
                t, v = max(near(s_["wobble"]), key=lambda x: x[1])
                peaks.append((v / (s_["threshold"] or 4.0), s_["name"], t, "× (approx.)"))
        best = max(peaks) if peaks else None
        if best and best[0] >= 0.8:
            v, name, t, unit = best
            explain = (f"The signal path to {name} wobbled {v:.1f}{unit} its own threshold at "
                       f"{datetime.fromtimestamp(t):%H:%M:%S}. Anything ≥ 1.0× counts as movement, ≥ 2.0× as strong.")
        else:
            later = [(max(s_["wobble"], key=lambda x: x[1]), s_["name"]) for s_ in series if s_["wobble"]]
            explain = ("This was triggered by your live link, which is read 10 times a second. Those fast readings "
                       "are only kept in memory for about an hour, and the per-scan readings below are too coarse "
                       "to show the moment it fired.")
            if later:
                (t, v), name = max(later, key=lambda x: x[0][1])
                if v > 0:
                    explain += f" The per-scan signal to {name} shows the biggest disturbance at {datetime.fromtimestamp(t):%H:%M:%S}."
    elif kind in ("blocked", "still") and series and series[0]["pct"]:
        pre = [v for t, v in series[0]["pct"] if t < t_ev - 5]
        post = [v for t, v in series[0]["pct"] if t >= t_ev - 3]
        if pre and post:
            explain = (f"Signal to {series[0]['name']} went from about {statistics.median(pre):.0f}% before to "
                       f"{statistics.median(post):.0f}% at the event, without much wobble: a steady change, "
                       f"like something or someone standing in that path.")
    elif kind in ("new_device", "arrived", "left"):
        explain = f"{detail}. Devices are tracked by their hardware address (BSSID)."
    return dict(det=dict(id=det_id, ts=str(ts), epoch=t_ev, kind=kind, severity=sev, msg=detail, conf=conf,
                         feedback=None if fb is None else ("correct" if fb else "false")),
                window=dict(start=t0, end=_ep(end)), series=series, idx=idx, link=link,
                in_memory=bool(mem), explain=explain)


# ------------------------- day -------------------------
def day(date_str):
    d0 = datetime.strptime(date_str, "%Y-%m-%d")
    d1 = d0 + timedelta(days=1)
    now = datetime.now()
    c = db_connect()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT HOUR(ts)*60+MINUTE(ts), COUNT(*) FROM scans WHERE ts >= %s AND ts < %s GROUP BY 1",
                        (d0, d1))
            running = dict(cur.fetchall())
            if not running:                                  # raw scans expired: fall back to hourly summaries
                cur.execute("SELECT HOUR(hour), running_min FROM hourly_stats WHERE hour >= %s AND hour < %s", (d0, d1))
                for h, rm in cur.fetchall():
                    for m in range(int(round(rm or 0))):
                        running[h * 60 + m] = 1
            cur.execute("SELECT HOUR(ts)*60+MINUTE(ts), COUNT(*), MAX(activity) FROM motion_events "
                        "WHERE ts >= %s AND ts < %s GROUP BY 1", (d0, d1))
            motion = {m: dict(n=n, peak=float(p or 0)) for m, n, p in cur.fetchall()}
            cur.execute("SELECT kind FROM detections WHERE kind IN ('occupied','empty') AND ts < %s "
                        "ORDER BY ts DESC LIMIT 1", (d0,))
            r = cur.fetchone()
            state = r[0] if r else "empty"
            cur.execute("SELECT id, ts, kind, severity, detail, confidence FROM detections WHERE ts >= %s AND ts < %s "
                        "AND kind IN (" + ",".join(["%s"] * len(EVENT_KINDS)) + ") ORDER BY ts",
                        (d0, d1, *EVENT_KINDS))
            evs = cur.fetchall()
    finally:
        c.close()
    occ, seg_start = [], _ep(d0)
    events = []
    for i, ts, kind, sev, detail, conf in evs:
        if kind in ("occupied", "empty"):
            if state == "occupied":
                occ.append((seg_start, _ep(ts)))
            state, seg_start = kind, _ep(ts)
        if kind != "motion":
            events.append(dict(id=i, t=_ep(ts), kind=kind, severity=sev, msg=detail, conf=conf))
    end = min(_ep(d1), _ep(now))
    if state == "occupied" and end > seg_start:
        occ.append((seg_start, end))
    return dict(date=date_str, start=_ep(d0), end=_ep(d1), now=_ep(now),
                running=sorted(running), motion=motion, occupied=occ, events=events,
                motion_ids=[dict(id=i, t=_ep(ts), severity=sev, msg=detail) for i, ts, kind, sev, detail, _ in evs
                            if kind == "motion"])


# ------------------------- replay a moment -------------------------
def at(ts_epoch, calibration, names, thresholds, distance_range, device_type):
    t = datetime.fromtimestamp(ts_epoch)
    c = db_connect()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT ts, bssid, ssid, signal_pct FROM scans WHERE ts BETWEEN %s AND %s ORDER BY ts, id",
                        (t - timedelta(seconds=HISTORY_LEN * 1.5 + 20), t + timedelta(seconds=1)))
            rows = cur.fetchall()
            cur.execute("SELECT kind FROM detections WHERE kind IN ('occupied','empty') AND ts <= %s "
                        "ORDER BY ts DESC LIMIT 1", (t,))
            r = cur.fetchone()
            cur.execute("SELECT id, ts, kind, severity, detail FROM detections WHERE ts BETWEEN %s AND %s ORDER BY ts",
                        (t - timedelta(minutes=5), t + timedelta(minutes=5)))
            near = [dict(id=i, ts=str(x), kind=k, severity=s, msg=m) for i, x, k, s, m in cur.fetchall()]
    finally:
        c.close()
    by = defaultdict(list)
    ssids = {}
    for x, b, s, pct in rows:
        by[b].append(pct)
        ssids[b] = s
    aps = []
    for b, vals in by.items():
        pct = vals[-1]
        w = statistics.pstdev(vals[-HISTORY_LEN:]) if len(vals) >= 3 else 0.0
        th = thresholds.get(b) or 4.0
        ratio = w / th
        mp = calibration.get(b, MEASURED_POWER_DBM)
        dbm = percent_to_dbm(pct)
        dist, lo, hi, acc = distance_range(dbm, w / 2, mp, b in calibration)
        aps.append(dict(bssid=b, ssid=ssids[b], name=names.get(b), pct=pct, dbm=round(dbm, 1), dist=dist,
                        dist_lo=lo, dist_hi=hi, acc=acc, calibrated=b in calibration, measured_power=mp,
                        wobble=round(w, 2), th=round(th, 2), ratio=round(ratio, 2), unit="%",
                        baseline=pct, dev=0.0, state="moving" if ratio >= 1 else "stable",
                        intensity=("strong" if ratio >= 2 else "light") if ratio >= 1 else None,
                        type=device_type(ssids[b], b), vendor=None, connected=False, new=False,
                        channel=None, band=None))
    return dict(ts=str(t), epoch=ts_epoch, aps=sorted(aps, key=lambda a: a["bssid"]),
                room=r[0] if r else None, events=near, has_data=bool(aps))


def trail(recent, minutes):
    """Disturbed paths over the last `minutes`, bucketed per 20 s for the floor-plan heat trail."""
    cutoff = time.time() - minutes * 60
    buckets = {}
    for t, _, _, rr in recent:
        if t < cutoff:
            continue
        for b, (ratio, state) in rr.items():
            if state in ("moving", "still", "blocked"):
                k = (b, int(t // 20))
                if ratio >= buckets.get(k, (0, "", 0))[0]:
                    buckets[k] = (ratio, state, t)
    return [dict(bssid=b, t=v[2], state=v[1], ratio=round(v[0], 2)) for (b, _), v in buckets.items()]
