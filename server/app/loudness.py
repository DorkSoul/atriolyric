"""ReplayGain for tracks whose files carry no gain tags.

Scans store tag-provided gains directly (see ``scanner.read_replaygain``);
this background pass covers the rest. Each pending track's tags are read
first - older rows were scanned before gains were stored - and only a file
with no track gain tag is measured with ffmpeg's EBU R128 filter. A track
whose file was there gets ``loudness_scanned_at``, even when ffmpeg could
not measure it, so a file ffmpeg can't decode is never retried until it
changes on disk.

A file that is not there (a share that is not mounted, a file deleted since
the scan) and a measurement that timed out store nothing: the track stays
pending. A library folder that cannot be reached is set aside for the rest
of the pass, and the pass is run again later (``LeftForLater``,
``_schedule_retry``). Marking those tracks as checked would let a share that
drops out during the days-long first pass mark its whole backlog "could
not be measured" for good.
"""

import logging
import math
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

from sqlalchemy import Table, bindparam, case, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.access import library_root_reachable, media_file_absent, media_file_path
from app.background import spawn
from app.background_status import RunProgress, library_counts
from app.db import SessionLocal
from app.library_cache import bump_library_version
from app.models import MediaItem, MediaKind, utcnow
from app.scanner import read_replaygain, read_track_peak
from app.video_meta import INPUT_PROTOCOLS

logger = logging.getLogger(__name__)

REPLAYGAIN_REFERENCE_LUFS = -18.0
MEASURE_TIMEOUT_SECONDS = 600
BATCH_SIZE = 100
# This many files in a row missing from one library folder set that folder
# aside for the rest of the pass, even when the folder itself looks fine (a
# share mounted below it, a mount that answers slowly with errors). Deleted
# files are missing too, so a folder with that many of them in a row waits
# for the scan that removes their rows.
MISSING_STREAK_PAUSE = 20
# How long after a pass that could not reach a library folder (or had ffmpeg
# time out) the next one runs, doubling up to the maximum while that stays so.
UNREACHABLE_RETRY_SECONDS = 900.0
UNREACHABLE_RETRY_MAX_SECONDS = 6 * 3600.0

# Measuring is background work that can decode for minutes: below normal
# priority, so playback transcodes and requests keep the CPU (the image's
# Debian base ships coreutils' nice; without it ffmpeg runs at normal priority).
_NICE = ("nice", "-n", "10") if shutil.which("nice") else ()
# Core table: writes by id that simply match nothing when a scan deleted the
# row meanwhile. ORM flushes would raise StaleDataError for that and end the pass.
_items = cast(Table, MediaItem.__table__)

_INTEGRATED_RE = re.compile(r"Integrated loudness:\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS")


def parse_integrated_lufs(ffmpeg_stderr: str) -> float | None:
    """The summary ffmpeg's ebur128 filter prints when it finishes."""
    matches = _INTEGRATED_RE.findall(ffmpeg_stderr)
    return float(matches[-1]) if matches else None


_PEAK_RE = re.compile(r"True peak:\s*Peak:\s*(-?\d+(?:\.\d+)?|-inf)\s*dBFS")


def parse_true_peak(ffmpeg_stderr: str) -> float | None:
    """Linear true peak from the ebur128 summary (silence is -inf dBFS = 0)."""
    matches = _PEAK_RE.findall(ffmpeg_stderr)
    if not matches:
        return None
    return 0.0 if matches[-1] == "-inf" else 10 ** (float(matches[-1]) / 20)


class MeasurementTimedOut(Exception):
    """ffmpeg did not finish in time: a stalled share or a busy machine as
    likely as the file, so nothing is recorded about the file."""


