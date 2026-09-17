#!/usr/bin/env python3
"""
ToneGet - Tonal Workout Data Export v3.0.0

Download your personal Tonal workout history to a local JSON file.
Includes strength scores, custom workouts, and full set-by-set data.

This script authenticates with your Tonal account and downloads your
workout data for backup, analysis, or use with third-party tools.

Usage:
    python sync_workouts.py [options]

Options:
    --full          Export all raw data (no trimming, larger file)
    --no-gzip       Skip gzip compression
    --json-only     Only output JSON (no gzip), same as --no-gzip

Output:
    tonal_workouts_YYYYMMDD_HHMMSS.json.gz  (compressed, recommended)
    tonal_workouts_YYYYMMDD_HHMMSS.json     (uncompressed)

Requirements:
    pip install requests

DISCLAIMER: This is an unofficial tool, not affiliated with Tonal Systems, Inc.
Use at your own risk. See LICENSE for details.
"""

import requests
import json
import gzip
import sys
import os
from datetime import datetime
from getpass import getpass
from typing import List, Dict, Any

__version__ = "3.1.0"

# Tonal's public OAuth2 client (used by their mobile app)
AUTH0_DOMAIN = "tonal.auth0.com"
CLIENT_ID = "ERCyexW-xoVG_Yy3RDe-eV4xsOnRHP6L"
API_BASE = "https://api.tonal.com"

# Known Tonal workout types (anything else is likely custom)
KNOWN_WORKOUT_TYPES = ['PROGRAM', 'ON_DEMAND', 'QUICK_FIT', 'LIVE', 'MOVEMENT', 'ASSESSMENT']


# ============================================================================
# DATA TRIMMING
# Remove fields the dashboard doesn't use to reduce file size
# ============================================================================

SET_FIELDS_TO_REMOVE = [
    'beginTimeMCB', 'endTimeMCB',
    'romWeightMode', 'romWeight', 'romWeightFrac',
    'isoModeSpeed', 'dualMotorReps', 'suggestedResistanceLevel',
    'offMachineModifiedWeight', 'userWeightPounds', 'meanMaxPos',
    'velAtMaxConPower', 'weightAtMaxConPower',
    'inchesUpdated', 'powerUpdated',
    'triggeredFeedback', 'reps',
    'workoutActivityID', 'workoutId', 'userId', 'setId',
]

WORKOUT_FIELDS_TO_REMOVE = ['deletedAt']

USER_FIELDS_TO_REMOVE = [
    'recentMobileDevice', 'auth0Id', 'isGuestAccount', 'isDemoAccount',
    'watchedSafetyVideo', 'social', 'profileAssetID', 'mobileWorkoutsEnabled',
    'accountType', 'sharingCustomWorkoutsDisabled', 'workoutDurationMin',
    'workoutDurationMax', 'updatedPreferencesAt', 'primaryDeviceType',
    'emailVerified', 'workoutsPerWeek',
]


def trim_dict(data: dict, fields_to_remove: list) -> dict:
    """Remove specified fields from a dictionary."""
    return {k: v for k, v in data.items() if k not in fields_to_remove}


def trim_set(set_data: dict) -> dict:
    """Remove unused fields from a single set."""
    return trim_dict(set_data, SET_FIELDS_TO_REMOVE)


def trim_workout(workout: dict) -> dict:
    """Remove unused fields from a workout and its sets."""
    trimmed = trim_dict(workout, WORKOUT_FIELDS_TO_REMOVE)
    
    if 'workoutSetActivity' in trimmed:
        trimmed['workoutSetActivity'] = [
            trim_set(s) for s in trimmed['workoutSetActivity']
        ]
    
    return trimmed


def trim_export(data: dict) -> dict:
    """Trim entire export to remove unused fields."""
    trimmed = data.copy()
    
    if 'user' in trimmed and trimmed['user']:
        trimmed['user'] = trim_dict(trimmed['user'], USER_FIELDS_TO_REMOVE)
    
    if 'profile' in trimmed and trimmed['profile']:
        trimmed['profile'] = trim_dict(trimmed['profile'], USER_FIELDS_TO_REMOVE)
    
    if 'workouts' in trimmed:
        trimmed['workouts'] = [trim_workout(w) for w in trimmed['workouts']]
    
    return trimmed


