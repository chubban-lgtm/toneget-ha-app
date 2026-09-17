#!/usr/bin/with-contenv bashio

echo "[ToneGet] Starting Home Assistant app..."

export MQTT_HOST="$(bashio::services mqtt 'host')"
export MQTT_PORT="$(bashio::services mqtt 'port')"
export MQTT_USERNAME="$(bashio::services mqtt 'username')"
export MQTT_PASSWORD="$(bashio::services mqtt 'password')"

if [ -z "${MQTT_HOST}" ]; then
    bashio::log.error "MQTT service is unavailable."
    exit 1
fi

bashio::log.info "MQTT service found at ${MQTT_HOST}:${MQTT_PORT}"

exec python3 /app/toneget_service.py