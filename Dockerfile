# GardenaMower-BLE-MQTT
#
# Multi-arch image: built for linux/amd64 (e.g. N150 mini PCs) and
# linux/arm64 (Raspberry Pi 4/5 running 64-bit Raspberry Pi OS).
#
# Bluetooth is NOT emulated inside the container — this image talks to the
# HOST's bluetoothd over D-Bus. The host's Bluetooth adapter, kernel, and
# BlueZ stack do all the real work; the container only needs a mounted
# D-Bus socket and host networking. See docker-compose.yml for the required
# runtime configuration (network_mode: host, /run/dbus mount, privileged).

FROM python:3.12-slim-bookworm

# bluez / bluez-tools: gives us `bt-agent`, which registers a
#   NoInputNoOutput pairing agent on the (shared) D-Bus bus so headless BLE
#   pairing succeeds — see entrypoint.sh
# libglib2.0-0 / libdbus-1-3: runtime libraries needed by bleak's BlueZ
#   D-Bus backend
# git: needed at build time to install automower-ble from its git branch
# tini: a minimal init process so signals (e.g. container stop) are
#   forwarded correctly to both bt-agent and the Python process below
RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        bluez-tools \
        libglib2.0-0 \
        libdbus-1-3 \
        git \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first so this layer is cached between builds
# that only change mower_mqtt.py
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mower_mqtt.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
