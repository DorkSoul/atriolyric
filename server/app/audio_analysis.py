"""The background pass that fills ``audio_features`` (see app.audio_features).

One daemon thread (registered with app.background), started at startup:

- it waits while the loudness backfill runs, so by default only one heavy
  ffmpeg decodes at a time (``ATRIOLYRIC_AUDIO_ANALYSIS_CONCURRENCY`` above 1
  adds workers that run alongside it);
- the tracks people play come first - most recently played, then most
  played, then the rest - so "sounds like" works for the music in rotation
  long before a large library is through;
- results are committed every BATCH_SIZE tracks with Core statements that
  skip a track a scan deleted meanwhile, and progress is logged every
  PROGRESS_EVERY tracks ("Audio analysis: N / M tracks, K analysed"), and
  a pass with work to do starts with one line saying where the library
  stands (app.background_status has the same counts for the Admin panel);
- a failure is stored too, so a broken file is not retried until it changes
  on disk (the scan updates media_items' mtime/size; a mismatch with the
  row's source_mtime/source_size queues it again) or ANALYSIS_VERSION goes
  up;
- but a file that is not there (a share that is not mounted, a file deleted
  since the scan) and an ffmpeg that timed out store nothing: the track
  stays pending and a later pass tries again. A library folder that cannot
  be reached is set aside for the rest of the pass (loudness.LeftForLater),
  so an unmounted share costs a pass one look, not one per track;
- it resumes after a restart from whatever is still pending, stops between
  reads of ffmpeg's output on shutdown (``stop_audio_analysis``), and after
  finishing checks again every IDLE_POLL_SECONDS for tracks a scan added.

``ATRIOLYRIC_AUDIO_ANALYSIS=false`` turns it off.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from sqlalchemy import Select, Table, and_, bindparam, exists, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app import loudness
from app.access import media_file_absent, media_file_path
from app.audio_features import (
    ANALYSIS_VERSION,
    AnalysisError,
    FeatureVector,
    analyse_file,
)
from app.background import spawn
from app.background_status import LibraryCounts, RunProgress, library_counts
from app.config import settings
from app.db import SessionLocal
from app.models import AudioFeatures, MediaItem, MediaKind, PlayHistory, utcnow

logger = logging.getLogger(__name__)

BATCH_SIZE = 25
PROGRESS_EVERY = 100
# How many pending ids one round orders by plays before asking again (new
# plays and scans are picked up between rounds).
ROUND_SIZE = 1000
LOUDNESS_POLL_SECONDS = 30.0
# How often a worker waiting for its slot looks again.
SLOT_POLL_SECONDS = 1.0
IDLE_POLL_SECONDS = 600.0
ERROR_MAX_LENGTH = 200
# audio_features' reason for an ffmpeg that did not finish in time. Like its
# "stopped", it is not stored: a stalled share or a busy machine is as likely
# as the file.
TIMED_OUT = "ffmpeg timed out"
# A track whose ffmpeg timed out is left alone for this long, so one file
# that always does is not decoded again on every IDLE_POLL_SECONDS pass.
TIMEOUT_RETRY_SECONDS = 6 * 3600.0

_items = cast(Table, MediaItem.__table__)
_features = cast(Table, AudioFeatures.__table__)

Analyse = Callable[[Path, float | None, threading.Event | None], FeatureVector]


def _pending_condition():
    return and_(
        _items.c.kind == MediaKind.track.value,
        or_(
            _features.c.media_item_id.is_(None),
            _features.c.version != ANALYSIS_VERSION,
            _features.c.source_mtime != _items.c.file_mtime,
            func.coalesce(_features.c.source_size, -1) != func.coalesce(_items.c.file_size, -1),
        ),
    )


def _with_features(columns) -> Select:
    return select(*columns).select_from(
        _items.outerjoin(_features, _features.c.media_item_id == _items.c.id)
    )


def pending_count(db: Session, exclude_roots: set[int] | None = None) -> int:
    """Tracks with no current analysis: never analysed, analysed by another
    ANALYSIS_VERSION, or whose file changed since; none from the library
    roots `exclude_roots`."""
    query = _with_features([func.count()]).where(_pending_condition())
    if exclude_roots:
        query = query.where(_items.c.library_root_id.not_in(exclude_roots))
    return db.execute(query).scalar_one()


def pending_ids(
    db: Session, limit: int | None = None, exclude_roots: set[int] | None = None
) -> list[int]:
    """Pending track ids, the ones played most recently first, then by play
    count, then the rest by id; none from the library roots `exclude_roots`."""
    plays = (
        select(
            PlayHistory.media_item_id.label("media_item_id"),
            func.max(PlayHistory.played_at).label("last_played"),
            func.count().label("play_count"),
        )
        .group_by(PlayHistory.media_item_id)
        .subquery()
    )
    query = (
        _with_features([_items.c.id])
        .outerjoin(plays, plays.c.media_item_id == _items.c.id)
        .where(_pending_condition())
        .order_by(
            plays.c.last_played.is_(None),
            plays.c.last_played.desc(),
            plays.c.play_count.desc(),
            _items.c.id,
        )
    )
    if exclude_roots:
        query = query.where(_items.c.library_root_id.not_in(exclude_roots))
    if limit is not None:
        query = query.limit(limit)
    return list(db.execute(query).scalars())


@dataclass(frozen=True)
class _Job:
    media_item_id: int
    path: Path | None
    duration_seconds: float | None
    mtime: float
    size: int | None
    root_id: int
    root_path: str


def _load_jobs(db: Session, ids: list[int], left: loudness.LeftForLater) -> list[_Job]:
    """The tracks among `ids` that are still pending and can be analysed now,
    with their paths (one query for the rows and their roots, one for their
    feature rows). A track whose file is not there is noted in `left` and
    left out; so is one in a folder `left` has set aside, without a look at
    the disk."""
    items = db.execute(
        select(MediaItem).where(MediaItem.id.in_(ids)).options(selectinload(MediaItem.library_root))
    ).scalars()
    by_id = {item.id: item for item in items}
    current = {
        row.media_item_id: row
        for row in db.execute(
            select(
                _features.c.media_item_id,
                _features.c.version,
                _features.c.source_mtime,
                _features.c.source_size,
            ).where(_features.c.media_item_id.in_(ids))
        )
    }
    jobs = []
    for item_id in ids:
        item = by_id.get(item_id)
        if item is None or item.kind != MediaKind.track:
            continue  # deleted (or changed kind) since the round began
        row = current.get(item_id)
        if (
            row is not None
            and row.version == ANALYSIS_VERSION
            and row.source_mtime == item.file_mtime
            and row.source_size == item.file_size
        ):
            continue  # analysed meanwhile
        if item.library_root_id in left.roots:
            left.skipped(1)
            continue
        path = media_file_path(item)
        if path is None and media_file_absent(item):
            left.absent(item.library_root_id, item.library_root.path)
            continue
        if path is not None:
            left.present(item.library_root_id)
        jobs.append(
            _Job(
                media_item_id=item_id,
                # None here: there, but not usable (it leads out of its
                # library folder). That is recorded as a failure.
                path=path,
                duration_seconds=item.duration_seconds,
                mtime=item.file_mtime,
                size=item.file_size,
                root_id=item.library_root_id,
                root_path=item.library_root.path,
            )
        )
    return jobs


def _row(job: _Job, outcome: FeatureVector | str) -> dict:
    values = {
        "media_item_id": job.media_item_id,
        "version": ANALYSIS_VERSION,
        "analysed_at": utcnow(),
        "source_mtime": job.mtime,
        "source_size": job.size,
        "error": None,
        "vector": None,
        "tempo_bpm": None,
        "loudness_db": None,
        "brightness_hz": None,
        "musical_key": None,
        "mode": None,
        "analysed_seconds": None,
    }
    if isinstance(outcome, str):
        values["error"] = outcome[:ERROR_MAX_LENGTH]
    else:
        values.update(
            vector=outcome.to_blob(),
            tempo_bpm=round(outcome.tempo_bpm, 2),
            loudness_db=round(outcome.loudness_db, 2),
            brightness_hz=round(outcome.brightness_hz, 1),
            musical_key=outcome.key,
            mode=outcome.mode,
            analysed_seconds=outcome.analysed_seconds,
        )
    return values


_COLUMNS = (
    "media_item_id",
    "version",
    "analysed_at",
    "source_mtime",
    "source_size",
    "error",
    "vector",
    "tempo_bpm",
    "loudness_db",
    "brightness_hz",
    "musical_key",
    "mode",
    "analysed_seconds",
)


def _upsert_statement():
    """INSERT ... SELECT ... WHERE the track still exists ... ON CONFLICT
    UPDATE: a track a scan deleted meanwhile is skipped instead of failing
    the batch on its foreign key."""
    source = select(*(bindparam(name, type_=_features.c[name].type) for name in _COLUMNS)).where(
        exists().where(_items.c.id == bindparam("media_item_id"))
    )
    statement = sqlite_insert(_features).from_select(list(_COLUMNS), source)
    return statement.on_conflict_do_update(
        index_elements=[_features.c.media_item_id],
        set_={name: statement.excluded[name] for name in _COLUMNS if name != "media_item_id"},
    )


def store_results(db: Session, rows: list[dict]) -> None:
    """Write a batch of results in one statement and commit."""
    if rows:
        db.execute(_upsert_statement(), rows)
    db.commit()


def _loudness_busy() -> bool:
    """Whether the loudness backfill is running (or queued to run again):
    its start function holds this lock for exactly that long."""
    return loudness._run_lock.locked()


def _allowed_workers(concurrency: int) -> int:
    return concurrency - 1 if _loudness_busy() else concurrency


def _lower_thread_priority() -> None:
    """Below-normal CPU priority for this thread only (Linux schedules
    threads as tasks with their own nice value); the numpy half of the work
    runs here, the ffmpeg half is niced on its own."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except (AttributeError, OSError):
        pass


