import logging
from dataclasses import dataclass

import httpx

from app.config import settings
from app.metadata_providers._throttle import Throttle
from app.metadata_providers.result import (
    DEFAULT_RATE_LIMIT_PAUSE,
    note_failure,
    retry_after_seconds,
)

BASE_URL = "https://musicbrainz.org/ws/2"
_throttle = Throttle(min_interval=1.0)  # MusicBrainz's documented rate limit

# Reuse TCP/TLS connections across the many requests made during a metadata
# scan. The throttle still enforces MusicBrainz's request-rate limit.
# Redirects are followed: MusicBrainz answers a merged entity's old MBID
# with 301 to the one it was merged into, and an unfollowed redirect would
# read as a failure.
_client = httpx.Client(follow_redirects=True)
_logger = logging.getLogger(__name__)
_MAX_ERROR_BODY_LENGTH = 500


@dataclass(frozen=True)
class LookupResult:
    """A provider result that distinguishes a catalogue miss from a retry -
    MusicBrainz's form of metadata_providers.result: value = Found, neither
    = NotFound, error = Failed."""

    value: dict | None = None
    error: str | None = None


def _failure(error: str) -> LookupResult:
    note_failure(error)
    return LookupResult(error=error)

# Preserve the rich recording payload rather than making a second, lossy
# application-specific representation of MusicBrainz. Tags and genres are
# also extracted into queryable tables by app.enrichment.
RECORDING_INCLUDES = "+".join(
    (
        "artist-credits",
        "genres",
        "tags",
        "annotation",
        "ratings",
        "isrcs",
        "releases",
        "release-groups",
        "artist-rels",
        "release-rels",
        "release-group-rels",
        "work-rels",
        "work-level-rels",
        "url-rels",
    )
)


def _request_json(
    path: str,
    params: dict[str, str],
    timeout: float,
) -> LookupResult:
    if not _throttle.wait():
        _logger.warning("MusicBrainz is backed up, not queueing path=%s", path)
        return _failure("MusicBrainz is busy - try again shortly")
    try:
        resp = _client.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"User-Agent": settings.user_agent},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return LookupResult()
        detail = " ".join(exc.response.text.split())[:_MAX_ERROR_BODY_LENGTH]
        error = f"MusicBrainz HTTP {exc.response.status_code}"
        if detail:
            error = f"{error}: {detail}"
        if exc.response.status_code in (429, 503):
            # MusicBrainz answers 503 when a client goes over its rate; wait
            # as long as it asks before the next request from anyone here.
            _throttle.pause(retry_after_seconds(exc.response) or DEFAULT_RATE_LIMIT_PAUSE)
        _logger.warning(
            "MusicBrainz request failed: status=%s path=%s detail=%s",
            exc.response.status_code,
            exc.request.url.path,
            detail or "<empty response body>",
        )
        return _failure(error)
    except Exception as exc:  # noqa: BLE001 - retain a retryable failure reason
        _logger.warning(
            "MusicBrainz request failed: path=%s error=%s",
            path,
            type(exc).__name__,
        )
        return _failure(f"MusicBrainz request failed: {type(exc).__name__}")

    if not isinstance(data, dict):
        _logger.warning("MusicBrainz returned an invalid response: path=%s", path)
        return _failure("MusicBrainz returned an invalid response")
    if data.get("error"):
        # MusicBrainz sometimes answers 200 with an error body (e.g. "server
        # is busy") rather than a non-2xx status.
        _logger.warning(
            "MusicBrainz response error: path=%s detail=%s", path, data["error"]
        )
        return _failure(f"MusicBrainz: {data['error']}")
    return LookupResult(value=data)


def _recording_summary(recording: dict | None) -> dict | None:
    if not recording or not recording.get("id"):
        return None
    artist_credit = (recording.get("artist-credit") or [{}])[0]
    releases = recording.get("releases") or []
    release = releases[0] if releases else {}
    return {
        "recording_mbid": recording.get("id"),
        "title": recording.get("title"),
        "artist": artist_credit.get("name"),
        "artist_mbid": (artist_credit.get("artist") or {}).get("id"),
        "album": release.get("title"),
        "release_mbid": release.get("id"),
        "length_seconds": (recording.get("length") or 0) / 1000 or None,
    }


