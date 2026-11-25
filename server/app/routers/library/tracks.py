"""Track lists and media lookup by id."""

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.access import accessible_root_ids
from app.artist_credits import annotate_credits
from app.custom_art import annotate_items
from app.deps import CurrentUser, DbSession
from app.library_cache import get_or_compute
from app.library_query import (
    DEFAULT_TRACK_DIRECTION,
    PLAYED_TRACK_SORTS,
    TrackSort,
    filter_tracks_query,
    track_release_date,
    track_sort_terms,
    year_of,
)
from app.models import (
    MediaItem,
)
from app.preferences import preference_overlay
from app.presenters import annotate_play_counts, apply_overlay, to_media_item_outs
from app.schemas import MediaItemOut

router = APIRouter()


@router.get("/media", response_model=list[MediaItemOut])
def get_media_by_ids(
    # The playback queue is windowed (app.playback.WINDOW_TARGET), so
    # the client only ever needs to resolve a few hundred ids at once
    # regardless of library size - this cap just needs headroom over that,
    # not over the whole library.
    db: DbSession,
    user: CurrentUser,
    ids: str = Query(max_length=20_000),
) -> list[MediaItemOut]:
    """Batch-fetch by id (comma-separated) - used to resolve a playback
    queue's ids into full media items without one request per id."""
    try:
        id_list = [int(x) for x in ids.split(",") if x][:2_000]
    except ValueError:
        return []
    items = db.scalars(
        select(MediaItem).where(
            MediaItem.id.in_(id_list),
            MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
        )
    ).all()
    by_id = {item.id: item for item in items}
    # Preserve the caller's order (and skip ids that don't resolve/aren't accessible).
    ordered = [by_id[i] for i in id_list if i in by_id]
    return to_media_item_outs(ordered, db, user)


@router.get("/tracks", response_model=list[MediaItemOut])
def list_tracks(
    db: DbSession,
    user: CurrentUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    genre: Annotated[str | None, Query(max_length=1000)] = None,
    tag: Annotated[str | None, Query(max_length=1000)] = None,
    artist: Annotated[str | None, Query(max_length=1000)] = None,
    artist_is_null: bool = False,
    album: Annotated[str | None, Query(max_length=1000)] = None,
    sort: TrackSort = TrackSort.artist,
    direction: str | None = Query(default=None, pattern="^(asc|desc)$"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> list[MediaItemOut]:
    """A page of tracks matching the filters, in `sort` order (`direction`
    defaults per sort: A-Z for artist/title/album, newest or most first for
    the rest). With neither given the order is artist, album, track
    number, id. See app.library_query.track_sort_terms
    for tie-breaks and where missing values go."""
    direction = direction or DEFAULT_TRACK_DIRECTION[sort]

    def compute() -> list[dict]:
        query = filter_tracks_query(
            db,
            user,
            {
                "q": q,
                "genre": genre,
                "tag": tag,
                "artist": artist,
                "artist_is_null": artist_is_null,
                "album": album,
            },
        )
        query, order = track_sort_terms(query, sort, direction, user.id)
        rows = db.execute(
            query.add_columns(track_release_date().label("release_year_date"))
            .order_by(*order)
            .offset(offset)
            .limit(limit)
        ).all()
        # is_favorite/is_disliked/excluded_by and play_count are per-user
        # and change on every heart tap or play, not just a scan or tag
        # edit, so they are deliberately left at their schema defaults in
        # the cached payload and filled in fresh for the requesting user
        # below, outside the cache, on every call.
        return [
            MediaItemOut.model_validate(item).model_copy(update={"year": year_of(release_date)}).model_dump()
            for item, release_date in rows
        ]

    key = (
        "tracks",
        tuple(sorted(accessible_root_ids(db, user))),
        q,
        genre,
        tag,
        artist,
        artist_is_null,
        album,
        sort.value,
        direction,
        limit,
        offset,
    )
    # A search is never stored: every keystroke would be its own entry. Nor
    # is a played sort: it is the requesting user's own history, changed by
    # every play (like the Albums tab's).
    cached = compute() if q or sort in PLAYED_TRACK_SORTS else get_or_compute(key, compute)
    overlay = preference_overlay(db, user, [d["id"] for d in cached])
    outs = [apply_overlay(MediaItemOut(**d), overlay) for d in cached]
    annotate_play_counts(db, user, outs)
    annotate_items(db, outs)
    annotate_credits(db, outs)
    return outs
