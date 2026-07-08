# GardenaMower-BLE-MQTT

A Raspberry Pi bridge that connects a **Gardena Bluetooth (BLE) robotic mower** (Sileno Minimo, Sileno City, and similar Gardena/Husqvarna-badged models that use the same protocol) to **MQTT**, so it can be monitored and controlled from **Homey** (or Home Assistant, or any other MQTT-capable smart home platform).

This project is a modified version of [andyb2000/AutoMower-BLE-MQTT](https://github.com/andyb2000/AutoMower-BLE-MQTT), adapted and hardened specifically for **Gardena** BLE mowers and for use with **Homey**. It builds on the excellent reverse-engineered protocol library by [alistair23/AutoMower-BLE](https://github.com/alistair23/AutoMower-BLE).

## What this does

- Connects to your mower over Bluetooth Low Energy (no cloud account, no Gardena Smart Gateway needed)
- Publishes live status to MQTT: battery level, charging state, mower state/activity, next scheduled start, mowing schedule, statistics, RSSI, and more
- Lets you send `MOW`, `PARK`, `PAUSE`, and `RESUME` commands to the mower via MQTT
- Easily readable and controllable directly from Homey (or other smart home platforms) via MQTT
- Only occupies the mower's single BLE connection slot briefly during each poll/command, so the **official Gardena app can still connect** the rest of the time
- Supports pausing the bridge entirely (`BRIDGE_PAUSE`/`BRIDGE_RESUME`) when you need guaranteed, uninterrupted access from the app

## Why this exists / how it differs from the original

The original `AutoMower-BLE-MQTT` script targets Husqvarna Automowers. Getting it working reliably with a **Gardena Sileno Minimo** surfaced several issues that this fork fixes:

- Pairing failures caused by no BlueZ pairing agent being registered on headless systems
- A concurrency bug where multiple mower commands were sent in parallel over a connection that can only handle one at a time
- Several parsing bugs in the underlying `automower_ble` library (`GetMessage`, `GetOverride`, `GetTask` all have response-schema mismatches that cause exceptions on legitimate mower responses)
- No automatic recovery when the BLE link drops
- No way to free up the mower for the official app without stopping the whole service
- Missing extra data: manufacturer, model, serial number, mower name, and the actual mowing schedule

See [Known upstream library issues](#known-upstream-library-issues) below for details and status.

## Requirements

- **A Raspberry Pi with Bluetooth**, running Raspberry Pi OS (Bookworm-based), Lite or Desktop, 32-bit or 64-bit — this project has been tested down to a **Raspberry Pi Zero W (1st generation)**, so any newer model (Zero 2 W, 3B, 3B+, 4, 5) works at least as well.
  - The plain, non-W original **Pi Zero and Pi 1/2** have no built-in Bluetooth — use a USB Bluetooth (BLE-capable) dongle with those instead.
  - If your Pi's onboard Bluetooth turns out to be unreliable, a cheap USB BLE dongle is a good fallback there too.
- Placement **close to the mower/charging station** — BLE range is limited, and a stable connection matters more than raw distance.
- A Gardena BLE mower already set up and working with the official Gardena Bluetooth app (so you know its **PIN** and it's a model that supports direct BLE pairing).
- An MQTT broker reachable from the Pi (e.g. running on Homey, Home Assistant, or standalone Mosquitto).
- Basic comfort with SSH and the command line. Every step below is spelled out — copy-paste should get you through it even if you've never used a Pi headless before.

## Part 1 — Prepare the Raspberry Pi

1. Flash **Raspberry Pi OS (Bookworm, Lite is fine)** to an SD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/). In the imager's advanced options (gear icon / Ctrl+Shift+X), set:
   - A hostname (e.g. `mower-bridge`)
   - Username/password (this guide assumes username `pi` — adjust paths below if you use something else)
   - Enable SSH
   - Your Wi-Fi credentials, if not using Ethernet
2. Boot the Pi and connect over SSH:
   ```bash
   ssh pi@mower-bridge.local
   ```
   (or use its IP address if `.local` resolution doesn't work on your network)
3. Update the system and install required packages:
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y git python3-venv python3-pip python3-dev build-essential libglib2.0-dev bluez bluez-tools
   ```
4. Confirm Bluetooth is up:
   ```bash
   bluetoothctl show
   ```
   You should see `Powered: yes`. If not:
   ```bash
   sudo systemctl enable --now bluetooth
   sudo rfkill unblock bluetooth
   ```
5. **If you're on a low-RAM board (Pi Zero W/2 W):** temporarily increase swap, since some Python dependencies may need to be compiled from source and can otherwise run out of memory:
   ```bash
   sudo nano /etc/dphys-swapfile
   ```
   Set `CONF_SWAPSIZE=1024`, save, then:
   ```bash
   sudo systemctl restart dphys-swapfile
   ```

## Part 2 — Register a BLE pairing agent

Without this step, pairing will fail on a headless Pi with `[org.bluez.Error.AuthenticationFailed]`, because BlueZ has no agent to hand the pairing request to.

```bash
sudo nano /etc/systemd/system/bt-agent.service
```

Paste:
```ini
[Unit]
Description=BlueZ NoInputNoOutput pairing agent
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/bin/bt-agent --capability=NoInputNoOutput
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bt-agent.service
sudo systemctl status bt-agent.service
```
Confirm it shows `active (running)`.

## Part 3 — Get the code

```bash
cd ~
git clone https://github.com/gruijter/GardenaMower-BLE-MQTT.git
cd GardenaMower-BLE-MQTT
python3 -m venv .venv
source .venv/bin/activate
pip3 install --upgrade pip
```

Install the Python dependencies:
```bash
pip3 install bleak aiomqtt
```

Install the mower protocol library **from the patched fork** (recommended — includes fixes for parsing bugs described below, no manual patching needed):
```bash
pip3 install "git+https://github.com/gruijter/AutoMower-BLE.git@combined-fixes"
```
> Adjust this URL if you end up publishing the patched library under a different fork name or branch.
>
> Alternatively, you can install the official `automower-ble` package directly and apply the patches yourself — see [Known upstream library issues](#known-upstream-library-issues).

On a Pi Zero W, dependency installation can take 20–40 minutes if any packages need to be compiled from source. Let it run.

## Part 4 — Configuration via a separate config file

Rather than editing the script or hardcoding values, all settings live in a small config file that's loaded as environment variables by `systemd`. This keeps secrets (MQTT password, etc.) out of the script and out of git.

Edit it with your own values:
   ```bash
   nano mower.env
   ```
   ```ini
   # MQTT broker connection
   MQTT_HOST=192.168.1.10
   MQTT_PORT=1883
   MQTT_USER=
   MQTT_PASS=

   # MQTT topic prefix used for all published/subscribed topics
   MOWER_BASE_TOPIC=mower_ble

   # How often (seconds) to poll the mower. 60 is a sensible default — don't go much lower.
   MOWER_POLL=60

   # Your mower's Bluetooth MAC address (see "Finding your mower's details" below)
   MOWER_ADDRESS=AA:BB:CC:DD:EE:FF

   # The PIN configured on your mower (same PIN used in the official Gardena app)
   MOWER_PIN=1234
   ```

### Finding your mower's details

- **MAC address**: check the official Gardena Bluetooth app's device info screen, or scan for it:
  ```bash
  bluetoothctl scan on
  ```
  Look for your mower's name, then `bluetoothctl scan off`.
- **PIN**: the same PIN you use to connect via the official app (Settings → Bluetooth, in the app). Default is often `1234` if never changed — entered on the mower via its buttons as: `1`=Power, `2`=Calendar, `3`=Start, `4`=Home.

## Part 5 — First manual test

Before setting up the service, run it directly so you can see what's happening and fix anything before it's "hidden" behind systemd:

```bash
export $(grep -v '^#' mower.env | xargs)
python3 mower_mqtt.py
```

Put the mower in Bluetooth pairing mode (check your mower's manual for the exact button sequence — pairing mode is usually indicated by specific LED symbols and lasts around 2–3 minutes) and watch the log. You should see it scan, connect, pair, and start publishing status.

Once this works, stop it with `Ctrl+C` and move on to running it as a proper service.

## Part 6 — Run as a systemd service

```bash
sudo nano /etc/systemd/system/mower-mqtt.service
```

```ini
[Unit]
Description=Gardena Mower BLE to MQTT Bridge
After=network.target bluetooth.target bt-agent.service
Requires=bt-agent.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/GardenaMower-BLE-MQTT
EnvironmentFile=/home/pi/GardenaMower-BLE-MQTT/mower.env
ExecStart=/home/pi/GardenaMower-BLE-MQTT/.venv/bin/python3 /home/pi/GardenaMower-BLE-MQTT/mower_mqtt.py
Restart=always
RestartSec=20
TimeoutStopSec=15
KillMode=mixed

[Install]
WantedBy=multi-user.target
```

The `EnvironmentFile=` line is what loads `mower.env` — no code changes needed to configure the bridge, and no secrets stored in the unit file itself.

Enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mower-mqtt.service
journalctl -u mower-mqtt.service -f
```

## Using it from Homey

Use Homey's **MQTT Client** or **MQTT Hub** app to:
- Subscribe to `<MOWER_BASE_TOPIC>/status` (JSON payload with battery, state, activity, schedule, statistics, RSSI, etc.) and map fields into Homey flows/insights
- Subscribe to `<MOWER_BASE_TOPIC>/availability` (bridge status: `online` or `offline`)
- Publish `MOW`, `PARK`, `PAUSE`, or `RESUME` to `<MOWER_BASE_TOPIC>/command` to control the mower
- Publish `BRIDGE_PAUSE` to `<MOWER_BASE_TOPIC>/command` before opening the official Gardena app for an extended session, and `BRIDGE_RESUME` afterwards, to guarantee no BLE conflicts (this stops/starts the bridge polling loop)
- Publish a duration in seconds to `<MOWER_BASE_TOPIC>/custom_value` to set a custom manual override mow duration (default is 3600), which can be read back from `<MOWER_BASE_TOPIC>/state/custom_value`

Example status payload:
```json
{
  "Battery": "79",
  "Charging": "ON",
  "State": "RESTRICTED",
  "Activity": "PARKED",
  "NextStartSchedule": "2026-07-07T15:00:00+00:00",
  "LastError": "UNKNOWN",
  "LastErrorSchedule": null,
  "CurrUpdateSchedule": "2026-07-07T08:50:55Z",
  "Manufacturer": "Gardena",
  "Model": "SILENO Minimo 250",
  "SerialNumber": "230471273",
  "MowerName": "SILENO minimo 250",
  "Schedule": "Tue,Fri 15:00 (1h30m)",
  "RSSI": -80,
  "RemainingMowTime": 0,
  "totalRunningTime": 12345,
  "totalCuttingTime": 10000,
  "totalChargingTime": 2345,
  "totalSearchingTime": 120,
  "numberOfCollisions": 42,
  "numberOfChargingCycles": 150,
  "cuttingBladeUsageTime": 9800
}
```

## Coexisting with the official Gardena app

A BLE mower only accepts **one** connection at a time. This bridge is designed to minimize conflicts:
- It connects only briefly during each poll cycle (roughly every `MOWER_POLL` seconds) and for the moment it takes to send a command, then disconnects immediately — it does not hold the connection open.
- If you need guaranteed access from the app (e.g. for a firmware update), send `BRIDGE_PAUSE` to the command topic first, and `BRIDGE_RESUME` when you're done.

## Known upstream library issues
The fixes below are combined in the [`combined-fixes`](https://github.com/gruijter/AutoMower-BLE/tree/combined-fixes) 
branch of the patched fork used by this project, pending review upstream. 
Track their status: [#151](https://github.com/alistair23/AutoMower-BLE/pull/151) 
(parse_response), [#152](https://github.com/alistair23/AutoMower-BLE/pull/152) 
(GetOverride), [#153](https://github.com/alistair23/AutoMower-BLE/pull/153) 
(GetTask).

The following bugs exist in the upstream `automower_ble` library (as of the versions tested) and are fixed in [the patched fork](https://github.com/gruijter/AutoMower-BLE) used by this project. Pull requests for these have been submitted upstream — check their status there before assuming they're still needed:

| Command | Issue | Fix |
|---|---|---|
| `GetMessage` | Crashes with `bytearray index out of range` when the mower returns an empty payload (e.g. no error ever logged) | `parse_response()` now returns `None` for a zero-length payload instead of indexing into it |
| `GetOverride` | `Data length mismatch. Read 9 bytes of 13` — response schema is missing 4 bytes | Added a trailing `unknown: uint32` field to the schema |
| `GetTask` | `Data length mismatch. Read 17 bytes of 19` — response schema is missing 2 bytes | Changed the trailing `unknown` field from `uint16` to `uint32` |

If you're installing the official `automower-ble` package instead of the fork and hit these errors, you'll need to manually patch your installed copy of `protocol.json` and `protocol.py` accordingly until the fixes land upstream.

## Troubleshooting

- **`[org.bluez.Error.AuthenticationFailed]` when pairing**: no BlueZ agent registered — make sure `bt-agent.service` (Part 2) is running.
- **Pairing works sometimes, not others**: the mower's Bluetooth pairing mode has a limited window (roughly 2–3 minutes) — make sure you're connecting while it's actively in that mode.
- **`Service Discovery has not been performed yet`**: usually a transient BLE hiccup right after (re)connecting — the bridge automatically reconnects on the next cycle.
- **Mower accepts `MOW` but doesn't move**: check that the boundary wire/charging station isn't in a power-saving mode with the signal disabled — the mower will accept the BLE command but refuses to physically move without an active guide wire signal.
- **Duplicate/garbled log lines right after a restart**: usually two processes briefly overlapping during a fast manual restart — this is what `TimeoutStopSec`/`KillMode=mixed` in the service file above prevents in normal operation.
- **Can't connect from the official app while the bridge runs**: send `BRIDGE_PAUSE` to the command topic before opening the app.

## Credits

- [andyb2000/AutoMower-BLE-MQTT](https://github.com/andyb2000/AutoMower-BLE-MQTT) — the original script this project is based on
- [alistair23/AutoMower-BLE](https://github.com/alistair23/AutoMower-BLE) — the reverse-engineered BLE protocol library this depends on

## License

This project is licensed under the GPL-3.0 License. See the [LICENSE](file:///home/robin/HomeyDev/GardenaMower-BLE-MQTT/LICENSE) file for the full text.
