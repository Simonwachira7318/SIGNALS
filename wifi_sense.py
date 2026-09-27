#!/usr/bin/env python3
"""
wifi_sense.py  -  Passive Wi-Fi motion detector + network counter for Windows.

WHAT IT DOES
------------
Uses your laptop's own Wi-Fi adapter (no Arduino, no extra hardware) to
repeatedly scan the surrounding access points via Windows' built-in `netsh`.
For each nearby access point it reads the signal strength (RSSI). Because human
bodies absorb and reflect 2.4/5 GHz, people moving through the space make those
signal levels wobble. We measure that wobble to flag MOTION, and we report how
many access points / networks are currently visible.

This is 100% passive and receive-only: it never transmits, never jams, and only
reads information Windows already exposes. It cannot see or count other people's
phones (that needs monitor mode, which normal Windows adapters don't support) --
it senses movement indirectly through the ambient signal environment.

USAGE
-----
    python wifi_sense.py

Press Ctrl+C to stop.

Tip: results are best when you stay still and let others move. Give it ~10
seconds at startup to learn the "quiet" baseline for each access point.
"""

import re
import time
import subprocess
from collections import defaultdict, deque

# --- Tunables -------------------------------------------------------------
SCAN_INTERVAL   = 1.5    # seconds between scans (netsh scans are slow; <1s is pointless)
HISTORY_LEN     = 8      # how many recent samples per BSSID we keep for variance
MOTION_STD_DB   = 4.0    # signal std-dev (in %) above this on any AP => "motion"
BASELINE_SCANS  = 6      # scans to collect before we start judging motion
# -------------------------------------------------------------------------


def scan_networks():
    """
    Run `netsh wlan show networks mode=bssid` and parse it.
    Returns: dict {bssid: signal_percent}
    Windows reports signal as a percentage (0-100), not dBm. That's fine --
    we only care about how much it *changes*.
    """
    try:
        raw = subprocess.run(
            ["netsh", "wlan", "show", "networks", "mode=bssid"],
            capture_output=True, timeout=15
        ).stdout
        # netsh emits OEM/localized bytes; decode leniently so odd SSIDs
        # (emoji, non-Latin names) never crash the parser.
        out = raw.decode("utf-8", errors="replace")
        if out.count("�") > len(out) // 20:  # lots of replacements => wrong codec
            out = raw.decode("cp1252", errors="replace")
    except Exception as e:
        print(f"[!] scan failed: {e}")
        return {}

    results = {}
    current_ssid = None
    current_bssid = None

    for line in out.splitlines():
        line = line.strip()

        # SSID line, e.g. "SSID 3 : MyHomeWiFi"
        m = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", line)
        if m:
            current_ssid = m.group(1).strip() or "<hidden>"
            continue

        # BSSID line, e.g. "BSSID 1 : a4:b1:c2:d3:e4:f5"
        m = re.match(r"^BSSID\s+\d+\s*:\s*([0-9a-fA-F:]{17})$", line)
        if m:
            current_bssid = m.group(1).lower()
            continue

        # Signal line, e.g. "Signal : 78%"
        m = re.match(r"^Signal\s*:\s*(\d+)%$", line)
        if m and current_bssid:
            results[current_bssid] = int(m.group(1))

    return results


def main():
    print(__doc__.split("USAGE")[0].strip())
    print("\nStarting passive Wi-Fi sensing.  Ctrl+C to stop.\n")

    history = defaultdict(lambda: deque(maxlen=HISTORY_LEN))  # bssid -> recent signals
    scan_count = 0

    try:
        while True:
            nets = scan_networks()
            scan_count += 1

            for bssid, sig in nets.items():
                history[bssid].append(sig)

            # Drop APs we haven't seen this round from consideration for motion,
            # but keep their history briefly (they may reappear).
            active = {b: list(h) for b, h in history.items() if len(h) >= 3}

            # Compute the biggest signal fluctuation across all active APs.
            worst_std = 0.0
            worst_ap = None
            for bssid, samples in active.items():
                n = len(samples)
                mean = sum(samples) / n
                std = (sum((s - mean) ** 2 for s in samples) / n) ** 0.5
                if std > worst_std:
                    worst_std = std
                    worst_ap = bssid

            num_aps = len(nets)

            if scan_count <= BASELINE_SCANS:
                print(f"[baseline {scan_count}/{BASELINE_SCANS}] "
                      f"learning environment... APs visible: {num_aps}")
            else:
                motion = worst_std >= MOTION_STD_DB
                flag = "  >>> MOTION <<<" if motion else "  (still)"
                ap_short = worst_ap[-8:] if worst_ap else "n/a"
                print(f"APs: {num_aps:2d} | max signal wobble: {worst_std:4.1f}%"
                      f" (AP ...{ap_short}){flag}")

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopped. No signals were transmitted at any point.")


if __name__ == "__main__":
    main()
