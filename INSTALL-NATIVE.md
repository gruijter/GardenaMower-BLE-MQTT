# GardenaMower-BLE-MQTT — Native Installation Guide

A complete, start-to-finish guide for getting your Gardena BLE mower talking via MQTT, running natively as a `systemd` service on a Raspberry Pi or other Linux host. No Docker required.

Tested down to a **Raspberry Pi Zero W (1st generation)**.

---

## Requirements

- **A Raspberry Pi with Bluetooth**, running Raspberry Pi OS (Bookworm-based), Lite or Desktop, 32-bit or 64-bit.
  - The plain, non-W original **Pi Zero and Pi 1/2** have no built-in Bluetooth — use a USB Bluetooth (BLE-capable) dongle with those instead.
  - If your Pi's onboard Bluetooth turns out to be unreliable, a cheap USB BLE dongle is a good fallback there too.
- Placement **close to the mower/charging station** — BLE range is limited, and a stable connection matters more than raw distance.
- Your Gardena mower already set up and working with the official Gardena Bluetooth app (so you know its **PIN**)
- **Mower must be docked in its charging station** with power on and a valid loop signal (boundary wire active) during the initial BLE pairing process. The mower will refuse to pair if it is out on the lawn or if the loop signal is inactive.
- **An MQTT broker** reachable from the Pi (e.g. running standalone Mosquitto, Homey, or Home Assistant)
- Basic comfort with SSH and the command line

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
5. **Low-RAM board (Pi Zero W/2 W)?** You need swap while dependencies compile. Check what you have:
   ```bash
   swapon --show
   ```

   | Output contains | What to do |
   |---|---|
   | `/dev/zram0` | **Nothing — go straight to Part 2.** zram is compressed RAM swap; it costs the SD card nothing. This is the default on current Raspberry Pi OS Lite. |
   | `/var/swap` | Raise it — see below |
   | nothing at all | `sudo apt install -y dphys-swapfile`, then raise it — see below |

   <details>
   <summary><b>Raising file-backed swap</b> (only for the last two rows)</summary>

   ```bash
   grep CONF_SWAPSIZE /etc/dphys-swapfile   # note this value, you restore it in Part 3
   sudo nano /etc/dphys-swapfile
   ```
   Set `CONF_SWAPSIZE=1024`, save, then:
   ```bash
   sudo systemctl restart dphys-swapfile
   swapon --show   # confirm the new size
   ```
   > [!IMPORTANT]
   > Temporary. Part 3 restores it — a permanent 1 GB swapfile wears out the SD card.
   </details>

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

**When it finishes, Part 3 is done — continue to Part 4.** The two boxes below are only for the cases they name.

<details>
<summary>The install was <b>Killed</b>, or raised <b>MemoryError</b></summary>

Most likely on a 512 MB board with only zram swap. Add disk swap just for the build, then remove it:
```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
# re-run the pip command above, then:
sudo swapoff /swapfile && sudo rm /swapfile
```
</details>

<details>
<summary>You raised <code>CONF_SWAPSIZE</code> in Part 1 step 5 — restore it now</summary>

Does not apply if `swapon --show` showed `/dev/zram0`.
```bash
sudo nano /etc/dphys-swapfile
```
Restore `CONF_SWAPSIZE` to the value you noted (`100` if you installed `dphys-swapfile` yourself), save, then:
```bash
sudo systemctl restart dphys-swapfile
free -h   # Swap should be back to its original size
```
The bridge warns at startup if this is left raised.
</details>

---

## Part 4 — Configure it

All settings live in a config file that's loaded as environment variables by `systemd`. This keeps secrets (MQTT password, etc.) out of the script and out of git.

Copy the example configuration file and edit it:
```bash
cp mower.env.example mower.env
nano mower.env
```

Configure your MQTT broker connection details, mower PIN, and settings inside `mower.env`. All options are fully documented with inline comments directly inside [mower.env.example](mower.env.example).

### Finding your mower's details

- **MAC address**: **nothing to do.** `mower.env.example` already ships `MOWER_ADDRESS=AA:BB:CC:DD:EE:FF`, which turns on autodiscovery. The bridge finds the mower on first run and writes the real address back into `mower.env` for you.

  <details>
  <summary>Setting it manually instead</summary>

  Check the official Gardena Bluetooth app's device info screen, or scan:
  ```bash
  bluetoothctl scan on
  ```
  Look for your mower's name, then `bluetoothctl scan off`. Put the address in `MOWER_ADDRESS`.
  </details>

