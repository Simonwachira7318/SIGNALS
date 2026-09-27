#!/usr/bin/env python3
"""
node.py  -  Turn another Windows laptop into a remote Wi-Fi Sense sensor.

Run this on a second computer (e.g. in the kitchen). It does the same passive
sensing as the main dashboard (no database needed) and reports its signal
paths to the main computer (the "hub") every scan. The hub then shows
per-room movement ("Movement in kitchen") and draws this node's paths on the
floor plan, where overlapping disturbed paths narrow down *where* the movement is.

On the HUB's .env:
    WIFI_SENSE_BIND=0.0.0.0                 # listen on the local network
    WIFI_SENSE_ACCESS_TOKEN=<long random>   # required once it's reachable

On the NODE (copy this folder over, or the .exe), .env or arguments:
    python node.py --hub http://192.168.0.10:8765 --name kitchen --token <same token>

Passive / receive-only sensing, like the hub. Sending the reports is normal
network traffic.
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import defaultdict, deque

import envfile  # noqa: F401
from wifi_web import (scan_full, link_info, LinkSampler, percent_to_dbm, _std, _clamp,
                      NOISE_MULT, AP_TH_RANGE, LINK_TH_RANGE, LINK_WINDOW_S, HISTORY_LEN, SCAN_INTERVAL)

WARMUP_SCANS = 6


def main():
    ap = argparse.ArgumentParser(description="Wi-Fi Sense remote sensor node")
    ap.add_argument("--hub", default=os.environ.get("WIFI_SENSE_HUB", ""), help="e.g. http://192.168.0.10:8765")
    ap.add_argument("--name", default=os.environ.get("WIFI_SENSE_NODE_NAME", os.environ.get("COMPUTERNAME", "node")))
    ap.add_argument("--token", default=os.environ.get("WIFI_SENSE_ACCESS_TOKEN", ""))
    args = ap.parse_args()
    if not args.hub:
        sys.exit("Give the hub address: --hub http://<hub-ip>:8765 (or WIFI_SENSE_HUB in .env)")
    if len(args.token) < 16:
        sys.exit("Give the hub's access token: --token ... (or WIFI_SENSE_ACCESS_TOKEN in .env)")
    url = args.hub.rstrip("/") + "/api/node/report"
    name = args.name.strip()[:32] or "node"

    link = LinkSampler(); link.start()
    wob = defaultdict(lambda: deque(maxlen=HISTORY_LEN))
    noise = defaultdict(lambda: 4.0 / NOISE_MULT)
    link_noise = 2.0 / NOISE_MULT
    scan_no, fails = 0, 0
    print(f"Node '{name}' reporting to {url}  (Ctrl+C to stop)")
    while True:
        t0 = time.time()
        scan_no += 1
        warming = scan_no <= WARMUP_SCANS
        nets, li = scan_full(), link_info()
        if li and li.get("pct") is not None:
            if li["bssid"] in nets:
                nets[li["bssid"]]["pct"] = li["pct"]
            link.add_slow(percent_to_dbm(li["pct"]))
        lw = link.window(LINK_WINDOW_S) if li else []
        link_std = _std(lw) if len(lw) >= 3 else 0.0
        link_th = _clamp(NOISE_MULT * link_noise + 0.5, *LINK_TH_RANGE)
        link_ratio = link_std / link_th if li else 0.0
        if li and link_ratio < 1:
            link_noise += (0.2 if warming else 0.02) * (link_std - link_noise)

        paths = []
        for b, info in nets.items():
            h = wob[b]
            h.append(info["pct"])
            w = statistics.pstdev(h) if len(h) >= 3 else 0.0
            th = _clamp(NOISE_MULT * noise[b] + 1.0, *AP_TH_RANGE)
            ratio = w / th
            if li and b == li.get("bssid") and lw:
                ratio = link_ratio
            moving = not warming and ratio >= 1.0
            if not moving and len(h) >= 3:
                noise[b] += (0.2 if warming else 0.02) * (w - noise[b])
            paths.append(dict(bssid=b, ssid=info["ssid"], pct=info["pct"], ratio=round(ratio, 2),
                              state="calibrating" if warming else "moving" if moving else "stable"))
        idx = max([p["ratio"] for p in paths] + [link_ratio])
        report = dict(name=name, ts=time.strftime("%Y-%m-%d %H:%M:%S"), warming=warming,
                      motion=any(p["state"] == "moving" for p in paths), idx=round(idx, 2), paths=paths,
                      link=dict(bssid=(li or {}).get("bssid"), ssid=(li or {}).get("ssid"),
                                dbm=lw[-1] if lw else None, ratio=round(link_ratio, 2)))
        try:
            req = urllib.request.Request(url, data=json.dumps(report).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Access-Token": args.token})
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read()
            if fails:
                print("reconnected to hub")
            fails = 0
        except Exception as e:
            fails += 1
            if fails in (1, 10) or fails % 100 == 0:
                print(f"[!] could not reach hub ({type(e).__name__}: {getattr(e, 'code', '') or e}); retrying")
        time.sleep(max(0.0, SCAN_INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stopped")
