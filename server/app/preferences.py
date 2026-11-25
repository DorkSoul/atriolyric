"""Per-user likes and dislikes on songs, albums and artists - the one place
that decides what counts as disliked or liked, so every automatic pick
(mixes, Song Mix, Discovery, autoplay) applies the same rule.

Precedence: a song's own state wins, then its album's (owner + title), then
its artist's. A track matches an artist on `artist` or `album_artist`, or on
any artist it is linked to as a main or album artist (app.artist_credits -
featured guests don't count); when those carry different states, the track
`artist` string wins. Names are
compared with SQL ``lower()`` on both sides - SQLite's lower() only folds
ASCII, so doing both sides in SQL keeps every comparison consistent (see
fold_name for the one Python-side comparison).

Songs live in Favorite (liked, kept in step with the Favourites playlist)
and TrackDislike. Albums and artists live in CollectionPreference.
"""

from collections import Counter
from collections.abc import Collection, Iterable
from typing import Any, Literal, cast

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    delete,
    exists,
    func,
    not_,
    or_,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import QueryableAttribute, Session

from app.artist_credits import (
    ALL_ROLES,
    OWNING_ROLES,
    album_owner_expr,
    artist_link_filter,
)
from app.library_query import filter_tracks_query
from app.models import (
    Artist,
    CollectionPreference,
    Favorite,
    MediaItem,
    MediaKind,
    Playlist,
    PlaylistItem,
    TrackArtist,
    TrackDislike,
    User,
)

TrackState = Literal["none", "liked", "disliked"]

# A mapped attribute (MediaItem.artist) or an expression over one.
ColumnLike = ColumnElement[Any] | QueryableAttribute[Any]
# Generated playlists hide disliked songs whenever they're read. User
# playlists and Favourites never do.
FILTERED_AUTO_KINDS = ("mix", "discovery", "generated")
FAVORITES_PLAYLIST_NAME = "Favourites"


def fold_name(name: str) -> str:
    """ASCII-only lowercase, matching SQLite's built-in lower() - for the
    rare comparison that has to happen in Python against names SQL folded."""
    return name.translate(_ASCII_LOWER)


_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)




def _song_state(user_id: int, model: type[Any]) -> ColumnElement[bool]:
    return exists().where(
        model.user_id == user_id, model.media_item_id == MediaItem.id
    )


def _album_state(user_id: int, state: str) -> ColumnElement[bool]:
    return exists().where(
        CollectionPreference.user_id == user_id,
        CollectionPreference.kind == "album",
        CollectionPreference.state == state,
        func.lower(CollectionPreference.artist) == func.lower(album_owner_expr()),
        func.lower(CollectionPreference.album) == func.lower(MediaItem.album),
    )


def _artist_name_state(user_id: int, state: str, column: ColumnLike) -> ColumnElement[bool]:
    return exists().where(
        CollectionPreference.user_id == user_id,
        CollectionPreference.kind == "artist",
        CollectionPreference.state == state,
        func.lower(CollectionPreference.artist) == func.lower(column),
    )


def _linked_artist_state(user_id: int, state: str) -> ColumnElement[bool]:
    """Any artist the track is by (main or album artist, not a featured
    guest) carries `state` - so liking "Rihanna" covers "Rihanna & Drake"."""
    return exists().where(
        TrackArtist.media_item_id == MediaItem.id,
        TrackArtist.role.in_(OWNING_ROLES),
        Artist.id == TrackArtist.artist_id,
        CollectionPreference.user_id == user_id,
        CollectionPreference.kind == "artist",
        CollectionPreference.state == state,
        func.lower(CollectionPreference.artist) == func.lower(Artist.name),
    )


def _state_parts(user_id: int) -> dict:
    return {
        "song_liked": _song_state(user_id, Favorite),
        "song_disliked": _song_state(user_id, TrackDislike),
        "album_liked": _album_state(user_id, "liked"),
        "album_disliked": _album_state(user_id, "disliked"),
        "artist_liked": _artist_name_state(user_id, "liked", MediaItem.artist),
        "artist_disliked": _artist_name_state(user_id, "disliked", MediaItem.artist),
        "album_artist_liked": or_(
            _artist_name_state(user_id, "liked", MediaItem.album_artist),
            _linked_artist_state(user_id, "liked"),
        ),
        "album_artist_disliked": or_(
            _artist_name_state(user_id, "disliked", MediaItem.album_artist),
            _linked_artist_state(user_id, "disliked"),
        ),
    }


