"""Library background tasks: scans and the metadata pipeline that run for
one library root in a worker thread, with progress, stop and failure kept
on their LibraryTask row for the admin page (app.routers.admin has the
endpoints).

Each task kind maps to its worker in TASK_WORKERS. Every worker runs
through _run_guarded, so a task always ends with a final status and its
stop event released, even when the worker's own failure handling fails."""

import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import cast

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session, undefer
from sqlalchemy.orm.interfaces import ORMOption

from app.activity import log_action
from app.art_candidates import apply_commons_photo
from app.art_epoch import bump_art_epoch
from app.artist_credits import OWNING_ROLES
from app.background import spawn
from app.db import SessionLocal
from app.enrichment import (
    _lookup_recording,
    album_refresh_batch,
    apply_listenbrainz_tags,
    enrich_media_item,
    fetch_cover_art_for_item,
    refresh_batched_albums,
    split_known_genres,
)
from app.folder_exclusions import not_in_ignored_folder
from app.library_cache import bump_library_version
from app.listenbrainz_data import artist_top_tracks
from app.loudness import start_loudness_backfill
from app.metadata_info import artist_info
from app.metadata_providers import listenbrainz, musicbrainz
from app.metadata_providers._throttle import bulk
from app.metadata_providers.result import recording_failures
from app.models import (
    Artist,
    LibraryRoot,
    LibraryTask,
    MediaItem,
    MediaKind,
    TrackArtist,
    User,
    utcnow,
)
from app.scanner import scan_library_root
from app.want_list import match_want_list

logger = logging.getLogger(__name__)


def active_task(db: Session, root_id: int) -> LibraryTask | None:
    return db.scalars(
        select(LibraryTask)
        .where(
            LibraryTask.library_root_id == root_id,
            LibraryTask.status.in_(("running", "stopping")),
        )
        .order_by(LibraryTask.started_at.desc())
        .limit(1)
    ).first()



_task_start_lock = threading.Lock()
_task_stop_events: dict[int, threading.Event] = {}


def _task_stop_requested(db: Session, task_id: int) -> bool:
    event = _task_stop_events.get(task_id)
    if event is not None and event.is_set():
        return True
    status = db.execute(
        select(LibraryTask.status).where(LibraryTask.id == task_id)
    ).scalar_one_or_none()
    return status == "stopping"


def _save_progress(
    db: Session,
    task: LibraryTask,
    processed: int,
    total: int,
    last_saved_at: list[float],
) -> None:
    task.processed = processed
    task.total = total
    task.updated_at = utcnow()
    now = time.monotonic()
    # Persist often enough for a useful progress bar, without turning a large
    # scan of unchanged files into thousands of database writes.
    if processed == total or now - last_saved_at[0] >= 0.75:
        db.commit()
        last_saved_at[0] = now


# Bulk provider tasks run for hours: they list the ids up front and load the
# rows this many at a time, so no task holds a whole root's rows (and their
# large JSON columns) for its whole run.
ITEM_CHUNK = 200


def _item_ids(db: Session, query: Select) -> list[int]:
    return list(db.scalars(query.with_only_columns(MediaItem.id).order_by(MediaItem.id)))


def _commit_before_provider_call(db: Session) -> None:
    """Commit what the previous item wrote before the caller asks a provider
    about the next one. A provider call can take seconds (longer with a
    rate-limit pause); made inside an open write transaction it would hold
    SQLite's single write lock that whole time and stall every other writer.
    The progress commits are throttled (see _save_progress), so this is what
    ends each item's transaction. A commit with nothing written is free."""
    db.commit()


def _iter_items(
    db: Session, ids: Sequence[int], options: Sequence[ORMOption] = ()
) -> Iterator[tuple[int, MediaItem | None]]:
    """(position, row) for each id in order, loading ITEM_CHUNK rows at a
    time with `options` (undefer the deferred columns the worker reads).
    The row is None when it was deleted after the ids were listed."""
    for start in range(0, len(ids), ITEM_CHUNK):
        chunk = ids[start : start + ITEM_CHUNK]
        rows = {
            row.id: row
            for row in db.scalars(select(MediaItem).options(*options).where(MediaItem.id.in_(chunk)))
        }
        for offset, item_id in enumerate(chunk, start=start + 1):
            _commit_before_provider_call(db)
            yield offset, rows.pop(item_id, None)