def _analyse(job: _Job, analyse: Analyse, stop: threading.Event) -> FeatureVector | str:
    if job.path is None:
        return "file missing or outside its library folder"
    try:
        return analyse(job.path, job.duration_seconds, stop)
    except AnalysisError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - one odd file must not end the pass
        logger.exception("Audio analysis of track %d failed", job.media_item_id)
        return f"analysis error: {exc.__class__.__name__}"


def _run_jobs(
    jobs: list[_Job],
    analyse: Analyse,
    stop: threading.Event,
    concurrency: int,
) -> Iterator[tuple[_Job, FeatureVector | str]]:
    """Analyse `jobs` with up to `concurrency` workers, each waiting while
    its slot is taken by the loudness backfill. Yields (job, outcome) as
    they finish; a job cut short by `stop` is not yielded. An outcome of
    TIMED_OUT, or a failure whose file has gone meanwhile, is not to be
    stored (see `_left_for_later`)."""
    results: list[tuple[_Job, FeatureVector | str]] = []
    lock = threading.Lock()
    queue = list(reversed(jobs))

    def next_job(slot: int) -> _Job | None:
        """The next job once this slot may run, or None when the batch is
        done or the pass stopped. Checks once a second: a waiting worker
        must notice the others emptying the batch."""
        while not stop.is_set():
            with lock:
                if not queue:
                    return None
                if slot < _allowed_workers(concurrency):
                    return queue.pop()
            stop.wait(SLOT_POLL_SECONDS)
        return None

    def work(slot: int) -> None:
        if slot:
            _lower_thread_priority()
        while (job := next_job(slot)) is not None:
            outcome = _analyse(job, analyse, stop)
            if stop.is_set() and isinstance(outcome, str) and outcome == "stopped":
                return
            with lock:
                results.append((job, outcome))

    if concurrency == 1:
        work(0)
    else:
        threads = [
            threading.Thread(target=work, args=(slot,), name=f"audio-analysis-{slot}", daemon=True)
            for slot in range(1, concurrency)
        ]
        for thread in threads:
            thread.start()
        work(0)
        for thread in threads:
            thread.join()
    yield from results


