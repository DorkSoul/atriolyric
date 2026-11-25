"""ListenBrainz: similar artists, popularity (top recordings, listener
counts), artist tags, and listen submission ("scrobbling").

Most reads need no key; top recordings for an artist need a user token
(anonymous callers get a 401). ListenBrainz's data is CC0, so answers may
be stored (app.external_cache, artist_metadata_cache). Submitting listens
needs the user's own ListenBrainz user token (listenbrainz.org/settings),
sent as ``Authorization: Token <token>``.

ListenBrainz allows about 30 requests per 10 seconds per client and says
so in X-RateLimit-* headers; a 429 carries X-RateLimit-Reset-In.
"""

import logging
import re
import threading
import time

import httpx

from app.config import settings
from app.metadata_providers._throttle import Throttle
from app.metadata_providers.result import (
    DEFAULT_RATE_LIMIT_PAUSE,
    MAX_RETRY_AFTER,
    NOT_FOUND,
    Failed,
    Found,
    ProviderResult,
    failed,
)

API_URL = "https://api.listenbrainz.org"
LABS_URL = "https://labs.api.listenbrainz.org"
# A similarity algorithm ListenBrainz keeps live for both similar artists
# and similar recordings (its own similar-artists page uses it).
SIMILAR_ARTISTS_ALGORITHM = "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
# 30 requests per 10 s is the advertised window; 0.4 s keeps under it.
# Token checks and listen submissions share it, so no single user can
# spend the server's ListenBrainz allowance for everyone.
_throttle = Throttle(0.4)
# One connection pool for every ListenBrainz call (and one thing for tests
# to stub: tests/conftest.py keeps ListenBrainz offline).
_client = httpx.Client()
# ListenBrainz's metadata and popularity endpoints take MBID lists; keep
# each request's URL/body modest.
MBID_BATCH = 50
# MusicBrainz tag votes are small numbers (1-10 for most tags); a single
# vote is mostly one-off spellings and personal labels.
MIN_TAG_COUNT = 2
MAX_TAGS = 25


_MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def valid_mbids(value: str | None) -> list[str]:
    """The MusicBrainz IDs in a stored tag value, in order. File tags are
    kept as written: ID3v2.3 joins several with "/" (others use ";" or
    ","), and a damaged tag may hold anything. ListenBrainz refuses a
    request with a malformed ID, so only well-formed ones are ever sent."""
    if not value:
        return []
    return list(dict.fromkeys(_MBID_RE.findall(value.lower())))


def first_mbid(value: str | None) -> str | None:
    found = valid_mbids(value)
    return found[0] if found else None


_logger = logging.getLogger(__name__)
# A failed read is logged at most this often, so an outage shows in the log
# without a line per request.
FAILURE_LOG_INTERVAL = 300.0
_last_failure_log = [0.0]
_failure_log_lock = threading.Lock()


def _failed(reason: str, retry_after: float | None = None) -> Failed:
    with _failure_log_lock:
        now = time.monotonic()
        due = now - _last_failure_log[0] >= FAILURE_LOG_INTERVAL
        if due:
            _last_failure_log[0] = now
    if due:
        _logger.warning("ListenBrainz did not answer: %s", reason)
    return failed(reason, retry_after, _throttle if retry_after is not None else None)


def _headers(token: str | None = None) -> dict[str, str]:
    headers = {"User-Agent": settings.user_agent}
    if token:
        headers["Authorization"] = f"Token {token}"
    return headers


def _reset_in(response: httpx.Response) -> float | None:
    value = response.headers.get("x-ratelimit-reset-in") or response.headers.get("retry-after")
    try:
        return max(0.0, min(float(value), MAX_RETRY_AFTER)) if value else None
    except ValueError:
        return None


def _error_text(response: httpx.Response) -> str:
    """": <ListenBrainz's own error message>", shortened, or "" - so a
    refusal says why (a 401 names the missing token) in the log and in a
    stopped task's message."""
    try:
        message = response.json().get("error")
    except Exception:  # noqa: BLE001 - not JSON, or not an object: no message
        return ""
    if not isinstance(message, str) or not message.strip():
        return ""
    message = " ".join(message.split())
    return f": {message[:157]}..." if len(message) > 160 else f": {message}"


def _body_hint(response: httpx.Response) -> str:
    """" (<content type>: <start of the body>)" for a reply that is not the
    JSON asked for - a maintenance or gateway page - so the log says what
    came back."""
    kind = response.headers.get("content-type", "no content type").split(";")[0].strip()
    text = " ".join(response.text[:200].split())
    return f" ({kind}: {text[:80]})" if text else f" ({kind}, empty)"