# A bulk provider task rides out a provider outage or rate limit: each
# failure in a row pauses it a little longer (2, 4, 8 ... s, at most
# FAILURE_PAUSE_CAP). It stops only once a streak of at least
# PROVIDER_FAILURE_LIMIT failures has spent OUTAGE_PAUSE_BUDGET pausing
# (about 15 minutes of failures) - a long outage, where marching on would
# only mark every remaining item "retry". A task over a big library runs for
# hours, so a minute-long blip at the provider must not end it.
PROVIDER_FAILURE_LIMIT = 5
FAILURE_PAUSE_CAP = 300.0
OUTAGE_PAUSE_BUDGET = 900.0


def _failure_pause(task_id: int, seconds: float) -> None:
    """Sleep, but wake at once when the task is asked to stop."""
    event = _task_stop_events.get(task_id)
    if event is not None:
        event.wait(seconds)
    else:
        time.sleep(seconds)


class _FailureStreak:
    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        self.count = 0
        self.paused = 0.0

    def record(self, failure: str | None) -> str | None:
        """One item's outcome: `failure` is the provider's reason when it
        failed, None otherwise. Pauses after a failure; returns the task's
        error message once the streak is a long outage (see
        OUTAGE_PAUSE_BUDGET)."""
        if failure is None:
            self.count = 0
            self.paused = 0.0
            return None
        self.count += 1
        if self.count >= PROVIDER_FAILURE_LIMIT and self.paused >= OUTAGE_PAUSE_BUDGET:
            minutes = round(self.paused / 60)
            return (
                f"Stopped after {self.count} provider failures in a row over about {minutes} minutes "
                f"(last: {failure}). The service may be down or rate limiting - try again later."
            )
        seconds = min(2.0**self.count, FAILURE_PAUSE_CAP)
        self.paused += seconds
        _failure_pause(self.task_id, seconds)
        return None


# Full setup tasks between steps: a step "completing" leaves them running
# for the next one (see _run_full_setup_task).
_continuing_tasks: set[int] = set()


def _finish_task(
    db: Session, task: LibraryTask, status: str, error: str | None = None
) -> None:
    if status == "completed" and task.id in _continuing_tasks:
        task.updated_at = utcnow()
        db.commit()
        return
    task.status = status
    task.error = error
    now = utcnow()
    task.completed_at = now
    task.updated_at = now
    db.commit()
    log_action(
        "task.finish", level=logging.ERROR if status == "failed" else logging.INFO,
        task=task.id, root=task.library_root_id, kind=task.kind, status=status,
        processed=task.processed, total=task.total, error=error,
        added=task.added, updated=task.updated, removed=task.removed,
    )


def _held_removal_note(missing_held: int, unreadable_dirs: list[str]) -> str | None:
    """What the admin sees when a scan kept tracks it could not find. The
    task still completes: the adds and updates were saved."""
    if not missing_held:
        return None
    tracks = "1 track looks" if missing_held == 1 else f"{missing_held} tracks look"
    note = (
        f"{tracks} missing - the folder may be unmounted or unreadable. "
        "Nothing was removed."
    )
    if unreadable_dirs:
        note += f" Could not read: {', '.join(unreadable_dirs)}"
    return note


