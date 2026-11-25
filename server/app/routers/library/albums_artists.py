"""Album and artist lists, including the full-list (full=1) responses."""

import hashlib
import logging
import secrets
from enum import Enum
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import StringConstraints
from sqlalchemy import case, func, or_, select

from app.access import accessible_root_ids
from app.artist_credits import (
    ROLE_ALBUM_ARTIST,
    ROLE_FEATURED,
    ROLE_MAIN,
    album_owner_expr,
    artist_link_filter,
    normalize_artist_name,
)
from app.custom_art import (
    annotate_albums,
    annotate_artists,
)
from app.deps import CurrentUser, DbSession
from app.library_cache import get_or_compute, library_version
from app.library_query import representative_media_item_id
from app.metadata_taxonomy import genre_filter
from app.models import (
    Album,
    Artist,
    MediaItem,
    MediaKind,
    PlayHistory,
    TrackArtist,
)
from app.presenters import album_dicts
from app.schemas import (
    AlbumOut,
    ArtistOut,
)

router = APIRouter()


# Safety cap for full-list (full=1) albums/artists requests: bounds the
# memory of one cached full-list entry no matter how large the library is.
# It has to sit well above any real library - the client treats a full list
# as complete, so anything past the cap is simply unreachable. 32k albums
# is ~3.9MB of JSON (~0.6MB gzipped), so 100k still keeps one entry to tens
# of MB at worst.
FULL_LIST_SAFETY_CAP = 100_000

# The package's logger: every library router module logs under one name.
logger = logging.getLogger("app.routers.library")

# These lists can be expensive to aggregate for a large library. They are
# user-scoped, so they must never be cached by a shared proxy, but browsers
# may safely reuse them briefly as users move between browse tabs.
BROWSE_CACHE_CONTROL = "private, max-age=60, stale-while-revalidate=120"


def _set_browse_cache_headers(response: Response | None) -> None:
    # Router tests also call these functions directly, without FastAPI's
    # injected response object. Such calls only exercise query logic.
    if response is None:
        return
    response.headers["Cache-Control"] = BROWSE_CACHE_CONTROL
    # A browser may retain private responses across sign-outs. Key its cache
    # entry by the session cookie so two accounts sharing a browser cannot
    # receive each other's accessible-library lists.
    response.headers["Vary"] = "Cookie"


# Library versions restart at 0 with the process, so an ETag also names the
# process it came from.
_ETAG_EPOCH = secrets.token_hex(8)


def _full_list_etag(cache_key: tuple, request: Request | None, response: Response | None) -> Response | None:
    """Tag a full album/artist list with a weak ETag and answer 304 when the
    browser already has it - a full list is megabytes of JSON for a large
    library. The tag covers everything the response
    depends on: the library version (bumped by scans, tag edits, enrichment,
    deletes and custom art changes alike), the caller's accessible roots and
    the query, all in the cache key. Returns the 304, or None to carry on."""
    digest = hashlib.sha256(repr((_ETAG_EPOCH, library_version(), cache_key)).encode()).hexdigest()
    etag = f'W/"{digest[:32]}"'
    if response is not None:
        response.headers["ETag"] = etag
    sent = request.headers.get("if-none-match", "") if request is not None else ""
    if etag in (tag.strip() for tag in sent.split(",")):
        not_modified = Response(status_code=304)
        not_modified.headers["ETag"] = etag
        _set_browse_cache_headers(not_modified)
        return not_modified
    return None


class BrowseSort(str, Enum):  # noqa: UP042 - StrEnum would change str()/format() output of the members
    alphabetical = "alphabetical"
    date_added = "date_added"
    last_played = "last_played"
    most_played = "most_played"
    date_published = "date_published"


def _played_order(sort: BrowseSort, direction: str, plays, last_played, names: list) -> list:
    """ORDER BY terms for the "most played" / "last played" sorts, with the
    same meaning as the Songs sorts (library_query.track_sort_terms): `desc`
    is most played / most recently played first, `asc` the least / oldest
    first, and something never played by this user goes last either way.
    The names break ties in ascending order in both directions, so a page
    boundary never repeats or skips an item."""
    key = plays if sort == BrowseSort.most_played else last_played
    return [key.is_(None), key.desc() if direction == "desc" else key.asc(), *(name.asc() for name in names)]


