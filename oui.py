"""
oui.py  -  Offline MAC-address vendor lookup (e.g. "TP-Link", "Huawei").

Uses Wireshark's `manuf` file, saved next to this script as oui_manuf.txt.
Refresh it with:  python oui.py --update
"""

import sys
import urllib.request

MANUF_URL = "https://www.wireshark.org/download/automated/data/manuf"
from paths import APP_DIR, resource
MANUF_FILE = resource("oui_manuf.txt")
MANUF_SAVE = APP_DIR / "oui_manuf.txt"     # --update writes here

_table = None    # {bits: {prefix_int: vendor}}


def _load():
    global _table
    _table = {}
    if not MANUF_FILE.exists():
        return
    for line in MANUF_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        prefix, short = parts[0].strip(), parts[1].strip()
        vendor = parts[2].strip() if len(parts) > 2 and parts[2].strip() else short
        bits = 24
        if "/" in prefix:
            prefix, b = prefix.split("/")
            bits = int(b)
        hexs = prefix.replace(":", "").replace("-", "").replace(".", "")
        try:
            val = int(hexs, 16) << (48 - 4 * len(hexs))
        except ValueError:
            continue
        _table.setdefault(bits, {})[val >> (48 - bits)] = vendor


def vendor(bssid):
    """Vendor name for a MAC, 'Randomized MAC' for locally administered ones, else None."""
    if _table is None:
        _load()
    try:
        mac = int(bssid.replace(":", ""), 16)
    except ValueError:
        return None
    if (mac >> 40) & 0x02:
        return "Randomized MAC"
    for bits in sorted(_table, reverse=True):          # most specific prefix first
        v = _table[bits].get(mac >> (48 - bits))
        if v:
            return v
    return None


def available():
    return MANUF_FILE.exists()


def update():
    print(f"Downloading {MANUF_URL} ...")
    with urllib.request.urlopen(MANUF_URL, timeout=300) as r:
        data = r.read()
    MANUF_SAVE.write_bytes(data)
    print(f"Saved {len(data) // 1024} KB to {MANUF_SAVE}")


if __name__ == "__main__":
    if "--update" in sys.argv:
        update()
    else:
        for mac in sys.argv[1:] or ["d8:32:14:73:1e:41"]:
            print(mac, "->", vendor(mac))
