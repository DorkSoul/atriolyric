import base64
import os
import socket
import subprocess
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4
from sqlalchemy import or_, select, update

from app.access import media_file_path
from app.config import art_thumb_path, find_art_path, settings
from app.config import art_thumb_path as _thumb_path
from app.db import SessionLocal
from app.image_sniff import decodable_size, sniff_image, sniff_mime, too_large
from app.models import MediaItem, MediaKind
from app.video_meta import FFMPEG_BINARY, INPUT_PROTOCOLS

# Grid tiles (Artists/Albums/Playlists/track rows) only ever display art at a
# few hundred pixels at most - serving the original embedded picture (often
# 1-3MB straight out of a lossless rip) to every tile on a page would make
# pages with hundreds of tiles drag multiple megabytes over what's often a
# slow link (a remote/tunneled connection especially). Capping the long edge
# at this size cuts that dramatically while still looking sharp at tile size.
THUMB_MAX_DIMENSION = 500
MAX_ART_BYTES = 10 * 1024 * 1024
HEADER_BYTES = 1024 * 1024
# Cover Art Archive redirects to archive.org storage; Wikimedia Commons
# (artist photos) serves files and thumbnails from these two hosts only.
ART_HOSTS = ("coverartarchive.org", "archive.org", "upload.wikimedia.org", "thumb.wikimedia.org")
_art_locks: weakref.WeakValueDictionary[int, threading.RLock] = weakref.WeakValueDictionary()
_art_locks_guard = threading.Lock()
_resize_slots = threading.BoundedSemaphore(2)
# A remote art download that failed (timeout, 5xx) is not tried again for an
# item for this long, so tile requests and the startup backfill do not retry
# it on every view and every restart. Bounded; oldest forgotten first.
REMOTE_RETRY_SECONDS = 3600
MAX_REMOTE_FAILURES = 4096
_remote_failures: OrderedDict[int, float] = OrderedDict()
_remote_failures_lock = threading.Lock()


def _remote_backed_off(media_item_id: int) -> bool:
    with _remote_failures_lock:
        until = _remote_failures.get(media_item_id)
        if until is None:
            return False
        if until < time.monotonic():
            del _remote_failures[media_item_id]
            return False
        return True


def _remote_failed(media_item_id: int) -> None:
    with _remote_failures_lock:
        _remote_failures[media_item_id] = time.monotonic() + REMOTE_RETRY_SECONDS
        _remote_failures.move_to_end(media_item_id)
        while len(_remote_failures) > MAX_REMOTE_FAILURES:
            _remote_failures.popitem(last=False)


@dataclass(frozen=True)
class ArtBytes:
    """Artwork held in memory rather than stored on disk - a track's
    embedded picture, read straight out of its audio file."""

    data: bytes
    mime: str


def _art_lock(media_item_id: int) -> threading.RLock:
    with _art_locks_guard:
        return _art_locks.setdefault(media_item_id, threading.RLock())


def _validate_remote_art_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("Artwork URL must be an HTTPS URL without credentials")
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(
                parts.hostname, parts.port or 443, type=socket.SOCK_STREAM
            )
        }
    except OSError as exc:
        raise ValueError("Artwork hostname could not be resolved") from exc
    if any(not ip_address(address).is_global for address in addresses):
        raise ValueError("Artwork URL resolves to a private or reserved address")
    # Artwork URLs are provider-derived. Keep the downloader restricted to
    # the provider currently used by the application even if a malformed or
    # manually modified database row contains another hostname.
    hostname = parts.hostname.lower().rstrip(".")
    if not any(hostname == h or hostname.endswith(f".{h}") for h in ART_HOSTS):
        raise ValueError("Artwork hostname is not an allowed provider")


def _checked(data: bytes) -> tuple[bytes, str] | None:
    """(data, sniffed MIME) if `data` is an acceptably small real image."""
    mime = sniff_mime(data) if len(data) <= MAX_ART_BYTES else None
    return (data, mime) if mime and not too_large(data) else None


