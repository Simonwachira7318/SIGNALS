# Wi-Fi Sense

Passive Wi-Fi motion and presence sensing for Windows. It reads the signal
strength of nearby Wi-Fi networks (and, optionally, Bluetooth devices) and
flags when movement disturbs them. It is receive-only and never transmits.

## Run it

Start **MySQL** in the XAMPP Control Panel, then in PowerShell:

```powershell
python wifi_web.py
```

The dashboard opens at <http://127.0.0.1:8765>. Press `Ctrl+C` in the terminal to stop.
You can also double-click `start_wifi_sense.bat`.

### Configuration (`.env`)

Passwords and tokens live in a `.env` file next to the scripts. Copy `.env.example` to `.env` and fill it in:

```ini
WIFI_SENSE_DB_PASSWORD=root
WIFI_SENSE_TELEGRAM_TOKEN=
WIFI_SENSE_TELEGRAM_CHAT=
WIFI_SENSE_PORT=8765
```

`.env` is git-ignored, so never commit it. A non-empty environment variable (`$env:X = ...` or `setx`)
overrides the file's value.

### First-time setup

```powershell
python -m pip install -r requirements.txt
python oui.py --update        # MAC-vendor database (~3 MB), refresh now and then
```

`bleak` (Bluetooth) is optional: without it, the Bluetooth panel just says so. `matplotlib` is only
needed for the old window version (`wifi_dashboard.py`).

If Wi-Fi Sense is already running, starting it again just prints "already running" and exits.

### Run in the background (tray icon)

```powershell
pythonw tray.py          # no console; icon by the clock → Open dashboard / Open log / Quit
```

Output goes to `wifi_sense.log`.

### Start automatically at login

```powershell
powershell -ExecutionPolicy Bypass -File install_startup.ps1               # tray icon (default)
powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Mode Exe     # the packaged .exe
powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Mode Console # console window
powershell -ExecutionPolicy Bypass -File install_startup.ps1 -Remove
```

Also tick **MySQL** under XAMPP Control Panel → Config → *Autostart of modules*.
`start_wifi_sense.bat` starts the dashboard by hand (`--no-browser` skips opening a tab).

### Standalone .exe (no Python needed)

