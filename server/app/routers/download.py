"""Downloads of original files, singly or as a streamed ZIP.

  GET /api/download/media/{id}                 the original file (songs and videos)
  GET /api/download/app/android                the Android app's APK, if the server carries one
  GET /api/download/video/{id}/renditions      lower-quality MP4s on offer (below the source height)
  POST /api/download/video/{id}/renditions/{q} start preparing one (q = 1080 | 720 | 480)
  GET /api/download/video/{id}/renditions/{q}  its status: none|queued|preparing|ready|failed
  GET /api/download/video/{id}/renditions/{q}/file  the MP4 (Range; 409 until ready)
  GET /api/download/album/{key}                ZIP of an album
  GET /api/download/artist/{key}               ZIP of an artist's tracks
  GET /api/download/playlist/{id}              ZIP of a playlist (owner only)
  GET /api/download/{kind}/{key}/manifest      JSON [{id, path, size, url}] for
                                               kind = media | album | artist | playlist
  GET /api/download/{kind}/{key}/ids           JSON [id, ...]: what it holds now

album/artist keys are the `art_key` values from the album/artist lists. Zip
entries are laid out as Atriolyric/<Artist>/<Album>/<NN Title>.<ext>, videos
under Atriolyric/Videos/<folder>/. Archives are stored (no compression) and
streamed from disk, never built in memory.
"""

import re
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import renditions
from app.access import accessible_root_ids, can_access_root, media_file_path
from app.android_app import APK_MEDIA_TYPE, android_app_info
from app.config import settings
from app.custom_art import collection_filters, decode_key, validate_entity
from app.deps import CurrentUser, DbSession
from app.models import MediaItem, MediaKind, Playlist, User
from app.playlist_access import visible_playlist_items
from app.schemas import (
    DownloadEntryOut,
    RenditionListOut,
    RenditionOut,
)
from app.version import read_web_version

router = APIRouter(prefix="/api/download", tags=["download"])

ROOT_FOLDER = "Atriolyric"
CHUNK_SIZE = 1024 * 1024
_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')
_MAX_COMPONENT = 100
# The DOS dates a ZIP entry can hold; zipfile raises outside them, which
# (after the 200 is sent) would cut the archive short.
_ZIP_EARLIEST = (1980, 1, 1, 0, 0, 0)
_ZIP_LATEST = (2107, 12, 31, 23, 59, 58)


def zip_date(mtime: float) -> tuple[int, int, int, int, int, int]:
    """The file's local modification time, clamped to what a ZIP can hold."""
    try:
        local = time.localtime(mtime)
    except (OverflowError, OSError, ValueError):
        return _ZIP_EARLIEST
    stamp = (local.tm_year, local.tm_mon, local.tm_mday, local.tm_hour, local.tm_min, local.tm_sec)
    return max(_ZIP_EARLIEST, min(_ZIP_LATEST, stamp))


def safe_component(value: str | None, fallback: str) -> str:
    """One path segment: no separators, control characters, or dot-only names."""
    cleaned = _UNSAFE.sub("_", value or "").strip().strip(".").strip()
    cleaned = cleaned[:_MAX_COMPONENT].strip()
    return cleaned or fallback


@dataclass(frozen=True)
class Entry:
    item_id: int
    path: str
    file: Path
    size: int


def _relative_path(item: MediaItem) -> str:
    ext = Path(item.relative_path).suffix.lower()
    title = safe_component(item.title, "Untitled")
    if item.kind == MediaKind.track:
        artist = safe_component(item.album_artist or item.artist, "Unknown Artist")
        album = safe_component(item.album, "Unknown Album")
        number = f"{item.track_number:02d} " if item.track_number else ""
        return f"{ROOT_FOLDER}/{artist}/{album}/{number}{title}{ext}"
    folder = safe_component(item.video_folder, "Unsorted")
    return f"{ROOT_FOLDER}/Videos/{folder}/{title}{ext}"


def _build_entries(items: list[MediaItem]) -> list[Entry]:
    """Existing, contained files with unique, sanitised archive paths."""
    used: set[str] = set()
    entries: list[Entry] = []
    for item in items:
        file = media_file_path(item)
        if file is None:
            continue
        path = _relative_path(item)
        stem, dot, ext = path.rpartition(".")
        candidate, counter = path, 1
        while candidate.lower() in used:
            counter += 1
            candidate = f"{stem} ({counter}){dot}{ext}"
        used.add(candidate.lower())
        entries.append(Entry(item.id, candidate, file, file.stat().st_size))
    return entries