- **PIN**: the same PIN you use in the official Gardena app (Settings → Bluetooth). Default is often `1234` if never changed. It must be physically entered on the mower's buttons during pairing (mapping details are documented inline in [mower.env.example](mower.env.example)).

---

## Part 5 — First run: pairing

Before setting up the service, run it directly so you can see what's happening live:

```bash
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ""|"#"*) continue ;; esac
  export "$line"
done < mower.env
python3 mower_mqtt.py
```
<details>
<summary>Why the loop, and not <code>source mower.env</code></summary>

It reads `mower.env` the same way `systemd`'s `EnvironmentFile=` will in Part 6 — the value is everything after the first `=`, taken literally. `source mower.env` and `export $(... | xargs)` both apply shell quoting, which mangles any password containing a space, `$`, `#` or a backtick.
</details>

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

3. **Resume normal operation**: Once pairing is confirmed, physically press the **Start** (or **Play**) button on the mower's keypad and **close the cover/hatch**. This returns the mower to its normal scheduled/docked state.
   > [!NOTE]
   > If you skip this, the mower will remain in a `STOPPED` state (reported as a "Safety Stop" or "Requires manual action" in smart home platforms) because the hatch is open and no start command was confirmed after the PIN entry.

Once this works, stop it with `Ctrl+C` and move on.

---

## Part 6 — Run as a systemd service

```bash
sudo nano /etc/systemd/system/mower-mqtt.service
```

```ini
[Unit]
Description=Gardena Mower BLE to MQTT Bridge
After=network-online.target bluetooth.service bt-agent.service
Wants=network-online.target bt-agent.service

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

<details>
<summary>Why <code>Wants=</code> and not <code>Requires=</code></summary>

The pairing agent is only needed while pairing. With `Requires=`, a `bt-agent` that fails to come back during auto-recovery would stop this service — which `Restart=always` does not undo.
</details>

Enable and start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mower-mqtt.service
journalctl -u mower-mqtt.service -f   # follows the log — Ctrl+C to exit and continue
```

From now on, this bonding is remembered by the host — you will **not** need to repeat the pairing steps on future restarts or reboots.

---

## Bluetooth stack auto-recovery (optional, recommended)
This will automatically restart the Bluetooth stack if it permanently crashes.

The escalation goes over polkit, not `sudo`. On Raspberry Pi OS Lite, polkit is not always installed — without it every rule below is silently ignored:
```bash
sudo apt install -y polkitd
```

```bash
sudo tee /etc/polkit-1/rules.d/49-mower-mqtt.rules > /dev/null <<'EOF'
polkit.addRule(function(action, subject) {
    if (subject.user === "pi" &&
        action.id === "org.freedesktop.systemd1.manage-units" &&
        (action.lookup("unit") === "bluetooth.service" || action.lookup("unit") === "bt-agent.service")) {
        return polkit.Result.YES;
    }
});
polkit.addRule(function(action, subject) {
    if (subject.user === "pi" &&
        (action.id === "org.freedesktop.login1.reboot" ||
         action.id === "org.freedesktop.login1.reboot-multiple-sessions")) {
        return polkit.Result.YES;
    }
});
EOF
```
If you used a different username than `pi` when creating `mower-mqtt.service` earlier, use that instead.

Verify:
```bash
sudo systemctl restart polkit
pkcheck --action-id org.freedesktop.login1.reboot --process $$ && echo "reboot authorized"
```
Tuning: `BLUETOOTH_RESTART_*` in `mower.env.example`. Set `BLUETOOTH_RESTART_THRESHOLD=0` to turn the whole escalation off.

### Last-resort host reboot (optional, off by default)
This will automatically reboot the host if the Bluetooth stack is wedged (a dead D-Bus socket) even after `bluetooth.service` has been restarted.

> [!IMPORTANT]
> A mower that simply does not answer — out of range, powered off, away for the winter, in for service, or the official Gardena app holding its single BLE slot — is **not** a host fault and never triggers a reboot. Only a dead D-Bus socket does.

```bash
cd ~/GardenaMower-BLE-MQTT
nano mower.env
```
Change `# REBOOT_ESCALATION_ENABLED=false` to `REBOOT_ESCALATION_ENABLED=true`, then save (Ctrl+O, Enter) and exit (Ctrl+X).