def not_disliked(user_id: int) -> ColumnElement[bool]:
    """SQL predicate on MediaItem: true unless the rule above excludes it."""
    p = _state_parts(user_id)
    disliked = or_(
        p["song_disliked"],
        and_(
            not_(p["song_liked"]),
            or_(
                p["album_disliked"],
                and_(
                    not_(p["album_liked"]),
                    or_(
                        p["artist_disliked"],
                        and_(
                            not_(p["artist_liked"]),
                            p["album_artist_disliked"],
                        ),
                    ),
                ),
            ),
        ),
    )
    return not_(disliked)


def artist_not_disliked(user_id: int, column: ColumnLike) -> ColumnElement[bool]:
    """SQL predicate on an artist-name column: the name itself isn't
    disliked. For picks that work on artists rather than tracks (Discovery
    and Explore seeds and recommendations)."""
    return not_(_artist_name_state(user_id, "disliked", column))


def _resolve(row) -> tuple[bool, bool, str | None, str | None]:
    """(is_favorite, is_disliked, excluded_by, liked_level) for one row of
    the _state_parts flags, following the precedence rule."""
    if row.song_disliked:
        return (False, True, None, None)
    if row.song_liked:
        return (True, False, None, "song")
    if row.album_disliked:
        return (False, False, "album", None)
    if row.album_liked:
        return (False, False, None, "album")
    if row.artist_disliked:
        return (False, False, "artist", None)
    if row.artist_liked:
        return (False, False, None, "artist")
    if row.album_artist_disliked:
        return (False, False, "artist", None)
    if row.album_artist_liked:
        return (False, False, None, "artist")
    return (False, False, None, None)


# Mix candidate pools can cover a whole genre; bound each IN (...) list
# well under SQLite's host-parameter limit.
_ID_BATCH = 900


def _batches(ids: list[int]) -> Iterable[list[int]]:
    for start in range(0, len(ids), _ID_BATCH):
        yield ids[start : start + _ID_BATCH]


def _resolved_states(
    db: Session, user_id: int, ids: Iterable[int]
) -> dict[int, tuple[bool, bool, str | None, str | None]]:
    has_collections = (
        db.execute(
            select(CollectionPreference.id).where(CollectionPreference.user_id == user_id).limit(1)
        ).first()
        is not None
    )
    result: dict[int, tuple[bool, bool, str | None, str | None]] = {}
    for batch in _batches(list(ids)):
        result.update(_resolved_batch(db, user_id, batch, has_collections))
    return result


def _resolved_batch(
    db: Session, user_id: int, ids: list[int], has_collections: bool
) -> dict[int, tuple[bool, bool, str | None, str | None]]:
    if not has_collections:
        # The common case needs only the two song tables - no media_items
        # lookup, so a cached track listing stays a cache hit.
        liked = set(
            db.execute(
                select(Favorite.media_item_id).where(
                    Favorite.user_id == user_id, Favorite.media_item_id.in_(ids)
                )
            ).scalars()
        )
        disliked = set(
            db.execute(
                select(TrackDislike.media_item_id).where(
                    TrackDislike.user_id == user_id,
                    TrackDislike.media_item_id.in_(ids),
                )
            ).scalars()
        )
        return {
            media_id: (
                (False, True, None, None)
                if media_id in disliked
                else (True, False, None, "song")
                if media_id in liked
                else (False, False, None, None)
            )
            for media_id in ids
        }
    parts = _state_parts(user_id)
    rows = db.execute(
        select(MediaItem.id, *(expr.label(name) for name, expr in parts.items()))
        .where(MediaItem.id.in_(ids))
    ).all()
    return {row.id: _resolve(row) for row in rows}


def preference_overlay(
    db: Session, user: User, ids: Iterable[int]
) -> dict[int, tuple[bool, bool, str | None]]:
    """Per-user (is_favorite, is_disliked, excluded_by) for each id - a
    query per few hundred ids, never one per id. `excluded_by` is "album" or
    "artist" when only the song's album or artist keeps it out of mixes."""
    return {
        media_id: state[:3]
        for media_id, state in _resolved_states(db, user.id, ids).items()
    }