def _collection(
    db: Session, user: User, kind: str, key: str
) -> tuple[str, list[MediaItem]]:
    """(display name, items) for a downloadable collection; 404s when it
    doesn't exist or the user may not see it."""
    if kind == "media":
        if not key.isdecimal():
            raise HTTPException(404, "Media item not found")
        item = db.get(MediaItem, int(key))
        if item is None or not can_access_root(db, user, item.library_root_id):
            raise HTTPException(404, "Media item not found")
        return item.title, [item]
    if kind == "playlist":
        if not key.isdecimal():
            raise HTTPException(404, "Playlist not found")
        playlist = db.get(Playlist, int(key))
        if playlist is None or playlist.user_id != user.id:
            raise HTTPException(404, "Playlist not found")
        return playlist.name, [i.media_item for i in visible_playlist_items(db, playlist, user)]
    if kind not in ("album", "artist"):
        raise HTTPException(404, "Unknown download type")
    entity_key = decode_key(key)
    validate_entity(db, user, kind, entity_key)
    items = (
        db.execute(
            select(MediaItem)
            .where(
                MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
                MediaItem.kind == MediaKind.track,
                *collection_filters(kind, entity_key),
            )
            .order_by(MediaItem.artist, MediaItem.album, MediaItem.track_number, MediaItem.id)
        )
        .scalars()
        .all()
    )
    name = entity_key if kind == "artist" else entity_key.partition("\x1f")[2]
    return name, list(items)


def _attachment(filename: str) -> dict[str, str]:
    fallback = filename.encode("ascii", "replace").decode().replace("?", "_")
    return {
        "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
    }


