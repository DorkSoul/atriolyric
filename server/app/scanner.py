import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.albums import assign_album, prune_orphan_albums
from app.art import ensure_art_thumb_cached
from app.artist_credits import (
    artist_tags_payload,
    credits_from_columns,
    load_split_rules,
    prune_orphan_artists,
    sync_track_credits,
)
from app.audio_quality import QUALITY_COLUMNS, quality_from_audio, read_audio_quality
from app.cleanup import (
    delete_media_cache,
    delete_media_records,
    listed_media_cache_files,
)
from app.config import settings, thumbnail_path
from app.metadata_providers.lyrics import get_embedded_lyrics, normalize_lyrics
from app.metadata_taxonomy import replace_item_genres
from app.models import LibraryRoot, MediaItem, MediaKind, utcnow
from app.video_meta import generate_thumbnail, probe_duration_seconds

logger = logging.getLogger(__name__)

# Picard/beets write every credited artist to a multi-value ARTISTS tag (and
# ALBUMARTISTS), keeping ARTIST as the display string. Vorbis comments expose
# these as plain keys; MP3 and MP4 need them mapped for easy mode.
for _key, _name in (("artists", "ARTISTS"), ("albumartists", "ALBUMARTISTS")):
    if _key not in EasyID3.valid_keys:
        EasyID3.RegisterTXXXKey(_key, _name)
    EasyMP4Tags.RegisterFreeformKey(_key, _name)


def title_or_stem(title: str | None, path: Path) -> str:
    """The stored title: the tag, or the file name when there is none
    (titles are never NULL)."""
    return title or path.stem


def _first_easy(tags: dict, key: str) -> str | None:
    values = tags.get(key)
    return values[0] if values else None


def _first_id3_frame(tags: dict, frame_id: str) -> str | None:
    frame = tags.get(frame_id)
    text = getattr(frame, "text", None)
    return str(text[0]) if text else None


def _first_musicbrainz_id(tags: dict, easy_key: str, txxx_name: str) -> str | None:
    """Read MusicBrainz IDs from both Vorbis/iTunes tags and raw ID3 frames."""
    value = _first_easy(tags, easy_key)
    if value:
        return str(value)

    value = _first_id3_frame(tags, f"TXXX:{txxx_name}")
    if value:
        return value

    # Some taggers write recording IDs to the standard MusicBrainz UFID
    # instead of a TXXX/Vorbis field. UFID stores the value as bytes.
    if easy_key == "musicbrainz_trackid":
        data = getattr(tags.get("UFID:http://musicbrainz.org"), "data", None)
        if data:
            try:
                return data.decode("ascii").strip()
            except (AttributeError, UnicodeDecodeError):
                return None
    return None


def _all_easy(tags: dict, key: str) -> list[str]:
    values = tags.get(key) or []
    return [str(value) for value in values if str(value).strip()]


def _all_id3_frame(tags: dict, frame_id: str) -> list[str]:
    text = getattr(tags.get(frame_id), "text", None) or []
    return [str(value) for value in text if str(value).strip()]


_DATE_RE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")


def _parse_release_date(raw: str | None) -> str | None:
    """Date tags show up in all sorts of precisions ("2005", "2005-03",
    "2005-03-01", "2005-03-01T00:00:00Z") depending on format/tagger -
    normalize whatever's there to a zero-padded "YYYY-MM-DD" (missing
    month/day default to the 1st) so plain string comparison still sorts
    chronologically regardless of how precise the original tag was."""
    if not raw:
        return None
    match = _DATE_RE.search(raw)
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{month or '01'}-{day or '01'}"


_GAIN_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
# Opus stores R128 gains as Q7.8 fixed point relative to -23 LUFS; the
# ReplayGain reference (-18 LUFS) is 5 dB louder.
R128_TO_REPLAYGAIN_DB = 5.0


