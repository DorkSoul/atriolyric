"""Explore: similar artists not in the library, with a Deezer preview each.

Only the artist names are stored (they come from ListenBrainz). Deezer's
answers - the preview, artwork and link - are looked up live and kept in
memory only (app.metadata_providers.deezer.artist_preview), as its terms
allow no local copy."""

import logging
import random
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import provider_queue
from app.access import accessible_root_ids
from app.discovery.common import _utcnow
from app.discovery.similar import (
    _online_allowed,
    _similar_artist_names,
    _top_played_artists,
)
from app.discovery.state import _get_or_create_state
from app.folder_exclusions import not_in_ignored_folder
from app.metadata_providers import deezer
from app.metadata_providers.result import recording_failures
from app.models import (
    CollectionPreference,
    MediaItem,
    MediaKind,
    User,
)
from app.preferences import fold_name

# The package's logger: every discovery module logs under one name.
logger = logging.getLogger("app.discovery")


MAX_EXPLORE_RESULTS = 13


# Deezer's public API is commonly documented at ~50 calls/5s per IP - well
# above what one click needs, but candidates are still capped and calls
# spaced out as considerate default behavior toward a free, keyless API.
MAX_EXPLORE_CANDIDATES = 60
EXPLORE_REQUEST_DELAY_SECONDS = 0.2
# Explore runs inline in a request (and in the daily job): it stops asking
# Deezer once this many seconds have gone, keeping whatever it found. A
# healthy run fills its 13 results in a few seconds.
EXPLORE_TIME_BUDGET_SECONDS = 30.0
# ...and after this many Deezer failures in a row (a timeout, a 5xx, its
# quota): Deezer is down, so the rest would fail too. Artists without a
# preview are not failures and never count.
EXPLORE_FAILURE_LIMIT = 3


def explore_new_artists(db: Session, user: User) -> dict:
    """The opposite of refresh_discovery_playlist: instead of resurfacing
    owned tracks by similar artists, this surfaces similar artists NOT
    already owned, each with a 30-second preview fetched from Deezer's free
    public API - so "you might like this" can actually be heard before
    deciding whether to go find it."""
    state = _get_or_create_state(db, user)
    top_artists = _top_played_artists(db, user)
    if not top_artists:
        state.explore_seed_artists_json = []
        state.explore_results_json = []
        state.explore_updated_at = _utcnow()
        db.commit()
        return {"seed_artists": [], "results": []}

    similar_names = _similar_artist_names(db, top_artists, _online_allowed(user))
    if not similar_names:
        state.explore_seed_artists_json = list(top_artists)
        state.explore_results_json = []
        state.explore_updated_at = _utcnow()
        db.commit()
        return {"seed_artists": top_artists, "results": []}

    accessible = accessible_root_ids(db, user)
    owned_artists = {
        name.lower()
        for name in db.scalars(
            select(MediaItem.artist)
            .where(
                MediaItem.kind == MediaKind.track,
                MediaItem.artist.is_not(None),
                MediaItem.library_root_id.in_(accessible),
                not_in_ignored_folder(),
            )
            .distinct()
        )
        if name is not None  # excluded in SQL; tells the type checker
    }
    # The ownership check above only looks at `artist`, so a disliked name
    # that's only ever an album artist would otherwise slip through.
    disliked_artists = {
        fold_name(name)
        for name in db.scalars(
            select(CollectionPreference.artist).where(
                CollectionPreference.user_id == user.id,
                CollectionPreference.kind == "artist",
                CollectionPreference.state == "disliked",
            )
        )
    }
    not_owned = sorted(
        n
        for n in similar_names
        if n.lower() not in owned_artists and fold_name(n) not in disliked_artists
    )

    # Prefer artists not shown in the previous batch, then fall back to the
    # full candidate pool if there aren't enough alternatives. Randomize each
    # group so refresh doesn't repeatedly search the same early subset.
    previous_results = state.explore_results_json
    previous_artists = {
        " ".join(str(track.get("artist") or "").split()).casefold()
        for track in previous_results
    }
    fresh_candidates = [
        name
        for name in not_owned
        if " ".join(name.split()).casefold() not in previous_artists
    ]
    fallback_candidates = [
        name
        for name in not_owned
        if " ".join(name.split()).casefold() in previous_artists
    ]
    random.shuffle(fresh_candidates)
    random.shuffle(fallback_candidates)
    candidate_names = fresh_candidates + fallback_candidates

    results: list[dict] = []
    result_artists: set[str] = set()
    result_previews: set[str] = set()
    # Up to 60 sequential Deezer calls run inline in the request: when
    # Deezer is down each one waits out its timeout (about five minutes in
    # all), so a time budget ends the batch with what it has. It also stops
    # after EXPLORE_FAILURE_LIMIT failures in a row:
    # search_artist_track answers None both for a failure and for "no
    # preview", so failures are told apart by the provider's recorded
    # Failed results - a run of artists without previews must not leave
    # the rail short.
    deadline = time.monotonic() + EXPLORE_TIME_BUDGET_SECONDS
    failures_in_a_row = 0
    for i, name in enumerate(candidate_names[:MAX_EXPLORE_CANDIDATES]):
        if len(results) >= MAX_EXPLORE_RESULTS:
            break
        if time.monotonic() >= deadline:
            logger.info(
                "Explore stopped at its time budget after %d of %d candidates",
                i, min(len(candidate_names), MAX_EXPLORE_CANDIDATES),
            )
            break
        if i > 0:
            time.sleep(EXPLORE_REQUEST_DELAY_SECONDS)
        with recording_failures() as failures:
            track = deezer.artist_preview(name)
        failures_in_a_row = failures_in_a_row + 1 if failures else 0
        if failures_in_a_row >= EXPLORE_FAILURE_LIMIT:
            logger.info(
                "Explore stopped after %d Deezer failures in a row (last: %s)",
                failures_in_a_row, failures[-1],
            )
            break
        if track:
            # The name that is kept is ListenBrainz's (Deezer's terms allow
            # no local copy); Deezer's own spelling is only shown live.
            track = {**track, "artist": name}
            artist_key = " ".join(str(track["artist"]).split()).casefold()
            preview_url = str(track.get("preview_url") or "")
            if artist_key in result_artists or (
                preview_url and preview_url in result_previews
            ):
                continue
            results.append(track)
            result_artists.add(artist_key)
            if preview_url:
                result_previews.add(preview_url)

    state.explore_seed_artists_json = list(top_artists)
    state.explore_results_json = [{"artist": track["artist"]} for track in results]
    state.explore_updated_at = _utcnow()
    db.commit()

    return {"seed_artists": top_artists, "results": results}


