"""
rules.py  -  Automation rules: "when <event> [and conditions] then <action>".

Triggers   any detection kind (motion, unusual, occupied, empty, new_device,
           arrived, security, health, room_motion, blocked, still, ...) or "any".
Conditions away mode on/off/any, a time window (e.g. 22:00-06:00), cooldown.
Actions    webhook  POST JSON to a URL (e.g. a Home Assistant webhook to turn on lights)
           telegram send the event to your phone (needs Telegram in .env)
           desktop  Windows notification
           sound    play a beep pattern on this computer

Rules are stored in MySQL (`rules`) and edited on the Rules page.
"""

import json
import threading
import time
import urllib.request

from wifi_dashboard import db_connect, notify

TRIGGERS = ["any", "motion", "unusual", "occupied", "empty", "new_device", "arrived", "left",
            "security", "health", "room_motion", "blocked", "still", "ble", "calm"]
ACTIONS = ["webhook", "telegram", "desktop", "sound"]


def prepare(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                name        VARCHAR(80) NOT NULL,
                enabled     TINYINT(1) NOT NULL DEFAULT 1,
                trig        VARCHAR(20) NOT NULL,
                away        VARCHAR(4) NOT NULL DEFAULT 'any',
                start_hm    VARCHAR(5) NULL,
                end_hm      VARCHAR(5) NULL,
                action      VARCHAR(10) NOT NULL,
                target      VARCHAR(500) NULL,
                cooldown_s  INT NOT NULL DEFAULT 60,
                last_fired  DATETIME NULL,
                fired_count INT NOT NULL DEFAULT 0
            ) ENGINE=InnoDB
        """)
    conn.commit()


def _minutes(hm):
    h, m = hm.split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time {hm!r}")
    return h * 60 + m


def validate(r):
    """Clean a rule dict from the UI; raises ValueError on bad input."""
    out = dict(name=str(r.get("name") or "").strip()[:80] or "Rule",
               enabled=bool(r.get("enabled", True)),
               trig=str(r.get("trig")), away=str(r.get("away") or "any"),
               start_hm=(r.get("start_hm") or None), end_hm=(r.get("end_hm") or None),
               action=str(r.get("action")), target=(str(r.get("target") or "").strip()[:500] or None),
               cooldown_s=max(0, min(86400, int(r.get("cooldown_s") or 0))))
    if out["trig"] not in TRIGGERS:
        raise ValueError("unknown trigger")
    if out["away"] not in ("any", "on", "off"):
        raise ValueError("away must be any/on/off")
    if out["action"] not in ACTIONS:
        raise ValueError("unknown action")
    if bool(out["start_hm"]) != bool(out["end_hm"]):
        raise ValueError("set both start and end time, or neither")
    if out["start_hm"]:
        _minutes(out["start_hm"]); _minutes(out["end_hm"])
    if out["action"] == "webhook" and not (out["target"] or "").lower().startswith(("http://", "https://")):
        raise ValueError("webhook needs an http:// or https:// URL")
    return out


class RuleEngine:
    def __init__(self, settings, alerter):
        self.settings = settings
        self.alerter = alerter
        self.rules = []
        self.lock = threading.Lock()
        self.last_fire = {}            # id -> epoch (in-memory cooldown)
        self.reload()

    def reload(self):
        c = db_connect()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT id, name, enabled, trig, away, start_hm, end_hm, action, target, cooldown_s, "
                            "last_fired, fired_count FROM rules ORDER BY id")
                cols = [d[0] for d in cur.description]
                rules = [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            c.close()
        for r in rules:
            r["enabled"] = bool(r["enabled"])
            r["last_fired"] = str(r["last_fired"]) if r["last_fired"] else None
        with self.lock:
            self.rules = rules

    def list(self):
        with self.lock:
            return [dict(r) for r in self.rules]

    def save(self, data):
        r = validate(data)
        c = db_connect()
        try:
            with c.cursor() as cur:
                if data.get("id"):
                    cur.execute("UPDATE rules SET name=%s, enabled=%s, trig=%s, away=%s, start_hm=%s, end_hm=%s, "
                                "action=%s, target=%s, cooldown_s=%s WHERE id=%s",
                                (r["name"], int(r["enabled"]), r["trig"], r["away"], r["start_hm"], r["end_hm"],
                                 r["action"], r["target"], r["cooldown_s"], int(data["id"])))
                else:
                    cur.execute("INSERT INTO rules (name, enabled, trig, away, start_hm, end_hm, action, target, "
                                "cooldown_s) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                (r["name"], int(r["enabled"]), r["trig"], r["away"], r["start_hm"], r["end_hm"],
                                 r["action"], r["target"], r["cooldown_s"]))
            c.commit()
        finally:
            c.close()
        self.reload()

    def delete(self, rid):
        c = db_connect()
        try:
            with c.cursor() as cur:
                cur.execute("DELETE FROM rules WHERE id=%s", (int(rid),))
            c.commit()
        finally:
            c.close()
        self.reload()

    # ---------- evaluation ----------
    def _in_window(self, r):
        if not r["start_hm"]:
            return True
        now = time.localtime()
        cur = now.tm_hour * 60 + now.tm_min
        a, b = _minutes(r["start_hm"]), _minutes(r["end_hm"])
        return a <= cur < b if a <= b else cur >= a or cur < b

    def on_event(self, ev):
        """ev: dict(kind, severity, title, msg, ts). Called on the sensor thread; actions run async."""
        away = self.settings.get("away_mode")
        now = time.time()
        for r in self.list():
            if not r["enabled"] or (r["trig"] != "any" and r["trig"] != ev["kind"]):
                continue
            if (r["away"] == "on" and not away) or (r["away"] == "off" and away):
                continue
            if not self._in_window(r) or now - self.last_fire.get(r["id"], 0) < r["cooldown_s"]:
                continue
            # snoozed alerts also silence notification-type actions (webhooks/automations still run)
            if r["action"] in ("telegram", "desktop", "sound") and self.alerter.is_snoozed(ev["kind"]):
                continue
            self.last_fire[r["id"]] = now
            threading.Thread(target=self._fire, args=(r, ev), daemon=True).start()

    def test(self, rid):
        r = next((x for x in self.list() if x["id"] == int(rid)), None)
        if not r:
            raise KeyError("rule not found")
        ev = dict(kind=r["trig"] if r["trig"] != "any" else "motion", severity="info", title="Test event",
                  msg=f"Test of rule '{r['name']}'", ts=time.strftime("%Y-%m-%d %H:%M:%S"), test=True)
        return self._fire(r, ev)

    def _fire(self, r, ev):
        ok, err = True, None
        try:
            if r["action"] == "webhook":
                body = json.dumps(dict(source="wifi-sense", rule=r["name"], event=ev["kind"],
                                       severity=ev["severity"], title=ev["title"], message=ev["msg"],
                                       ts=ev["ts"], test=bool(ev.get("test")))).encode()
                req = urllib.request.Request(r["target"], data=body, method="POST",
                                             headers={"Content-Type": "application/json",
                                                      "User-Agent": "wifi-sense"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read(1024)
            elif r["action"] == "telegram":
                ok = self.alerter.send_phone(f"{ev['title']}\n{ev['msg']}", wait=True)
                err = None if ok else self.alerter.last_error
            elif r["action"] == "desktop":
                notify(ev["title"], ev["msg"])
            elif r["action"] == "sound":
                import winsound
                for f, d in ((880, 180), (660, 180), (880, 260)):
                    winsound.Beep(f, d)
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {str(e)[:150]}"
            print(f"[!] rule '{r['name']}' failed: {err}")
        if not ev.get("test"):
            try:
                c = db_connect()
                with c.cursor() as cur:
                    cur.execute("UPDATE rules SET last_fired=NOW(), fired_count=fired_count+1 WHERE id=%s", (r["id"],))
                c.commit(); c.close()
            except Exception:
                pass
        return dict(ok=ok, error=err)
