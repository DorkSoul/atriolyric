import asyncio
import json
import logging
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import provider_queue
from app.activity import begin_request, log_token, request_logger, set_detail
from app.activity_log import actor_name, record_event, wait_for_writes
from app.android_app import android_app_info
from app.art import backfill_art_thumbs
from app.art_epoch import art_epoch
from app.audio_analysis import start_audio_analysis, stop_audio_analysis
from app.background import join_all, spawn
from app.body_limit import BodyLimitMiddleware
from app.cache_warmup import start_cache_warmup, stop_cache_warmup
from app.cleanup import (
    enforce_cache_budget,
    prune_activity_events,
    prune_play_history,
    purge_expired_sessions,
)
from app.config import settings
from app.db import SessionLocal
from app.default_radio_stations import DEFAULT_RADIO_STATIONS
from app.deps import DbSession
from app.discovery import run_due_auto_refreshes
from app.external_cache import prune_expired
from app.hls import reset_hls_dir, shutdown_sessions
from app.log_mirror import install_log_mirror, remove_log_mirror
from app.logging_setup import configure_app_logging
from app.loudness import start_loudness_backfill
from app.metadata_taxonomy import replace_item_genres
from app.migrations import run_migrations
from app.models import (
    Device,
    LibraryTask,
    MediaItem,
    RadioStation,
    SystemSetting,
    User,
    utcnow,
)
from app.playback import prune_stale_devices
from app.renditions import shutdown_renditions
from app.routers import (
    admin,
    art,
    auth,
    client_log,
    download,
    favorites,
    integrations,
    library,
    metadata,
    playback,
    playlists,
    preferences,
    radio,
    search,
    setup,
    stream,
    want_list,
)
from app.routers.setup import issue_setup_code
from app.schemas import (
    VersionOut,
)
from app.scrobbling import retry_queued_scrobbles
from app.security import hash_password
from app.security_headers import OriginCheckMiddleware, SecurityHeadersMiddleware
from app.version import app_version as build_version
from app.version import read_web_version
from app.want_list import refresh_wanted_catalogs
from app.want_metadata import backfill_wanted_metadata

logger = logging.getLogger(__name__)

# Uvicorn's own logging config leaves this package's loggers without a
# handler, so logger.info here would be dropped - including the startup
# art-thumbnail backfill progress lines an operator expects in
# `docker logs`. See app/logging_setup for why it is scoped to `app`.
configure_app_logging()


class _ClientAddressLogFilter(logging.Filter):
    """Keeps uvicorn's lines that name a client address out of the default
    log: its access line (every request, so every `GET /api/stream/{id}`,
    with the address) and its WebSocket handshake line. The README promises
    no address in the log unless verbose logging is on, and the app's own
    request log already covers writes, failures and slow requests. Filtered
    here rather than with `--no-access-log` so that verbose logging still
    has every uvicorn line it had (requests outside /api, query strings),
    whichever way the server was started."""

    def filter(self, record: logging.LogRecord) -> bool:
        if settings.verbose_logging:
            return True
        if record.name == "uvicorn.access":
            return False
        return '"WebSocket %s"' not in str(record.msg)


_client_address_log_filter = _ClientAddressLogFilter()
# uvicorn logs the handshake line on uvicorn.error.
for _uvicorn_logger in ("uvicorn.access", "uvicorn.error"):
    logging.getLogger(_uvicorn_logger).addFilter(_client_address_log_filter)


def mark_playback_devices_disconnected() -> None:
    """A process restart drops every WebSocket, so no saved device row can
    honestly remain connected until its browser registers again."""
    db = SessionLocal()
    try:
        db.execute(
            update(Device).where(Device.is_connected.is_(True)).values({Device.is_connected: False}),
            execution_options={"synchronize_session": False},
        )
        # Every snapshot lists every device row, and a browser that lost its
        # token (private window, cleared storage) leaves one behind for good.
        prune_stale_devices(db)
        db.commit()
    finally:
        db.close()


