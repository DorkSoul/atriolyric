"""Download renditions: a video as ONE ordinary MP4 at a chosen height.

Unlike the HLS sessions (app.hls), these are real files kept in
`settings.rendition_dir` as `<item id>_<quality>.mp4`, so a phone can download
them. One background worker thread runs the encodes strictly one at a time
(the rest wait in order), so a batch of download requests cannot starve live
playback. A file written before its source's last change is stale and is
rebuilt. The cache is capped (ATRIOLYRIC_RENDITION_CACHE_MAX_BYTES) and the
least recently used file goes first; a file being written has a dot-prefixed
temporary name and is never a candidate. Failures are remembered briefly, as
in app.transcode.
"""

import functools
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.background import spawn
from app.config import settings
from app.hls import VAAPI_DEVICE, vaapi_usable
from app.models import MediaItem
from app.video_meta import FFMPEG_BINARY, FFPROBE_BINARY, INPUT_PROTOCOLS

logger = logging.getLogger(__name__)

# Heights offered, highest first. "Original" is the untouched file and is not here.
LEVELS = (1080, 720, 480)
# Video bitrate ceiling per level, kbit/s. The software encoder uses it as
# the CRF's maxrate; the hardware encoder targets 80% of it.
MAXRATE_KBPS = {1080: 6000, 720: 3500, 480: 1500}
# A rendition that failed is not offered as anything but `failed` for this
# long (per item, level and source version), unless a POST asks again.
FAILURE_MEMORY_SECONDS = 3600
MAX_REMEMBERED_FAILURES = 1024
# The encode timeout grows with the video, within these bounds.
MIN_TIMEOUT_SECONDS = 600
MAX_TIMEOUT_SECONDS = 6 * 3600
SHUTDOWN_JOIN_SECONDS = 5

# Lines of ffmpeg's `-progress` report; anything else on the pipe is an error message.
_PROGRESS_KEYS = frozenset(
    {
        "frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms",
        "out_time", "dup_frames", "drop_frames", "speed", "progress",
    }
)


@dataclass
class _Job:
    item_id: int
    quality: int
    source: Path
    source_mtime_ns: int
    duration: float | None
    status: str = "queued"
    progress: float | None = None


# One lock for the queue, the job table, the failure memory and the worker's
# handles; the worker waits on it for work.
_lock = threading.Condition()
_pending: deque[_Job] = deque()
_jobs: dict[tuple[int, int], _Job] = {}
_failures: OrderedDict[tuple[int, int, int], float] = OrderedDict()
# The running worker and its stop flag (a new flag per worker, so one that
# is still winding down after a shutdown never mistakes a restart for "carry on").
_worker_thread: threading.Thread | None = None
_worker_stop: threading.Event | None = None
_current_proc: subprocess.Popen[str] | None = None


def parse_quality(value: str) -> int | None:
    return int(value) if value in {str(level) for level in LEVELS} else None


