import asyncio
import email.utils
import hashlib
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    HTTPException,
    Query,
    Request,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import can_access_root, media_file_path
from app.art import ArtBytes, ensure_art_thumb_cached, load_full_art
from app.config import art_thumb_path, settings, thumbnail_path
from app.custom_art import find_item_art
from app.deps import CurrentUser, DbSession
from app.hls import build_playlist, get_segment
from app.listenbrainz_data import prefetch_similar_in_background
from app.listening import ListenOutOfRange, record_listen
from app.models import MediaItem, MediaKind, PlayHistory, User, utcnow
from app.presenters import ART_CACHE_HEADERS, no_art_response
from app.schemas import (
    ListenedOut,
    ListenedReport,
    PlaybackModeOut,
    PlayedOut,
    PlayedReport,
)
from app.scrobbling import scrobble_track
from app.transcode import (
    ALLOWED_BITRATES_KBPS,
    ensure_transcoded,
    estimate_source_bitrate_kbps,
)
from app.video_meta import is_browser_playable, probe_duration_seconds

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["stream"])

CHUNK_SIZE = 1024 * 1024  # 1 MiB
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
}


def _image_content_type(path: Path) -> str:
    """Return the actual MIME type for a cached artwork file."""
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(path.suffix.lower(), "application/octet-stream")


def _resolve_file(
    db: Session, media_item_id: int, user: User
) -> tuple[MediaItem, Path]:
    item = db.get(MediaItem, media_item_id)
    if item is None:
        raise HTTPException(404, "Media item not found")
    if not can_access_root(db, user, item.library_root_id):
        # 404, not 403 - don't confirm to an unauthorized caller that this id exists.
        raise HTTPException(404, "Media item not found")
    file_path = media_file_path(item)
    if file_path is None:
        raise HTTPException(404, "Underlying file is missing")
    return item, file_path


def _hls_target(
    db: Session, media_item_id: int, user: User
) -> tuple[Path, float]:
    """Resolve a video for HLS: its file and duration. Releases the database
    connection before returning - the segment work after it can take a while."""
    item, file_path = _resolve_file(db, media_item_id, user)
    if item.kind == MediaKind.track:
        raise HTTPException(404, "Not a video")
    duration = item.duration_seconds
    db.close()
    if not duration or duration <= 0:
        duration = probe_duration_seconds(file_path)
    if not duration or duration <= 0:
        raise HTTPException(404, "Video duration unknown")
    return file_path, float(duration)


