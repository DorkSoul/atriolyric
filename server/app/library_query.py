from collections.abc import Collection
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    ColumnElement,
    CompoundSelect,
    Select,
    and_,
    case,
    func,
    literal,
    select,
    union,
)
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.artist_credits import album_owner_expr, artist_link_filter
from app.folder_exclusions import not_in_ignored_folder
from app.metadata_taxonomy import genre_filter, tag_filter
from app.models import (
    Album,
    Genre,
    MediaItem,
    MediaItemGenre,
    MediaItemMusicBrainzTag,
    MediaItemTag,
    MediaKind,
    MusicBrainzTag,
    PlayHistory,
    Tag,
    User,
)


def filter_tracks_query(db: Session, user: User, filters: dict) -> Select[MediaItem]:
    """The track-matching WHERE clause shared by /api/library/tracks,
    /api/library/track-ids, and the windowed "library"-kind playback queue
    context (app.playback) - kept in one place so all three ways of asking
    "which tracks match this filter" never drift apart. A select of
    MediaItem: run it with db.scalars(), or narrow its columns with
    with_only_columns()."""
    query = select(MediaItem).where(
        MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
        MediaItem.kind == MediaKind.track,
    )
    q = filters.get("q")
    if q:
        like = f"%{q}%"
        query = query.where(
            (MediaItem.title.ilike(like))
            | (MediaItem.artist.ilike(like))
            | (MediaItem.album.ilike(like))
        )
    genre = filters.get("genre")
    if genre:
        # The legacy column contains the first embedded genre, while the
        # normalized relationship contains every embedded genre plus any
        # provider genres.  Keep both paths so old rows and multi-genre files
        # remain searchable when external metadata is unavailable.
        query = query.where(genre_filter(genre))
    tag = filters.get("tag")
    if tag:
        query = query.where(tag_filter(tag))
    artist = filters.get("artist")
    album = filters.get("album")
    if filters.get("artist_is_null") is True:
        query = query.where(MediaItem.artist.is_(None))
    elif artist is not None and artist != "":
        # An artist's own tracks: credited as a main artist or album artist
        # (featured guest spots are listed apart, under "Appears on"). The
        # raw strings still match too, for rows a scan hasn't linked yet,
        # and an album is opened by its displayed owner ("A & B").
        by_artist = (
            artist_link_filter(artist)
            | (MediaItem.artist == artist)
            | (MediaItem.album_artist == artist)
        )
        if album:
            by_artist = by_artist | (album_owner_expr() == artist)
        query = query.where(by_artist)
    if album:
        query = query.where(MediaItem.album == album)
    return query


class TrackSort(Enum):
    """The orders /api/library/tracks (the Songs tab) can list in. `artist`
    is the default: artist, album, track number, id."""

    artist = "artist"
    title = "title"
    album = "album"
    year = "year"
    date_added = "date_added"
    most_played = "most_played"
    last_played = "last_played"


# The direction a sort means when none is asked for: A-Z for the names,
# newest / most first for the rest (as the Albums tab's sorts default).
DEFAULT_TRACK_DIRECTION: dict[TrackSort, str] = {
    TrackSort.artist: "asc",
    TrackSort.title: "asc",
    TrackSort.album: "asc",
    TrackSort.year: "desc",
    TrackSort.date_added: "desc",
    TrackSort.most_played: "desc",
    TrackSort.last_played: "desc",
}

# Sorts that read the requesting user's play history: per user and changed
# by every play, so never cached (like the Albums tab's played sorts).
PLAYED_TRACK_SORTS = frozenset({TrackSort.most_played, TrackSort.last_played})

# The default sort's order. The id breaks ties, so a page
# boundary between tracks that share artist, album and number (or have
# none) cannot repeat or skip one.
_DEFAULT_TRACK_ORDER = (MediaItem.artist, MediaItem.album, MediaItem.track_number, MediaItem.id)


def track_release_date() -> Any:
    """A track's release date as the album page dates it: the album's
    original release (the MusicBrainz release group's first release), else
    the file's own date tag - the same preference as AlbumOut.release_date
    (app.routers.library.albums_artists._album_group_query). A correlated
    lookup by primary key rather than a join, so it composes with any query
    over MediaItem (filters, the playback queue's id-only selects)."""
    album_date = (
        select(Album.first_release_date).where(Album.id == MediaItem.album_id).scalar_subquery()
    )
    return func.coalesce(album_date, MediaItem.release_date)


def year_of(release_date: str | None) -> int | None:
    """The year of a "YYYY-MM-DD" release date, or None when it has none."""
    head = (release_date or "")[:4]
    return int(head) if len(head) == 4 and head.isdigit() and int(head) > 0 else None