```powershell
python -m pip install pyinstaller pystray
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

This runs the tests, then builds:

- `dist\WiFiSense\WiFiSense.exe`: the tray app. Your `.env`, settings, model and floor plan are copied next to it.
- `dist\WiFiSenseNode\WiFiSenseNode.exe`: the remote room sensor (see *Rooms*).

Copy a whole `dist\...` folder to use it elsewhere. The hub still needs XAMPP's MySQL.

### Tests

```powershell
python -m unittest discover -s tests -v
```

## The dashboard

| Area | What it shows |
| --- | --- |
| Room tile | **Occupied / Empty** with how likely, for how long, why, and a last-hour timeline |
| Summary tiles | Activity score, room activity (5 min), motion episodes today, nearby sources, your live link (10 samples/s), **detection accuracy**, channel congestion + best channel |
| Signal map | Routers, hotspots and TVs at estimated distance. The shaded band is the likely range (wider = less accurate). A person icon marks a disturbed signal path |
| Detections | Colour-coded feed with a confidence % and **👍 Correct / 👎 False** buttons on each alert, filterable |
| Activity chart | Each path's wobble as a multiple of its **own** threshold (1× = motion, 2× = strong) |
| Activity recognition | A model you train on your own room (see below) |
| Bluetooth nearby | Phones, watches and earbuds heard nearby, by maker |
| Alerts & settings | Away mode, desktop alerts, quiet hours, adaptive thresholds, data retention, test alert |
| Sources table | ✎ name a device, Known/Trusted badges, vendor, channel, security, signal trend, distance + range, accuracy, wobble / threshold, state, **Calibrate** button |
| History page (`/history`) | Day × hour motion heatmap, detection log, CSV export |
| Devices page (`/devices`) | Every device ever seen: name it, mark it trusted or muted, 7-day presence grid, present-now dot |
| Movement rhythm | Frequency analysis of the live link (is it walking-like?) plus link-speed swings |
| Rooms | Motion per room from remote nodes |
| Wi-Fi health (`/health`) | Signal, link speed, router + internet latency, packet loss, worst hours, disconnects/roaming |
| Floor plan (`/floorplan`) | Your plan with routers, laptop and nodes placed. Disturbed paths show their sensitive zones |
| Rules (`/rules`) | "When X then Y" automations: webhook, Telegram, desktop, sound |
| Tuning lab (`/tuning`) | Replay recorded scans at different sensitivities before changing the live one |

## Detections

| Detection | Colour | Meaning |
| --- | --- | --- |
| Strong / light movement | red / orange | A path's wobble is above its learned threshold (strong = 2×) |
| Path blocked | amber | A signal dropped sharply below its learned normal level |
| Possible still presence | amber | A signal sits steadily off its normal level without wobbling |
| Unknown device / device left | blue / grey | An *unknown* Wi-Fi source appeared, or a device disappeared (open networks are flagged) |
| Security | red | Your network name broadcast by different hardware (possible evil twin), or a network's security type changed |
| Bluetooth | blue | Two or more extra Bluetooth devices came close |
| Room occupied / empty | blue / green | The fused room state changed (see below) |
| Known device arrived | blue | A named or trusted device came back (quiet: no phone alert) |
| Unusual activity | orange | Movement at a time that's normally quiet (after 7 days of learning) |
| Movement in another room | orange | A remote node saw movement |
| Wi-Fi health | amber | Disconnect, reconnect, roaming, or slow / lossy connection |
| Movement stopped / calibrated | green | Episode ended / a router was calibrated |

## Room occupied / empty

The Room tile combines every signal into one steady answer:

| Evidence | Weight |
| --- | --- |
| Movement on any path | strongest |
| Activity model predicts a non-"Empty" label (≥ 70% sure) | strong |
| Still presence or blocked path | medium |
| Bluetooth devices came close | weak |

After the last evidence it **holds** "occupied" for the hold time (default 5 min, in settings),
because people sit still. After that it fades to "empty". If you've trained an "Empty room" label, a
confident "Empty" prediction ends the hold early. It switches to occupied at 60% and back to empty below 30%,
so it doesn't flicker.

## Feedback: 👍 / 👎

Every detection in the feed has **👍 Correct** and **👎 False** buttons:

| On a… | 👎 False does | 👍 Correct does |
| --- | --- | --- |
| Movement alert | Raises the motion threshold ×1.15 on the paths involved | Lowers it slightly (×0.97) |
| Path blocked / still presence | Raises that path's presence threshold | Lowers it slightly |
| Device appeared / left / arrived | **Mutes** that device | Nothing to change |
| Security alert | Marks the device **trusted** | Nothing to change |
| Room empty | Adds 2 min to the hold time | Nothing to change |
| Room occupied | Removes 1 min from the hold time | Nothing to change |

Thresholds stay between ×0.6 and ×2.5. Tuned paths show "tuned ×1.3" in the sources table.
**Alerts & settings → Feedback tuning → Reset** undoes all tuning.

## Known devices

Open **Devices** (top right), or click ✎ next to a name in the sources table.
On first run the list is filled from every network already in `scans`.

- **Name** a device ("Neema's hotspot", "Living-room TV") to make it *known*. Its arrivals become a
  quiet "arrived" note instead of an "unknown device" alert.
- **Trusted** means no security alerts for it, e.g. a mesh node or extender from another brand.
- **Muted** means no appear/leave events at all, useful for weak networks that keep dropping in and out.
- The 7-day grid shows the hours each device was in range, so a phone hotspot that shows up every
  evening reads as "someone came home".

## Daily summary

At the time set in **Alerts & settings** (default 08:00), a summary of the last 24 h goes to Telegram:
motion episodes and the busiest hour, hours the room was occupied, unknown devices, security alerts
and your feedback. Without Telegram, a short desktop notification appears instead.
**Preview summary** shows it on screen and **Send summary now** sends it immediately.

## Getting better accuracy

1. **Calibrate your routers.** Hold the laptop about 1 m from a router, click **Calibrate** on its row
   and keep still for ~12 s. That router's distance estimate then uses its real signal level,
   which narrows its error range. Use ✕ to forget a calibration.
2. **Let it learn.** Leave *Adaptive thresholds* on. Each path learns its own noise level over the first minute or so.
3. **Stay connected to Wi-Fi.** Your connected router is sampled 10× per second, which makes it the best motion sensor.
4. **Train activity recognition.**
   - Pick a label (*Empty room*, *Walking*, …, or *Custom*) and press **Start recording**.
     Do that activity for at least 30 s (20+ samples), then press **Stop**.
   - Record at least 2 labels, then press **Train model**. Predictions appear live with probabilities.
   - More and varied recordings (different times of day, doors open or closed) give better results.
   - **Reset** deletes the model and all samples.

## Phone alerts (Telegram)

1. In Telegram, message **@BotFather** → `/newbot` → copy the bot token.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`.
3. Put both in `.env`, then restart the dashboard:

   ```ini
   WIFI_SENSE_TELEGRAM_TOKEN=123456:ABC...
   WIFI_SENSE_TELEGRAM_CHAT=987654321
   ```

   Use **Send test alert** to check.

