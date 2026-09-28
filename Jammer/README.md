# ESP32-Quad-nRF24L01-Plus-Jammer-Multi-Band-2-4GHz-RF-Controller

This project allows an ESP32 board to control **four nRF24L01+ modules** for RF jamming (Bluetooth jammer, BLE jammer, WiFi jammer, RC jammer, ...) or channel sweeping across multiple 2.4GHz bands. Each module can be independently controlled and jam on its own channel. Modes are selectable via serial commands.

---

## Pin Mapping

# ESP32 with 4 nRF24L01+ Modules Wiring Guide

This guide shows how to connect **four nRF24L01+ modules** to an **ESP32**, using both **HSPI** and **VSPI** SPI buses.

## 🔌 Pin Connections

| ESP32 Pin | Function       | Module 1 (HSPI) | Module 2 (HSPI) | Module 3 (VSPI) | Module 4 (VSPI) |
|----------|----------------|------------------|------------------|------------------|------------------|
| GPIO14   | HSPI SCK       | SCK              | SCK              | -                | -                |
| GPIO13   | HSPI MOSI      | MOSI             | MOSI             | -                | -                |
| GPIO12   | HSPI MISO      | MISO             | MISO             | -                | -                |
| GPIO26   | CE             | ✅ (Module 1)     | -                | -                | -                |
| GPIO15   | CSN            | ✅ (Module 1)     | -                | -                | -                |
| GPIO27   | CE             | -                | ✅ (Module 2)     | -                | -                |
| GPIO25   | CSN            | -                | ✅ (Module 2)     | -                | -                |
| GPIO18   | VSPI SCK       | -                | -                | SCK              | SCK              |
| GPIO23   | VSPI MOSI      | -                | -                | MOSI             | MOSI             |
| GPIO19   | VSPI MISO      | -                | -                | MISO             | MISO             |
| GPIO4    | CE             | -                | -                | ✅ (Module 3)     | -                |
| GPIO2    | CSN            | -                | -                | ✅ (Module 3)     | -                |
| GPIO16   | CE             | -                | -                | -                | ✅ (Module 4)     |
| GPIO17   | CSN            | -                | -                | -                | ✅ (Module 4)     |

## ⚠️ Power and Misc

| nRF24L01+ Pin | Connection |
|---------------|------------|
| VCC           | 3.3V *(DO NOT USE 5V!)* |
| GND           | GND        |
| IRQ           | *(Optional, can be left unconnected)* |

> 💡 Tip: Use a **10µF capacitor** between VCC and GND on each module for better power stability.
---

## Hardware Schematic (ASCII Art)

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                                  ESP32 Dev Board                              │
│                                                                               │
│  ┌─────────┐       ┌─────────┐       ┌─────────┐       ┌─────────┐            │
│  │ nRF24 #1│       │ nRF24 #2│       │ nRF24 #3│       │ nRF24 #4│            │
│  └────┬────┘       └────┬────┘       └────┬────┘       └────┬────┘            │
│       │CE(26)           │CE(27)           │CE(4)            │CE(16)           │
│       │CSN(15)          │CSN(25)          │CSN(2)           │CSN(17)          │
│       │                 │                 │                 │                 │
│  ┌────▼────┐       ┌────▼────┐       ┌────▼────┐       ┌────▼────┐            │
│  │  HSPI   │       │  HSPI   │       │  VSPI   │       │  VSPI   │            │
│  ├─────────┤       ├─────────┤       ├─────────┤       ├─────────┤            │
│  │ SCK(14)◄────────┤ SCK(14) │       │ SCK(18)◄────────┤ SCK(18) │            │
│  │ MOSI(13)◄───────┤ MOSI(13)│       │ MOSI(23)◄───────┤ MOSI(23)│            │
│  │ MISO(12)├───────► MISO(12)│       │ MISO(19)├───────► MISO(19)│            │
│  └─────────┘       └─────────┘       └─────────┘       └─────────┘            │
│                                                                               │
│  ┌───────────────────────────────────────────────────────────────────────┐    │
│  │                            Power Connections                          │    │
│  │  All VCC pins → 3.3V (with 10µF cap to GND for each module)           │    │
│  │  All GND pins → ESP32 GND                                             │    │
│  └───────────────────────────────────────────────────────────────────────┘    │
│                                                                               │
└───────────────────────────────────────────────────────────────────────────────┘

All modules: VCC to 3.3V, GND to GND (do NOT use 5V!)
```

---

## Serial Commands

- `start` – Initialize and activate all 4 jammers
- `stop` – Deactivate all jammers
- `r` – Random Bluetooth hopping mode (odd/even channels)
- `w` – WiFi channels mode
- `b` – BLE advertising channels mode
- `u` – USB wireless channels mode
- `v` – Video streaming channels mode
- `c` – RC toys/drones channels mode
- `f` – Full spectrum mode (all channels)
- `help` or `?` – Show help

---

## Usage

1. Wire up the ESP32 and 4x nRF24L01+ modules as shown above.
2. Flash the provided firmware to your ESP32.
3. Open a serial terminal at 115200 baud.
4. Use the commands above to control the jammers.

---

## Warnings

- **FOR EDUCATIONAL AND TESTING PURPOSES ONLY.** Jamming radio frequencies may be illegal in your country. Always comply with local laws.
- Always power nRF24L01+ modules with **3.3V** (never 5V).
- For best stability, use capacitor filtering for each nRF24L01+ module’s VCC/GND.

---

## License

See [LICENSE.md](LICENSE.md) for license information.
