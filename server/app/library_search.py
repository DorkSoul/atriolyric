"""Home search: the user's artists, albums, songs and playlists matching
every word of a query, ignoring case and accents.

Matching folds both sides at query time with the atrio_fold SQL function
(app.db.fold_text), so no stored normalized column or rescan is needed.
Results are never cached: one entry per typed query would grow the
library cache without limit (it only empties when the library changes).
"""

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.artist_credits import ROLE_ALBUM_ARTIST, album_owner_expr
from app.custom_art import annotate_albums, annotate_artists
from app.db import fold_text
from app.library_query import representative_media_item_id
from app.models import Artist, MediaItem, MediaKind, Playlist, TrackArtist, User
from app.preferences import ColumnLike
from app.presenters import album_dicts, playlist_outs, to_media_item_outs
from app.schemas import AlbumOut, ArtistOut, LibrarySearchOut, MediaItemOut, PlaylistOut

# The same cap as each Home rail (MAX_HOME_ITEMS in HomeView.tsx).
SEARCH_SECTION_LIMIT = 13


def parse_query(q: str) -> list[str]:
    """The folded words of `q`. A word that folds to nothing (a lone
    combining mark, say) is dropped."""
    return [word for word in (fold_text(part) for part in q.split()) if word]


def escape_like(s: str) -> str:
    """Escape LIKE metacharacters so '%' and '_' in a query are literal
    (the same rule as the Files search)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def word_filters(col: ColumnLike, words: list[str]) -> list[ColumnElement[bool]]:
    """Every word must appear somewhere in the folded column."""
    return [
        func.atrio_fold(col).like(f"%{escape_like(word)}%", escape="\\") for word in words
    ]


def rank_expr(col: ColumnLike, words: list[str]) -> ColumnElement[int]:
    """0 when the folded column starts with the whole query, else 1, so
    starts-with matches sort first."""
    prefix = f"{escape_like(' '.join(words))}%"
    return case((func.atrio_fold(col).like(prefix, escape="\\"), 0), else_=1)


def _track_filters(db: Session, user: User) -> list:
    return [
        MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
        MediaItem.kind == MediaKind.track,
    ]


def search_songs(db: Session, user: User, words: list[str]) -> list[MediaItemOut]:
    """Songs match on title only - a song by a matching artist, or on a
    matching album, isn't a song match."""
    items = list(db.scalars(
        select(MediaItem)
        .where(*_track_filters(db, user), *word_filters(MediaItem.title, words))
        .order_by(
            rank_expr(MediaItem.title, words),
            func.atrio_fold(MediaItem.title),
            func.atrio_fold(MediaItem.artist),
            MediaItem.id,
        )
        .limit(SEARCH_SECTION_LIMIT)
    ))
    return to_media_item_outs(items, db, user)


def search_albums(db: Session, user: User, words: list[str]) -> list[AlbumOut]:
    """Grouped by (owner, album), the same as the Albums tab, so opening a
    result shows exactly what that tab would."""
    owner = album_owner_expr()
    rows = db.execute(
        select(
            owner.label("artist"),
            MediaItem.album,
            func.count(MediaItem.id).label("track_count"),
            representative_media_item_id(),
        )
        .where(
            *_track_filters(db, user),
            MediaItem.album.is_not(None),
            *word_filters(MediaItem.album, words),
        )
        .group_by(owner, MediaItem.album)
        .order_by(
            rank_expr(MediaItem.album, words),
            func.atrio_fold(MediaItem.album),
            func.lower(func.coalesce(owner, "")),
        )
        .limit(SEARCH_SECTION_LIMIT)
    ).all()
    outs = [AlbumOut(**d) for d in album_dicts(db, rows)]
    annotate_albums(db, outs)
    return outs


def search_artists(db: Session, user: User, words: list[str]) -> list[ArtistOut]:
    """Linked artists, built the same way as the Artists tab."""
    owned_album = case((TrackArtist.role == ROLE_ALBUM_ARTIST, MediaItem.album), else_=None)
    rows = db.execute(
        select(
            Artist.name.label("artist"),
            func.count(func.distinct(MediaItem.id)).label("track_count"),
            func.count(func.distinct(owned_album)).label("album_count"),
            representative_media_item_id(),
        )
        .select_from(TrackArtist)
        .join(Artist, Artist.id == TrackArtist.artist_id)
        .join(MediaItem, MediaItem.id == TrackArtist.media_item_id)
        .where(*_track_filters(db, user), *word_filters(Artist.name, words))
        .group_by(Artist.id)
        .order_by(
            rank_expr(Artist.name, words),
            func.atrio_fold(Artist.name),
            Artist.name,
        )
        .limit(SEARCH_SECTION_LIMIT)
    ).all()
    outs = [
        ArtistOut(
            artist=row.artist,
            track_count=row.track_count,
            album_count=row.album_count,
            representative_media_item_id=row.representative_media_item_id,
        )
        for row in rows
    ]
    annotate_artists(db, outs)
    return outs


def search_playlists(db: Session, user: User, words: list[str]) -> list[PlaylistOut]:
    """Every playlist row the user owns, Mixes and Discovery included."""
    playlists = list(db.scalars(
        select(Playlist)
        .where(Playlist.user_id == user.id, *word_filters(Playlist.name, words))
        .order_by(
            rank_expr(Playlist.name, words),
            func.atrio_fold(Playlist.name),
            Playlist.id,
        )
        .limit(SEARCH_SECTION_LIMIT)
    ))
    return playlist_outs(db, user, playlists)


def search_library(db: Session, user: User, q: str) -> LibrarySearchOut:
    words = parse_query(q)
    if not words:
        return LibrarySearchOut(artists=[], albums=[], songs=[], playlists=[])
    return LibrarySearchOut(
        artists=search_artists(db, user, words),
        albums=search_albums(db, user, words),
        songs=search_songs(db, user, words),
        playlists=search_playlists(db, user, words),
    )