def extract_embedded_art(path: Path) -> tuple[bytes, str] | None:
    """Pull the embedded cover image out of a tagged audio file. Each tag
    format carries it differently - FLAC's own picture blocks, MP4 'covr'
    atoms, ID3 APIC frames (also what WAVE uses), and Vorbis comments'
    base64-encoded METADATA_BLOCK_PICTURE. Returns (bytes, mime) for the
    first image found, or None if the file has no embedded picture. The
    tag's own MIME string is ignored (it can claim text/html): the type is
    sniffed from the bytes, and anything that is not a real image counts as
    no picture."""
    try:
        audio = mutagen.File(path)
    except Exception:  # noqa: BLE001
        return None
    if audio is None:
        return None

    if isinstance(audio, FLAC):
        if audio.pictures:
            pic = audio.pictures[0]
            return _checked(pic.data)
        return None

    if isinstance(audio, MP4):
        covr = audio.tags.get("covr") if audio.tags else None
        if covr:
            cover = covr[0]
            return _checked(bytes(cover))
        return None

    tags = audio.tags
    if tags is None:
        return None

    apic_frames = tags.getall("APIC") if hasattr(tags, "getall") else []
    if apic_frames:
        frame = apic_frames[0]
        return _checked(frame.data)

    block = tags.get("metadata_block_picture")
    if block:
        try:
            pic = Picture(base64.b64decode(block[0]))
            return _checked(pic.data)
        except Exception:  # noqa: BLE001
            return None

    return None


# Cover images a folder may hold beside its tracks, best first, as media
# players look for them ("artist.jpg" is an artist's photo, not a cover).
FOLDER_COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "albumartlarge", "albumartsmall")
FOLDER_COVER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")


# {folder: (its mtime, {lower-case cover file name: real name})} - a scan
# asks once per track without a picture, so each folder is listed once until
# it changes, not once per track. Bounded: the oldest half goes when full.
_FOLDER_LISTINGS_MAX = 512
_folder_listings: dict[str, tuple[int, dict[str, str]]] = {}
_folder_listings_lock = threading.Lock()


