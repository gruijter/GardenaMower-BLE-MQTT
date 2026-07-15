# GardenaMower-BLE-MQTT — Native Installation Guide

A complete, start-to-finish guide for getting your Gardena BLE mower talking to Homey via MQTT, running natively as a `systemd` service on a Raspberry Pi or other Linux host. No Docker required.

Tested down to a **Raspberry Pi Zero W (1st generation)**.

---

## Requirements

- **A Raspberry Pi with Bluetooth**, running Raspberry Pi OS (Bookworm-based), Lite or Desktop, 32-bit or 64-bit.
  - The plain, non-W original **Pi Zero and Pi 1/2** have no built-in Bluetooth — use a USB Bluetooth (BLE-capable) dongle with those instead.
  - If your Pi's onboard Bluetooth turns out to be unreliable, a cheap USB BLE dongle is a good fallback there too.
- Placement **close to the mower/charging station** — BLE range is limited, and a stable connection matters more than raw distance.
- Your Gardena mower already set up and working with the official Gardena Bluetooth app (so you know its **PIN**)
- **Mower must be docked in its charging station** with power on and a valid loop signal (boundary wire active) during the initial BLE pairing process. The mower will refuse to pair if it is out on the lawn or if the loop signal is inactive.
- An MQTT broker reachable from the Pi (e.g. running on Homey, Home Assistant, or standalone Mosquitto)
- Basic comfort with SSH and the command line
- Homey with the **MQTT Client** or **MQTT Hub** app installed

---

## Part 1 — Prepare the host

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
4. Confirm Bluetooth is working:
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
pip3 install --no-cache-dir --ignore-requires-python -r requirements.txt
```

On a Pi Zero W, dependency installation can take 20–40 minutes if any packages need to be compiled from source. Let it run.

---

## Part 4 — Configure it

All settings live in a config file that's loaded as environment variables by `systemd`. This keeps secrets (MQTT password, etc.) out of the script and out of git.

```bash
nano mower.env
```

Configure your MQTT broker connection details, mower PIN, and settings inside `mower.env`. All options are fully documented with inline comments directly inside [mower.env.example](mower.env.example).

### Finding your mower's details

- **MAC address**: The bridge can **autodiscover** this if you leave `MOWER_ADDRESS` blank or set to `AA:BB:CC:DD:EE:FF`. Alternatively, you can check the official Gardena Bluetooth app's device info screen, or scan for it manually:
  ```bash
  bluetoothctl scan on
  ```
  Look for your mower's name, then `bluetoothctl scan off`.
- **PIN**: the same PIN you use in the official Gardena app (Settings → Bluetooth). Default is often `1234` if never changed. It must be physically entered on the mower's buttons during pairing (mapping details are documented inline in [mower.env.example](mower.env.example)).

---

## Part 5 — First run: pairing

Before setting up the service, run it directly so you can see what's happening live:

```bash
export $(grep -v '^#' mower.env | xargs)
python3 mower_mqtt.py
```

### Put the mower in pairing mode & enter PIN

> [!IMPORTANT]
> **Before you start**: Ensure the mower is physically placed in its charging station, the station is powered on, and there is a valid green loop signal light (boundary wire active). The mower will refuse to pair if it is not docked or if the loop signal is inactive/powered off.

1. **Restart the mower**: Turn the mower completely off and then turn it back on. 
2. **Enter the PIN**: Enter the PIN on the mower's own buttons. The buttons map to digits as:
   `1` = Power, `2` = Calendar, `3` = Start, `4` = Home

This opens a **3-minute Bluetooth pairing window**.

Watch for a line like:
```
Status: Battery=XX%, Charging=..., State=..., Activity=...
```
That confirms pairing succeeded and status is being published to MQTT.

Once this works, stop it with `Ctrl+C` and move on.

---

## Part 6 — Connect it to Homey

In Homey, using the **MQTT Client** or **MQTT Hub** app:
- Subscribe to `<MOWER_BASE_TOPIC>/<AA_BB_CC_DD_EE_FF>/status` — a JSON payload with battery, charging state, mower state/activity, next scheduled start, schedule, RSSI, orientation/sensors, and more
- Subscribe to `<MOWER_BASE_TOPIC>/<AA_BB_CC_DD_EE_FF>/availability` — bridge status: `online` or `offline`
- Publish commands to `<MOWER_BASE_TOPIC>/<AA_BB_CC_DD_EE_FF>/command` to control the mower
- Publish `BRIDGE_PAUSE` to that same command topic before opening the official Gardena app for an extended session (e.g. a firmware update), and `BRIDGE_RESUME` afterwards — this guarantees no BLE conflict between the bridge and the app

For the complete list of status fields and all available commands, see the [README.md](README.md).

---

## Part 7 — Run as a systemd service

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

From now on, this bonding is remembered by the host — you will **not** need to repeat the pairing steps on future restarts or reboots.

---

## Coexisting with the official Gardena app

A BLE mower accepts only **one** connection at a time. This bridge connects only briefly during each poll cycle (roughly every `MOWER_POLL` seconds) and for the moment it takes to send a command, then disconnects — it never holds the connection open. The official app should be able to connect the rest of the time without a fight. For guaranteed conflict-free access (e.g. a firmware update), use `BRIDGE_PAUSE` / `BRIDGE_RESUME` as described above.

---

## Updating to a new version

```bash
cd ~/GardenaMower-BLE-MQTT
git pull
source .venv/bin/activate
pip3 install --no-cache-dir --ignore-requires-python --upgrade -r requirements.txt
sudo systemctl restart mower-mqtt.service
```
No pairing repeat needed — bonding is preserved on the host.

---

## Uninstalling

```bash
# Stop and disable services
sudo systemctl disable --now mower-mqtt.service
sudo systemctl disable --now bt-agent.service