def _parse_gain(raw: object) -> float | None:
    """"-6.54 dB" (any case/spacing) -> -6.54. Absurd values are treated as
    a broken tag rather than trusted."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    match = _GAIN_RE.search(str(raw)) if raw is not None else None
    if not match:
        return None
    value = float(match.group())
    return value if -60.0 <= value <= 60.0 else None


def _parse_r128(raw: object) -> float | None:
    try:
        q78 = int(str(raw).strip())
    except ValueError:
        return None
    return _parse_gain(q78 / 256 + R128_TO_REPLAYGAIN_DB)


def _raw_replaygain_text(path: Path, name: str) -> str | bytes | None:
    """First value of a ReplayGain tag that easy mode can't see: ID3 TXXX
    frames (EasyID3 maps the replaygain keys to RVA2 instead) and MP4
    freeform iTunes atoms."""
    try:
        raw = mutagen.File(path)
    except Exception:  # noqa: BLE001 - mutagen raises many different errors on a corrupt or odd file; treat all as "no tags"
        return None
    raw_tags = getattr(raw, "tags", None)
    if not raw_tags:
        return None
    if hasattr(raw_tags, "getall"):
        for frame in raw_tags.getall("TXXX"):
            if frame.desc.lower() == name and frame.text:
                return frame.text[0]
        return None
    for key, values in raw_tags.items():
        if key.lower() == f"----:com.apple.itunes:{name}" and values:
            return values[0]
    return None


def _raw_replaygain(path: Path, scope: str) -> float | None:
    return _parse_gain(_raw_replaygain_text(path, f"replaygain_{scope}_gain"))


def _parse_peak(raw: object) -> float | None:
    """"0.988553" -> 0.988553. Zero or absurd values are a broken tag."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    match = _GAIN_RE.search(str(raw)) if raw is not None else None
    if not match:
        return None
    value = float(match.group())
    return value if 0.0 < value <= 20.0 else None


def _track_peak_from_tags(path: Path, tags: dict) -> float | None:
    peak = _parse_peak(_first_easy(tags, "replaygain_track_peak"))
    if peak is None:
        peak = _parse_peak(_raw_replaygain_text(path, "replaygain_track_peak"))
    return peak


def _replaygain_from_tags(path: Path, tags: dict) -> tuple[float | None, float | None]:
    gains: list[float | None] = []
    for scope in ("track", "album"):
        gain = _parse_gain(_first_easy(tags, f"replaygain_{scope}_gain"))
        if gain is None:
            r128 = _first_easy(tags, f"r128_{scope}_gain")
            gain = _parse_r128(r128) if r128 is not None else None
        if gain is None:
            gain = _raw_replaygain(path, scope)
        gains.append(gain)
    return gains[0], gains[1]


def read_replaygain(path: Path) -> tuple[float | None, float | None]:
    """(track gain, album gain) in dB from whatever ReplayGain/R128 tags the
    file carries; None for each one it doesn't."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:  # noqa: BLE001 - mutagen raises many different errors on a corrupt or odd file; treat all as "no tags"
        return None, None
    if audio is None:
        return None, None
    return _replaygain_from_tags(path, audio.tags or {})


def read_track_peak(path: Path) -> float | None:
    """Linear track peak from a REPLAYGAIN_TRACK_PEAK tag, if the file has one."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:  # noqa: BLE001 - mutagen raises many different errors on a corrupt or odd file; treat all as "no tags"
        return None
    if audio is None:
        return None
    return _track_peak_from_tags(path, audio.tags or {})