class _ZipSink:
    """Write-only, non-seekable target that hands bytes to the response as
    zipfile produces them."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.position = 0

    def write(self, data: bytes) -> int:
        self.chunks.append(bytes(data))
        self.position += len(data)
        return len(data)

    def tell(self) -> int:
        return self.position

    def flush(self) -> None:
        pass

    def close(self) -> None:
        # Part of the writable-file protocol zipfile is typed against; it never
        # closes a file object it was handed, so there is nothing to release.
        pass

    def drain(self) -> Iterator[bytes]:
        chunks, self.chunks = self.chunks, []
        yield from chunks


def _stream_zip(entries: list[Entry]) -> Iterator[bytes]:
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as archive:
        for entry in entries:
            try:
                handle = entry.file.open("rb")
            except OSError:
                continue  # vanished since listing; skip rather than abort
            with handle:
                info = zipfile.ZipInfo(entry.path, zip_date(entry.file.stat().st_mtime))
                info.compress_type = zipfile.ZIP_STORED
                with archive.open(info, "w", force_zip64=True) as target:
                    while chunk := handle.read(CHUNK_SIZE):
                        target.write(chunk)
                        yield from sink.drain()
            yield from sink.drain()
    yield from sink.drain()


@router.get("/app/android")
def download_android_app(user: CurrentUser) -> FileResponse:
    """The Android app's APK, when this server carries one (signed in only,
    like every other download)."""
    info = android_app_info(settings.android_apk_path, read_web_version(settings.web_dist_path))
    if info is None or settings.android_apk_path is None:
        raise HTTPException(404, "This server does not offer the Android app")
    return FileResponse(
        settings.android_apk_path,
        media_type=APK_MEDIA_TYPE,
        headers=_attachment(f"atriolyric-{info['version']}.apk"),
    )


@router.get("/media/{media_item_id}")
def download_media(
    media_item_id: int,
    db: DbSession,
    user: CurrentUser,
) -> FileResponse:
    item = db.get(MediaItem, media_item_id)
    if item is None or not can_access_root(db, user, item.library_root_id):
        raise HTTPException(404, "Media item not found")
    file = media_file_path(item)
    if file is None:
        raise HTTPException(404, "File not found")
    ext = file.suffix.lower()
    artist = safe_component(item.artist, "")
    title = safe_component(item.title, "Untitled")
    filename = f"{artist} - {title}{ext}" if artist else f"{title}{ext}"
    # A big video can take a long time to send: give the connection back
    # first.
    db.close()
    return FileResponse(file, headers=_attachment(filename))


def _video_source(db: Session, user: User, media_item_id: int) -> tuple[MediaItem, Path]:
    item = db.get(MediaItem, media_item_id)
    if item is None or not can_access_root(db, user, item.library_root_id):
        raise HTTPException(404, "Media item not found")
    if item.kind == MediaKind.track:
        raise HTTPException(404, "Not a video")
    file = media_file_path(item)
    if file is None:
        raise HTTPException(404, "File not found")
    return item, file


def _rendition_level(file: Path, quality: str) -> int:
    level = renditions.parse_quality(quality)
    if level is None or level not in renditions.offered_levels(renditions.source_height(file)):
        raise HTTPException(404, "Rendition not available")
    return level


def _rendition_out(item_id: int, level: int, state: dict) -> dict:
    return {
        "quality": str(level),
        **state,
        "url": f"/api/download/video/{item_id}/renditions/{level}/file",
    }


@router.get("/video/{media_item_id}/renditions", response_model=RenditionListOut)
def list_renditions(media_item_id: int, db: DbSession, user: CurrentUser) -> dict:
    item, file = _video_source(db, user, media_item_id)
    db.close()
    height = renditions.source_height(file)
    return {
        "source_height": height,
        "source_size": file.stat().st_size,
        "renditions": [
            _rendition_out(item.id, level, renditions.status(item.id, level, file))
            for level in renditions.offered_levels(height)
        ],
    }


@router.post("/video/{media_item_id}/renditions/{quality}", response_model=RenditionOut)
def start_rendition(media_item_id: int, quality: str, db: DbSession, user: CurrentUser) -> dict:
    item, file = _video_source(db, user, media_item_id)
    db.close()
    level = _rendition_level(file, quality)
    return _rendition_out(item.id, level, renditions.request(item, level, file))


@router.get("/video/{media_item_id}/renditions/{quality}", response_model=RenditionOut)
def rendition_status(media_item_id: int, quality: str, db: DbSession, user: CurrentUser) -> dict:
    item, file = _video_source(db, user, media_item_id)
    db.close()
    level = _rendition_level(file, quality)
    return _rendition_out(item.id, level, renditions.status(item.id, level, file))


@router.get("/video/{media_item_id}/renditions/{quality}/file")
def rendition_file(
    media_item_id: int, quality: str, db: DbSession, user: CurrentUser
) -> FileResponse:
    item, file = _video_source(db, user, media_item_id)
    artist = safe_component(item.artist, "")
    title = safe_component(item.title, "Untitled")
    db.close()
    level = _rendition_level(file, quality)
    path = renditions.rendition_path(item.id, level)
    if renditions.status(item.id, level, file)["status"] != "ready":
        raise HTTPException(409, "Rendition is not ready")
    renditions.touch(path)
    base = f"{artist} - {title}" if artist else title
    return FileResponse(path, media_type="video/mp4", headers=_attachment(f"{base} ({level}p).mp4"))


@router.get("/{kind}/{key}/ids", response_model=list[int])
def download_ids(kind: str, key: str, db: DbSession, user: CurrentUser) -> list[int]:
    """The ids of what an album, artist or playlist holds now, each once, in
    order. The Android app keeps a downloaded one in step with this: it
    fetches what was added and deletes what left. Read from the library
    alone, not from the disk: a folder that is briefly unreachable must not
    look like songs that were removed."""
    _name, items = _collection(db, user, kind, key)
    return list(dict.fromkeys(item.id for item in items))


@router.get("/{kind}/{key}/manifest", response_model=list[DownloadEntryOut])
def download_manifest(
    kind: str,
    key: str,
    db: DbSession,
    user: CurrentUser,
) -> list[dict]:
    _name, items = _collection(db, user, kind, key)
    return [
        {
            "id": entry.item_id,
            "path": entry.path,
            "size": entry.size,
            "url": f"/api/download/media/{entry.item_id}",
        }
        for entry in _build_entries(items)
    ]


@router.get("/{kind}/{key}")
def download_zip(
    kind: str,
    key: str,
    db: DbSession,
    user: CurrentUser,
) -> StreamingResponse:
    name, items = _collection(db, user, kind, key)
    entries = _build_entries(items)
    if not entries:
        raise HTTPException(404, "Nothing to download")
    # Release the connection now: the archive may take minutes to stream.
    db.close()
    filename = f"Atriolyric-{safe_component(name, kind)}.zip"
    return StreamingResponse(
        _stream_zip(entries), media_type="application/zip", headers=_attachment(filename)
    )