def _run_scan_task(root_id: int, task_id: int, mode: str = "normal") -> None:
    db = SessionLocal()
    try:
        root = db.get(LibraryRoot, root_id)
        task = db.get(LibraryTask, task_id)
        if root is None or task is None:
            return
        last_saved_at = [0.0]

        def report(processed: int, total: int) -> None:
            _save_progress(db, task, processed, total, last_saved_at)

        root.last_scanned_at = utcnow()
        db.commit()
        result = scan_library_root(
            db, root, report, mode, should_stop=lambda: _task_stop_requested(db, task_id)
        )
        task.added = result["added"]
        task.updated = result["updated"]
        task.removed = result["removed"]
        task.missing_held = result.get("missing_held", 0)
        task.note = _held_removal_note(task.missing_held, result.get("unreadable_dirs", []))
        root.last_scan_error = None
        if result["added"] or result["updated"] or result["removed"]:
            # A no-op rescan (nothing added/updated/removed) can't have
            # changed anything the cached library listings show, so skip
            # the bump rather than needlessly cooling a warm cache. After a
            # real change app.cache_warmup refills the slow entries (tab
            # counts, the Albums and Artists lists, genre and tag counts,
            # Library info) in the background; the rest refill on the
            # next request.
            bump_library_version()
        if result.get("art_redone"):
            # Open apps hold a day-old copy of each of these tracks' art.
            bump_art_epoch(db)
        if result["added"] or result["updated"]:
            match_want_list(db)
            start_loudness_backfill()
        # A stopped scan inside a full setup ends the whole run, as a Stop
        # during any other step does (see _run_full_setup_task).
        _finish_task(db, task, "stopped" if result.get("stopped") else "completed")
    except Exception as exc:  # persist task failures for the UI
        logger.exception("Library task %s failed (root %s)", task_id, root_id)
        db.rollback()
        task = db.get(LibraryTask, task_id)
        root = db.get(LibraryRoot, root_id)
        if root is not None:
            root.last_scan_error = str(exc) or type(exc).__name__
            root.last_scanned_at = utcnow()
        if task is not None:  # pragma: no branch - task can disappear only via external DB tampering
            _finish_task(db, task, "failed", str(exc) or type(exc).__name__)
        elif root is not None:
            db.commit()
    finally:
        _task_stop_events.pop(task_id, None)
        db.close()


def _run_enrichment_task(
    root_id: int,
    task_id: int,
    refresh_all: bool = False,
) -> None:
    db = SessionLocal()
    try:
        task = db.get(LibraryTask, task_id)
        if task is None:
            return
        query = select(MediaItem).where(
            MediaItem.library_root_id == root_id,
            MediaItem.kind == MediaKind.track,
            not_in_ignored_folder(),
        )
        if not refresh_all:  # pragma: no branch - explicit task modes cover this path
            query = query.where(MediaItem.musicbrainz_metadata_json.is_(None))
        ids = _item_ids(db, query)
        task.total = len(ids)
        task.updated_at = utcnow()
        db.commit()
        last_saved_at = [0.0]
        matched = 0
        streak = _FailureStreak(task.id)
        # Each album touched is re-derived once per chunk, not once per track
        # (enrich_media_item defers it to the batch). No undefer: the only
        # read of musicbrainz_metadata_json follows enrich setting it.
        with album_refresh_batch(db):
            for processed, item in _iter_items(db, ids):
                if item is None:  # deleted since the ids were listed
                    _save_progress(db, task, processed, len(ids), last_saved_at)
                    continue
                result = enrich_media_item(db, item, commit=False, fetch_cover_art=False)
                if result.get("metadata_saved", result.get("matched")):
                    matched += 1
                task.matched = matched
                if processed % ITEM_CHUNK == 0:
                    refresh_batched_albums(db)
                _save_progress(db, task, processed, len(ids), last_saved_at)
                failure = item.musicbrainz_last_error or "MusicBrainz failed"
                stop_error = streak.record(failure if result.get("status") == "retry" else None)
                if stop_error:
                    refresh_batched_albums(db)
                    _finish_task(db, task, "failed", stop_error)
                    return
                if _task_stop_requested(db, task.id):
                    refresh_batched_albums(db)
                    _finish_task(db, task, "stopped")
                    return
            refresh_batched_albums(db)
        _finish_task(db, task, "completed")
    except Exception as exc:  # persist task failures for the UI
        logger.exception("Library task %s failed (root %s)", task_id, root_id)
        db.rollback()
        task = db.get(LibraryTask, task_id)
        if task is not None:  # pragma: no branch - task can disappear only via external DB tampering
            _finish_task(db, task, "failed", str(exc) or type(exc).__name__)
    finally:
        _task_stop_events.pop(task_id, None)
        db.close()
        # Genres, tags, album types and dates feed the cached listings;
        # whatever this run committed (all of it, or up to a stop or a
        # failure) must show.
        bump_library_version()


