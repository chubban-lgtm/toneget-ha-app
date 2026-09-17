#!/usr/bin/env python3

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import sync_workouts as toneget


OPTIONS_FILE = "/data/options.json"
OUTPUT_FILE = "/data/tonal_latest.json"

MQTT_DISCOVERY_PREFIX = "homeassistant"
MQTT_BASE_TOPIC = "toneget"
MQTT_STATE_TOPIC = f"{MQTT_BASE_TOPIC}/state"
MQTT_AVAILABILITY_TOPIC = f"{MQTT_BASE_TOPIC}/availability"

DEVICE_ID = "toneget_tonal"
DEVICE_NAME = "ToneGet"
DEVICE_MANUFACTURER = "ToneGet"
DEVICE_MODEL = "Tonal Workout Sync"


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def load_options():
    try:
        with open(OPTIONS_FILE, "r", encoding="utf-8") as f:
            options = json.load(f)
    except Exception as exc:
        log(f"ERROR: Could not read {OPTIONS_FILE}: {exc}")
        sys.exit(1)

    email = str(options.get("tonal_email", "")).strip()
    password = str(options.get("tonal_password", ""))
    sync_interval = int(options.get("sync_interval", 900))

    if not email:
        log("ERROR: Tonal email is not configured.")
        sys.exit(1)

    if not password:
        log("ERROR: Tonal password is not configured.")
        sys.exit(1)

    if sync_interval < 300:
        log("WARNING: sync_interval below 300 seconds; using 300.")
        sync_interval = 300

    return email, password, sync_interval


def get_mqtt_service():
    host = os.environ.get("MQTT_HOST")
    port = os.environ.get("MQTT_PORT")
    username = os.environ.get("MQTT_USERNAME")
    password = os.environ.get("MQTT_PASSWORD")

    if not host:
        raise RuntimeError(
            "MQTT service information was not provided by Home Assistant."
        )

    try:
        port = int(port or 1883)
    except ValueError:
        port = 1883

    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }


def connect_mqtt():
    service = get_mqtt_service()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="toneget-ha-app",
    )

    if service["username"]:
        client.username_pw_set(
            service["username"],
            service["password"],
        )

    client.will_set(
        MQTT_AVAILABILITY_TOPIC,
        payload="offline",
        qos=1,
        retain=True,
    )

    log(
        f"Connecting to MQTT broker at "
        f"{service['host']}:{service['port']}..."
    )

    client.connect(
        service["host"],
        service["port"],
        keepalive=60,
    )

    client.loop_start()

    client.publish(
        MQTT_AVAILABILITY_TOPIC,
        payload="online",
        qos=1,
        retain=True,
    )

    log("MQTT connected.")

    return client


def device_info():
    return {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": DEVICE_MANUFACTURER,
        "model": DEVICE_MODEL,
        "sw_version": "0.2.1",
    }


def publish_discovery_sensor(
    client,
    object_id,
    name,
    value_template,
    icon=None,
    unit=None,
    device_class=None,
    state_class=None,
):
    topic = (
        f"{MQTT_DISCOVERY_PREFIX}/sensor/"
        f"{DEVICE_ID}/{object_id}/config"
    )

    payload = {
        "name": name,
        "unique_id": f"{DEVICE_ID}_{object_id}",
        "state_topic": MQTT_STATE_TOPIC,
        "availability_topic": MQTT_AVAILABILITY_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "value_template": value_template,
        "device": device_info(),
    }

    if icon:
        payload["icon"] = icon

    if unit:
        payload["unit_of_measurement"] = unit

    if device_class:
        payload["device_class"] = device_class

    if state_class:
        payload["state_class"] = state_class

    client.publish(
        topic,
        json.dumps(payload),
        qos=1,
        retain=True,
    )


def publish_discovery(client):
    sensors = [
        {
            "object_id": "strength_score",
            "name": "Strength Score",
            "value_template": "{{ value_json.strength_score }}",
            "icon": "mdi:arm-flex",
            "state_class": "measurement",
        },
        {
            "object_id": "upper_strength_score",
            "name": "Upper Strength Score",
            "value_template": "{{ value_json.upper_strength_score }}",
            "icon": "mdi:arm-flex-outline",
            "state_class": "measurement",
        },
        {
            "object_id": "lower_strength_score",
            "name": "Lower Strength Score",
            "value_template": "{{ value_json.lower_strength_score }}",
            "icon": "mdi:run",
            "state_class": "measurement",
        },
        {
            "object_id": "core_strength_score",
            "name": "Core Strength Score",
            "value_template": "{{ value_json.core_strength_score }}",
            "icon": "mdi:human-handsup",
            "state_class": "measurement",
        },
        {
            "object_id": "total_workouts",
            "name": "Total Workouts",
            "value_template": "{{ value_json.total_workouts }}",
            "icon": "mdi:dumbbell",
            "state_class": "total",
        },
        {
            "object_id": "latest_workout",
            "name": "Latest Workout",
            "value_template": "{{ value_json.latest_workout }}",
            "icon": "mdi:weight-lifter",
        },
        {
            "object_id": "latest_workout_date",
            "name": "Latest Workout Date",
            "value_template": "{{ value_json.latest_workout_date }}",
            "icon": "mdi:calendar-clock",
        },
        {
            "object_id": "last_sync",
            "name": "Last Sync",
            "value_template": "{{ value_json.last_sync }}",
            "device_class": "timestamp",
            "icon": "mdi:sync",
        },
    ]

    for sensor in sensors:
        publish_discovery_sensor(client, **sensor)

    log(f"Published MQTT Discovery for {len(sensors)} sensors.")