def liked_levels(db: Session, user: User, ids: Iterable[int]) -> dict[int, str]:
    """Which of `ids` are liked, and through what: "song", "album" or
    "artist". Each song counts once, at its most specific state - likes
    don't stack."""
    return {
        media_id: state[3]
        for media_id, state in _resolved_states(db, user.id, ids).items()
        if state[3] is not None
    }


def disliked_ids(db: Session, user: User, ids: Iterable[int]) -> set[int]:
    """The subset of `ids` the rule excludes from automatic picks."""
    hidden: set[int] = set()
    for batch in _batches(list(ids)):
        hidden.update(
            db.execute(
                select(MediaItem.id).where(
                    MediaItem.id.in_(batch), not_(not_disliked(user.id))
                )
            ).scalars()
        )
    return hidden


# --- generated playlists -------------------------------------------------


def visible_auto_items(
    db: Session, user: User, playlist: Playlist, items: list[PlaylistItem]
) -> list[PlaylistItem]:
    """Drop disliked songs from a generated playlist's items (mixes,
    Discovery, Song Mix). A Song Mix's own seed (position 0) always stays -
    it was an explicit request. Other playlists come back unchanged."""
    if playlist.auto_kind not in FILTERED_AUTO_KINDS:
        return items
    hidden = disliked_ids(db, user, {item.media_item_id for item in items})
    if not hidden:
        return items
    return [
        item
        for item in items
        if item.media_item_id not in hidden
        or (playlist.auto_kind == "generated" and item.position == 0)
    ]


def playlist_item_visible(user_id: int) -> ColumnElement[bool]:
    """SQL counterpart of visible_auto_items, for queries joining
    PlaylistItem, Playlist and MediaItem (playlist item counts)."""
    return or_(
        Playlist.auto_kind.is_(None),
        Playlist.auto_kind.notin_(FILTERED_AUTO_KINDS),
        and_(Playlist.auto_kind == "generated", PlaylistItem.position == 0),
        not_disliked(user_id),
    )


# --- songs ---------------------------------------------------------------


def get_or_create_favorites_playlist(db: Session, user: User) -> Playlist:
    """A real, auto-managed Playlist row kept in sync with the Favorite
    table (added to on like, removed from on unlike or dislike) - rather
    than a virtual/derived list, so it shows up and behaves like any other
    playlist (playable, exportable, reorderable). Flushes rather than
    commits, so it can sit inside a caller's transaction."""
    playlist = db.scalars(
        select(Playlist)
        .where(Playlist.user_id == user.id, Playlist.auto_kind == "favorites")
        .limit(1)
    ).first()
    if playlist is None:
        playlist = Playlist(
            user_id=user.id,
            name=FAVORITES_PLAYLIST_NAME,
            is_auto_generated=True,
            auto_kind="favorites",
        )
        db.add(playlist)
        db.flush()
    return playlist


def _has_favorite(db: Session, user_id: int, media_item_id: int) -> bool:
    return (
        db.execute(
            select(Favorite.user_id)
            .where(Favorite.user_id == user_id, Favorite.media_item_id == media_item_id)
            .limit(1)
        ).first()
        is not None
    )


def _add_favorite(db: Session, user: User, media_item_id: int) -> None:
    if not _has_favorite(db, user.id, media_item_id):
        db.add(Favorite(user_id=user.id, media_item_id=media_item_id))
    # Even for a song already liked: a missing Favourites playlist is
    # rebuilt, and the song put back in it.
    playlist = get_or_create_favorites_playlist(db, user)
    already_queued = db.scalars(
        select(PlaylistItem)
        .where(
            PlaylistItem.playlist_id == playlist.id,
            PlaylistItem.media_item_id == media_item_id,
        )
        .limit(1)
    ).first()
    if already_queued is None:
        next_position = db.execute(
            select(func.max(PlaylistItem.position)).where(PlaylistItem.playlist_id == playlist.id)
        ).scalar_one_or_none()
        db.add(
            PlaylistItem(
                playlist_id=playlist.id,
                media_item_id=media_item_id,
                position=(next_position if next_position is not None else -1) + 1,
            )
        )


