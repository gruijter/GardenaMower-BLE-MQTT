# Native Installation Guide

This guide walks you through installing the GardenaMower-BLE-MQTT bridge natively as a `systemd` service on a Raspberry Pi or other Linux host.

---

## Requirements

- **A Raspberry Pi with Bluetooth**, running Raspberry Pi OS (Bookworm-based), Lite or Desktop, 32-bit or 64-bit — this project has been tested down to a **Raspberry Pi Zero W (1st generation)**.
  - The plain, non-W original **Pi Zero and Pi 1/2** have no built-in Bluetooth — use a USB Bluetooth (BLE-capable) dongle with those instead.
  - If your Pi's onboard Bluetooth turns out to be unreliable, a cheap USB BLE dongle is a good fallback there too.
- Placement **close to the mower/charging station** — BLE range is limited, and a stable connection matters more than raw distance.
- A Gardena BLE mower already set up and working with the official Gardena Bluetooth app (so you know its **PIN** and it's a model that supports direct BLE pairing).
- An MQTT broker reachable from the Pi (e.g. running on Homey, Home Assistant, or standalone Mosquitto).
- Basic comfort with SSH and the command line.

---

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

---

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

---

## Part 3 — Get the code

```bash
cd ~
git clone https://github.com/gruijter/GardenaMower-BLE-MQTT.git
cd GardenaMower-BLE-MQTT
python3 -m venv .venv
source .venv/bin/activate
pip3 install --upgrade pip
```

Install all Python dependencies (including the patched mower protocol library) in one command:
```bash
pip3 install -r requirements.txt
```

On a Pi Zero W, dependency installation can take 20–40 minutes if any packages need to be compiled from source. Let it run.

---

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

   # Your mower's Bluetooth MAC address
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

---

## Part 5 — First manual test

Before setting up the service, run it directly so you can see what's happening and fix anything before it's "hidden" behind systemd:

```bash
export $(grep -v '^#' mower.env | xargs)
python3 mower_mqtt.py
```

Put the mower in Bluetooth pairing mode (check your mower's manual for the exact button sequence — pairing mode is usually indicated by specific LED symbols and lasts around 2–3 minutes) and watch the log. You should see it scan, connect, pair, and start publishing status.

Once this works, stop it with `Ctrl+C` and move on to running it as a proper service.

---

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

Enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mower-mqtt.service
journalctl -u mower-mqtt.service -f
```

---

## Troubleshooting

- **`[org.bluez.Error.AuthenticationFailed]` when pairing**: no BlueZ agent registered — make sure `bt-agent.service` (Part 2) is running.
- **Pairing works sometimes, not others**: the mower's Bluetooth pairing mode has a limited window (roughly 2–3 minutes) — make sure you're connecting while it's actively in that mode.
- **`Service Discovery has not been performed yet`**: usually a transient BLE hiccup right after (re)connecting — the bridge automatically reconnects on the next cycle.
- **Mower accepts `MOW` but doesn't move**: check that the boundary wire/charging station isn't in a power-saving mode with the signal disabled — the mower will accept the BLE command but refuses to physically move without an active guide wire signal.
- **Duplicate/garbled log lines right after a restart**: usually two processes briefly overlapping during a fast manual restart — this is what `TimeoutStopSec`/`KillMode=mixed` in the service file above prevents in normal operation.
- **Can't connect from the official app while the bridge runs**: send `BRIDGE_PAUSE` to the command topic before opening the app.
