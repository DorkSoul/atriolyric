import logging
import subprocess
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from pathlib import Path

from app.config import settings
from app.models import MediaItem
from app.video_meta import INPUT_PROTOCOLS

# Kept in sync with the quality options offered in Settings on the
# frontend - streaming only ever accepts one of these, so cache filenames
# stay predictable and bounded.
ALLOWED_BITRATES_KBPS = (96, 128, 192, 320)

# A capped bitrate only kicks in once the source is meaningfully above it -
# otherwise a 128kbps MP3 would get needlessly re-encoded (and slightly
# degraded) just because its estimated bitrate is 129kbps.
HEADROOM_FACTOR = 1.1
_cache_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_cache_locks_guard = threading.Lock()
_ffmpeg_slots = threading.BoundedSemaphore(2)
logger = logging.getLogger(__name__)

# A transcode that failed or timed out is not tried again for this long (per
# track, bitrate and source version): otherwise every Range request for the
# track would wait the full timeout again before falling back to the original.
FAILURE_MEMORY_SECONDS = 3600
MAX_REMEMBERED_FAILURES = 1024
_failures: OrderedDict[tuple[int, int, int], float] = OrderedDict()
_failures_lock = threading.Lock()
# The transcode timeout grows with the track (a long DJ mix on a slow NAS
# takes far longer than a pop song), within these bounds.
MIN_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 900


def transcode_timeout(duration_seconds: float | None) -> float:
    return min(MAX_TIMEOUT_SECONDS, max(MIN_TIMEOUT_SECONDS, (duration_seconds or 0) * 0.5))


def _failed_recently(key: tuple[int, int, int]) -> bool:
    with _failures_lock:
        expires = _failures.get(key)
        if expires is None:
            return False
        if expires < time.monotonic():
            del _failures[key]
            return False
        return True


def _remember_failure(key: tuple[int, int, int]) -> None:
    with _failures_lock:
        _failures[key] = time.monotonic() + FAILURE_MEMORY_SECONDS
        _failures.move_to_end(key)
        while len(_failures) > MAX_REMEMBERED_FAILURES:
            _failures.popitem(last=False)


def _cache_lock(path: Path) -> threading.Lock:
    with _cache_locks_guard:
        return _cache_locks.setdefault(str(path), threading.Lock())


def estimate_source_bitrate_kbps(
    file_path: Path, duration_seconds: float | None
) -> float | None:
    """Cheap, format-agnostic estimate from file size / duration - avoids
    depending on mutagen's bitrate field, which several lossless formats
    don't populate consistently. Always correctly reads lossless files as
    "very high bitrate", which is exactly the case a cap should catch."""
    if not duration_seconds or duration_seconds <= 0:
        return None
    try:
        size_bits = file_path.stat().st_size * 8
    except OSError:
        return None
    return (size_bits / duration_seconds) / 1000


def ensure_transcoded(
    item: MediaItem, file_path: Path, target_kbps: int
) -> Path | None:
    """Returns a cached, capped-bitrate MP3 for this track, transcoding it
    with ffmpeg on first request. Returns None - meaning "stream the
    original file as-is" - whenever a transcode isn't needed or isn't
    possible, so callers always have a safe fallback and never end up
    upsampling a lower-quality source to the requested cap."""
    if target_kbps not in ALLOWED_BITRATES_KBPS:
        return None

    source_kbps = estimate_source_bitrate_kbps(file_path, item.duration_seconds)
    if source_kbps is None or source_kbps <= target_kbps * HEADROOM_FACTOR:
        return None

    out_dir = settings.transcode_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{item.id}_{target_kbps}k.mp3"

    try:
        source_stat = file_path.stat()
    except OSError:
        return None
    source_mtime = source_stat.st_mtime
    failure_key = (item.id, target_kbps, source_stat.st_mtime_ns)
    with _cache_lock(out_path):
        if out_path.is_file() and out_path.stat().st_mtime >= source_mtime:
            return out_path
        if _failed_recently(failure_key):
            return None
        tmp_path = out_dir / f".{out_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with _ffmpeg_slots:
                subprocess.run(  # noqa: S603 - fixed argv list, no shell; paths come from the library, never a command
                    [  # noqa: S607 - ffmpeg is resolved from PATH on purpose
                        "ffmpeg",
                        "-y",
                        *INPUT_PROTOCOLS,
                        "-i",
                        str(file_path),
                        "-vn",
                        "-b:a",
                        f"{target_kbps}k",
                        "-f",
                        "mp3",
                        str(tmp_path),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=transcode_timeout(item.duration_seconds),
                )
            tmp_path.replace(out_path)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
            OSError,
        ) as exc:
            tmp_path.unlink(missing_ok=True)
            _remember_failure(failure_key)
            logger.warning(
                "Transcode failed for media %s at %sk (%s); streaming the original for the next hour",
                item.id, target_kbps, type(exc).__name__,
            )
            return None
    return out_path
