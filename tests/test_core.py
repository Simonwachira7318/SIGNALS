"""
Unit tests for Wi-Fi Sense's pure logic (no Wi-Fi, MySQL or network needed).

    python -m unittest discover -s tests -v
"""

import math
import os
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class PresenceTests(unittest.TestCase):
    def run_for(self, f, t, sec, **kw):
        events = []
        for _ in range(int(sec / 1.5)):
            t += 1.5
            c = f.update(t, 300, kw.get("motion", False), kw.get("idx", 0), kw.get("nm", 0),
                         kw.get("still", 0), False, kw.get("ml"))
            if c:
                events.append(c[0])
        return t, events

    def test_walk_in_hold_then_empty(self):
        from presence import PresenceFusion
        f = PresenceFusion()
        t, ev = self.run_for(f, 0, 30)
        self.assertEqual(f.state, "empty")
        t, ev = self.run_for(f, t, 10, motion=True, idx=1.5, nm=1)
        self.assertEqual(ev, ["occupied"])
        t, ev = self.run_for(f, t, 240)                       # sits still, within the 5-min hold
        self.assertEqual(f.state, "occupied")
        t, ev = self.run_for(f, t, 600)
        self.assertEqual(ev, ["empty"])

    def test_never_claims_certainty(self):
        from presence import PresenceFusion
        f = PresenceFusion()
        self.run_for(f, 0, 20, motion=True, idx=5, nm=3)
        self.assertLess(f.p, 1.0)

    def test_ai_empty_ends_hold_early(self):
        from presence import PresenceFusion
        f = PresenceFusion()
        t, _ = self.run_for(f, 0, 10, motion=True, idx=1.5, nm=1)
        t, ev = self.run_for(f, t, 90, ml=dict(label="Empty room", prob=0.95))
        self.assertEqual(ev, ["empty"])


class ClassifierTests(unittest.TestCase):
    def test_train_and_predict(self):
        import classifier
        with tempfile.TemporaryDirectory() as d:
            classifier.MODEL_FILE = Path(d) / "m.json"
            m = classifier.ActivityModel()
            rnd = random.Random(1)
            samples = [("Empty", [rnd.gauss(0.2, 0.05), rnd.gauss(0, 0.1)]) for _ in range(30)] + \
                      [("Walking", [rnd.gauss(1.8, 0.2), rnd.gauss(1, 0.2)]) for _ in range(30)]
            ok, msg = m.train(samples, ["a", "b"], "now")
            self.assertTrue(ok, msg)
            self.assertEqual(m.predict([1.9, 1.1])[0], "Walking")
            self.assertEqual(m.predict([0.1, 0.0])[0], "Empty")
            self.assertTrue(classifier.MODEL_FILE.exists())

    def test_needs_two_labels(self):
        import classifier
        with tempfile.TemporaryDirectory() as d:
            classifier.MODEL_FILE = Path(d) / "m.json"
            ok, _ = classifier.ActivityModel().train([("Only", [1.0])] * 30, ["a"], "now")
            self.assertFalse(ok)


class EnvFileTests(unittest.TestCase):
    def test_parsing_and_precedence(self):
        import envfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('# c\nA_X=1\nB_X="two words"\nC_X=3 # trailing\nexport D_X=4\nE_X=file\n', encoding="utf-8")
            os.environ["E_X"] = "real"
            os.environ["F_X"] = ""
            p.write_text(p.read_text() + "F_X=fromfile\n")
            envfile._loaded = False
            envfile.load_env(p)
            self.assertEqual(os.environ["A_X"], "1")
            self.assertEqual(os.environ["B_X"], "two words")
            self.assertEqual(os.environ["C_X"], "3")
            self.assertEqual(os.environ["D_X"], "4")
            self.assertEqual(os.environ["E_X"], "real")       # real env wins
            self.assertEqual(os.environ["F_X"], "fromfile")   # ...unless empty


class AnalysisTests(unittest.TestCase):
    def test_walking_rhythm(self):
        from analysis import rhythm
        rnd = random.Random(2)
        walk = [-45 + 3 * math.sin(2 * math.pi * 1.8 * k / 10) + rnd.gauss(0, .3) for k in range(128)]
        r = rhythm(walk)
        self.assertEqual(r["verdict"], "walking-like rhythm")
        self.assertAlmostEqual(r["dom_hz"], 1.8, delta=0.25)

    def test_slow_driver_is_reported(self):
        from analysis import rhythm
        stepped, v = [], -45
        for k in range(128):
            if k % 7 == 0:
                v = -45 + (k % 3) * 3
            stepped.append(v)
        r = rhythm(stepped)
        self.assertFalse(r["reliable"])

    def test_rate_features(self):
        from analysis import rate_features
        inst, drop = rate_features([144, 144, 144, 144, 58])
        self.assertGreater(inst, 0.2)
        self.assertGreater(drop, 0.5)