# ============================================================================
# AUTHENTICATION & API
# ============================================================================

def authenticate(email: str, password: str) -> dict:
    """
    Authenticate with Tonal using OAuth2 Resource Owner Password Grant.
    
    This is the same authentication flow used by Tonal's mobile app.
    Credentials are sent directly to Tonal's Auth0 instance over HTTPS.
    """
    print("\n🔐 Authenticating with Tonal...")
    
    try:
        response = requests.post(
            f"https://{AUTH0_DOMAIN}/oauth/token",
            json={
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "username": email,
                "password": password,
                "scope": "openid profile email offline_access"
            },
            timeout=30
        )
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Failed to connect to Tonal: {e}")
    
    if response.status_code == 401:
        raise ValueError("Invalid email or password")
    elif response.status_code == 403:
        raise PermissionError("Access denied. Your account may be locked or require verification.")
    elif response.status_code != 200:
        raise Exception(f"Authentication failed: {response.status_code} - {response.text}")
    
    print("✅ Authentication successful!")
    return response.json()


def get_user_info(id_token: str) -> dict:
    """Get basic user info including user ID."""
    headers = {"Authorization": f"Bearer {id_token}"}
    response = requests.get(f"{API_BASE}/v6/users/userinfo", headers=headers, timeout=30)
    
    if response.status_code != 200:
        raise Exception(f"Failed to get user info: {response.status_code}")
    
    return response.json()


def get_user_profile(id_token: str, user_id: str) -> dict:
    """Get user profile with stats like total workouts and volume."""
    headers = {"Authorization": f"Bearer {id_token}"}
    response = requests.get(
        f"{API_BASE}/v6/users/{user_id}/profile", 
        headers=headers, 
        timeout=30
    )
    
    if response.status_code != 200:
        return {}
    
    return response.json()


