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

bashio::log.info "Starting ToneGet companion API on port 8787..."
python3 /app/tonal_service.py &
COMPANION_PID=$!

bashio::log.info "Starting ToneGet MQTT sync service..."
python3 /app/toneget_service.py &
MQTT_PID=$!

wait -n "${COMPANION_PID}" "${MQTT_PID}"
EXIT_CODE=$?

bashio::log.error "One of the ToneGet services exited with code ${EXIT_CODE}. Stopping app."

kill "${COMPANION_PID}" "${MQTT_PID}" 2>/dev/null || true

exit "${EXIT_CODE}"