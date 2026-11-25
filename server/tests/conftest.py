from __future__ import annotations

import os
import tempfile
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

# Point the module-level engine at a throwaway file before `app.*` is
# imported anywhere, so the one-time startup seeding in app.main's lifespan
# never touches a real dev database. Assigned unconditionally: a path
# already exported in the shell would otherwise win, and every test client's
# startup would migrate that database and wipe the hls/ and video_cache/
# folders beside it. Every other ATRIOLYRIC_* setting is dropped too, so the
# suite runs on the defaults whatever the shell exports (an exported
# COOKIE_SECURE=true, say, would break cookie auth over the test client).
for _name in [name for name in os.environ if name.startswith("ATRIOLYRIC_")]:
    del os.environ[_name]
# A Secure session cookie is never sent back over the test client's plain
# http://testserver, so the suite turns it off explicitly; test_config.py
# covers the default.
os.environ["ATRIOLYRIC_COOKIE_SECURE"] = "false"
os.environ["ATRIOLYRIC_DATABASE_PATH"] = str(
    Path(tempfile.mkdtemp()) / "atriolyric-test.db"
)

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import APIC, TALB, TCON, TDRC, TIT2, TPE1, TRCK
from mutagen.wave import WAVE
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db import configure_sqlite_connection, get_db, make_session_factory
from app.main import app
from app.migrations import run_migrations
from app.models import User, utcnow
from app.security import hash_password

DEFAULT_USERNAME = "dork_soul"
DEFAULT_PASSWORD = "changeme"


def played_now() -> dict:
    """A `/played` body for a play that began just now (the body and its
    `started_at` are required; each call gives a distinct start)."""
    return {"started_at": utcnow().isoformat()}


def make_audio_fixture(
    path: Path,
    *,
    title: str,
    artist: str | None,
    album: str | None,
    genre: str | None,
    track_number: int = 1,
    seconds: float = 1.0,
    embed_art: bytes | None = None,
    date: str | int | None = None,
) -> None:
    """Generate a tiny silent WAV with ID3 tags — no ffmpeg required.
    Pass None for artist/album/genre to leave that tag entirely absent
    (e.g. to test metadata-enrichment backfill of a blank field). Pass
    `embed_art` (raw JPEG/PNG bytes) to also attach an APIC cover frame.
    `date` accepts any precision the real tag might have ("1999",
    "1999-05", "1999-05-20")."""
    framerate = 8000
    n_frames = int(framerate * seconds)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(b"\x00\x00" * n_frames)

    audio = WAVE(str(path))
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=title))
    if artist is not None:
        audio.tags.add(TPE1(encoding=3, text=artist))
    if album is not None:
        audio.tags.add(TALB(encoding=3, text=album))
    if genre is not None:
        audio.tags.add(TCON(encoding=3, text=genre))
    audio.tags.add(TRCK(encoding=3, text=str(track_number)))
    if date is not None:
        audio.tags.add(TDRC(encoding=3, text=str(date)))
    if embed_art is not None:
        audio.tags.add(
            APIC(encoding=3, mime="image/jpeg", type=3, desc="cover", data=embed_art)
        )
    audio.save()


@pytest.fixture(autouse=True)
def _no_background_provider_jobs(request, monkeypatch):
    """Adding a want-list entry (or wanting an artist) queues provider
    downloads on app.provider_queue's real worker thread, which would call
    the live MusicBrainz/ListenBrainz APIs during the run and back up the shared
    rate limiter for every other test. Queued jobs are dropped here;
    tests that exercise the queue itself keep the real one."""
    if request.module.__name__.endswith("test_provider_queue"):
        return
    monkeypatch.setattr("app.provider_queue.submit", lambda key, job, **_: True)
    monkeypatch.setattr("app.provider_queue.submit_interactive", lambda key, job, **_: True)


