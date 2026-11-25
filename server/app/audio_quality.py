"""Audio quality of a track: codec, bitrate, sample rate and bit depth, read
from the stream header mutagen already parses for the duration, and the
short label the app shows for it ("FLAC 16/44", "MP3 320", "Opus 128").

Nothing here touches the database; app.scanner stores the values on
MediaItem and app.routers.library.stats aggregates them."""

from pathlib import Path
from typing import Any

import mutagen

# Codecs that keep every bit: they get a bit depth and a "depth/rate" label.
LOSSLESS_CODECS = frozenset(
    {"flac", "alac", "wav", "aiff", "wavpack", "ape", "tta", "dsd", "optimfrog"}
)
# File extensions of formats that are always lossless, for rows a scan has
# not read the header of yet (".m4a" can be either, so it is not here).
LOSSLESS_EXTENSIONS = frozenset(
    {"flac", "wav", "aif", "aiff", "ape", "wv", "tta", "dsf", "dff"}
)

# mutagen's file class (easy variants included) -> codec.
_CLASS_CODECS = {
    "FLAC": "flac",
    "OggFLAC": "flac",
    "MP3": "mp3",
    "EasyMP3": "mp3",
    "OggOpus": "opus",
    "OggVorbis": "vorbis",
    "OggSpeex": "speex",
    "WAVE": "wav",
    "AIFF": "aiff",
    "ASF": "wma",
    "WavPack": "wavpack",
    "MonkeysAudio": "ape",
    "TrueAudio": "tta",
    "EasyTrueAudio": "tta",
    "DSF": "dsd",
    "DSDIFF": "dsd",
    "Musepack": "musepack",
    "OptimFROG": "optimfrog",
    "AAC": "aac",
    "AC3": "ac3",
}

_DISPLAY = {
    "flac": "FLAC",
    "alac": "ALAC",
    "mp3": "MP3",
    "aac": "AAC",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "speex": "Speex",
    "wav": "WAV",
    "aiff": "AIFF",
    "wma": "WMA",
    "wavpack": "WavPack",
    "ape": "APE",
    "tta": "TTA",
    "dsd": "DSD",
    "musepack": "Musepack",
    "optimfrog": "OptimFROG",
    "ac3": "AC-3",
}


def codec_display_name(codec: str) -> str:
    """"flac" -> "FLAC", "opus" -> "Opus"; an unknown codec upper-cased."""
    return _DISPLAY.get(codec, codec.upper())


def is_lossless(codec: str | None, extension: str | None = None) -> bool:
    if codec:
        return codec in LOSSLESS_CODECS
    return (extension or "").lstrip(".").lower() in LOSSLESS_EXTENSIONS


def _khz(sample_rate_hz: int) -> str:
    # 44100 -> "44", 96000 -> "96": the short form the label uses.
    return str(sample_rate_hz // 1000)


def format_label(
    codec: str | None,
    bit_depth: int | None,
    sample_rate_hz: int | None,
    bitrate_kbps: int | None,
    extension: str | None,
) -> str | None:
    """"FLAC 16/44", "FLAC 24/96", "MP3 320", "Opus 128"; the codec alone
    when the numbers are missing; the upper-cased file extension when the
    codec is not known (not scanned yet); None without either."""
    if codec:
        name = codec_display_name(codec)
        if codec in LOSSLESS_CODECS:
            if bit_depth and sample_rate_hz and codec != "dsd":
                return f"{name} {bit_depth}/{_khz(sample_rate_hz)}"
            return name
        if bitrate_kbps:
            return f"{name} {bitrate_kbps}"
        return name
    ext = (extension or "").lstrip(".").upper()
    return ext or None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _codec_of(audio: Any) -> str | None:
    name = type(audio).__name__
    if name in ("MP4", "EasyMP4"):
        # "mp4a.40.2" (AAC LC), "mp4a.40.5" (HE-AAC), "alac", "ac-3"...
        raw = str(getattr(audio.info, "codec", "") or "").lower()
        if raw.startswith("mp4a"):
            return "aac"
        if raw == "alac":
            return "alac"
        if raw in ("ac-3", "ec-3"):
            return "ac3"
        return raw.split(".")[0] or "aac"
    return _CLASS_CODECS.get(name)


def quality_from_audio(audio: Any, path: Path, file_size: int | None = None) -> dict[str, Any]:
    """codec, bitrate_kbps, sample_rate_hz and bit_depth of an opened
    mutagen file (easy or not). The codec falls back to the file extension
    so a row read once is never read again for want of a codec."""
    info = getattr(audio, "info", None) if audio is not None else None
    codec = _codec_of(audio) if audio is not None else None
    if codec is None:
        codec = path.suffix.lstrip(".").lower() or None
    sample_rate = _positive_int(getattr(info, "sample_rate", None))
    bit_depth = _positive_int(getattr(info, "bits_per_sample", None))
    if not is_lossless(codec):
        bit_depth = None
    bitrate_bps = _positive_int(getattr(info, "bitrate", None))
    if bitrate_bps is None:
        # Opus and a few others report no bitrate: the average from the
        # file size is close enough for a label.
        length = getattr(info, "length", None) or 0
        if file_size is None:
            try:
                file_size = path.stat().st_size
            except OSError:
                file_size = None
        if length and length > 0 and file_size:
            bitrate_bps = int(file_size * 8 / length)
    bitrate_kbps = round(bitrate_bps / 1000) if bitrate_bps else None
    return {
        "codec": codec,
        "bitrate_kbps": bitrate_kbps or None,
        "sample_rate_hz": sample_rate,
        "bit_depth": bit_depth,
    }


def read_audio_quality(path: Path, file_size: int | None = None) -> dict[str, Any]:
    """quality_from_audio for a file on disk: one header read, no tags.
    A file mutagen cannot parse still gets its extension as the codec."""
    try:
        audio = mutagen.File(path)
    except Exception:  # noqa: BLE001 - mutagen raises many different errors on a corrupt or odd file
        audio = None
    return quality_from_audio(audio, path, file_size)


QUALITY_COLUMNS = ("codec", "bitrate_kbps", "sample_rate_hz", "bit_depth")