def track_sort_terms(
    query: Select,
    sort: TrackSort,
    direction: str,
    user_id: int,
    played_before: datetime | None = None,
) -> tuple[Select, list]:
    """`query` (over MediaItem) with whatever the sort needs joined in, and
    the ORDER BY terms for it. Shared by /api/library/tracks and the
    "library" playback queue context, so the queue plays in the order the
    Songs tab shows.

    Every order ends with the default order (always ascending) and the
    id, so pages never repeat or skip a track. A track with no year, no
    album, or never played by `user_id` sorts last in both directions.
    `played_before` counts only plays up to that moment (a queue freezes
    its order when it starts, so its own plays cannot reshuffle it)."""
    desc = direction == "desc"
    tie = list(_DEFAULT_TRACK_ORDER)
    if sort == TrackSort.artist:
        return query, [column.desc() if desc else column for column in _DEFAULT_TRACK_ORDER]

    def directed(expr: Any) -> Any:
        return expr.desc() if desc else expr.asc()

    if sort in (TrackSort.title, TrackSort.album):
        column = MediaItem.title if sort == TrackSort.title else MediaItem.album
        # Case-folded like the Albums tab's A-Z, the raw value splitting
        # "ABBA" from "Abba".
        return query, [column.is_(None), directed(func.atrio_lower(column)), directed(column), *tie]
    if sort == TrackSort.year:
        year = func.substr(track_release_date(), 1, 4)
        return query, [year.is_(None), directed(year), *tie]
    if sort == TrackSort.date_added:
        return query, [directed(MediaItem.created_at), *tie]
    # most_played / last_played: the user's own plays only.
    conditions = [PlayHistory.user_id == user_id]
    if played_before is not None:
        conditions.append(PlayHistory.played_at <= played_before)
    stats = (
        select(
            PlayHistory.media_item_id.label("media_item_id"),
            func.count(PlayHistory.id).label("plays"),
            func.max(PlayHistory.played_at).label("last_played"),
        )
        .where(*conditions)
        .group_by(PlayHistory.media_item_id)
        .subquery()
    )
    key = stats.c.plays if sort == TrackSort.most_played else stats.c.last_played
    return (
        query.outerjoin(stats, stats.c.media_item_id == MediaItem.id),
        [key.is_(None), directed(key), *tie],
    )


def genre_rows(*where: ColumnElement[bool]) -> list[Select]:
    """(kind "genre", name, media_item_id) selects for the genres of the
    MediaItems matching `where`, to be UNIONed: the normalised genres, and
    the legacy MediaItem.genre column only for a track that has none -
    otherwise a track tagged "rock" whose canonical genre is "Rock" is
    counted under both. The one definition of "a track's genres" for
    listings and counts."""
    return [
        select(literal("genre").label("kind"), MediaItem.genre.label("name"), MediaItem.id.label("media_item_id"))
        .where(MediaItem.genre.is_not(None), ~MediaItem.genres.any(), *where),
        select(literal("genre").label("kind"), Genre.name.label("name"), MediaItem.id.label("media_item_id"))
        .select_from(Genre).join(MediaItemGenre).join(MediaItem).where(*where),
    ]


def tag_rows(*where: ColumnElement[bool]) -> list[Select]:
    """(kind "tag", name, media_item_id) selects for the tags of the
    MediaItems matching `where`: the library's own tags and the
    MusicBrainz/ListenBrainz ones."""
    return [
        select(literal("tag").label("kind"), Tag.name.label("name"), MediaItem.id.label("media_item_id"))
        .select_from(Tag).join(MediaItemTag).join(MediaItem).where(*where),
        select(literal("tag").label("kind"), MusicBrainzTag.name.label("name"), MediaItem.id.label("media_item_id"))
        .select_from(MusicBrainzTag).join(MediaItemMusicBrainzTag).join(MediaItem).where(*where),
    ]


def label_track_rows(
    accessible: Collection[int], *filters: ColumnElement[bool], kinds: Collection[str] = ("genre", "tag")
) -> CompoundSelect:
    """(kind, name, media_item_id) for every genre ("genre") and tag ("tag")
    on an accessible track outside ignored folders, distinct, narrowed by
    `filters` on MediaItem (genre_rows and tag_rows say what counts).

    The one definition behind /genres, /mixes/options, the automatic mixes
    and Discovery's artist labels."""
    base = (
        MediaItem.kind == MediaKind.track,
        MediaItem.library_root_id.in_(accessible),
        not_in_ignored_folder(),
        *filters,
    )
    parts: list[Select] = []
    if "genre" in kinds:
        parts += genre_rows(*base)
    if "tag" in kinds:
        parts += tag_rows(*base)
    return union(*parts)


def representative_media_item_id():
    """The track an album's or artist's tile takes its picture from, as an
    aggregate over the group's tracks: the lowest id among tracks not known
    to have no art (app.art.backfill_art_thumbs marks a track it found
    without any, until its file changes), else the lowest id. A group whose
    first track has no picture - one folder without covers among several
    with - so still shows a cover."""
    known_without_art = and_(
        MediaItem.art_checked_mtime.is_not(None), MediaItem.art_checked_mtime == MediaItem.file_mtime
    )
    return func.coalesce(
        func.min(case((known_without_art, None), else_=MediaItem.id)), func.min(MediaItem.id)
    ).label("representative_media_item_id")
