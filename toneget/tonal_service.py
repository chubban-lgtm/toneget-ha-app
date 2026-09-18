#!/usr/bin/env python3
"""Local Tonal companion service for Home Assistant.

The service never requires Tonal credentials in environment variables. Home Assistant
submits credentials only when authentication is needed; the password is used for the
OAuth exchange and is not persisted by this service.
"""

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from sync_workouts import (
    apply_workout_titles,
    authenticate,
    build_activity_names,
    build_custom_workouts,
    download_workouts,
    fetch_workout_catalog,
    get_current_strength_scores,
    get_strength_score_history,
    get_user_info,
    get_user_profile,
)

HOST = os.getenv("TONAL_SERVICE_HOST", "0.0.0.0")
PORT = int(os.getenv("TONAL_SERVICE_PORT", "8787"))
SYNC_INTERVAL_MINUTES = int(os.getenv("TONAL_SYNC_INTERVAL_MINUTES", "180"))
TOKEN_FILE = os.getenv("TONAL_TOKEN_FILE", "/data/tonal_token.json")

_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    "status": "auth_required",
    "auth_required": True,
    "last_sync": None,
    "last_error": None,
    "summary": {},
}

_token_lock = threading.Lock()
_tokens: Dict[str, Any] = {}


def _parse_dt(value: str):
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_number(value):
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_tokens() -> None:
    global _tokens
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data.get("id_token"):
            with _token_lock:
                _tokens = data
            with _state_lock:
                _state.update({"status": "starting", "auth_required": False})
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        with _state_lock:
            _state["last_error"] = f"Could not load saved Tonal token: {exc}"


def _save_tokens(tokens: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(TOKEN_FILE) or ".", exist_ok=True)
    temp = f"{TOKEN_FILE}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(tokens, handle, separators=(",", ":"))
    os.chmod(temp, 0o600)
    os.replace(temp, TOKEN_FILE)


def _set_tokens(tokens: Dict[str, Any]) -> None:
    global _tokens
    with _token_lock:
        _tokens = tokens
    _save_tokens(tokens)
    with _state_lock:
        _state.update({"auth_required": False, "status": "starting", "last_error": None})


def _clear_tokens(error: Optional[str] = None) -> None:
    global _tokens
    with _token_lock:
        _tokens = {}
    try:
        os.remove(TOKEN_FILE)
    except FileNotFoundError:
        pass
    with _state_lock:
        _state.update({
            "status": "auth_required",
            "auth_required": True,
            "last_error": error,
        })


def _workout_duration(workout: dict) -> Any:
    return workout.get("duration") or workout.get("durationSeconds") or workout.get("workoutDuration")


def _set_summary(workout: dict) -> dict[str, Any]:
    sets = workout.get("workoutSetActivity") or []
    movement_groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "sets": 0,
            "reps": 0,
            "volume": 0.0,
            "max_weight": None,
            "max_one_rep_max": None,
            "best_range_of_motion": None,
        }
    )
    total_sets = 0
    total_reps = 0
    max_weight = None
    max_one_rep_max = None
    best_rom = None
    max_power = None

    for item in sets:
        total_sets += 1
        reps = _safe_number(item.get("repCount")) or 0
        weight = _safe_number(item.get("weight"))
        one_rm = _safe_number(item.get("oneRepMax"))
        rom = _safe_number(item.get("rangeOfMotion"))
        power = _safe_number(item.get("power")) or _safe_number(item.get("maxConPower"))
        total_reps += int(reps)
        if weight is not None:
            max_weight = weight if max_weight is None else max(max_weight, weight)
        if one_rm is not None:
            max_one_rep_max = one_rm if max_one_rep_max is None else max(max_one_rep_max, one_rm)
        if rom is not None:
            best_rom = rom if best_rom is None else max(best_rom, rom)
        if power is not None:
            max_power = power if max_power is None else max(max_power, power)

        movement_id = str(item.get("movementId") or item.get("activityId") or "unknown")
        group = movement_groups[movement_id]
        group["sets"] += 1
        group["reps"] += int(reps)
        if weight is not None and reps:
            group["volume"] += weight * reps
        if weight is not None:
            group["max_weight"] = weight if group["max_weight"] is None else max(group["max_weight"], weight)
        if one_rm is not None:
            group["max_one_rep_max"] = one_rm if group["max_one_rep_max"] is None else max(group["max_one_rep_max"], one_rm)
        if rom is not None:
            group["best_range_of_motion"] = rom if group["best_range_of_motion"] is None else max(group["best_range_of_motion"], rom)

    movements = []
    for movement_id, stats in movement_groups.items():
        movements.append({"movement_id": movement_id, **stats, "volume": round(stats["volume"], 1)})
    movements.sort(key=lambda item: (-item["volume"], item["movement_id"]))

    return {
        "set_count": total_sets,
        "reps_from_sets": total_reps,
        "movement_count": len(movement_groups),
        "max_weight": max_weight,
        "max_one_rep_max": max_one_rep_max,
        "best_range_of_motion": best_rom,
        "max_power": max_power,
        "movements": movements[:25],
    }


