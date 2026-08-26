# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Starts at 1.5.0 — earlier history is in `git log`.

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
