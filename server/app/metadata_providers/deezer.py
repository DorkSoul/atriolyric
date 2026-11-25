import threading
from time import monotonic as _clock

import httpx

from app.artist_credits import normalize_artist_name
from app.config import settings
from app.metadata_providers._throttle import Throttle
from app.metadata_providers.result import (
    NOT_FOUND,
    Found,
    ProviderResult,
    failed,
    note_failure,
    recording_failures,
    retry_after_seconds,
)

SEARCH_URL = "https://api.deezer.com/search"
# Deezer's terms (developers.deezer.com/termsofuse, sec. III.4) allow no
# local copy of its content - metadata included - and no database built from
# it. So nothing Deezer answers is stored: answers live in this process's
# memory for a few hours, then are asked again.
MEMORY_TTL_SECONDS = 6 * 3600
MEMORY_MAX_ENTRIES = 2000
# No pacing of its own (interval 0): it carries the pause after a quota error.
_throttle = Throttle(0.0)
# Deezer's quota is 50 requests per 5 seconds; error code 4 means it was hit.
_QUOTA_ERROR = 4
_QUOTA_PAUSE = 5.0


def _request(url: str, params: dict, timeout: float) -> ProviderResult[list[dict]]:
    """A search call's result rows. Deezer reports errors - its quota among
    them - as HTTP 200 with an {"error": ...} body, which counts as Failed
    here, not as an empty result to cache as "nothing"."""
    if not _throttle.wait():
        return failed("Deezer is busy - try again shortly")
    try:
        resp = httpx.get(url, params=params, headers={"User-Agent": settings.user_agent}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - any transport failure is a retry
        return failed(f"Deezer request failed: {type(exc).__name__}")
    if resp.status_code in (429, 503):
        return failed(f"Deezer HTTP {resp.status_code}", retry_after_seconds(resp) or _QUOTA_PAUSE, _throttle)
    if resp.status_code != 200:
        return failed(f"Deezer HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        return failed("Deezer returned an invalid response")
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"] if isinstance(payload["error"], dict) else {}
        code = error.get("code")
        return failed(
            f"Deezer error {code}: {error.get('message') or error.get('type') or 'unknown'}",
            _QUOTA_PAUSE if code == _QUOTA_ERROR else None,
            _throttle,
        )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return failed("Deezer returned an invalid response")
    rows = [row for row in data if isinstance(row, dict)]
    return Found(rows) if rows else NOT_FOUND


def search_artist_track(artist_name: str) -> dict | None:
    """Best-effort lookup of a representative track + 30-second preview for
    an artist, via Deezer's free, keyless public search API. Used to let a
    user hear an artist they don't own anything by yet, purely for "should
    I check this out" - never played through the real library pipeline.
    Returns None on any failure, same graceful-degradation rule as the
    other providers, and also when Deezer has no preview for the artist; a
    caller that must tell the two apart (explore_new_artists) reads the
    Failed results counted by recording_failures()."""
    if not artist_name:
        return None
    result = _request(
        SEARCH_URL,
        # A plain name search: Deezer's advanced syntax (artist:"...")
        # matches nothing. The fuzzy results are narrowed to an exact
        # artist name below.
        {"q": artist_name, "limit": 10},
        timeout=5,
    )
    if not isinstance(result, Found):
        return None
    results = result.data

    def _to_track(r: dict) -> dict | None:
        preview = r.get("preview")
        if not preview:
            return None
        artist = r.get("artist") or {}
        album = r.get("album") or {}
        return {
            "artist": artist.get("name") or artist_name,
            "title": r.get("title"),
            "artwork_url": album.get("cover_medium") or album.get("cover_big"),
            "preview_url": preview,
            # Deezer's terms require preview content to link back to the
            # real listing - this is that link, surfaced so the frontend
            # can render one instead of using the preview as a bare clip.
            "view_url": r.get("link") or artist.get("link"),
        }

    # Only an artist of exactly this name (case, accents and spacing
    # aside): the plain search is fuzzy, and a near-miss would preview, and
    # link to, someone else.
    wanted = normalize_artist_name(artist_name)
    for r in results:
        artist = r.get("artist") or {}
        if normalize_artist_name(str(artist.get("name") or "")) == wanted:
            track = _to_track(r)
            if track:
                return track
    return None


_memory_lock = threading.Lock()
_memory: dict[str, tuple[float, object]] = {}


# A failed look-up is remembered this long, so a Deezer outage costs one
# request per artist per minute, not one per page load.
FAILURE_TTL_SECONDS = 60.0
_FAILED = object()


def _key(artist_name: str) -> str:
    return normalize_artist_name(artist_name)


def peek_preview(artist_name: str) -> tuple[bool, dict | None]:
    """(known, track) from memory only - never a request. `known` is False
    when the artist was never asked, its answer expired, or the last ask
    failed."""
    now = _clock()
    with _memory_lock:
        found = _memory.get(_key(artist_name))
    if found is None:
        return False, None
    at, value = found
    if value is _FAILED or now - at >= MEMORY_TTL_SECONDS:
        return False, None
    return True, value  # type: ignore[return-value]  # _FAILED handled above


def recently_failed(artist_name: str) -> bool:
    with _memory_lock:
        found = _memory.get(_key(artist_name))
    return found is not None and found[1] is _FAILED and _clock() - found[0] < FAILURE_TTL_SECONDS


def _remember(artist_name: str, value: object) -> None:
    with _memory_lock:
        if len(_memory) >= MEMORY_MAX_ENTRIES:
            # Drop the oldest half: cheap, and keeps memory bounded.
            for old in sorted(_memory, key=lambda k: _memory[k][0])[: MEMORY_MAX_ENTRIES // 2]:
                del _memory[old]
        _memory[_key(artist_name)] = (_clock(), value)


def artist_preview(artist_name: str) -> dict | None:
    """`search_artist_track`, remembered in memory only (MEMORY_TTL_SECONDS;
    a failure for FAILURE_TTL_SECONDS, during which it is not asked again
    and counts as a failure for the caller's recording_failures)."""
    known, track = peek_preview(artist_name)
    if known:
        return track
    if recently_failed(artist_name):
        note_failure("Deezer failed just now")
        return None
    with recording_failures() as failures:
        track = search_artist_track(artist_name)
    if failures:
        note_failure(failures[-1])  # visible to a caller's recording_failures too
        _remember(artist_name, _FAILED)
        return None
    _remember(artist_name, track)
    return track