def build_summary(export_data: Dict[str, Any]) -> Dict[str, Any]:
    workouts = export_data.get("workouts", [])
    now = datetime.now(timezone.utc)
    latest = workouts[0] if workouts else {}

    def workouts_since(days: int) -> list[dict]:
        cutoff = now - timedelta(days=days)
        return [workout for workout in workouts if (_parse_dt(workout.get("beginTime")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]

    def volume(workout_list: list[dict]) -> float:
        return sum(float(workout.get("totalVolume") or 0) for workout in workout_list)

    def reps(workout_list: list[dict]) -> int:
        return sum(int(workout.get("totalReps") or 0) for workout in workout_list)

    windows = {}
    for days in (7, 14, 30, 90, 365):
        selected = workouts_since(days)
        windows[str(days)] = {
            "workouts": len(selected),
            "volume": volume(selected),
            "reps": reps(selected),
            "avg_volume_per_workout": round(volume(selected) / len(selected), 1) if selected else 0,
        }

    latest_set_summary = _set_summary(latest)
    recent = workouts[:10]
    recent_workouts = []
    workout_types: dict[str, int] = defaultdict(int)
    max_workout_volume = None
    max_workout_reps = None
    max_set_weight = None
    max_one_rm = None
    best_rom = None
    max_power = None
    total_sets = 0

    for workout in workouts:
        workout_types[str(workout.get("workoutType") or "Unknown")] += 1
        workout_volume = _safe_number(workout.get("totalVolume"))
        workout_reps = _safe_number(workout.get("totalReps"))
        if workout_volume is not None:
            max_workout_volume = workout_volume if max_workout_volume is None else max(max_workout_volume, workout_volume)
        if workout_reps is not None:
            max_workout_reps = workout_reps if max_workout_reps is None else max(max_workout_reps, workout_reps)
        set_stats = _set_summary(workout)
        total_sets += set_stats["set_count"]
        for current, name in (
            (set_stats["max_weight"], "max_set_weight"),
            (set_stats["max_one_rep_max"], "max_one_rm"),
            (set_stats["best_range_of_motion"], "best_rom"),
            (set_stats["max_power"], "max_power"),
        ):
            if current is None:
                continue
            if name == "max_set_weight":
                max_set_weight = current if max_set_weight is None else max(max_set_weight, current)
            elif name == "max_one_rm":
                max_one_rm = current if max_one_rm is None else max(max_one_rm, current)
            elif name == "best_rom":
                best_rom = current if best_rom is None else max(best_rom, current)
            else:
                max_power = current if max_power is None else max(max_power, current)

    for workout in recent:
        recent_workouts.append({
            "id": workout.get("id") or workout.get("workoutActivityID"),
            "begin_time": workout.get("beginTime"),
            "title": workout.get("workoutTitle"),
            "type": workout.get("workoutType"),
            "total_volume": workout.get("totalVolume"),
            "total_reps": workout.get("totalReps"),
            "duration": _workout_duration(workout),
            **_set_summary(workout),
        })

    strength_history = export_data.get("strengthScoreHistory") or []
    strength_latest = strength_history[0] if strength_history else {}
    parsed_current = ((export_data.get("currentStrengthScores") or {}).get("parsed") or {})

    latest_dt = _parse_dt(latest.get("beginTime"))
    days_since_latest = (now - latest_dt).total_seconds() / 86400 if latest_dt else None

    return {
        "exported_at": export_data.get("exportedAt"),
        "profile": {
            "total_workouts": (export_data.get("profile") or {}).get("totalWorkouts", len(workouts)),
            "total_volume": (export_data.get("profile") or {}).get("totalVolume") or volume(workouts),
            "total_reps": reps(workouts),
            "total_sets": total_sets,
            "first_workout": workouts[-1].get("beginTime") if workouts else None,
            "last_workout": latest.get("beginTime") if latest else None,
            "days_since_last_workout": round(days_since_latest, 2) if days_since_latest is not None else None,
        },
        "latest_workout": {
            "id": latest.get("id") or latest.get("workoutActivityID"),
            "begin_time": latest.get("beginTime"),
            "title": latest.get("workoutTitle"),
            "type": latest.get("workoutType"),
            "total_volume": latest.get("totalVolume"),
            "total_reps": latest.get("totalReps"),
            "duration": _workout_duration(latest),
            **latest_set_summary,
        },
        "rolling": {
            "workouts_7d": windows["7"]["workouts"],
            "workouts_30d": windows["30"]["workouts"],
            "volume_7d": windows["7"]["volume"],
            "volume_30d": windows["30"]["volume"],
            "windows": windows,
        },
        "records": {
            "max_workout_volume": max_workout_volume,
            "max_workout_reps": max_workout_reps,
            "max_set_weight": max_set_weight,
            "max_one_rep_max": max_one_rm,
            "best_range_of_motion": best_rom,
            "max_power": max_power,
        },
        "workout_types": dict(sorted(workout_types.items())),
        "recent_workouts": recent_workouts,
        "strength": {
            "overall": strength_latest.get("overall") or (parsed_current.get("regions") or {}).get("Overall"),
            "upper": strength_latest.get("upper") or (parsed_current.get("regions") or {}).get("Upper"),
            "lower": strength_latest.get("lower") or (parsed_current.get("regions") or {}).get("Lower"),
            "core": strength_latest.get("core") or (parsed_current.get("regions") or {}).get("Core"),
            "regions": parsed_current.get("regions", {}),
            "muscles": parsed_current.get("muscles", {}),
            "history": strength_history[:100],
        },
    }


def authenticate_interactively(email: str, password: str) -> None:
    tokens = authenticate(email, password)
    if not tokens.get("id_token"):
        raise RuntimeError("Tonal authentication did not return an id_token")
    _set_tokens(tokens)
    sync_once()


def sync_once() -> None:
    with _token_lock:
        id_token = _tokens.get("id_token")
    if not id_token:
        _clear_tokens()
        return

    try:
        user_info = get_user_info(id_token)
        user_id = user_info.get("id")
        profile = get_user_profile(id_token, user_id)
        workouts = download_workouts(id_token, user_id)
        workout_catalog = fetch_workout_catalog(id_token, workouts)
        apply_workout_titles(workouts, workout_catalog)
        workouts.sort(key=lambda x: x.get("beginTime", ""), reverse=True)

        export_data = {
            "version": "3.0",
            "exportedAt": datetime.now(timezone.utc).isoformat(),
            "user": user_info,
            "profile": profile,
            "workouts": workouts,
            "activityNames": build_activity_names(workouts),
            "workoutCatalog": workout_catalog,
            "customWorkouts": build_custom_workouts(workouts, workout_catalog),
            "strengthScoreHistory": get_strength_score_history(id_token, user_id),
            "currentStrengthScores": get_current_strength_scores(id_token, user_id),
        }
        summary = build_summary(export_data)
        with _state_lock:
            _state.update({
                "status": "ok",
                "auth_required": False,
                "last_sync": datetime.now(timezone.utc).isoformat(),
                "last_error": None,
                "summary": summary,
            })
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "401" in message or "403" in message or "token" in message.lower():
            _clear_tokens("Tonal authentication expired or was rejected")
            return
        with _state_lock:
            _state.update({"status": "error", "last_error": message})


def sync_loop() -> None:
    while True:
        sync_once()
        time.sleep(max(60, SYNC_INTERVAL_MINUTES * 60))


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 16384:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):  # noqa: N802
        with _state_lock:
            snapshot = json.loads(json.dumps(_state))
        if self.path == "/health":
            self._send_json({
                "status": snapshot["status"],
                "auth_required": snapshot["auth_required"],
                "last_sync": snapshot["last_sync"],
                "last_error": snapshot["last_error"],
            })
            return
        if self.path in ("/", "/summary"):
            self._send_json(snapshot)
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if self.path == "/auth":
            payload = self._read_json()
            email = str(payload.get("email", "")).strip()
            password = str(payload.get("password", ""))
            if not email or not password:
                self._send_json({"ok": False, "error": "email and password are required"}, 400)
                return
            try:
                authenticate_interactively(email, password)
                self._send_json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(exc)}, 401)
            return
        if self.path == "/sync":
            sync_once()
            with _state_lock:
                snapshot = json.loads(json.dumps(_state))
            self._send_json(snapshot)
            return
        self._send_json({"error": "not found"}, 404)

    def log_message(self, format, *args):  # noqa: A003
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), format % args))


def main() -> None:
    _load_tokens()
    worker = threading.Thread(target=sync_loop, daemon=True)
    worker.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Tonal service listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
