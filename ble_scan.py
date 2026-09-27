"""
ble_scan.py  -  Passive Bluetooth LE presence counter (optional).

Listens for BLE advertisements from phones, watches, earbuds, etc. using
bleak in *passive* scanning mode (receive-only, no scan requests are sent).
Needs:  python -m pip install bleak

Caveat: phones rotate their Bluetooth address every ~15 min for privacy, so
one phone can briefly count as two. Treat counts as a trend, not a headcount.
"""

import asyncio
import threading
import time

try:
    from bleak import BleakScanner
except Exception:            # bleak not installed / no BT stack
    BleakScanner = None

WINDOW_S = 30                # a device counts as present if heard in the last 30 s
NEAR_DBM = -65               # "near" = roughly same room / within a few metres

COMPANIES = {                # Bluetooth SIG company identifiers (common ones)
    0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung", 0x00E0: "Google",
    0x038F: "Xiaomi", 0x027D: "Huawei", 0x0157: "Huami", 0x0087: "Garmin",
    0x0310: "Tecno/Infinix", 0x072F: "OnePlus", 0x0059: "Nordic", 0x000F: "Broadcom",
    0x0171: "Amazon", 0x02E5: "Espressif", 0x0131: "Cypress", 0x012D: "Sony",
}


class BLEPresence(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.devices = {}              # address -> dict(rssi, seen, name, vendor)
        self.lock = threading.Lock()
        self.error = None if BleakScanner else "bleak not installed"
        self.running = False

    @property
    def available(self):
        return BleakScanner is not None

    def _on_adv(self, device, adv):
        vendor = None
        for cid in (adv.manufacturer_data or {}):
            vendor = COMPANIES.get(cid, f"0x{cid:04X}")
            break
        with self.lock:
            self.devices[device.address] = dict(
                rssi=adv.rssi, seen=time.time(),
                name=adv.local_name or device.name or None, vendor=vendor)

    async def _main(self):
        try:
            scanner = BleakScanner(detection_callback=self._on_adv, scanning_mode="passive")
        except Exception:
            # some adapters/drivers don't support passive mode filters
            scanner = BleakScanner(detection_callback=self._on_adv)
        async with scanner:
            self.running = True
            while True:
                await asyncio.sleep(5)
                cutoff = time.time() - 5 * WINDOW_S
                with self.lock:
                    for a in [a for a, d in self.devices.items() if d["seen"] < cutoff]:
                        del self.devices[a]

    def run(self):
        if not self.available:
            return
        while True:           # retry, e.g. after the user switches Bluetooth on
            try:
                asyncio.run(self._main())
            except Exception as e:
                msg = str(e)
                self.error = ("Bluetooth is switched off" if "powered on" in msg.lower()
                              else f"{type(e).__name__}: {msg[:120]}")
                self.running = False
            time.sleep(30)

    def snapshot(self):
        now = time.time()
        with self.lock:
            live = [dict(d, address=a) for a, d in self.devices.items() if now - d["seen"] <= WINDOW_S]
        live.sort(key=lambda d: -d["rssi"])
        near = [d for d in live if d["rssi"] >= NEAR_DBM]
        vendors = {}
        for d in live:
            vendors[d["vendor"] or "Unknown"] = vendors.get(d["vendor"] or "Unknown", 0) + 1
        return dict(
            available=self.available, running=self.running, error=self.error,
            total=len(live), near=len(near),
            vendors=sorted(vendors.items(), key=lambda kv: -kv[1])[:6],
            top=[dict(name=d["name"], vendor=d["vendor"], rssi=d["rssi"]) for d in live[:8]],
        )


if __name__ == "__main__":
    b = BLEPresence()
    b.start()
    for _ in range(4):
        time.sleep(5)
        print(b.snapshot())