def _left_for_later(job: _Job, outcome: FeatureVector | str) -> bool:
    """Whether a failure says nothing about the file itself: ffmpeg timed
    out, or the file went away while it was read (the share dropped out)."""
    if not isinstance(outcome, str):
        return False
    return outcome == TIMED_OUT or (job.path is not None and not job.path.is_file())


# When each track's ffmpeg last timed out (monotonic seconds).
_timed_out_at: dict[int, float] = {}


def _recently_timed_out() -> set[int]:
    now = time.monotonic()
    for item_id in [i for i, at in _timed_out_at.items() if now - at >= TIMEOUT_RETRY_SECONDS]:
        del _timed_out_at[item_id]
    return set(_timed_out_at)


# The database and its counts as a pass that left tracks for later (and said
# so) ended. The pass after it (IDLE_POLL_SECONDS later) that finds the same
# and again can store nothing says nothing: an unmounted share is reported
# once, not every ten minutes.
_stuck: tuple[str, LibraryCounts] | None = None


def analyse_pending(
    session_factory: sessionmaker = SessionLocal,
    analyse: Analyse = analyse_file,
    stop: threading.Event | None = None,
    concurrency: int | None = None,
    progress_cb: Callable[[int, int, int], None] | None = None,
) -> tuple[int, int]:
    """Analyse every pending track (until `stop` is set). Returns (analysed,
    failed). Tracks whose file cannot be reached now are left pending."""
    global _stuck
    stop = stop or threading.Event()
    concurrency = concurrency or settings.audio_analysis_concurrency
    analysed = failed = done = 0
    # Tried in this pass (or timed out not long ago), so not taken from the
    # next round again. Those left for later among them are still pending,
    # so each round's query still returns them: `still_offered` counts them.
    attempted = _recently_timed_out()
    carried = len(attempted)
    left = loudness.LeftForLater()
    db = session_factory()
    try:
        counts = library_counts(db, fresh=True)
        database = str(db.get_bind().engine.url)
        repeat = (database, counts) == _stuck
        if not repeat:
            _log_state(counts)
        total = counts.analysis_pending - carried
        while not stop.is_set():
            still_offered = carried + left.left_in_reachable_folders()
            round_ids = [
                i
                for i in pending_ids(db, ROUND_SIZE + still_offered, exclude_roots=set(left.roots))
                if i not in attempted
            ]
            db.commit()  # end the read transaction; analysis takes a while
            if not round_ids:
                break
            for start in range(0, len(round_ids), BATCH_SIZE):
                if stop.is_set():
                    break
                batch = round_ids[start : start + BATCH_SIZE]
                attempted.update(batch)
                left_before, roots_before = left.tracks, len(left.roots)
                jobs = _load_jobs(db, batch, left)
                db.commit()
                rows = []
                for job, outcome in _run_jobs(jobs, analyse, stop, concurrency):
                    if _left_for_later(job, outcome):
                        if outcome == TIMED_OUT:
                            _timed_out_at[job.media_item_id] = time.monotonic()
                            left.timed_out(job.root_id)
                        else:
                            left.absent(job.root_id, job.root_path)
                        continue
                    rows.append(_row(job, outcome))
                    if isinstance(outcome, str):
                        failed += 1
                    else:
                        analysed += 1
                store_results(db, rows)
                # Progress counts the tracks dealt with against those that
                # can be dealt with now: what is left for later comes off
                # the total, not onto "done".
                left_now = left.tracks - left_before
                before = done
                done += len(batch) - left_now
                if len(left.roots) > roots_before:
                    # A folder was set aside: the pending tracks in it that
                    # were never looked at are not coming either.
                    reachable = pending_count(db, exclude_roots=set(left.roots))
                    db.commit()
                    total = done + reachable - carried - left.left_in_reachable_folders()
                else:
                    total -= left_now
                total = max(total, done)
                if progress_cb is not None and (
                    done // PROGRESS_EVERY > before // PROGRESS_EVERY
                    or (done == total and (done > before or left_now) and done)
                ):
                    progress_cb(done, total, analysed)
        if not left.tracks:
            _stuck = None
        else:
            if not repeat or analysed or failed:
                logger.warning(
                    "Audio analysis left %s tracks pending: %s",
                    f"{left.tracks if stop.is_set() else pending_count(db):,}",
                    left.describe(),
                )
            _stuck = (database, library_counts(db, fresh=True))
    finally:
        db.close()
    return analysed, failed


