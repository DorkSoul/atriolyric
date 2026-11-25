"""The slow whole-library caches are filled in the background
(app.cache_warmup), so no request has to compute them."""

from __future__ import annotations

import logging
import re

import pytest
from sqlalchemy import event

from app import background_status, cache_warmup, label_counts, library_cache
from app.models import LibraryRoot, MediaItem, TrackDislike, User
from app.scanner import scan_library_root
from app.security import hash_password
from tests.conftest import DEFAULT_USERNAME, make_audio_fixture, wait_until

MEDIA_ITEMS = re.compile(r"\bmedia_items\b", re.IGNORECASE)


def _scan_sample_library(db_session, library_dir):
    make_audio_fixture(library_dir / "a1.wav", title="A1", artist="Band", album="LP", genre="Rock", track_number=1)
    make_audio_fixture(library_dir / "a2.wav", title="A2", artist="Band", album="LP", genre="Rock", track_number=2)
    make_audio_fixture(library_dir / "b1.wav", title="B1", artist="Duo", album="EP", genre="Jazz", track_number=1)
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    return root


def _library_queries(db_session):
    """Every statement that reads the tracks table: what a warm cache spares."""
    statements: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if MEDIA_ITEMS.search(statement):
            statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", listener)
    return statements, lambda: event.remove(engine, "before_cursor_execute", listener)


def _cached_keys(name: str = "label_counts") -> set[tuple]:
    return {key for _version, key in list(library_cache._store) if key[0] == name}


def _warm() -> bool:
    return bool(_cached_keys("library_stats"))  # the last step of a pass


@pytest.fixture()
def warmer(monkeypatch):
    """The real warmer thread, settling almost at once; stopped afterwards."""
    monkeypatch.setattr(cache_warmup, "SETTLE_SECONDS", 0.01)
    cache_warmup.start_cache_warmup()
    yield
    cache_warmup.stop_cache_warmup()
    wait_until(lambda: cache_warmup._thread is None, what="the warmer thread to end")


def test_bump_listeners_are_called_until_removed():
    calls: list[int] = []

    def listener() -> None:
        calls.append(library_cache.library_version())

    library_cache.add_bump_listener(listener)
    library_cache.add_bump_listener(listener)  # kept once
    try:
        library_cache.bump_library_version()
        assert calls == [library_cache.library_version()]  # after the bump, once
    finally:
        library_cache.remove_bump_listener(listener)
    library_cache.remove_bump_listener(listener)  # already gone: no error
    library_cache.bump_library_version()
    assert len(calls) == 1


def test_a_warm_pass_answers_the_slow_requests_without_reading_the_library(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)

    assert cache_warmup.warm_caches() is True

    statements, stop = _library_queries(db_session)
    try:
        options = client.get("/api/library/mixes/options").json()
        genres = client.get("/api/library/genres").json()
        albums = client.get("/api/library/albums?sort=alphabetical&direction=asc&full=1").json()
        albums_page = client.get("/api/library/albums?sort=alphabetical&direction=asc&limit=100").json()
        artists = client.get("/api/library/artists?sort=alphabetical&direction=asc&full=1").json()
        artists_page = client.get("/api/library/artists?sort=alphabetical&direction=asc&limit=100").json()
        warmed = list(statements)
        # These two also read per-user figures (playlists, plays, likes)
        # on every request; only their library-wide part is cached.
        nav = client.get("/api/library/stats/nav-counts").json()
        info = client.get("/api/library/stats").json()
    finally:
        stop()
    assert {(o["name"], o["track_count"]) for o in options} == {("Rock", 2), ("Jazz", 1)}
    assert genres == ["Jazz", "Rock"]
    assert [a["album"] for a in albums] == [a["album"] for a in albums_page] == ["EP", "LP"]
    assert [a["artist"] for a in artists] == [a["artist"] for a in artists_page] == ["Band", "Duo"]
    assert warmed == []
    assert (nav["songs"], nav["albums"], nav["artists"]) == (3, 2, 2)
    assert info["totals"]["songs"] == 3
    assert {key[0] for _version, key in library_cache._store} >= {
        "library_nav_counts", "library_stats", "label_counts", "albums", "albums_full", "artists", "artists_full",
    }


def test_library_info_is_computed_once_a_day_not_on_every_request(client, db_session, library_dir, monkeypatch):
    """Its shared numbers are cached per library version and date: a later
    request the same day reuses them, the next day's computes them again."""
    from datetime import timedelta

    from app.routers.library import stats

    _scan_sample_library(db_session, library_dir)
    computed: list[int] = []
    real = stats._shared_stats
    monkeypatch.setattr(stats, "_shared_stats", lambda db, roots: computed.append(1) or real(db, roots))

    assert cache_warmup.warm_caches() is True
    assert client.get("/api/library/stats").status_code == 200
    assert client.get("/api/library/stats").status_code == 200
    assert len(computed) == 1

    tomorrow = stats.utcnow() + timedelta(days=1)
    monkeypatch.setattr(stats, "utcnow", lambda: tomorrow)
    assert client.get("/api/library/stats").status_code == 200
    assert len(computed) == 2


