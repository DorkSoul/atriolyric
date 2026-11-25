"""/api/library/stats: the numbers behind the Library info screen.

Everything is aggregated in SQL over the user's accessible roots (outside
ignored folders, unless noted). The parts that do not depend on who is
asking are cached per root set, library version (app.library_cache) and
date, and app.cache_warmup computes them in the background after every
library change and once a day, so a request does not wait on them. Counts
that background work changes without bumping the version (lyrics fetches)
therefore show from the next day or the next library change. The listening
figures, liked songs, the per-user tab counts, the last scan time and the
free space are read on every request."""

import os
import shutil
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter
from sqlalchemy import ColumnElement, and_, case, exists, func, select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.artist_credits import ROLE_MAIN, album_owner_expr
from app.audio_quality import (
    LOSSLESS_CODECS,
    LOSSLESS_EXTENSIONS,
    codec_display_name,
    format_label,
)
from app.cleanup import LISTEN_EVENTS_MAX_PER_USER
from app.config import settings
from app.custom_art import annotate_albums
from app.deps import CurrentUser, DbSession
from app.folder_exclusions import not_in_ignored_folder
from app.library_cache import get_or_compute
from app.library_query import label_track_rows, representative_media_item_id
from app.models import (
    Album,
    Artist,
    Favorite,
    LibraryRoot,
    ListenEvent,
    MediaItem,
    MediaKind,
    PlayHistory,
    Playlist,
    RadioStation,
    TrackArtist,
    User,
    WantListItem,
    utcnow,
)
from app.schemas import (
    LibraryStatsOut,
    StatsNavCountsOut,
    StatsRecentAlbumOut,
    StatsStorageOut,
)

router = APIRouter()

RECENT_ALBUMS = 50
TOP_GENRES = 12
HI_RES_MIN_BIT_DEPTH = 24
HI_RES_ABOVE_SAMPLE_RATE = 48_000
LOW_BITRATE_KBPS = 192
VIDEO_KINDS = (MediaKind.video, MediaKind.music_video)


def _extension_expr() -> ColumnElement[str]:
    """The lower-cased file extension of MediaItem.relative_path, in SQL:
    rtrim with every character but "." strips back to the last dot."""
    path = MediaItem.relative_path
    up_to_last_dot = func.rtrim(path, func.replace(path, ".", ""))
    return func.lower(func.substr(path, func.length(up_to_last_dot) + 1))


def _format_key() -> ColumnElement[str]:
    return func.coalesce(MediaItem.codec, _extension_expr())


def _track_filters(
    roots: set[int], *, ignored: bool = False
) -> tuple[ColumnElement[bool], ...]:
    base = (MediaItem.kind == MediaKind.track, MediaItem.library_root_id.in_(roots))
    return base if ignored else (*base, not_in_ignored_folder())


def _period_starts() -> tuple[datetime, datetime, datetime]:
    now = utcnow()
    return (
        now - timedelta(days=7),
        now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0),
    )


@router.get("/stats", response_model=LibraryStatsOut)
def library_stats(db: DbSession, user: CurrentUser) -> dict[str, Any]:
    roots = accessible_root_ids(db, user)
    shared = shared_stats(db, roots)
    recent = [StatsRecentAlbumOut(**row) for row in shared["recently_added"]]
    annotate_albums(db, recent)
    last_scanned = db.execute(
        select(func.max(LibraryRoot.last_scanned_at)).where(LibraryRoot.id.in_(roots))
    ).scalar()
    liked = db.execute(
        select(func.count())
        .select_from(Favorite)
        .join(MediaItem, MediaItem.id == Favorite.media_item_id)
        .where(Favorite.user_id == user.id, *_track_filters(roots))
    ).scalar_one()
    return {
        "totals": {**shared["totals"], "last_scanned_at": last_scanned},
        "recently_added": recent,
        "contents": {**shared["contents"], "liked_songs": liked},
        "quality": shared["quality"],
        "listening": _listening(db, user, roots),
        "by_genre": shared["by_genre"],
        "by_decade": shared["by_decade"],
        "growth": shared["growth"],
        "nav_counts": {**shared["nav_counts"], **_user_nav_counts(db, user)},
    }