class LeftForLater:
    """What one pass over the library (this one, or audio analysis) could
    not get at and therefore left pending: tracks whose file is not there or
    whose ffmpeg timed out, and the library folders found unreachable, whose
    remaining tracks the pass no longer looks at."""

    def __init__(self) -> None:
        self.tracks = 0
        self.timeouts = 0
        # Unreachable library root id -> its path.
        self.roots: dict[int, str] = {}
        self._missing_in_a_row: dict[int, int] = {}
        # Tracks looked at and left, by library root.
        self._left_in: dict[int, int] = {}

    def absent(self, root_id: int, root_path: str) -> None:
        """A track's file is not there. Its folder is set aside when the
        folder is not reachable or too many of its files in a row were
        missing."""
        self.tracks += 1
        self._left_in[root_id] = self._left_in.get(root_id, 0) + 1
        streak = self._missing_in_a_row.get(root_id, 0) + 1
        self._missing_in_a_row[root_id] = streak
        if streak >= MISSING_STREAK_PAUSE or not library_root_reachable(root_path):
            self.roots[root_id] = root_path

    def present(self, root_id: int) -> None:
        self._missing_in_a_row.pop(root_id, None)

    def skipped(self, count: int) -> None:
        """Tracks of a folder that was set aside, not looked at."""
        self.tracks += count

    def timed_out(self, root_id: int) -> None:
        self.tracks += 1
        self.timeouts += 1
        self._left_in[root_id] = self._left_in.get(root_id, 0) + 1

    def left_in_reachable_folders(self) -> int:
        """How many tracks were looked at and left in folders that are not
        set aside: a query that leaves the set-aside folders out still
        returns these."""
        return sum(n for root_id, n in self._left_in.items() if root_id not in self.roots)

    @property
    def retry_wanted(self) -> bool:
        """Whether running again later could get further without a scan. A
        lone missing file in a reachable folder was deleted: only a scan
        changes anything about it."""
        return bool(self.roots or self.timeouts)

    def describe(self) -> str:
        if self.roots:
            return "library folder not reachable: " + ", ".join(sorted(self.roots.values()))
        if self.timeouts == self.tracks:
            return "ffmpeg timed out"
        return "files missing (a scan removes deleted files)"