class AnomalyTests(unittest.TestCase):
    def test_learning_then_alert(self):
        from anomaly import UnusualDetector
        u = UnusualDetector()
        when = datetime(2026, 9, 27, 3, 10)                    # Sunday 03:10
        u.days = 3
        self.assertIsNone(u.check(when))                       # still learning
        u.days = 14
        u.slots = {(6, 3): dict(hours=8.0, episodes=0), (6, 20): dict(hours=8.0, episodes=30)}
        msg, conf = u.check(when)
        self.assertIn("usually quiet", msg)
        self.assertGreater(conf, 0.3)
        self.assertIsNone(u.check(when.replace(hour=20)))      # busy slot: not unusual


class RulesTests(unittest.TestCase):
    def test_validation(self):
        from rules import validate
        ok = validate(dict(name="x", trig="motion", action="webhook", target="https://h/a", cooldown_s=5))
        self.assertEqual(ok["action"], "webhook")
        for bad in (dict(trig="nope", action="desktop"), dict(trig="motion", action="webhook", target="ftp://x"),
                    dict(trig="motion", action="desktop", start_hm="22:00"),
                    dict(trig="motion", action="desktop", start_hm="25:00", end_hm="01:00")):
            with self.assertRaises(ValueError):
                validate(bad)


class ReplayTests(unittest.TestCase):
    def test_episodes_merge_within_gap(self):
        from replay import episodes
        t0 = datetime(2026, 1, 1)
        times = [t0 + timedelta(seconds=1.5 * i) for i in range(100)]
        wob = [{"ap": 9.0 if i in (10, 15, 60) else 0.5} for i in range(100)]
        th = {"ap": 4.0}
        self.assertEqual(len(episodes(times, wob, th, 1.0, 30)), 2)   # 10 & 15 merge; 60 separate
        self.assertEqual(len(episodes(times, wob, th, 3.0, 30)), 0)   # less sensitive: none


class SettingsTests(unittest.TestCase):
    def test_ranges_and_validation(self):
        import alerts
        with tempfile.TemporaryDirectory() as d:
            alerts.SETTINGS_FILE = Path(d) / "s.json"
            s = alerts.Settings()
            r = s.update(dict(presence_hold_min=999, motion_threshold_scale=9, episode_gap_s=1, bogus=1))
            self.assertEqual(r["presence_hold_min"], 60)
            self.assertEqual(r["motion_threshold_scale"], 4.0)
            self.assertEqual(r["episode_gap_s"], 5)
            self.assertNotIn("bogus", r)
            with self.assertRaises(ValueError):
                s.update(dict(daily_report_time="24:30"))


class OuiTests(unittest.TestCase):
    def test_randomized_mac(self):
        import oui
        self.assertEqual(oui.vendor("1a:1d:ea:2d:84:c1"), "Randomized MAC")

    @unittest.skipUnless(Path(__file__).resolve().parents[1].joinpath("oui_manuf.txt").exists(), "no vendor db")
    def test_known_vendor(self):
        import oui
        self.assertIn("TP-Link", oui.vendor("3c:6a:d2:b8:40:08") or "")


class FloorplanTests(unittest.TestCase):
    def test_image_validation(self):
        import base64
        import wifi_web
        with tempfile.TemporaryDirectory() as d:
            wifi_web.APP_DIR = Path(d)
            wifi_web.FLOORPLAN_JSON = Path(d) / "fp.json"
            with self.assertRaises(ValueError):
                wifi_web.floorplan_set_image("data:x;base64," + base64.b64encode(b"<svg onload=x>").decode())
            png = bytes.fromhex("89504e470d0a1a0a") + b"\0" * 20
            name = wifi_web.floorplan_set_image("data:image/png;base64," + base64.b64encode(png).decode())
            self.assertTrue(name.startswith("floorplan_image.png"))

    def test_node_report_sanitised(self):
        import wifi_web
        r = wifi_web.clean_node_report(dict(name="<b>kitchen</b>", paths=[dict(bssid="AA:BB:CC:DD:EE:FF", state="evil"),
                                                                          dict(bssid="not-a-mac")]))
        self.assertEqual(r["name"], "bkitchenb")
        self.assertEqual(len(r["paths"]), 1)
        self.assertEqual(r["paths"][0]["state"], "stable")


if __name__ == "__main__":
    unittest.main()
