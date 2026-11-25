import logging
import os
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.orm import Session

from app.after_commit import run_after_commit
from app.config import art_thumb_path, settings, thumbnail_path
from app.custom_art import purge_custom_art
from app.library_cache import bump_library_version_after_commit
from app.models import (
    ActivityEvent,
    AudioFeatures,
    CollectionPreference,
    Device,
    DiscoveryState,
    Favorite,
    ListenEvent,
    MediaItem,
    MediaItemGenre,
    MediaItemMusicBrainzTag,
    MediaItemTag,
    PlaybackQueueItem,
    PlaybackState,
    PlayHistory,
    Playlist,
    PlaylistItem,
    PlaySourceHistory,
    RadioStation,
    RadioStationFavorite,
    ScrobbleQueueItem,
    TrackArtist,
    TrackDislike,
    User,
    UserIntegration,
    UserLibraryAccess,
    WantListItem,
    utcnow,
)
from app.models import (
    Session as SessionModel,
)
from app.playback import normalize_queue

logger = logging.getLogger(__name__)

# Rows deleted per transaction by prune_play_history, so the writer lock is
# released between batches (a first prune of a years-old history can be large).
PLAY_HISTORY_PRUNE_BATCH = 5000
# How many listen outcomes (app.listening) are kept per user, newest first:
# about a year of heavy daily listening, skips included (~12 MB per user).
# The preference signal halves a listen's weight every 90 days, so what is
# dropped weighs a few percent at most.
LISTEN_EVENTS_MAX_PER_USER = 50_000


def listed_media_cache_files() -> dict[int, list[Path]]:
    """Downloaded art "<id>.<ext>" and transcodes "<id>_<variant>"
    by media row id, listing each cache directory once. Globbing per id
    would list the directory once per id, which is quadratic for a big
    delete or a full rescan."""
    found: dict[int, list[Path]] = {}
    for directory, separator in (
        (settings.art_dir, "."),
        (settings.transcode_dir, "_"),
    ):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            # The names f"{id}.*" / f"{id}_*" globs would match.
            head, sep, _rest = name.partition(separator)
            if sep and head.isdecimal() and head == str(int(head)):
                found.setdefault(int(head), []).append(directory / name)
    return found


