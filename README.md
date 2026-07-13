# GardenaMower-BLE-MQTT

A bridge that connects a **Gardena or Husqvarna robotic mower with Bluetooth (BLE)** to **MQTT**. Supported models include Gardena Sileno Minimo, Sileno City, and similar — and because Husqvarna and Gardena are the same parent company sharing the same BLE protocol, many **Husqvarna Automower** models are compatible too.

This allows you to monitor and control your mower from **Homey** (or Home Assistant, or any other MQTT-capable smart home platform) without any cloud accounts or Gardena/Husqvarna Smart Gateways.

---

## Installation Guides

To install and run the bridge, please refer to one of the dedicated installation documents:

* 🐳 **[INSTALL-DOCKER.md](INSTALL-DOCKER.md)** (Docker Installation Guide - Recommended): The fastest way to get up and running inside a Docker container.
* ⚙️ **[INSTALL-NATIVE.md](INSTALL-NATIVE.md)** (Native Installation Guide): Run the script natively as a systemd service directly on your host system (e.g. Raspberry Pi OS).

---

## MQTT Interface Reference

The bridge uses the mower's **Bluetooth MAC address** as the unique device identifier in all MQTT topics. Colons are replaced with underscores, e.g. `AA:BB:CC:DD:EE:FF` → `AA_BB_CC_DD_EE_FF`.

### Topics Structure

| Topic | Retained | Description |
|---|---|---|
| `<MOWER_BASE_TOPIC>/<AA_BB_CC_DD_EE_FF>/status` | ✅ yes | Full JSON status payload — cached by broker, delivered immediately on subscribe |
| `<MOWER_BASE_TOPIC>/<AA_BB_CC_DD_EE_FF>/command` | no | Publish commands here to control the mower |
| `<MOWER_BASE_TOPIC>/<AA_BB_CC_DD_EE_FF>/availability` | ✅ yes | Bridge status: `online` / `offline`. Uses MQTT Last Will Testament — broker auto-publishes `offline` on crash |
| `<MOWER_BASE_TOPIC>/<AA_BB_CC_DD_EE_FF>/mower` | ✅ yes | Mower reachability: `online` when last BLE poll succeeded, `offline` after 2 consecutive failed polls |

**Combined state interpretation:**

| `availability` | `mower` | Meaning |
|---|---|---|
| `online` | `online` | Bridge running ✅, mower responding ✅ |
| `online` | `offline` | Bridge running ✅, mower unreachable ⚠️ |
| `offline` | `offline` | Bridge is down ❌ |

---

## Output Payload (Status JSON)

Below is the complete list of keys that can be published in the JSON status payload. Availability of specific sensor values (like pitch/roll or radar) depends on your specific mower hardware model.

```json
{
  "Battery": "100",
  "Charging": "OFF",
  "State": "RESTRICTED",
  "Activity": "PARKED",
  "NextStartSchedule": "2026-07-07T15:00:00+00:00",
  "LastError": "UNKNOWN",
  "LastErrorSchedule": null,
  "CurrUpdateSchedule": "2026-07-07T08:50:55Z",
  "mowerLocalTime": "2026-07-13T12:09:17",
  "Manufacturer": "Gardena",
  "Model": "SILENO Minimo 250",
  "SerialNumber": "123456789",
  "MowerName": "SILENO model 250",
  "Schedule": "Tue,Fri 15:00 (1h30m)",
  "RSSI": -74,
  "RemainingMowTime": 0,
  "totalRunningTime": 12345,
  "totalCuttingTime": 10000,
  "totalChargingTime": 2345,
  "totalSearchingTime": 120,
  "numberOfCollisions": 42,
  "numberOfChargingCycles": 150,
  "cuttingBladeUsageTime": 9800,
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
  "SoftwareVersion": "50.x",
  "SoftwareBundle": "1234567-01A",
  "SwVersionBoot": "0.104_Main-Boot-P005_50.2",
  "SwVersionAppl": "0.5_Main-App-P005_50.2",
  "SwVersionSub": "0.50_Sub-App_6.15",
  "ProductionTime": "2023-05-15T08:00:00+00:00",
  "NodeIprId": "Automower",
  "HusqvarnaId": "AABBCC01X1Y2000000000001",
  "HwSerialNumber": "11223344",
  "HardwareRevision": "1",
  "SupportedAccessories": 0,
  "SoftwarePackageVersion": "1234567-01A_P005G-SwPkg_50.x",
  "customMowDuration": 3600
}
```