def _folder_cover_names(folder: Path) -> dict[str, str]:
    try:
        mtime = os.stat(folder).st_mtime_ns
    except OSError:
        return {}
    key = str(folder)
    with _folder_listings_lock:
        known = _folder_listings.get(key)
    if known is not None and known[0] == mtime:
        return known[1]
    wanted = {stem + suffix for stem in FOLDER_COVER_NAMES for suffix in FOLDER_COVER_SUFFIXES}
    try:
        with os.scandir(folder) as entries:
            names = {entry.name.lower(): entry.name for entry in entries if entry.name.lower() in wanted}
    except OSError:
        return {}
    with _folder_listings_lock:
        if len(_folder_listings) >= _FOLDER_LISTINGS_MAX:
            for old in list(_folder_listings)[: _FOLDER_LISTINGS_MAX // 2]:
                del _folder_listings[old]
        _folder_listings[key] = (mtime, names)
    return names


def folder_cover(file_path: Path) -> tuple[bytes, str] | None:
    """A cover image in the track's own folder (cover.jpg, folder.jpg ...),
    for a track whose file has no picture. Only a regular file right in that
    folder - never a symlink, which could point anywhere on the server - of
    an acceptable size whose bytes really are an image."""
    folder = file_path.parent
    names = _folder_cover_names(folder)
    for stem in FOLDER_COVER_NAMES:
        for suffix in FOLDER_COVER_SUFFIXES:
            name = names.get(stem + suffix)
            if name is None:
                continue
            entry = folder / name
            try:
                if entry.is_symlink() or not entry.is_file() or entry.stat().st_size > MAX_ART_BYTES:
                    continue
                found = _checked(entry.read_bytes())
            except OSError:
                continue
            if found is not None:
                return found
    return None


def cache_art_bytes(media_item_id: int, data: bytes) -> Path:
    # Only the sniffed type decides the stored extension (and so how the file
    # is served later); a caller-supplied MIME is never trusted.
    ext = sniff_image(data) if len(data) <= MAX_ART_BYTES else None
    if ext is None:
        raise ValueError("Invalid or oversized artwork")
    settings.art_dir.mkdir(parents=True, exist_ok=True)
    path = settings.art_dir / f"{media_item_id}{ext}"
    tmp = settings.art_dir / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _download_remote_art(initial_url: str) -> tuple[bytes, str] | None:
    """Download artwork with redirect revalidation and a hard response cap."""
    url = initial_url
    with httpx.Client(
        headers={"User-Agent": settings.user_agent},
        timeout=10,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for _redirect in range(4):
            _validate_remote_art_url(url)
            with client.stream("GET", url) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location")
                    if not location:
                        return None
                    url = urljoin(url, location)
                    continue
                resp.raise_for_status()
                try:
                    declared_size = int(resp.headers.get("content-length", "0"))
                except ValueError:
                    declared_size = 0
                if declared_size > MAX_ART_BYTES:
                    return None
                data = bytearray()
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_ART_BYTES:
                        return None
                # The server's Content-Type is ignored, like a tag's MIME.
                return _checked(bytes(data))
    return None


def load_full_art(item: MediaItem, file_path: Path | None) -> Path | ArtBytes | None:
    """The original, full-size art for this item, cheapest source first: a
    stored copy, the track's own embedded picture, a cover image in its
    folder (cover.jpg, folder.jpg ...), then a previously-matched Cover Art
    Archive URL. None if none of those have anything.

    Only downloaded art is stored (under settings.art_dir): it can't be
    recovered from the library. Embedded art is read from the audio file
    each time - tags sit near the start of the file, and storing a second
    copy of every embedded picture would cost far more disk than it saves
    (only Now Playing shows art full size). Stored copies of embedded art
    are still served until they are cleaned up."""
    with _art_lock(item.id):
        stored = find_art_path(item.id)
        if stored is not None:
            return stored

        if file_path is not None:
            found = extract_embedded_art(file_path) or folder_cover(file_path)
            if found is not None:
                return ArtBytes(*found)

        if item.cover_art_url and not _remote_backed_off(item.id):
            try:
                downloaded = _download_remote_art(item.cover_art_url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (404, 410):
                    _remote_failed(item.id)
                return None
            except ValueError:  # a URL the downloader refuses: final
                return None
            except Exception:  # noqa: BLE001 - transport failure: back off
                _remote_failed(item.id)
                return None
            if downloaded is None:
                return None
            try:
                return cache_art_bytes(item.id, downloaded[0])
            except Exception:  # noqa: BLE001
                return None

        return None


def _read_head(path: Path) -> bytes:
    """Enough of an image file to read its dimensions (a JPEG can carry
    large metadata blocks before its frame header)."""
    with path.open("rb") as handle:
        return handle.read(HEADER_BYTES)


def _resize_art(src: Path | ArtBytes, out_path: Path) -> bool:
    """Downscale to THUMB_MAX_DIMENSION via ffmpeg (already a dependency for
    video thumbnails, so this needs no new library). In-memory art is piped
    in on stdin. Never raises - a missing ffmpeg or an unreadable image just
    means no thumbnail."""
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.parent / f".{out_path.stem}.{uuid.uuid4().hex}.jpg"
        source, stdin = (
            ("pipe:0", src.data) if isinstance(src, ArtBytes) else (str(src), None)
        )
        # Read the size from the header first: ffmpeg would allocate for
        # whatever the header claims before it notices the file is tiny.
        if not decodable_size(src.data if isinstance(src, ArtBytes) else _read_head(src)):
            return False
        with _resize_slots:
            subprocess.run(  # noqa: S603 - fixed argv list, no shell; paths come from the library, never a command
                [
                    FFMPEG_BINARY,
                    "-y",
                    *INPUT_PROTOCOLS,
                    "-i",
                    source,
                    "-vf",
                    f"scale='min({THUMB_MAX_DIMENSION},iw)':'min({THUMB_MAX_DIMENSION},ih)':force_original_aspect_ratio=decrease",
                    # One frame: an animated GIF has many, and writing them
                    # all to a single .jpg fails ("Not a decodable image").
                    "-frames:v",
                    "1",
                    "-q:v",
                    "4",
                    str(tmp_path),
                ],
                input=stdin,
                capture_output=True,
                timeout=30,
                check=True,
            )
        if not tmp_path.is_file():
            return False
        tmp_path.replace(out_path)
        return True
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        if "tmp_path" in locals():
            tmp_path.unlink(missing_ok=True)
        return False


def ensure_art_thumb_cached(
    item: MediaItem, file_path: Path | None
) -> Path | ArtBytes | None:
    """The resized copy of this item's art for grid/tile display - see
    THUMB_MAX_DIMENSION. Thumbnails are kept for as long as the item exists
    (see app.cleanup), so this is normally just a file check that never
    touches the library. Made from the full-size art when missing; falls
    back to that full-size art (still far better than nothing) if resizing
    isn't possible for any reason."""
    with _art_lock(item.id):
        thumb = art_thumb_path(item.id)
        if thumb.is_file():
            return thumb
        full = load_full_art(item, file_path)
        if full is None:
            return None
        return thumb if _resize_art(full, thumb) else full


def backfill_art_thumbs(progress_cb: Callable[[int], None] | None = None) -> int:
    """Generate the resized grid-tile copy for every track that has (or can
    get) art but no thumbnail yet.

    The scanner generates each item's thumbnail at scan time and nothing
    evicts them, so a healthy library never needs this - but a track whose
    thumbnail is missing otherwise only gets one when a tile first requests
    it. That request-time path is the slow
    one (tag extraction + an ffmpeg resize under load), so this runs
    opportunistically at startup: one shared
    DB session, batching ids; the per-item lock in ensure_art_thumb_cached
    still serializes against any concurrent request for the same item.
    Audio items only - video has its own frame-thumbnail pipeline.

    A track found to have no art is marked with the file version probed
    (art_checked_mtime) and skipped until the file changes, so a restart does
    not re-read the tags of every track without art. Not when its remote
    download failed for now - that one is tried again later.
    """
    done = 0
    db = SessionLocal()
    try:
        # Only items that can actually have art, whose thumb file doesn't
        # exist yet. Candidate ids are fetched one small page at a time so a
        # big library costs no more memory than a small one; each page is
        # committed before moving on, keeping the writer-lock window short.
        base_query = (
            select(MediaItem.id)
            .where(
                MediaItem.kind == MediaKind.track,
                MediaItem.library_root_id.isnot(None),
                or_(
                    MediaItem.art_checked_mtime.is_(None),
                    MediaItem.art_checked_mtime != MediaItem.file_mtime,
                ),
            )
            .order_by(MediaItem.id)
        )
        last_id = 0
        while True:
            page = list(db.scalars(base_query.where(MediaItem.id > last_id).limit(200)))
            if not page:
                break
            last_id = page[-1]
            for item_id in page:
                if _thumb_path(item_id).is_file():
                    continue
                item = db.get(MediaItem, item_id)
                if item is None:
                    continue
                # media_file_path resolves + containment-checks against the
                # item's root (None if the file has vanished - ensure_*
                # then simply finds nothing to extract).
                file_path = media_file_path(item, require_file=True)
                if ensure_art_thumb_cached(item, file_path) is not None:
                    done += 1
                elif file_path is not None and not _remote_backed_off(item_id):
                    # By id: a row deleted meanwhile is simply not updated.
                    db.execute(
                        update(MediaItem)
                        .where(MediaItem.id == item_id)
                        .values(art_checked_mtime=item.file_mtime)
                    )
            db.commit()
            if progress_cb is not None:
                progress_cb(done)
    finally:
        db.close()
    return done
