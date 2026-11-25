"""Fills the slow whole-library caches in the background, so no request has
to: at startup (they start empty), after every library change (a change
empties app.library_cache) and once a day (Library info is cached per date).

Each of these is a query over the whole library. On a large one a request
that finds the cache cold waits many seconds - longer than the web app
waits for an answer in the case of the genre and tag counts, so the
specify-mix dialog would fail while the server was still counting.

What is warmed, most-used first:

* the sidebar's tab counts (asked for every time the app opens);
* the Admin page's background-pass counts (app.background_status);
* the genre and tag counts behind the specify-mix dialog and /genres;
* the Albums and Artists tabs as they first open (A-Z: the first page and
  the full list) - other sorts and filters are computed when asked for;
* the Library info numbers.

The Songs tab is not here: it is read a page at a time, which is quick."""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import background_status, library_cache
from app.access import accessible_root_ids
from app.background import spawn
from app.db import SessionLocal
from app.label_counts import KINDS, label_counts
from app.models import User, utcnow
from app.routers.library.albums_artists import BrowseSort, list_albums, list_artists
from app.routers.library.stats import shared_nav_counts, shared_stats

logger = logging.getLogger(__name__)

# A change is rarely alone (a tag edit after a tag edit, a scan and then its
# loudness pass): wait until the library has been quiet this long, so a
# burst of changes costs one pass.
SETTLE_SECONDS = 5.0
# The first page the Albums and Artists tabs ask for (web/src/api.ts).
FIRST_PAGE = 100

_wake = threading.Event()
_stop = threading.Event()
_thread_lock = threading.Lock()
_thread: threading.Thread | None = None

Step = Callable[[Session, User], object]


def _browse(route: Callable[..., object], *, full: bool) -> Step:
    """The Albums or Artists tab's opening request, as the route itself
    answers it (so the cache key is the route's own)."""

    def step(db: Session, user: User) -> object:
        return route(
            db, user, q=None, genre=None, sort=BrowseSort.alphabetical, direction="asc",
            limit=FIRST_PAGE, offset=0, full=full, response=None, request=None,
            **({"artist": None, "appears_on": False} if route is list_albums else {}),
        )

    return step


def _steps() -> Iterator[tuple[str, Step]]:
    yield "tab counts", lambda db, user: shared_nav_counts(db, accessible_root_ids(db, user))
    for kind in KINDS:
        yield f"{kind} counts for mixes", lambda db, user, kind=kind: label_counts(db, user, kind, mixable=True)
    yield "albums, first page", _browse(list_albums, full=False)
    yield "artists, first page", _browse(list_artists, full=False)
    yield "albums, full list", _browse(list_albums, full=True)
    yield "artists, full list", _browse(list_artists, full=True)
    for kind in KINDS:
        yield f"{kind} counts", lambda db, user, kind=kind: label_counts(db, user, kind)
    yield "library info", lambda db, user: shared_stats(db, accessible_root_ids(db, user))


def warm_caches(stop: threading.Event | None = None) -> bool:
    """One pass: every step for every user. Users sharing a root set share
    the cache entries, so the second one costs next to nothing.

    Returns False when it gave up part-way, because the library changed
    again (what it computed would not be kept; the next pass redoes it) or
    `stop` was set (the server is shutting down)."""
    version = library_cache.library_version()
    db = SessionLocal()
    try:
        # Not per user, and the Admin page polls it: first.
        background_status.library_counts(db, fresh=True)
        db.rollback()
        user_ids = list(db.execute(select(User.id).order_by(User.id)).scalars())
        for _name, step in _steps():
            for user_id in user_ids:
                if (stop is not None and stop.is_set()) or library_cache.library_version() != version:
                    return False
                user = db.get(User, user_id)
                if user is not None:
                    step(db, user)
                # Each step reads the library as it is now, and no read
                # transaction is held open across the whole pass.
                db.rollback()
        return True
    finally:
        db.close()


def _seconds_until_tomorrow() -> float:
    """Until just after the next UTC midnight, when Library info's cache
    entry (keyed by date) goes out of use."""
    now = utcnow()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return max(1.0, (tomorrow - now).total_seconds())


def _run() -> None:
    global _thread
    while True:
        # A library change, a stop, or the new day.
        if _wake.wait(_seconds_until_tomorrow()):
            # Start over whenever another change arrives while settling.
            while not _stop.is_set():
                _wake.clear()
                if not _wake.wait(SETTLE_SECONDS):
                    break
        with _thread_lock:
            # Under the lock, so a start right after a stop either finds
            # this thread still serving or knows to begin a new one.
            if _stop.is_set():
                _thread = None
                return
        started = time.monotonic()
        try:
            finished = warm_caches(_stop)
        except Exception:
            # The next library change tries again; until then requests
            # compute for themselves.
            logger.exception("Could not warm the library caches")
            continue
        if finished:
            logger.info("Library caches warmed in %.1fs", time.monotonic() - started)


def start_cache_warmup() -> None:
    """Warm the caches now (they start empty), after every library change
    and once a day, in one background thread."""
    global _thread
    with _thread_lock:
        _stop.clear()
        library_cache.add_bump_listener(_wake.set)
        _wake.set()
        if _thread is None:
            _thread = spawn("cache-warmup", _run)


def stop_cache_warmup() -> None:
    """Stop warming: the thread ends after the step it is running."""
    with _thread_lock:
        library_cache.remove_bump_listener(_wake.set)
        _stop.set()
        _wake.set()
