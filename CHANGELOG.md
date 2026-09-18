# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Starts at 1.5.0 — earlier history is in `git log`.

## [1.7.1] - 2026-09-18

### Added
- `lift` falls back to `GetLiftSensorLegacy` (4476/6) on mowers where `GetComboardSensorData` is unsupported.

### Fixed
- `FROST_SENSOR` wrote to register 4476, which is lift sensing, not frost. On models where `GetFrostSensorEnabled` is unsupported this fallback ran first. Frost is 5370 only.
- `frostSensorEnabled` reported `ON` whenever the mower was lifted, because the 4476 reading was OR'd into it.
- `FROST_SENSOR` raised `KeyError`: `SetFrostSensorEnabledLegacy` no longer exists in the `automower-ble` dependency.
- A mower answering `GetBatteryLevel` with 0% on alternating polls no longer publishes 0%. The last plausible level is held for up to 3 polls; a level the mower confirms with `LOW_BATTERY`/`EMPTY_BATTERY`, a 0% that persists, and the first reading after startup are all published as-is.
- `batteryVoltage` is read as signed. A negative reading arrived as 65531 and was published as 65.53 V; non-positive readings are now dropped.
- `GetBatteryCurrent` and `GetBatteryTemperature` are cached as unsupported when the mower answers with a payload the parser rejects, instead of being retried every poll.

### Removed
- `GetFrostSensorEnabledLegacy` / `SetFrostSensorEnabledLegacy`, both gone from the `automower-ble` dependency. Register 4476/6 is now read as `GetLiftSensorLegacy`; 4476/5 is no longer written to at all.

## [1.7.0] - 2026-09-06

### Added
- `REBOOT_COOLDOWN_MAX`, `REBOOT_MAX_ATTEMPTS`, `REBOOT_GIVE_UP_AFTER`: reboot escalation now backs off exponentially (1h, 2h, 4h, 8h, 16h, then 24h) and gives up after 10 reboots or 7 days.
- `BLUETOOTH_RESTART_THRESHOLD=0` disables the escalation.
- `RESTART_STATE_FILE` overrides where the escalation counters are stored. The reference `docker-compose.yml` points it at an optional `./state` volume, so counters survive container recreation.
- Startup warning when a low-RAM board is left on a large disk-backed swapfile. zram is excluded.
- Bridge version is published to MQTT: `BridgeVersion` in the status payload, and a retained `<base>/<mac>/bridge` topic sent on connect (readable even when the mower has never been reachable).

### Changed
- Host reboot only fires on a wedged D-Bus socket. An unreachable mower — out of range, powered off, or the official app holding the BLE slot — escalates no further than a `bluetooth.service` restart.
- A successful poll clears the reboot ladder, not just the restart streak.
- `.mower_restart_state` is written atomically and fsync'd, before the reboot is requested.
- Service unit orders after `network-online.target` and `bluetooth.service` instead of `network.target` and `bluetooth.target`, and uses `Wants=bt-agent.service` instead of `Requires=` so a pairing agent that fails to restart during auto-recovery can't stop the bridge.

### Fixed
- Reboot escalation could reboot the host every ~75 minutes indefinitely while the mower was unreachable. `REBOOT_COOLDOWN` never bound, because reaching the reboot tier already took longer than the cooldown.
- `BLUETOOTH_RESTART_THRESHOLD=0` escalated on every failure instead of disabling escalation.
- `bt_escalations` grew without bound across a long outage.
- Swap raised to 1 GB in INSTALL-NATIVE.md Part 1 was never restored.
- INSTALL-NATIVE.md Part 1 step 5 assumed `/etc/dphys-swapfile` exists. It now checks `swapon --show` first and covers zram, no swap, and a missing `dphys-swapfile` package.
- Polkit rule missed `reboot-multiple-sessions`, so reboots failed whenever anyone was logged in. `polkitd` install now documented.
- INSTALL-NATIVE.md Part 5 loaded `mower.env` through shell quoting, so it did not match `systemd`'s `EnvironmentFile=` and mangled any password containing a space, `$`, `#` or a backtick.
- Docker: escalation counters lived in the container layer, so `docker compose up -d` reset the reboot back-off. They can now be persisted via `./state`.
- Docker: restarting the non-existent host `bt-agent.service` logged a misleading polkit error. The container runs its own agent, so this is now skipped quietly.
- A BLE connect timeout counted as a wedged Bluetooth stack. On Python >= 3.11 `asyncio.TimeoutError` is the builtin `TimeoutError`, which subclasses `OSError`, so the broad D-Bus check matched it. Reboot escalation now uses an explicit D-Bus-socket test; process restarts are unchanged.
- A state file holding a wrong type raised out of `escalate_and_exit` and was swallowed by the callers, disabling auto-recovery silently. Values are now coerced per key.
- A reboot refused by polkit still counted as a reboot, so the ladder ended by reporting reboots that never happened. Refusals are now logged and counted as attempts.
- A state file that cannot be written is logged at warning level instead of debug — this is what a read-only SD card looks like.

## [1.6.1] - 2026-09-01

### Changed
- `Status:` log line includes battery voltage, current and temperature when the mower reports them.

## [1.6.0] - 2026-09-01

### Added
- `OverrideAction`, `OverrideStartSchedule`, `OverrideDuration` in published status.
- `MowPending` / `MowStartsAt`: a forced mow accepted while the mower still charges in the dock.
- `remainingChargingTime` (optional, mower-dependent).

### Changed
- `GetOverride` is polled every cycle, not only while mowing.

### Fixed
- `NextStartSchedule` publishes `null` instead of `1970-01-01` when the mower reports no next start.
- Remaining mow time used the wrong clock domain when `GetTime` is unsupported.

## [1.5.0] - 2026-08-25

### Added
- Bluetooth stack auto-recovery: restarts `bluetooth.service` after repeated failed reconnects, with an opt-in host reboot as a further fallback (`REBOOT_ESCALATION_ENABLED`). See `INSTALL-NATIVE.md`.
- `SET_SCHEDULE` / `CLEAR_SCHEDULE`, `STARTING_POINT_*`, `RESET_BLADE_USAGE` commands.
- `REFRESH_INFO` command to force a full re-fetch of cached mower info.
- `ScheduleTasks` field in published status (structured weekly schedule).
- Version number logged at startup.
- `LOG_FILE` rotation now gzips backups and keeps 10 of them, with noisy duplicate debug lines filtered — same disk space, much longer retention.

### Changed
- Cached schedule now refreshes periodically (`MOWER_SCHEDULE_POLL`) and after every schedule write, not just once at startup.

### Fixed
- `LastError` defaults to `UNKNOWN` outside error states, so Homey clears `alarm_stuck` correctly.
- Critical D-Bus failures and persistent connectivity loss now force a clean process restart.