def _run_mbid_lookup_task(
    root_id: int, task_id: int, *, resolve_names: bool = True
) -> None:
    """Resolve missing recording MBIDs using ISRC, then artist/title search."""
    db = SessionLocal()
    try:
        task = db.get(LibraryTask, task_id)
        if task is None:
            return
        query = select(MediaItem).where(
            MediaItem.library_root_id == root_id,
            MediaItem.kind == MediaKind.track,
            MediaItem.musicbrainz_recording_mbid.is_(None),
            not_in_ignored_folder(),
        )
        if not resolve_names:
            query = query.where(MediaItem.musicbrainz_isrc.is_not(None))
        ids = _item_ids(db, query)
        task.total = len(ids)
        db.commit()
        saved_at, matched = [0.0], 0
        streak = _FailureStreak(task.id)
        for processed, item in _iter_items(db, ids):
            if item is None:  # deleted since the ids were listed
                _save_progress(db, task, processed, len(ids), saved_at)
                continue
            result = (
                _lookup_recording(item)
                if resolve_names
                else musicbrainz.lookup_recording_by_isrc(item.musicbrainz_isrc)
            )
            if result.value and result.value.get("recording_mbid"):
                item.musicbrainz_recording_mbid = result.value["recording_mbid"]
                item.musicbrainz_artist_mbid = result.value.get("artist_mbid")
                item.musicbrainz_lookup_status = "resolved"
                item.musicbrainz_last_error = None
                item.musicbrainz_last_attempt_at = utcnow()
                matched += 1
            elif result.error:
                item.musicbrainz_lookup_status = "retry"
                item.musicbrainz_last_error = result.error
                item.musicbrainz_last_attempt_at = utcnow()
            else:
                item.musicbrainz_lookup_status = "no_match"
                item.musicbrainz_last_error = None
                item.musicbrainz_last_attempt_at = utcnow()
            task.matched = matched
            _save_progress(db, task, processed, len(ids), saved_at)
            stop_error = streak.record(result.error)
            if stop_error:
                _finish_task(db, task, "failed", stop_error)
                return
            if _task_stop_requested(db, task.id):
                _finish_task(db, task, "stopped")
                return
        _finish_task(db, task, "completed")
    except Exception as exc:  # persist task failures for the UI
        logger.exception("Library task %s failed (root %s)", task_id, root_id)
        db.rollback()
        task = db.get(LibraryTask, task_id)
        if task is not None:  # pragma: no branch - task can disappear only via external DB tampering
            _finish_task(db, task, "failed", str(exc) or type(exc).__name__)
    finally:
        _task_stop_events.pop(task_id, None)
        db.close()


# Tracks whose artists' tags are fetched in one ListenBrainz request.
_TAG_BATCH = listenbrainz.MBID_BATCH


def _run_listenbrainz_tags_task(root_id: int, task_id: int, refresh_all: bool = False) -> None:
    """Add each track's artist tags from ListenBrainz (the artist's
    MusicBrainz community tags), for tracks whose artist has a MusicBrainz
    ID - the MusicBrainz IDs step sets them. Artists are asked in batches."""
    db = SessionLocal()
    try:
        task = db.get(LibraryTask, task_id)
        if task is None:
            return
        query = select(MediaItem).where(
            MediaItem.library_root_id == root_id,
            MediaItem.kind == MediaKind.track,
            MediaItem.musicbrainz_artist_mbid.is_not(None),
            not_in_ignored_folder(),
        )
        if not refresh_all:
            query = query.where(MediaItem.online_tags_fetched_at.is_(None))
        ids = _item_ids(db, query)
        task.total = len(ids)
        db.commit()
        saved_at, matched = [0.0], 0
        streak = _FailureStreak(task.id)
        for start in range(0, len(ids), _TAG_BATCH):
            # One query per batch; a row deleted since the ids were listed
            # is simply not there.
            batch = list(db.scalars(select(MediaItem).where(MediaItem.id.in_(ids[start : start + _TAG_BATCH]))))
            _commit_before_provider_call(db)
            # The track's (first) artist; a malformed tag value is never sent.
            artist_of = {item.id: listenbrainz.first_mbid(item.musicbrainz_artist_mbid) for item in batch}
            mbids = sorted({mbid for mbid in artist_of.values() if mbid})
            with bulk():
                tags = listenbrainz.artist_tags(mbids)
            stop_error = streak.record(None if tags is not None else "ListenBrainz did not answer")
            if tags is not None:
                splits = {mbid: split_known_genres(db, rows) for mbid, rows in tags.items()}
                for item in batch:
                    mbid = artist_of[item.id] or ""
                    found = tags.get(mbid)
                    apply_listenbrainz_tags(db, item, found or [], splits.get(mbid))
                    if found:
                        matched += 1
                db.commit()
            task.matched = matched
            _save_progress(db, task, min(start + _TAG_BATCH, len(ids)), len(ids), saved_at)
            if stop_error:
                _finish_task(db, task, "failed", stop_error)
                return
            if _task_stop_requested(db, task.id):
                _finish_task(db, task, "stopped")
                return
        _finish_task(db, task, "completed")
    except Exception as exc:  # persist task failures for the UI
        logger.exception("Library task %s failed (root %s)", task_id, root_id)
        db.rollback()
        task = db.get(LibraryTask, task_id)
        if task is not None:  # pragma: no branch - task can disappear only via external DB tampering
            _finish_task(db, task, "failed", str(exc) or type(exc).__name__)
    finally:
        _task_stop_events.pop(task_id, None)
        db.close()
        # Genres, tags, album types and dates feed the cached listings;
        # whatever this run committed (all of it, or up to a stop or a
        # failure) must show.
        bump_library_version()