---

## Input Commands

Publish any of the following raw strings to `<MOWER_BASE_TOPIC>/<AA_BB_CC_DD_EE_FF>/command` to trigger the corresponding operation.

> **Note**: Not all commands are supported by every mower model. Commands that target hardware not present on your mower (e.g. `RADAR_ENABLED` on a model without a collision radar) are accepted by the bridge but silently ignored or return an error from the mower. The status JSON reflects which features are actually available on your specific device.

### Mower Activity & Scheduling Commands
* **`MOW`**: Logs in with operator PIN, sets mode to AUTO, overrides standard schedule using custom duration, and issues physical start trigger.
* **`PARK`**: Parks the mower until the next scheduled start.
* **`PARK_PERMANENTLY`**: Parks the mower indefinitely.
* **`RESUME_SCHEDULE`**: Clears override settings and returns to the scheduled automatic timer.
* **`PAUSE`**: Pauses the mower's physical movement.
* **`RESUME`**: Resumes mower operation (issues physical start trigger).
* **`SPOT_CUT`**: Initiates a spiral spot cutting pattern at the current location.
* **`STOP_SPOT_CUT`**: Stops the active spot cutting program.

### Configuration & Maintenance Commands
* **`DRIVE_PAST_WIRE <distance>`**: Sets drive-past-wire distance (in cm, e.g. `DRIVE_PAST_WIRE 30`).
* **`REVERSING_DISTANCE <distance>`**: Sets reverse-out distance (in mm, e.g. `REVERSING_DISTANCE 600`).
* **`GARAGE_ENABLED <ON/OFF>`**: Enable/disable garage collision avoidance mode (e.g. `GARAGE_ENABLED ON`).
* **`RADAR_ENABLED <ON/OFF>`**: Enable/disable collision radar (e.g. `RADAR_ENABLED OFF`).
* **`ECO_MODE <ON/OFF>`**: Toggles loop signal Eco Mode (disables loop generator when parked/charging to save power).
* **`MOW_DURATION <seconds>`**: Sets the manual mow override duration used by the `MOW` command (0–28800 seconds, e.g. `MOW_DURATION 3600`). Current value is always visible in the status JSON as `customMowDuration`.
* **`GENERATE_LOOP_SIGNAL`**: Generates a new loop signal synchronization code.
* **`SET_TIME`**: Syncs the mower's internal clock with the host container's local clock.
* **`SET_TIME <epoch>`**: Sets the mower's internal clock to a specific Unix epoch timestamp (e.g. `SET_TIME 1783933759`).

### Bridge System Commands
* **`BRIDGE_PAUSE`**: Pauses the bridge's background BLE polling loop, freeing the single Bluetooth slot so the official Gardena app can connect continuously.
* **`BRIDGE_RESUME`**: Resumes normal background BLE polling.

---

## Credits

- [andyb2000/AutoMower-BLE-MQTT](https://github.com/andyb2000/AutoMower-BLE-MQTT) — the original script this project is based on
- [alistair23/AutoMower-BLE](https://github.com/alistair23/AutoMower-BLE) — the underlying BLE protocol library

---

## License

This project is licensed under the GPL-3.0 License. See the [LICENSE](file:///home/robin/HomeyDev/GardenaMower-BLE-MQTT/LICENSE) file for the full text.
