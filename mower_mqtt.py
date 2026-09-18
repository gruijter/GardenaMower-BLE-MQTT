#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# GardenaMower-BLE-MQTT mower_mqtt.py
#
# Copyright (c) 2026 Robin de Gruijter (gruijter@hotmail.com)
# Based on mower_mqtt.py by Andy Brown https://github.com/andyb2000/AutoMower-BLE-MQTT/
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
VERSION = "1.7.1"

import asyncio
import gzip
import json
import logging
import os
import shutil
import sys
import datetime as dt
import signal
import contextlib
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple, Awaitable, Callable

from bleak import BleakScanner
import aiomqtt

# Add local library path
LOCAL_LIB = "/usr/src/AutoMower-BLE.git"
if LOCAL_LIB not in sys.path:
    sys.path.insert(0, LOCAL_LIB)

from automower_ble.mower import Mower
from automower_ble.protocol import MowerState, MowerActivity, ModeOfOperation, OverrideAction, ResponseResult, TaskInformation
from automower_ble.error_codes import ErrorCodes

# ----------------------------
# Logging
# ----------------------------
_log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_str, logging.INFO)
_log_file = os.getenv("LOG_FILE")

_handlers = [logging.StreamHandler(sys.stdout)]
if _log_file:
    try:
        _log_dir = os.path.dirname(_log_file)
        if _log_dir:
            os.makedirs(_log_dir, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        # gzip each rotated backup — DEBUG logs are highly repetitive text
        # (hex frames, D-Bus boilerplate) and compress ~15-18x, so many more
        # backups fit in the same disk budget. `namer` must add the .gz
        # suffix (not the rotator) since RotatingFileHandler also uses it,
        # via plain os.rename, to shift older backups .1.gz -> .2.gz etc.
        def _gzip_rotator(source, dest):
            with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.remove(source)

        # 1MB per (uncompressed) segment, 10 gzipped backups — combined with
        # the bleak noise suppression below, comfortably covers several days
        # of DEBUG logging in less disk space than the old 4MB/3-backup setup.
        _file_handler = RotatingFileHandler(_log_file, maxBytes=1024 * 1024, backupCount=10, encoding="utf-8")
        _file_handler.rotator = _gzip_rotator
        _file_handler.namer = lambda name: name + ".gz"
        _handlers.append(_file_handler)
    except Exception as _e:
        sys.stderr.write(f"Failed to initialize file logger for {_log_file}: {_e}\n")

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
LOG = logging.getLogger("mower_mqtt")

if _log_level == logging.DEBUG:
    logging.getLogger("automower_ble.mower").setLevel(logging.DEBUG)
    logging.getLogger("automower_ble.protocol").setLevel(logging.DEBUG)
else:
    logging.getLogger("automower_ble.mower").setLevel(logging.ERROR)
    logging.getLogger("automower_ble.protocol").setLevel(logging.WARNING)

# bleak's own bluezdbus backend logs every raw D-Bus notification signal and
# GATT characteristic write at DEBUG — almost entirely duplicating what
# automower_ble.protocol already logs a line above/below (same bytes,
# different framing). Those two message patterns alone made up >90% of
# bleak's DEBUG lines in a captured session, so they're dropped even in
# DEBUG mode — but everything else from bleak stays, since it's the rare,
# genuinely useful connection-lifecycle info (connect/pair/disconnect/etc.)
# that a blanket level bump would otherwise throw away too.
class _BleakNoiseFilter(logging.Filter):
    _NOISY_PREFIXES = ("received D-Bus signal:", "Write Characteristic ")

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith(self._NOISY_PREFIXES)


_bleak_noise_filter = _BleakNoiseFilter()
logging.getLogger("bleak.backends.bluezdbus.manager").addFilter(_bleak_noise_filter)
logging.getLogger("bleak.backends.bluezdbus.client").addFilter(_bleak_noise_filter)

# ----------------------------
# Configuration
# ----------------------------
@dataclass
class Config:
    mqtt_broker: str = os.getenv("MQTT_HOST", "localhost")
    mqtt_port: int = int(os.getenv("MQTT_PORT", 1883))
    mqtt_username: str = os.getenv("MQTT_USER", "")
    mqtt_password: str = os.getenv("MQTT_PASS", "")
    mqtt_base_topic: str = os.getenv("MOWER_BASE_TOPIC", "mower_ble")
    poll_interval: int = int(os.getenv("MOWER_POLL", 60))
    schedule_poll_interval: int = int(os.getenv("MOWER_SCHEDULE_POLL", 900))
    mower_address: str = os.getenv("MOWER_ADDRESS", "00:00:00:00:00:00")
    mower_pin: int = int(os.getenv("MOWER_PIN", "1234"))
    # After this many consecutive process restarts that never reach a
    # successful poll, escalate to restarting the bluetooth stack itself
    # (a plain process restart can't clear a wedged BlueZ/D-Bus adapter).
    # 0 disables the bluetooth-restart tier entirely.
    bt_restart_threshold: int = max(0, int(os.getenv("BLUETOOTH_RESTART_THRESHOLD", 3)))
    # Minimum seconds between two bluetooth.service restart attempts.
    bt_restart_cooldown: int = int(os.getenv("BLUETOOTH_RESTART_COOLDOWN", 900))
    # Rebooting the host is disabled by default — it's a much bigger
    # blast radius than restarting bluetooth.service (takes down everything
    # else on the machine too), so it must be opted into explicitly.
    reboot_escalation_enabled: bool = os.getenv("REBOOT_ESCALATION_ENABLED", "false").strip().lower() == "true"
    # After this many bluetooth.service restart escalations still fail to
    # recover the connection, reboot the host as a last resort.
    reboot_after_bt_escalations: int = max(1, int(os.getenv("REBOOT_AFTER_BT_ESCALATIONS", 2)))
    # Back-off before the first reboot of an episode; doubles after every
    # reboot, up to reboot_cooldown_max.
    reboot_cooldown: int = int(os.getenv("REBOOT_COOLDOWN", 3600))
    # Ceiling for the doubling back-off, so it can't grow past a day.
    reboot_cooldown_max: int = int(os.getenv("REBOOT_COOLDOWN_MAX", 86400))
    # Hard stops on the ladder: give up after this many reboots, or this many
    # seconds since the episode's first reboot, whichever comes first. With
    # the defaults both land at roughly a week.
    reboot_max_attempts: int = int(os.getenv("REBOOT_MAX_ATTEMPTS", 10))
    reboot_give_up_after: int = int(os.getenv("REBOOT_GIVE_UP_AFTER", 604800))

CFG = Config()

# ----------------------------
# Shutdown handling
# ----------------------------
shutdown_event = asyncio.Event()

async def shutdown():
    LOG.info("Shutting down tasks...")
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    LOG.info("All tasks cancelled.")

def _handle_sigterm(*_):
    LOG.warning("Received termination signal, shutting down...")
    shutdown_event.set()
    loop = asyncio.get_event_loop()
    loop.create_task(shutdown())

signal.signal(signal.SIGINT, _handle_sigterm)
signal.signal(signal.SIGTERM, _handle_sigterm)

# ----------------------------
# Heartbeat Watchdog
# ----------------------------
WATCHDOG_TIMEOUT = 90  # seconds
last_heartbeat = 0

custom_mow_duration = 3600  # default

# When True, the bridge stays fully off BLE (no polling, no commands)
# so the official Gardena app has exclusive, guaranteed access.
# Toggle with payload "BRIDGE_PAUSE" / "BRIDGE_RESUME" on the normal command topic.
bridge_paused = False

# Cache of one-time/slow-changing mower info (model, serial, schedule, ...).
# Populated on first successful poll, refreshed periodically (schedule only,
# since it's the one field in here that can change post-startup) and
# immediately after a schedule write. REFRESH_INFO forces a full re-fetch.
static_info: Dict[str, Any] = {}
last_schedule_refresh: float = 0.0


def watchdog_reset():
    global last_heartbeat
    last_heartbeat = asyncio.get_event_loop().time()


async def heartbeat_task(availability_topic: str):
    """Heartbeat watchdog: ensures event loop is alive."""
    watchdog_reset()
    while not shutdown_event.is_set():
        await asyncio.sleep(10)
        now = asyncio.get_event_loop().time()
        if now - last_heartbeat > WATCHDOG_TIMEOUT:
            LOG.critical("Watchdog: Event loop stalled for >%d seconds, shutting down!", WATCHDOG_TIMEOUT)
            shutdown_event.set()
            try:
                async with aiomqtt.Client(
                    hostname=CFG.mqtt_broker,
                    port=CFG.mqtt_port,
                    username=CFG.mqtt_username,
                    password=CFG.mqtt_password,
                ) as client:
                    await client.publish(availability_topic, "offline", retain=True)
            except Exception:
                LOG.error("Failed to publish offline status to MQTT")
            os._exit(1)

# ----------------------------
# Bluetooth stack recovery escalation
# ----------------------------
# A plain process restart (systemd Restart=always) gives the app a fresh
# D-Bus socket, but it can't clear a wedged BlueZ adapter/bluetoothd — that
# needs bluetooth.service itself restarted, or (if even that doesn't help)
# the whole host rebooted. We track failed-restart streaks in a small state
# file (survives across process restarts, unlike an in-memory counter) and
# escalate in two tiers:
#   1. After CFG.bt_restart_threshold consecutive failed process restarts,
#      restart bluetooth.service + bt-agent.service.
#   2. After CFG.reboot_after_bt_escalations such restarts still don't fix
#      it, reboot the host.
#
# Tier 2 only runs for a *hard fault* — a dead D-Bus socket, i.e. a genuinely
# wedged stack. A mower that simply doesn't answer (out of range, powered off,
# in for service, or the official app holding its single BLE slot) is normal
# operation and must never reboot the host, however long it lasts.
#
# Tier 2 is also bounded in three directions, because a reboot that doesn't
# fix the fault will not fix it on the hundredth try either: each attempt
# waits twice as long as the last (CFG.reboot_cooldown doubling, capped at
# CFG.reboot_cooldown_max), and the ladder gives up entirely after
# CFG.reboot_max_attempts or CFG.reboot_give_up_after. A successful poll
# clears the whole episode. Without those bounds a permanently unreachable
# mower reboots the host every ~75 minutes forever, which is how you kill an
# SD card.
#
# Both actions go straight over the system D-Bus (via dbus_fast, already a
# transitive dependency of bleak) instead of shelling out to `systemctl`/
# `sudo` — this works identically for a privileged Docker container (which
# shares the host's D-Bus system bus and connects as root, so systemd/
# logind authorize it like any other root caller) and for the unprivileged
# `pi` user on a native install (which needs the polkit rule documented in
# INSTALL-NATIVE.md, since polkit — not sudoers — gates D-Bus callers).
# Defaults next to the script, which is persistent for a native install. In
# Docker that path is the container's writable layer — it survives a restart
# but NOT `docker compose up -d`, which recreates the container and would
# silently reset the reboot back-off. docker-compose.yml therefore points
# RESTART_STATE_FILE at a mounted volume.
_RESTART_STATE_FILE = os.getenv("RESTART_STATE_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".mower_restart_state"
)

_SYSTEMD_BUS = "org.freedesktop.systemd1"
_SYSTEMD_PATH = "/org/freedesktop/systemd1"
_SYSTEMD_IFACE = "org.freedesktop.systemd1.Manager"
_LOGIND_BUS = "org.freedesktop.login1"
_LOGIND_PATH = "/org/freedesktop/login1"
_LOGIND_IFACE = "org.freedesktop.login1.Manager"


_DEFAULT_RESTART_STATE: Dict[str, Any] = {
    "streak": 0,
    "last_escalation": 0.0,
    "bt_escalations": 0,
    "last_reboot": 0.0,
    "reboot_attempts": 0,
    "first_reboot": 0.0,
    "reboot_gave_up": False,
}


# Coerced on read, per key. A wrong type here used to raise out of
# escalate_and_exit and be swallowed by the callers' generic handlers, which
# disabled auto-recovery silently. Per-key rather than all-or-nothing so one
# unreadable field can't reset the reboot back-off to zero.
_RESTART_STATE_TYPES: Dict[str, Callable[[Any], Any]] = {
    "streak": lambda v: max(0, int(v)),
    "last_escalation": float,
    "bt_escalations": lambda v: max(0, int(v)),
    "last_reboot": float,
    "reboot_attempts": lambda v: max(0, int(v)),
    "first_reboot": float,
    "reboot_gave_up": bool,
}


def _read_restart_state() -> Dict[str, Any]:
    """Missing or unreadable keys fall back to the defaults, so a state file
    written by an older version upgrades in place instead of resetting the
    ladder."""
    state = dict(_DEFAULT_RESTART_STATE)
    try:
        with open(_RESTART_STATE_FILE, "r") as f:
            loaded = json.load(f)
    except Exception:
        LOG.debug("Could not read restart-escalation state, using defaults", exc_info=True)
        return state
    if not isinstance(loaded, dict):
        LOG.warning("Restart-escalation state file is not an object, using defaults")
        return state
    for key, cast in _RESTART_STATE_TYPES.items():
        if key not in loaded:
            continue
        try:
            state[key] = cast(loaded[key])
        except (TypeError, ValueError):
            LOG.warning("Ignoring unreadable %r in restart-escalation state", key)
    return state


def _write_restart_state(state: Dict[str, Any]) -> None:
    """Written atomically and fsync'd: the reboot back-off is only a back-off
    if it survives the reboot. A half-written or truncated state file reads
    back as the all-zero defaults, which would clear every cooldown on boot."""
    tmp = _RESTART_STATE_FILE + ".tmp"
    directory = os.path.dirname(_RESTART_STATE_FILE) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _RESTART_STATE_FILE)
    except Exception as e:
        # Counters that can't be persisted mean the escalation can never build
        # a streak — auto-recovery is effectively off. A read-only SD card
        # looks exactly like this, so don't bury it at debug level.
        LOG.warning("Could not persist restart-escalation state to %s (%s) — "
                    "auto-recovery escalation is disabled until this is fixed.",
                    _RESTART_STATE_FILE, e)
        with contextlib.suppress(Exception):
            os.unlink(tmp)
        return
    # Best-effort: the rename is already durable on ext4/overlayfs, and some
    # filesystems reject fsync on a directory fd outright.
    with contextlib.suppress(Exception):
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def mark_poll_success() -> None:
    """Call after a successful mower poll: the fault is over, so clear the
    whole escalation episode — streak, bluetooth escalations and the reboot
    ladder. The last_reboot/last_escalation timestamps are kept, so a
    flapping adapter still has to wait out the back-off."""
    state = _read_restart_state()
    if any((state["streak"], state["bt_escalations"], state["reboot_attempts"], state["reboot_gave_up"])):
        state["streak"] = 0
        state["bt_escalations"] = 0
        state["reboot_attempts"] = 0
        state["first_reboot"] = 0.0
        state["reboot_gave_up"] = False
        _write_restart_state(state)