def _stored_names(stored: list[dict]) -> list[str]:
    return [name for row in stored if (name := str(row.get("artist") or "").strip())]


def explore_previews(stored: list[dict], *, user_id: int | None = None) -> tuple[list[dict], bool]:
    """(the stored Explore artists that have a Deezer preview in memory,
    whether some are still being looked up). Never asks Deezer itself: a
    page load must not wait on it. Artists not in memory (after a restart,
    or once their answer expired) are looked up by one background job,
    with the same time budget and failure limit as an Explore refresh; the
    client asks again while `pending`."""
    results: list[dict] = []
    missing: list[str] = []
    for name in _stored_names(stored):
        known, track = deezer.peek_preview(name)
        if known:
            if track:
                results.append({**track, "artist": name})
        elif not deezer.recently_failed(name):
            missing.append(name)
    if not missing:
        return results, False
    if _warming(missing):
        return results, True  # a job for these is queued or running
    queued = provider_queue.submit(
        f"explore-previews:{user_id}:{'|'.join(missing)}",
        lambda: warm_previews(missing),
        user_id=user_id,
    )
    if queued:
        with _warming_lock:
            _warming_names.update(missing)
    return results, queued


_warming_lock = threading.Lock()
_warming_names: set[str] = set()


def _warming(names: list[str]) -> bool:
    with _warming_lock:
        return any(name in _warming_names for name in names)


def warm_previews(names: list[str]) -> None:
    """Look Deezer previews up for `names` (into memory), stopping at the
    Explore time budget or after EXPLORE_FAILURE_LIMIT failures in a row."""
    try:
        deadline = time.monotonic() + EXPLORE_TIME_BUDGET_SECONDS
        failures_in_a_row = 0
        for name in names:
            if time.monotonic() >= deadline or failures_in_a_row >= EXPLORE_FAILURE_LIMIT:
                break
            with recording_failures() as failures:
                deezer.artist_preview(name)
            failures_in_a_row = failures_in_a_row + 1 if failures else 0
    finally:
        with _warming_lock:
            _warming_names.difference_update(names)