> [!WARNING]
> Reboots are the most write-intensive thing a Pi does. On an SD-card Pi Zero W, do not raise `REBOOT_MAX_ATTEMPTS` or lower `REBOOT_COOLDOWN`.

<details>
<summary>How the ladder is bounded, and how to reset it</summary>

It will not reboot-loop your Pi. Each reboot waits twice as long as the last (1h, 2h, 4h, 8h, 16h, then 24h), and it gives up entirely after `REBOOT_MAX_ATTEMPTS` (10) or `REBOOT_GIVE_UP_AFTER` (7 days), whichever comes first — roughly 10 reboots over 6.5 days, then it stops and logs `Giving up on reboot escalation`. A successful poll clears the ladder.

`reboot-multiple-sessions` in the polkit rule above is required whenever anyone else is logged in — without it the reboot fails while you are on SSH and succeeds when you are not.

Counters live in `.mower_restart_state` in the project directory. Reset them by hand with:
```bash
rm -f ~/GardenaMower-BLE-MQTT/.mower_restart_state
```

Tuning: see `REBOOT_*` examples in `mower.env.example`.
</details>

---

## Coexisting with the official Gardena app

A BLE mower accepts only **one** connection at a time. This bridge connects only briefly during each poll cycle (roughly every `MOWER_POLL` seconds) and for the moment it takes to send a command, then disconnects — it never holds the connection open. The official app should be able to connect the rest of the time without a fight. For guaranteed conflict-free access (e.g. a firmware update), publish `BRIDGE_PAUSE` to `<MOWER_BASE_TOPIC>/command` and `BRIDGE_RESUME` when you're done. While paused the bridge counts no poll failures, so it will not escalate.

---

## Updating to a new version

```bash
cd ~/GardenaMower-BLE-MQTT
git pull
source .venv/bin/activate
pip3 install --no-cache-dir --ignore-requires-python --upgrade -r requirements.txt
pip3 install --no-cache-dir --force-reinstall --no-deps git+https://github.com/gruijter/AutoMower-BLE.git@combined-fixes
sudo systemctl restart mower-mqtt.service
```
No pairing repeat needed — bonding is preserved on the host.

<details>
<summary><b>Upgrading from 1.6.x?</b> Three one-time steps (skip on a fresh install)</summary>

`git pull` only updates the project directory. These three fixes live outside it:

1. **Swap.** If you raised it during install and never put it back, the SD card has been running on a 1 GB swapfile ever since:
   ```bash
   grep CONF_SWAPSIZE /etc/dphys-swapfile   # 1024 = never restored
   ```
   Restore it as in Part 3. From 1.7.0 the bridge also warns about this at startup:
   ```
   Swap is 1024 MB (/var/swap) on a 432 MB board. If you raised it to build the dependencies, restore it
   ```
2. **Polkit rule.** Re-run the `tee` command in [Bluetooth stack auto-recovery](#bluetooth-stack-auto-recovery-optional-recommended) — it gained `reboot-multiple-sessions`, without which reboots fail whenever anyone is logged in.
3. **Unit file.** In `/etc/systemd/system/mower-mqtt.service`, replace the `After=`/`Requires=` lines with the `After=`/`Wants=` lines from Part 6, then:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart mower-mqtt.service
   ```

`mower.env` is untouched — the new `REBOOT_*` options all default safely. Existing counters in `.mower_restart_state` carry over.
</details>

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
- **Status stops updating after a while**: check `journalctl -u mower-mqtt.service -f` for errors; the bridge automatically reconnects on BLE drops. A fully unreachable mower (out of range, powered off) publishes `offline` and shows no new status until it's reachable again. The bridge does restart its own process every ~12 minutes while unreachable, and may restart `bluetooth.service`, but it will not reboot the host for this.
- **The Pi reboots by itself**: only possible with `REBOOT_ESCALATION_ENABLED=true`. Check the history and the counters:
  ```bash
  last -x reboot | head
  cat ~/GardenaMower-BLE-MQTT/.mower_restart_state
  grep -a "rebooting the host\|Giving up on reboot" ~/GardenaMower-BLE-MQTT/logs/mower.log
  ```
  Set `REBOOT_ESCALATION_ENABLED=false` to stop it immediately. Repeated reboots point at a genuinely wedged Bluetooth adapter — a USB BLE dongle is usually the real fix.
- **Still stuck**: open an issue on the [GitHub repository](https://github.com/gruijter/GardenaMower-BLE-MQTT) with your `journalctl` output attached.