Routing: security alerts always go to your phone. With **Away mode** on, motion,
presence, room-occupied, unknown-device and Bluetooth alerts do too. Known-device arrivals never do. Desktop motion alerts are muted
during quiet hours; security alerts still show. The token is only read from `.env` / the environment and is never shown or logged.

## Movement rhythm and link speed

The live link is read about 10 times a second, and every scan the last ~13 s are run through a
small frequency analysis. Walking modulates the signal at roughly 1–2.5 Hz (step rate).
The card shows the spectrum and a verdict ("walking-like rhythm", "slow movement", "steady"…).

**Honest limit:** many Wi-Fi drivers, including the one tested here, only refresh the signal
value about once a second or less. Then step rhythm can't be seen, and the card says so
("rhythm not measurable") instead of guessing. Movement is still detected either way.

Link-speed swings (the router stepping its rate up and down as bodies move near it) are tracked
too. Both feed the activity model as extra inputs. **Retrain your model** after upgrading,
because the input list grew.

## Unusual activity

Every hour is summarised (see *Keeping history*). After **7 days** it knows, for each hour of the
week, how often there's normally movement. A movement episode during a normally quiet slot
(e.g. Tuesday 03:00) raises an **Unusual activity** alert with a confidence. The Room tile shows
what's normal for the current hour. Switch it off under *Alerts & settings*.

## Tuning lab (`/tuning`)

Replays up to 72 h of recorded scans through the detector at sensitivities from ×0.5 to ×3.
You see how many episodes each would have produced and, if you've given 👍/👎 feedback in
that period, how many confirmed detections each keeps and how many false alarms it drops.
**Apply to live detector** sets the global sensitivity and episode gap. Replay uses per-scan values
only, so live detection (with 10×/s link sampling) is slightly more sensitive.

## Wi-Fi health (`/health`)

Once a minute it records your signal, link speeds and (if *Latency test* is on) 3 pings each to
your router and 1.1.1.1. It also logs disconnects, reconnects, roaming between access points,
and slow periods (router > 100 ms, internet > 300 ms, or ≥ 34% loss). The "worst hours" chart
answers "why is my Wi-Fi bad in the evening?". Pinging is ordinary network traffic; the
motion sensing itself stays passive.

## Floor plan (`/floorplan`)

1. Upload a PNG/JPEG of your room or house (or use the 1 m grid) and set its real width in metres.
2. **Place** yourself, your routers and any nodes, then drag them to where they really are.
3. Live: each signal path is a line. When one is disturbed, its **sensitive zone** (the ellipse
   between device and router where a body can affect the signal) lights up. Where several
   zones overlap is where the movement most likely is.

With one laptop you get one zone per router. That's a strip, not a point. Add a node in
another room and the zones cross.

## Rooms: remote nodes