# Remove systemd unit files
sudo rm /etc/systemd/system/mower-mqtt.service
sudo rm /etc/systemd/system/bt-agent.service
sudo systemctl daemon-reload

# Remove the project directory and virtual environment
rm -rf ~/GardenaMower-BLE-MQTT
```
If you also want to remove the Bluetooth bonding from the host:
```bash
bluetoothctl remove <MOWER_MAC>
```

---

## Troubleshooting

- **`bluetoothctl show` shows no adapter / `Powered: no`**: see Part 1 step 4. If there's genuinely no Bluetooth hardware, use a USB BLE dongle.
- **`[org.bluez.Error.AuthenticationFailed]` when pairing**: 
  - Verify the **pairing agent (`bt-agent.service`) is active** (see Part 2).
  - Verify the mower is **docked in its charging station** with power on and a valid green loop signal light. The mower's firmware will reject pairing requests immediately if it is out on the lawn or has no loop signal.
  - Make sure the mower is in pairing mode (switch the mower's power off and back on while docked to open the 3-minute pairing window).
  - Clear any old paired devices on the mower side (disconnect it in the official Gardena app and remove other devices if possible).
- **Pairing fails or times out**: the mower's pairing window (~2–3 minutes) likely closed before you connected — power-cycle the mower to put it back in pairing mode and try again. Make sure you're entering the PIN on the mower's own buttons, not just relying on `MOWER_PIN` in the config.
- **`MOW` command is accepted but the mower doesn't physically move**: check that the charging station / boundary wire isn't in a power-saving mode with its signal disabled — the mower will accept the BLE command but refuses to move without an active guide wire signal. Check this in the Gardena app.
- **Can't connect with the official Gardena app while the bridge is running**: publish `BRIDGE_PAUSE` to the command topic first, `BRIDGE_RESUME` when you're done.
- **Status stops updating after a while**: check `journalctl -u mower-mqtt.service -f` for errors; the bridge automatically reconnects on BLE drops, but a fully unreachable mower (out of range, powered off) will simply show no new status until it's reachable again.
- **Still stuck**: open an issue on the [GitHub repository](https://github.com/gruijter/GardenaMower-BLE-MQTT) with your `journalctl` output attached.
