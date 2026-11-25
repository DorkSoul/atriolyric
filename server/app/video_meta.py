import json
import shutil

# Calls below use fixed argument vectors and never invoke a shell.
import subprocess
import threading
import uuid
from pathlib import Path

FFPROBE_BINARY = shutil.which("ffprobe") or "ffprobe"
FFMPEG_BINARY = shutil.which("ffmpeg") or "ffmpeg"
# Library files and uploads are untrusted input to ffmpeg, which can follow
# references inside a container (an HLS/concat playlist naming http:// or
# other local files). Input options, so each goes just before its `-i`:
# plain files, stdin and decryption only, never the network.
INPUT_PROTOCOLS = ("-protocol_whitelist", "file,pipe,crypto")


def probe_duration_seconds(path: Path) -> float | None:
    """Shell out to ffprobe for a video's duration. Returns None on any
    failure (binary missing, unreadable file, bad output) rather than
    raising - a scan shouldn't abort over one file's metadata."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv list, no shell; paths come from the library, never a command
            [
                FFPROBE_BINARY,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                *INPUT_PROTOCOLS,
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return None


def probe_av_codecs(path: Path) -> tuple[str | None, str | None, str | None] | None:
    """(video codec, video pixel format, audio codec) of the first video and
    audio streams, via ffprobe. A missing stream is None; None overall means
    the probe itself failed (callers should then leave the file alone)."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv list, no shell; paths come from the library, never a command
            [
                FFPROBE_BINARY,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                *INPUT_PROTOCOLS,
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        streams = json.loads(result.stdout)["streams"]
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return None
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return (
        video.get("codec_name") if video else None,
        video.get("pix_fmt") if video else None,
        audio.get("codec_name") if audio else None,
    )


def generate_thumbnail(path: Path, out_path: Path, at_seconds: float = 1.0) -> bool:
    """Extract one frame as a JPEG thumbnail via ffmpeg. Returns whether it
    succeeded; never raises, so a missing ffmpeg just means no thumbnail.

    Written to a temp file beside `out_path` and renamed into place, so a
    timeout or a crash mid-write never leaves a partial JPEG at the final
    path (where it would be served as the thumbnail from then on)."""
    tmp_path = out_path.parent / f".{out_path.stem}.{uuid.uuid4().hex}.jpg"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603 - fixed argv list, no shell; paths come from the library, never a command
            [
                FFMPEG_BINARY,
                "-y",
                "-ss",
                str(at_seconds),
                *INPUT_PROTOCOLS,
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(tmp_path),
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        if not tmp_path.is_file():
            return False
        tmp_path.replace(out_path)
        return True
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


# What browsers can decode natively. Anything else (HEVC, AV1, 10-bit H.264,
# AC-3/DTS audio, ...) plays as sound with a black picture, or not at all, so
# those videos are streamed through the HLS transcoder (app.hls) instead.
_BROWSER_H264_PIX_FMTS = ("yuv420p", "yuvj420p")
_BROWSER_AUDIO_CODECS = ("aac", "mp3", "opus", "vorbis")

_playable_cache: dict[tuple[str, int, int], bool] = {}
_playable_cache_lock = threading.Lock()


def is_browser_playable(path: Path) -> bool:
    """Whether every browser can play this file's streams as-is. Unprobeable
    files count as playable - the original is then streamed untouched.
    Cached per path and mtime/size
    since ffprobe is far too slow to run on every request."""
    try:
        stat = path.stat()
    except OSError:
        return True
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _playable_cache_lock:
        cached = _playable_cache.get(key)
    if cached is not None:
        return cached
    codecs = probe_av_codecs(path)
    if codecs is None:
        playable = True
    else:
        vcodec, pix_fmt, acodec = codecs
        video_ok = (
            vcodec is None
            or (vcodec == "h264" and pix_fmt in _BROWSER_H264_PIX_FMTS)
            or (vcodec in ("vp8", "vp9") and path.suffix.lower() == ".webm")
        )
        audio_ok = acodec is None or acodec in _BROWSER_AUDIO_CODECS
        playable = video_ok and audio_ok
    with _playable_cache_lock:
        if len(_playable_cache) > 4096:
            _playable_cache.clear()
        _playable_cache[key] = playable
    return playable