def _request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: object = None,
    timeout: float = 10,
    bad_request_is_miss: bool = True,
    token: str | None = None,
) -> ProviderResult[object]:
    """One read call. A 400 means "ListenBrainz does not know this ID" for a
    single-ID look-up (a miss), but for a batch it would hide every other
    ID's answer behind one bad one, so batches (`bad_request_is_miss=False`)
    count it as a failure."""
    if not _throttle.wait():
        return failed("ListenBrainz is busy - try again shortly")
    try:
        response = _client.request(
            method, url, params=params, json=json, headers=_headers(token), timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure is a retry
        return _failed(f"ListenBrainz request failed: {type(exc).__name__}")
    if response.status_code == 429 or response.status_code == 503:
        return _failed(
            f"ListenBrainz HTTP {response.status_code}",
            _reset_in(response) or DEFAULT_RATE_LIMIT_PAUSE,
        )
    if response.headers.get("x-ratelimit-remaining") == "0":
        # The window is used up: wait it out before the next call.
        _throttle.pause(_reset_in(response) or DEFAULT_RATE_LIMIT_PAUSE)
    if response.status_code == 404 or (response.status_code == 400 and bad_request_is_miss):
        return NOT_FOUND
    if response.status_code != 200:
        return _failed(f"ListenBrainz HTTP {response.status_code}{_error_text(response)}")
    try:
        payload = response.json()
    except ValueError:
        return _failed(f"ListenBrainz returned an invalid response{_body_hint(response)}")
    if not payload:
        return NOT_FOUND
    return Found(payload)


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _tags(rows) -> list[tuple[str, int]]:
    out: dict[str, int] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("tag"))
        count = _int(row.get("count")) or 0
        if name and count >= MIN_TAG_COUNT:
            out[name] = max(out.get(name, 0), count)
    return sorted(out.items(), key=lambda pair: (-pair[1], pair[0]))[:MAX_TAGS]


def similar_artists(artist_mbid: str) -> list[dict] | None:
    """[{"name", "mbid", "match"}] best first, match scaled 0..1 against the
    best score. [] when ListenBrainz knows none; None when the request failed."""
    result = _request(
        "GET",
        f"{LABS_URL}/similar-artists/json",
        params={"artist_mbids": artist_mbid, "algorithm": SIMILAR_ARTISTS_ALGORITHM},
    )
    if isinstance(result, Failed):
        return None
    rows = result.data if isinstance(result, Found) and isinstance(result.data, list) else []
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("name"))
        score = row.get("score")
        if name and isinstance(score, int | float) and score > 0:
            parsed.append((name, _text(row.get("artist_mbid")), float(score)))
    if not parsed:
        return []
    best = max(score for _, _, score in parsed)
    parsed.sort(key=lambda row: -row[2])
    return [{"name": name, "mbid": mbid, "match": round(score / best, 4)} for name, mbid, score in parsed]


def similar_recordings(recording_mbid: str) -> list[dict] | None:
    """[{"artist", "title", "mbid", "match"}] best first, match scaled 0..1
    against the best score. [] when ListenBrainz knows none; None when the
    request failed."""
    result = _request(
        "GET",
        f"{LABS_URL}/similar-recordings/json",
        params={"recording_mbids": recording_mbid, "algorithm": SIMILAR_ARTISTS_ALGORITHM},
    )
    if isinstance(result, Failed):
        return None
    rows = result.data if isinstance(result, Found) and isinstance(result.data, list) else []
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _text(row.get("recording_name"))
        score = row.get("score")
        if title and isinstance(score, int | float) and score > 0:
            parsed.append((_text(row.get("artist_credit_name")), title, _text(row.get("recording_mbid")), float(score)))
    if not parsed:
        return []
    best = max(row[3] for row in parsed)
    parsed.sort(key=lambda row: -row[3])
    return [
        {"artist": artist, "title": title, "mbid": mbid, "match": round(score / best, 4)}
        for artist, title, mbid, score in parsed
    ]


def top_recordings_for_artist(artist_mbid: str, token: str, limit: int = 50) -> list[dict] | None:
    """[{"title", "mbid", "playcount", "listeners", "rank"}] most listened
    first. [] when ListenBrainz has none; None when the request failed.
    Needs a ListenBrainz user token: anonymous calls get 401."""
    result = _request(
        "GET", f"{API_URL}/1/popularity/top-recordings-for-artist/{artist_mbid}", token=token
    )
    if isinstance(result, Failed):
        return None
    rows = result.data if isinstance(result, Found) and isinstance(result.data, list) else []
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _text(row.get("recording_name"))
        if not title or title.casefold() in seen:
            continue  # the same song on several releases: the most played one counts
        seen.add(title.casefold())
        out.append(
            {
                "title": title,
                "mbid": _text(row.get("recording_mbid")),
                "playcount": _int(row.get("total_listen_count")),
                "listeners": _int(row.get("total_user_count")),
                "rank": len(out) + 1,
            }
        )
        if len(out) >= limit:
            break
    return out