@router.get("/{media_item_id}/playback", response_model=PlaybackModeOut)
def get_playback(
    media_item_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """How the player should load this item: straight from /api/stream/{id},
    or through HLS because its codecs can't be decoded by a browser."""
    item, file_path = _resolve_file(db, media_item_id, user)
    is_video = item.kind != MediaKind.track
    db.close()
    if is_video and not is_browser_playable(file_path):
        return {
            "mode": "hls",
            "url": f"/api/stream/{media_item_id}/hls/index.m3u8",
        }
    return {"mode": "direct", "url": f"/api/stream/{media_item_id}"}


@router.get("/{media_item_id}/hls/index.m3u8")
def hls_playlist(
    media_item_id: int,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    _, duration = _hls_target(db, media_item_id, user)
    return Response(
        build_playlist(duration),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{media_item_id}/hls/{segment}")
def hls_segment(
    media_item_id: int,
    segment: str,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    match = re.fullmatch(r"(\d{1,6})\.ts", segment)
    if not match:
        raise HTTPException(404, "No such segment")
    file_path, duration = _hls_target(db, media_item_id, user)
    path = get_segment(media_item_id, user.id, file_path, int(match.group(1)), duration)
    return FileResponse(
        path, media_type="video/mp2t", headers={"Cache-Control": "no-store"}
    )


# Audio-only background rendition bitrate when the user's music setting is
# "original quality" (a video's own soundtrack is rarely better than this).
DEFAULT_BACKGROUND_AUDIO_KBPS = 192


def _audio_kbps(max_bitrate: int | None) -> int:
    """Follow the user's music quality cap; anything else gets the default."""
    return max_bitrate if max_bitrate in ALLOWED_BITRATES_KBPS else DEFAULT_BACKGROUND_AUDIO_KBPS


@router.get("/{media_item_id}/audio/index.m3u8")
def audio_playlist(
    media_item_id: int,
    db: DbSession,
    user: CurrentUser,
    max_bitrate: int | None = Query(default=None),
) -> Response:
    """The video's sound alone, as HLS, for background playback, at the
    caller's music quality cap."""
    _, duration = _hls_target(db, media_item_id, user)
    kbps = _audio_kbps(max_bitrate)
    return Response(
        build_playlist(duration, f"?max_bitrate={kbps}"),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{media_item_id}/audio/{segment}")
def audio_segment(
    media_item_id: int,
    segment: str,
    db: DbSession,
    user: CurrentUser,
    max_bitrate: int | None = Query(default=None),
) -> Response:
    match = re.fullmatch(r"(\d{1,6})\.ts", segment)
    if not match:
        raise HTTPException(404, "No such segment")
    file_path, duration = _hls_target(db, media_item_id, user)
    path = get_segment(
        media_item_id, user.id, file_path, int(match.group(1)), duration, audio_kbps=_audio_kbps(max_bitrate)
    )
    return FileResponse(
        path, media_type="video/mp2t", headers={"Cache-Control": "no-store"}
    )


@router.get("/{media_item_id}")
def stream_media(
    media_item_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    max_bitrate: int | None = Query(default=None),
) -> Response:
    item, file_path = _resolve_file(db, media_item_id, user)
    user_id = user.id
    # Everything below works from the loaded item and the file: give the
    # pooled connection back before a transcode (minutes) or a stream (as
    # long as the track plays). Loaded attributes stay readable.
    db.close()
    source_bitrate_kbps = (
        estimate_source_bitrate_kbps(file_path, item.duration_seconds)
        if item.kind == MediaKind.track
        else None
    )
    stream_bitrate_kbps = source_bitrate_kbps
    transcoded_kbps: int | None = None

    # Bitrate cap only ever applies to audio tracks, and only ever
    # transcodes *down* - a track already below the cap streams untouched.
    if max_bitrate is not None and item.kind == MediaKind.track and max_bitrate in ALLOWED_BITRATES_KBPS:
        transcoded_path = ensure_transcoded(item, file_path, max_bitrate)
        if transcoded_path is not None:
            file_path = transcoded_path
            stream_bitrate_kbps = float(max_bitrate)
            transcoded_kbps = max_bitrate

    stat = file_path.stat()
    file_size = stat.st_size
    content_type = _CONTENT_TYPES.get(
        file_path.suffix.lower(), "application/octet-stream"
    )
    etag = _representation_etag(file_path, stat, transcoded_kbps)
    last_modified = email.utils.formatdate(stat.st_mtime, usegmt=True)

    range_header = request.headers.get("range")
    if range_header and not _if_range_matches(request.headers.get("if-range"), etag, stat.st_mtime):
        # The client's earlier bytes came from another representation (the
        # original vs a transcode, or a file since replaced): stitching a
        # range onto them would corrupt the audio, so send it all again.
        range_header = None
    start, end = 0, file_size - 1
    status_code = 200
    if range_header:
        match = RANGE_RE.fullmatch(range_header.strip())
        if not match:
            raise HTTPException(
                416,
                "Invalid Range header",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        raw_start, raw_end = match.groups()
        if not raw_start and not raw_end:
            raise HTTPException(
                416,
                "Invalid Range header",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        if not raw_start:
            try:
                suffix_length = int(raw_end)
            except ValueError:
                suffix_length = 0
            if suffix_length <= 0 or file_size == 0:
                raise HTTPException(
                    416,
                    "Requested range not satisfiable",
                    headers={"Content-Range": f"bytes */{file_size}"},
                )
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            try:
                start = int(raw_start)
                end = int(raw_end) if raw_end else file_size - 1
            except ValueError:
                start, end = file_size, -1
            end = min(end, file_size - 1)
        if start > end or start >= file_size:
            raise HTTPException(
                416,
                "Requested range not satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        status_code = 206

    content_length = end - start + 1

    def iter_range():
        sent = 0
        outcome = "complete"
        try:
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    sent += len(chunk)
                    yield chunk
        except GeneratorExit:
            # The client hung up mid-stream (seek, track change, phone lock).
            outcome = "client_closed"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            if settings.verbose_logging:
                logger.info(
                    "stream_finished media_item_id=%s user_id=%s outcome=%s range=%s-%s sent=%d/%d",
                    media_item_id,
                    user_id,
                    outcome,
                    start,
                    end,
                    sent,
                    content_length,
                )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "ETag": etag,
        "Last-Modified": last_modified,
    }
    if source_bitrate_kbps is not None:
        # Browsers expose Server-Timing values on same-origin media resource
        # timing entries. The player uses these average rates to estimate the
        # bandwidth required by original quality and by the active capped
        # stream. This is useful for variable-bitrate sources such as FLAC.
        timing_metrics = [f'source-bitrate;desc="{source_bitrate_kbps:.3f}"']
        # stream_bitrate_kbps starts from the source estimate and is only
        # replaced when a capped transcode is selected, so it is present here.
        timing_metrics.append(f'stream-bitrate;desc="{stream_bitrate_kbps:.3f}"')
        headers["Server-Timing"] = ", ".join(timing_metrics)
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    return StreamingResponse(
        iter_range(), status_code=status_code, media_type=content_type, headers=headers
    )


def _representation_etag(path: Path, stat: os.stat_result, transcoded_kbps: int | None) -> str:
    """A strong validator for exactly the bytes served: which file, its size
    and modification time, and the bitrate for a transcode (the original and
    a capped copy of one track are different representations)."""
    digest = hashlib.sha1(  # noqa: S324 - an identifier, not security
        f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}".encode(), usedforsecurity=False
    ).hexdigest()[:20]
    return f'"{digest}{f"-t{transcoded_kbps}" if transcoded_kbps else ""}"'


def _if_range_matches(if_range: str | None, etag: str, mtime: float) -> bool:
    """Whether a Range request may be answered as a range (RFC 9110 13.1.5):
    no If-Range, the same strong ETag, or a date no older than the file."""
    if if_range is None:
        return True
    if_range = if_range.strip()
    if if_range.startswith(('"', "W/")):
        return if_range == etag  # weak validators never match
    try:
        when = email.utils.parsedate_to_datetime(if_range)
    except (TypeError, ValueError):
        return False
    return int(mtime) <= when.timestamp()


@router.get("/{media_item_id}/thumbnail")
def get_thumbnail(
    media_item_id: int,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    item = db.get(MediaItem, media_item_id)
    if item is None or not can_access_root(db, user, item.library_root_id):
        raise HTTPException(404, "Media item not found")
    custom = find_item_art(db, item)
    if custom is not None and (settings.custom_art_dir / custom.thumb_path).is_file():
        return FileResponse(
            settings.custom_art_dir / custom.thumb_path,
            media_type="image/jpeg",
            headers=ART_CACHE_HEADERS,
        )
    path = thumbnail_path(media_item_id)
    if not path.is_file():
        return no_art_response()
    return FileResponse(path, media_type="image/jpeg", headers=ART_CACHE_HEADERS)


# The per-media RLocks in app.art serialize duplicate work for the same
# item, and the resize semaphore caps simultaneous ffmpeg processes. A
# caller waiting on either on the shared anyio worker threadpool would hold
# its DB connection (auth's session is pinned until the response
# finishes), so a grid of tiles whose art isn't generated yet could park
# up to pool-size requests on idle connections and exhaust the pool -
# taking every other endpoint down with it (30s checkout timeout -> 500s).
# The art endpoints are async and run the (potentially slow) art work on
# this small dedicated pool instead: a request waiting for its turn holds no
# DB connection and no shared-pool thread - just an event-loop task. 12
# workers (~3x the 2-4 CPU cores an ffmpeg resize realistically saturates)
# bounds a grid-wide burst without letting ffmpeg processes pile up on the
# host.
_art_work_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="atriolyric-art")

def _load_art(load, item: MediaItem) -> Path | ArtBytes | None:
    # Resolving the file path touches the library's disk (which may be busy
    # or spun down), so it runs here on the art pool, never the event loop.
    return load(item, media_file_path(item, require_file=False))


def _resolve_art(
    media_item_id: int, db: Session, user: User, ready_path, thumb: bool
) -> Response | MediaItem:
    """The blocking first half of an art request, run on a worker thread: the
    item, access and uploaded-art lookups and the ready-file checks. Returns
    the response when one of those answers, else the item, with everything
    the art work needs loaded and the request's connection released."""
    item = db.get(MediaItem, media_item_id)
    if item is None or not can_access_root(db, user, item.library_root_id):
        raise HTTPException(404, "Media item not found")
    # Uploaded art (the song's own, else its album's) beats everything else.
    custom = find_item_art(db, item)
    if custom is not None:
        custom_file = settings.custom_art_dir / (custom.thumb_path if thumb else custom.path)
        if custom_file.is_file():
            return FileResponse(
                custom_file,
                media_type=_image_content_type(custom_file),
                headers=ART_CACHE_HEADERS,
            )
    if ready_path is not None:
        ready = ready_path(media_item_id)
        if ready.is_file():
            return FileResponse(
                ready, media_type=_image_content_type(ready), headers=ART_CACHE_HEADERS
            )
    # Everything needed from the DB is loaded onto `item` (its root too, for
    # resolving the file path later) - the connection is explicitly released
    # before the (potentially slow) art work; see the art pool above.
    _ = item.library_root
    db.close()
    return item


def _album_siblings(db: Session, item: MediaItem) -> list[MediaItem]:
    """Other songs of the same album, lowest id first (at most a few).

    The same Album row (app.albums: owner and title), not just the same
    title: with no album artist tag, a title many artists share ("Greatest
    Hits", "Singles") would hand a song another artist's picture."""
    album_id, root_id, item_id = item.album_id, item.library_root_id, item.id
    if album_id is None:
        return []
    rows = db.execute(
        select(MediaItem)
        .where(
            MediaItem.library_root_id == root_id,
            MediaItem.kind == MediaKind.track,
            MediaItem.album_id == album_id,
            MediaItem.id != item_id,
        )
        .order_by(MediaItem.id)
        .limit(4)
    ).scalars().all()
    for row in rows:
        _ = row.library_root
    db.close()
    return list(rows)


async def _art_response(
    media_item_id: int,
    db: Session,
    user: User,
    load,
    ready_path=None,
    thumb: bool = False,
) -> Response:
    # The lookups are quick but blocking, so they run on the shared worker
    # threadpool like any sync endpoint (never on the event loop); the slow
    # art work then runs on the dedicated art pool with no connection held.
    resolved = await run_in_threadpool(_resolve_art, media_item_id, db, user, ready_path, thumb)
    if isinstance(resolved, Response):
        return resolved
    loop = asyncio.get_running_loop()
    art = await loop.run_in_executor(_art_work_pool, _load_art, load, resolved)
    if art is None:
        # A song with no embedded picture shows its album's (the album tile
        # uses the album's lowest-id song, which does have one).
        siblings = await run_in_threadpool(_album_siblings, db, resolved)
        for sibling in siblings:
            art = await loop.run_in_executor(_art_work_pool, _load_art, load, sibling)
            if art is not None:
                break
    if art is None:
        return no_art_response()
    if isinstance(art, ArtBytes):
        return Response(art.data, media_type=art.mime, headers=ART_CACHE_HEADERS)
    return FileResponse(art, media_type=_image_content_type(art), headers=ART_CACHE_HEADERS)


@router.get("/{media_item_id}/art")
async def get_art(
    media_item_id: int,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    """A resized copy for grid/tile display (Artists, Albums, Playlists,
    track rows, ...) - see ensure_art_thumb_cached. Use /art/full for a
    context that actually shows the image large, like Now Playing. An
    existing thumbnail is served without touching the library at all."""
    return await _art_response(
        media_item_id,
        db,
        user,
        ensure_art_thumb_cached,
        ready_path=art_thumb_path,
        thumb=True,
    )


@router.get("/{media_item_id}/art/full")
async def get_art_full(
    media_item_id: int,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    return await _art_response(media_item_id, db, user, load_full_art)


# How far a reported start may be from the server's clock: scrobbles older
# than 14 days are not kept, and a client clock may run a little fast.
PLAYED_MAX_AGE = timedelta(days=14)
PLAYED_MAX_FUTURE = timedelta(minutes=5)


@router.post("/{media_item_id}/played", response_model=PlayedOut)
def record_played(
    media_item_id: int,
    background_tasks: BackgroundTasks,
    db: DbSession,
    user: CurrentUser,
    report: PlayedReport = Body(...),
) -> dict:
    """Records one play in the history and scrobbles it.

    The body is required and names when the play began (`started_at`): a
    track or music video once it has been listened to, a plain video when it
    loaded. A repeat of the same user, item and start is not recorded twice."""
    item = db.get(MediaItem, media_item_id)
    if item is None or not can_access_root(db, user, item.library_root_id):
        raise HTTPException(404, "Media item not found")
    started_at = report.started_at
    if started_at.tzinfo is not None:
        started_at = started_at.astimezone(UTC).replace(tzinfo=None)
    now = utcnow()
    if started_at < now - PLAYED_MAX_AGE or started_at > now + PLAYED_MAX_FUTURE:
        raise HTTPException(422, "started_at is out of range")
    # A retry of a report the client never saw confirmed: the same user,
    # item and start. Checked first so the usual case logs no error; the
    # unique index catches two retries racing each other.
    duplicate = db.execute(
        select(PlayHistory.id)
        .where(
            PlayHistory.user_id == user.id,
            PlayHistory.media_item_id == media_item_id,
            PlayHistory.started_at == started_at,
        )
        .limit(1)
    ).first()
    if duplicate is not None:
        return {"ok": True, "recorded": False}
    db.add(PlayHistory(user_id=user.id, media_item_id=media_item_id, started_at=started_at))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"ok": True, "recorded": False}
    # Provider calls can each take up to ten seconds. Keep playback/history
    # responsive and perform best-effort scrobbling after the response.
    background_tasks.add_task(scrobble_track, db, user, item, started_at)
    # Warm ListenBrainz's similar tracks for this song: autoplay never calls
    # out itself, it reads what this cached (app.listenbrainz_data). Skipped
    # entirely for a user who turned ListenBrainz recommendations off.
    if user.online_recommendations:
        prefetch_similar_in_background(item.id)
    return {"ok": True, "recorded": True}


@router.post("/{media_item_id}/listened", response_model=ListenedOut)
def record_listened(
    media_item_id: int,
    db: DbSession,
    user: CurrentUser,
    report: ListenedReport = Body(...),
) -> dict:
    """Records how one listen ended (app.listening): finished, skipped,
    stopped, transferred, error or unknown, with how long it was heard.

    Sent by the device producing the sound once per track or music video it
    left, whether or not that counted as a play; `started_at` is the same as
    the play's. Never a play or a scrobble. A repeat of the same user, item
    and start is not recorded twice."""
    item = db.get(MediaItem, media_item_id)
    if item is None or not can_access_root(db, user, item.library_root_id):
        raise HTTPException(404, "Media item not found")
    try:
        recorded = record_listen(db, user.id, media_item_id, report)
    except ListenOutOfRange as error:
        raise HTTPException(422, str(error)) from None
    return {"ok": True, "recorded": recorded}
