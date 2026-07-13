# GardenaMower-BLE-MQTT — Docker Installation Guide

A complete, start-to-finish guide for getting your Gardena BLE mower talking to Homey via MQTT, using the prebuilt Docker image. No Python, no manual library patching, no systemd units to write by hand.

This works on **any Linux host with Bluetooth and Docker** — tested on both a Raspberry Pi 4/5 (64-bit Raspberry Pi OS) and an x86_64 mini PC (e.g. an N150-based device). The image is multi-arch, so the same instructions apply regardless of which one you have.

## What you need before starting

- A Linux host (Raspberry Pi 4/5 recommended for ARM, or any x86_64 mini PC / NUC-style device) with:
  - A **built-in or USB Bluetooth adapter**
  - **Docker** and the **Docker Compose plugin** installed
- Your Gardena mower already set up and working with the official Gardena Bluetooth app (so you know its **PIN**)
- An MQTT broker reachable from this host (e.g. running on Homey, Home Assistant, or standalone Mosquitto)
- Physical access to the mower to put it in pairing mode and enter its PIN via its buttons — this is a **one-time step**, not something you'll need to repeat later
- Homey with the **MQTT Client** or **MQTT Hub** app installed

## Part 1 — Prepare the host

### 1.1 Install Docker (skip if already installed)

Check first:
```bash
docker --version
docker compose version
```
If both return a version number, skip to 1.2. Otherwise, install Docker using the official convenience script:
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```
Log out and back in (or reboot) for the group change to take effect, then re-run the version checks above to confirm.

### 1.2 Confirm Bluetooth is working

```bash
bluetoothctl show
```
You should see `Powered: yes`. If Bluetooth isn't powered or the adapter isn't listed at all:
```bash
sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth
bluetoothctl show
```
If there's still no adapter, check that your device actually has Bluetooth hardware (some mini PCs and older Pi models don't) — a cheap USB Bluetooth dongle works fine as a fallback.

### 1.3 Start from a clean slate (important if you've experimented with this mower before)

Bluetooth pairing/bonding information is stored **on the host itself**, independent of any container. If this mower was ever paired with this host before — even from a failed or abandoned attempt — leftover bonding data can interfere with a fresh pairing attempt. Check and clean up:

```bash
# List any devices already known to this host
bluetoothctl devices

# If your mower's MAC address appears, remove the old pairing
bluetoothctl remove <MOWER_MAC>

# Check for any old containers/images from previous attempts
docker ps -a
docker images

# Remove anything left over from earlier experiments (adjust names as needed)
docker rm -f <old_container_name>
docker rmi <old_image_name>

# Check no old bt-agent or bluetoothctl session is still running
ps aux | grep -i bt-agent
ps aux | grep -i bluetoothctl
```

If you're setting this up on a host that's never touched this mower before, this whole section will simply come up empty — that's fine, just move on.

## Part 2 — Get the project and configure it

### 2.1 Clone the repository

```bash
git clone https://github.com/gruijter/GardenaMower-BLE-MQTT.git
cd GardenaMower-BLE-MQTT
```

### 2.2 Create your config file

```bash
cp mower.env.example mower.env
nano mower.env
```

Fill in:
```ini
# MQTT broker connection
MQTT_HOST=192.168.1.10
MQTT_PORT=1883
MQTT_USER=
MQTT_PASS=

# MQTT topic prefix used for all published/subscribed topics
MOWER_BASE_TOPIC=mower_ble

# How often (seconds) to poll the mower. 60 is a sensible default.
MOWER_POLL=60

# Your mower's Bluetooth MAC address
MOWER_ADDRESS=AA:BB:CC:DD:EE:FF

# The PIN configured on your mower (same PIN used in the official Gardena app)
MOWER_PIN=1234