def artist_tags(artist_mbids: list[str]) -> dict[str, list[tuple[str, int]]] | None:
    """{artist MBID: [(tag, count)]} for each artist ListenBrainz knows
    (MusicBrainz's community tags). None when a request failed."""
    out: dict[str, list[tuple[str, int]]] = {}
    for start in range(0, len(artist_mbids), MBID_BATCH):
        batch = artist_mbids[start : start + MBID_BATCH]
        result = _request(
            "GET",
            f"{API_URL}/1/metadata/artist/",
            params={"artist_mbids": ",".join(batch), "inc": "tag"},
            bad_request_is_miss=False,
        )
        if isinstance(result, Failed):
            return None
        rows = result.data if isinstance(result, Found) and isinstance(result.data, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            mbid = _text(row.get("artist_mbid")) or _text(row.get("mbid"))
            tag_block = row.get("tag")
            if mbid:
                out[mbid] = _tags(tag_block.get("artist") if isinstance(tag_block, dict) else None)
    return out


def _popularity(kind: str, mbids: list[str]) -> dict[str, dict] | None:
    """{MBID: {"playcount", "listeners"}} from POST /1/popularity/<kind>
    (kind: artist, recording, release-group). None when a request failed."""
    field = f"{kind.replace('-', '_')}_mbid"
    out: dict[str, dict] = {}
    for start in range(0, len(mbids), MBID_BATCH):
        result = _request(
            "POST",
            f"{API_URL}/1/popularity/{kind}",
            json={f"{field}s": mbids[start : start + MBID_BATCH]},
            bad_request_is_miss=False,
        )
        if isinstance(result, Failed):
            return None
        rows = result.data if isinstance(result, Found) and isinstance(result.data, list) else []
        for row in rows:
            if isinstance(row, dict) and (mbid := _text(row.get(field))):
                out[mbid] = {
                    "playcount": _int(row.get("total_listen_count")),
                    "listeners": _int(row.get("total_user_count")),
                }
    return out


def artist_popularity(artist_mbids: list[str]) -> dict[str, dict] | None:
    return _popularity("artist", artist_mbids)


def recording_popularity(recording_mbids: list[str]) -> dict[str, dict] | None:
    return _popularity("recording", recording_mbids)


def release_group_popularity(release_group_mbids: list[str]) -> dict[str, dict] | None:
    return _popularity("release-group", release_group_mbids)


# --- listen submission (needs the user's token) -------------------------------------


def validate_token(token: str) -> str | None | bool:
    """The ListenBrainz user name the token belongs to; False when
    ListenBrainz says the token is not valid; None when it couldn't be asked."""
    if not _throttle.wait():
        return None
    try:
        response = _client.get(f"{API_URL}/1/validate-token", headers=_headers(token), timeout=15)
    except Exception:  # noqa: BLE001 - any transport failure: not asked
        return None
    if response.status_code in (401, 403):
        return False
    if response.status_code == 429:
        _throttle.pause(_reset_in(response) or DEFAULT_RATE_LIMIT_PAUSE)
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if response.status_code != 200 or not isinstance(payload, dict):
        return None
    if not payload.get("valid"):
        return False
    return _text(payload.get("user_name")) or False


def submit_listen(
    token: str,
    *,
    listen_type: str,
    artist: str,
    title: str,
    album: str | None,
    listened_at: int | None,
    duration: float | None,
    recording_mbid: str | None = None,
    release_mbid: str | None = None,
    artist_mbids: list[str] | None = None,
) -> str:
    """One `single` (a finished play, with `listened_at`) or `playing_now`
    listen. Returns "accepted"; "unauthorized" (ListenBrainz no longer
    accepts the token: the user must link again); "rejected" (a payload it
    refuses - a retry cannot fix it); or "transient" (network error, rate
    limit, 5xx, or the shared pacing is backed up)."""
    additional: dict[str, object] = {"submission_client": "Atriolyric"}
    if duration:
        additional["duration_ms"] = round(duration * 1000)
    if recording_mbid:
        additional["recording_mbid"] = recording_mbid
    if release_mbid:
        additional["release_mbid"] = release_mbid
    if artist_mbids:
        additional["artist_mbids"] = artist_mbids
    metadata: dict[str, object] = {"artist_name": artist, "track_name": title, "additional_info": additional}
    if album:
        metadata["release_name"] = album
    listen: dict[str, object] = {"track_metadata": metadata}
    if listen_type == "single" and listened_at is not None:
        listen["listened_at"] = listened_at
    if not _throttle.wait():
        return "transient"
    try:
        response = _client.post(
            f"{API_URL}/1/submit-listens",
            json={"listen_type": listen_type, "payload": [listen]},
            headers=_headers(token),
            timeout=10,
        )
    except Exception:  # noqa: BLE001 - any transport failure is a retry
        return "transient"
    if response.status_code == 200:
        return "accepted"
    if response.status_code in (401, 403):
        return "unauthorized"
    if response.status_code == 429:
        _throttle.pause(_reset_in(response) or DEFAULT_RATE_LIMIT_PAUSE)
        return "transient"
    if response.status_code >= 500:
        return "transient"
    return "rejected"