def _log_state(counts: LibraryCounts) -> None:
    """Logs where the library stands when there is work to do (an idle
    pass looks every IDLE_POLL_SECONDS and stays quiet)."""
    if counts.analysis_pending:
        logger.info(
            "Audio analysis: %s of %s tracks analysed, %s could not be analysed, %s to go",
            f"{counts.analysis_done:,}",
            f"{counts.tracks:,}",
            f"{counts.analysis_failed:,}",
            f"{counts.analysis_pending:,}",
        )


# The running pass's counters, for app.background_status.
progress = RunProgress()


def _log_progress(done: int, total: int, stored: int = 0) -> None:
    progress.update(done, stored)
    logger.info("Audio analysis: %d / %d tracks, %d analysed", done, total, stored)


_stop = threading.Event()
_wake = threading.Event()
_run_lock = threading.Lock()


def _run_forever() -> None:
    try:
        _lower_thread_priority()
        while not _stop.is_set():
            _wake.clear()
            if _loudness_busy() and settings.audio_analysis_concurrency == 1:
                # Its own wait, before the pending queries: the loudness
                # pass can run for days on a first start.
                _wake.wait(LOUDNESS_POLL_SECONDS)
                continue
            progress.begin()
            try:
                analysed, failed = analyse_pending(stop=_stop, progress_cb=_log_progress)
                if analysed or failed:
                    logger.info(
                        "Audio analysis %s: %d tracks analysed, %d failed",
                        "stopped" if _stop.is_set() else "complete",
                        analysed,
                        failed,
                    )
            except Exception:
                logger.exception("Audio analysis failed")
            finally:
                progress.end()
            _wake.wait(IDLE_POLL_SECONDS)
    finally:
        _run_lock.release()


def start_audio_analysis() -> None:
    """Start the background pass (once; a later call wakes an idle pass to
    look for new tracks). Does nothing when ATRIOLYRIC_AUDIO_ANALYSIS is
    off."""
    if not settings.audio_analysis:
        logger.info("Audio analysis is turned off (ATRIOLYRIC_AUDIO_ANALYSIS)")
        return
    _stop.clear()
    _wake.set()
    if not _run_lock.acquire(blocking=False):
        return
    spawn("audio-analysis", _run_forever)


def stop_audio_analysis() -> None:
    """Ask the pass to stop: an ffmpeg being read is killed, the batch done
    so far is committed and the thread ends."""
    _stop.set()
    _wake.set()
