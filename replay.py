"""
replay.py  -  Replay recorded scans through the motion detector offline.

Answers "what would a different sensitivity have done?" using real data from
the `scans` table (raw rows are kept for the retention period, 7 days by
default). For each sensitivity scale in a sweep it counts motion episodes and,
where you've given 👍/👎 feedback, how many confirmed detections it would
keep and how many false alarms it would drop.

Approximation: the replay only has per-scan signal values (every ~1.5 s).
The live dashboard additionally samples your connected link 10x/second, so
live results are somewhat more sensitive than the replay.
"""

import statistics
from collections import defaultdict, deque

from wifi_dashboard import db_connect, HISTORY_LEN, SCAN_INTERVAL

AP_TH_RANGE = (2.0, 8.0)
NOISE_MULT = 3.0
SWEEP = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
MATCH_S = 20                      # a labelled detection counts as reproduced within this many seconds
MAX_HOURS = 72


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def load(hours):
    """-> (scan times [datetime], per-scan {bssid: wobble}) for the last `hours`."""
    c = db_connect()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT ts, bssid, signal_pct FROM scans WHERE ts >= NOW() - INTERVAL %s HOUR "
                        "ORDER BY ts, id", (hours,))
            by_ts = []
            last_ts = None
            for ts, b, pct in cur.fetchall():
                if ts != last_ts:
                    by_ts.append((ts, {}))
                    last_ts = ts
                by_ts[-1][1][b] = pct
            cur.execute("SELECT ts, feedback FROM detections WHERE kind='motion' AND feedback IS NOT NULL "
                        "AND ts >= NOW() - INTERVAL %s HOUR", (hours,))
            labels = [(ts, bool(fb)) for ts, fb in cur.fetchall()]
    finally:
        c.close()
    hist = defaultdict(lambda: deque(maxlen=HISTORY_LEN))
    times, wob = [], []
    for ts, readings in by_ts:
        w = {}
        for b, pct in readings.items():
            h = hist[b]
            h.append(pct)
            if len(h) >= 3:
                w[b] = statistics.pstdev(h)
        times.append(ts)
        wob.append(w)
    return times, wob, labels


def thresholds(wob, adaptive=True):
    """Per-path threshold from each path's median wobble (robust 'noise level')."""
    per = defaultdict(list)
    for w in wob:
        for b, v in w.items():
            per[b].append(v)
    if not adaptive:
        return {b: 4.0 for b in per}
    return {b: _clamp(NOISE_MULT * statistics.median(v) + 1.0, *AP_TH_RANGE) for b, v in per.items()}


def episodes(times, wob, th, scale, gap_s):
    """-> list of (start, end) motion episodes."""
    eps, cur_start, last_motion = [], None, None
    for ts, w in zip(times, wob):
        moving = any(v / (th[b] * scale) >= 1.0 for b, v in w.items())
        if moving:
            if cur_start is None or (ts - last_motion).total_seconds() > gap_s:
                if cur_start is not None:
                    eps.append((cur_start, last_motion))
                cur_start = ts
            last_motion = ts
    if cur_start is not None:
        eps.append((cur_start, last_motion))
    return eps


def _agreement(eps, labels):
    keep = drop = 0
    for ts, correct in labels:
        hit = any((s - _td(MATCH_S)) <= ts <= (e + _td(MATCH_S)) for s, e in eps)
        if correct and hit:
            keep += 1
        if not correct and not hit:
            drop += 1
    return keep, drop


def _td(s):
    from datetime import timedelta
    return timedelta(seconds=s)


def run(hours=24, gap_s=30, scale=1.0, adaptive=True):
    hours = int(_clamp(hours, 1, MAX_HOURS))
    times, wob, labels = load(hours)
    if len(times) < 20:
        return dict(ok=False, message="Not enough recorded scans in this period yet.")
    th = thresholds(wob, adaptive)
    n_conf = sum(1 for _, c in labels if c)
    n_false = len(labels) - n_conf
    sweep = []
    for sc in sorted(set(SWEEP + [round(scale, 3)])):
        eps = episodes(times, wob, th, sc, gap_s)
        keep, drop = _agreement(eps, labels)
        sweep.append(dict(scale=sc, episodes=len(eps),
                          motion_min=round(sum((e - s).total_seconds() + SCAN_INTERVAL for s, e in eps) / 60, 1),
                          keep_confirmed=keep, drop_false=drop))
    chosen = episodes(times, wob, th, scale, gap_s)
    hourly = defaultdict(int)
    for s, _ in chosen:
        hourly[s.strftime("%Y-%m-%d %H:00")] += 1
    span_h = (times[-1] - times[0]).total_seconds() / 3600
    return dict(ok=True, hours=hours, span_h=round(span_h, 1), scans=len(times), paths=len(th),
                thresholds={b: round(v, 2) for b, v in th.items()},
                labels=dict(confirmed=n_conf, false=n_false), sweep=sweep,
                chosen=dict(scale=scale, gap_s=gap_s, episodes=len(chosen),
                            hourly=sorted(hourly.items()),
                            first=str(times[0]), last=str(times[-1])),
                note="Replay uses per-scan values only; the live link's 10x/second sampling makes live "
                     "detection a little more sensitive than this.")