def _run_cover_art_task(root_id: int, task_id: int) -> None:
    db = SessionLocal()
    matched = 0
    try:
        task = db.get(LibraryTask, task_id)
        if task is None:
            return
        ids = _item_ids(
            db,
            select(MediaItem).where(
                MediaItem.library_root_id == root_id,
                MediaItem.kind == MediaKind.track,
                MediaItem.musicbrainz_metadata_json.is_not(None),
                MediaItem.cover_art_url.is_(None),
                not_in_ignored_folder(),
            )
        )
        task.total = len(ids)
        task.updated_at = utcnow()
        db.commit()
        last_saved_at = [0.0]
        streak = _FailureStreak(task.id)
        # The release id comes from the stored recording.
        for processed, item in _iter_items(db, ids, [undefer(MediaItem.musicbrainz_metadata_json)]):
            if item is None:  # deleted since the ids were listed
                _save_progress(db, task, processed, len(ids), last_saved_at)
                continue
            result = fetch_cover_art_for_item(db, item, commit=False)
            if result.get("fetched"):
                matched += 1
            task.matched = matched
            _save_progress(db, task, processed, len(ids), last_saved_at)
            stop_error = streak.record(result.get("error") if result.get("failed") else None)
            if stop_error:
                _finish_task(db, task, "failed", stop_error)
                return
            if _task_stop_requested(db, task.id):
                _finish_task(db, task, "stopped")
                return
        _finish_task(db, task, "completed")
    except Exception as exc:  # persist task failures for the UI
        logger.exception("Library task %s failed (root %s)", task_id, root_id)
        db.rollback()
        task = db.get(LibraryTask, task_id)
        if task is not None:
            _finish_task(db, task, "failed", str(exc) or type(exc).__name__)
    finally:
        _task_stop_events.pop(task_id, None)
        try:
            if matched:
                # Tracks that answered "no art" now have a cover to download.
                bump_art_epoch(db)
        except Exception:  # the art itself is saved; only its cache-busting is late
            logger.exception("Could not bump the art epoch after task %s", task_id)
            db.rollback()
        db.close()