@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch):
    """No test reaches a real service: every httpx connection - module
    functions and Client instances alike (MusicBrainz, Cover Art Archive,
    ListenBrainz, Wikipedia, Deezer, lrclib, art downloads) - is refused at
    the transport, so a provider a test did not mock answers as if offline.
    Mocks of httpx.get/post or of a provider's client sit above the
    transport and still work; the app's own TestClient uses its own
    transport. Tests marked `network` are left alone."""
    if request.node.get_closest_marker("network"):
        return
    import httpx

    def refuse(_transport, http_request):
        raise httpx.ConnectError(f"No network in tests ({http_request.url.host})", request=http_request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture(autouse=True)
def _inline_provider_fetches(request, monkeypatch):
    """Info, lyrics and catalog requests answer from stored data and fetch
    in the background (app.deferred). Most tests expect the answer to
    include what their mocked provider returns, so they fetch inline;
    tests marked `deferred_fetches` keep the real behaviour."""
    from app import deferred
    from app.metadata_providers import deezer

    deferred._tried.clear()
    deezer._memory.clear()
    if not request.node.get_closest_marker("deferred_fetches"):
        monkeypatch.setattr(deferred, "DEFER", False)
    yield
    deferred._tried.clear()
    deezer._memory.clear()


@pytest.fixture(autouse=True)
def _no_lifespan_background_jobs(request, monkeypatch):
    """Every test client enters the app's real lifespan. Its migrations,
    seeding and housekeeping stay, but the art-thumbnail and loudness
    backfill threads and the scheduler task it also starts would run (or
    sit waiting) beside every test, against the shared module-level
    database. Tests marked `real_background_jobs` keep the real starters."""
    if request.node.get_closest_marker("real_background_jobs"):
        return

    async def idle_scheduler() -> None:
        return None

    monkeypatch.setattr("app.main._start_art_thumb_backfill", lambda: None)
    monkeypatch.setattr("app.main.start_loudness_backfill", lambda: None)
    monkeypatch.setattr("app.main.start_audio_analysis", lambda: None)
    monkeypatch.setattr("app.main.start_cache_warmup", lambda: None)
    # The app log is not copied into each test's activity log either
    # (tests/test_log_mirror.py installs the handler itself).
    monkeypatch.setattr("app.main.install_log_mirror", lambda: None)
    monkeypatch.setattr("app.main.discovery_scheduler_loop", idle_scheduler)


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    """The login limiter is process-global and a success does not clear the
    address's failures, so one test's bad logins must not count in the next."""
    from app import login_limits

    login_limits._login_failures.clear()
    login_limits._user_failures.clear()
    yield
    login_limits._login_failures.clear()
    login_limits._user_failures.clear()


@pytest.fixture(autouse=True)
def _reset_library_cache():
    """app.library_cache holds process-global state (by design - see its
    module docstring notes), but each test gets its own throwaway database
    via `session_factory`. Without resetting between tests, two unrelated
    tests whose accessible root ids happen to coincide (e.g. both use root
    id 1) could otherwise read back a cached response computed against a
    different test's database."""
    from app import background_status, library_cache, want_list
    from app.discovery import similar as discovery_similar

    background_status.clear_cache()
    library_cache._store.clear()
    library_cache._version = 0
    # Keyed by library version (and user id or root ids), all of which
    # repeat from one test's database to the next.
    want_list._matched_against.clear()
    discovery_similar._overlap_cache.clear()
    yield
    # A read may have queued a background re-match against this test's
    # database; let it end before the next test resets the state. The same
    # for a refresh of the Admin page's counts.
    want_list.wait_for_rematches()
    background_status.wait_for_refresh()
    background_status.clear_cache()
    library_cache._store.clear()
    library_cache._version = 0
    want_list._matched_against.clear()
    discovery_similar._overlap_cache.clear()


@pytest.fixture(autouse=True)
def _drain_activity_log():
    """Activity events are written by a background thread (app.activity_log);
    let a test's events land before the next test starts."""
    yield
    from app.activity_log import wait_for_writes

    wait_for_writes(10)


@pytest.fixture()
def session_factory(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    # The same connection setup (foreign keys on, WAL, busy timeout, SQL
    # functions) and session options (no expiry on commit) as app.db, so a
    # foreign-key violation or a stale-object bug fails here as it would in
    # production instead of passing silently.
    event.listen(
        engine, "connect", lambda conn, _record: configure_sqlite_connection(conn)
    )
    factory = make_session_factory(engine)
    # The schema comes from the real upgrade path (create_all, then every
    # migration step and data backfill), so a step that breaks a fresh
    # database fails every test, not just production's first boot.
    with startup_against(factory):
        run_migrations()
    # Admin scan/enrichment tasks run in a background thread and therefore
    # create their own session. Point that session at each test's database.
    monkeypatch.setattr("app.library_tasks.SessionLocal", factory)
    # So do the background jobs that open their own session: the want
    # list's re-match after a library change and the provider-queue jobs.
    # The activity log's writer thread too: each event goes to the database
    # that was current when it was recorded.
    for module in ("app.want_list", "app.want_metadata", "app.catalog", "app.listenbrainz_data", "app.activity_log", "app.deferred", "app.cache_warmup", "app.background_status"):
        monkeypatch.setattr(f"{module}.SessionLocal", factory)
    return factory


@pytest.fixture()
def db_session(session_factory):
    session = session_factory()
    user = User(
        username=DEFAULT_USERNAME, password_hash=hash_password(DEFAULT_PASSWORD), is_admin=True
    )
    session.add(user)
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def raw_client(session_factory, db_session) -> Iterator[TestClient]:
    """An app client with the DB wired to the test database, but not logged
    in - use this to test auth itself. Most tests want `client` instead."""

    def override_get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def client(raw_client) -> TestClient:
    """Logged in as the seeded default admin user - real cookie-based auth,
    exercised end to end rather than bypassed."""
    resp = raw_client.post(
        "/api/auth/login", json={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return raw_client


@contextmanager
def unwritable_audio(path: Path) -> Iterator[None]:
    """Saving tags to the WAV fixture at `path` fails as on a read-only file
    or mount. A chmod would not do it: root (the CI runner) writes a
    read-only file anyway. Other files save as usual."""
    real_save = WAVE.save

    def save(self, *args, **kwargs):
        if Path(self.filename) == Path(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_save(self, *args, **kwargs)

    with patch.object(WAVE, "save", save):
        yield


def wait_until(
    condition: Callable[[], object], timeout: float = 30.0, interval: float = 0.01, what: str = "condition"
):
    """Poll `condition` until it returns something truthy, and return that.
    For waiting on a background thread or task: the deadline is generous
    (a loaded machine running coverage is slow), and a passing test waits
    only as long as the work actually takes. Fails the test at the deadline."""
    deadline = time.monotonic() + timeout
    while True:
        result = condition()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(interval)


def login_as(session_factory, base_client: TestClient, username: str, password: str) -> TestClient:
    """A second, independently-authenticated client sharing the same
    database - for testing cross-user isolation without cookie-jar
    contamination between identities."""
    other = TestClient(base_client.app)
    resp = other.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return other


@pytest.fixture()
def library_dir(tmp_path) -> Path:
    d = tmp_path / "library"
    d.mkdir()
    return d


@contextmanager
def startup_against(factory: sessionmaker) -> Iterator[None]:
    """Point startup (``app.main.seed_default_user``) and every migration step
    at the database behind `factory` instead of the module-level engine."""
    with (
        patch("app.main.SessionLocal", factory),
        patch("app.migrations.engine", factory.kw["bind"]),
        patch("app.migrations.SessionLocal", factory),
    ):
        yield