def _album_base_filters(roots: set[int]) -> list:
    """What every album listing starts from: accessible tracks with an album."""
    return [
        MediaItem.library_root_id.in_(roots),
        MediaItem.kind == MediaKind.track,
        MediaItem.album.is_not(None),
    ]


def _album_group_query(filters: list) -> tuple[Any, Any]:
    """The grouped album rows (one per owner and title) for `filters`, and
    the original-release-date expression the date sorts order by. Shared by
    /albums and /album-summaries so the two can never disagree."""
    owner = album_owner_expr()
    # Albums are (owner, title): a compilation with an album artist is one
    # tile however many track artists it has, and a collaboration shows
    # under each of its album artists with the same owner string ("A & B"),
    # which is also what opening it filters on (app.library_query).
    # Albums date by their original release (the MusicBrainz release
    # group's first release), falling back to the files' own date tags.
    original_date = func.coalesce(
        func.max(Album.first_release_date), func.max(MediaItem.release_date)
    )
    query = (
        select(
            owner.label("artist"),
            MediaItem.album,
            func.count(MediaItem.id).label("track_count"),
            representative_media_item_id(),
            func.min(MediaItem.created_at).label("date_added"),
            original_date.label("release_date"),
            func.max(Album.primary_type).label("primary_type"),
            func.max(Album.secondary_types_json).label("secondary_types_json"),
            func.max(Album.release_group_mbid).label("release_group_mbid"),
        )
        .outerjoin(Album, Album.id == MediaItem.album_id)
        .where(*filters)
        .group_by(owner, MediaItem.album)
    )
    return query, original_date