def mark_interrupted_library_tasks() -> None:
    """Background threads cannot survive a server restart.

    Leaving their rows as ``running`` would make the Admin page display a
    permanent, misleading progress bar, so retain the task but mark it failed.
    """
    db = SessionLocal()
    try:
        now = utcnow()
        db.execute(
            update(LibraryTask)
            .where(LibraryTask.status == "running")
            .values(
                {
                    LibraryTask.status: "failed",
                    LibraryTask.error: "Interrupted by server restart",
                    LibraryTask.updated_at: now,
                    LibraryTask.completed_at: now,
                }
            ),
            execution_options={"synchronize_session": False},
        )
        # A stop was already requested, so the restart is what finished it;
        # left as "stopping" it would block every new task for the root.
        db.execute(
            update(LibraryTask)
            .where(LibraryTask.status == "stopping")
            .values(
                {
                    LibraryTask.status: "stopped",
                    LibraryTask.updated_at: now,
                    LibraryTask.completed_at: now,
                }
            ),
            execution_options={"synchronize_session": False},
        )
        db.commit()
    finally:
        db.close()


def seed_default_user() -> None:
    run_migrations()
    # Per-boot housekeeping, not schema changes: these must run on every
    # start, so they stay out of the versioned migration list.
    mark_playback_devices_disconnected()
    mark_interrupted_library_tasks()
    db = SessionLocal()
    try:
        # Bootstrap exactly once: changing or deleting the configured account
        # must not silently recreate a privileged login on the next restart.
        if db.execute(select(User.id).limit(1)).first() is None:
            if settings.default_password:
                db.add(
                    User(
                        username=settings.default_username,
                        password_hash=hash_password(settings.default_password),
                        is_admin=True,
                    )
                )
                db.commit()
            else:
                # No headless credentials: the web app's setup screen creates
                # the first admin, gated by a code only the server log shows.
                issue_setup_code()
        # Copy existing libraries' already-indexed primary genres into the
        # many-to-many genre table immediately; a later scan adds
        # additional embedded genre values where present.
        legacy_items = db.scalars(
            select(MediaItem).where(
                MediaItem.genre.is_not(None),
                ~MediaItem.genres.any(),
            )
        ).all()
        for item in legacy_items:
            replace_item_genres(db, item, [item.genre], "embedded")
        if legacy_items:
            db.commit()
    finally:
        db.close()


def seed_default_radio_stations() -> None:
    """Idempotent and safe to run on every boot:
    - a station whose stream_url is already in the table is left alone,
      except that a missing description/provider is backfilled
    - a new default gets inserted once, tracked by its own marker. This
      lets later releases add defaults to existing installs without
      resurrecting a station that a user deliberately deleted.
    """
    db = SessionLocal()
    try:
        by_url = {s.stream_url: s for s in db.scalars(select(RadioStation))}
        for name, stream_url, description, provider in DEFAULT_RADIO_STATIONS:
            existing = by_url.get(stream_url)
            marker_key = f"default_radio_station:{stream_url}"
            marker = db.get(SystemSetting, marker_key)
            if existing is not None:
                if existing.provider is None:
                    existing.description = description
                    existing.provider = provider
                if marker is None:
                    db.add(SystemSetting(key=marker_key, value="1"))
                continue
            if marker is not None:
                continue
            db.add(
                RadioStation(
                    name=name,
                    stream_url=stream_url,
                    description=description,
                    provider=provider,
                    is_shared=True,
                )
            )
            db.add(SystemSetting(key=marker_key, value="1"))
        db.commit()
    finally:
        db.close()


# Checked well under once an hour so a user's chosen refresh hour is never
# missed by a wide margin, while `run_due_auto_refreshes` itself guards
# against running twice in the same day if this fires more than once
# within that hour.
DISCOVERY_SCHEDULER_INTERVAL_SECONDS = 600


