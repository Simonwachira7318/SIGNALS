"""
presence.py  -  Fuses all signals into one steady "room occupied / empty" state.

Evidence each scan (strongest wins):
    movement on any path ........... 0.90-0.97
    AI model says a non-empty label  0.55-0.95 (when >= 70% sure)
    still presence / blocked path .. 0.70
    Bluetooth devices came close ... 0.60

Probability jumps up on evidence, then HOLDS for `hold_s` seconds after the
last evidence (people sit still) and decays after that. An "Empty ..." AI
prediction speeds the decay. Hysteresis: occupied at >= 0.6, empty below 0.3,
so the state doesn't flicker.
"""

import math
import time
from collections import deque

OCCUPIED_AT, EMPTY_AT = 0.6, 0.3
DECAY_TAU_S = 120.0          # decay time constant once the hold time has passed
FLOOR = 0.05
TIMELINE_S = 3600


class PresenceFusion:
    def __init__(self):
        self.p = 0.2
        self.state = None            # None until calibrated, then "occupied" / "empty"
        self.since = time.time()
        self.last_evidence = None    # time of last evidence
        self.p_at_evidence = self.p
        self.last_reason = None
        self.timeline = deque()      # (t, state)

    def update(self, now, hold_s, motion, idx_all, n_moving, n_still, ble_jump, ml):
        """Returns ("occupied"|"empty", p, duration_of_previous_state_s) on a change, else None."""
        ev, reason = 0.0, None
        if motion:
            ev = 0.9 + 0.07 * min(1.0, idx_all - 1)      # capped below 100%: RF sensing is never certain
            reason = f"Movement on {n_moving} path{'s' if n_moving != 1 else ''}"
        ml_empty = False
        if ml and ml["prob"] >= 0.7:
            if "empty" in ml["label"].lower():
                ml_empty = ml["prob"] >= 0.8
            else:
                e = 0.55 + 0.4 * (ml["prob"] - 0.7) / 0.3
                if e > ev:
                    ev, reason = e, f"AI: {ml['label']} {ml['prob'] * 100:.0f}%"
        if n_still and 0.7 > ev:
            ev, reason = 0.7, f"Steady change on {n_still} path{'s' if n_still != 1 else ''}"
        if ble_jump and 0.6 > ev:
            ev, reason = 0.6, "Bluetooth devices came close"

        if ev > 0 and not (ml_empty and not motion):
            self.p = max(self.p, ev) if ev < self.p else ev
            self.last_evidence, self.p_at_evidence, self.last_reason = now, self.p, reason
        elif self.last_evidence is not None:
            t = now - self.last_evidence
            if t > hold_s:
                self.p = max(FLOOR, self.p_at_evidence * math.exp(-(t - hold_s) / DECAY_TAU_S))
            if ml_empty:
                self.p = max(FLOOR, self.p * 0.85)
                self.p_at_evidence = self.p
        else:
            self.p = max(FLOOR, self.p * (0.85 if ml_empty else 0.99))

        change = None
        if self.state is None:                             # first decision: silent
            self.state = "occupied" if self.p >= OCCUPIED_AT else "empty"
            self.since = now
        elif self.state != "occupied" and self.p >= OCCUPIED_AT:
            change = ("occupied", self.p, now - self.since)
            self.state, self.since = "occupied", now
        elif self.state != "empty" and self.p < EMPTY_AT:
            change = ("empty", self.p, now - self.since)
            self.state, self.since = "empty", now

        self.timeline.append((now, self.state))
        while self.timeline and self.timeline[0][0] < now - TIMELINE_S:
            self.timeline.popleft()
        return change

    def view(self, now, hold_s):
        """JSON-ready summary for the dashboard."""
        segs = []
        for t, s in self.timeline:                          # compress to segments
            if segs and segs[-1]["s"] == s:
                segs[-1]["b"] = t
            else:
                segs.append(dict(s=s, a=t, b=t))
        reasons = []
        if self.last_evidence is not None:
            ago = now - self.last_evidence
            reasons.append(f"Last evidence: {self.last_reason} ({_fmt(ago)} ago)")
            if self.state == "occupied":
                left = hold_s - ago
                reasons.append(f"Holding for {_fmt(left)} more" if left > 0 else "Fading: no evidence recently")
        else:
            reasons.append("No evidence of anyone yet this session")
        return dict(state=self.state, p=round(self.p, 3), since=self.since,
                    reasons=reasons, timeline=segs, hold_s=hold_s, window_s=TIMELINE_S)


def _fmt(sec):
    sec = max(0, int(sec))
    return f"{sec // 60} min {sec % 60} s" if sec >= 60 else f"{sec} s"