def measure_loudness(path: Path) -> tuple[float, float | None] | None:
    """(integrated LUFS, linear true peak) for the file's first audio stream,
    or None when ffmpeg can't measure it. Raises MeasurementTimedOut when
    ffmpeg does not finish in time."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv list, no shell; paths come from the library, never a command
            [  # noqa: S607 - ffmpeg (and nice) are resolved from PATH on purpose
                *_NICE,
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                *INPUT_PROTOCOLS,
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-af",
                # framelog=verbose, not quiet: "quiet" only exists from ffmpeg
                # 6, and ffmpeg 5.1 (Debian bookworm, which CI and older images
                # use) makes every measurement fail with it. At the default log
                # level the per-frame lines stay hidden either way; the summary
                # prints. This works on 5.1, 7.1 (the trixie image) and 9.0.
                "ebur128=peak=true:framelog=verbose",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=MEASURE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MeasurementTimedOut from exc
    except OSError:
        return None
    if result.returncode != 0:
        return None
    lufs = parse_integrated_lufs(result.stderr)
    return None if lufs is None else (lufs, parse_true_peak(result.stderr))


def measure_integrated_lufs(path: Path) -> float | None:
    try:
        measured = measure_loudness(path)
    except MeasurementTimedOut:
        return None
    return None if measured is None else measured[0]


def track_gain_for_lufs(lufs: float) -> float:
    return REPLAYGAIN_REFERENCE_LUFS - lufs


def album_gain_for_track_gains(track_gains: list[float]) -> float:
    """Approximates whole-album loudness as the energy mean of each track's
    integrated loudness (recovered from its gain), without duration weights."""
    lufs = [REPLAYGAIN_REFERENCE_LUFS - gain for gain in track_gains]
    mean_energy = sum(10 ** (value / 10) for value in lufs) / len(lufs)
    return REPLAYGAIN_REFERENCE_LUFS - 10 * math.log10(mean_energy)


def _measure_pending_tracks(
    db: Session,
    measure: Callable[[Path], float | tuple[float, float | None] | None],
    progress_cb: Callable[[int, int, int], None] | None,
    left: LeftForLater,
) -> int:
    pending_rows = db.execute(
        select(MediaItem.id, MediaItem.library_root_id)
        .where(
            MediaItem.kind == MediaKind.track,
            MediaItem.loudness_scanned_at.is_(None),
        )
        .order_by(MediaItem.id)
    ).all()
    # Progress counts the tracks dealt with against those that can be dealt
    # with now: what is left for later comes off the total, not onto "checked".
    total = len(pending_rows)
    checked = reported = changed = 0
    # Measuring a batch takes minutes, and SQLite allows one writer: the
    # values are held here and written together when the batch ends, so the
    # write lock is held for milliseconds. Writing each row as it is measured
    # would hold the lock from a batch's first track to its commit, and every
    # other write in the app (mix refresh, device heartbeats) would fail with
    # "database is locked" for as long as the backfill runs.
    measured_rows: list[dict] = []
    statement = update(_items).where(_items.c.id == bindparam("item_id"))
    for position, (item_id, root_id) in enumerate(pending_rows, start=1):
        if root_id not in left.roots:
            outcome = _measure_one(db, item_id, measure, left)
            if outcome is None:
                total -= 1
                if root_id in left.roots:
                    # Just set aside: the rest of its tracks are not looked
                    # at (each would wait on the same missing share).
                    rest = sum(1 for _, other in pending_rows[position:] if other == root_id)
                    left.skipped(rest)
                    total -= rest
            else:
                checked += 1
                if outcome:
                    measured_rows.append({"item_id": item_id, **outcome})
                    changed += any(
                        outcome.get(column) is not None
                        for column in (
                            "replaygain_track_gain",
                            "replaygain_album_gain",
                            "replaygain_track_peak",
                        )
                    )
        if position % BATCH_SIZE == 0 or position == len(pending_rows):
            # Rows differ in which columns they set, so one statement each.
            for row in measured_rows:
                row_values = {k: v for k, v in row.items() if k != "item_id"}
                db.execute(statement.values(**row_values), {"item_id": row["item_id"]})
            measured_rows.clear()
            db.commit()
            if progress_cb is not None and checked != reported:
                progress_cb(checked, total, changed)
                reported = checked
    return changed


def _measure_one(
    db: Session,
    item_id: int,
    measure: Callable[[Path], float | tuple[float, float | None] | None],
    left: LeftForLater,
) -> dict | None:
    """The values to store for one pending track: empty when there is nothing
    to do for it (a scan removed or covered it meanwhile), None when it is
    left for later (noted in `left`) and nothing may be stored."""
    item = db.get(MediaItem, item_id)
    # A scan may have removed or already covered it since the query.
    pending = item is not None and item.loudness_scanned_at is None
    path = media_file_path(item) if item is not None and pending else None
    absent = item is not None and pending and path is None and media_file_absent(item)
    root = (item.library_root_id, item.library_root.path) if item is not None and pending else None
    # Nothing is held open while ffmpeg runs, not even a read snapshot
    # (writing from a stale one fails at once, whatever the busy timeout).
    db.commit()
    if root is None:
        return {}
    if absent:
        left.absent(*root)
        return None
    track_gain = album_gain = peak = None
    # No path here means the file is there but not usable (it leads out of
    # its library folder): that is recorded, like a file ffmpeg cannot read.
    if path is not None:
        left.present(root[0])
        track_gain, album_gain = read_replaygain(path)
        peak = read_track_peak(path)
        if track_gain is None:
            try:
                measured = measure(path)
            except MeasurementTimedOut:
                left.timed_out(root[0])
                return None
            if measured is None and not path.is_file():
                # It went away while being read (the share dropped out).
                left.absent(*root)
                return None
            lufs, measured_peak = measured if isinstance(measured, tuple) else (measured, None)
            track_gain = track_gain_for_lufs(lufs) if lufs is not None else None
            peak = peak if peak is not None else measured_peak
    values: dict = {"replaygain_track_gain": track_gain, "loudness_scanned_at": utcnow()}
    if peak is not None:
        values["replaygain_track_peak"] = peak
    if album_gain is not None:
        values["replaygain_album_gain"] = album_gain
    # Not written through the loaded object: a row a scan deletes before the
    # commit would make the whole batch fail (StaleDataError).
    return values


def _fill_album_gains(db: Session) -> int:
    """Album gain for albums whose tracks have a track gain but no album
    gain, from all of that album's measured tracks: one query for every
    such album's tracks, then batched updates."""
    album_artist = func.coalesce(MediaItem.album_artist, MediaItem.artist)
    group = (MediaItem.library_root_id, album_artist, MediaItem.album)
    missing = func.sum(case((MediaItem.replaygain_album_gain.is_(None), 1), else_=0)).over(
        partition_by=group
    )
    tracks = (
        select(
            MediaItem.id,
            MediaItem.library_root_id,
            album_artist.label("album_artist"),
            MediaItem.album,
            MediaItem.replaygain_track_gain,
            MediaItem.replaygain_album_gain,
            missing.label("missing"),
        )
        .where(
            MediaItem.kind == MediaKind.track,
            MediaItem.album.is_not(None),
            MediaItem.replaygain_track_gain.is_not(None),
        )
        .subquery()
    )
    albums: dict[tuple, list] = {}
    for row in db.execute(select(tracks).where(tracks.c.missing > 0)):
        albums.setdefault((row.library_root_id, row.album_artist, row.album), []).append(row)
    updates = []
    for rows in albums.values():
        gain = album_gain_for_track_gains([row.replaygain_track_gain for row in rows])
        updates += [
            {"item_id": row.id, "gain": gain} for row in rows if row.replaygain_album_gain is None
        ]
    statement = (
        update(_items)
        .where(_items.c.id == bindparam("item_id"), _items.c.replaygain_album_gain.is_(None))
        .values(replaygain_album_gain=bindparam("gain"))
    )
    for start in range(0, len(updates), BATCH_SIZE):
        db.execute(statement, updates[start : start + BATCH_SIZE])
        db.commit()
    return len(updates)