def _run_artist_info_task(root_id: int, task_id: int, user_id: int) -> None:
    """Fetch and cache info and top tracks for every artist on this root, so
    artist pages open without waiting (app.metadata_info), and give each
    artist without a picture its Wikimedia Commons photo
    (app.art_candidates.apply_commons_photo)."""
    db = SessionLocal()
    try:
        task = db.get(LibraryTask, task_id)
        user = db.get(User, user_id)
        if task is None or user is None:
            return
        names = list(
            db.execute(
                select(Artist.name)
                .join(TrackArtist, TrackArtist.artist_id == Artist.id)
                .join(MediaItem, MediaItem.id == TrackArtist.media_item_id)
                .where(
                    MediaItem.library_root_id == root_id,
                    MediaItem.kind == MediaKind.track,
                    TrackArtist.role.in_(OWNING_ROLES),
                )
                .distinct()
                .order_by(Artist.name)
            ).scalars()
        )
        task.total = len(names)
        db.commit()
        saved_at, matched, photos = [0.0], 0, 0
        streak = _FailureStreak(task.id)
        for processed, name in enumerate(names, start=1):
            _commit_before_provider_call(db)
            # Several providers per artist: any of them failing counts.
            with recording_failures() as failures:
                # Unscoped: the task is admin-initiated and limited to this
                # root, which the admin need not have granted themselves.
                info = artist_info(db, user, name, scoped=False)
                artist_top_tracks(db, user, name)
                if apply_commons_photo(db, name, info.get("mbid")):
                    photos += 1
                    if photos % 25 == 0:
                        bump_library_version()  # pictures show while the task runs
            if info.get("bio") or info.get("mbid"):
                matched += 1
            task.matched = matched
            _save_progress(db, task, processed, len(names), saved_at)
            stop_error = streak.record(failures[-1] if failures else None)
            if stop_error:
                _finish_task(db, task, "failed", stop_error)
                return
            if _task_stop_requested(db, task.id):
                _finish_task(db, task, "stopped")
                return
        _finish_task(db, task, "completed")
    except Exception as exc:  # persist task failures for the UI
        logger.exception("Library task %s failed (root %s)", task_id, root_id)
        db.rollback()
        task = db.get(LibraryTask, task_id)
        if task is not None:
            _finish_task(db, task, "failed", str(exc) or type(exc).__name__)
    finally:
        _task_stop_events.pop(task_id, None)
        db.close()
        # Artist pictures saved by this run must show in the listings.
        bump_library_version()


def _run_full_setup_task(
    root_id: int, task_id: int, user_id: int, new_only: bool = False
) -> None:
    """Everything, in order, as one task: a full rescan, MusicBrainz IDs,
    MusicBrainz metadata for every track, ListenBrainz artist tags for every
    track, missing cover art, then artist info. For a
    first setup, or to rebuild after something went wrong.

    Each step is the same worker its own button runs. A step finishing
    re-arms the task for the next one, so a Stop (or a failure) ends the
    whole run where it is.

    new_only is the same pipeline for a library that already went through a
    full setup: a normal scan, then every step limited to what is still
    missing (so a newly added artist gets everything, old tracks are left
    alone)."""
    db = SessionLocal()
    try:
        task = db.get(LibraryTask, task_id)
        user = db.get(User, user_id)
        if task is None or user is None:
            return
    finally:
        db.close()
    stop_event = _task_stop_events.get(task_id) or threading.Event()
    steps: list[tuple[str, Callable[[], None]]] = [
        (
            "Scan" if new_only else "Full rescan",
            lambda: _run_scan_task(root_id, task_id, "normal" if new_only else "full"),
        ),
        ("MusicBrainz IDs", lambda: _run_mbid_lookup_task(root_id, task_id)),
        ("MusicBrainz metadata", lambda: _run_enrichment_task(root_id, task_id, refresh_all=not new_only)),
    ]
    steps += [
        ("ListenBrainz tags", lambda: _run_listenbrainz_tags_task(root_id, task_id, not new_only)),
        ("Cover art", lambda: _run_cover_art_task(root_id, task_id)),
        ("Artist info", lambda: _run_artist_info_task(root_id, task_id, user_id)),
    ]
    for number, (label, run) in enumerate(steps, start=1):
        db = SessionLocal()
        try:
            task = db.get(LibraryTask, task_id)
            if task is None:
                return
            if task.status != "running" or stop_event.is_set():
                # Stopped between steps.
                _finish_task(db, task, "stopped")
                return
            task.stage = f"{number}/{len(steps)} {label}"
            task.processed = task.total = task.matched = 0
            # Each step's own rate and ETA.
            task.started_at = task.updated_at = utcnow()
            db.commit()
        finally:
            db.close()
        # Workers drop the stop event when they end; the next step needs it.
        _task_stop_events[task_id] = stop_event
        last = number == len(steps)
        if not last:
            _continuing_tasks.add(task_id)
        try:
            run()
        finally:
            _continuing_tasks.discard(task_id)
        db = SessionLocal()
        try:
            task = db.get(LibraryTask, task_id)
            if task is None or task.status in ("stopped", "failed"):
                return  # the task already says why
        finally:
            db.close()
    db = SessionLocal()
    try:
        task = db.get(LibraryTask, task_id)
        if task is not None:
            task.stage = "Done"
            db.commit()
    finally:
        db.close()
        _task_stop_events.pop(task_id, None)