def _art_backfill_progress_reporter() -> Callable[[int], None]:
    """One log line per backfill page that actually generated something.

    ``backfill_art_thumbs`` calls back once per page of 200 candidate ids -
    including pages where every item already had a thumbnail - so a healthy
    library would otherwise print "0 generated so far" once per 200 tracks
    on every startup. The first count is always reported, so an operator can
    tell "the pass ran and found nothing missing" apart from "the pass never
    logged at all".
    """
    reported: int | None = None

    def report(done: int) -> None:
        nonlocal reported
        if done == reported:
            return
        reported = done
        logger.info("Art thumbnail backfill: %d generated so far", done)

    return report


def _start_art_thumb_backfill() -> None:
    """Generate missing grid-tile thumbnails for the whole library in the
    background at startup.

    Scan time already generates each item's thumbnail and nothing evicts
    them, so this is a no-op for a healthy library - it covers items that
    still have no thumbnail (they would otherwise only get one when a tile
    first requests it, which is the slow convert-on-request path).
    Deliberately a daemon thread, not a task on the event
    loop: the work is minutes of disk reads and ffmpeg, none of which the
    event loop must ever see."""
    def run() -> None:
        try:
            total = backfill_art_thumbs(
                progress_cb=_art_backfill_progress_reporter()
            )
            if total:
                logger.info("Art thumbnail backfill complete: %d generated", total)
        except Exception:
            logger.exception("Art thumbnail backfill failed")

    spawn("art-thumb-backfill", run)