def _remove_favorite(db: Session, user: User, media_item_id: int) -> None:
    db.execute(
        delete(Favorite).where(Favorite.user_id == user.id, Favorite.media_item_id == media_item_id)
    )
    playlist = db.scalars(
        select(Playlist)
        .where(Playlist.user_id == user.id, Playlist.auto_kind == "favorites")
        .limit(1)
    ).first()
    if playlist is not None:
        db.execute(
            delete(PlaylistItem).where(
                PlaylistItem.playlist_id == playlist.id,
                PlaylistItem.media_item_id == media_item_id,
            )
        )


def set_track_state(
    db: Session, user: User, media_item_id: int, state: TrackState
) -> None:
    """Set one song's state in a single transaction, keeping the Favourites
    playlist in step with `favorites`. Clearing a dislike ("none") never
    re-adds the song to Favourites."""
    # Two likes of one song (two devices, a double tap) can both decide to
    # insert the same Favorite or the Favourites playlist; the loser's
    # commit fails on the key, and a second pass finds the winner's rows.
    for attempt in (1, 2):
        try:
            db.execute(
                delete(TrackDislike).where(
                    TrackDislike.user_id == user.id, TrackDislike.media_item_id == media_item_id
                )
            )
            if state == "liked":
                _add_favorite(db, user, media_item_id)
            else:
                _remove_favorite(db, user, media_item_id)
                if state == "disliked":
                    db.add(TrackDislike(user_id=user.id, media_item_id=media_item_id))
            db.commit()
            return
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise


# --- albums and artists --------------------------------------------------


def album_owner(
    db: Session, user: User, artist: str | None, album: str
) -> str | None:
    """Who owns the album the album page shows for (artist, album): the most
    common album_artist-else-artist among exactly the tracks that page lists
    (app.library_query.filter_tracks_query). The Albums grid groups by the
    track artist, so a compilation opened from one of its track artists
    resolves to the compilation's album artist. None if the page is empty."""
    filters = {"artist": artist, "artist_is_null": artist is None, "album": album}
    owner = album_owner_expr()
    query = (
        filter_tracks_query(db, user, filters)
        .where(owner.is_not(None))
        .with_only_columns(owner)
    )
    counts = Counter(db.scalars(query).all())
    if not counts:
        return None
    # Ties break alphabetically so the answer is stable between requests.
    return min(counts, key=lambda name: (-counts[name], name))


def artist_exists(db: Session, user: User, artist: str, roots: Collection[int]) -> bool:
    return (
        db.execute(
            select(MediaItem.id)
            .where(
                MediaItem.library_root_id.in_(roots),
                MediaItem.kind == MediaKind.track,
                or_(
                    func.lower(MediaItem.artist) == func.lower(artist),
                    func.lower(MediaItem.album_artist) == func.lower(artist),
                    artist_link_filter(artist, ALL_ROLES),
                ),
            )
            .limit(1)
        ).first()
        is not None
    )


def _collection_query(user: User, kind: str, artist: str, album: str | None) -> Select[CollectionPreference]:
    query = select(CollectionPreference).where(
        CollectionPreference.user_id == user.id,
        CollectionPreference.kind == kind,
        func.lower(CollectionPreference.artist) == func.lower(artist),
    )
    if album is not None:
        query = query.where(
            func.lower(CollectionPreference.album) == func.lower(album)
        )
    return query


def get_collection_state(
    db: Session, user: User, kind: str, artist: str, album: str | None = None
) -> TrackState:
    row = db.scalars(_collection_query(user, kind, artist, album).limit(1)).first()
    # The column is a plain string; only set_collection_state writes it, with a TrackState.
    return cast(TrackState, row.state) if row is not None else "none"


def set_collection_state(
    db: Session,
    user: User,
    kind: str,
    artist: str,
    album: str | None,
    state: TrackState,
) -> None:
    """Set or clear one album's or artist's state. Liking an album or artist
    never likes its songs or touches Favourites."""
    row = db.scalars(_collection_query(user, kind, artist, album).limit(1)).first()
    if state == "none":
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(
            CollectionPreference(
                user_id=user.id, kind=kind, artist=artist, album=album, state=state
            )
        )
    else:
        row.state = state
    db.commit()