async def _dbus_manager_call(bus_name: str, path: str, interface: str, member: str, signature: str, body: list) -> None:
    from dbus_fast import Message, MessageType
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bus.call(
            Message(destination=bus_name, path=path, interface=interface, member=member, signature=signature, body=body)
        )
        if reply is not None and reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body}")
    finally:
        bus.disconnect()


async def _restart_systemd_unit(unit: str, required: bool = True) -> bool:
    try:
        await _dbus_manager_call(_SYSTEMD_BUS, _SYSTEMD_PATH, _SYSTEMD_IFACE, "RestartUnit", "ss", [unit, "replace"])
        LOG.info("Restarted %s via systemd D-Bus API", unit)
        return True
    except Exception as e:
        # On a Docker install the pairing agent runs inside the container, so
        # the host has no bt-agent.service. That's expected, not a failure.
        if not required and "NoSuchUnit" in str(e):
            LOG.debug("%s not present on this host, skipping", unit)
            return False
        LOG.error(
            "Could not restart %s via D-Bus (%s) — see INSTALL-NATIVE.md for the "
            "required polkit rule (no-op without it).", unit, e,
        )
        return False


# Low-RAM boards are told to raise swap to 1 GB in INSTALL-NATIVE.md Part 1 so
# dependencies can compile, and Part 3 tells them to put it back. Plenty of
# installs never do, and a permanent 1 GB swapfile on an SD card wears it out.
# Split from the logging so it can be tested against fixture text.
_SWAP_WARN_MIN_MB = 512      # RPi OS ships 100-200 MB; the install guide sets 1024
_SWAP_WARN_MAX_RAM_MB = 1536  # only Pi Zero / Zero 2 / Pi 3 class boards