Any other Windows laptop can be a sensor in another room, with no database needed on it.

On the **hub** (this PC), in `.env`, then restart:

```ini
WIFI_SENSE_BIND=0.0.0.0
WIFI_SENSE_ACCESS_TOKEN=<a long random string, 16+ characters>
```

Allow Python (or WiFiSense.exe) through the Windows firewall when asked, on **private** networks only.

On the **node**: copy this folder (or `dist\WiFiSenseNode`) over and run

```powershell
python node.py --hub http://<hub-ip>:8765 --name kitchen --token <same token>
```

The hub prints its address at start-up. The node reports every 1.5 s. The Rooms card shows each
room's state, and **Movement in kitchen** events can trigger rules. Place the node on the
floor plan to cross its paths with yours.

## Phone app and remote access

The dashboard is installable as an app ("Add to Home screen"). Two ways to reach it:

- **Tailscale (recommended: private and encrypted, works anywhere).** Install Tailscale on this PC
  and your phone, set `WIFI_SENSE_ACCESS_TOKEN` in `.env`, then run `tailscale serve --bg 8765`.
  Open the `https://<pc-name>.<tailnet>.ts.net` address it prints on your phone, enter the token
  once, then use *Add to Home screen*. HTTPS makes it a real installable app.
- **Home Wi-Fi only.** Set `WIFI_SENSE_BIND=0.0.0.0` plus a token (see *Rooms*) and open
  `http://<pc-ip>:8765` on your phone. It works in the browser, but plain HTTP can't be installed as an app.

Security: anything that isn't this PC's own browser needs the token. It's entered once and then
stored as a secure HttpOnly cookie. Without a token, the dashboard refuses to start on anything but
`127.0.0.1`. Never forward the port on your router to the internet.

## Automation rules (`/rules`)

"When *event* [only in away mode / only at home] [only between hh:mm–hh:mm] then *action*",
with a cooldown. The actions are:

- **Webhook**: POST JSON to a URL, e.g. a Home Assistant webhook to switch on lights.
- **Telegram**: send the event to your phone.
- **Desktop**: show a Windows notification.
- **Sound**: beep on this PC.

Presets cover common cases, and **Test** fires a rule immediately. Webhook body:

```json
{"source": "wifi-sense", "rule": "...", "event": "occupied", "severity": "info",
 "title": "Room occupied", "message": "...", "ts": "2026-09-27 19:02:11", "test": false}
```

## Keeping history + backups

- Every full hour is summarised into `hourly_stats` **before** raw scans expire, and those rows
  are kept forever: minutes the sensor ran, motion episodes, minutes occupied, average signal
  and latency. The History page's *Long-term trends* charts and the unusual-activity detector use them.
- A backup (`mysqldump` from XAMPP) is written to `backups\` daily with the summary. The newest 14
  are kept, and there's a *Back up now* button. It skips the huge raw `scans`/`motion_events` tables,
  since the hourly summaries cover them. Restore with
  `C:\xampp\mysql\bin\mysql -u root -p wifi_sense < backups\<file>.sql`.

## Home Assistant, Grafana, other tools

- `GET http://127.0.0.1:8765/api/summary`: compact JSON (motion, level, score, occupancy,
  **occupied / occupied_prob**, confidence, predicted activity, Bluetooth count, away mode). In Home Assistant, use a
  `rest` sensor with `json_attributes`.
- `GET /api/state`: everything the dashboard uses.
- `GET /api/export/detections.csv?days=30` and `/api/export/scans.csv?hours=24`: CSV downloads.
- **Grafana:** add a MySQL data source pointed at `wifi_sense` and query the `detections` / `scans` tables.

By default the server only listens on `127.0.0.1` and rejects requests carrying another site's
`Host` header or a non-JSON POST, so other websites can't drive it from your browser.
Remote access needs the access token (see *Phone app and remote access*). An automation webhook can
also push events into Home Assistant directly.

## What's in this folder