def test_the_wait_for_the_daily_pass_ends_just_after_utc_midnight(monkeypatch):
    from datetime import datetime

    monkeypatch.setattr(cache_warmup, "utcnow", lambda: datetime(2026, 11, 19, 23, 59, 0))
    assert cache_warmup._seconds_until_tomorrow() == 65
    monkeypatch.setattr(cache_warmup, "utcnow", lambda: datetime(2026, 11, 19, 0, 0, 1))
    assert cache_warmup._seconds_until_tomorrow() == 86404


def test_a_warm_pass_fills_the_admin_pages_counts(db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    background_status.clear_cache()

    assert cache_warmup.warm_caches() is True

    assert background_status._cache is not None
    assert background_status._cache[1].tracks == 3


def test_a_warm_pass_covers_each_users_dislikes(client, db_session, library_dir):
    """A user with dislikes has their own mixable counts; one without shares
    the plain ones."""
    _scan_sample_library(db_session, library_dir)
    other = User(username="other", password_hash=hash_password("x" * 12))
    db_session.add(other)
    db_session.flush()
    disliked = db_session.query(MediaItem).filter_by(title="A1").one()
    db_session.add(TrackDislike(user_id=other.id, media_item_id=disliked.id))
    db_session.commit()

    assert cache_warmup.warm_caches() is True

    per_user = {key[3] for key in _cached_keys()}
    assert per_user == {None, other.id}
    mine = db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()
    assert label_counts.label_counts(db_session, mine, "genre", mixable=True)[("genre", "Rock")] == 2
    assert label_counts.label_counts(db_session, other, "genre", mixable=True)[("genre", "Rock")] == 1


def test_a_warm_pass_gives_up_when_the_library_changes_under_it(db_session, library_dir, monkeypatch):
    _scan_sample_library(db_session, library_dir)
    real = cache_warmup.shared_nav_counts
    ran: list[str] = []

    def counting_then_changing(db, roots):
        ran.append("tab counts")
        result = real(db, roots)
        library_cache.bump_library_version()
        return result

    monkeypatch.setattr(cache_warmup, "shared_nav_counts", counting_then_changing)
    monkeypatch.setattr(cache_warmup, "label_counts", lambda *a, **k: ran.append("label counts"))

    assert cache_warmup.warm_caches() is False
    assert ran == ["tab counts"]


def test_a_warm_pass_stops_when_told_to(db_session, library_dir):
    import threading

    _scan_sample_library(db_session, library_dir)
    stop = threading.Event()
    stop.set()
    assert cache_warmup.warm_caches(stop) is False
    assert _cached_keys("library_nav_counts") == set()


def test_the_warmer_counts_at_start_and_again_after_a_library_change(db_session, library_dir, warmer):
    _scan_sample_library(db_session, library_dir)
    wait_until(_warm, what="the first warm pass")
    version = library_cache.library_version()

    library_cache.bump_library_version()
    wait_until(
        lambda: any(v == version + 1 and key[0] == "library_stats" for v, key in list(library_cache._store)),
        what="the warm pass after the change",
    )
    assert len(_cached_keys()) == 2


def test_the_warmer_survives_a_failed_pass(db_session, library_dir, warmer, monkeypatch, caplog):
    _scan_sample_library(db_session, library_dir)
    wait_until(_warm, what="the first warm pass")
    real = cache_warmup.warm_caches
    failures: list[int] = []

    def fail_once(stop=None) -> bool:
        if not failures:
            failures.append(1)
            raise RuntimeError("database is locked")
        return real(stop)

    monkeypatch.setattr(cache_warmup, "warm_caches", fail_once)
    with caplog.at_level(logging.ERROR, logger="app.cache_warmup"):
        library_cache.bump_library_version()
        wait_until(lambda: failures, what="the failing pass")
        wait_until(lambda: "Could not warm" in caplog.text, what="the failure to be logged")
    library_cache.bump_library_version()
    wait_until(_warm, what="the pass after the failure")


def test_the_warmer_runs_again_when_the_day_changes(db_session, library_dir, monkeypatch):
    """With no library change at all, the wait for tomorrow running out
    starts a pass (here it is made to run out at once)."""
    _scan_sample_library(db_session, library_dir)
    passes: list[int] = []
    real = cache_warmup.warm_caches
    monkeypatch.setattr(cache_warmup, "warm_caches", lambda stop=None: passes.append(1) or real(stop))
    monkeypatch.setattr(cache_warmup, "SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(cache_warmup, "_seconds_until_tomorrow", lambda: 0.05)
    cache_warmup.start_cache_warmup()
    try:
        wait_until(lambda: len(passes) >= 3, what="passes with no library change")
    finally:
        cache_warmup.stop_cache_warmup()
        wait_until(lambda: cache_warmup._thread is None, what="the warmer thread to end")


def test_starting_twice_keeps_one_thread_and_stopping_ends_it(monkeypatch):
    monkeypatch.setattr(cache_warmup, "SETTLE_SECONDS", 60)
    cache_warmup.start_cache_warmup()
    first = cache_warmup._thread
    assert first is not None
    cache_warmup.start_cache_warmup()
    assert cache_warmup._thread is first
    cache_warmup.stop_cache_warmup()  # does not wait out the settle time
    wait_until(lambda: cache_warmup._thread is None, timeout=5, what="the warmer thread to end")
    wait_until(lambda: not first.is_alive(), timeout=5, what="the thread to exit")
    library_cache.bump_library_version()  # the listener is gone: nothing restarts
    assert cache_warmup._thread is None