def _swap_warning(meminfo: str, swaps: str) -> Optional[str]:
    """Return a warning if this looks like a low-RAM board left on a big
    disk-backed swapfile, else None. zram is excluded — compressed RAM swap
    costs the card nothing."""
    mem_mb = 0
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            mem_mb = int(line.split()[1]) // 1024
            break
    if not mem_mb or mem_mb >= _SWAP_WARN_MAX_RAM_MB:
        return None

    backing = []
    for line in swaps.splitlines()[1:]:  # skip the header row
        parts = line.split()
        if len(parts) >= 3 and not parts[0].startswith("/dev/zram"):
            backing.append((parts[0], int(parts[2]) // 1024))
    total_mb = sum(mb for _, mb in backing)
    if total_mb < _SWAP_WARN_MIN_MB:
        return None

    return (
        f"Swap is {total_mb} MB ({', '.join(p for p, _ in backing)}) on a "
        f"{mem_mb} MB board. If you raised it to build the dependencies, restore "
        f"it — see INSTALL-NATIVE.md Part 3. A large swapfile on an SD card "
        f"wears the card out."
    )


def _warn_on_large_swap() -> None:
    try:
        with open("/proc/meminfo") as f:
            meminfo = f.read()
        with open("/proc/swaps") as f:
            swaps = f.read()
    except Exception:
        return  # not Linux, or /proc not mounted
    message = _swap_warning(meminfo, swaps)
    if message:
        LOG.warning("%s", message)


def _is_dbus_socket_failure(exc: BaseException) -> bool:
    """True only for a genuinely dead D-Bus socket — the one fault a host
    reboot can plausibly clear.

    Deliberately excludes TimeoutError. On Python >= 3.11 (both targets: RPi
    OS Bookworm ships 3.11, the image is 3.12) asyncio.TimeoutError *is* the
    builtin TimeoutError, which subclasses OSError — so a plain BLE connect
    timeout from a mower at the edge of range satisfies the broad
    `isinstance(e, OSError)` check the callers use, and would otherwise be
    misread as a wedged stack and count toward the reboot ladder."""
    if isinstance(exc, TimeoutError):
        return False
    text = str(exc)
    return (
        isinstance(exc, (EOFError, ConnectionError))
        or "EOFError" in text
        or "Bad file descriptor" in text
    )


async def _reboot_host() -> bool:
    try:
        await _dbus_manager_call(_LOGIND_BUS, _LOGIND_PATH, _LOGIND_IFACE, "Reboot", "b", [False])
        LOG.critical("Reboot requested via logind D-Bus API")
        return True
    except Exception as e:
        LOG.error(
            "Could not reboot via D-Bus (%s) — see INSTALL-NATIVE.md for the "
            "required polkit rule (no-op without it).", e,
        )
        return False


def _reboot_hold_off(attempts: int) -> float:
    """Seconds of back-off required before reboot number `attempts` + 1.
    Doubles per attempt from CFG.reboot_cooldown, capped at
    CFG.reboot_cooldown_max. With the defaults: 1h, 2h, 4h, 8h, 16h, then
    24h from there on — ~6 days of ladder before reboot_max_attempts."""
    return min(CFG.reboot_cooldown * (2 ** min(max(attempts, 0), 30)), CFG.reboot_cooldown_max)


def _reboot_allowed(state: Dict[str, Any], now: float) -> bool:
    """Tier-2 gate. Mutates `state` only to latch the give-up flag, so the
    'giving up' line is logged once rather than on every later escalation."""
    if not CFG.reboot_escalation_enabled:
        return False
    if state["bt_escalations"] < CFG.reboot_after_bt_escalations:
        return False

    attempts = state["reboot_attempts"]
    first_reboot = state["first_reboot"]
    expired = bool(first_reboot) and now - first_reboot >= CFG.reboot_give_up_after
    if attempts >= CFG.reboot_max_attempts or expired:
        if not state["reboot_gave_up"]:
            state["reboot_gave_up"] = True
            LOG.critical(
                "Giving up on reboot escalation after %d reboot attempts over %.1f "
                "days (some may have been refused — check for 'Could not reboot' "
                "above). The host needs manual attention. Bluetooth stack restarts "
                "continue; a successful poll re-arms the ladder.",
                attempts, (now - first_reboot) / 86400 if first_reboot else 0.0,
            )
        return False

    hold_off = _reboot_hold_off(attempts)
    waited = now - state["last_reboot"]
    if waited < hold_off:
        LOG.warning(
            "Reboot escalation held off — %.0f of %.0f min back-off remaining "
            "before attempt %d/%d.",
            (hold_off - waited) / 60, hold_off / 60, attempts + 1, CFG.reboot_max_attempts,
        )
        return False
    return True


async def escalate_and_exit(reason: str, hard_fault: bool = False) -> None:
    """Record a failed-restart streak and, once it crosses the threshold,
    try to restart the host's bluetooth stack (or, if that keeps failing to
    help and `hard_fault` says the stack itself is wedged, reboot the host)
    before exiting for systemd to restart this process.

    `hard_fault` must only be True for a dead D-Bus socket. An unreachable
    mower is not a host fault and never reaches tier 2, no matter how long
    it lasts. Always exits — never returns."""
    state = _read_restart_state()
    streak = state["streak"] + 1
    now = dt.datetime.now().timestamp()
    LOG.error("%s (failed-restart streak: %d)", reason, streak)

    if (
        CFG.bt_restart_threshold > 0
        and streak >= CFG.bt_restart_threshold
        and now - state["last_escalation"] >= CFG.bt_restart_cooldown
    ):
        # Clamp so the counter can't grow without bound across a long outage.
        state["bt_escalations"] = min(state["bt_escalations"] + 1, CFG.reboot_after_bt_escalations)
        state["streak"] = 0
        state["last_escalation"] = now

        if hard_fault and _reboot_allowed(state, now):
            state["reboot_attempts"] += 1
            state["last_reboot"] = now
            state["bt_escalations"] = 0
            if not state["first_reboot"]:
                state["first_reboot"] = now
            LOG.critical(
                "Bluetooth stack still wedged after %d restarts — rebooting the host "
                "(attempt %d/%d, next back-off %.0f min)...",
                CFG.reboot_after_bt_escalations, state["reboot_attempts"],
                CFG.reboot_max_attempts, _reboot_hold_off(state["reboot_attempts"]) / 60,
            )
            # Persist *before* asking logind to reboot: the call returns as
            # soon as shutdown is queued, and losing this write would reset
            # the back-off to zero on the next boot.
            _write_restart_state(state)
            if not await _reboot_host():
                # Still alive, so the reboot was refused (missing polkit rule
                # or no polkitd). The attempt is deliberately still counted —
                # retrying a refused call forever helps nobody — but say so,
                # or the give-up line below reads as if reboots had happened.
                LOG.error(
                    "Host was NOT rebooted — escalation attempt %d/%d counted anyway.",
                    state["reboot_attempts"], CFG.reboot_max_attempts,
                )
            sys.exit(1)

        LOG.critical(
            "Still unreachable after %d process restarts — restarting the "
            "bluetooth stack to clear a possibly wedged BlueZ/D-Bus adapter...",
            CFG.bt_restart_threshold,
        )
        await _restart_systemd_unit("bluetooth.service")
        await _restart_systemd_unit("bt-agent.service", required=False)
    else:
        state["streak"] = streak

    _write_restart_state(state)
    sys.exit(1)

# ----------------------------
# Helper Functions
# ----------------------------
async def connect_mower() -> Tuple[Optional[Mower], Optional[int]]:
    """Connect to the mower over BLE. Returns (mower, rssi_at_connect)."""
    try:
        LOG.info("Scanning for mower at %s...", CFG.mower_address)

        found_device = {}
        rssi_holder: Dict[str, int] = {}

        def _detect(device, adv_data):
            if device.address.upper() == CFG.mower_address.upper():
                found_device["device"] = device
                rssi_holder["rssi"] = adv_data.rssi

        scanner = BleakScanner(_detect)
        await scanner.start()
        for _ in range(100):  # up to ~10s
            if "device" in found_device:
                break
            await asyncio.sleep(0.1)
        await scanner.stop()
        await asyncio.sleep(1.0)  # Let BLE adapter settle after scanning

        device = found_device.get("device")
        if not device:
            LOG.warning(
                "Mower %s not found (out of range, or the official app is "
                "currently connected to it).",
                CFG.mower_address,
            )
            return None, None

        mower = Mower(1197489078, CFG.mower_address, CFG.mower_pin)
        res = await mower.connect(device)
        if res != 0:
            LOG.error("BLE connection handshake failed (result: %s)", res)
            return None, None
        rssi = rssi_holder.get("rssi")
        LOG.info("BLE connection established ✅ (RSSI: %s dBm)", rssi)
        return mower, rssi
    except Exception as e:
        LOG.exception("Failed to connect to mower")
        err_str = str(e)
        if isinstance(e, (EOFError, OSError)) or "EOFError" in err_str or "Bad file descriptor" in err_str:
            await escalate_and_exit(
                f"Critical D-Bus socket failure detected ({e}). Exiting process to trigger systemd restart...",
                hard_fault=_is_dbus_socket_failure(e),
            )
        return None, None


# Cache to track BLE commands that this specific mower model/firmware does not support
UNSUPPORTED_COMMANDS = set()
UNSUPPORTED_COMMANDS_WHITELIST = {
    "GetComboardSensorData",
    "GetSignalQuality",
    "GetSupportedAccessories",
    "GetNodeIprId",
    "GetAntiCollisionRadar",
    "GetFrostSensorEnabled",
    "GetEcoModeEnabled",
    "GetRemainingChargingTime",
    "GetTime",
    # The 305 answers these with a 1-byte payload the parser rejects, every poll
    "GetBatteryCurrent",
    "GetBatteryTemperature"
}

# Some mowers answer GetBatteryLevel with 0% on every other connection while the pack
# is actually full, so the reading alternates 99/0/99/0 for hours. A real discharge to
# empty never happens in a single poll, so hold the last plausible level across a short
# burst of zeroes -- but give up after BATTERY_ZERO_HOLD_POLLS so a mower that genuinely
# runs flat is still reported.
BATTERY_ZERO_HOLD_POLLS = 3
_last_good_battery: Optional[int] = None
_battery_zero_polls = 0
_battery_hold_warned = False


async def safe_mower_command(mower: Mower, cmd: str, optional: bool = False, **kwargs) -> Any:
    """Run mower command safely with timeout + retries, caching unsupported commands."""
    if cmd in UNSUPPORTED_COMMANDS:
        return None

    retries = 1 if optional else 3
    for attempt in range(1, retries + 1):
        try:
            # We call command_response to get both the status code and the parsed result.
            # We pass warn_on_error=False to suppress log spam for optional commands.
            status, result = await asyncio.wait_for(
                mower.command_response(cmd, warn_on_error=not optional, **kwargs),
                timeout=10
            )
            watchdog_reset()

            if status == ResponseResult.OK:
                # If an optional command successfully returns None (without timeout or exception),
                # and is on our whitelist of hardware/model-dependent features, cache it as unsupported.
                if result is None and optional and cmd in UNSUPPORTED_COMMANDS_WHITELIST:
                    LOG.debug("Optional command %s returned None (OK), caching as unsupported.", cmd)
                    UNSUPPORTED_COMMANDS.add(cmd)
                return result
            else:
                # Command failed on the mower (status is not OK)
                if optional:
                    LOG.debug("Optional command %s returned error status %s", cmd, status.name)
                    # If it's a clear indication that the command is unsupported/unrecognized by this hardware,
                    # cache it permanently to avoid future BLE queries.
                    if mower.is_connected() and status in (
                        ResponseResult.UNKNOWN_ERROR,
                        ResponseResult.NOT_AVAILABLE,
                        ResponseResult.INVALID_GROUP,
                        ResponseResult.INVALID_ID,
                    ):
                        LOG.info("Caching optional command %s as unsupported (returned %s)", cmd, status.name)
                        UNSUPPORTED_COMMANDS.add(cmd)
                    return None
                else:
                    LOG.error("Mower command %s failed with status %s (attempt %d/%d)", cmd, status.name, attempt, retries)
        except asyncio.TimeoutError:
            if optional:
                LOG.debug("Optional mower command %s timed out", cmd)
            else:
                LOG.warning("Mower command %s timed out (attempt %d/%d)", cmd, attempt, retries)
        except Exception as e:
            if optional:
                LOG.debug("Optional mower command %s failed: %s", cmd, e)
                if isinstance(e, (ValueError, UnicodeDecodeError)) and cmd in UNSUPPORTED_COMMANDS_WHITELIST:
                    LOG.debug("Optional command %s failed permanently with %s, caching as unsupported.", cmd, type(e).__name__)
                    UNSUPPORTED_COMMANDS.add(cmd)
            else:
                LOG.error("Mower command %s failed: %s (attempt %d/%d)", cmd, e, attempt, retries)

        if attempt < retries:
            await asyncio.sleep(2 * attempt)  # backoff

    if not optional:
        LOG.error("Mower command %s unresponsive after %d attempts, skipping.", cmd, retries)
    return None


async def get_static_info(mower: Mower) -> Dict[str, Any]:
    """Collect one-time, unchanging mower info: model, serial, name, schedule."""
    info: Dict[str, Any] = {"BridgeVersion": VERSION}
    try:
        manufacturer = await mower.get_manufacturer()
        model = await mower.get_model()
        if manufacturer:
            info["Manufacturer"] = manufacturer
        if model:
            info["Model"] = model

        serial = await safe_mower_command(mower, "GetSerialNumber", optional=True)
        if serial is not None:
            info["SerialNumber"] = str(serial)

        name = await safe_mower_command(mower, "GetUserMowerNameAsAsciiString", optional=True)
        if name:
            info["MowerName"] = name

        # Query and expose new static info commands if supported
        sw_boot = await safe_mower_command(mower, "GetSwVersionStringBoot", optional=True)
        if sw_boot is not None:
            info["SwVersionBoot"] = sw_boot
        sw_appl = await safe_mower_command(mower, "GetSwVersionStringAppl", optional=True)
        if sw_appl is not None:
            info["SwVersionAppl"] = sw_appl
        sw_sub = await safe_mower_command(mower, "GetSwVersionStringSub", optional=True)
        if sw_sub is not None:
            info["SwVersionSub"] = sw_sub
            
        sw_package = await safe_mower_command(mower, "GetSoftwarePackageVersion", optional=True)
        if sw_package is not None:
            info["SoftwarePackageVersion"] = sw_package

        # Extract Platform, Version, and Bundle details
        sw_platform = None
        sw_version = None
        sw_bundle = sw_package if sw_package else None

        for sw_str in [sw_appl, sw_boot]:
            if sw_str and "_" in sw_str:
                try:
                    parts = sw_str.split("_")
                    if len(parts) >= 3:
                        platform_part = parts[1].split("-")[-1]
                        if platform_part:
                            sw_platform = platform_part
                        version_part = parts[-1]
                        if version_part:
                            sw_version = version_part
                except Exception:
                    pass

        if sw_platform:
            info["SoftwarePlatform"] = sw_platform
        if sw_version:
            info["SoftwareVersion"] = sw_version
        if sw_bundle:
            info["SoftwareBundle"] = sw_bundle
        
        prod_time = await safe_mower_command(mower, "GetProductionTime", optional=True)
        if prod_time is not None:
            try:
                info["ProductionTime"] = dt.datetime.fromtimestamp(int(prod_time), tz=dt.timezone.utc).isoformat()
            except Exception as e:
                LOG.debug("Failed to parse production time %s: %s", prod_time, e)
        
        node_ipr_id = await safe_mower_command(mower, "GetNodeIprId", optional=True)
        if node_ipr_id is not None:
            info["NodeIprId"] = node_ipr_id
        husqvarna_id = await safe_mower_command(mower, "GetHusqvarnaId", optional=True)
        if husqvarna_id is not None:
            info["HusqvarnaId"] = husqvarna_id
        hw_serial = await safe_mower_command(mower, "GetHwSerialNumber", optional=True)
        if hw_serial is not None:
            info["HwSerialNumber"] = hw_serial
        hw_revision = await safe_mower_command(mower, "GetHardwareRevision", optional=True)
        if hw_revision is not None:
            info["HardwareRevision"] = hw_revision
        supported_accessories = await safe_mower_command(mower, "GetSupportedAccessories", optional=True)
        if supported_accessories is not None:
            info["SupportedAccessories"] = supported_accessories

        info.update(await get_schedule_info(mower))
    except Exception:
        LOG.exception("Unexpected error collecting static mower info")
    return info


async def get_schedule_info(mower: Mower) -> Dict[str, Any]:
    """Fetch the current weekly schedule tasks.

    Unlike the rest of get_static_info(), the schedule can change after
    startup — from a SET_SCHEDULE/CLEAR_SCHEDULE command, or from the
    official Gardena app. Callers re-run this periodically and immediately
    after a schedule write, merging the result into the cached static_info.

    On success, always returns all keys (Schedule=None / _tasks=[] /
    ScheduleTasks=[] when the mower has no tasks) so a merge correctly
    clears stale cached data. On failure, returns {} so the caller keeps
    the last known-good values instead of wiping them due to a transient
    BLE error.

    `ScheduleTasks` is published in the exact same shape SET_SCHEDULE
    accepts — {"days": [...], "start": "HH:MM", "duration_minutes": N} —
    so consumers (e.g. Homey) can round-trip a fetched schedule straight
    back into a write without reshaping it.
    """
    info: Dict[str, Any] = {"Schedule": None, "_tasks": [], "ScheduleTasks": []}
    try:
        num_tasks = await safe_mower_command(mower, "GetNumberOfTasks", optional=True)
        if num_tasks:
            day_keys = [
                "useOnMonday", "useOnTuesday", "useOnWednesday", "useOnThursday",
                "useOnFriday", "useOnSaturday", "useOnSunday",
            ]
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            schedule_parts = []
            raw_tasks = []
            schedule_tasks = []
            for task_id in range(num_tasks):
                task = await safe_mower_command(mower, "GetTask", optional=True, taskId=task_id)
                if not task:
                    continue
                raw_tasks.append(task)
                active_days = [d for d, k in zip(day_names, day_keys) if task.get(k)]
                start_h, start_rem = divmod(task["start"], 3600)
                start_m = start_rem // 60
                dur_h, dur_rem = divmod(task["duration"], 3600)
                dur_m = dur_rem // 60
                schedule_parts.append(
                    f"{','.join(active_days) or 'none'} {start_h:02d}:{start_m:02d} ({dur_h}h{dur_m:02d}m)"
                )
                schedule_tasks.append({
                    "days": [d.lower() for d in active_days],
                    "start": f"{start_h:02d}:{start_m:02d}",
                    "duration_minutes": (task["duration"] + 59) // 60,
                })
            if schedule_parts:
                info["Schedule"] = " | ".join(schedule_parts)
            info["_tasks"] = raw_tasks  # internal only, filtered before MQTT publish
            info["ScheduleTasks"] = schedule_tasks
    except Exception:
        LOG.exception("Unexpected error collecting schedule info")
        return {}
    return info


async def refresh_schedule_cache(mower: Mower) -> None:
    """Re-fetch the schedule and merge it into the cached static_info,
    so the next status publish reflects it. Called periodically (see
    Config.schedule_poll_interval) and immediately after SET_SCHEDULE /
    CLEAR_SCHEDULE so a write is visible on the very next poll."""
    global last_schedule_refresh
    schedule_data = await get_schedule_info(mower)
    if schedule_data:
        static_info.update(schedule_data)
    last_schedule_refresh = asyncio.get_event_loop().time()


async def collect_status(mower: Mower, static_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Collect mower status asynchronously with error handling."""
    status: Dict[str, Any] = {}
    try:
        data = await safe_mower_command(mower, "GetAllStatistics")
        if not data:
            return status

        battery = await safe_mower_command(mower, "GetBatteryLevel")
        charging = await safe_mower_command(mower, "IsCharging")
        state = await safe_mower_command(mower, "GetState")
        activity = await safe_mower_command(mower, "GetActivity")
        next_start = await safe_mower_command(mower, "GetNextStartTime")
        last_error = await safe_mower_command(mower, "GetMessage", optional=True, messageId=0)
        mower_local_time = await safe_mower_command(mower, "GetTime", optional=True)

        if None in (battery, charging, state, activity, next_start):
            LOG.error("One or more essential mower commands failed, skipping this poll cycle")
            return status

        # Compute timezone offset between mower's local clock and real UTC epoch
        now_utc = dt.datetime.now(tz=dt.timezone.utc)
        now_utc_ts = int(now_utc.timestamp())
        
        if mower_local_time is not None:
            # Mower time is seconds since 1970 naive local time.
            # Offset is mower local epoch minus container UTC epoch.
            offset_seconds = mower_local_time - now_utc_ts
            mower_local_dt = dt.datetime.fromtimestamp(mower_local_time, tz=dt.timezone.utc).replace(tzinfo=None)
        else:
            # Fallback if GetTime is unsupported: estimate offset from container local timezone settings
            local_now = dt.datetime.now()
            utc_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            offset_seconds = int((local_now - utc_now).total_seconds())
            mower_local_dt = local_now

        # Mower-local "now", used to age the override window below
        now_local_ts = now_utc_ts + offset_seconds

        def local_epoch_to_iso(local_ts: int) -> str:
            """Convert a mower-local epoch timestamp to a real UTC ISO string."""
            return dt.datetime.fromtimestamp(int(local_ts) - offset_seconds, tz=dt.timezone.utc).isoformat()

        # The mower reports 0 when no next start is scheduled (e.g. while an override runs).
        # Publish null rather than 1970-01-01 so consumers can tell "unknown" from a real time.
        next_start_iso = local_epoch_to_iso(next_start) if int(next_start) > 0 else None

        mower_state_name = MowerState(state).name
        is_error_state = mower_state_name in ("ERROR", "FATAL_ERROR", "WAIT_FOR_SAFETY_PIN")

        if last_error is not None:
            last_error_name = ErrorCodes(last_error["code"]).name
            last_error_time_utc = int(last_error["time"]) - offset_seconds
            last_error_time = dt.datetime.fromtimestamp(last_error_time_utc, tz=dt.timezone.utc).isoformat()
        else:
            last_error_name = "UNKNOWN"
            last_error_time = None

        # LastError represents active error status. If the mower is not in an error state, report UNKNOWN so Homey clears alarm_stuck.
        active_last_error = last_error_name if is_error_state else "UNKNOWN"

        # Ride out a spurious 0%. The mower reporting a flat battery itself is always
        # believed; so is a 0% that persists, and so is the very first reading.
        global _last_good_battery, _battery_zero_polls, _battery_hold_warned
        battery = int(battery)
        battery_raw = battery
        battery_held = False
        if (
            battery == 0
            and _last_good_battery
            and last_error_name not in ("LOW_BATTERY", "EMPTY_BATTERY")
        ):
            _battery_zero_polls += 1
            if _battery_zero_polls <= BATTERY_ZERO_HOLD_POLLS:
                # A mower that does this does it every other poll, so say it once
                # per run at WARNING and keep the rest for DEBUG.
                log_hold = LOG.debug if _battery_hold_warned else LOG.warning
                log_hold(
                    "Mower reported Battery=0%% while last plausible level was %d%% "
                    "(%d/%d), holding the previous value",
                    _last_good_battery, _battery_zero_polls, BATTERY_ZERO_HOLD_POLLS,
                )
                _battery_hold_warned = True
                battery = _last_good_battery
                battery_held = True
            else:
                LOG.warning("Battery=0%% persists over %d polls, accepting it as the real level",
                            _battery_zero_polls)
                _last_good_battery = 0
        else:
            _battery_zero_polls = 0
            _last_good_battery = battery

        activity_name = MowerActivity(activity).name

        # Read the override unconditionally. A forced mow issued while the mower is still
        # charging stays queued in the dock, so the override is the only place that shows
        # the pending job during the wait -- exactly when Activity is not MOWING.
        override = await safe_mower_command(mower, "GetOverride", optional=True)
        override_action_name = "NONE"
        override_start_iso = None
        override_duration = 0
        override_remaining = 0
        if override:
            try:
                override_action = OverrideAction(int(override.get("action") or 0))
            except ValueError:
                LOG.debug("Unknown override action: %s", override.get("action"))
                override_action = OverrideAction.NONE
            override_action_name = override_action.name
            override_start_ts = int(override.get("startTime") or 0)
            override_duration = int(override.get("duration") or 0)
            if override_start_ts > 0:
                override_start_iso = local_epoch_to_iso(override_start_ts)
                if override_duration > 0:
                    override_remaining = max(0, override_start_ts + override_duration - now_local_ts)

        # A forced mow that has not reached the lawn yet. The override window is already
        # ticking, so an expired override is no longer pending.
        mow_pending = (
            override_action_name == "FORCEDMOW"
            and override_remaining > 0
            and activity_name not in ("MOWING", "GOING_OUT")
        )

        # Estimated moment the mower actually leaves the dock. Only meaningful while it is
        # topping up the battery first; without a usable charging estimate this stays null
        # and consumers fall back to the MowPending flag alone.
        mow_starts_at_iso = None
        if charging:
            charge_remaining = await safe_mower_command(mower, "GetRemainingChargingTime", optional=True)
            if isinstance(charge_remaining, int) and not isinstance(charge_remaining, bool) and 0 < charge_remaining <= 86400:
                status["remainingChargingTime"] = charge_remaining
                if mow_pending:
                    mow_starts_at_iso = (now_utc + dt.timedelta(seconds=charge_remaining)).isoformat()

        status.update(
            Battery=str(battery),
            Charging="ON" if charging else "OFF",
            State=mower_state_name,
            Activity=activity_name,
            NextStartSchedule=next_start_iso,
            OverrideAction=override_action_name,
            OverrideStartSchedule=override_start_iso,
            OverrideDuration=override_duration,
            MowPending=mow_pending,
            MowStartsAt=mow_starts_at_iso,
            LastError=active_last_error,
            LastErrorHistory=last_error_name,
            LastErrorSchedule=last_error_time,
            CurrUpdateSchedule=now_utc.isoformat(),
            totalRunningTime=data.get("totalRunningTime", 0),
            totalCuttingTime=data.get("totalCuttingTime", 0),
            totalChargingTime=data.get("totalChargingTime", 0),
            totalSearchingTime=data.get("totalSearchingTime", 0),
            numberOfCollisions=data.get("numberOfCollisions", 0),
            numberOfChargingCycles=data.get("numberOfChargingCycles", 0),
            cuttingBladeUsageTime=data.get("cuttingBladeUsageTime", 0),
            customMowDuration=custom_mow_duration
        )

        if mower_local_time is not None:
            status["mowerLocalTime"] = mower_local_dt.isoformat()

        # Calculate remaining mow time
        remaining_mow_seconds = 0
        if activity_name == "MOWING":
            if override_start_iso is not None and override_duration > 0:
                # Manual override mow: computed above from override startTime + duration
                remaining_mow_seconds = override_remaining
                LOG.debug("Override mow remaining: %d seconds", remaining_mow_seconds)
            else:
                # Scheduled mow: find the active task for today
                tasks = (static_info or {}).get("_tasks", [])
                if tasks:
                    day_keys = [
                        "useOnMonday", "useOnTuesday", "useOnWednesday", "useOnThursday",
                        "useOnFriday", "useOnSaturday", "useOnSunday",
                    ]
                    day_of_week = mower_local_dt.weekday()  # 0=Mon, 6=Sun
                    secs_since_midnight = mower_local_dt.hour * 3600 + mower_local_dt.minute * 60 + mower_local_dt.second
                    for task in tasks:
                        if task.get(day_keys[day_of_week]):
                            task_start = int(task.get("start", 0))
                            task_duration = int(task.get("duration", 0))
                            if task_start <= secs_since_midnight < task_start + task_duration:
                                remaining_mow_seconds = max(0, task_start + task_duration - secs_since_midnight)
                                LOG.debug("Scheduled mow remaining: %d seconds", remaining_mow_seconds)
                                break
        status["RemainingMowTime"] = remaining_mow_seconds

        # Query and expose new dynamic status / sensor data if supported
        realtime_sensor = await safe_mower_command(mower, "GetComboardSensorData", optional=True)
        if realtime_sensor is not None:
            status.update(
                collision=bool(realtime_sensor.get("collision")),
                lift=bool(realtime_sensor.get("lift")),
                pitch=realtime_sensor.get("pitch"),
                roll=realtime_sensor.get("roll"),
                zAcceleration=realtime_sensor.get("zAcceleration"),
                upsideDown=bool(realtime_sensor.get("upsideDown")),
                mowerTemperature=realtime_sensor.get("mowerTemperature")
            )
        else:
            # Fallback to individual orientation pitch & roll queries if supported
            pitch = await safe_mower_command(mower, "GetOrientationPitch", optional=True)
            if pitch is not None:
                status["pitch"] = pitch
            roll = await safe_mower_command(mower, "GetOrientationRoll", optional=True)
            if roll is not None:
                status["roll"] = roll

        # Tilt and Collision sensor status mapping based on active error code
        # In the Gardena app: "Tilsensor" (tilt / lift) and "Botssensor" (collision)
        if is_error_state and last_error_name in ["MOWER_TILTED", "TILT_SENSOR_PROBLEM", "ALARM_MOWER_TILTED"]:
            status["tiltSensor"] = "Tilted"
        elif is_error_state and last_error_name in ["MOWER_LIFTED", "LIFTED", "ALARM_MOWER_LIFTED", "LIFTED_IN_LINK_ARM", "LIFT_SENSOR_DEFECT"]:
            status["tiltSensor"] = "Lifted"
        else:
            status["tiltSensor"] = "Oké"

        if is_error_state and last_error_name in ["COLLISION_SENSOR_ERROR", "COLLISION_SENSOR_PROBLEM_FRONT", "COLLISION_SENSOR_PROBLEM_REAR", "COLLISION_SENSOR_DEFECT"]:
            status["collisionSensor"] = "Error"
        else:
            status["collisionSensor"] = "Oké"

        # Frost sensor. 5370/1 only -- 4476/6 was read here as a "legacy frost"
        # fallback, but it is a lift sensor, so OR-ing it in reported frost as ON
        # whenever the mower was lifted.
        frost_sensor = await safe_mower_command(mower, "GetFrostSensorEnabled", optional=True)
        if frost_sensor is not None:
            status["frostSensorEnabled"] = "ON" if frost_sensor else "OFF"
            
        # SensorControl
        sensor_control = await safe_mower_command(mower, "GetSensorControlEnabled", optional=True)
        if sensor_control is not None:
            status["sensorControlEnabled"] = "ON" if sensor_control else "OFF"
            
        sensor_sensitivity = await safe_mower_command(mower, "GetSensorControlSensitivity", optional=True)
        if sensor_sensitivity is not None:
            sens_map = {1: "LOW", 2: "MEDIUM", 3: "HIGH"}
            status["sensorControlSensitivity"] = sens_map.get(sensor_sensitivity, str(sensor_sensitivity))
            
        # Loop signal quality / strength / A0 / F / Guide
        sig_quality = await safe_mower_command(mower, "GetSignalQuality", optional=True)
        if sig_quality is not None:
            status["loopSignalStrength"] = sig_quality.get("signalQuality")
            status["loopSignalA"] = sig_quality.get("a0Signal")
            status["loopSignalF"] = sig_quality.get("fSignal")
            status["loopSignalGuide"] = sig_quality.get("guide1Signal")
            if sig_quality.get("guide2Signal") is not None:
                status["loopSignalGuide2"] = sig_quality.get("guide2Signal")
            if sig_quality.get("guide3Signal") is not None:
                status["loopSignalGuide3"] = sig_quality.get("guide3Signal")
        else:
            # Fallback for models (like Sileno Minimo) that don't support the comboard-based GetSignalQuality command
            # Try signalType=1 first, then signalType=0
            loop_signals = await safe_mower_command(mower, "GetLoopSignals", optional=True, signalType=1)
            if loop_signals is None:
                loop_signals = await safe_mower_command(mower, "GetLoopSignals", optional=True, signalType=0)
                if loop_signals is None:
                    UNSUPPORTED_COMMANDS.add("GetLoopSignals")
                
            if loop_signals is not None:
                status["loopSignalA"] = loop_signals.get("a0Signal")
                status["loopSignalF"] = loop_signals.get("fSignal")
                status["loopSignalGuide"] = loop_signals.get("guide1Signal")
                if loop_signals.get("guide2Signal") is not None:
                    status["loopSignalGuide2"] = loop_signals.get("guide2Signal")
                if loop_signals.get("guide3Signal") is not None:
                    status["loopSignalGuide3"] = loop_signals.get("guide3Signal")
                    
            loop_strength = await safe_mower_command(mower, "GetLoopSignalStrength", optional=True, signalType=1)
            if loop_strength is None:
                loop_strength = await safe_mower_command(mower, "GetLoopSignalStrength", optional=True, signalType=0)
                if loop_strength is None:
                    UNSUPPORTED_COMMANDS.add("GetLoopSignalStrength")
                
            if loop_strength is not None:
                status["loopSignalStrength"] = loop_strength
                
        # Battery details (Voltage, Current, Temperature)
        batt_volt = await safe_mower_command(mower, "GetBatteryVoltage", optional=True)
        if batt_volt is not None:
            # The protocol types this register as uint16, but the 305 answers signed:
            # -5 mV arrives as 65531 and would be published as 65.53 V.
            if batt_volt > 0x7FFF:
                batt_volt -= 0x10000
            if batt_volt > 0:
                status["batteryVoltage"] = round(batt_volt / 1000.0, 2)
            else:
                LOG.debug("Ignoring implausible battery voltage: %d mV", batt_volt)
            
        batt_curr = await safe_mower_command(mower, "GetBatteryCurrent", optional=True)
        if batt_curr is not None:
            status["batteryCurrent"] = batt_curr
            
        batt_temp = await safe_mower_command(mower, "GetBatteryTemperature", optional=True)
        if batt_temp is not None:
            status["batteryTemperature"] = batt_temp
            
        garage_enabled = await safe_mower_command(mower, "GetGarageEnabled", optional=True)
        if garage_enabled is not None:
            status["garageEnabled"] = "ON" if garage_enabled else "OFF"
            
        radar = await safe_mower_command(mower, "GetAntiCollisionRadar", optional=True)
        if radar is not None:
            status["radarEnabled"] = "ON" if radar.get("enabled") else "OFF"
            status["radarAvailable"] = "ON" if radar.get("available") else "OFF"
            
        eco_mode = await safe_mower_command(mower, "GetEcoModeEnabled", optional=True)
        if eco_mode is not None:
            status["ecoMode"] = "ON" if eco_mode else "OFF"
            
        drive_past_wire = await safe_mower_command(mower, "GetDrivePastWire", optional=True)
        if drive_past_wire is not None:
            status["drivePastWire"] = drive_past_wire
            
        reversing_distance = await safe_mower_command(mower, "GetReversingDistance", optional=True)
        if reversing_distance is not None:
            status["reversingDistance"] = reversing_distance
            
        spot_cutting_state = await safe_mower_command(mower, "GetSpotCuttingState", optional=True)
        if spot_cutting_state is not None:
            status["spotCuttingState"] = spot_cutting_state

        if static_info:
            # Exclude internal keys (prefixed with _) from the published status
            status.update({k: v for k, v in static_info.items() if not k.startswith("_")})

        pending_note = ""
        if mow_pending:
            pending_note = ", MowPending (starts %s)" % (mow_starts_at_iso or "unknown")

        # Pack readings alongside the gauge percentage: when a mower reports an
        # implausible battery level, voltage/current/temperature are what tell a
        # real pack problem apart from a bad percentage reading.
        batt_parts = ["held, mower said %d%%" % battery_raw] if battery_held else []
        batt_parts += [
            "%s=%s" % (label, status[key])
            for key, label in (
                ("batteryVoltage", "Volt"),
                ("batteryCurrent", "Curr"),
                ("batteryTemperature", "Temp"),
            )
            if status.get(key) is not None
        ]
        batt_detail = ", ".join(batt_parts)
        batt_note = " (%s)" % batt_detail if batt_detail else ""

        LOG.info(
            "Status: Battery=%s%%%s, Charging=%s, State=%s, Activity=%s, RemainingMow=%ds%s",
            status["Battery"],
            batt_note,
            status["Charging"],
            status["State"],
            status["Activity"],
            status["RemainingMowTime"],
            pending_note,
        )
    except Exception:
        LOG.exception("Unexpected error collecting mower status")
    return status


DAY_ABBREVIATIONS = {
    "mon": "on_monday",
    "tue": "on_tuesday",
    "wed": "on_wednesday",
    "thu": "on_thursday",
    "fri": "on_friday",
    "sat": "on_saturday",
    "sun": "on_sunday",
}


def _parse_schedule_tasks(raw_json: str) -> list:
    """Parse a JSON array of {"days": [...], "start": "HH:MM", "duration_minutes": N}
    task definitions into a list of TaskInformation objects."""
    task_defs = json.loads(raw_json)
    if not isinstance(task_defs, list):
        raise ValueError("SET_SCHEDULE JSON must be a list of task objects")

    tasks = []
    for task_def in task_defs:
        hh, mm = task_def["start"].split(":")
        start_minutes = int(hh) * 60 + int(mm)
        duration_minutes = int(task_def["duration_minutes"])

        days = dict.fromkeys(DAY_ABBREVIATIONS.values(), False)
        for day in task_def.get("days", []):
            key = DAY_ABBREVIATIONS.get(str(day).strip().lower()[:3])
            if key is None:
                raise ValueError(f"Unknown day '{day}'")
            days[key] = True

        tasks.append(TaskInformation(
            start_minutes,
            duration_minutes,
            days["on_monday"],
            days["on_tuesday"],
            days["on_wednesday"],
            days["on_thursday"],
            days["on_friday"],
            days["on_saturday"],
            days["on_sunday"],
        ))
    return tasks


async def send_command(mower: Mower, cmd: str, args: Optional[list] = None, raw_args: str = "") -> None:
    """Send control commands to the mower. Raises on failure so the caller can react."""
    global custom_mow_duration, static_info, last_schedule_refresh
    cmd = cmd.upper()
    if cmd == "MOW":
        logged_in = await safe_mower_command(mower, "IsOperatorLoggedIn")
        LOG.info("Operator logged in? %s", logged_in)
        if not logged_in:
            LOG.info("Submitting operator PIN...")
            await safe_mower_command(mower, "EnterOperatorPin", code=CFG.mower_pin)
            logged_in = await safe_mower_command(mower, "IsOperatorLoggedIn")
            LOG.info("Operator logged in after PIN submit? %s", logged_in)

        LOG.info("Mower start sequence initiated")
        await mower.command("SetMode", mode=ModeOfOperation.AUTO)
        await mower.command("SetOverrideMow", duration=custom_mow_duration)
        await mower.command("StartTrigger")
        LOG.info("Mower started ✅")

    elif cmd == "PARK":
        await mower.command("SetOverrideParkUntilNextStart")
        LOG.info("Mower parked ⛔")
    elif cmd == "PARK_PERMANENTLY":
        await mower.command("SetMode", mode=ModeOfOperation.HOME)
        LOG.info("Mower parked permanently ⛔")
    elif cmd == "RESUME_SCHEDULE":
        await mower.command("ClearOverride")
        await mower.command("SetMode", mode=ModeOfOperation.AUTO)
        LOG.info("Mower resumed schedule 🗓")
    elif cmd == "SET_SCHEDULE":
        if not raw_args:
            LOG.warning(
                'SET_SCHEDULE requires a JSON array of tasks, e.g. '
                'SET_SCHEDULE [{"days":["mon","wed","fri"],"start":"15:00","duration_minutes":90}]'
            )
            return
        try:
            tasks = _parse_schedule_tasks(raw_args)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            LOG.error("Invalid SET_SCHEDULE payload: %s", e)
            return
        await mower.set_tasks(tasks)
        LOG.info("Set weekly schedule with %d task(s) ✅", len(tasks))
        await refresh_schedule_cache(mower)
    elif cmd == "CLEAR_SCHEDULE":
        await mower.clear_tasks()
        LOG.info("Cleared weekly schedule 🗑")
        await refresh_schedule_cache(mower)
    elif cmd == "PAUSE":
        await mower.command("Pause")
        LOG.info("Mower paused ⏸")
    elif cmd == "RESUME":
        await mower.command("StartTrigger")
        LOG.info("Mower resumed ▶")
    elif cmd == "SPOT_CUT":
        LOG.info("Mower spot cut sequence initiated")
        res = await mower.mower_spot_cut()
        LOG.info("Mower spot cut result: %s", res)
    elif cmd == "STOP_SPOT_CUT":
        LOG.info("Mower stop spot cut sequence initiated")
        res = await mower.mower_stop_spot_cut()
        LOG.info("Mower stop spot cut result: %s", res)
    elif cmd == "DRIVE_PAST_WIRE":
        if args:
            try:
                dist = int(args[0])
                await mower.command("SetDrivePastWire", distance=dist)
                LOG.info("Set drive past wire to %d ✅", dist)
            except ValueError:
                LOG.error("Invalid distance for DRIVE_PAST_WIRE: %s", args[0])
        else:
            LOG.warning("DRIVE_PAST_WIRE requires a distance argument")
    elif cmd == "REVERSING_DISTANCE":
        if args:
            try:
                dist = int(args[0])
                await mower.command("SetReversingDistance", distance=dist)
                LOG.info("Set reversing distance to %d ✅", dist)
            except ValueError:
                LOG.error("Invalid distance for REVERSING_DISTANCE: %s", args[0])
        else:
            LOG.warning("REVERSING_DISTANCE requires a distance argument")
    elif cmd == "GARAGE_ENABLED":
        if args:
            enabled = args[0].upper() in ("ON", "TRUE", "1")
            await mower.command("SetGarageEnabled", enabled=enabled)
            LOG.info("Set garage enabled to %s ✅", enabled)
        else:
            LOG.warning("GARAGE_ENABLED requires ON/OFF argument")
    elif cmd == "RADAR_ENABLED":
        if args:
            enabled = args[0].upper() in ("ON", "TRUE", "1")
            await mower.command("SetAntiCollisionRadarEnabled", enabled=enabled)
            LOG.info("Set anti-collision radar enabled to %s ✅", enabled)
        else:
            LOG.warning("RADAR_ENABLED requires ON/OFF argument")
    elif cmd == "ECO_MODE":
        if args:
            enabled = args[0].upper() in ("ON", "TRUE", "1")
            await mower.command("SetEcoModeEnabled", enabled=enabled)
            LOG.info("Set eco mode to %s ✅", args[0])
        else:
            LOG.warning("ECO_MODE requires ON/OFF argument")
    elif cmd == "FROST_SENSOR":
        if args:
            enabled = args[0].upper() in ("ON", "TRUE", "1")

            # 4476/5 was previously used as a "legacy frost" fallback here, but
            # 4476 is lift sensing: probing 4476/6 on a Minimo 250 tracks physical
            # lift, not the frost setting. Never write to it. Frost is 5370/2 only.
            res, _ = await mower.command_response("SetFrostSensorEnabled", enabled=enabled)
            if res == ResponseResult.OK:
                LOG.info("Set frost sensor enabled to %s ✅", enabled)
            else:
                raise RuntimeError(f"Failed to set frost sensor: {res}")
        else:
            LOG.warning("FROST_SENSOR requires ON/OFF argument")
    elif cmd == "SENSOR_CONTROL":
        if args:
            enabled = args[0].upper() in ("ON", "TRUE", "1")
            await mower.command("SetSensorControlEnabled", enabled=enabled)
            LOG.info("Set sensor control enabled to %s ✅", enabled)
        else:
            LOG.warning("SENSOR_CONTROL requires ON/OFF argument")
    elif cmd == "SENSOR_CONTROL_SENSITIVITY":
        if args:
            val = args[0].upper()
            sens_map = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
            if val in sens_map:
                sensitivity = sens_map[val]
            else:
                try:
                    sensitivity = int(val)
                    if sensitivity not in (1, 2, 3):
                        LOG.warning("SENSOR_CONTROL_SENSITIVITY value out of range (must be LOW/MEDIUM/HIGH or 1/2/3): %s", val)
                        return
                except ValueError:
                    LOG.error("Invalid sensitivity value for SENSOR_CONTROL_SENSITIVITY: %s", val)
                    return
            await mower.command("SetSensorControlSensitivity", sensitivity=sensitivity)
            LOG.info("Set sensor control sensitivity to %d ✅", sensitivity)
        else:
            LOG.warning("SENSOR_CONTROL_SENSITIVITY requires LOW/MEDIUM/HIGH or 1/2/3 argument")
    elif cmd == "MOW_DURATION":
        if args:
            try:
                new_duration = int(args[0])
                if new_duration < 0 or new_duration > 28800:
                    LOG.warning("MOW_DURATION out of range (0–28800 seconds): %d", new_duration)
                    return
                custom_mow_duration = new_duration
                LOG.info("Set custom mow duration to %d seconds ✅", custom_mow_duration)
            except ValueError:
                LOG.error("Invalid duration for MOW_DURATION: %s", args[0])
        else:
            LOG.warning("MOW_DURATION requires a duration in seconds (e.g. MOW_DURATION 3600)")
    elif cmd == "GENERATE_LOOP_SIGNAL":
        await mower.command("GenerateLoopSignal")
        LOG.info("Generated new loop signal ✅")
    elif cmd == "RESET_BLADE_USAGE":
        await mower.command("ResetCuttingBladeUsageTime")
        LOG.info("Reset cutting blade usage time ✅")
    elif cmd == "REFRESH_INFO":
        static_info = await get_static_info(mower)
        last_schedule_refresh = asyncio.get_event_loop().time()
        LOG.info("Refreshed cached mower info (model, serial, versions, schedule, ...) ✅")
    elif cmd == "STARTING_POINT_ENABLED":
        if len(args) >= 2:
            try:
                point_id = int(args[0])
                enabled = args[1].upper() in ("ON", "TRUE", "1")
                await mower.command("SetStartingPointEnabled", startingPointId=point_id, enabled=enabled)
                LOG.info("Set starting point %d enabled to %s ✅", point_id, enabled)
            except ValueError:
                LOG.error("Invalid starting point id for STARTING_POINT_ENABLED: %s", args[0])
        else:
            LOG.warning("STARTING_POINT_ENABLED requires <id> <ON/OFF>")
    elif cmd == "STARTING_POINT_WIRE":
        if len(args) >= 2:
            try:
                point_id, wire = int(args[0]), int(args[1])
                await mower.command("SetStartingPointWire", startingPointId=point_id, wire=wire)
                LOG.info("Set starting point %d wire to %d ✅", point_id, wire)
            except ValueError:
                LOG.error("Invalid value for STARTING_POINT_WIRE: %s", args)
        else:
            LOG.warning("STARTING_POINT_WIRE requires <id> <wire>")
    elif cmd == "STARTING_POINT_DISTANCE":
        if len(args) >= 2:
            try:
                point_id, distance = int(args[0]), int(args[1])
                await mower.command("SetStartingPointDistance", startingPointId=point_id, distance=distance)
                LOG.info("Set starting point %d distance to %d ✅", point_id, distance)
            except ValueError:
                LOG.error("Invalid value for STARTING_POINT_DISTANCE: %s", args)
        else:
            LOG.warning("STARTING_POINT_DISTANCE requires <id> <distance>")
    elif cmd == "STARTING_POINT_PROPORTION":
        if len(args) >= 2:
            try:
                point_id, proportion = int(args[0]), int(args[1])
                await mower.command("SetStartingPointProportion", startingPointId=point_id, proportion=proportion)
                LOG.info("Set starting point %d proportion to %d%% ✅", point_id, proportion)
            except ValueError:
                LOG.error("Invalid value for STARTING_POINT_PROPORTION: %s", args)
        else:
            LOG.warning("STARTING_POINT_PROPORTION requires <id> <percent>")
    elif cmd == "STARTING_POINT_CORRIDOR_CUT":
        if len(args) >= 2:
            try:
                point_id = int(args[0])
                corridor_cut = args[1].upper() in ("ON", "TRUE", "1")
                await mower.command("SetStartingPointCorridorCut", startingPointId=point_id, corridorCut=corridor_cut)
                LOG.info("Set starting point %d corridor cut to %s ✅", point_id, corridor_cut)
            except ValueError:
                LOG.error("Invalid starting point id for STARTING_POINT_CORRIDOR_CUT: %s", args[0])
        else:
            LOG.warning("STARTING_POINT_CORRIDOR_CUT requires <id> <ON/OFF>")
    elif cmd == "SET_TIME":
        if args:
            try:
                target_time = int(args[0])
            except ValueError:
                LOG.error("Invalid timestamp for SET_TIME: %s", args[0])
                return
        else:
            # Sync to local naive time of the container
            local_now = dt.datetime.now()
            target_time = int(local_now.replace(tzinfo=dt.timezone.utc).timestamp())
            
        await mower.command("SetTime", time=target_time)
        LOG.info("Set mower time to %d ✅", target_time)
    else:
        LOG.warning("Unknown command received: %s", cmd)


# Home Assistant discovery logic removed for exclusive Homey use.


def save_mac_to_env(mac_address: str):
    """Attempt to persistently write the discovered MAC address to mower.env if it exists."""
    for path in ("mower.env", "/app/mower.env"):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                
                updated = False
                new_lines = []
                for line in lines:
                    if line.strip().startswith("MOWER_ADDRESS=") or line.strip().startswith("# MOWER_ADDRESS="):
                        new_lines.append(f"MOWER_ADDRESS={mac_address}\n")
                        updated = True
                    else:
                        new_lines.append(line)
                
                if not updated:
                    new_lines.append(f"\nMOWER_ADDRESS={mac_address}\n")
                
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                LOG.info("Persistently saved autodiscovered MAC address to %s", path)
                return
            except Exception as e:
                LOG.warning("Could not write MAC address to %s: %s", path, e)


async def discover_mower_mac() -> Optional[str]:
    """Scan for a nearby mower and return its MAC address if found."""
    LOG.info("Starting BLE scan to discover mower...")
    mower_uuid = "98bd0001-0b0e-421a-84e5-ddbf75dc6de4"
    discovered = {}

    def _detect(device, adv_data):
        is_mower = (
            mower_uuid in adv_data.service_uuids
            or (device.name and any(p in device.name.upper() for p in ["SILENO", "AUTOMOWER", "GARDENA"]))
        )
        if is_mower and "address" not in discovered:
            discovered["address"] = device.address
            discovered["name"] = device.name or "Unknown Mower"

    scanner = BleakScanner(_detect)
    await scanner.start()
    for _ in range(150):  # scan up to ~15s
        if "address" in discovered:
            break
        await asyncio.sleep(0.1)
    await scanner.stop()
    await asyncio.sleep(1.0)  # Let BLE adapter settle after scanning

    if "address" in discovered:
        LOG.info("Autodiscovered mower: %s at %s ✅", discovered["name"], discovered["address"])
        return discovered["address"]
    return None


# ----------------------------
# Main Loop
# ----------------------------
async def main() -> None:
    global custom_mow_duration, bridge_paused, static_info, last_schedule_refresh

    LOG.info("GardenaMower-BLE-MQTT v%s starting...", VERSION)
    _warn_on_large_swap()

    # Resolve MAC address if not configured or using placeholder
    is_placeholder = (
        not CFG.mower_address
        or CFG.mower_address.strip() == ""
        or CFG.mower_address.upper() in ("00:00:00:00:00:00", "AA:BB:CC:DD:EE:FF")
    )
    if is_placeholder:
        LOG.info("MOWER_ADDRESS not set or using placeholder. Initiating autodiscovery...")
        while not shutdown_event.is_set():
            discovered_mac = await discover_mower_mac()
            if discovered_mac:
                CFG.mower_address = discovered_mac
                save_mac_to_env(discovered_mac)
                break
            LOG.warning("Mower discovery failed, retrying in 10 seconds...")
            for _ in range(10):
                if shutdown_event.is_set():
                    break
                await asyncio.sleep(1)
                watchdog_reset()

    if shutdown_event.is_set():
        return

    mower_lock = asyncio.Lock()

    async def with_mower_connection(action: Callable[[Mower, Optional[int]], Awaitable[Any]]) -> Any:
        """Connect, run `action(mower, rssi)`, then always disconnect —
        so the mower's single BLE slot is only occupied for the brief
        duration of the actual operation, freeing it up the rest of the
        time for the official Gardena app."""
        async with mower_lock:
            mower, rssi = await connect_mower()
            if not mower:
                raise RuntimeError("Could not connect to mower")
            try:
                return await action(mower, rssi)
            finally:
                with contextlib.suppress(Exception):
                    await mower.disconnect()

    async def run_poll_cycle() -> Dict[str, Any]:
        async def _do(mower: Mower, rssi: Optional[int]) -> Dict[str, Any]:
            global static_info, last_schedule_refresh
            if not static_info:
                static_info = await get_static_info(mower)
                last_schedule_refresh = asyncio.get_event_loop().time()
            elif asyncio.get_event_loop().time() - last_schedule_refresh >= CFG.schedule_poll_interval:
                # Schedule can change from the official app too, so re-check it
                # periodically even without a Homey-initiated write.
                await refresh_schedule_cache(mower)
            info = dict(static_info)
            if rssi is not None:
                info["RSSI"] = rssi
            return await collect_status(mower, info)

        try:
            return await with_mower_connection(_do)
        except Exception as e:
            err_str = str(e)
            if isinstance(e, (EOFError, OSError)) or "EOFError" in err_str or "Bad file descriptor" in err_str:
                await escalate_and_exit(
                    f"Critical D-Bus socket error during poll cycle ({e}). Exiting process to trigger systemd restart...",
                    hard_fault=_is_dbus_socket_failure(e),
                )
            LOG.warning("Poll cycle skipped — mower unreachable (out of range, or app connected)")
            return {}

    async def dispatch_command(payload: str) -> None:
        global bridge_paused
        
        stripped = payload.strip()
        if not stripped:
            return
        cmd, _, raw_args = stripped.partition(" ")
        cmd = cmd.upper()
        raw_args = raw_args.strip()
        args = raw_args.split() if raw_args else []

        if cmd == "BRIDGE_PAUSE":
            bridge_paused = True
            LOG.info("Bridge PAUSED — staying off BLE so the official app has exclusive access")
            return
        if cmd == "BRIDGE_RESUME":
            bridge_paused = False
            LOG.info("Bridge RESUMED — normal polling will continue")
            return

        if bridge_paused:
            LOG.warning("Command '%s' ignored — bridge is paused. Send BRIDGE_RESUME first.", payload)
            return

        async def _do(mower: Mower, _rssi: Optional[int]) -> None:
            await send_command(mower, cmd, args, raw_args)

        for attempt in (1, 2):
            try:
                await with_mower_connection(_do)
                return
            except Exception as e:
                LOG.warning("Command '%s' attempt %d failed: %s", payload, attempt, e)
                if attempt == 1:
                    await asyncio.sleep(5)
        LOG.error("Command '%s' failed after retry", payload)

    # Derive a stable, unique device ID from the configured MAC address.
    # Using the MAC means the bridge starts immediately without a BLE pre-connection
    # just to fetch the serial number, and the topic is always predictable.
    mower_id = CFG.mower_address.replace(":", "_").upper()
    mower_base_topic = f"{CFG.mqtt_base_topic}/{mower_id}"
    availability_topic = f"{mower_base_topic}/availability"
    mower_topic = f"{mower_base_topic}/mower"
    bridge_topic = f"{mower_base_topic}/bridge"
    LOG.info("MQTT device topic: %s", mower_base_topic)

    # Start the watchdog heartbeat task
    asyncio.create_task(heartbeat_task(availability_topic))

    while not shutdown_event.is_set():
        try:
            # LWT: broker auto-publishes 'offline' on ungraceful disconnect/crash
            lwt = aiomqtt.Will(
                topic=availability_topic,
                payload="offline",
                qos=1,
                retain=True,
            )
            async with aiomqtt.Client(
                hostname=CFG.mqtt_broker,
                port=CFG.mqtt_port,
                username=CFG.mqtt_username,
                password=CFG.mqtt_password,
                will=lwt,
            ) as client:

                await client.publish(availability_topic, "online", retain=True)
                # Retained and published before any BLE work, so the bridge
                # version is readable even if the mower is never reachable.
                await client.publish(
                    bridge_topic, json.dumps({"Version": VERSION}), retain=True
                )
                LOG.info("MQTT connected ✅")

                await client.subscribe(f"{mower_base_topic}/command")
                LOG.info("Subscribed to %s/command", mower_base_topic)

                consecutive_poll_failures = 0
                MOWER_OFFLINE_AFTER = 2  # mark mower offline after this many consecutive failed polls

                async def status_loop():
                    nonlocal consecutive_poll_failures
                    while not shutdown_event.is_set():
                        if bridge_paused:
                            await asyncio.sleep(5)
                            watchdog_reset()
                            continue

                        status = await run_poll_cycle()
                        if status:
                            consecutive_poll_failures = 0
                            mark_poll_success()
                            try:
                                # retain=True: broker caches last value so Homey gets
                                # it immediately on subscribe without waiting for next poll
                                await client.publish(
                                    f"{mower_base_topic}/status", json.dumps(status), retain=True
                                )
                                await client.publish(mower_topic, "online", retain=True)
                            except Exception:
                                LOG.exception("MQTT publish error")
                                break
                        else:
                            consecutive_poll_failures += 1
                            if consecutive_poll_failures >= MOWER_OFFLINE_AFTER:
                                try:
                                    await client.publish(mower_topic, "offline", retain=True)
                                    LOG.warning(
                                        "Mower unreachable for %d consecutive polls, marking offline",
                                        consecutive_poll_failures,
                                    )
                                except Exception:
                                    pass

                            # If mower remains unreachable for 10 consecutive polls (~10 minutes),
                            # exit process so systemd (Restart=always) restarts Python with a fresh D-Bus socket.
                            # Not a hard fault: a mower can be legitimately away for weeks, so this
                            # escalates at most to a bluetooth.service restart, never to a reboot.
                            if consecutive_poll_failures >= 10:
                                await escalate_and_exit(
                                    f"Mower unreachable for {consecutive_poll_failures} consecutive polls. "
                                    "Exiting process to trigger systemd restart..."
                                )

                        await asyncio.sleep(CFG.poll_interval)
                        watchdog_reset()

                loop_task = asyncio.create_task(status_loop())

                async for msg in client.messages:
                    if shutdown_event.is_set():
                        break
                    topic = msg.topic.value
                    payload = msg.payload.decode().strip()
                    if topic.endswith("/command"):
                        LOG.info("MQTT command received: %s", payload)
                        await dispatch_command(payload)
                    watchdog_reset()

                await loop_task

        except Exception as e:
            LOG.error("MQTT loop error: %s", e)
            await asyncio.sleep(5)

    LOG.info("Shutting down...")
    with contextlib.suppress(Exception):
        async with aiomqtt.Client(
            hostname=CFG.mqtt_broker,
            port=CFG.mqtt_port,
            username=CFG.mqtt_username,
            password=CFG.mqtt_password,
        ) as client:
            await client.publish(availability_topic, "offline", retain=True)
            await client.publish(mower_topic, "offline", retain=True)


# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        LOG.info("Interrupted, shutting down...")