def _fail_task_last_resort(task_id: int, error: str) -> None:
    """Mark a task failed from a fresh session. For when a worker's own
    failure handler failed too (its commit hit a locked database, say):
    without this the task would show as running until the next restart and
    block every other task for its root."""
    db = SessionLocal()
    try:
        now = utcnow()
        db.execute(
            update(LibraryTask)
            .where(LibraryTask.id == task_id, LibraryTask.status.in_(("running", "stopping")))
            .values({"status": "failed", "error": error, "completed_at": now, "updated_at": now}),
            execution_options={"synchronize_session": False},
        )
        db.commit()
    except Exception:  # nothing further can be done; the restart sweep settles it
        logger.exception("Library task %s could not be marked failed", task_id)
    finally:
        db.close()


def _run_guarded(worker: Callable[..., None], task_id: int, *args: object) -> None:
    """Thread entry for every task: whatever escapes the worker (which only
    happens when its failure handler failed) still ends the task, and the
    stop event is always released."""
    try:
        worker(*args)
    except Exception as exc:
        logger.exception("Library task %s could not record its outcome", task_id)
        _fail_task_last_resort(task_id, str(exc) or type(exc).__name__)
    finally:
        _task_stop_events.pop(task_id, None)


@dataclass(frozen=True)
class TaskOptions:
    """What some task kinds need beyond the root: the scan mode, and the
    admin the artist info is fetched for."""

    scan_mode: str = "normal"
    user_id: int | None = None


# Workers are looked up by name when a task starts, so a test can patch one.
TASK_WORKERS: dict[str, Callable[[int, int, TaskOptions], None]] = {
    "scan": lambda root_id, task_id, o: _run_scan_task(root_id, task_id, o.scan_mode),
    # ISRC lookups only: an MBID lookup that does not resolve names.
    "mb_isrc": lambda root_id, task_id, o: _run_mbid_lookup_task(
        root_id, task_id, resolve_names=False
    ),
    "mb_mbid": lambda root_id, task_id, o: _run_mbid_lookup_task(root_id, task_id),
    "artist_info": lambda root_id, task_id, o: _run_artist_info_task(
        root_id, task_id, cast(int, o.user_id)
    ),
    "full_setup": lambda root_id, task_id, o: _run_full_setup_task(
        root_id, task_id, cast(int, o.user_id), False
    ),
    "full_setup_new": lambda root_id, task_id, o: _run_full_setup_task(
        root_id, task_id, cast(int, o.user_id), True
    ),
    "lb_tags": lambda root_id, task_id, o: _run_listenbrainz_tags_task(root_id, task_id, False),
    "lb_tags_all": lambda root_id, task_id, o: _run_listenbrainz_tags_task(root_id, task_id, True),
    "enrich": lambda root_id, task_id, o: _run_enrichment_task(root_id, task_id, False),
    "enrich_all": lambda root_id, task_id, o: _run_enrichment_task(root_id, task_id, True),
}


def start_task(
    db: Session, root_id: int, kind: str, options: TaskOptions | None = None
) -> LibraryTask | None:
    """Record a running task of `kind` for the root and start its worker.
    None when a task is already running for the root."""
    worker = TASK_WORKERS[kind]
    with _task_start_lock:
        if active_task(db, root_id) is not None:
            return None
        task = LibraryTask(library_root_id=root_id, kind=kind, status="running")
        db.add(task)
        db.commit()
        db.refresh(task)
        _task_stop_events[task.id] = threading.Event()
        mode = (options or TaskOptions()).scan_mode if kind == "scan" else None
        log_action("task.start", task=task.id, root=root_id, kind=kind, mode=mode)
        spawn(
            f"library-task-{task.id}", _run_guarded,
            worker, task.id, root_id, task.id, options or TaskOptions(),
        )
        return task


def request_stop(task_id: int) -> None:
    """Wake the worker of a task that was just marked stopping."""
    event = _task_stop_events.get(task_id)
    if event is not None:
        event.set()
