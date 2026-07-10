#!/bin/bash
set -e

echo "[entrypoint] Starting BLE pairing agent (bt-agent)..."
bt-agent --capability=NoInputNoOutput &

# Give bt-agent a moment to register itself on D-Bus before we start
# using Bluetooth — mirrors the delay used successfully during manual
# testing on native Raspberry Pi OS.
sleep 2

echo "[entrypoint] Starting mower_mqtt.py..."
exec python3 mower_mqtt.py
