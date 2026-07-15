# GardenaMower-BLE-MQTT — Docker Installation Guide

A complete, start-to-finish guide for getting your Gardena BLE mower talking via MQTT, using the prebuilt Docker image. No Python, no manual library patching, no systemd units to write by hand.

This works on **any Linux host with Bluetooth and Docker** — tested on both a Raspberry Pi 4/5 (64-bit Raspberry Pi OS) and an x86_64 mini PC (e.g. an N150-based device). The image is multi-arch, so the same instructions apply regardless of which one you have.

---

## Requirements

- A Linux host (Raspberry Pi 4/5 recommended for ARM, or any x86_64 mini PC / NUC-style device) with:
  - A **built-in or USB Bluetooth adapter**
  - **Docker** and the **Docker Compose plugin** installed
- Your Gardena mower already set up and working with the official Gardena Bluetooth app (so you know its **PIN**)
- **Mower must be docked in its charging station** with power on and a valid loop signal (boundary wire active) during the initial BLE pairing process. The mower will refuse to pair if it is out on the lawn or if the loop signal is inactive.
- **An MQTT broker** reachable from this host (e.g. running standalone Mosquitto, Home Assistant, or Homey)
- Physical access to the mower to put it in pairing mode and enter its PIN via its buttons — this is a **one-time step**, not something you'll need to repeat later

---

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
# (If you only see the grep command itself in the output, nothing is running)
ps aux | grep -i bt-agent
ps aux | grep -i bluetoothctl
```

> [!NOTE]
> When running the `ps aux | grep` commands above, if the only output line you see contains `grep --color=auto`, it means no background process is running. This is exactly what you want (a clean slate). If a process *were* running, you would see another line with the actual executable path `/usr/bin/bt-agent` or `bluetoothctl`.

If you're setting this up on a host that's never touched this mower before, this whole section will simply come up empty — that's fine, just move on.

---

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

Configure your MQTT broker connection details, mower PIN, and settings inside `mower.env`. All options are fully documented with inline comments directly inside [mower.env.example](mower.env.example).

### Finding your mower's details

- **MAC address**: The bridge can **autodiscover** this if you leave `MOWER_ADDRESS` blank or set to `AA:BB:CC:DD:EE:FF`. It will persistently save the found address back to the `mower.env` file. 

  Alternatively, you can check the official Gardena Bluetooth app's device info screen, or scan for it manually:
  ```bash
  bluetoothctl scan on
  ```
  Look for your mower's name in the output, note its address, then `bluetoothctl scan off`.
- **PIN**: the same PIN you use in the official Gardena app (Settings → Bluetooth). Default is often `1234` if never changed. It must be physically entered on the mower's buttons during pairing (mapping details are documented inline in [mower.env.example](mower.env.example)).

---

## Part 3 — First run: pairing

This is the only part that needs you standing next to the mower. Everything after this is fully automatic.


### 3.1 Start the container in the foreground

```bash
docker compose up
```
(Deliberately not `-d` yet — you want to watch this happen live the first time.)

The container includes its own Bluetooth pairing agent (`bt-agent`), which starts automatically — you don't need to set up anything separately on the host for pairing to work.

### 3.2 Put the mower in pairing mode & enter PIN

> [!IMPORTANT]
> **Before you start**: Ensure the mower is physically placed in its charging station, the station is powered on, and there is a valid green loop signal light (boundary wire active). The mower will refuse to pair if it is not docked or if the loop signal is inactive/powered off.

1. **Restart the mower**: Turn the mower completely off and then turn it back on. 
2. **Enter the PIN**: Enter the PIN on the mower's own buttons. The buttons map to digits as:
   `1` = Power, `2` = Calendar, `3` = Start, `4` = Home

This opens a **3-minute Bluetooth pairing window**.

### 3.3 Resume normal operation

Once pairing is confirmed, return the mower to its normal operating state:

1. **Start & close**: Physically press the **Start** (or **Play**) button on the mower's keypad and **close the cover/hatch**. This returns the mower to its normal scheduled/docked state.
   > [!NOTE]
   > If you skip this, the mower will remain in a `STOPPED` state (reported as a "Safety Stop" or "Requires manual action" in smart home platforms) because the hatch is open and no start command was confirmed after the PIN entry.

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

---

## Coexisting with the official Gardena app

A BLE mower accepts only **one** connection at a time. This bridge connects only briefly during each poll cycle (roughly every `MOWER_POLL` seconds) and for the moment it takes to send a command, then disconnects — it never holds the connection open. The official app should be able to connect the rest of the time without a fight. For guaranteed conflict-free access (e.g. a firmware update), use `BRIDGE_PAUSE` / `BRIDGE_RESUME` as described above.

---

## Updating to a new version

```bash
cd GardenaMower-BLE-MQTT
docker compose pull
docker compose up -d
```
No pairing repeat needed — bonding is preserved on the host.

---

## Uninstalling

```bash
cd GardenaMower-BLE-MQTT
docker compose down
docker rmi ghcr.io/gruijter/gardenamower-ble-mqtt:latest
cd ..
rm -rf GardenaMower-BLE-MQTT
```
If you also want to remove the Bluetooth bonding from the host:
```bash
bluetoothctl remove <MOWER_MAC>
```

---

## Troubleshooting

- **`bluetoothctl show` shows no adapter / `Powered: no`**: see [1.2](#12-confirm-bluetooth-is-working). If there's genuinely no Bluetooth hardware, use a USB BLE dongle.
- **Pairing fails with `[org.bluez.Error.AuthenticationFailed] Authentication Failed`**: 
  - Verify the mower is **docked in its charging station** with power on and a valid green loop signal light. The mower's firmware will reject pairing requests immediately if it is out on the lawn or has no loop signal.
  - Make sure the mower is in pairing mode (switch the mower's power off and back on while docked to open the 3-minute pairing window).
  - Clear any old paired devices on the mower side (disconnect it in the official Gardena app and remove other devices if possible).
- **Pairing fails, or the container seems to hang on `pairing device...`**: the mower's pairing window (~2–3 minutes) likely closed before you entered the PIN — power-cycle the mower to put it back in pairing mode and try again. Make sure you're entering the PIN on the mower's own buttons, not just relying on `MOWER_PIN` in the config.
- **Pairing behaves oddly / mower connects instantly without asking for anything**: possible leftover bonding from a previous attempt — run `bluetoothctl remove <MOWER_MAC>` on the host (see [1.3](#13-start-from-a-clean-slate-important-if-youve-experimented-with-this-mower-before)) and try again.
- **`MOW` command is accepted but the mower doesn't physically move**: check that the charging station / boundary wire isn't in a power-saving mode with its signal disabled — the mower will accept the BLE command but refuses to move without an active guide wire signal. Check this in the Gardena app.
- **Can't connect with the official Gardena app while the bridge is running**: publish `BRIDGE_PAUSE` to the command topic first, `BRIDGE_RESUME` when you're done.
- **Status stops updating after a while**: check `docker compose logs -f` for errors; the bridge automatically reconnects on BLE drops, but a fully unreachable mower (out of range, powered off) will simply show no new status until it's reachable again.
- **Still stuck**: open an issue on the [GitHub repository](https://github.com/gruijter/GardenaMower-BLE-MQTT) with your `docker compose logs` output attached.