def read_audio_tags(path: Path) -> dict:
    """Extract common tags across formats.

    ``easy=True`` normalizes tag access for MP3/FLAC/OGG/M4A, but mutagen
    doesn't wrap WAVE/AIFF's embedded ID3 frames in easy mode, so those fall
    back to reading the raw ID3 frames directly.
    """
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:  # noqa: BLE001 - mutagen raises many different errors on a corrupt or odd file; treat all as "no tags"
        return {}
    if audio is None:
        return {}
    tags = audio.tags or {}

    def first(easy_key: str, frame_id: str) -> str | None:
        return _first_easy(tags, easy_key) or _first_id3_frame(tags, frame_id)

    def joined(easy_key: str, frame_id: str) -> str | None:
        # A multi-valued ARTIST/ALBUMARTIST tag displays every value, not
        # just the first. The separate artists are kept in artist_tags_json,
        # so the links come from that list rather than from re-splitting
        # this display string.
        values = _all_easy(tags, easy_key) or _all_id3_frame(tags, frame_id)
        return ", ".join(values) if values else None

    raw_track = first("tracknumber", "TRCK")
    track_number = None
    if raw_track:
        try:
            track_number = int(str(raw_track).split("/")[0])
        except ValueError:
            track_number = None

    duration = getattr(audio.info, "length", None) if audio.info else None

    genres = _all_easy(tags, "genre") or _all_id3_frame(tags, "TCON")
    track_gain, album_gain = _replaygain_from_tags(path, tags)
    return {
        "title": first("title", "TIT2"),
        "artist": joined("artist", "TPE1"),
        "album_artist": joined("albumartist", "TPE2"),
        # Every value, not just the first: a multi-value ARTIST frame (ID3v2.4
        # null-separated, repeated Vorbis fields) lists each artist too.
        "artists_multi": _all_easy(tags, "artists")
        or _all_id3_frame(tags, "TXXX:ARTISTS")
        or _all_easy(tags, "artist")
        or _all_id3_frame(tags, "TPE1"),
        "album_artists_multi": _all_easy(tags, "albumartists")
        or _all_id3_frame(tags, "TXXX:ALBUMARTISTS")
        or _all_easy(tags, "albumartist")
        or _all_id3_frame(tags, "TPE2"),
        "album": first("album", "TALB"),
        # Keep the legacy primary genre for existing browse views, but retain
        # every embedded value in the normalized relationship used by mixes.
        "genre": genres[0] if genres else None,
        "genres": genres,
        "track_number": track_number,
        "duration_seconds": duration,
        "replaygain_track_gain": track_gain,
        "replaygain_album_gain": album_gain,
        "replaygain_track_peak": _track_peak_from_tags(path, tags),
        "release_date": _parse_release_date(first("date", "TDRC")),
        "musicbrainz_recording_mbid": _first_musicbrainz_id(
            tags, "musicbrainz_trackid", "MusicBrainz Track Id"
        )
        or _first_musicbrainz_id(
            tags, "musicbrainz_recordingid", "MusicBrainz Recording Id"
        ),
        "musicbrainz_artist_mbid": _first_musicbrainz_id(
            tags, "musicbrainz_artistid", "MusicBrainz Artist Id"
        ),
        "musicbrainz_isrc": first("isrc", "TSRC"),
        # codec, bitrate_kbps, sample_rate_hz, bit_depth from the same header.
        **quality_from_audio(audio, path),
    }


def read_video_metadata(path: Path) -> dict:
    """Tags for a video file, plus a `kind` classification.

    Some video containers (MP4/M4V/MOV) can carry the same iTunes-style tag
    atoms mutagen already reads for audio, so try that first - a file with
    both artist and album tagged is treated as a music video; anything else
    (a ripped concert, a compilation with no such tags) is a plain video.
    Duration falls back to `ffprobe` when mutagen didn't find one (it never
    will for MKV/WebM/AVI, which mutagen doesn't parse at all).
    """
    tags = read_audio_tags(path)
    kind = (
        MediaKind.music_video
        if (tags.get("artist") and tags.get("album"))
        else MediaKind.video
    )
    duration = tags.get("duration_seconds") or probe_duration_seconds(path)
    return {
        "title": tags.get("title"),
        "artist": tags.get("artist"),
        "album": tags.get("album"),
        "duration_seconds": duration,
        "kind": kind,
    }


# The cleanup batch size. New/changed rows are committed before
# expensive media processing so SQLite's writer lock is never held while
# ffmpeg or artwork extraction runs.
SCAN_COMMIT_BATCH_SIZE = 200
# "confirm_removals" is a normal scan that also performs a removal held back
# by the size checks below (see _removal_hold_reason).
SCAN_MODES = ("normal", "metadata", "full", "confirm_removals")
# A scan that would remove more than this share of a root's rows, and more
# than MISSING_HOLD_MIN_ROWS of them, removes nothing until an admin confirms.
MISSING_HOLD_PERCENT = 20
MISSING_HOLD_MIN_ROWS = 20
UNREADABLE_DIRS_REPORTED = 20
_scan_locks: dict[int, threading.Lock] = {}
_scan_locks_guard = threading.Lock()