def backfill_loudness(
    session_factory: sessionmaker = SessionLocal,
    measure: Callable[[Path], float | tuple[float, float | None] | None] = measure_loudness,
    progress_cb: Callable[[int, int, int], None] | None = None,
) -> int:
    """Returns how many rows gained a value, bumping the library version if
    any did so cached listings pick the gains up."""
    left = LeftForLater()
    db = session_factory()
    try:
        _log_state(db)
        changed = _measure_pending_tracks(db, measure, progress_cb, left)
        changed += _fill_album_gains(db)
    finally:
        db.close()
    if left.tracks:
        logger.warning(
            "Loudness backfill left %s tracks pending: %s", f"{left.tracks:,}", left.describe()
        )
    if left.retry_wanted:
        _retry_wanted.set()
    if changed:
        bump_library_version()
    return changed


def _log_state(db: Session) -> None:
    """Where the library stands as a pass starts (fresh counts)."""
    counts = library_counts(db, fresh=True)
    logger.info(
        "Loudness: %s of %s tracks have a value, %s could not be measured, %s to go",
        f"{counts.loudness_done:,}",
        f"{counts.tracks:,}",
        f"{counts.loudness_failed:,}",
        f"{counts.loudness_pending:,}",
    )


# The running pass's counters, for app.background_status.
progress = RunProgress()


def _log_progress(done: int, total: int, stored: int = 0) -> None:
    progress.update(done, stored)
    logger.info("Loudness backfill: %d / %d tracks checked, %d values stored", done, total, stored)


_run_lock = threading.Lock()
_rerun = threading.Event()
# Set by a pass that could not reach everything (see LeftForLater).
_retry_wanted = threading.Event()
_retry_timer: threading.Timer | None = None
_retry_delay = 0.0


def _schedule_retry(wanted: bool) -> None:
    """After a pass: run another one later when this one could not reach a
    library folder, waiting twice as long each time that stays so. Nothing
    else would start it before the next scan or restart."""
    global _retry_timer, _retry_delay
    if _retry_timer is not None:
        _retry_timer.cancel()
        _retry_timer = None
    if not wanted:
        _retry_delay = 0.0
        return
    _retry_delay = min(_retry_delay * 2 or UNREACHABLE_RETRY_SECONDS, UNREACHABLE_RETRY_MAX_SECONDS)
    _retry_timer = threading.Timer(_retry_delay, start_loudness_backfill)
    _retry_timer.name = "loudness-backfill-retry"
    _retry_timer.daemon = True
    _retry_timer.start()
    logger.info("Loudness backfill: trying again in %d minutes", round(_retry_delay / 60))


def _run_until_idle() -> None:
    while True:
        try:
            while _rerun.is_set():
                _rerun.clear()
                _retry_wanted.clear()
                progress.begin()
                try:
                    changed = backfill_loudness(progress_cb=_log_progress)
                    logger.info("Loudness backfill complete: %d values stored", changed)
                    _schedule_retry(_retry_wanted.is_set())
                except Exception:
                    logger.exception("Loudness backfill failed")
                finally:
                    progress.end()
        finally:
            _run_lock.release()
        # A request that arrived between the last check and the release
        # found the lock held and left the work to this thread.
        if not _rerun.is_set() or not _run_lock.acquire(blocking=False):
            return


def start_loudness_backfill() -> None:
    """Run the backfill on a daemon thread (ffmpeg for minutes must never
    touch the event loop). Calls made while it's running queue exactly one
    more pass, so tracks added by a scan mid-run are still picked up."""
    _rerun.set()
    if not _run_lock.acquire(blocking=False):
        return
    spawn("loudness-backfill", _run_until_idle)
