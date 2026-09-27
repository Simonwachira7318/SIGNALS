"""
classifier.py  -  Tiny trainable activity classifier (Gaussian Naive Bayes).

Pure Python, no extra packages. You label what's happening in the room from
the dashboard ("Empty", "Still", "Walking", ...) while it records feature
vectors; "Train" fits one Gaussian per feature per label. Live predictions
then say e.g. "Walking 81%".

Features (one vector per scan) are defined by FEATURES in wifi_web.py.
"""

import json
import math

from paths import APP_DIR
MODEL_FILE = APP_DIR / "activity_model.json"
MIN_PER_LABEL = 20        # samples needed per label before training
VAR_FLOOR = 1e-3


class ActivityModel:
    def __init__(self):
        self.labels, self.stats, self.priors, self.features = [], {}, {}, []
        self.trained_at, self.counts = None, {}
        self.load()

    @property
    def ready(self):
        return len(self.labels) >= 2

    def load(self):
        if MODEL_FILE.exists():
            try:
                d = json.loads(MODEL_FILE.read_text())
                self.labels, self.stats, self.priors = d["labels"], d["stats"], d["priors"]
                self.features, self.trained_at, self.counts = d["features"], d.get("trained_at"), d.get("counts", {})
            except Exception as e:
                print(f"[!] could not load {MODEL_FILE.name}: {e}")

    def train(self, samples, features, trained_at):
        """samples: list of (label, [floats]). Returns (ok, message)."""
        by = {}
        for label, x in samples:
            by.setdefault(label, []).append(x)
        counts = {l: len(v) for l, v in by.items()}
        usable = {l: v for l, v in by.items() if len(v) >= MIN_PER_LABEL}
        if len(usable) < 2:
            return False, (f"Need at least 2 labels with {MIN_PER_LABEL}+ samples each "
                           f"(have: {', '.join(f'{l} {n}' for l, n in counts.items()) or 'none'})")
        total = sum(len(v) for v in usable.values())
        stats, priors = {}, {}
        for label, xs in usable.items():
            cols = list(zip(*xs))
            means = [sum(c) / len(c) for c in cols]
            vars_ = [max(VAR_FLOOR, sum((v - m) ** 2 for v in c) / len(c)) for c, m in zip(cols, means)]
            stats[label] = dict(mean=means, var=vars_)
            priors[label] = len(xs) / total
        self.labels, self.stats, self.priors = sorted(usable), stats, priors
        self.features, self.trained_at, self.counts = list(features), trained_at, counts
        MODEL_FILE.write_text(json.dumps(dict(
            labels=self.labels, stats=stats, priors=priors, features=self.features,
            trained_at=trained_at, counts=counts), indent=1))
        skipped = [l for l in by if l not in usable]
        return True, (f"Trained on {total} samples ({', '.join(f'{l} {counts[l]}' for l in self.labels)})"
                      + (f"; skipped {', '.join(skipped)} (too few)" if skipped else ""))

    def predict(self, x):
        """-> (label, probability 0-1, {label: prob}) or None if untrained."""
        if not self.ready or len(x) != len(self.features):
            return None
        logp = {}
        for label in self.labels:
            s = self.stats[label]
            lp = math.log(self.priors[label])
            for v, m, var in zip(x, s["mean"], s["var"]):
                lp += -0.5 * math.log(2 * math.pi * var) - (v - m) ** 2 / (2 * var)
            logp[label] = lp
        top = max(logp.values())
        exp = {l: math.exp(v - top) for l, v in logp.items()}
        z = sum(exp.values())
        probs = {l: e / z for l, e in exp.items()}
        best = max(probs, key=probs.get)
        return best, probs[best], probs

    def reset(self):
        self.labels, self.stats, self.priors, self.features = [], {}, {}, []
        self.trained_at, self.counts = None, {}
        if MODEL_FILE.exists():
            MODEL_FILE.unlink()
