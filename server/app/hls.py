"""On-the-fly HLS transcoding for videos browsers can't decode natively.

Nothing is kept as a second copy of the file. The playlist is generated up
front from the video's duration (fixed 6 s segments, marked VOD, so the player
knows the full length and can seek anywhere immediately), and segments are
encoded on demand by one ffmpeg process per viewing session:

- the process starts at the first segment asked for and keeps encoding
  forward, but is paused (SIGSTOP) once it is a few segments ahead of the
  player, so an idle or paused viewer costs no CPU;
- a request far from where the encoder is (a seek) restarts it there - the
  keyframes are forced onto the same 6 s grid, so segment N always begins at
  N * 6 s whichever run produced it;
- finished segments stay cached for a while (a 10 minute window behind the
  playhead, 10 minutes of idleness, and a total size cap), then are deleted.
  Nothing is ever stored as a permanent copy of the video.

A second, audio-only flavour of the same machinery (`audio_kbps`) serves
a video's sound without its picture, for background playback on phones that
suspend a hidden <video>. It shares the playlist grid and session handling, but
keeps its own cache directory and encoder, and never touches the GPU.

Video is always re-encoded (never copied) because copied video can't be
forced onto the keyframe grid the pre-built playlist promises. It uses VAAPI
(iGPU) for the encode when available, otherwise libx264.
"""

import itertools
import logging
import math
import os
import shutil
import signal

# Fixed argument vectors, never a shell.
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from app.background import spawn
from app.config import settings
from app.video_meta import INPUT_PROTOCOLS, probe_av_codecs

logger = logging.getLogger(__name__)

SEGMENT_SECONDS = 6
# How far past the newest finished segment a request may land before it counts
# as a seek that needs the encoder restarted there.
RESTART_GAP_SEGMENTS = 3
# The encoder is paused while it is this many segments ahead of the player.
AHEAD_SEGMENTS = 8
# Finished segments are kept (so rewinding, or coming back to a video shortly
# after, doesn't re-encode) until they are this far behind the player, the
# session has been idle this long, or the cache goes over its size cap.
KEEP_BEHIND_SEGMENTS = 100
IDLE_SECONDS = 600
# An idle session's encoder is stopped much sooner than its cached segments
# are deleted.
ENCODER_IDLE_SECONDS = 60
SEGMENT_WAIT_SECONDS = 120
# A segment that took longer than this to appear is logged.
SLOW_SEGMENT_LOG_SECONDS = 3
VAAPI_DEVICE = "/dev/dri/renderD128"
# At the session cap a new viewer may only take over a session nobody has
# asked for in this long; when every session is busier than that the new
# viewer gets a 503 instead of killing someone else's encoder (else three
# viewers on a cap of two would evict each other in turn, and none get video).
ACTIVE_SESSION_SECONDS = 30
# ffmpeg's stderr goes to this file in the session directory rather than a
# pipe nobody reads while it runs: a damaged video makes it log more than a
# pipe holds (64 KB), it blocks on the write and stops encoding for good.
STDERR_LOG = "_ffmpeg.log"
# The log is emptied when it grows past this (it is opened in append mode,
# so ffmpeg keeps writing at the new end), and only its tail is ever read.
STDERR_LOG_MAX_BYTES = 1024 * 1024
STDERR_TAIL_BYTES = 4096


def segment_count(duration_seconds: float) -> int:
    return max(1, math.ceil(duration_seconds / SEGMENT_SECONDS))