@router.get("/albums", response_model=list[AlbumOut])
def list_albums(
    db: DbSession,
    user: CurrentUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    genre: Annotated[str | None, Query(max_length=1000)] = None,
    artist: Annotated[str | None, Query(max_length=1000)] = None,
    appears_on: bool = False,
    sort: BrowseSort = BrowseSort.alphabetical,
    direction: str | None = Query(default=None, pattern="^(asc|desc)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    full: bool = Query(default=False),
    response: Response = None,  # pyright: ignore[reportArgumentType] - FastAPI injects the real Response
    request: Request = None,  # pyright: ignore[reportArgumentType] - FastAPI injects the real Request
) -> Any:
    _set_browse_cache_headers(response)
    direction = direction or (
        "desc"
        if sort in (BrowseSort.date_added, BrowseSort.date_published, BrowseSort.most_played, BrowseSort.last_played)
        else "asc"
    )
    # full=1 opts into the whole list at once (for the client's A-Z jump
    # bar): pagination params are ignored and a single shared cache entry
    # serves every full-list request for the same filters. Default
    # (full=False) behavior is per-page caching keyed by limit/offset.
    effective_limit = FULL_LIST_SAFETY_CAP if full else limit
    effective_offset = 0 if full else offset
    # Aggregated in SQL rather than loading every track row and grouping in
    # Python - fine at a few thousand tracks, but a real cost once a library
    # reaches the tens/hundreds of thousands. representative_media_item_id
    # picks the lowest id in the group that is not known to lack art
    # (library_query) rather than specifically the lowest track_number -
    # visually equivalent for a real album (one shared cover), and worth the
    # difference for not needing a second per-group query or a window
    # function to preserve exactly.
    roots = accessible_root_ids(db, user)
    owner = album_owner_expr()
    filters = _album_base_filters(roots)
    if q:
        like = f"%{q}%"
        filters.append(or_(MediaItem.album.ilike(like), owner.ilike(like)))
    if genre:
        filters.append(genre_filter(genre))
    if artist and appears_on:
        # Albums someone else owns that the artist is on - a guest spot, or
        # a track on a compilation (app.artist_credits roles).
        filters.append(
            artist_link_filter(artist, (ROLE_MAIN, ROLE_FEATURED))
            & ~artist_link_filter(artist, (ROLE_ALBUM_ARTIST,))
            & (func.coalesce(owner, "") != artist)
        )
    elif artist:
        # The artist's own albums: they are (one of) its album artists. The
        # artists list counts album_count the same way.
        filters.append(artist_link_filter(artist, (ROLE_ALBUM_ARTIST,)) | (owner == artist))

    artist_expr = owner
    query, original_date = _album_group_query(filters)

    if sort in (BrowseSort.last_played, BrowseSort.most_played):
        # Per-user PlayHistory changes on every play, so this branch is
        # never cached - it always reflects the very latest play. Ordered
        # and paged in SQL.
        stats = (
            select(
                owner.label("artist"),
                MediaItem.album.label("album"),
                func.count(PlayHistory.id).label("plays"),
                func.max(PlayHistory.played_at).label("last_played"),
            )
            .join(PlayHistory, PlayHistory.media_item_id == MediaItem.id)
            .where(PlayHistory.user_id == user.id, MediaItem.kind == MediaKind.track)
            .group_by(owner, MediaItem.album)
            .subquery()
        )
        albums = query.subquery()
        rows = db.execute(
            select(albums)
            .outerjoin(
                stats,
                albums.c.artist.is_not_distinct_from(stats.c.artist) & (albums.c.album == stats.c.album),
            )
            .order_by(
                *_played_order(
                    sort,
                    direction,
                    stats.c.plays,
                    stats.c.last_played,
                    [func.atrio_lower(func.coalesce(albums.c.artist, "")), func.atrio_lower(albums.c.album)],
                ),
                # Ties keep the albums' grouped order.
                albums.c.artist,
                albums.c.album,
            )
            .offset(effective_offset)
            .limit(effective_limit)
        ).all()
        album_outs = [AlbumOut(**d) for d in album_dicts(db, rows)]
        annotate_albums(db, album_outs)
        return album_outs

    def compute() -> list[dict]:
        if sort == BrowseSort.date_added:
            date_added = func.min(MediaItem.created_at)
            order = date_added.desc() if direction == "desc" else date_added.asc()
            rows = db.execute(query.order_by(order, artist_expr, MediaItem.album).offset(effective_offset).limit(effective_limit)).all()
        elif sort == BrowseSort.date_published:
            # Two stable passes rather than one composite key: sort() is
            # guaranteed stable even with reverse=True (ties keep their
            # pre-sort relative order, they don't get reversed too), so
            # sorting alphabetically first and then by release_date descending
            # gives "newest first, alphabetical among ties or missing dates"
            # without needing to invert a string key by hand.
            release_date = original_date
            order = release_date.desc().nullslast() if direction == "desc" else release_date.asc().nullsfirst()
            rows = db.execute(query.order_by(order, func.lower(func.coalesce(artist_expr, "")), func.lower(MediaItem.album), artist_expr, MediaItem.album).offset(effective_offset).limit(effective_limit)).all()
        else:
            # Album title first, artist only as the tie-breaker for albums
            # that share a title - the client's A-Z jump bar indexes each
            # album by its title's first letter, so the list must be in
            # title order for those letters to land in the right place.
            album_order = func.lower(MediaItem.album).desc() if direction == "desc" else func.lower(MediaItem.album).asc()
            artist_order = func.lower(func.coalesce(artist_expr, "")).desc() if direction == "desc" else func.lower(func.coalesce(artist_expr, "")).asc()
            # The raw group keys last: "ABBA"/"Abba" fold to the same tie.
            rows = db.execute(query.order_by(album_order, artist_order, MediaItem.album, artist_expr).offset(effective_offset).limit(effective_limit)).all()

        return album_dicts(db, rows)

    if full:
        # No limit/offset in the key: one entry per filter combination,
        # regardless of what pagination params the caller sent (they are
        # ignored when full=1 anyway).
        cache_key = ("albums_full", tuple(sorted(roots)), q, genre, artist, appears_on, sort, direction)
        if not q and (not_modified := _full_list_etag(cache_key, request, response)) is not None:
            return not_modified
    else:
        cache_key = ("albums", tuple(sorted(roots)), q, genre, artist, appears_on, sort, direction, limit, offset)
    # A search is never stored: every keystroke would be its own entry.
    cached = compute() if q else get_or_compute(cache_key, compute)
    if full and len(cached) >= FULL_LIST_SAFETY_CAP:
        # One log line per compute where the cap binds, so operators can see
        # whether the cap is still large enough for real libraries.
        logger.info(
            "Full-list albums response hit the %d-item safety cap for roots %s",
            FULL_LIST_SAFETY_CAP,
            sorted(roots),
        )
    album_outs = [AlbumOut(**d) for d in cached]
    annotate_albums(db, album_outs)
    return album_outs


@router.get("/artists", response_model=list[ArtistOut])
def list_artists(
    db: DbSession,
    user: CurrentUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    genre: Annotated[str | None, Query(max_length=1000)] = None,
    sort: BrowseSort = BrowseSort.alphabetical,
    direction: str | None = Query(default=None, pattern="^(asc|desc)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    full: bool = Query(default=False),
    response: Response = None,  # pyright: ignore[reportArgumentType] - FastAPI injects the real Response
    request: Request = None,  # pyright: ignore[reportArgumentType] - FastAPI injects the real Request
) -> Any:
    _set_browse_cache_headers(response)
    direction = direction or (
        "desc"
        if sort in (BrowseSort.date_added, BrowseSort.date_published, BrowseSort.most_played, BrowseSort.last_played)
        else "asc"
    )
    # Same full=1 semantics as /albums above: pagination ignored, one shared
    # cache entry per filter combination, default paging otherwise.
    effective_limit = FULL_LIST_SAFETY_CAP if full else limit
    effective_offset = 0 if full else offset
    # One entry per linked artist (app.artist_credits), whatever the role -
    # a featured-only guest is listed too and opens on its "Appears on".
    # album_count counts only albums the artist owns (album-artist links),
    # the same rule the artist page's album grid uses.
    roots = accessible_root_ids(db, user)
    filters = [
        MediaItem.library_root_id.in_(roots),
        MediaItem.kind == MediaKind.track,
    ]
    if q:
        filters.append(Artist.name.ilike(f"%{q}%"))
    if genre:
        filters.append(genre_filter(genre))
    owned_album = case((TrackArtist.role == ROLE_ALBUM_ARTIST, MediaItem.album), else_=None)
    grouped = (
        select(
            Artist.name.label("artist"),
            func.count(func.distinct(MediaItem.id)).label("track_count"),
            func.count(func.distinct(owned_album)).label("album_count"),
            representative_media_item_id(),
            func.min(MediaItem.created_at).label("date_added"),
            func.max(MediaItem.release_date).label("release_date"),
        )
        .select_from(TrackArtist)
        .join(Artist, Artist.id == TrackArtist.artist_id)
        .join(MediaItem, MediaItem.id == TrackArtist.media_item_id)
        .where(*filters)
        .group_by(Artist.id)
    )
    date_added_col = func.min(MediaItem.created_at)
    release_date_col = func.max(MediaItem.release_date)
    name_order = func.lower(Artist.name)

    if sort in (BrowseSort.last_played, BrowseSort.most_played):
        # Per-user PlayHistory changes on every play, so this branch is
        # never cached - it always reflects the very latest play. Ordered
        # and paged in SQL, like the albums above.
        played = (
            select(
                TrackArtist.artist_id.label("artist_id"),
                func.count(func.distinct(PlayHistory.id)).label("plays"),
                func.max(PlayHistory.played_at).label("last_played"),
            )
            .join(PlayHistory, PlayHistory.media_item_id == TrackArtist.media_item_id)
            .where(PlayHistory.user_id == user.id)
            .group_by(TrackArtist.artist_id)
            .subquery()
        )
        artists = grouped.add_columns(Artist.id.label("artist_id")).subquery()
        rows = db.execute(
            select(artists)
            .outerjoin(played, played.c.artist_id == artists.c.artist_id)
            .order_by(
                *_played_order(
                    sort, direction, played.c.plays, played.c.last_played, [func.atrio_lower(artists.c.artist)]
                ),
                # Ties keep the grouped (artist id) order.
                artists.c.artist_id,
            )
            .offset(effective_offset)
            .limit(effective_limit)
        ).all()
        artist_outs = [
            ArtistOut(
                artist=row.artist,
                track_count=row.track_count,
                album_count=row.album_count,
                representative_media_item_id=row.representative_media_item_id,
            )
            for row in rows
        ]
        annotate_artists(db, artist_outs)
        return artist_outs

    def compute() -> list[dict]:
        if sort == BrowseSort.date_added:
            order = date_added_col.desc() if direction == "desc" else date_added_col.asc()
            rows = db.execute(grouped.order_by(order, name_order, Artist.id).offset(effective_offset).limit(effective_limit)).all()
        elif sort == BrowseSort.date_published:
            order = release_date_col.desc().nullslast() if direction == "desc" else release_date_col.asc().nullsfirst()
            rows = db.execute(grouped.order_by(order, name_order, Artist.id).offset(effective_offset).limit(effective_limit)).all()
        else:
            order = name_order.desc() if direction == "desc" else name_order.asc()
            rows = db.execute(grouped.order_by(order, Artist.id).offset(effective_offset).limit(effective_limit)).all()

        return [
            ArtistOut(
                artist=row.artist,
                track_count=row.track_count,
                album_count=row.album_count,
                representative_media_item_id=row.representative_media_item_id,
            ).model_dump()
            for row in rows
        ]

    if full:
        cache_key = ("artists_full", tuple(sorted(roots)), q, genre, sort, direction)
        if not q and (not_modified := _full_list_etag(cache_key, request, response)) is not None:
            return not_modified
    else:
        cache_key = ("artists", tuple(sorted(roots)), q, genre, sort, direction, limit, offset)
    # A search is never stored: every keystroke would be its own entry.
    cached = compute() if q else get_or_compute(cache_key, compute)
    if full and len(cached) >= FULL_LIST_SAFETY_CAP:
        logger.info(
            "Full-list artists response hit the %d-item safety cap for roots %s",
            FULL_LIST_SAFETY_CAP,
            sorted(roots),
        )
    artist_outs = [ArtistOut(**d) for d in cached]
    annotate_artists(db, artist_outs)
    return artist_outs


@router.get("/artist-summaries", response_model=list[ArtistOut])
def artist_summaries(
    db: DbSession,
    user: CurrentUser,
    names: list[Annotated[str, StringConstraints(max_length=1000)]] = Query(default=[], max_length=50),
) -> list[ArtistOut]:
    """The Artists-tab entries for a few names, in the order asked - for the
    multi-artist picker, which needs each artist's art and counts. Names
    with no accessible tracks are left out."""
    keys = {normalize_artist_name(name): name for name in names if name.strip()}
    if not keys:
        return []
    owned_album = case((TrackArtist.role == ROLE_ALBUM_ARTIST, MediaItem.album), else_=None)
    rows = db.execute(
        select(
            Artist.normalized_name,
            Artist.name.label("artist"),
            func.count(func.distinct(MediaItem.id)).label("track_count"),
            func.count(func.distinct(owned_album)).label("album_count"),
            representative_media_item_id(),
        )
        .select_from(TrackArtist)
        .join(Artist, Artist.id == TrackArtist.artist_id)
        .join(MediaItem, MediaItem.id == TrackArtist.media_item_id)
        .where(
            Artist.normalized_name.in_(list(keys)),
            MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
            MediaItem.kind == MediaKind.track,
        )
        .group_by(Artist.id)
    ).all()
    by_key = {row.normalized_name: row for row in rows}
    outs = [
        ArtistOut(
            artist=row.artist,
            track_count=row.track_count,
            album_count=row.album_count,
            representative_media_item_id=row.representative_media_item_id,
        )
        for key in keys
        if (row := by_key.get(key)) is not None
    ]
    annotate_artists(db, outs)
    return outs


@router.get("/album-summaries", response_model=list[AlbumOut])
def album_summaries(
    db: DbSession,
    user: CurrentUser,
    album: Annotated[str, Query(min_length=1, max_length=1000)],
    artist: Annotated[str | None, Query(max_length=1000)] = None,
) -> list[AlbumOut]:
    """One album's Albums-tab entry (art key, dates, types, MusicBrainz
    release group, track count), so an album page can show its header
    without downloading the album list. `artist` is the album's owner as
    the list shows it (AlbumOut.artist); leave it out for an album with no
    owner. A list of at most one item: empty when the album has no
    accessible tracks. Built from the same query as /albums."""
    owner = album_owner_expr()
    filters = [
        *_album_base_filters(accessible_root_ids(db, user)),
        MediaItem.album == album,
        owner.is_(None) if artist is None else owner == artist,
    ]
    query, _original_date = _album_group_query(filters)
    outs = [AlbumOut(**d) for d in album_dicts(db, db.execute(query).all())]
    annotate_albums(db, outs)
    return outs
