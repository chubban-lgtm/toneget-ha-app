#!/usr/bin/env python3

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import sync_workouts as toneget


OPTIONS_FILE = "/data/options.json"
OUTPUT_FILE = "/data/tonal_latest.json"


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


def sync_tonal(email, password):
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

    profile = toneget.get_user_profile(id_token, user_id)
    workouts = toneget.download_workouts(id_token, user_id)

    workout_catalog = toneget.fetch_workout_catalog(id_token, workouts)
    toneget.apply_workout_titles(workouts, workout_catalog)

    activity_names = toneget.build_activity_names(workouts)

    custom_workouts = toneget.build_custom_workouts(
        workouts,
        workout_catalog
    )

    strength_history = toneget.get_strength_score_history(
        id_token,
        user_id
    )

    current_strength = toneget.get_current_strength_scores(
        id_token,
        user_id
    )

    workouts.sort(
        key=lambda x: x.get("beginTime", ""),
        reverse=True
    )

    export_data = {
        "version": "3.0-ha",
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "exportedWith": f"ToneGet v{toneget.__version__} / HAOS App",
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
            ensure_ascii=False
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
            or latest.get("workoutType")
            or "Unknown workout"
        )
        latest_date = latest.get("beginTime", "unknown date")
        log(f"Latest workout: {latest_name} ({latest_date})")

    return True


def main():
    log("==========================================")
    log("ToneGet Home Assistant App starting")
    log(f"ToneGet version: {toneget.__version__}")
    log("==========================================")

    email, password, sync_interval = load_options()

    log(f"Automatic synchronization: every {sync_interval} seconds")
    log(f"Output file: {OUTPUT_FILE}")

    while True:
        try:
            sync_tonal(email, password)
        except KeyboardInterrupt:
            log("Shutdown requested.")
            return
        except Exception as exc:
            log(f"SYNC FAILED: {exc}")
            traceback.print_exc()

        log(f"Next synchronization in {sync_interval} seconds.")
        time.sleep(sync_interval)


if __name__ == "__main__":
    main()