def build_playlist(duration_seconds: float, segment_suffix: str = "") -> str:
    count = segment_count(duration_seconds)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{SEGMENT_SECONDS}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for index in range(count):
        length = min(SEGMENT_SECONDS, duration_seconds - index * SEGMENT_SECONDS)
        lines.append(f"#EXTINF:{max(length, 0.001):.3f},")
        lines.append(f"{index}.ts{segment_suffix}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


_vaapi_ok: bool | None = None
_vaapi_lock = threading.Lock()


def vaapi_usable() -> bool:
    """One-off test encode on the render device, remembered for the process."""
    global _vaapi_ok
    mode = settings.hwaccel.lower()
    if mode == "none":
        return False
    with _vaapi_lock:
        if _vaapi_ok is None:
            _vaapi_ok = _test_vaapi()
            if not _vaapi_ok and mode == "vaapi":
                # Explicitly requested hardware encoding: say loudly that it
                # is not happening, instead of the routine INFO line below.
                logger.error(
                    "ATRIOLYRIC_HWACCEL=vaapi but VAAPI is not usable (is /dev/dri "
                    "mapped into the container and its render group added?); "
                    "falling back to software encoding"
                )
            logger.info(
                "HLS video encoder: %s",
                "VAAPI (hardware)" if _vaapi_ok else "libx264 (software)",
            )
        return _vaapi_ok


def _test_vaapi() -> bool:
    if not os.path.exists(VAAPI_DEVICE):
        return False
    try:
        subprocess.run(  # noqa: S603 - fixed argv list, no shell
            [  # noqa: S607 - ffmpeg is resolved from PATH on purpose (see the Dockerfile)
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}",
                "-filter_hw_device", "va",
                "-f", "lavfi", "-i", "testsrc=size=256x144:rate=10:duration=0.5",
                "-vf", "format=nv12,hwupload",
                "-c:v", "h264_vaapi", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _encode_command(
    file_path: Path,
    out_dir: Path,
    start_index: int,
    use_hw: bool,
    audio_kbps: int | None = None,
) -> list[str]:
    start_seconds = start_index * SEGMENT_SECONDS
    if audio_kbps:
        return [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(start_seconds), *INPUT_PROTOCOLS, "-i", str(file_path),
            "-map", "0:a:0?", "-vn", "-sn", "-dn",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2",
            "-output_ts_offset", str(start_seconds),
            "-f", "hls",
            "-hls_time", str(SEGMENT_SECONDS),
            "-hls_segment_type", "mpegts",
            "-hls_list_size", "0",
            "-hls_flags", "temp_file+independent_segments",
            "-start_number", str(start_index),
            "-hls_segment_filename", str(out_dir / "%d.ts"),
            str(out_dir / "_ffmpeg.m3u8"),
        ]
    keyframes = f"expr:gte(t,n_forced*{SEGMENT_SECONDS})"
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if use_hw:
        # Decode on the CPU (iGPUs can't hardware-decode everything, e.g.
        # 10-bit H.264) and use the GPU just for the H.264 encode.
        command += [
            "-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}", "-filter_hw_device", "va",
        ]
    command += ["-ss", str(start_seconds), *INPUT_PROTOCOLS, "-i", str(file_path)]
    command += ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if use_hw:
        command += [
            "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-b:v", "6M", "-maxrate", "8M",
        ]
    else:
        command += [
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p",
            "-sc_threshold", "0",
        ]
    command += [
        "-force_key_frames", keyframes, "-g", "99999",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-output_ts_offset", str(start_seconds),
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_segment_type", "mpegts",
        "-hls_list_size", "0",
        "-hls_flags", "temp_file+independent_segments",
        "-start_number", str(start_index),
        "-hls_segment_filename", str(out_dir / "%d.ts"),
        str(out_dir / "_ffmpeg.m3u8"),
    ]
    return command


@dataclass
class _Session:
    key: tuple[int, int, int]
    directory: Path
    audio_kbps: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    proc: subprocess.Popen | None = None
    start_index: int = 0
    stopped: bool = False
    use_hw: bool = True
    last_request: float = field(default_factory=time.monotonic)
    last_index: int = 0
    codecs: str | None = None
    # The newest request for this session (see get_segment).
    latest_token: int = 0
    latest_index: int = -1
    # Set (under `lock`) once the session is evicted or dropped: `_start`
    # must not launch an encoder into a directory that is being deleted.
    dead: bool = False

    def newest_finished(self) -> int:
        """Highest segment finished by the current run: the unbroken stretch
        from where it started (temp_file mode only renames a segment to its
        final name once done). Segments further on that are left from an
        earlier run - seek to 169, then back to 130 - are not this encoder's
        progress, and counting them would make a backward seek look "behind"
        the encoder forever, restarting it on every check."""
        newest = self.start_index - 1
        while (self.directory / f"{newest + 1}.ts").is_file():
            newest += 1
        return newest

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def signal(self, sig: int) -> None:
        proc = self.proc
        if proc is not None and self.alive():
            try:
                proc.send_signal(sig)
            except (OSError, ProcessLookupError):
                pass

    def pause(self) -> None:
        if self.alive() and not self.stopped:
            self.signal(signal.SIGSTOP)
            self.stopped = True

    def resume(self) -> None:
        if self.stopped:
            self.signal(signal.SIGCONT)
            self.stopped = False

    def kill(self) -> None:
        # Work on one read of `proc`: another thread may clear `self.proc`
        # meanwhile, and re-reading it (through alive()/signal()) would then
        # skip the kill and leave the encoder running.
        proc = self.proc
        if proc is not None and proc.poll() is None:
            signals = (signal.SIGCONT, signal.SIGKILL) if self.stopped else (signal.SIGKILL,)
            for sig in signals:
                try:
                    proc.send_signal(sig)
                except (OSError, ProcessLookupError):
                    pass
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        self.proc = None
        self.stopped = False

    def stderr_tail(self) -> str:
        """The end of the encoder's log, for the error message."""
        try:
            with open(self.directory / STDERR_LOG, "rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - STDERR_TAIL_BYTES))
                return log.read().decode(errors="replace")
        except OSError:
            return ""


_sessions: dict[tuple[int, int, int], _Session] = {}
_sessions_lock = threading.Lock()
_request_counter = itertools.count(1)
_janitor_started = False
# Set by shutdown_sessions, so the janitor thread ends instead of keeping the
# shutdown's join_all waiting on it. A fresh event per started janitor.
_janitor_stop = threading.Event()


def reset_hls_dir() -> None:
    """Startup: nothing survives a restart, so drop leftover segment dirs -
    and any whole-file remuxes left in video_cache/."""
    shutil.rmtree(settings.hls_dir, ignore_errors=True)
    shutil.rmtree(settings.database_path.parent / "video_cache", ignore_errors=True)


def shutdown_sessions() -> None:
    global _janitor_started
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
        _janitor_stop.set()
        _janitor_started = False
    for session in sessions:
        session.kill()
    shutil.rmtree(settings.hls_dir, ignore_errors=True)


def _retire(session: _Session) -> None:
    """Stop a session already taken out of `_sessions` and delete its files.
    Under its lock, so an in-flight `_start` either finishes before (and its
    encoder is killed here) or sees `dead` and launches nothing."""
    with session.lock:
        session.dead = True
        session.kill()
    with _sessions_lock:
        # The same viewer may be back already: their new session reuses the
        # directory (and cleared it itself), so it is not ours to delete.
        if session.key not in _sessions:
            shutil.rmtree(session.directory, ignore_errors=True)


def _drop(session: _Session) -> None:
    with _sessions_lock:
        if _sessions.get(session.key) is session:
            del _sessions[session.key]
    _retire(session)


def _janitor(stop: threading.Event | None = None) -> None:
    stop = stop or _janitor_stop
    tick = 0
    while not stop.wait(1):
        tick += 1
        try:
            _janitor_pass(tick)
        except Exception:  # noqa: BLE001 - one bad pass must not end pausing and the size cap for good
            logger.exception("HLS janitor pass failed")


def _janitor_pass(tick: int) -> None:
    with _sessions_lock:
        sessions = list(_sessions.values())
    now = time.monotonic()
    for session in sessions:
        if now - session.last_request > IDLE_SECONDS:
            _drop(session)
            continue
        with session.lock:
            if session.alive() and now - session.last_request > ENCODER_IDLE_SECONDS:
                session.kill()
            if session.alive():
                ahead = session.newest_finished() - session.last_index
                if ahead > AHEAD_SEGMENTS:
                    session.pause()
                else:
                    session.resume()
            try:
                log = session.directory / STDERR_LOG
                if log.stat().st_size > STDERR_LOG_MAX_BYTES:
                    os.truncate(log, 0)
            except OSError:
                pass
            try:
                for entry in session.directory.iterdir():
                    if entry.suffix == ".ts" and entry.stem.isdigit():
                        if int(entry.stem) < session.last_index - KEEP_BEHIND_SEGMENTS:
                            entry.unlink(missing_ok=True)
            except OSError:
                pass
    if tick % 10 == 0:
        _enforce_size_cap(sessions)


def _enforce_size_cap(sessions: list[_Session]) -> None:
    """Delete the oldest finished segments, ones the player has already passed
    first, until the cache is back under its cap."""
    entries: list[tuple[bool, float, int, Path]] = []
    total = 0
    for session in sessions:
        try:
            for entry in session.directory.iterdir():
                if entry.suffix != ".ts" or not entry.stem.isdigit():
                    continue
                stat = entry.stat()
                total += stat.st_size
                upcoming = int(entry.stem) >= session.last_index
                entries.append((upcoming, stat.st_mtime, stat.st_size, entry))
        except OSError:
            continue
    if total <= settings.hls_cache_max_bytes:
        return
    for _upcoming, _mtime, size, path in sorted(entries):
        if total <= settings.hls_cache_max_bytes:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


def _ensure_janitor() -> None:
    global _janitor_started, _janitor_stop
    with _sessions_lock:
        if _janitor_started:
            return
        _janitor_started = True
        _janitor_stop = threading.Event()
        stop = _janitor_stop
    spawn("hls-janitor", _janitor, stop)


def _get_session(item_id: int, user_id: int, audio_kbps: int | None = None) -> _Session:
    key = (item_id, user_id, audio_kbps or 0)
    with _sessions_lock:
        session = _sessions.get(key)
        if session is not None:
            return session
    # Outside the global lock: the first call runs a VAAPI test encode that
    # can take up to 30 s, and every other viewer's segment requests would
    # wait on it.
    use_hw = not audio_kbps and vaapi_usable()
    with _sessions_lock:
        session = _sessions.get(key)
        if session is not None:
            return session
        # Over the limit: take over the session idle longest, unless every
        # one is in use.
        while len(_sessions) >= max(1, settings.hls_max_sessions):
            oldest = min(_sessions.values(), key=lambda s: s.last_request)
            idle_for = time.monotonic() - oldest.last_request
            if idle_for < ACTIVE_SESSION_SECONDS:
                retry_after = max(1, math.ceil(ACTIVE_SESSION_SECONDS - idle_for))
                logger.info(
                    "HLS session cap reached: item=%s user=%s, all %s sessions active",
                    item_id, user_id, len(_sessions),
                )
                raise HTTPException(
                    503,
                    "Too many videos are being converted right now - try again in a moment",
                    headers={"Retry-After": str(retry_after)},
                )
            del _sessions[oldest.key]
            spawn("hls-evict", _retire, oldest)
        directory = settings.hls_dir / f"{item_id}_{user_id}{f'_audio{audio_kbps}' if audio_kbps else ''}"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        session = _Session(
            key=key,
            directory=directory,
            audio_kbps=audio_kbps,
            use_hw=use_hw,
        )
        _sessions[key] = session
        return session


def _start(session: _Session, file_path: Path, index: int) -> None:
    """(Re)start the encoder at segment `index`. Called under `session.lock`."""
    session.kill()
    if session.dead:
        return
    session.start_index = index
    session.directory.mkdir(parents=True, exist_ok=True)
    if session.codecs is None:
        probed = probe_av_codecs(file_path)
        session.codecs = "/".join(str(part) for part in probed) if probed else "unknown"
    logger.info(
        "HLS encoder start: item=%s index=%s (%ss) hw=%s audio_kbps=%s source=%s",
        session.key[0], index, index * SEGMENT_SECONDS, session.use_hw,
        session.audio_kbps, session.codecs,
    )
    # Truncated per start, append mode so the janitor can empty it under a
    # running encoder (see STDERR_LOG_MAX_BYTES).
    log_fd = os.open(
        session.directory / STDERR_LOG,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND,
        0o600,
    )
    try:
        session.proc = subprocess.Popen(  # noqa: S603 - fixed argv list, no shell; ffmpeg from PATH
            _encode_command(
                file_path, session.directory, index, session.use_hw, session.audio_kbps
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_fd,
        )
    finally:
        os.close(log_fd)  # the child has its own copy


def get_segment(
    item_id: int,
    user_id: int,
    file_path: Path,
    index: int,
    duration_seconds: float,
    audio_kbps: int | None = None,
) -> Path:
    """Path of finished segment `index`, encoding it first if need be. Blocks
    (the route is a sync handler, so it runs in a worker thread)."""
    if index < 0 or index >= segment_count(duration_seconds):
        raise HTTPException(404, "No such segment")
    _ensure_janitor()
    began = time.monotonic()
    deadline = began + SEGMENT_WAIT_SECONDS
    token = next(_request_counter)
    announced = False
    while True:
        # Re-fetched every pass: the janitor may have dropped an idle session.
        session = _get_session(item_id, user_id, audio_kbps)
        target = session.directory / f"{index}.ts"
        session.last_request = time.monotonic()
        with session.lock:
            if session.dead:
                # Evicted or dropped between the lookup and the lock; it is
                # already out of `_sessions`, so the next lookup is fresh.
                continue
            if not announced:
                session.latest_token = token
                session.latest_index = index
                announced = True
            session.last_index = index
            if target.is_file():
                if target.stat().st_size == 0:
                    # Segments only get their final name once complete, so an
                    # empty one is an encoder that started past the real end
                    # (the stored duration was longer than the file): ffmpeg
                    # exits 0 and leaves 0 bytes. There is no such segment.
                    raise HTTPException(404, "No such segment")
                waited = time.monotonic() - began
                if waited > SLOW_SEGMENT_LOG_SECONDS:
                    logger.info(
                        "HLS segment slow: item=%s index=%s waited=%.1fs audio_kbps=%s",
                        item_id, index, waited, audio_kbps,
                    )
                return target
            if session.latest_token != token and session.latest_index != index:
                # A newer request for a different segment (a seek) has taken
                # over this session. The client already abandoned this one, but
                # a sync handler can't see that - and if it kept waiting it
                # would keep restarting the encoder back to its own position,
                # each side killing the other's work (a seek would take 60-120 s).
                logger.info(
                    "HLS segment superseded: item=%s index=%s by index=%s audio_kbps=%s",
                    item_id, index, session.latest_index, audio_kbps,
                )
                raise HTTPException(409, "Superseded by a newer request")
            newest = session.newest_finished()
            # Before this run's start, or already past it but pruned away.
            behind = index < session.start_index or index <= newest
            far_ahead = index > newest + RESTART_GAP_SEGMENTS
            if not session.alive() and session.proc is not None and session.proc.returncode:
                # The encoder died. A broken hardware path falls back to
                # software once; anything else is a genuine failure.
                error = session.stderr_tail()
                if session.use_hw:
                    logger.warning(
                        "VAAPI encode failed, falling back to software: %s", error[-300:]
                    )
                    session.use_hw = False
                    _start(session, file_path, index)
                else:
                    logger.error("HLS transcode failed: %s", error[-300:])
                    session.proc = None
                    raise HTTPException(500, "Video transcode failed")
            elif session.proc is None or behind or far_ahead or not session.alive():
                # Not started, a seek, or the encoder finished its run without
                # producing this segment (it was pruned or never reached).
                _start(session, file_path, index)
            else:
                session.resume()
        if time.monotonic() > deadline:
            logger.warning(
                "HLS segment timed out: item=%s index=%s after %ss audio_kbps=%s",
                item_id, index, SEGMENT_WAIT_SECONDS, audio_kbps,
            )
            raise HTTPException(504, "Video transcode timed out")
        time.sleep(0.1)
