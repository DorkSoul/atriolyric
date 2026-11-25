"""ListenBrainz data: similar recordings, top tracks, popularity and artist
tags - cached server-wide (app.external_cache) and matched back to the
library.

Most reads need no key, so they run for every user who has not turned off
"Use ListenBrainz to find similar music" (User.online_recommendations).
Most look-ups are by MusicBrainz ID: the track's recording MBID, the
artist's MBID (from tags, enrichment or a MusicBrainz name match).
ListenBrainz's data is CC0, so keeping it is fine.
"""

import logging
import threading
import time
from collections.abc import Collection, Iterable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.artist_credits import normalize_artist_name
from app.background import spawn
from app.db import SessionLocal
from app.external_cache import cached
from app.metadata_providers import listenbrainz
from app.metadata_providers._throttle import bulk
from app.models import Artist, MediaItem, MediaKind, TrackArtist, User
from app.owned_match import match_owned
from app.scrobbling import listenbrainz_token

logger = logging.getLogger(__name__)

PROVIDER = "listenbrainz"
SIMILAR_TTL = timedelta(days=30)
TOP_TRACKS_TTL = timedelta(days=14)
INFO_TTL = timedelta(days=30)
# How strongly a similar-recording match lifts an owned track in a weighted
# draw: weight = 1 + SIMILAR_TRACK_MULTIPLIER * match (match 0..1).
SIMILAR_TRACK_MULTIPLIER = 4.0
# A mix asks ListenBrainz live for at most this long; seeds not reached by
# then use what is cached and are looked up in the background for next time.
ONLINE_FETCH_BUDGET_SECONDS = 6.0


def online_allowed(user: User | None) -> bool:
    """Whether look-ups about what this user plays may go to ListenBrainz.
    Background work with no user (admin tasks) is always allowed."""
    return user is None or bool(user.online_recommendations)


# --- artist MBIDs --------------------------------------------------------------


def artist_mbid(db: Session, name: str, *, allow_fetch: bool = True) -> str | None:
    """The MusicBrainz ID of a library artist: the artist row's (from tags,
    enrichment or a hand fix), else one a track by that name carries, else -
    when `allow_fetch` - the best MusicBrainz name match
    (app.metadata_info.resolve_artist_mbid, which saves it)."""
    if not name:
        return None
    artist = db.execute(
        select(Artist).where(Artist.normalized_name == normalize_artist_name(name))
    ).scalar_one_or_none()
    if artist is not None and (found := listenbrainz.first_mbid(artist.mbid)):
        return found
    for from_track in db.execute(
        select(MediaItem.musicbrainz_artist_mbid)
        .where(MediaItem.artist == name, MediaItem.musicbrainz_artist_mbid.is_not(None))
        .limit(5)
    ).scalars():
        if found := listenbrainz.first_mbid(from_track):
            return found
    if artist is None or not allow_fetch:
        return None
    from app.metadata_info import (
        resolve_artist_mbid,  # metadata_info imports this module
    )

    try:
        return resolve_artist_mbid(db, artist)
    except Exception:  # noqa: BLE001 - a failed match is the same as none
        logger.warning("MusicBrainz artist match failed for %r", name, exc_info=True)
        return None


def _first_main_artist(db: Session, item: MediaItem) -> str | None:
    return db.execute(
        select(Artist.name)
        .join(TrackArtist, TrackArtist.artist_id == Artist.id)
        .where(TrackArtist.media_item_id == item.id, TrackArtist.role == "main")
        .order_by(TrackArtist.position)
        .limit(1)
    ).scalar()


# --- similar tracks --------------------------------------------------------


def similar_tracks(db: Session, item: MediaItem, *, allow_fetch: bool = True) -> list[dict]:
    """ListenBrainz's similar recordings for one owned track (cached per
    recording MBID); [] for a track without a recording MBID."""
    mbid = listenbrainz.first_mbid(item.musicbrainz_recording_mbid)
    if not mbid:
        return []
    return cached(
        db,
        PROVIDER,
        "similar-recordings",
        mbid,
        lambda: listenbrainz.similar_recordings(mbid),
        ttl=SIMILAR_TTL,
        allow_fetch=allow_fetch,
    ) or []


def similar_owned_weights(
    db: Session,
    accessible: Collection[int],
    seeds: Iterable[MediaItem],
    online: bool,
    *,
    allow_fetch: bool = True,
) -> dict[int, float]:
    """{owned media id: draw weight} for tracks ListenBrainz says are similar
    to any seed - the best match across seeds counts. Seeds themselves are
    left out. `online` False reads nothing at all (not even the cache): the
    user turned the feature off."""
    if not online:
        return {}
    seeds = list(seeds)
    seed_ids = {seed.id for seed in seeds}
    best: dict[int, float] = {}
    deadline = time.monotonic() + ONLINE_FETCH_BUDGET_SECONDS
    for seed in seeds:
        fetch_now = allow_fetch and time.monotonic() < deadline
        if allow_fetch and not fetch_now:
            prefetch_similar_in_background(seed.id)
        rows = similar_tracks(db, seed, allow_fetch=fetch_now)
        for row, media_id in zip(rows, match_owned(db, accessible, rows), strict=False):
            if media_id is None or media_id in seed_ids:
                continue
            match = max(0.0, min(1.0, float(row.get("match") or 0.0)))
            best[media_id] = max(best.get(media_id, 0.0), match)
    return {media_id: 1.0 + SIMILAR_TRACK_MULTIPLIER * match for media_id, match in best.items()}