def extract_strength_scores(current_strength):
    overall = None
    upper = None
    lower = None
    core = None

    if isinstance(current_strength, dict):
        overall = (
            current_strength.get("overall")
            or current_strength.get("strengthScore")
            or current_strength.get("score")
        )

        upper = (
            current_strength.get("upper")
            or current_strength.get("upperBody")
            or current_strength.get("upperBodyScore")
        )

        lower = (
            current_strength.get("lower")
            or current_strength.get("lowerBody")
            or current_strength.get("lowerBodyScore")
        )

        core = (
            current_strength.get("core")
            or current_strength.get("coreScore")
        )

        granular = current_strength.get("granular")

        if isinstance(granular, dict):
            if overall is None:
                overall = (
                    granular.get("overall")
                    or granular.get("strengthScore")
                )

            if upper is None:
                upper = (
                    granular.get("upper")
                    or granular.get("upperBody")
                )

            if lower is None:
                lower = (
                    granular.get("lower")
                    or granular.get("lowerBody")
                )

            if core is None:
                core = granular.get("core")

    return overall, upper, lower, core


def publish_state(
    client,
    workouts,
    current_strength,
):
    overall, upper, lower, core = extract_strength_scores(
        current_strength
    )

    latest_name = None
    latest_date = None

    if workouts:
        latest = workouts[0]

        latest_name = (
            latest.get("workoutTitle")
            or latest.get("title")
            or latest.get("workoutType")
            or "Unknown workout"
        )

        latest_date = latest.get("beginTime")

    payload = {
        "strength_score": overall,
        "upper_strength_score": upper,
        "lower_strength_score": lower,
        "core_strength_score": core,
        "total_workouts": len(workouts),
        "latest_workout": latest_name,
        "latest_workout_date": latest_date,
        "last_sync": datetime.now(timezone.utc).isoformat(),
    }

    client.publish(
        MQTT_STATE_TOPIC,
        json.dumps(payload),
        qos=1,
        retain=True,
    )

    log("Published ToneGet state to MQTT.")


def sync_tonal(email, password, mqtt_client):
    log("Starting Tonal synchronization...")

    tokens = toneget.authenticate(email, password)
    id_token = tokens["id_token"]

    user_info = toneget.get_user_info(id_token)
    user_id = user_info.get("id")

    if not user_id:
        raise RuntimeError("Tonal API did not return a user ID.")

    first_name = user_info.get("firstName", "")
    last_name = user_info.get("lastName", "")
    display_name = f"{first_name} {last_name}".strip()

    if display_name:
        log(f"Authenticated as {display_name}.")
    else:
        log("Authenticated successfully.")

    profile = toneget.get_user_profile(
        id_token,
        user_id,
    )

    workouts = toneget.download_workouts(
        id_token,
        user_id,
    )

    workout_catalog = toneget.fetch_workout_catalog(
        id_token,
        workouts,
    )

    toneget.apply_workout_titles(
        workouts,
        workout_catalog,
    )

    activity_names = toneget.build_activity_names(workouts)

    custom_workouts = toneget.build_custom_workouts(
        workouts,
        workout_catalog,
    )

    strength_history = toneget.get_strength_score_history(
        id_token,
        user_id,
    )

    current_strength = toneget.get_current_strength_scores(
        id_token,
        user_id,
    )

    workouts.sort(
        key=lambda x: x.get("beginTime", ""),
        reverse=True,
    )

    export_data = {
        "version": "3.0-ha",
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "exportedWith": (
            f"ToneGet v{toneget.__version__} / HAOS App"
        ),
        "user": user_info,
        "profile": profile,
        "workouts": workouts,
        "activityNames": activity_names,
        "workoutCatalog": workout_catalog,
        "customWorkouts": custom_workouts,
        "strengthScoreHistory": strength_history,
        "currentStrengthScores": current_strength,
    }

    export_data = toneget.trim_export(export_data)

    temp_file = OUTPUT_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(
            export_data,
            f,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    os.replace(temp_file, OUTPUT_FILE)

    file_size = os.path.getsize(OUTPUT_FILE)
    workout_count = len(workouts)

    log(
        f"Synchronization complete: {workout_count} workouts, "
        f"{file_size / 1024:.1f} KB."
    )

    if workouts:
        latest = workouts[0]

        latest_name = (
            latest.get("workoutTitle")
            or latest.get("title")
            or latest.get("workoutType")
            or "Unknown workout"
        )

        latest_date = latest.get(
            "beginTime",
            "unknown date",
        )

        log(
            f"Latest workout: "
            f"{latest_name} ({latest_date})"
        )

    publish_state(
        mqtt_client,
        workouts,
        current_strength,
    )

    return True


def main():
    log("==========================================")
    log("ToneGet Home Assistant App starting")
    log(f"ToneGet version: {toneget.__version__}")
    log("==========================================")

    email, password, sync_interval = load_options()

    log(
        f"Automatic synchronization: "
        f"every {sync_interval} seconds"
    )

    log(f"Output file: {OUTPUT_FILE}")

    mqtt_client = connect_mqtt()

    publish_discovery(mqtt_client)

    try:
        while True:
            try:
                sync_tonal(
                    email,
                    password,
                    mqtt_client,
                )

            except KeyboardInterrupt:
                log("Shutdown requested.")
                break

            except Exception as exc:
                log(f"SYNC FAILED: {exc}")
                traceback.print_exc()

            log(
                f"Next synchronization in "
                f"{sync_interval} seconds."
            )

            time.sleep(sync_interval)

    finally:
        try:
            mqtt_client.publish(
                MQTT_AVAILABILITY_TOPIC,
                payload="offline",
                qos=1,
                retain=True,
            )

            time.sleep(0.25)

            mqtt_client.loop_stop()
            mqtt_client.disconnect()

        except Exception:
            pass


if __name__ == "__main__":
    main()