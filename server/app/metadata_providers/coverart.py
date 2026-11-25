import threading

import httpx

from app.config import settings
from app.metadata_providers._throttle import Throttle
from app.metadata_providers.result import (
    NOT_FOUND,
    Failed,
    Found,
    NotFound,
    ProviderResult,
    failed,
    retry_after_seconds,
)

BASE_URL = "https://coverartarchive.org"
_client = httpx.Client(follow_redirects=True)
_front_image_cache: dict[str, str | None] = {}
_cache_lock = threading.Lock()
# No pacing of its own (interval 0): it carries the pause after a 429/503.
_throttle = Throttle(0.0)


def _release(release_mbid: str, kind: str = "release") -> ProviderResult[dict]:
    """The archive's listing for a release (or, `kind` "release-group", the
    art of the group's chosen edition). A 404 (with an HTML body, not JSON)
    means no art is archived: a normal, common, definitive answer."""
    if not _throttle.wait():
        return failed("Cover Art Archive is busy - try again shortly")
    try:
        resp = _client.get(
            f"{BASE_URL}/{kind}/{release_mbid}",
            headers={"User-Agent": settings.user_agent},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure is a retry
        return failed(f"Cover Art Archive request failed: {type(exc).__name__}")
    if resp.status_code == 404:
        return NOT_FOUND
    if resp.status_code in (429, 503):
        return failed(
            f"Cover Art Archive HTTP {resp.status_code}", retry_after_seconds(resp), _throttle
        )
    if resp.status_code != 200:
        return failed(f"Cover Art Archive HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        return failed("Cover Art Archive returned an invalid response")
    return Found(data) if isinstance(data, dict) else failed("Cover Art Archive returned an invalid response")


def front_image(release_mbid: str, release_group_mbid: str | None = None) -> ProviderResult[str]:
    """The front cover image URL for a MusicBrainz release - or, when that
    edition has none archived, for its release group (another edition's
    cover). Answers (with or without a front image) are remembered for the
    process; failures are not."""
    found = _front(release_mbid, "release")
    if isinstance(found, NotFound) and release_group_mbid:
        return _front(release_group_mbid, "release-group")
    return found


def _front(release_mbid: str, kind: str) -> ProviderResult[str]:
    if not release_mbid:
        return NOT_FOUND
    with _cache_lock:
        if release_mbid in _front_image_cache:
            known = _front_image_cache[release_mbid]
            return Found(known) if known else NOT_FOUND
    result = _release(release_mbid, kind)
    if isinstance(result, Failed):
        return result
    front_image_url = None
    if isinstance(result, Found):
        for image in result.data.get("images", []):
            if isinstance(image, dict) and image.get("front"):
                front_image_url = image.get("image")
                break
    with _cache_lock:
        _front_image_cache[release_mbid] = front_image_url
    return Found(front_image_url) if front_image_url else NOT_FOUND


def get_front_image_url(release_mbid: str) -> str | None:
    """`front_image`'s URL, or None (no front image, or the request failed)."""
    result = front_image(release_mbid)
    return result.data if isinstance(result, Found) else None


def list_release_images(release_mbid: str) -> list[dict] | None:
    """Every archived image of a release: [{"url", "thumb", "types",
    "front"}]. [] when the release has none; None when the request failed."""
    if not release_mbid:
        return []
    result = _release(release_mbid)
    if isinstance(result, Failed):
        return None
    if not isinstance(result, Found):
        return []
    out = []
    for image in result.data.get("images") or []:
        if not isinstance(image, dict) or not image.get("image"):
            continue
        thumbs = image.get("thumbnails") or {}
        out.append(
            {
                "url": image["image"],
                "thumb": thumbs.get("500") or thumbs.get("large") or thumbs.get("250") or image["image"],
                "types": [str(t) for t in image.get("types") or []],
                "front": bool(image.get("front")),
            }
        )
    return out