@router.get("/stats/nav-counts", response_model=StatsNavCountsOut)
def library_nav_counts(db: DbSession, user: CurrentUser) -> dict[str, int]:
    """Only the sidebar's tab counts (the `nav_counts` of /stats), without
    the rest of the Library info numbers, so the sidebar never waits on
    them. The library part is cached per root set and library version."""
    roots = accessible_root_ids(db, user)
    return {**shared_nav_counts(db, roots), **_user_nav_counts(db, user)}


# --- Shared (cached) ---------------------------------------------------------------


def shared_stats(db: Session, roots: set[int]) -> dict[str, Any]:
    """The Library info numbers that do not depend on who is asking, cached
    until the library changes or the (UTC) date does - some are "this
    month"/"this year" figures."""
    key = ("library_stats", tuple(sorted(roots)), utcnow().date().isoformat())
    return get_or_compute(key, lambda: _shared_stats(db, roots))


def shared_nav_counts(db: Session, roots: set[int]) -> dict[str, int]:
    """The sidebar's library counts, cached until the library changes."""
    return get_or_compute(
        ("library_nav_counts", tuple(sorted(roots))), lambda: _shared_nav_counts(db, roots)
    )


def _shared_stats(db: Session, roots: set[int]) -> dict[str, Any]:
    tracks = _track_filters(roots)
    _week, month_start, year_start = _period_starts()
    lossless = MediaItem.codec.in_(sorted(LOSSLESS_CODECS))

    def count_if(condition: ColumnElement[bool]) -> Any:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    track_row = db.execute(
        select(
            func.count(MediaItem.id).label("songs"),
            func.coalesce(func.sum(MediaItem.file_size), 0).label("bytes"),
            func.coalesce(func.sum(MediaItem.duration_seconds), 0.0).label("seconds"),
            func.max(MediaItem.duration_seconds).label("longest"),
            count_if(
                MediaItem.lyrics_plain.is_not(None)
                | MediaItem.lyrics_synced.is_not(None)
            ).label("lyrics"),
            count_if(MediaItem.replaygain_track_gain.is_not(None)).label("loudness"),
            count_if(MediaItem.codec.is_not(None)).label("scanned"),
            count_if(lossless).label("lossless"),
            count_if(
                lossless
                & (
                    (MediaItem.bit_depth >= HI_RES_MIN_BIT_DEPTH)
                    | (MediaItem.sample_rate_hz > HI_RES_ABOVE_SAMPLE_RATE)
                )
            ).label("hi_res"),
            func.avg(
                case((MediaItem.codec == "mp3", MediaItem.bitrate_kbps), else_=None)
            ).label("mp3_kbps"),
            count_if(
                MediaItem.codec.is_not(None)
                & ~lossless
                & (MediaItem.bitrate_kbps < LOW_BITRATE_KBPS)
            ).label("low_bitrate"),
        ).where(*tracks)
    ).one()

    video_row = db.execute(
        select(
            func.count(MediaItem.id), func.coalesce(func.sum(MediaItem.file_size), 0)
        ).where(
            MediaItem.kind.in_(VIDEO_KINDS),
            MediaItem.library_root_id.in_(roots),
            not_in_ignored_folder(),
        )
    ).one()

    fmt = _format_key().label("format")
    formats = [
        {
            "format": key,
            "label": codec_display_name(key),
            "lossless": key in LOSSLESS_CODECS or key in LOSSLESS_EXTENSIONS,
            "songs": songs,
            "bytes": size or 0,
        }
        for key, songs, size in db.execute(
            select(fmt, func.count(MediaItem.id), func.sum(MediaItem.file_size))
            .where(*tracks)
            .group_by(fmt)
            .order_by(func.count(MediaItem.id).desc(), fmt)
        )
        if key
    ]

    owner = album_owner_expr()
    albums = (
        select(
            owner.label("artist"),
            MediaItem.album.label("album"),
            func.sum(MediaItem.duration_seconds).label("seconds"),
            func.coalesce(func.sum(MediaItem.file_size), 0).label("bytes"),
            func.min(MediaItem.created_at).label("added_at"),
        )
        .where(*tracks, MediaItem.album.is_not(None))
        .group_by(owner, MediaItem.album)
        .subquery()
    )
    album_row = db.execute(
        select(
            func.count(),
            func.avg(albums.c.seconds),
            func.coalesce(
                func.sum(case((albums.c.added_at >= month_start, 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((albums.c.added_at >= year_start, 1), else_=0)), 0
            ),
        )
    ).one()
    largest = db.execute(
        select(albums.c.artist, albums.c.album, albums.c.bytes)
        .order_by(albums.c.bytes.desc(), albums.c.album)
        .limit(1)
    ).first()

    artists = db.execute(
        select(func.count(func.distinct(TrackArtist.artist_id)))
        .join(MediaItem, MediaItem.id == TrackArtist.media_item_id)
        .where(*tracks)
    ).scalar_one()

    songs = track_row.songs
    scanned = track_row.scanned
    genres = _genres(db, roots)
    return {
        "totals": {
            "albums": album_row[0],
            "songs": songs,
            "artists": artists,
            "videos": video_row[0],
            "total_bytes": (track_row.bytes or 0) + (video_row[1] or 0),
            "total_seconds": float(track_row.seconds or 0),
        },
        "recently_added": _recent_albums(db, tracks),
        "contents": {
            "formats": formats,
            "with_lyrics": track_row.lyrics,
            "loudness_measured": track_row.loudness,
            "genre_count": genres["distinct"],
        },
        "quality": {
            "lossless_songs": track_row.lossless,
            "lossless_share": (track_row.lossless / scanned) if scanned else None,
            "hi_res_songs": track_row.hi_res,
            "average_mp3_kbps": round(track_row.mp3_kbps)
            if track_row.mp3_kbps is not None
            else None,
            "below_192_kbps_songs": track_row.low_bitrate,
            "longest_song_seconds": track_row.longest,
            "average_album_seconds": float(album_row[1])
            if album_row[1] is not None
            else None,
            "quality_scanned": scanned,
            "quality_total": songs,
        },
        "by_genre": genres,
        "by_decade": _decades(db, tracks),
        "growth": {
            "albums_added_this_month": album_row[2],
            "albums_added_this_year": album_row[3],
            "largest_album": (
                {
                    "artist": largest.artist,
                    "album": largest.album,
                    "bytes": largest.bytes,
                }
                if largest is not None and largest.bytes
                else None
            ),
            "video_bytes": video_row[1] or 0,
        },
        "nav_counts": _shared_nav_counts(db, roots),
    }


def _genres(db: Session, roots: set[int]) -> dict[str, Any]:
    """Songs per genre, by app.library_query's definition of a track's
    genres, with names compared case-insensitively ("rock" is "Rock")."""
    rows = label_track_rows(roots, kinds=("genre",)).subquery()
    folded = func.lower(rows.c.name)
    counted = (
        select(
            func.max(rows.c.name).label("name"),
            func.count(func.distinct(rows.c.media_item_id)).label("songs"),
        )
        .where(rows.c.name.is_not(None), rows.c.name != "")
        .group_by(folded)
        .subquery()
    )
    distinct = db.execute(select(func.count()).select_from(counted)).scalar_one()
    top = [
        {"name": name, "songs": songs}
        for name, songs in db.execute(
            select(counted.c.name, counted.c.songs)
            .order_by(counted.c.songs.desc(), func.lower(counted.c.name))
            .limit(TOP_GENRES)
        )
    ]
    return {"top": top, "distinct": distinct}


def _decades(db: Session, tracks: tuple[ColumnElement[bool], ...]) -> dict[str, Any]:
    decade = func.substr(MediaItem.release_date, 1, 3).label("decade")
    counts: dict[str | None, int] = {
        prefix: songs
        for prefix, songs in db.execute(
            select(decade, func.count(MediaItem.id)).where(*tracks).group_by(decade)
        )
    }
    unknown = counts.pop(None, 0)
    decades = []
    for prefix, songs in sorted(
        (key, value) for key, value in counts.items() if key is not None
    ):
        if prefix and prefix.isdigit():
            decades.append({"decade": f"{prefix}0s", "songs": songs})
        else:
            unknown += songs
    return {"decades": decades, "unknown": unknown}


def _recent_albums(
    db: Session, tracks: tuple[ColumnElement[bool], ...]
) -> list[dict[str, Any]]:
    owner = album_owner_expr()
    rows = db.execute(
        select(
            owner.label("artist"),
            MediaItem.album.label("album"),
            func.count(MediaItem.id).label("track_count"),
            representative_media_item_id(),
            func.min(MediaItem.created_at).label("added_at"),
            func.coalesce(
                func.max(Album.first_release_date), func.max(MediaItem.release_date)
            ).label("release_date"),
        )
        .outerjoin(Album, Album.id == MediaItem.album_id)
        .where(*tracks, MediaItem.album.is_not(None))
        .group_by(owner, MediaItem.album)
        .order_by(func.min(MediaItem.created_at).desc(), func.min(MediaItem.id).desc())
        .limit(RECENT_ALBUMS)
    ).all()
    labels = _album_format_labels(db, tracks, [(row.artist, row.album) for row in rows])
    out = []
    for row in rows:
        release_date = row.release_date
        year = (
            int(release_date[:4])
            if release_date and release_date[:4].isdigit()
            else None
        )
        out.append(
            {
                "artist": row.artist,
                "album": row.album,
                "track_count": row.track_count,
                "representative_media_item_id": row.representative_media_item_id,
                "release_date": release_date,
                "year": year,
                "format_label": labels.get((row.artist, row.album)),
                "added_at": row.added_at,
            }
        )
    return out


def _album_format_labels(
    db: Session,
    tracks: tuple[ColumnElement[bool], ...],
    albums: list[tuple[str | None, str]],
) -> dict[tuple[str | None, str], str | None]:
    """Each album's most common format among its tracks; for lossy files the
    average bitrate of those tracks. Only the given (few) albums are read."""
    if not albums:
        return {}
    wanted = set(albums)
    owner = album_owner_expr().label("artist")
    groups: dict[tuple[str | None, str], list[tuple[Any, ...]]] = {}
    for artist, album, codec, depth, rate, kbps, ext in db.execute(
        select(
            owner,
            MediaItem.album,
            MediaItem.codec,
            MediaItem.bit_depth,
            MediaItem.sample_rate_hz,
            MediaItem.bitrate_kbps,
            _extension_expr(),
        ).where(
            *tracks, MediaItem.album.in_(sorted({album for _artist, album in albums}))
        )
    ):
        if album is not None and (artist, album) in wanted:
            groups.setdefault((artist, album), []).append(
                (codec, depth, rate, kbps, ext)
            )
    labels: dict[tuple[str | None, str], str | None] = {}
    for key, rows in groups.items():
        fmt, _count = Counter(
            codec or ext for codec, _d, _r, _k, ext in rows
        ).most_common(1)[0]
        same = [row for row in rows if (row[0] or row[4]) == fmt]
        codec = same[0][0]
        if codec in LOSSLESS_CODECS:
            (depth, rate), _n = Counter((row[1], row[2]) for row in same).most_common(
                1
            )[0]
            labels[key] = format_label(codec, depth, rate, None, same[0][4])
        else:
            rates = [row[3] for row in same if row[3]]
            kbps = round(sum(rates) / len(rates)) if rates else None
            labels[key] = format_label(codec, None, None, kbps, same[0][4])
    return labels


def _shared_nav_counts(db: Session, roots: set[int]) -> dict[str, int]:
    """What the Songs, Albums, Artists and Videos tabs list: accessible
    roots, ignored folders included (the tabs show those too)."""
    tracks = _track_filters(roots, ignored=True)
    songs = db.execute(select(func.count(MediaItem.id)).where(*tracks)).scalar_one()
    owner = album_owner_expr()
    albums = db.execute(
        select(func.count()).select_from(
            select(owner, MediaItem.album)
            .where(*tracks, MediaItem.album.is_not(None))
            .group_by(owner, MediaItem.album)
            .subquery()
        )
    ).scalar_one()
    artists = db.execute(
        select(func.count(func.distinct(TrackArtist.artist_id)))
        .join(MediaItem, MediaItem.id == TrackArtist.media_item_id)
        .where(*tracks)
    ).scalar_one()
    videos = db.execute(
        select(func.count(MediaItem.id)).where(
            MediaItem.kind.in_(VIDEO_KINDS), MediaItem.library_root_id.in_(roots)
        )
    ).scalar_one()
    return {"songs": songs, "albums": albums, "artists": artists, "videos": videos}


# --- Per user (never cached) ----------------------------------------------------------


def _user_nav_counts(db: Session, user: User) -> dict[str, int]:
    playlists = db.execute(
        select(func.count(Playlist.id)).where(Playlist.user_id == user.id)
    ).scalar_one()
    stations = db.execute(
        select(func.count(RadioStation.id)).where(
            RadioStation.is_shared.is_(True) | (RadioStation.owner_user_id == user.id)
        )
    ).scalar_one()
    wants = db.execute(
        select(func.count(WantListItem.id)).where(
            WantListItem.user_id == user.id, WantListItem.excluded.is_(False)
        )
    ).scalar_one()
    return {"playlists": playlists, "radio_stations": stations, "want_list": wants}


def _listening(db: Session, user: User, roots: set[int]) -> dict[str, Any]:
    week_start, _month, year_start = _period_starts()
    in_roots = MediaItem.library_root_id.in_(roots)
    plays = select(func.count(PlayHistory.id)).join(
        MediaItem, MediaItem.id == PlayHistory.media_item_id
    )
    plays_week = db.execute(
        plays.where(
            PlayHistory.user_id == user.id,
            PlayHistory.played_at >= week_start,
            in_roots,
        )
    ).scalar_one()
    plays_year = db.execute(
        plays.where(
            PlayHistory.user_id == user.id,
            PlayHistory.played_at >= year_start,
            in_roots,
        )
    ).scalar_one()
    seconds = db.execute(
        select(func.coalesce(func.sum(ListenEvent.listened_seconds), 0.0))
        .join(MediaItem, MediaItem.id == ListenEvent.media_item_id)
        .where(
            ListenEvent.user_id == user.id,
            ListenEvent.started_at >= year_start,
            in_roots,
        )
    ).scalar_one()

    artist_row = db.execute(
        select(Artist.name, func.count(PlayHistory.id).label("plays"))
        .select_from(PlayHistory)
        .join(MediaItem, MediaItem.id == PlayHistory.media_item_id)
        .join(
            TrackArtist,
            and_(
                TrackArtist.media_item_id == PlayHistory.media_item_id,
                TrackArtist.role == ROLE_MAIN,
            ),
        )
        .join(Artist, Artist.id == TrackArtist.artist_id)
        .where(PlayHistory.user_id == user.id, in_roots)
        .group_by(Artist.id)
        .order_by(func.count(PlayHistory.id).desc(), func.lower(Artist.name))
        .limit(1)
    ).first()
    owner = album_owner_expr()
    album_row = db.execute(
        select(
            owner.label("artist"),
            MediaItem.album,
            func.count(PlayHistory.id).label("plays"),
        )
        .select_from(PlayHistory)
        .join(MediaItem, MediaItem.id == PlayHistory.media_item_id)
        .where(PlayHistory.user_id == user.id, in_roots, MediaItem.album.is_not(None))
        .group_by(owner, MediaItem.album)
        .order_by(func.count(PlayHistory.id).desc(), MediaItem.album)
        .limit(1)
    ).first()
    never_played = db.execute(
        select(func.count(MediaItem.id)).where(
            *_track_filters(roots),
            ~exists().where(
                PlayHistory.user_id == user.id,
                PlayHistory.media_item_id == MediaItem.id,
            ),
        )
    ).scalar_one()
    return {
        "plays_this_week": plays_week,
        "plays_this_year": plays_year,
        "listening_seconds_this_year": float(seconds or 0),
        "listening_capped": _listening_capped(db, user, year_start),
        "most_played_artist": {"name": artist_row[0], "plays": artist_row[1]}
        if artist_row
        else None,
        "most_played_album": (
            {"artist": album_row[0], "album": album_row[1], "plays": album_row[2]}
            if album_row
            else None
        ),
        "never_played_songs": never_played,
    }


def _listening_capped(db: Session, user: User, year_start: datetime) -> bool:
    """Whether this year's listening time may be short of the truth: the
    play-history retention (which the listen log follows) reaches into the
    year, the user's listen log is at its row cap, or plays this year
    predate the user's first logged listen."""
    days = settings.play_history_retention_days
    if days > 0 and utcnow() - timedelta(days=days) > year_start:
        return True
    count, first_listen = db.execute(
        select(func.count(ListenEvent.id), func.min(ListenEvent.started_at)).where(
            ListenEvent.user_id == user.id
        )
    ).one()
    if count >= LISTEN_EVENTS_MAX_PER_USER:
        return True
    unlogged = [PlayHistory.user_id == user.id, PlayHistory.played_at >= year_start]
    if first_listen is not None:
        unlogged.append(PlayHistory.played_at < first_listen)
    return (
        db.execute(select(PlayHistory.id).where(*unlogged).limit(1)).first() is not None
    )


# How long one folder's free-space check may take before it is reported as
# unknown. A hung network mount blocks os.stat indefinitely; the check runs on
# a worker thread so the request can give up on it.
STORAGE_CHECK_SECONDS = 3.0
_storage_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="storage-check")


def _disk_usage(path: str) -> tuple[object, int, int]:
    device: object = os.stat(path).st_dev
    usage = shutil.disk_usage(path)
    return device, usage.free, usage.total


@router.get("/stats/storage", response_model=list[StatsStorageOut])
def library_storage(db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    """Free and total disk space, split from /stats so a slow or hung mount
    can never hold up the Library info page: the client loads this after the
    page is showing."""
    return _storage(db, accessible_root_ids(db, user))


def _storage(db: Session, roots: set[int]) -> list[dict[str, Any]]:
    """Free and total space per filesystem holding an accessible root. A
    folder that cannot be read (an unmounted share) or does not answer in
    time (a hung one) reports None; this never raises."""
    rows = [
        (name, path)
        for name, path in db.execute(
            select(LibraryRoot.display_name, LibraryRoot.path)
            .where(LibraryRoot.id.in_(roots))
            .order_by(LibraryRoot.id)
        )
    ]
    # Release the connection before waiting on the filesystem.
    db.rollback()
    pending = [
        (name, path, _storage_pool.submit(_disk_usage, path)) for name, path in rows
    ]
    deadline = time.monotonic() + STORAGE_CHECK_SECONDS
    groups: dict[object, dict[str, Any]] = {}
    for name, path, future in pending:
        try:
            device, free, total = future.result(
                timeout=max(0.0, deadline - time.monotonic())
            )
        except (OSError, FutureTimeout):
            device, free, total = ("unreadable", path), None, None
        group = groups.setdefault(
            device, {"roots": [], "free_bytes": free, "total_bytes": total}
        )
        group["roots"].append(name)
    return list(groups.values())