def download_workouts(id_token: str, user_id: str) -> List[Dict[Any, Any]]:
    """
    Download all workout activities using header-based pagination.
    
    Tonal's API uses custom headers (pg-offset, pg-limit, pg-total) for pagination.
    """
    all_workouts = []
    offset = 0
    limit = 100  # API maximum
    
    base_url = f"{API_BASE}/v6/users/{user_id}/workout-activities"
    
    # First request to get total count
    headers = {
        "Authorization": f"Bearer {id_token}",
        "pg-offset": "0",
        "pg-limit": str(limit)
    }
    
    response = requests.get(base_url, headers=headers, timeout=30)
    
    if response.status_code != 200:
        raise Exception(f"Failed to fetch workouts: {response.status_code}")
    
    total = int(response.headers.get('pg-total', 0))
    
    if total == 0:
        print("📭 No workouts found.")
        return []
    
    print(f"\n📊 Found {total} workouts to download")
    print("-" * 40)
    
    while offset < total:
        headers = {
            "Authorization": f"Bearer {id_token}",
            "pg-offset": str(offset),
            "pg-limit": str(limit)
        }
        
        response = requests.get(base_url, headers=headers, timeout=30)
        
        if response.status_code != 200:
            print(f"⚠️  Error at offset {offset}, continuing...")
            offset += limit
            continue
        
        batch = response.json()
        all_workouts.extend(batch)
        
        # Progress indicator
        downloaded = min(offset + limit, total)
        pct = (downloaded / total) * 100
        bar_len = 20
        filled = int(bar_len * downloaded / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r   [{bar}] {downloaded}/{total} ({pct:.0f}%)", end="", flush=True)
        
        offset += limit
    
    print()  # Newline after progress bar
    print(f"✅ Downloaded {len(all_workouts)} workouts!")
    
    return all_workouts


def get_workout_template(id_token: str, workout_id: str) -> dict:
    """Fetch a single workout template by ID."""
    headers = {"Authorization": f"Bearer {id_token}"}
    response = requests.get(
        f"{API_BASE}/v6/workouts/{workout_id}", 
        headers=headers, 
        timeout=30
    )
    
    if response.status_code != 200:
        return None
    
    return response.json()


def is_custom_workout(workout: dict) -> bool:
    """Return True when a workout activity references a custom template."""
    workout_type = workout.get('workoutType', '')
    return workout_type == 'Custom' or workout_type not in KNOWN_WORKOUT_TYPES


def build_workout_template_entry(details: dict, workout_type: str = None) -> dict:
    """Keep only minimal template metadata useful for naming user activities."""
    title = (
        details.get("title")
        or details.get("name")
        or details.get("displayName")
        or details.get("workoutTitle")
    )

    entry = {
        "id": details.get("id"),
        "title": title,
    }

    if workout_type:
        entry["workoutType"] = workout_type

    if details.get("userId"):
        entry["userId"] = details.get("userId")

    return {key: value for key, value in entry.items() if value is not None}


def fetch_workout_catalog(id_token: str, workouts: List[dict]) -> Dict[str, dict]:
    """
    Fetch minimal name metadata for workout templates referenced by user activities.
    
    This does not export program structure or instructional content; it only keeps
    IDs and titles so activity records can be associated with human-readable names.
    """
    template_types: Dict[str, str] = {}

    for workout in workouts:
        template_id = workout.get('workoutId')
        if template_id:
            template_types.setdefault(template_id, workout.get('workoutType', ''))

    if not template_types:
        return {}

    print(f"\n🏋️  Fetching names for {len(template_types)} workout templates...")

    workout_catalog = {}
    for i, (workout_id, workout_type) in enumerate(template_types.items(), 1):
        details = get_workout_template(id_token, workout_id)
        if details:
            entry = build_workout_template_entry(details, workout_type)
            if entry.get("title"):
                workout_catalog[workout_id] = entry

        if i % 20 == 0:
            print(f"   Fetched {i}/{len(template_types)}...")

    print(f"   ✅ Fetched names for {len(workout_catalog)} workout templates!")
    return workout_catalog


def apply_workout_titles(workouts: List[dict], workout_catalog: Dict[str, dict]) -> None:
    """Add workoutTitle to each activity when a template title is available."""
    for workout in workouts:
        template_id = workout.get('workoutId')
        title = workout_catalog.get(template_id, {}).get('title')
        if title:
            workout['workoutTitle'] = title


def build_activity_names(workouts: List[dict]) -> Dict[str, str]:
    """Build a direct activity ID -> activity name mapping."""
    activity_names = {}

    for workout in workouts:
        activity_id = workout.get('id') or workout.get('workoutActivityID')
        title = workout.get('workoutTitle')
        if activity_id and title:
            activity_names[activity_id] = title

    return activity_names


def build_custom_workouts(workouts: List[dict], workout_catalog: Dict[str, dict]) -> Dict[str, dict]:
    """Return the legacy customWorkouts map from the full workout catalog."""
    custom_workouts = {}

    for workout in workouts:
        template_id = workout.get('workoutId')
        if template_id and is_custom_workout(workout) and template_id in workout_catalog:
            custom_workouts[template_id] = workout_catalog[template_id]

    return custom_workouts


def get_strength_score_history(id_token: str, user_id: str) -> List[dict]:
    """Fetch complete strength score history."""
    print("\n💪 Fetching Strength Score history...")
    
    headers = {"Authorization": f"Bearer {id_token}"}
    today = datetime.now().strftime('%Y-%m-%d')
    
    response = requests.get(
        f"{API_BASE}/v6/users/{user_id}/strength-scores/history",
        headers=headers,
        params={'limit': 5000, 'endDate': today},
        timeout=30
    )
    
    if response.status_code != 200:
        print(f"   ⚠️ Failed to fetch strength score history: {response.status_code}")
        return []
    
    history = response.json()
    
    if history:
        print(f"   ✅ Got {len(history)} strength score entries")
        latest = history[0] if history else None
        if latest:
            print(f"   🏆 Current Score: {latest.get('overall', 'N/A')}")
    else:
        print("   ⚠️ No strength score history found")
    
    return history


def get_current_strength_scores(id_token: str, user_id: str) -> dict:
    """
    Fetch current strength scores with granular muscle group breakdown.
    
    Returns both raw API response and parsed format for easier use.
    """
    print("\n💪 Fetching granular Strength Score breakdown...")
    
    headers = {"Authorization": f"Bearer {id_token}"}
    
    response = requests.get(
        f"{API_BASE}/v6/users/{user_id}/strength-scores/current",
        headers=headers,
        timeout=30
    )
    
    if response.status_code != 200:
        print(f"   ⚠️ Failed to fetch current strength scores: {response.status_code}")
        return {}
    
    data = response.json()
    
    if isinstance(data, list) and len(data) > 0:
        print(f"   ✅ Got granular strength score data!")
        
        # Parse into more usable format
        parsed = {
            'regions': {},
            'muscles': {}
        }
        
        for region in data:
            region_name = region.get('strengthBodyRegion', 'Unknown')
            region_score = region.get('score', 0)
            
            parsed['regions'][region_name] = region_score
            
            for muscle in region.get('familyActivity', []):
                muscle_name = muscle.get('strengthFamily', 'Unknown')
                muscle_score = muscle.get('score', 0)
                parsed['muscles'][muscle_name] = {
                    'score': round(muscle_score),
                    'region': region_name,
                    'updatedAt': muscle.get('updatedAt')
                }
        
        if 'Overall' in parsed['regions']:
            print(f"   🏆 Overall: {parsed['regions']['Overall']}")
        
        return {
            'raw': data,
            'parsed': parsed
        }
    else:
        print("   ⚠️ No granular strength score data found")
        return {}


# ============================================================================
# FILE OUTPUT
# ============================================================================

def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def save_export(data: dict, base_filename: str, use_gzip: bool = True, trim: bool = True) -> dict:
    """
    Save export data to file(s).
    
    Returns dict with filenames and sizes.
    """
    results = {}
    
    # Apply trimming if requested
    if trim:
        print("\n✂️  Trimming unused fields to reduce file size...")
        output_data = trim_export(data)
    else:
        output_data = data
    
    # Convert to compact JSON (no pretty-printing)
    json_str = json.dumps(output_data, separators=(',', ':'))
    json_bytes = json_str.encode('utf-8')
    
    # Always save uncompressed JSON
    json_filename = f"{base_filename}.json"
    with open(json_filename, 'w') as f:
        f.write(json_str)
    
    json_size = len(json_bytes)
    results['json'] = {
        'filename': json_filename,
        'size': json_size
    }
    
    # Save gzipped version if requested
    if use_gzip:
        gz_filename = f"{base_filename}.json.gz"
        with gzip.open(gz_filename, 'wb', compresslevel=9) as f:
            f.write(json_bytes)
        
        gz_size = os.path.getsize(gz_filename)
        compression_ratio = (1 - gz_size / json_size) * 100
        
        results['gzip'] = {
            'filename': gz_filename,
            'size': gz_size,
            'compression_ratio': compression_ratio
        }
    
    return results


def print_summary(workouts: List[dict], custom_workouts: dict, workout_catalog: dict, strength_history: List[dict]) -> None:
    """Print summary statistics."""
    total_volume = sum(w.get('totalVolume', 0) for w in workouts)
    total_reps = sum(w.get('totalReps', 0) for w in workouts)
    dates = [w.get('beginTime') for w in workouts if w.get('beginTime')]
    
    print("\n" + "=" * 50)
    print("📊 YOUR DATA")
    print("=" * 50)
    print(f"   Workouts:        {len(workouts)}")
    if workout_catalog:
        print(f"   Named Templates: {len(workout_catalog)}")
    if custom_workouts:
        print(f"   Custom Workouts: {len(custom_workouts)}")
    print(f"   Total Volume:    {total_volume:,} lbs")
    print(f"   Total Reps:      {total_reps:,}")
    if dates:
        print(f"   Date Range:      {min(dates)[:10]} → {max(dates)[:10]}")
    
    if strength_history:
        latest = strength_history[0]
        print(f"\n   💪 Strength Score: {latest.get('overall', 'N/A')}")
        upper = latest.get('upper', 'N/A')
        lower = latest.get('lower', 'N/A')
        core = latest.get('core', 'N/A')
        print(f"      Upper: {upper} | Lower: {lower} | Core: {core}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    # Parse command line args
    use_full = '--full' in sys.argv
    skip_gzip = '--no-gzip' in sys.argv or '--json-only' in sys.argv
    
    print("=" * 50)
    print(f"🏋️  TONEGET v{__version__}")
    print("   Export your Tonal workout data")
    print("=" * 50)
    
    if use_full:
        print("\n   Mode: FULL (all raw data, larger file)")
    else:
        print("\n   Mode: Optimized (trimmed for smaller files)")
    
    print("\n⚠️  Disclaimer: Unofficial tool, not affiliated with Tonal.")
    
    # Get credentials
    print("\n" + "-" * 50)
    email = input("Tonal email: ").strip()
    if not email:
        print("❌ Email is required")
        sys.exit(1)
    
    password = getpass("Tonal password: ")
    if not password:
        print("❌ Password is required")
        sys.exit(1)
    
    try:
        # Authenticate
        tokens = authenticate(email, password)
        id_token = tokens["id_token"]
        
        # Get user info
        user_info = get_user_info(id_token)
        user_id = user_info.get("id")
        print(f"\n👤 Logged in as: {user_info.get('firstName')} {user_info.get('lastName')}")
        
        # Get profile
        profile = get_user_profile(id_token, user_id)
        if profile.get('totalWorkouts'):
            print(f"   Total workouts on record: {profile.get('totalWorkouts')}")
        
        # Download workouts
        workouts = download_workouts(id_token, user_id)
        
        if not workouts:
            print("\n❌ No workouts to export")
            sys.exit(0)
        
        # Fetch minimal workout title metadata
        workout_catalog = fetch_workout_catalog(id_token, workouts)
        apply_workout_titles(workouts, workout_catalog)
        activity_names = build_activity_names(workouts)
        custom_workouts = build_custom_workouts(workouts, workout_catalog)
        
        # Fetch strength scores
        strength_history = get_strength_score_history(id_token, user_id)
        current_strength = get_current_strength_scores(id_token, user_id)
        
        # Sort by date (newest first)
        workouts.sort(key=lambda x: x.get('beginTime', ''), reverse=True)
        
        # Build export
        export_data = {
            'version': '3.0',
            'exportedAt': datetime.now().isoformat() + 'Z',
            'exportedWith': f'ToneGet v{__version__}',
            'user': user_info,
            'profile': profile,
            'workouts': workouts,
            'activityNames': activity_names,
            'workoutCatalog': workout_catalog,
            'customWorkouts': custom_workouts,
            'strengthScoreHistory': strength_history,
            'currentStrengthScores': current_strength,
        }
        
        # Save files
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base_filename = f"tonal_workouts_{timestamp}"
        
        print("\n" + "=" * 50)
        print("💾 SAVING EXPORT")
        print("=" * 50)
        
        file_results = save_export(
            export_data, 
            base_filename, 
            use_gzip=not skip_gzip,
            trim=not use_full
        )
        
        # Print summary
        print_summary(workouts, custom_workouts, workout_catalog, strength_history)
        
        # Print file info
        print("\n" + "=" * 50)
        print("📁 FILES CREATED")
        print("=" * 50)
        
        json_info = file_results.get('json', {})
        print(f"\n   {json_info.get('filename')}")
        print(f"      Size: {format_size(json_info.get('size', 0))}")
        
        if 'gzip' in file_results:
            gz_info = file_results['gzip']
            print(f"\n   {gz_info.get('filename')} ⬅️  RECOMMENDED")
            print(f"      Size: {format_size(gz_info.get('size', 0))}")
            print(f"      Compression: {gz_info.get('compression_ratio', 0):.0f}% smaller")
        
        print("\n" + "=" * 50)
        print("✅ EXPORT COMPLETE!")
        print("=" * 50)
        print("\n🎉 Your Tonal workout data has been exported!")
        print("   This is YOUR data - do with it what you will.\n")
        
    except ValueError as e:
        print(f"\n❌ {e}")
        sys.exit(1)
    except ConnectionError as e:
        print(f"\n❌ Connection error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Cancelled by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