_prefetching: set[int] = set()
_prefetch_lock = threading.Lock()


def prefetch_similar_in_background(media_item_id: int) -> None:
    """Warm the similar-track cache for a song that just started playing,
    so autoplay (cache-only) can use it when the queue runs out. Never
    blocks the caller; a song already being fetched is skipped."""
    with _prefetch_lock:
        if media_item_id in _prefetching:
            return
        _prefetching.add(media_item_id)

    def run():
        db = SessionLocal()
        try:
            item = db.get(MediaItem, media_item_id)
            if item is not None and item.kind == MediaKind.track and listenbrainz.first_mbid(item.musicbrainz_recording_mbid):
                with bulk():
                    similar_tracks(db, item)
        except Exception:  # a background warm-up must never surface
            logger.exception("Similar-track prefetch failed for media %s", media_item_id)
        finally:
            db.close()
            with _prefetch_lock:
                _prefetching.discard(media_item_id)

    spawn(f"listenbrainz-similar-{media_item_id}", run)


# --- similar artists (info dialog) ------------------------------------------------


def similar_artist_rows(
    db: Session, mbid: str, *, refresh: bool = False, allow_fetch: bool = True
) -> list[dict] | None:
    """ListenBrainz's similar artists for an artist MBID ([{"name", "mbid",
    "match"}], best first), cached by MBID - so a fixed match never serves
    the old artist's list. Discovery keeps its own per-name copy
    (artist_metadata_cache, app.discovery.similar)."""
    return cached(
        db,
        PROVIDER,
        "similar-artists",
        mbid,
        lambda: listenbrainz.similar_artists(mbid),
        ttl=SIMILAR_TTL,
        refresh=refresh,
        allow_fetch=allow_fetch,
    )


# --- artist top tracks -----------------------------------------------------------


def top_recordings(
    db: Session, mbid: str, token: str | None, *, refresh: bool = False, allow_fetch: bool = True
) -> list[dict] | None:
    """The artist's most listened recordings (cached); None when unknown and
    the request failed. ListenBrainz answers this only with a user token, so
    without one only what is cached is returned."""
    return cached(
        db,
        PROVIDER,
        "top-recordings",
        mbid,
        lambda: listenbrainz.top_recordings_for_artist(mbid, token) if token else None,
        ttl=TOP_TRACKS_TTL,
        refresh=refresh and token is not None,
        allow_fetch=allow_fetch and token is not None,
    )


def artist_top_tracks(
    db: Session,
    user: User,
    artist: str,
    *,
    limit: int = 20,
    refresh: bool = False,
) -> dict:
    """{"available": bool, "tracks": [{title, playcount, listeners, rank,
    mbid, media_item_id}]} - the most listened songs by `artist` on
    ListenBrainz, each matched to an owned track when there is one.
    `available` is False when the artist has no MusicBrainz ID to ask by,
    or nothing is cached and the user has no linked ListenBrainz token to
    ask with (ListenBrainz refuses anonymous calls here). For a user who
    turned ListenBrainz off only what is cached is shown."""
    mbid = artist_mbid(db, artist)
    if not mbid:
        return {"available": False, "tracks": []}
    token = listenbrainz_token(db, user)
    rows = top_recordings(db, mbid, token, refresh=refresh, allow_fetch=online_allowed(user))
    if rows is None:
        return {"available": token is not None, "tracks": []}
    rows = rows[:limit]
    accessible = accessible_root_ids(db, user)
    matches = match_owned(db, accessible, ({**row, "artist": artist} for row in rows))
    return {
        "available": True,
        "tracks": [{**row, "media_item_id": media_id} for row, media_id in zip(rows, matches, strict=False)],
    }


# --- popularity and tags (cached) --------------------------------------------------
# Each returns {} when ListenBrainz knows nothing (cached for the miss TTL)
# and None for a failed request, which `cached` never stores.


def _one(result: dict | None, key: str) -> dict | None:
    if result is None:
        return None
    return result.get(key) or {}


def artist_popularity(db: Session, mbid: str, *, refresh: bool = False, allow_fetch: bool = True) -> dict | None:
    return cached(
        db,
        PROVIDER,
        "artist-popularity",
        mbid,
        lambda: _one(listenbrainz.artist_popularity([mbid]), mbid),
        ttl=INFO_TTL,
        refresh=refresh,
        allow_fetch=allow_fetch,
    )


def recording_popularity(db: Session, mbid: str, *, refresh: bool = False, allow_fetch: bool = True) -> dict | None:
    return cached(
        db,
        PROVIDER,
        "recording-popularity",
        mbid,
        lambda: _one(listenbrainz.recording_popularity([mbid]), mbid),
        ttl=INFO_TTL,
        refresh=refresh,
        allow_fetch=allow_fetch,
    )


def release_group_popularity(db: Session, mbid: str, *, refresh: bool = False, allow_fetch: bool = True) -> dict | None:
    return cached(
        db,
        PROVIDER,
        "release-group-popularity",
        mbid,
        lambda: _one(listenbrainz.release_group_popularity([mbid]), mbid),
        ttl=INFO_TTL,
        refresh=refresh,
        allow_fetch=allow_fetch,
    )


def listenbrainz_url(kind: str, mbid: str | None) -> str | None:
    """The ListenBrainz page for an artist, album or song."""
    if not mbid:
        return None
    return f"https://listenbrainz.org/{kind}/{mbid}/"
