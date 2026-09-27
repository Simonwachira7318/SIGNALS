"""
wlan_native.py  -  Fast, read-only access to the connected link's RSSI.

Uses the Windows Native Wi-Fi API (wlanapi.dll) via ctypes. It only *queries*
the driver for the current signal of the link you're already connected to, so
nothing is transmitted. A query takes well under a millisecond, versus
~100-300 ms for `netsh`, which makes high-rate sampling possible.

    from wlan_native import LinkRSSI
    r = LinkRSSI()          # raises OSError if wlanapi is unavailable
    r.rssi()                # -> int dBm, or None when not connected
"""

import ctypes
from ctypes import wintypes

_WLAN_INTF_OPCODE_RSSI = 0x10000102
_ERROR_SUCCESS = 0


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("InterfaceGuid", _GUID),
                ("strInterfaceDescription", ctypes.c_wchar * 256),
                ("isState", ctypes.c_uint)]


class _WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", wintypes.DWORD),
                ("dwIndex", wintypes.DWORD),
                ("InterfaceInfo", _WLAN_INTERFACE_INFO * 1)]


class LinkRSSI:
    def __init__(self):
        self._api = ctypes.WinDLL("wlanapi.dll")
        self._handle = wintypes.HANDLE()
        negotiated = wintypes.DWORD()
        if self._api.WlanOpenHandle(2, None, ctypes.byref(negotiated),
                                    ctypes.byref(self._handle)) != _ERROR_SUCCESS:
            raise OSError("WlanOpenHandle failed")
        self._guid = None

    def _interface(self):
        plist = ctypes.POINTER(_WLAN_INTERFACE_INFO_LIST)()
        if self._api.WlanEnumInterfaces(self._handle, None, ctypes.byref(plist)) != _ERROR_SUCCESS:
            return None
        try:
            lst = plist.contents
            if not lst.dwNumberOfItems:
                return None
            items = ctypes.cast(ctypes.byref(lst.InterfaceInfo),
                                ctypes.POINTER(_WLAN_INTERFACE_INFO * lst.dwNumberOfItems)).contents
            # prefer a connected interface (state 1)
            chosen = next((i for i in items if i.isState == 1), items[0])
            g = _GUID()
            ctypes.memmove(ctypes.byref(g), ctypes.byref(chosen.InterfaceGuid), ctypes.sizeof(_GUID))
            return g
        finally:
            self._api.WlanFreeMemory(plist)

    def rssi(self):
        """Current RSSI of the connected link in dBm, or None."""
        if self._guid is None:
            self._guid = self._interface()
            if self._guid is None:
                return None
        size = wintypes.DWORD()
        data = ctypes.c_void_p()
        rc = self._api.WlanQueryInterface(self._handle, ctypes.byref(self._guid),
                                          _WLAN_INTF_OPCODE_RSSI, None, ctypes.byref(size),
                                          ctypes.byref(data), None)
        if rc != _ERROR_SUCCESS:
            self._guid = None          # interface may have changed; re-enumerate next time
            return None
        try:
            return ctypes.cast(data, ctypes.POINTER(ctypes.c_long)).contents.value
        finally:
            self._api.WlanFreeMemory(data)

    def close(self):
        if self._handle:
            self._api.WlanCloseHandle(self._handle, None)
            self._handle = None


if __name__ == "__main__":
    import time
    r = LinkRSSI()
    t0 = time.perf_counter()
    vals = [r.rssi() for _ in range(20)]
    print(f"20 reads in {(time.perf_counter() - t0) * 1000:.1f} ms -> {vals}")