def _escape_query_term(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def search_recording_result(
    artist: str, title: str
) -> LookupResult:
    """Search by the supplied artist/title, retaining provider failures."""
    if not artist or not title:
        return LookupResult()

    query = (
        f'artist:"{_escape_query_term(artist)}" '
        f'AND recording:"{_escape_query_term(title)}"'
    )
    response = _request_json(
        "/recording", {"query": query, "fmt": "json", "limit": "1"}, 10,
    )
    if response.error:
        return response
    recordings = (response.value or {}).get("recordings") or []
    return LookupResult(value=_recording_summary(recordings[0] if recordings else None))


def lookup_recording_by_isrc(
    isrc: str | None
) -> LookupResult:
    """Resolve an embedded ISRC to its exact MusicBrainz recording."""
    normalized = "".join(char for char in (isrc or "").upper() if char.isalnum())
    if not normalized:
        return LookupResult()
    response = _request_json(
        f"/isrc/{normalized}", {"fmt": "json"}, 10,
    )
    if response.error:
        return response
    recordings = (response.value or {}).get("recordings") or []
    return LookupResult(value=_recording_summary(recordings[0] if recordings else None))


def fetch_recording_result(
    recording_mbid: str
) -> LookupResult:
    """Fetch detailed metadata while retaining whether a failure is retryable."""
    if not recording_mbid:
        return LookupResult()
    response = _request_json(
        f"/recording/{recording_mbid}",
        {"fmt": "json", "inc": RECORDING_INCLUDES},
        20,
    )
    if response.error:
        return response
    detail = response.value
    return LookupResult(value=detail if detail and detail.get("id") else None)


# --- artists, release groups and releases -----------------------------------
# Used for artist info, the album edition picker and the want list's
# discography. Each call is one throttled request except browse_release_groups,
# which pages through an artist's whole discography.

ARTIST_INCLUDES = "+".join(
    ("url-rels", "artist-rels", "tags", "genres", "aliases", "annotation", "ratings")
)
RELEASE_INCLUDES = "+".join(
    ("recordings", "labels", "artist-credits", "release-groups", "isrcs", "media")
)
RELEASE_GROUP_INCLUDES = "+".join(
    ("artists", "releases", "media", "tags", "genres", "url-rels", "annotation")
)
_BROWSE_PAGE = 100
# Plenty for any real discography; bounds a runaway loop on bad paging data.
_BROWSE_MAX_PAGES = 30


def lookup_artist(artist_mbid: str) -> LookupResult:
    if not artist_mbid:
        return LookupResult()
    return _request_json(
        f"/artist/{artist_mbid}", {"fmt": "json", "inc": ARTIST_INCLUDES}, 20
    )


_LUCENE_SPECIAL = set('+-&|!(){}[]^"~*?:\\/')


def _lucene_words(value: str) -> str:
    """Free text for a Lucene field query: every special character escaped,
    so "AC/DC" or "Sigur Rós (live)" search as typed."""
    return "".join(f"\\{ch}" if ch in _LUCENE_SPECIAL else ch for ch in value.strip())


def search_artists(name: str, limit: int = 10) -> LookupResult:
    """{"artists": [...]} best matches first (each has a `score`)."""
    if not name.strip():
        return LookupResult(value={"artists": []})
    return _request_json(
        "/artist",
        {"query": f"artist:({_lucene_words(name)})", "fmt": "json", "limit": str(limit)},
        15,
    )


def search_release_groups(query: str, limit: int = 10) -> LookupResult:
    if not query.strip():
        return LookupResult(value={"release-groups": []})
    return _request_json(
        "/release-group",
        {"query": f"releasegroup:({_lucene_words(query)})", "fmt": "json", "limit": str(limit)},
        15,
    )


def search_recordings(query: str, limit: int = 10) -> LookupResult:
    if not query.strip():
        return LookupResult(value={"recordings": []})
    return _request_json(
        "/recording",
        {"query": f"recording:({_lucene_words(query)})", "fmt": "json", "limit": str(limit)},
        15,
    )


def browse_release_groups(artist_mbid: str) -> LookupResult:
    """Every release group the artist is credited on, all types, as
    {"release-groups": [...]}. A failed page fails the whole browse, so a
    half discography is never cached as the full one."""
    if not artist_mbid:
        return LookupResult()
    groups: list[dict] = []
    offset = 0
    for _page in range(_BROWSE_MAX_PAGES):
        response = _request_json(
            "/release-group",
            {
                "artist": artist_mbid,
                "fmt": "json",
                "limit": str(_BROWSE_PAGE),
                "offset": str(offset),
            },
            20,
        )
        if response.error:
            return response
        page = response.value or {}
        rows = [row for row in page.get("release-groups") or [] if isinstance(row, dict)]
        groups.extend(rows)
        total = page.get("release-group-count") or 0
        offset += len(rows)
        if not rows or offset >= total:
            break
    return LookupResult(value={"release-groups": groups})


def lookup_release_group(release_group_mbid: str) -> LookupResult:
    if not release_group_mbid:
        return LookupResult()
    return _request_json(
        f"/release-group/{release_group_mbid}",
        {"fmt": "json", "inc": RELEASE_GROUP_INCLUDES},
        20,
    )


def lookup_release(release_mbid: str) -> LookupResult:
    if not release_mbid:
        return LookupResult()
    return _request_json(
        f"/release/{release_mbid}", {"fmt": "json", "inc": RELEASE_INCLUDES}, 20
    )
