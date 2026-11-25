"""The app's one version source: ``version.json``, which the web build
writes next to the built frontend from ``web/package.json`` (see
web/vite.config.ts). The FastAPI app version, the outbound User-Agent and
GET /api/version all read it. Deliberately imports nothing from the app, so
app.config can use it."""

import json
from pathlib import Path

# Development and the tests serve no built frontend, so there is no file.
FALLBACK_VERSION = "0.0.0+dev"
SOURCE_URL = "https://github.com/DorkSoul/atriolyric"


def read_web_version(web_dist_path: Path | None) -> str | None:
    """The version in `web_dist_path`/version.json, or None when there is no
    built frontend or the file is missing or unreadable."""
    if web_dist_path is None:
        return None
    try:
        data = json.loads((web_dist_path / "version.json").read_text())
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else None


def app_version(web_dist_path: Path | None) -> str:
    """The version to report, never empty: FALLBACK_VERSION without a build."""
    return read_web_version(web_dist_path) or FALLBACK_VERSION


def default_user_agent(web_dist_path: Path | None) -> str:
    return f"atriolyric/{app_version(web_dist_path)} (+{SOURCE_URL})"