async def discovery_scheduler_loop() -> None:
    while True:
        await asyncio.sleep(DISCOVERY_SCHEDULER_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(_run_due_auto_refreshes)
        except Exception:
            logger.exception("Scheduled discovery auto-refresh failed")


def _refresh_query_statistics(db: Session) -> None:
    """Nothing else runs ANALYZE, so without this the query planner picks
    indexes without knowing how big any table is. `PRAGMA optimize`
    re-analyses only tables that have none or have changed size a lot, and
    bounds the work itself; 0x10002 is its default plus "all tables, not
    only those this connection happened to query" (each tick may get a
    different pooled connection)."""
    db.execute(text("PRAGMA optimize=0x10002"))
    db.commit()


def _run_store_maintenance(db) -> None:
    """Keep the stores that otherwise only grow bounded: the transcode cache
    (budget, least recently used first), expired provider-cache rows,
    expired sessions and, when a retention is set, old play history. Each
    runs on its own so one failure skips only itself."""
    for name, step in (
        ("cache budget", enforce_cache_budget),
        ("external cache prune", lambda: prune_expired(db)),
        ("expired session purge", lambda: purge_expired_sessions(db)),
        ("play history prune", lambda: prune_play_history(db)),
        ("activity log prune", lambda: prune_activity_events(db)),
        ("query statistics refresh", lambda: _refresh_query_statistics(db)),
    ):
        try:
            step()
        except Exception:
            db.rollback()
            logger.exception("Scheduled %s failed", name)


def _queued_with_session(job: Callable[[Session], object]) -> Callable[[], None]:
    """A provider_queue job that runs `job` in a session of its own."""

    def run() -> None:
        db = SessionLocal()
        try:
            job(db)
        finally:
            db.close()

    return run


def _run_due_auto_refreshes() -> None:
    """Own the SQLAlchemy session in the worker thread that uses it. Each
    job runs on its own: a failure is rolled back so it cannot leave the
    session unusable for the next one."""
    db = SessionLocal()
    try:
        # First, so a long refresh below cannot hold the cleanup back.
        _run_store_maintenance(db)
        # Scrobbles ListenBrainz could not take earlier (outage, rate limit).
        try:
            retry_queued_scrobbles(db)
        except Exception:
            db.rollback()
            logger.exception("Scheduled scrobble retry failed")
        try:
            run_due_auto_refreshes(db)
        except Exception:
            db.rollback()
            logger.exception("Scheduled discovery refresh failed")
    finally:
        db.close()
    # Same cadence: re-browse wanted artists whose catalog is a week old, so
    # their new releases reach the want list, and fill missing want-list
    # metadata. Slow, rate-limited MusicBrainz work: it goes on the provider
    # queue as scheduled work instead of holding this tick. There it runs
    # one job at a time, only when no user-triggered job (a cover, a
    # tracklist, a wanted entry's metadata) is waiting, hands the worker to
    # those between its own artists/entries/release groups, and its provider
    # calls give way to page requests. A tick that finds one still queued
    # adds nothing.
    provider_queue.submit(
        "scheduled:want-catalogs", _queued_with_session(refresh_wanted_catalogs), scheduled=True
    )
    provider_queue.submit(
        "scheduled:want-metadata", _queued_with_session(backfill_wanted_metadata), scheduled=True
    )


def _purge_sessions_at_startup() -> None:
    db = SessionLocal()
    try:
        purge_expired_sessions(db)
    except Exception:
        logger.exception("Expired session purge at startup failed")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # From here on the app log's warnings and background-work lines are also
    # kept for Admin -> Server activity.
    install_log_mirror()
    seed_default_user()
    seed_default_radio_stations()
    enforce_cache_budget()
    _purge_sessions_at_startup()
    reset_hls_dir()
    _start_art_thumb_backfill()
    start_loudness_backfill()
    # Waits for the loudness backfill to finish before decoding anything.
    start_audio_analysis()
    # Fills the slow whole-library caches now, after every library change
    # and once a day, so no request waits on a cold one.
    start_cache_warmup()
    scheduler_task = asyncio.create_task(discovery_scheduler_loop())
    try:
        yield
    finally:
        shutdown_sessions()
        shutdown_renditions()
        remove_log_mirror()
        # Events still queued for the activity log (a second at most).
        await asyncio.to_thread(wait_for_writes, 2)
        provider_queue.shutdown()
        stop_audio_analysis()
        stop_cache_warmup()
        # Give background workers (scans, backfills, provider jobs) a moment
        # to finish their current commit; they are daemons, so whatever is
        # still running after this is cut off when the process exits.
        still_running = await asyncio.to_thread(join_all, 10)
        if still_running:
            logger.warning("Background threads still running at shutdown: %s", ", ".join(still_running))
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


# The built app is served by this process in production; its interactive API
# docs (and the schema, which lists every route) are a development aid only.
_serving_built_app = settings.web_dist_path is not None
app = FastAPI(
    title="atriolyric",
    version=build_version(settings.web_dist_path),
    lifespan=lifespan,
    docs_url=None if _serving_built_app else "/docs",
    redoc_url=None if _serving_built_app else "/redoc",
    openapi_url=None if _serving_built_app else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    # Cookie-based auth needs allow_credentials, which the CORS spec forbids
    # pairing with a wildcard origin - explicit dev origins instead. In
    # practice the Vite dev server proxies /api same-origin, so this rarely
    # even applies; it matters once something hits the API cross-origin.
    allow_origins=list(settings.allowed_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The full Albums/Artists lists run to megabytes of JSON on a big library
# (~3.9MB -> ~0.6MB for 32k albums). Starlette already skips audio, video,
# images and 206 range responses; octet-stream is added because the stream
# router falls back to it for audio suffixes it doesn't recognise, and
# gzipping a whole audio file on the fly would only cost CPU.
app.add_middleware(
    GZipMiddleware,
    minimum_size=1024,
    exclude_content_types=(*DEFAULT_EXCLUDED_CONTENT_TYPES, "application/octet-stream"),
)


_forwarded_for_warned = False


def _warn_if_forwarded_for_untrusted(forwarded_for: str | None, client_host: str | None) -> bool:
    """Log once when a reverse proxy's X-Forwarded-For is being ignored.

    Uvicorn replaces the client address with the header's only for peers in
    FORWARDED_ALLOW_IPS (default: 127.0.0.1). For a trusted peer the address
    the app sees is therefore one of the header's entries; when it is not,
    the header came from a peer uvicorn did not trust, and the login limiter
    and the logs are keying on the proxy's own address. (Uvicorn ignores the
    standard `Forwarded` header altogether, so it is not checked: setting
    FORWARDED_ALLOW_IPS would not change what it does.) Returns whether it
    warned."""
    global _forwarded_for_warned
    if _forwarded_for_warned or not forwarded_for:
        return False
    if client_host in {part.strip() for part in forwarded_for.split(",")}:
        return False
    _forwarded_for_warned = True
    logger.warning(
        "A request carried X-Forwarded-For but arrived from %s, which uvicorn does "
        "not trust, so the login limiter and the logs see that address instead of "
        "the real client. If %s is your reverse proxy, set FORWARDED_ALLOW_IPS to "
        "its address or network (see README, 'Behind a reverse proxy').",
        client_host or "an unknown peer",
        client_host or "it",
    )
    return True


# Inside the request logging below (added after, so it runs outside), so a
# rejected request is still logged.
app.add_middleware(OriginCheckMiddleware, warn_forwarded=_warn_if_forwarded_for_untrusted)


# Long-running responses by design; their duration says nothing about health.
_SLOW_EXEMPT_PREFIXES = ("/api/stream/",)
_SLOW_REQUEST_SECONDS = 3.0
_QUIET_PATHS = {"/api/health"}


@app.exception_handler(StarletteHTTPException)
async def _logged_http_exception(request: Request, exc: StarletteHTTPException):
    set_detail(exc.detail)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _logged_validation_error(request: Request, exc: RequestValidationError):
    fields = ", ".join(
        ".".join(str(part) for part in error["loc"] if part != "body") + ": " + error["msg"]
        for error in exc.errors()[:5]
    )
    set_detail(f"invalid request ({fields})")
    return await request_validation_exception_handler(request, exc)


class RequestLogMiddleware:
    """One log line per API write, failure or slow request, with a request id.

    Added after the security middleware so it is outermost and also sees the
    requests that middleware rejects. Successful reads are not logged (that
    would drown real activity) unless verbose logging is on; uvicorn's own
    access line covers them only then too (see _ClientAddressLogFilter).
    A pure ASGI middleware like the others: the line is written when the
    response starts (its status and headers are known then; a streamed body
    may run on long after), and the X-Request-ID header is added to it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        if not path.startswith("/api/") or path in _QUIET_PATHS:
            await self.app(scope, receive, send)
            return
        context = begin_request()
        started = time.monotonic()
        response_started = False

        async def send_logged(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = context["request_id"]
                _log_api_request(
                    request, context, message["status"], headers, time.monotonic() - started
                )
            await send(message)

        try:
            await self.app(scope, receive, send_logged)
        except Exception:
            if not response_started:
                request_logger.exception(
                    "%s %s -> unhandled error (%.0fms) user=%s req=%s",
                    request.method,
                    log_token(path),
                    (time.monotonic() - started) * 1000,
                    log_token(context["user"] or "-"),
                    context["request_id"],
                )
                _record_server_error(request, context, 500, sys.exc_info()[0])
            raise


def _log_api_request(
    request: Request, context: dict, status: int, headers: MutableHeaders, elapsed: float
) -> None:
    path = request.url.path
    is_write = request.method not in {"GET", "HEAD", "OPTIONS"}
    slow = elapsed >= _SLOW_REQUEST_SECONDS and not path.startswith(_SLOW_EXEMPT_PREFIXES)
    # An unauthenticated GET is the browser asking "am I signed in?" - routine.
    routine_401 = status == 401 and not is_write
    if status >= 500:
        level = logging.ERROR
    elif status >= 400 and not routine_401:
        level = logging.WARNING
    elif slow:
        level = logging.WARNING
    elif is_write:
        level = logging.INFO
    else:
        level = logging.DEBUG if status >= 400 else None
    if level is None and settings.verbose_logging and path != "/api/client-log/debug":
        level = logging.INFO
    if level is None:
        return
    if status >= 500:
        _record_server_error(request, context, status, None)
    detail = context["detail"]
    request_logger.log(
        level,
        "%s %s -> %d (%.0fms)%s user=%s req=%s%s",
        request.method,
        log_token(path),
        status,
        elapsed * 1000,
        " SLOW" if slow else "",
        log_token(context["user"] or "-"),
        context["request_id"],
        f" detail={json.dumps(str(detail), ensure_ascii=False)}" if detail else "",
    )
    if settings.verbose_logging:
        request_logger.info(
            "  req=%s ip=%s range=%s bytes=%s ua=%s",
            context["request_id"],
            log_token(request.client.host) if request.client else "-",
            log_token(request.headers.get("range", "-")),
            headers.get("content-length", "-"),
            json.dumps(request.headers.get("user-agent", "-")[:120]),
        )


def _record_server_error(
    request: Request, context: dict, status: int, error: type[BaseException] | None
) -> None:
    """A server-side failure in the admin's activity log: method, path (no
    query string or body) and the exception's type, never its message."""
    what = f" ({error.__name__})" if error is not None else ""
    record_event(
        "server.error",
        "error",
        actor=actor_name(context.get("user")),
        summary=f"{request.method} {request.url.path} failed with {status}{what}",
        request_id=context.get("request_id"),
    )


app.add_middleware(RequestLogMiddleware)


# Outermost but one of the app's own middleware (the last one added runs
# first), so an oversized body is refused before authentication or any
# handler reads it.
app.add_middleware(BodyLimitMiddleware)
# Outside even that, so the body limit's 413 and unhandled errors carry the
# security headers too.
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth.router)
app.include_router(client_log.router)
app.include_router(admin.router)
app.include_router(library.router)
app.include_router(metadata.router)
app.include_router(want_list.router)
app.include_router(want_list.catalog_router)
app.include_router(stream.router)
app.include_router(art.router)
app.include_router(download.router)
app.include_router(playlists.router)
app.include_router(favorites.router)
app.include_router(preferences.router)
app.include_router(playback.router)
app.include_router(integrations.router)
app.include_router(radio.router)
app.include_router(search.router)
app.include_router(setup.router)


def _health_problems() -> list[str]:
    """What is wrong with the two things the app cannot work without: the
    database answering a query and the data directory (database, art, caches)
    being writable. Both checks are a few microseconds."""
    problems = []
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any failure here means "degraded", whatever it was
        logger.exception("Health check: the database did not answer")
        problems.append("database")
    try:
        # A real create + delete next to the database: a read-only mount or
        # a full disk fails here long before the app's own writes do.
        with tempfile.TemporaryFile(dir=settings.database_path.parent):
            pass
    except OSError:
        logger.exception("Health check: the data directory is not writable")
        problems.append("data_dir")
    return problems


@app.get("/api/health", response_model=None)
def health() -> Response | dict:
    problems = _health_problems()
    if problems:
        # urlopen in the container's HEALTHCHECK raises on a 503.
        return JSONResponse({"status": "degraded", "problems": problems}, status_code=503)
    return {"status": "ok"}


@app.get("/api/version", response_model=VersionOut)
def app_version(db: DbSession) -> dict:
    # Read per request (a tiny file) so it always matches what is on disk.
    # debug_log tells the web app whether the server wants its debug events;
    # when false it neither patches the audio element nor posts anything.
    # None (not the fallback) without a built frontend: there is no build
    # for an open app to compare itself with.
    version = read_web_version(settings.web_dist_path)
    return {
        "version": version,
        "debug_log": settings.verbose_logging,
        "android_app": android_app_info(settings.android_apk_path, version),
        "art_epoch": art_epoch(db),
    }


# Keep this mount last so API and WebSocket routes always win.  Production
# points it at the frontend build copied into the image; development leaves it
# unset and uses Vite's dev server instead.
class WebAppFiles(StaticFiles):
    """The built frontend. Without a Cache-Control header browsers guess a
    freshness lifetime for index.html from its age, so a reload after a
    deploy could keep running the previous build for hours. The shell is
    always revalidated (a cheap 304 when unchanged); the content-hashed
    files under /assets/ never change and can be cached for good."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            if path.startswith("assets/"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        return response


if settings.web_dist_path is not None and settings.web_dist_path.is_dir():
    app.mount(
        "/",
        WebAppFiles(directory=settings.web_dist_path, html=True),
        name="web",
    )
