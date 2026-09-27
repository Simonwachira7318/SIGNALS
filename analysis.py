"""
analysis.py  -  Movement-rhythm (frequency) and connection-speed features.

rhythm()
    Takes the last ~13 s of live-link RSSI samples (~10/s) and computes a
    small spectrum (pure-Python DFT, no numpy). Walking modulates the signal
    at roughly 1-2.5 Hz (step rate); fidgeting and fans look different.
    It also measures how often the Wi-Fi driver really refreshes the RSSI:
    many drivers only update it 1-2x per second, and then step rhythm above
    ~1 Hz can't be seen. The result says so instead of guessing.

rate_features()
    Download/upload link-rate instability: bodies moving near the router make
    the rate controller step up and down.
"""

import math
import statistics

BANDS = {"slow": (0.2, 0.8), "gait": (0.8, 2.5), "fast": (2.5, 5.0)}
N_BINS = 24                 # spectrum bars shown in the UI (0.2-5 Hz)


def rhythm(samples, fs=10.0):
    """samples: [dbm, ...] at ~fs Hz, oldest first. Returns a dict (JSON-ready)."""
    n = len(samples)
    if n < int(fs * 4):
        return dict(ok=False, reason="collecting samples", n=n)
    changes = sum(1 for a, b in zip(samples, samples[1:]) if a != b)
    update_hz = changes / (n / fs)
    mean = sum(samples) / n
    # remove linear trend, apply a Hann window
    xs = range(n)
    xm = (n - 1) / 2
    sxx = sum((x - xm) ** 2 for x in xs)
    slope = sum((x - xm) * (v - mean) for x, v in zip(xs, samples)) / sxx
    y = [(v - mean - slope * (x - xm)) * (0.5 - 0.5 * math.cos(2 * math.pi * x / (n - 1)))
         for x, v in zip(xs, samples)]
    std_db = statistics.pstdev(samples)

    def power(f):
        w = 2 * math.pi * f / fs
        re = sum(v * math.cos(w * k) for k, v in enumerate(y))
        im = sum(v * math.sin(w * k) for k, v in enumerate(y))
        return (re * re + im * im) / n

    f_lo, f_hi = 0.2, min(5.0, fs / 2)
    freqs = [f_lo + (f_hi - f_lo) * i / (N_BINS - 1) for i in range(N_BINS)]
    spec = [power(f) for f in freqs]
    total = sum(spec) or 1e-9
    bands = {b: sum(p for f, p in zip(freqs, spec) if lo <= f < hi) / total for b, (lo, hi) in BANDS.items()}
    dom = freqs[max(range(N_BINS), key=spec.__getitem__)]
    reliable = update_hz >= 3.0
    moving = std_db >= 0.8
    if not moving:
        verdict = "steady"
    elif not reliable:
        verdict = "movement (rhythm not measurable)"
    elif bands["gait"] >= 0.45 and 0.8 <= dom <= 2.5:
        verdict = "walking-like rhythm"
    elif bands["slow"] >= 0.5:
        verdict = "slow movement / shifting"
    else:
        verdict = "irregular movement"
    peak = max(spec) or 1e-9
    return dict(ok=True, n=n, update_hz=round(update_hz, 1), reliable=reliable, std_db=round(std_db, 2),
                dom_hz=round(dom, 2), bands={k: round(v, 3) for k, v in bands.items()},
                spectrum=[round(p / peak, 3) for p in spec], freqs=[round(f, 2) for f in freqs],
                verdict=verdict)


def rate_features(rx_hist):
    """rx_hist: recent receive rates (Mbps), oldest first. -> (instability 0-1+, drop 0-1)."""
    vals = [v for v in rx_hist if v]
    if len(vals) < 5:
        return 0.0, 0.0
    med = statistics.median(vals)
    inst = statistics.pstdev(vals) / med if med else 0.0
    drop = max(0.0, 1 - vals[-1] / med) if med else 0.0
    return round(inst, 3), round(drop, 3)
