#!/usr/bin/with-contenv bashio

echo "[ToneGet] Starting Home Assistant app..."

exec python3 /app/toneget_service.py
