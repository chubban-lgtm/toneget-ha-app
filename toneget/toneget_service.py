#!/usr/bin/env python3

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import requests
import sync_workouts as toneget


OPTIONS_FILE = "/data/options.json"
OUTPUT_FILE = "/data/tonal_latest.json"

MQTT_DISCOVERY_PREFIX = "homeassistant"
MQTT_BASE_TOPIC = "toneget"
MQTT_STATE_TOPIC = f"{MQTT_BASE_TOPIC}/state"
MQTT_WORKOUT_STATE_TOPIC = f"{MQTT_BASE_TOPIC}/workout_state"
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
    github_token = str(options.get("github_token", "")).strip()
    workout_repo = str(options.get("workout_repo", "chubban-lgtm/toneget-workout-data")).strip()
    workout_branch = str(options.get("workout_branch", "main")).strip() or "main"

    if not email:
        log("ERROR: Tonal email is not configured.")
        sys.exit(1)

    if not password:
        log("ERROR: Tonal password is not configured.")
        sys.exit(1)

    if sync_interval < 300:
        log("WARNING: sync_interval below 300 seconds; using 300.")
        sync_interval = 300

    return email, password, sync_interval, github_token, workout_repo, workout_branch


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
        "sw_version": "0.3.0",
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


def publish_workout_discovery(client):
    sensors = [
        ("manual_workout_count", "Manual Workout Count", "{{ value_json.manual_workout_count }}", "mdi:counter", None),
        ("manual_latest_workout", "Manual Latest Workout", "{{ value_json.manual_latest_workout }}", "mdi:weight-lifter", None),
        ("manual_latest_workout_date", "Manual Latest Workout Date", "{{ value_json.manual_latest_workout_date }}", "mdi:calendar-check", None),
        ("arm_relaxed", "Arm Relaxed", "{{ value_json.arm_relaxed }}", "mdi:tape-measure", "in"),
        ("arm_flexed", "Arm Flexed", "{{ value_json.arm_flexed }}", "mdi:arm-flex", "in"),
        ("arm_measurement_date", "Arm Measurement Date", "{{ value_json.arm_measurement_date }}", "mdi:calendar", None),
    ]
    for object_id, name, template, icon, unit in sensors:
        topic = f"{MQTT_DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/{object_id}/config"
        payload = {
            "name": name,
            "unique_id": f"{DEVICE_ID}_{object_id}",
            "state_topic": MQTT_WORKOUT_STATE_TOPIC,
            "availability_topic": MQTT_AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "value_template": template,
            "device": device_info(),
            "icon": icon,
        }
        if unit:
            payload["unit_of_measurement"] = unit
            payload["state_class"] = "measurement"
        client.publish(topic, json.dumps(payload), qos=1, retain=True)


def github_json(token, repo, branch, path):
    if not token or not repo:
        return None
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Accept": "application/vnd.github.raw+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ToneGet-HA",
    }
    response = requests.get(url, headers=headers, params={"ref": branch}, timeout=20)
    response.raise_for_status()
    return response.json()


def sync_workout_data(client, token, repo, branch):
    if not token:
        log("Private workout sync disabled: GitHub token not configured.")
        return
    try:
        workouts_doc = github_json(token, repo, branch, "workout_data/workouts.json") or {}
        measurements_doc = github_json(token, repo, branch, "workout_data/measurements.json") or {}
        baselines_doc = github_json(token, repo, branch, "workout_data/exercise_baselines.json") or {}

        workouts = workouts_doc.get("workouts", [])
        measurements = measurements_doc.get("measurements", [])
        baselines = baselines_doc.get("exercises", [])

        latest_workout = workouts[-1] if workouts else {}
        latest_measurement = measurements[-1] if measurements else {}

        payload = {
            "manual_workout_count": len(workouts),
            "manual_latest_workout": latest_workout.get("workout"),
            "manual_latest_workout_date": latest_workout.get("date"),
            "arm_relaxed": latest_measurement.get("arm_relaxed_in"),
            "arm_flexed": latest_measurement.get("arm_flexed_in"),
            "arm_measurement_date": latest_measurement.get("date"),
            "baseline_count": len(baselines),
            "last_workout_data_sync": datetime.now(timezone.utc).isoformat(),
        }
        client.publish(MQTT_WORKOUT_STATE_TOPIC, json.dumps(payload), qos=1, retain=True)

        for item in baselines:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            object_id = "baseline_" + "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
            while "__" in object_id:
                object_id = object_id.replace("__", "_")
            value = item.get("weight_each_lb")
            unit = "lb/arm"
            if value is None:
                value = item.get("weight_total_lb")
                unit = "lb"
            if value is None:
                value = item.get("weight_each_lb_range")
                unit = "lb/arm"
            topic = f"{MQTT_DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/{object_id}/config"
            config = {
                "name": f"{name} Baseline",
                "unique_id": f"{DEVICE_ID}_{object_id}",
                "state_topic": f"{MQTT_BASE_TOPIC}/baseline/{object_id}",
                "availability_topic": MQTT_AVAILABILITY_TOPIC,
                "device": device_info(),
                "icon": "mdi:dumbbell",
                "unit_of_measurement": unit,
            }
            client.publish(topic, json.dumps(config), qos=1, retain=True)
            client.publish(f"{MQTT_BASE_TOPIC}/baseline/{object_id}", str(value), qos=1, retain=True)

        log(f"Private workout data synchronized: {len(workouts)} workouts, {len(baselines)} baselines.")
    except Exception as exc:
        log(f"WORKOUT DATA SYNC FAILED: {exc}")


def extract_strength_scores(current_strength):
    overall = None
    upper = None
    lower = None
    core = None

    if not isinstance(current_strength, dict):
        return overall, upper, lower, core

    parsed = current_strength.get("parsed", {})
    regions = parsed.get("regions", {})

    if isinstance(regions, dict):
        overall = regions.get("Overall")
        upper = regions.get("Upper Body")
        lower = regions.get("Lower Body")
        core = regions.get("Core")

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

    email, password, sync_interval, github_token, workout_repo, workout_branch = load_options()

    log(
        f"Automatic synchronization: "
        f"every {sync_interval} seconds"
    )

    log(f"Output file: {OUTPUT_FILE}")

    mqtt_client = connect_mqtt()

    publish_discovery(mqtt_client)
    publish_workout_discovery(mqtt_client)

    try:
        while True:
            try:
                sync_tonal(
                    email,
                    password,
                    mqtt_client,
                )
                sync_workout_data(
                    mqtt_client,
                    github_token,
                    workout_repo,
                    workout_branch,
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