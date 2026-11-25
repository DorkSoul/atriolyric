"""The Android app's APK, when this server carries one.

The release build puts the signed APK at `settings.android_apk_path` (the
image has it at /app/android/atriolyric.apk) with a small `<name>.json` beside
it naming its version. GET /api/version says whether there is one, so the web
app can offer it and an installed app can tell it is out of date; GET
/api/download/app/android sends it. Deliberately imports nothing from the app.
"""

import json
from pathlib import Path

APK_MEDIA_TYPE = "application/vnd.android.package-archive"


def android_app_info(apk_path: Path | None, fallback_version: str | None) -> dict | None:
    """{version, size} of the APK on offer, or None when there is none.

    The version comes from the JSON file beside the APK; without one it is
    taken to be the web app's own (they are built from the same release)."""
    if apk_path is None:
        return None
    try:
        size = apk_path.stat().st_size
    except OSError:
        return None
    if size <= 0 or not apk_path.is_file():
        return None
    version = fallback_version
    try:
        data = json.loads(apk_path.with_name(apk_path.name + ".json").read_text())
        named = data.get("version") if isinstance(data, dict) else None
        if isinstance(named, str) and named:
            version = named
    except (OSError, ValueError):
        pass
    if not version:
        return None
    return {"version": version, "size": size}
