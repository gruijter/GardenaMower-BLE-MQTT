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
VERSION = "0.9.0"

import asyncio
import json
import logging
import os
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
from automower_ble.protocol import MowerState, MowerActivity, ModeOfOperation, ResponseResult
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
        # Limit log file to 1MB and keep at most 3 backups to prevent infinite disk usage
        _handlers.append(RotatingFileHandler(_log_file, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"))
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
    mower_address: str = os.getenv("MOWER_ADDRESS", "00:00:00:00:00:00")
    mower_pin: int = int(os.getenv("MOWER_PIN", "1234"))

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
# Toggle with payload "PAUSE" / "RESUME" on the normal command topic.
bridge_paused = False


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
    except Exception:
        LOG.exception("Failed to connect to mower")
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
    "GetFrostSensorEnabledLegacy",
    "GetEcoModeEnabled",
    "GetTime"
}


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
    info: Dict[str, Any] = {}
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

        num_tasks = await safe_mower_command(mower, "GetNumberOfTasks", optional=True)
        if num_tasks:
            day_keys = [
                "useOnMonday", "useOnTuesday", "useOnWednesday", "useOnThursday",
                "useOnFriday", "useOnSaturday", "useOnSunday",
            ]
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            schedule_parts = []
            raw_tasks = []
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
            if schedule_parts:
                info["Schedule"] = " | ".join(schedule_parts)
            if raw_tasks:
                info["_tasks"] = raw_tasks  # internal only, filtered before MQTT publish
    except Exception:
        LOG.exception("Unexpected error collecting static mower info")
    return info


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

        # Convert local epoch timestamps from mower back to real UTC datetime for external consumers
        next_start_utc = int(next_start) - offset_seconds
        next_start_iso = dt.datetime.fromtimestamp(next_start_utc, tz=dt.timezone.utc).isoformat()

        if last_error is not None:
            last_error_name = ErrorCodes(last_error["code"]).name
            last_error_time_utc = int(last_error["time"]) - offset_seconds
            last_error_time = dt.datetime.fromtimestamp(last_error_time_utc, tz=dt.timezone.utc).isoformat()
        else:
            last_error_name = "UNKNOWN"
            last_error_time = None

        activity_name = MowerActivity(activity).name

        status.update(
            Battery=str(battery),
            Charging="ON" if charging else "OFF",
            State=MowerState(state).name,
            Activity=activity_name,
            NextStartSchedule=next_start_iso,
            LastError=last_error_name,
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
            override = await safe_mower_command(mower, "GetOverride", optional=True)
            if override and int(override.get("startTime") or 0) > 0 and int(override.get("duration") or 0) > 0:
                # Manual override mow: compute from override startTime + duration
                start_ts = int(override["startTime"])
                duration_s = int(override["duration"])
                if mower_local_time is not None:
                    remaining_mow_seconds = max(0, (start_ts + duration_s) - mower_local_time)
                else:
                    now_ts_local = int(dt.datetime.now().timestamp())
                    remaining_mow_seconds = max(0, (start_ts + duration_s) - now_ts_local)
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

        # Tilt and Collision sensor status mapping based on the active error code
        # In the Gardena app: "Tilsensor" (tilt / lift) and "Botssensor" (collision)
        if last_error_name in ["MOWER_TILTED", "TILT_SENSOR_PROBLEM", "ALARM_MOWER_TILTED"]:
            status["tiltSensor"] = "Tilted"
        elif last_error_name in ["MOWER_LIFTED", "LIFTED", "ALARM_MOWER_LIFTED", "LIFTED_IN_LINK_ARM", "LIFT_SENSOR_DEFECT"]:
            status["tiltSensor"] = "Lifted"
        else:
            status["tiltSensor"] = "Oké"

        if last_error_name in ["COLLISION_SENSOR_ERROR", "COLLISION_SENSOR_PROBLEM_FRONT", "COLLISION_SENSOR_PROBLEM_REAR", "COLLISION_SENSOR_DEFECT"]:
            status["collisionSensor"] = "Error"
        else:
            status["collisionSensor"] = "Oké"

        # Frost sensor (try regular and legacy)
        frost_sensor = await safe_mower_command(mower, "GetFrostSensorEnabled", optional=True)
        legacy_frost = await safe_mower_command(mower, "GetFrostSensorEnabledLegacy", optional=True)
        if frost_sensor is not None or legacy_frost is not None:
            is_enabled = bool(frost_sensor) or bool(legacy_frost)
            status["frostSensorEnabled"] = "ON" if is_enabled else "OFF"
            
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
            status["batteryVoltage"] = round(batt_volt / 1000.0, 2)
            
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

        LOG.info(
            "Status: Battery=%s%%, Charging=%s, State=%s, Activity=%s, RemainingMow=%ds",
            status["Battery"],
            status["Charging"],
            status["State"],
            status["Activity"],
            status["RemainingMowTime"],
        )
    except Exception:
        LOG.exception("Unexpected error collecting mower status")
    return status


async def send_command(mower: Mower, cmd: str, args: Optional[list] = None) -> None:
    """Send control commands to the mower. Raises on failure so the caller can react."""
    global custom_mow_duration
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
            
            # If standard GetFrostSensorEnabled is already cached as unsupported, try legacy first
            if "GetFrostSensorEnabled" in UNSUPPORTED_COMMANDS:
                LOG.info("GetFrostSensorEnabled is cached as unsupported. Trying legacy SetFrostSensorEnabledLegacy...")
                res, _ = await mower.command_response("SetFrostSensorEnabledLegacy", enabled=enabled)
                if res == ResponseResult.OK:
                    LOG.info("Set legacy frost sensor enabled to %s ✅", enabled)
                    return
                LOG.warning("SetFrostSensorEnabledLegacy failed with %s, falling back to standard...", res)

            # Try standard command
            res, _ = await mower.command_response("SetFrostSensorEnabled", enabled=enabled)
            if res == ResponseResult.OK:
                LOG.info("Set frost sensor enabled to %s ✅", enabled)
            else:
                # If standard failed and we haven't tried legacy yet, try legacy
                if "GetFrostSensorEnabled" not in UNSUPPORTED_COMMANDS:
                    LOG.info("SetFrostSensorEnabled failed with %s, trying legacy SetFrostSensorEnabledLegacy...", res)
                    res_legacy, _ = await mower.command_response("SetFrostSensorEnabledLegacy", enabled=enabled)
                    if res_legacy == ResponseResult.OK:
                        LOG.info("Set legacy frost sensor enabled to %s ✅", enabled)
                        return
                    raise RuntimeError(f"Failed to set frost sensor: standard returned {res}, legacy returned {res_legacy}")
                else:
                    raise RuntimeError(f"Failed to set frost sensor: standard returned {res}")
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
    global custom_mow_duration, bridge_paused

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

    static_info: Dict[str, Any] = {}
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
        nonlocal static_info

        async def _do(mower: Mower, rssi: Optional[int]) -> Dict[str, Any]:
            nonlocal static_info
            if not static_info:
                static_info = await get_static_info(mower)
            info = dict(static_info)
            if rssi is not None:
                info["RSSI"] = rssi
            return await collect_status(mower, info)

        try:
            return await with_mower_connection(_do)
        except Exception:
            LOG.warning("Poll cycle skipped — mower unreachable (out of range, or app connected)")
            return {}

    async def dispatch_command(payload: str) -> None:
        global bridge_paused
        
        parts = payload.strip().split()
        if not parts:
            return
        cmd = parts[0].upper()
        args = parts[1:]

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
            await send_command(mower, cmd, args)

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
    except KeyboardInterrupt:
        LOG.info("Interrupted, shutting down...")