# Optional logging settings (LOG_LEVEL can be DEBUG, INFO, WARNING, ERROR)
# LOG_LEVEL=INFO
# LOG_FILE=/app/logs/mower.log
```

**Finding your MAC address**, if you don't already know it:
```bash
bluetoothctl scan on
```
Look for your mower's name in the output, note its address, then `bluetoothctl scan off`.

**The PIN** is the same one you use in the official Gardena app (Settings → Bluetooth). If never changed, it's often `1234`.

## Part 3 — First run: pairing

This is the only part that needs you standing next to the mower. Everything after this is fully automatic.

### 3.1 Start the container in the foreground

```bash
docker compose up
```
(Deliberately not `-d` yet — you want to watch this happen live the first time.)

The container includes its own Bluetooth pairing agent (`bt-agent`), which starts automatically — you don't need to set up anything separately on the host for pairing to work.

### 3.2 Put the mower in pairing mode

Check your mower's manual for the exact button sequence. Pairing mode is usually indicated by specific LED symbols and **lasts only around 2–3 minutes** — do this right after starting the container so both fall within the same window.

### 3.3 Enter the PIN on the mower

When the log shows `pairing device...`, enter the PIN on the mower's own buttons, mapping digits to buttons as:
`1` = Power, `2` = Calendar, `3` = Start, `4` = Home

(So PIN `1234` = press Power, then Calendar, then Start, then Home.)

### 3.4 Confirm success

Watch for a line like:
```
Status: Battery=XX%, Charging=..., State=..., Activity=...
```
That confirms pairing succeeded and status is being published to MQTT.

### 3.5 Switch to running in the background

```bash
# Ctrl+C to stop the foreground session
docker compose up -d
docker compose logs -f   # optional, to keep watching
```

From now on, this bonding is remembered by the host — you will **not** need to repeat the pairing steps on future restarts, reboots, or container updates.

## Part 4 — Connect it to Homey

In Homey, using the **MQTT Client** or **MQTT Hub** app:
- Subscribe to `<MOWER_BASE_TOPIC>/<serial_number>/status` — a JSON payload with battery, charging state, mower state/activity, next scheduled start, schedule, RSSI, orientation/sensors, and more
- Subscribe to `<MOWER_BASE_TOPIC>/<serial_number>/availability` — bridge status: `online` or `offline`
- Publish `MOW`, `PARK`, `PARK_PERMANENTLY`, `RESUME_SCHEDULE`, `SPOT_CUT`, `STOP_SPOT_CUT`, or configuration commands (like `DRIVE_PAST_WIRE <dist>`, `REVERSING_DISTANCE <dist>`, `GARAGE_ENABLED <ON/OFF>`, `RADAR_ENABLED <ON/OFF>`, `ECO_MODE <ON/OFF>`, `GENERATE_LOOP_SIGNAL`, `SET_TIME`) to `<MOWER_BASE_TOPIC>/<serial_number>/command` to control the mower
- Publish `BRIDGE_PAUSE` to that same command topic before opening the official Gardena app for an extended session (e.g. a firmware update), and `BRIDGE_RESUME` afterwards — this guarantees no BLE conflict between the bridge and the app

Example status payload:
```json
{
  "Battery": "79",
  "Charging": "ON",
  "State": "RESTRICTED",
  "Activity": "PARKED",
  "NextStartSchedule": "2026-07-07T15:00:00+00:00",
  "Manufacturer": "Gardena",
  "Model": "SILENO Minimo 250",
  "MowerName": "SILENO minimo 250",
  "Schedule": "Tue,Fri 15:00 (1h30m)",
  "RSSI": -80,
  "collision": false,
  "lift": false,
  "pitch": 0,
  "roll": 1,
  "zAcceleration": 980,
  "upsideDown": false,
  "mowerTemperature": 22,
  "batteryTemperature": 25,
  "batteryVoltage": 20.60,
  "batteryCurrent": -6,
  "frostSensorEnabled": "ON",
  "sensorControlEnabled": "ON",
  "sensorControlSensitivity": "MEDIUM",
  "loopSignalStrength": 100,
  "loopSignalA": 17588,
  "loopSignalF": 15159,
  "loopSignalGuide": -9002,
  "tiltSensor": "Oké",
  "collisionSensor": "Oké",
  "garageEnabled": "ON",
  "radarEnabled": "OFF",
  "radarAvailable": "OFF",
  "ecoMode": "ON",
  "drivePastWire": 30,
  "reversingDistance": 600,
  "spotCuttingState": 0,
  "SoftwarePlatform": "P005G",
  "SoftwareVersion": "50.3",
  "SoftwareBundle": "5995762-04C"
}
```

## Coexisting with the official Gardena app

A BLE mower accepts only **one** connection at a time. This bridge connects only briefly during each poll cycle (roughly every `MOWER_POLL` seconds) and for the moment it takes to send a command, then disconnects — it never holds the connection open. The official app should be able to connect the rest of the time without a fight. For guaranteed conflict-free access (e.g. a firmware update), use `PAUSE` / `RESUME` as described above.

## Updating to a new version

```bash
cd GardenaMower-BLE-MQTT
docker compose pull
docker compose up -d
```
No pairing repeat needed — bonding is preserved on the host.

## Uninstalling

```bash
cd GardenaMower-BLE-MQTT
docker compose down
docker rmi ghcr.io/gruijter/gardenamower-ble-mqtt:latest
```
If you also want to remove the Bluetooth bonding from the host:
```bash
bluetoothctl remove <MOWER_MAC>
```

## Troubleshooting

- **`bluetoothctl show` shows no adapter / `Powered: no`**: see [1.2](#12-confirm-bluetooth-is-working). If there's genuinely no Bluetooth hardware, use a USB BLE dongle.
- **Pairing fails, or the container seems to hang on `pairing device...`**: the mower's pairing window (~2–3 minutes) likely closed before you entered the PIN — put it back in pairing mode and try again. Make sure you're entering the PIN on the mower's own buttons, not just relying on `MOWER_PIN` in the config.
- **Pairing behaves oddly / mower connects instantly without asking for anything**: possible leftover bonding from a previous attempt — run `bluetoothctl remove <MOWER_MAC>` (see [1.3](#13-start-from-a-clean-slate-important-if-youve-experimented-with-this-mower-before)) and try again.
- **`MOW` command is accepted but the mower doesn't physically move**: check that the charging station / boundary wire isn't in a power-saving mode with its signal disabled — the mower will accept the BLE command but refuses to move without an active guide wire signal. Check this in the Gardena app.
- **Can't connect with the official Gardena app while the bridge is running**: publish `PAUSE` to the command topic first, `RESUME` when you're done.
- **Status stops updating after a while**: check `docker compose logs -f` for errors; the bridge automatically reconnects on BLE drops, but a fully unreachable mower (out of range, powered off) will simply show no new status until it's reachable again.
- **Still stuck**: open an issue on the [GitHub repository](https://github.com/gruijter/GardenaMower-BLE-MQTT) with your `docker compose logs` output attached.

## Credits

- [andyb2000/AutoMower-BLE-MQTT](https://github.com/andyb2000/AutoMower-BLE-MQTT) — the original script this project is based on
- [alistair23/AutoMower-BLE](https://github.com/alistair23/AutoMower-BLE) — the underlying BLE protocol library