| File | What it is |
| --- | --- |
| `wifi_web.py` | Main app: scanner, detection engine, web server |
| `wifi_web.html`, `history.html` | Dashboard and history pages |
| `wlan_native.py` | Fast live-link signal reads via the Windows Wi-Fi API |
| `classifier.py` | Trainable activity model (pure Python) |
| `alerts.py` | Settings, desktop and Telegram alerts |
| `ble_scan.py` | Passive Bluetooth listener (optional) |
| `oui.py` | MAC-vendor lookup (`oui_manuf.txt`) |
| `presence.py` | Occupied / empty fusion with hold time and hysteresis |
| `devices.py`, `devices.html` | Known-device registry, feedback tuning, Devices page |
| `report.py` | Daily summary |
| `envfile.py`, `.env`, `.env.example`, `paths.py` | Configuration, secrets, file locations (script or .exe) |
| `analysis.py` | Movement-rhythm spectrum + link-speed features |
| `anomaly.py` | Unusual-activity detector (learned per hour of week) |
| `health.py`, `health.html` | Wi-Fi health log and page |
| `rollup.py` | Hourly summaries (kept forever) + backups |
| `replay.py`, `tuning.html` | Tuning lab |
| `rules.py`, `rules.html` | Automation rules |
| `floorplan.html` | Floor plan |
| `node.py` | Remote room sensor |
| `tray.py`, `build_exe.ps1` | Tray app, standalone .exe build |
| `manifest.json`, `sw.js`, `icon.svg` | Phone-app (PWA) files |
| `tests/` | Unit tests |
| `start_wifi_sense.bat`, `install_startup.ps1` | Start by hand / at login |
| `wifi_dashboard.py` | Older matplotlib window version; also holds shared config and MySQL setup |
| `wifi_sense.py` | Minimal console version, no database needed |

Generated at runtime: `settings.json`, `activity_model.json`, `floorplan.json`, `floorplan_image.*`,
`backups/`, `wifi_sense.log`. They're git-ignored, like `.env`, `dist/` and `build/`.

## Database

`wifi_sense` on `root@localhost:3306`, created on first run:

- `scans`: every signal reading. Rows older than the retention setting (default 7 days) are deleted hourly.
- `motion_events`: every scan in which motion was seen (same retention)
- `detections`: one row per detection, with confidence, involved paths and your 👍/👎 (kept)
- `devices`: every device seen, with your name / trusted / muted flags
- `path_tuning`: thresholds learned from feedback
- `calibration`: measured 1-metre signal level per calibrated router
- `training_samples`: labelled feature vectors for activity recognition
- `hourly_stats`: one summary row per hour, kept forever
- `wifi_health`: one connection sample per minute
- `rules`: automation rules

## Limits

- Distances are estimates from signal strength. Even calibrated, they're off by a metre or more.
- One laptop antenna can't sense **direction**, so the angle on the signal map is arbitrary.
- A person icon marks a disturbed signal **path**, not a person's position. It can't count people.
- Windows refreshes the list of other networks only every so often. Your connected link is the live sensor.
- "Evil twin" alerts can also be caused by a mesh node or extender from another brand.
- Phones rotate Bluetooth addresses, so Bluetooth counts are a trend, not a headcount.
- Breathing and fall detection need channel state information (CSI), and laptop Wi-Fi drivers
  don't expose it. That can't be solved in software. It needs ESP32 boards (or an Intel 5300 card on Linux).
  Remote *nodes* are the no-extra-hardware way to get per-room sensing.
- Movement rhythm needs a driver that refreshes the signal 3+ times a second. Many don't.

## Settings in code

Thresholds are at the top of `wifi_dashboard.py` (`MOTION_STD_DB`, `SCAN_INTERVAL`,
`MEASURED_POWER_DBM`, `PATH_LOSS_EXPONENT`) and `wifi_web.py` (`BLOCK_DROP_PCT`,
`STILL_DEV_PCT`, `SHADOW_DB`, `NOISE_MULT`, `LINK_HZ`). Sensitivity and episode gap are set from the
Tuning lab. Network settings (`WIFI_SENSE_PORT`, `WIFI_SENSE_BIND`, `WIFI_SENSE_ACCESS_TOKEN`) go in `.env`.