def delete_media_cache(
    media_item_ids: int | Iterable[int], listed: dict[int, list[Path]] | None = None
) -> None:
    """Idempotently remove every derived file keyed by these media row ids.
    A caller removing ids one at a time (a scan) passes one
    listed_media_cache_files() for all of them; the entries it removes are
    taken out of it."""
    ids = [media_item_ids] if isinstance(media_item_ids, int) else media_item_ids
    if listed is None:
        listed = listed_media_cache_files()
    for media_item_id in ids:
        for path in (
            thumbnail_path(media_item_id),
            art_thumb_path(media_item_id),
            *listed.pop(media_item_id, ()),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def enforce_cache_budget(max_bytes: int | None = None) -> None:
    """Evict the least recently used transcodes until the configured budget
    fits.

    "Used" is the later of access and modification time: a file that is
    served is read, which moves atime on a normal (relatime) mount. On a
    `noatime` mount atime never moves past creation, which is never later than
    mtime, so the order falls back to mtime (oldest written first).

    Only playback caches count. Thumbnails (video frames and resized art)
    and downloaded art are kept for as long as their item exists - they are
    small, every grid needs them, and evicting them would leave the
    startup backfill regenerating thousands of thumbnails from the library
    disk after every restart."""
    budget = settings.cache_max_bytes if max_bytes is None else max_bytes
    files = []
    directory = settings.transcode_dir
    if directory.is_dir():
        for path in directory.iterdir():
            try:
                if path.is_file():
                    stat = path.stat()
                    files.append((max(stat.st_atime, stat.st_mtime), stat.st_size, path))
            except OSError:
                continue
    total = sum(size for _mtime, size, _path in files)
    for _mtime, size, path in sorted(files):
        if total <= budget:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


def purge_expired_sessions(db: Session) -> int:
    """Delete sessions past `expires_at` (an indexed range delete). Lookups
    already ignore them; this only stops the table growing forever."""
    result = db.execute(
        delete(SessionModel).where(SessionModel.expires_at < utcnow()),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return cast("CursorResult[Any]", result).rowcount


def prune_play_history(
    db: Session, retention_days: int | None = None, batch_size: int = PLAY_HISTORY_PRUNE_BATCH
) -> int:
    """Delete play_history rows older than ATRIOLYRIC_PLAY_HISTORY_RETENTION_DAYS,
    `batch_size` rows per commit. 0 (the default) keeps everything: play
    counts, most played, recently played and discovery all read this table.
    Listen outcomes go by the same retention, and are capped per user
    either way (prune_listen_events). Not the play-source history, not the
    scrobble queue. Returns how many play_history rows were deleted."""
    days = settings.play_history_retention_days if retention_days is None else retention_days
    prune_listen_events(db, days, batch_size=batch_size)
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    total = 0
    while True:
        batch = select(PlayHistory.id).where(PlayHistory.played_at < cutoff).limit(batch_size)
        result = db.execute(
            delete(PlayHistory).where(PlayHistory.id.in_(batch.scalar_subquery())),
            execution_options={"synchronize_session": False},
        )
        deleted = cast("CursorResult[Any]", result).rowcount or 0
        db.commit()
        total += deleted
        if deleted < batch_size:
            break
    if total:
        logger.info("Pruned %d play history rows older than %d days", total, days)
    return total


def prune_listen_events(
    db: Session,
    retention_days: int,
    max_per_user: int = LISTEN_EVENTS_MAX_PER_USER,
    batch_size: int = PLAY_HISTORY_PRUNE_BATCH,
) -> int:
    """Delete listen_events rows started more than `retention_days` ago (0:
    none for age) and each user's rows beyond the newest `max_per_user`.
    Commits only when something was deleted. Returns how many rows went."""
    total = 0
    if retention_days > 0:
        cutoff = utcnow() - timedelta(days=retention_days)
        while True:
            batch = select(ListenEvent.id).where(ListenEvent.started_at < cutoff).limit(batch_size)
            result = db.execute(
                delete(ListenEvent).where(ListenEvent.id.in_(batch.scalar_subquery())),
                execution_options={"synchronize_session": False},
            )
            deleted = cast("CursorResult[Any]", result).rowcount or 0
            if deleted:
                db.commit()
            total += deleted
            if deleted < batch_size:
                break
    over = db.execute(
        select(ListenEvent.user_id)
        .group_by(ListenEvent.user_id)
        .having(func.count() > max_per_user)
    ).scalars().all()
    for user_id in over:
        # Everything after the newest max_per_user of this user's rows.
        oldest_kept = (
            select(ListenEvent.id)
            .where(ListenEvent.user_id == user_id)
            .order_by(ListenEvent.started_at.desc(), ListenEvent.id.desc())
            .limit(-1)
            .offset(max_per_user)
        )
        result = db.execute(
            delete(ListenEvent).where(ListenEvent.id.in_(oldest_kept.scalar_subquery())),
            execution_options={"synchronize_session": False},
        )
        total += cast("CursorResult[Any]", result).rowcount or 0
        db.commit()
    if total:
        logger.info("Pruned %d listen events", total)
    return total


def prune_activity_events(
    db: Session,
    retention_days: int | None = None,
    max_rows: int | None = None,
    batch_size: int = PLAY_HISTORY_PRUNE_BATCH,
) -> int:
    """Delete activity log rows older than ATRIOLYRIC_ACTIVITY_LOG_RETENTION_DAYS
    (0: none for age), then the oldest beyond ATRIOLYRIC_ACTIVITY_LOG_MAX_ROWS,
    `batch_size` rows per commit so the writer lock is released between
    batches. Returns how many rows went."""
    days = settings.activity_log_retention_days if retention_days is None else retention_days
    cap = settings.activity_log_max_rows if max_rows is None else max_rows
    total = 0
    if days > 0:
        cutoff = utcnow() - timedelta(days=days)
        while True:
            batch = select(ActivityEvent.id).where(ActivityEvent.created_at < cutoff).limit(batch_size)
            result = db.execute(
                delete(ActivityEvent).where(ActivityEvent.id.in_(batch.scalar_subquery())),
                execution_options={"synchronize_session": False},
            )
            deleted = cast("CursorResult[Any]", result).rowcount or 0
            if deleted:
                db.commit()
            total += deleted
            if deleted < batch_size:
                break
    # Ids only grow, so everything below the newest `cap` rows' lowest id.
    oldest_kept = db.execute(
        select(ActivityEvent.id).order_by(ActivityEvent.id.desc()).limit(1).offset(cap - 1)
    ).scalar()
    if oldest_kept is not None:
        while True:
            batch = select(ActivityEvent.id).where(ActivityEvent.id < oldest_kept).limit(batch_size)
            result = db.execute(
                delete(ActivityEvent).where(ActivityEvent.id.in_(batch.scalar_subquery())),
                execution_options={"synchronize_session": False},
            )
            deleted = cast("CursorResult[Any]", result).rowcount or 0
            if deleted:
                db.commit()
            total += deleted
            if deleted < batch_size:
                break
    db.commit()
    if total:
        logger.info("Pruned %d activity log rows", total)
    return total


def delete_media_records(db: Session, media_item_ids: Iterable[int]) -> None:
    """Remove media rows and every dependent row from existing SQLite schemas.

    The initial schema predates database-level ON DELETE cascades, so cleanup has
    to be explicit to avoid dangling playlist, queue, like/dislike, and history rows.
    """
    ids = set(media_item_ids)
    if not ids:
        return
    purge_custom_art(db, "media_item", ids)
    # A wanted song whose track is removed goes back to wanted, so a later
    # scan can match it again.
    db.execute(
        update(WantListItem)
        .where(WantListItem.media_item_id.in_(ids))
        .values({WantListItem.media_item_id: None}),
        execution_options={"synchronize_session": False},
    )
    # Queue rows go through normalize_queue so each affected user's index
    # stays on the same track (a plain delete would leave it pointing N rows on).
    # One query finds the rows, then one queue query per affected user.
    queued_by_user: dict[int, set[int]] = {}
    for item_id, user_id in db.execute(
        select(PlaybackQueueItem.id, PlaybackQueueItem.user_id).where(
            PlaybackQueueItem.media_item_id.in_(ids)
        )
    ):
        queued_by_user.setdefault(user_id, set()).add(item_id)
    for user_id, item_ids in queued_by_user.items():
        normalize_queue(db, user_id, item_ids, stop_if_current_removed=True)
    for model in (
        MediaItemGenre,
        MediaItemTag,
        MediaItemMusicBrainzTag,
        Favorite,
        TrackDislike,
        PlaylistItem,
        PlayHistory,
        ListenEvent,
        PlaybackQueueItem,
        TrackArtist,
        AudioFeatures,
    ):
        db.execute(
            delete(model).where(model.media_item_id.in_(ids)),
            execution_options={"synchronize_session": False},
        )
    db.execute(
        update(PlaybackState)
        .where(PlaybackState.media_item_id.in_(ids))
        .values({PlaybackState.media_item_id: None, PlaybackState.is_playing: False}),
        execution_options={"synchronize_session": False},
    )
    db.execute(
        delete(MediaItem).where(MediaItem.id.in_(ids)),
        execution_options={"synchronize_session": "fetch"},
    )
    # Only once the rows are really gone: a rollback keeps the rows, and
    # with them their art and transcodes.
    run_after_commit(db, "media_cache", delete_media_cache, ids)
    # Album/artist/track groupings may have changed - drop the cached
    # library listings once the caller commits, so the next request
    # recomputes them from what is really there.
    bump_library_version_after_commit(db)


# Rows per transaction when a whole library is removed (the scan removes
# missing files in batches of the same size, SCAN_COMMIT_BATCH_SIZE).
MEDIA_DELETE_BATCH_SIZE = 200


def delete_root_media_records(db: Session, root_id: int) -> int:
    """Remove every media row of a library root, a batch and a commit at a
    time; returns how many.

    In one transaction, every track id of the library would be bound into
    each of delete_media_records' statements (130,000 parameters apiece, more
    than SQLite accepts unless the build raises its limit) and the write lock
    held from the first statement to the commit, so every other write in the
    app would wait behind it, up to its 30-second busy timeout. Committing per
    batch lets them in between. If it stops midway, the rows not reached yet
    are intact and still belong to the root, which the caller removes last."""
    removed = 0
    while True:
        ids = list(
            db.scalars(
                select(MediaItem.id)
                .where(MediaItem.library_root_id == root_id)
                .order_by(MediaItem.id)
                .limit(MEDIA_DELETE_BATCH_SIZE)
            )
        )
        if not ids:
            return removed
        delete_media_records(db, ids)
        db.commit()
        removed += len(ids)


def delete_user_records(db: Session, user_id: int) -> None:
    owned_station_ids = set(
        db.scalars(select(RadioStation.id).where(RadioStation.owner_user_id == user_id))
    )
    radio_filter = RadioStationFavorite.user_id == user_id
    if owned_station_ids:
        radio_filter = radio_filter | RadioStationFavorite.station_id.in_(
            owned_station_ids
        )
    db.execute(
        delete(RadioStationFavorite).where(radio_filter),
        execution_options={"synchronize_session": False},
    )
    playlist_ids = set(db.scalars(select(Playlist.id).where(Playlist.user_id == user_id)))
    if playlist_ids:
        purge_custom_art(db, "playlist", playlist_ids)
        db.execute(
            delete(PlaylistItem).where(PlaylistItem.playlist_id.in_(playlist_ids)),
            execution_options={"synchronize_session": False},
        )
    for model in (
        SessionModel,
        UserLibraryAccess,
        Favorite,
        TrackDislike,
        CollectionPreference,
        UserIntegration,
        PlayHistory,
        ListenEvent,
        PlaySourceHistory,
        PlaybackQueueItem,
        PlaybackState,
        Device,
        Playlist,
        WantListItem,
        DiscoveryState,
        ScrobbleQueueItem,
    ):
        db.execute(
            delete(model).where(model.user_id == user_id),
            execution_options={"synchronize_session": False},
        )
    db.execute(
        delete(RadioStation).where(RadioStation.owner_user_id == user_id),
        execution_options={"synchronize_session": False},
    )
    db.execute(delete(User).where(User.id == user_id), execution_options={"synchronize_session": False})