def _maybe_generate_thumbnail(file_path: Path, media_item: MediaItem) -> None:
    duration = media_item.duration_seconds or 0
    at_seconds = min(duration / 10, 5.0) if duration else 1.0
    generate_thumbnail(file_path, thumbnail_path(media_item.id), at_seconds=at_seconds)


def scan_library_root(
    db: Session,
    root: LibraryRoot,
    progress_callback: Callable[[int, int], None] | None = None,
    mode: str = "normal",
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Scan a root. ``mode`` is "normal" (only new/changed files), "metadata"
    (also re-read tags and embedded lyrics of unchanged files, leaving art,
    thumbnails and loudness alone), "full" (redo everything for every file)
    or "confirm_removals" (a normal scan that removes missing tracks even when
    there are many of them; an unreadable folder still holds removal).

    ``should_stop`` is asked once per file (and per folder while walking);
    when it says yes the scan keeps what it has saved and returns with
    ``stopped`` set, without its removal step."""
    if mode not in SCAN_MODES:
        raise ValueError(f"Unknown scan mode: {mode}")
    with _scan_locks_guard:
        lock = _scan_locks.setdefault(root.id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise RuntimeError("A scan is already running for this library root")
    try:
        return _scan_library_root_unlocked(db, root, progress_callback, mode, should_stop)
    finally:
        lock.release()


def _scan_library_root_unlocked(
    db: Session,
    root: LibraryRoot,
    progress_callback: Callable[[int, int], None] | None = None,
    mode: str = "normal",
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Walk a library root, upsert changed files, drop ones that vanished."""
    confirm_removals = mode == "confirm_removals"
    if confirm_removals:
        mode = "normal"
    root_path = Path(root.path)
    if not root_path.is_dir():
        # A typo'd path is an error, not an empty library. (An unmounted
        # share is an existing empty folder; the removal hold covers that.)
        raise FileNotFoundError(
            f"Library root path does not exist or is not a directory: {root_path}"
        )

    seen_relative_paths: set[str] = set()
    added = updated = removed = failed = 0
    # Existing rows whose art and thumbnails were made again (not new files).
    art_redone = 0
    stopped = False
    split_rules = load_split_rules(db)
    artist_cache: dict = {}
    album_cache: dict = {}
    cache_listing: dict | None = None

    # Only what deciding "unchanged" needs, for every row of the root, not
    # whole ORM rows (enrichment JSON included) held for the length of the
    # scan. A changed file loads its full row.
    existing_by_path = {
        row.relative_path: row
        for row in db.execute(
            select(
                MediaItem.id,
                MediaItem.relative_path,
                MediaItem.kind,
                MediaItem.file_mtime,
                MediaItem.file_size,
                MediaItem.musicbrainz_isrc_scanned_at,
                MediaItem.album_artist_scanned_at,
                MediaItem.artists_scanned_at,
                MediaItem.musicbrainz_metadata_json.is_not(None).label("has_recording"),
                MediaItem.codec,
            ).where(MediaItem.library_root_id == root.id)
        )
    }
    # Sizes for rows from before file_size existed, written with the final
    # commit.
    size_backfill: list[dict] = []
    # Codec/bitrate/sample rate/bit depth for unchanged tracks from before
    # those columns existed: one header read each, written in small batches.
    quality_backfill: list[dict] = []

    def flush_quality_backfill() -> None:
        if quality_backfill:
            db.execute(update(MediaItem), quality_backfill)
            db.commit()
            quality_backfill.clear()

    root_path = root_path.resolve()
    files: list[Path] = []
    # Anything the walk could not read. Its tracks look missing, but that is
    # no evidence they were deleted, so removal is held (see below).
    unreadable_dirs: list[str] = []
    unreadable_files = 0

    def record_unreadable_dir(error: OSError) -> None:
        logger.warning("Scan of %s could not read %s: %s", root.path, error.filename, error)
        unreadable_dirs.append(os.path.relpath(error.filename or root_path, root_path))

    # os.walk rather than rglob: rglob silently skips a folder it cannot list.
    # Top-down order matches rglob's (a folder's files, then its subfolders).
    for dirpath, _dirnames, filenames in os.walk(root_path, onerror=record_unreadable_dir):
        # Walking a huge tree on a slow share can take minutes on its own.
        if should_stop is not None and should_stop():
            stopped = True
            break
        for filename in filenames:
            file_path = Path(dirpath, filename)
            if file_path.suffix.lower() not in settings.scan_extensions:
                continue
            try:
                if not file_path.is_file():
                    continue
                # Do not index symlinks that escape the configured library root.
                if not file_path.resolve(strict=True).is_relative_to(root_path):
                    continue
            except (OSError, RuntimeError) as exc:
                logger.warning("Scan of %s could not read %s: %s", root.path, file_path, exc)
                unreadable_files += 1
                continue
            files.append(file_path)

    # Rows whose path the walk did not find. A stopped walk is partial, so it
    # says nothing about what is gone (and the loop below does not run).
    walked = set() if stopped else {str(path.relative_to(root_path)) for path in files}
    moved_files = _MovedFiles(
        db, [] if stopped else [row for path, row in existing_by_path.items() if path not in walked]
    )
    total = len(files)
    if progress_callback is not None and not stopped:
        progress_callback(0, total)
    for processed, file_path in enumerate(files, start=1):
        if stopped or (should_stop is not None and should_stop()):
            stopped = True
            break
        try:
            suffix = file_path.suffix.lower()
            is_video = suffix in settings.video_extensions

            relative_path = str(file_path.relative_to(root_path))
            seen_relative_paths.add(relative_path)
            stat = file_path.stat()
            mtime = stat.st_mtime
            size = stat.st_size

            existing = existing_by_path.get(relative_path)
            # A path with no row may be a file that moved here (see _MovedFiles).
            moved = False
            if existing is None and moved_files:
                existing = moved_files.same_file(size, mtime, file_path.name)
                moved = existing is not None
            # Editors can rewrite tags in place keeping the mtime, so a changed
            # size counts too. Rows from before file_size existed (None) are not
            # treated as changed, or the first rescan would redo the whole library.
            unchanged = (
                existing is not None
                and mode != "full"
                and existing.file_mtime == mtime
                and (existing.file_size is None or existing.file_size == size)
                and existing.musicbrainz_isrc_scanned_at is not None
                and existing.album_artist_scanned_at is not None
                and (existing.kind != MediaKind.track or existing.artists_scanned_at is not None)
            )
            if unchanged and mode == "normal" and not moved:
                assert existing is not None  # `unchanged` requires an existing row
                if existing.file_size is None:
                    size_backfill.append({"id": existing.id, "file_size": size})
                if existing.kind == MediaKind.track and existing.codec is None:
                    quality_backfill.append({"id": existing.id, **read_audio_quality(file_path, size)})
                    if len(quality_backfill) >= SCAN_COMMIT_BATCH_SIZE:
                        flush_quality_backfill()
                if progress_callback is not None:
                    progress_callback(processed, total)
                continue
            # A metadata rescan of an untouched file refreshes tags only, and
            # so does a file that only moved: its measurements still stand.
            light = unchanged

            if is_video:
                tags = read_video_metadata(file_path)
            else:
                tags = read_audio_tags(file_path)
                tags["kind"] = MediaKind.track
            title = title_or_stem(tags.get("title"), file_path)
            # The lyrics are a second tag read on the library mount: done
            # here, before anything is flushed, so this file's write
            # transaction never spans that disk I/O.
            embedded_lyrics = None if is_video else get_embedded_lyrics(file_path)
            track_gain = tags.get("replaygain_track_gain")
            # A file without a track gain tag is left for the ffmpeg backfill.
            loudness_scanned_at = utcnow() if track_gain is not None else None

            if existing is None and moved_files:
                existing = moved_files.same_recording(tags["kind"], title, tags)
                moved = existing is not None

            was_known = existing is not None
            if existing is None:
                item = MediaItem(
                    library_root_id=root.id,
                    kind=tags["kind"],
                    relative_path=relative_path,
                    title=title,
                    artist=tags.get("artist"),
                    album_artist=tags.get("album_artist"),
                    album_artist_scanned_at=utcnow(),
                    artists_scanned_at=utcnow(),
                    album=tags.get("album"),
                    genre=tags.get("genre"),
                    track_number=tags.get("track_number"),
                    duration_seconds=tags.get("duration_seconds"),
                    replaygain_track_gain=track_gain,
                    replaygain_album_gain=tags.get("replaygain_album_gain"),
                    replaygain_track_peak=tags.get("replaygain_track_peak"),
                    loudness_scanned_at=loudness_scanned_at,
                    release_date=tags.get("release_date"),
                    musicbrainz_recording_mbid=tags.get("musicbrainz_recording_mbid"),
                    musicbrainz_artist_mbid=tags.get("musicbrainz_artist_mbid"),
                    musicbrainz_isrc=tags.get("musicbrainz_isrc"),
                    musicbrainz_isrc_scanned_at=utcnow(),
                    file_mtime=mtime,
                    file_size=size,
                    **{column: tags.get(column) for column in QUALITY_COLUMNS},
                )
                db.add(item)
                added += 1
            else:
                loaded = db.get(MediaItem, existing.id)
                if loaded is None:  # deleted since the scan started
                    raise LookupError(f"{relative_path} was removed during the scan")
                item = loaded
                if moved:
                    logger.info("Scan of %s: %s moved to %s", root.path, item.relative_path, relative_path)
                    item.relative_path = relative_path
                previous_title = item.title
                previous_artist = item.artist
                previous_album_artist = item.album_artist
                previous_isrc = item.musicbrainz_isrc
                item.kind = tags["kind"]
                item.title = title
                item.artist = tags.get("artist")
                item.album_artist = tags.get("album_artist")
                item.album_artist_scanned_at = utcnow()
                item.artists_scanned_at = utcnow()
                item.album = tags.get("album")
                item.genre = tags.get("genre")
                item.track_number = tags.get("track_number")
                item.duration_seconds = tags.get("duration_seconds")
                if light:
                    # The file is the same, so an earlier ffmpeg measurement
                    # stands unless the tags now carry a value.
                    if track_gain is not None:
                        item.replaygain_track_gain = track_gain
                        item.loudness_scanned_at = loudness_scanned_at
                    if tags.get("replaygain_album_gain") is not None:
                        item.replaygain_album_gain = tags["replaygain_album_gain"]
                    if tags.get("replaygain_track_peak") is not None:
                        item.replaygain_track_peak = tags["replaygain_track_peak"]
                else:
                    # The file changed, so any earlier measurement is stale too.
                    item.replaygain_track_gain = track_gain
                    item.replaygain_album_gain = tags.get("replaygain_album_gain")
                    item.replaygain_track_peak = tags.get("replaygain_track_peak")
                    item.loudness_scanned_at = loudness_scanned_at
                item.release_date = tags.get("release_date")
                if tags.get("musicbrainz_recording_mbid"):
                    item.musicbrainz_recording_mbid = tags["musicbrainz_recording_mbid"]
                if tags.get("musicbrainz_artist_mbid"):
                    item.musicbrainz_artist_mbid = tags["musicbrainz_artist_mbid"]
                isrc = tags.get("musicbrainz_isrc")
                if not existing.has_recording and (
                    isrc != previous_isrc
                    or item.title != previous_title
                    or item.artist != previous_artist
                    or item.album_artist != previous_album_artist
                ):
                    item.musicbrainz_lookup_status = None
                    item.musicbrainz_last_error = None
                item.musicbrainz_isrc = isrc
                item.musicbrainz_isrc_scanned_at = utcnow()
                item.file_mtime = mtime
                item.file_size = size
                if not is_video:
                    for column in QUALITY_COLUMNS:
                        setattr(item, column, tags.get(column))
                updated += 1

            if item.id is None:
                db.flush()  # assigns id for a new row, needed to name the thumbnail/art file

            if not is_video:
                replace_item_genres(db, item, tags.get("genres", []), "embedded")
                item.artist_tags_json = artist_tags_payload(
                    tags.get("artists_multi"), tags.get("album_artists_multi")
                )
                sync_track_credits(
                    db, item, credits_from_columns(item, split_rules), artist_cache
                )
                assign_album(db, item, album_cache)
                if embedded_lyrics:
                    item.lyrics_plain, item.lyrics_synced = normalize_lyrics(embedded_lyrics, None)

            item_id = item.id
            db.commit()
            if light:
                if progress_callback is not None:
                    progress_callback(processed, total)
                continue
            # A changed file can carry different embedded artwork and always
            # invalidates transcodes/remuxes derived from its old bytes. The
            # cache folders are listed once per scan, not once per file.
            if cache_listing is None:
                cache_listing = listed_media_cache_files()
            delete_media_cache(item_id, cache_listing)
            if was_known:
                art_redone += 1

            if is_video:
                _maybe_generate_thumbnail(file_path, item)
            else:
                # Generates the resized grid-tile copy now too (not just the
                # original), same as video thumbnails - so a page full of art
                # is never waiting on first-time ffmpeg work, just serving an
                # already-cached file. See app.art.THUMB_MAX_DIMENSION.
                ensure_art_thumb_cached(item, file_path)

            if progress_callback is not None:
                progress_callback(processed, total)
        except Exception:  # one unreadable file must not end the scan
            logger.exception("Skipping %s: it could not be scanned", file_path)
            db.rollback()
            # Artists and albums this file created were rolled back with it,
            # but the caches still hold them; handing them to a later file
            # by the same artist would link it to rows that no longer exist
            # (a foreign-key failure, so that file would be skipped too).
            artist_cache.clear()
            album_cache.clear()
            failed += 1
            if progress_callback is not None:
                progress_callback(processed, total)

    if stopped:
        # A partial walk is no evidence that anything was deleted: everything
        # not reached stays as it is, and nothing is reported as missing.
        logger.info("Scan of %s stopped after %d of %d file(s)", root.path, len(seen_relative_paths), total)
        missing = []
        hold_reason = None
    else:
        missing = [
            item for relative_path, item in existing_by_path.items()
            if relative_path not in seen_relative_paths
            and relative_path not in moved_files.claimed_paths
        ]
        hold_reason = _removal_hold_reason(
            len(missing), len(existing_by_path), len(files),
            len(unreadable_dirs) + unreadable_files, confirm_removals,
        )
    missing_held = 0
    if hold_reason is not None:
        # Removal also deletes every user's favourites, playlist entries and
        # history for these tracks, and a later rescan cannot bring them back.
        missing_held = len(missing)
        logger.warning(
            "Scan of %s: not removing %d missing track(s) of %d: %s",
            root.path, missing_held, len(existing_by_path), hold_reason,
        )
        missing = []
    # One delete_media_records call (a dozen statements) per batch rather
    # than per missing row.
    for start in range(0, len(missing), SCAN_COMMIT_BATCH_SIZE):
        batch = missing[start : start + SCAN_COMMIT_BATCH_SIZE]
        delete_media_records(db, [item.id for item in batch])
        removed += len(batch)
        db.commit()

    flush_quality_backfill()
    for start in range(0, len(size_backfill), SCAN_COMMIT_BATCH_SIZE):
        db.execute(update(MediaItem), size_backfill[start : start + SCAN_COMMIT_BATCH_SIZE])
    db.commit()
    if removed or added or updated:
        prune_orphan_artists(db)
        prune_orphan_albums(db)
    if failed:
        logger.warning("Scan of %s skipped %d file(s) that could not be read", root.path, failed)
    return {
        "added": added,
        "updated": updated,
        "removed": removed,
        "failed": failed,
        "art_redone": art_redone,
        "missing_held": missing_held,
        "unreadable_dirs": unreadable_dirs[:UNREADABLE_DIRS_REPORTED],
        "stopped": stopped,
    }


class _MovedFiles:
    """Rows of this root whose file is gone from its path, to be matched with
    files that turned up at a path with no row: a renamed or moved file.

    A track's row is what every user's favourites, playlist entries, play
    history and dislikes hang on. Treating a renamed album folder as "twelve
    files removed, twelve added" would delete all of that, so a new path first
    looks for the row it came from and takes it over. Each row is claimed at
    most once, and only on a clear match; anything else stays a new file."""

    def __init__(self, db: Session, rows: list) -> None:
        self._db = db
        self._free = {row.id: row for row in rows}
        self._by_file: dict[tuple[int, float], list] = {}
        self._by_size_name: dict[tuple[int, str], list] = {}
        for row in rows:
            if row.file_size is None:
                continue
            self._by_file.setdefault((row.file_size, row.file_mtime), []).append(row)
            name = Path(row.relative_path).name
            self._by_size_name.setdefault((row.file_size, name), []).append(row)
        self._by_mbid: dict[str, list] | None = None
        self._by_tags: dict[tuple, list] = {}
        self.claimed_paths: set[str] = set()

    def __bool__(self) -> bool:
        return bool(self._free)

    def _take(self, rows: list, name: str | None = None):
        free = [row for row in rows if row.id in self._free]
        if name is not None and len(free) > 1:
            free = [row for row in free if Path(row.relative_path).name == name]
        if len(free) != 1:
            return None
        row = free[0]
        del self._free[row.id]
        self.claimed_paths.add(row.relative_path)
        return row

    def same_file(self, size: int, mtime: float, name: str):
        """The row of the same bytes: same size and modification time (a
        rename or a move keeps both), or same size and file name (a copy to
        another disk does not keep the time)."""
        return self._take(self._by_file.get((size, mtime), []), name) or self._take(
            self._by_size_name.get((size, name), [])
        )

    @staticmethod
    def _tag_key(kind, title, artist, album, track_number) -> tuple:
        def fold(value: str | None) -> str:
            return (value or "").strip().casefold()

        return (kind, fold(title), fold(artist), fold(album), track_number)

    def _load_tags(self) -> None:
        self._by_mbid = {}
        ids = list(self._free)
        for start in range(0, len(ids), 500):
            for row in self._db.execute(
                select(
                    MediaItem.id,
                    MediaItem.kind,
                    MediaItem.title,
                    MediaItem.artist,
                    MediaItem.album,
                    MediaItem.track_number,
                    MediaItem.duration_seconds,
                    MediaItem.musicbrainz_recording_mbid,
                ).where(MediaItem.id.in_(ids[start : start + 500]))
            ):
                free = self._free[row.id]
                if row.musicbrainz_recording_mbid:
                    self._by_mbid.setdefault(row.musicbrainz_recording_mbid, []).append(free)
                key = self._tag_key(row.kind, row.title, row.artist, row.album, row.track_number)
                self._by_tags.setdefault(key, []).append((free, row.duration_seconds))

    def same_recording(self, kind, title: str, tags: dict):
        """The row of a file that was renamed and rewritten at once (a tagger
        that renames as it saves): the same MusicBrainz recording, or the
        same title, artist, album and track number at the same length."""
        if self._by_mbid is None:
            self._load_tags()
        assert self._by_mbid is not None
        mbid = tags.get("musicbrainz_recording_mbid")
        if mbid and (row := self._take(self._by_mbid.get(mbid, []))) is not None:
            return row
        if not title or not (tags.get("artist") or tags.get("album")):
            return None  # a bare file name is no evidence of anything
        duration = tags.get("duration_seconds")
        key = self._tag_key(kind, title, tags.get("artist"), tags.get("album"), tags.get("track_number"))
        close = [
            row
            for row, length in self._by_tags.get(key, [])
            if duration is None or length is None or abs(length - duration) <= 2
        ]
        return self._take(close)


def _removal_hold_reason(
    missing: int, existing: int, found: int, unreadable: int, confirmed: bool
) -> str | None:
    """Why the missing rows must not be removed yet, or None to remove them.

    An unmounted share is an existing, empty folder, and an unreadable folder
    lists as empty, so "the files are not there" alone is weak evidence. A
    confirmed scan overrides the size checks but never an unreadable folder."""
    if not missing:
        return None
    if unreadable:
        return f"{unreadable} folder(s) or file(s) could not be read"
    if confirmed:
        return None
    if found == 0:
        return "no media files were found (is the folder mounted?)"
    if missing > MISSING_HOLD_MIN_ROWS and missing * 100 > existing * MISSING_HOLD_PERCENT:
        return f"that is more than {MISSING_HOLD_PERCENT}% of this library"
    return None
