"""How far the two background passes over the library have got: the
loudness backfill (app.loudness) and audio analysis (app.audio_analysis).

``library_counts`` is one aggregate query over the tracks (a left join from
media_items to audio_features on its primary key: one pass over the table,
no per-track work). ``background_status`` adds whether each pass is running
right now and its rate over the current run, from counters the passes
already keep (``RunProgress``, updated on each progress log line), and
caches the counts for ``CACHE_SECONDS`` so a page polling it cannot load
the database. The Admin page never waits for that query: it is given the
counts in hand and, when they are older than ``CACHE_SECONDS``, a
background thread fetches new ones for its next poll (``polled_counts``).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Literal, cast

from sqlalchemy import Table, and_, case, func, select
from sqlalchemy.orm import Session

from app.audio_features import ANALYSIS_VERSION
from app.background import spawn
from app.db import SessionLocal
from app.models import AudioFeatures, MediaItem, MediaKind

logger = logging.getLogger(__name__)

CACHE_SECONDS = 15.0
# A rate is reported once the run has lasted this long and handled this many
# tracks: the first minutes of a run (tagged files that need no measuring,
# the tracks people play) say little about the rest.
RATE_MIN_SECONDS = 300.0
RATE_MIN_TRACKS = 200

_items = cast(Table, MediaItem.__table__)
_features = cast(Table, AudioFeatures.__table__)

PassState = Literal["running", "waiting", "idle", "disabled"]


@dataclass(frozen=True)
class LibraryCounts:
    tracks: int
    loudness_done: int
    loudness_failed: int
    analysis_done: int
    analysis_failed: int

    @property
    def loudness_pending(self) -> int:
        return self.tracks - self.loudness_done - self.loudness_failed

    @property
    def analysis_pending(self) -> int:
        return self.tracks - self.analysis_done - self.analysis_failed


def _analysis_current():
    """A feature row from this ANALYSIS_VERSION for the file as it is now
    (the opposite of app.audio_analysis's pending condition)."""
    return and_(
        _features.c.media_item_id.is_not(None),
        _features.c.version == ANALYSIS_VERSION,
        _features.c.source_mtime == _items.c.file_mtime,
        func.coalesce(_features.c.source_size, -1) == func.coalesce(_items.c.file_size, -1),
    )


def _count_if(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _query_counts(db: Session) -> LibraryCounts:
    gain = _items.c.replaygain_track_gain
    current = _analysis_current()
    row = db.execute(
        select(
            func.count(),
            _count_if(gain.is_not(None)),
            _count_if(and_(_items.c.loudness_scanned_at.is_not(None), gain.is_(None))),
            _count_if(and_(current, _features.c.vector.is_not(None))),
            _count_if(
                and_(current, _features.c.vector.is_(None), _features.c.error.is_not(None))
            ),
        )
        .select_from(_items.outerjoin(_features, _features.c.media_item_id == _items.c.id))
        .where(_items.c.kind == MediaKind.track.value)
    ).one()
    return LibraryCounts(*(int(value) for value in row))


_cache_lock = threading.Lock()
_cache: tuple[float, LibraryCounts] | None = None


def library_counts(db: Session, *, fresh: bool = False) -> LibraryCounts:
    """The counts, from a cache at most CACHE_SECONDS old unless `fresh`
    (a pass starting asks for fresh ones, which refreshes the cache)."""
    global _cache
    now = time.monotonic()
    with _cache_lock:
        if not fresh and _cache is not None and now - _cache[0] < CACHE_SECONDS:
            return _cache[1]
    counts = _query_counts(db)
    with _cache_lock:
        _cache = (time.monotonic(), counts)
    return counts


_refreshing = threading.Lock()


def polled_counts(db: Session) -> LibraryCounts:
    """The counts for a page that polls: whatever is in hand, at once. Counts
    older than CACHE_SECONDS are still returned, and replaced in the
    background for the next poll - the query takes many seconds on a big
    library while the database is busy. Only the very first call (nothing
    in hand; app.cache_warmup makes it at startup) runs the query itself."""
    with _cache_lock:
        cached = _cache
    if cached is None:
        return library_counts(db)
    if time.monotonic() - cached[0] >= CACHE_SECONDS:
        _refresh_in_background()
    return cached[1]


def _refresh_in_background() -> None:
    if not _refreshing.acquire(blocking=False):
        return  # one refresh at a time

    def run() -> None:
        try:
            db = SessionLocal()
            try:
                library_counts(db, fresh=True)
            finally:
                db.close()
        except Exception:
            logger.exception("Could not refresh the background-pass counts")
        finally:
            _refreshing.release()

    spawn("background-status-refresh", run)


def wait_for_refresh(timeout: float = 10.0) -> bool:
    """Wait for a background refresh in flight (tests). False on timeout."""
    if _refreshing.acquire(timeout=timeout):
        _refreshing.release()
        return True
    return False


def clear_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


class RunProgress:
    """A pass's counters for its current run: set by its thread when the
    run begins and on each progress line, read by background_status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.started = 0.0
        self.updated = 0.0
        self.done = 0
        self.stored = 0

    def begin(self) -> None:
        with self._lock:
            self.running = True
            self.started = self.updated = time.monotonic()
            self.done = self.stored = 0

    def update(self, done: int, stored: int) -> None:
        with self._lock:
            self.done, self.stored, self.updated = done, stored, time.monotonic()

    def end(self) -> None:
        with self._lock:
            self.running = False

    def snapshot(self) -> tuple[bool, int, int, float | None]:
        """(running, done, stored, tracks per minute or None while not yet
        known), the rate over the run up to its last progress line."""
        with self._lock:
            elapsed = self.updated - self.started
            rate = None
            if self.running and elapsed >= RATE_MIN_SECONDS and self.done >= RATE_MIN_TRACKS:
                rate = round(self.done / (elapsed / 60), 1)
            return self.running, self.done, self.stored, rate


def _pass_out(
    state: PassState, done: int, failed: int, pending: int, total: int, progress: RunProgress
) -> dict:
    running, run_done, run_stored, rate = progress.snapshot()
    return {
        "state": state,
        "total": total,
        "done": done,
        "failed": failed,
        "pending": pending,
        "run_checked": run_done if running else None,
        "run_stored": run_stored if running else None,
        "rate_per_minute": rate if state == "running" else None,
    }


def background_status(db: Session) -> dict:
    """Counts (cached) plus each pass's live state, for the Admin panel."""
    # Imported here: both pass modules import this one for RunProgress and
    # library_counts.
    from app import audio_analysis, loudness
    from app.config import settings

    counts = polled_counts(db)
    loudness_running = loudness._run_lock.locked()
    if not settings.audio_analysis:
        analysis_state: PassState = "disabled"
    elif (
        audio_analysis._run_lock.locked()
        and loudness_running
        and settings.audio_analysis_concurrency == 1
    ):
        analysis_state = "waiting"
    elif audio_analysis.progress.running:
        analysis_state = "running"
    else:
        analysis_state = "idle"
    return {
        "loudness": _pass_out(
            "running" if loudness_running else "idle",
            counts.loudness_done,
            counts.loudness_failed,
            counts.loudness_pending,
            counts.tracks,
            loudness.progress,
        ),
        "audio_analysis": _pass_out(
            analysis_state,
            counts.analysis_done,
            counts.analysis_failed,
            counts.analysis_pending,
            counts.tracks,
            audio_analysis.progress,
        ),
    }
