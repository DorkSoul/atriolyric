import logging
import re
from pathlib import Path

import httpx
import mutagen

from app.config import settings
from app.metadata_providers._throttle import Throttle
from app.metadata_providers.result import failed, retry_after_seconds

LRCLIB_BASE_URL = "https://lrclib.net/api"
# lrclib asks clients to leave 200-500 ms between requests (see
# docs/providers.md); it also carries the pause after a 429/503.
_throttle = Throttle(0.3)
logger = logging.getLogger(__name__)

# [mm:ss], [mm:ss.xx], [mm:ss:xx] - the time tags of LRC lyrics.
_LRC_TIME_TAG = re.compile(r"\[\d+:\d{1,2}(?:[.:]\d+)?\]")
# Metadata tags such as [ar:Artist], [ti:Title], [offset:0].
_LRC_META_TAG = re.compile(r"^\s*\[[a-zA-Z#]+:[^\]]*\]\s*$")


def normalize_lyrics(plain: str | None, synced: str | None) -> tuple[str | None, str | None]:
    """Embedded lyrics tags often hold LRC text. Whatever field it arrived in,
    timestamped text is returned as `synced` and `plain` never carries
    timestamps (it is the synced text with the tags stripped when nothing
    better exists)."""
    if not synced and plain and _LRC_TIME_TAG.search(plain):
        synced, plain = plain, None
    if synced and not _LRC_TIME_TAG.search(synced):
        plain, synced = plain or synced, None
    if plain and _LRC_TIME_TAG.search(plain):
        plain = None
    if plain is None and synced:
        lines = []
        for row in synced.splitlines():
            if _LRC_META_TAG.match(row):
                continue
            lines.append(_LRC_TIME_TAG.sub("", row).strip())
        plain = "\n".join(lines).strip() or None
    return plain, synced


def get_embedded_lyrics(path: Path) -> str | None:
    """Check the file's own lyrics tag first - free, no network, and often
    already correct. Different containers store it under different keys:
    ID3 USLT frames, Vorbis comments (FLAC/OGG), or the MP4 lyrics atom."""
    try:
        audio = mutagen.File(path)
    except Exception:  # noqa: BLE001 - mutagen raises many different errors on a corrupt or odd file; treat all as "no tags"
        return None
    if audio is None or audio.tags is None:
        return None
    tags = audio.tags

    for frame_id, frame in tags.items():
        if str(frame_id).startswith("USLT"):
            text = getattr(frame, "text", None)
            if text:
                return str(text)

    for key in ("LYRICS", "UNSYNCEDLYRICS", "\xa9lyr"):
        try:
            value = tags.get(key)
        except (KeyError, ValueError):
            # Each container only accepts its own key spellings: Vorbis
            # comments (FLAC/Ogg) raise ValueError for the MP4 "\xa9lyr".
            continue
        if value:
            return str(value[0]) if isinstance(value, list) else str(value)

    return None


def fetch_lrclib_lyrics(
    artist: str,
    title: str,
    album: str | None = None,
    duration_seconds: float | None = None,
) -> dict | None:
    """Fall back to lrclib.net (free, no key, community-run) for anything
    missing an embedded tag. Tries an exact match first (`/get`, needs
    artist+title+album+duration to agree), then a fuzzy `/search` as a
    looser fallback. Returns {"plain": str|None, "synced": str|None}, {}
    when lrclib has nothing, or None when it could not be asked (not to be
    remembered as a miss)."""
    if not artist or not title:
        return {}
    if not _throttle.wait():
        failed("lrclib is busy - try again shortly")
        return None

    if album and duration_seconds:
        try:
            resp = httpx.get(
                f"{LRCLIB_BASE_URL}/get",
                params={
                    "artist_name": artist,
                    "track_name": title,
                    "album_name": album,
                    "duration": round(duration_seconds),
                },
                headers={"User-Agent": settings.user_agent},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "plain": data.get("plainLyrics") or None,
                    "synced": data.get("syncedLyrics") or None,
                }
        except Exception:
            logger.debug("Exact-match lyrics lookup failed", exc_info=True)

    try:
        resp = httpx.get(
            f"{LRCLIB_BASE_URL}/search",
            params={"track_name": title, "artist_name": artist},
            headers={"User-Agent": settings.user_agent},
            timeout=10,
        )
        if resp.status_code in (429, 503):
            failed(f"lrclib HTTP {resp.status_code}", retry_after_seconds(resp), _throttle)
            return None
        resp.raise_for_status()
        results = resp.json()
    except Exception as exc:  # noqa: BLE001
        failed(f"lrclib request failed: {type(exc).__name__}")
        return None

    if not isinstance(results, list):
        failed("lrclib returned an invalid response")
        return None
    top = next((row for row in results if isinstance(row, dict)), None)
    if top is None:
        return {}
    return {
        "plain": top.get("plainLyrics") or None,
        "synced": top.get("syncedLyrics") or None,
    }