@functools.lru_cache(maxsize=512)
def _probe_height(path: str, _mtime_ns: int) -> int | None:
    """The first video stream's height. Raises when ffprobe could not be run
    or timed out (not cached, so a slow disk is asked again); a file ffprobe
    answered for without a usable height is remembered as None."""
    result = subprocess.run(  # noqa: S603 - fixed argv list, no shell; the path comes from the library
        [
            FFPROBE_BINARY, "-v", "quiet", "-print_format", "json",
            "-select_streams", "v:0", "-show_entries", "stream=height",
            *INPUT_PROTOCOLS, path,
        ],
        capture_output=True, timeout=30, check=False,
    )
    try:
        height = int(json.loads(result.stdout)["streams"][0]["height"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return height if height > 0 else None


def source_height(source: Path) -> int | None:
    try:
        return _probe_height(str(source), source.stat().st_mtime_ns)
    except (OSError, subprocess.SubprocessError):
        return None


def offered_levels(height: int | None) -> list[int]:
    """Levels strictly below the source height (never an upscale); all of
    them when the height is unknown."""
    return [level for level in LEVELS if height is None or level < height]


def rendition_path(item_id: int, quality: int) -> Path:
    return settings.rendition_dir / f"{item_id}_{quality}.mp4"


def _source_mtime_ns(source: Path) -> int:
    try:
        return source.stat().st_mtime_ns
    except OSError:
        return 0


def _failed_recently(key: tuple[int, int, int]) -> bool:
    """Caller holds `_lock`."""
    expires = _failures.get(key)
    if expires is None:
        return False
    if expires < time.monotonic():
        del _failures[key]
        return False
    return True


def _remember_failure(key: tuple[int, int, int]) -> None:
    """Caller holds `_lock`."""
    _failures[key] = time.monotonic() + FAILURE_MEMORY_SECONDS
    _failures.move_to_end(key)
    while len(_failures) > MAX_REMEMBERED_FAILURES:
        _failures.popitem(last=False)


def _ready_size(item_id: int, quality: int, source: Path) -> int | None:
    """The finished rendition's size, or None when there is none or it is
    older than its source (stale)."""
    try:
        stat = rendition_path(item_id, quality).stat()
        if stat.st_mtime >= source.stat().st_mtime:
            return stat.st_size
    except OSError:
        pass
    return None


def _state(status: str, progress: float | None = None, size: int | None = None) -> dict[str, Any]:
    return {"status": status, "progress": progress, "size": size}


def status(item_id: int, quality: int, source: Path) -> dict[str, Any]:
    """{"status", "progress", "size"} for one rendition; starts nothing."""
    size = _ready_size(item_id, quality, source)
    if size is not None:
        return _state("ready", size=size)
    with _lock:
        job = _jobs.get((item_id, quality))
        if job is not None:
            return _state(job.status, progress=job.progress)
        if _failed_recently((item_id, quality, _source_mtime_ns(source))):
            return _state("failed")
    return _state("none")


def request(item: MediaItem, quality: int, source: Path) -> dict[str, Any]:
    """Queue the rendition unless it is ready or already queued/running
    (then this joins it). A remembered failure is forgotten: asking again
    is how a failed one is retried."""
    global _worker_thread, _worker_stop
    if _ready_size(item.id, quality, source) is None:
        mtime_ns = _source_mtime_ns(source)
        with _lock:
            if (item.id, quality) not in _jobs:
                _failures.pop((item.id, quality, mtime_ns), None)
                job = _Job(item.id, quality, source, mtime_ns, item.duration_seconds)
                _jobs[(item.id, quality)] = job
                _pending.append(job)
                if _worker_thread is None or not _worker_thread.is_alive():
                    _worker_stop = threading.Event()
                    _worker_thread = spawn("rendition-worker", _worker, _worker_stop)
                _lock.notify_all()
    return status(item.id, quality, source)


def touch(path: Path) -> None:
    """Mark a rendition recently used (the LRU order). Only the access time
    moves: the modification time is what says whether it is stale."""
    try:
        os.utime(path, (time.time(), path.stat().st_mtime))
    except OSError:
        pass


def build_command(source: Path, out: Path, quality: int, use_hw: bool) -> list[str]:
    maxrate = MAXRATE_KBPS[quality]
    # Target height, never above the source's (the height may be unknown
    # when the level was offered), even; the width follows the aspect, even.
    scale = f"scale=-2:trunc(min(ih\\,{quality})/2)*2"
    command = [FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if use_hw:
        # As in app.hls: decode and scale on the CPU, encode on the GPU.
        command += ["-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}", "-filter_hw_device", "va"]
    command += [*INPUT_PROTOCOLS, "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if use_hw:
        command += [
            "-vf", f"{scale},format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-b:v", f"{maxrate * 8 // 10}k", "-maxrate", f"{maxrate}k",
        ]
    else:
        command += [
            "-vf", scale,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-maxrate", f"{maxrate}k", "-bufsize", f"{maxrate * 2}k", "-pix_fmt", "yuv420p",
        ]
    command += [
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "+faststart", "-f", "mp4",
        "-progress", "pipe:1", "-nostats", str(out),
    ]
    return command


def encode_timeout(duration_seconds: float | None) -> float:
    return min(MAX_TIMEOUT_SECONDS, max(MIN_TIMEOUT_SECONDS, (duration_seconds or 0) * 3))


def _run_ffmpeg(job: _Job, command: list[str], stop: threading.Event) -> bool:
    """Run one ffmpeg to the end, updating job.progress from its -progress
    report. False when it failed, timed out or was stopped."""
    global _current_proc
    with _lock:
        if stop.is_set():
            return False
        # stderr shares the pipe that is read below, so a file that logs a
        # lot can never block ffmpeg on an unread pipe.
        proc = subprocess.Popen(  # noqa: S603 - fixed argv list, no shell; the path comes from the library
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
        )
        _current_proc = proc
    watchdog = threading.Timer(encode_timeout(job.duration), proc.kill)
    watchdog.daemon = True
    watchdog.start()
    last_error = ""
    try:
        assert proc.stdout is not None  # stdout=PIPE above
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us":
                if value.isdigit() and job.duration and job.duration > 0:
                    job.progress = min(0.99, int(value) / 1e6 / job.duration)
            elif key not in _PROGRESS_KEYS and not key.startswith("stream_") and line.strip():
                last_error = line.strip()[-300:]
        code = proc.wait()
        if code != 0 and not stop.is_set():
            logger.warning(
                "ffmpeg exited %s for the %sp rendition of media %s: %s",
                code, job.quality, job.item_id, last_error or "no message",
            )
        return code == 0 and not stop.is_set()
    finally:
        watchdog.cancel()
        with _lock:
            _current_proc = None
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _transcode(job: _Job, tmp: Path, stop: threading.Event) -> bool:
    use_hw = vaapi_usable()
    if _run_ffmpeg(job, build_command(job.source, tmp, job.quality, use_hw), stop):
        return True
    if use_hw and not stop.is_set():
        logger.warning(
            "VAAPI rendition encode failed for media %s, retrying in software", job.item_id
        )
        job.progress = 0.0 if job.duration else None
        return _run_ffmpeg(job, build_command(job.source, tmp, job.quality, False), stop)
    return False


def _process(job: _Job, stop: threading.Event) -> None:
    out = rendition_path(job.item_id, job.quality)
    tmp = settings.rendition_dir / f".{out.name}.{uuid.uuid4().hex}.tmp"
    ok = False
    changed = False
    try:
        settings.rendition_dir.mkdir(parents=True, exist_ok=True)
        ok = _transcode(job, tmp, stop)
        if ok:
            # The source was replaced while it was being encoded: the result
            # would look newer than a file it was not made from.
            changed = _source_mtime_ns(job.source) != job.source_mtime_ns
            if changed:
                ok = False
            else:
                tmp.replace(out)
    except Exception as exc:  # noqa: BLE001 - one bad job must not end the worker
        ok = False
        logger.warning(
            "Rendition of media %s at %sp failed (%s)", job.item_id, job.quality, type(exc).__name__
        )
    finally:
        tmp.unlink(missing_ok=True)
        with _lock:
            if _jobs.get((job.item_id, job.quality)) is job:
                del _jobs[(job.item_id, job.quality)]
            if not ok and not changed and not stop.is_set():
                _remember_failure((job.item_id, job.quality, job.source_mtime_ns))
    if ok:
        enforce_cache_budget(keep=out)


def _worker(stop: threading.Event) -> None:
    try:
        # Whatever a killed server left half-written.
        if settings.rendition_dir.is_dir():
            for leftover in settings.rendition_dir.glob(".*.tmp"):
                leftover.unlink(missing_ok=True)
    except OSError:
        pass
    while True:
        with _lock:
            while not _pending and not stop.is_set():
                _lock.wait()
            if stop.is_set():
                return
            job = _pending.popleft()
            job.status = "preparing"
            job.progress = 0.0 if job.duration else None
        _process(job, stop)


def enforce_cache_budget(max_bytes: int | None = None, keep: Path | None = None) -> None:
    """Evict the least recently used renditions until the budget fits.

    "Used" is the later of access and modification time, as in
    app.cleanup.enforce_cache_budget (serving a file sets its access time).
    A file still being written has a dot-prefixed `.tmp` name and is never
    a candidate; `keep` (the one just finished) is counted but not removed."""
    budget = settings.rendition_cache_max_bytes if max_bytes is None else max_bytes
    files = []
    total = 0
    directory = settings.rendition_dir
    if directory.is_dir():
        for path in directory.iterdir():
            if path.suffix != ".mp4" or path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            total += stat.st_size
            if path != keep:
                files.append((max(stat.st_atime, stat.st_mtime), stat.st_size, path))
    for _used, size, path in sorted(files):
        if total <= budget:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


def shutdown_renditions() -> None:
    """Stop the worker and any running ffmpeg; queued jobs are dropped (the
    half-written file is removed by the worker on its way out)."""
    global _worker_thread, _worker_stop
    with _lock:
        if _worker_stop is not None:
            _worker_stop.set()
        thread = _worker_thread
        _worker_thread = None
        _worker_stop = None
        _pending.clear()
        _jobs.clear()
        proc = _current_proc
        _lock.notify_all()
    if proc is not None and proc.poll() is None:
        proc.kill()
    if thread is not None and thread is not threading.current_thread():
        thread.join(SHUTDOWN_JOIN_SECONDS)
