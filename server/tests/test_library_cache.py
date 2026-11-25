from __future__ import annotations

import re
import threading

from sqlalchemy import event

from app import library_cache
from app.models import LibraryRoot, LibraryTask, MediaItem, PlayHistory, User
from app.scanner import scan_library_root
from tests.conftest import DEFAULT_USERNAME, make_audio_fixture

MEDIA_ITEM_TABLE = re.compile(r"\bmedia_items\b", re.IGNORECASE)


def _scan_sample_library(db_session, library_dir):
    make_audio_fixture(
        library_dir / "a1.wav", title="A1", artist="Band One", album="LP", genre="Rock", track_number=1
    )
    make_audio_fixture(
        library_dir / "a2.wav", title="A2", artist="Band One", album="LP", genre="Rock", track_number=2
    )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    return root


def _count_media_item_queries(db_session):
    """Collects every SQL statement executed against the shared engine that
    actually queries the media_items table - as opposed to, say, favorite or
    dislike rows filtered by a media_item_id column, whose table name isn't
    "media_items" itself."""
    statements: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if MEDIA_ITEM_TABLE.search(statement):
            statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", listener)
    return statements, lambda: event.remove(engine, "before_cursor_execute", listener)


def test_get_or_compute_reuses_the_cached_value(db_session):
    calls = []

    def compute():
        calls.append(1)
        return {"n": len(calls)}

    first = library_cache.get_or_compute(("some-key",), compute)
    second = library_cache.get_or_compute(("some-key",), compute)

    assert first == second == {"n": 1}
    assert len(calls) == 1


def test_bump_library_version_clears_previously_cached_values(db_session):
    library_cache.get_or_compute(("stale-key",), lambda: "stale")
    library_cache.bump_library_version()

    calls = []

    def compute():
        calls.append(1)
        return "fresh"

    result = library_cache.get_or_compute(("stale-key",), compute)

    assert result == "fresh"
    assert calls == [1]


def test_second_identical_albums_request_is_served_from_cache(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    statements, stop_listening = _count_media_item_queries(db_session)
    try:
        first = client.get("/api/library/albums")
        assert first.status_code == 200
        count_after_first = len(statements)
        assert count_after_first > 0

        second = client.get("/api/library/albums")
        assert second.status_code == 200
        assert second.json() == first.json()
        assert len(statements) == count_after_first
    finally:
        stop_listening()


def test_second_identical_artists_request_is_served_from_cache(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    statements, stop_listening = _count_media_item_queries(db_session)
    try:
        first = client.get("/api/library/artists")
        assert first.status_code == 200
        count_after_first = len(statements)
        assert count_after_first > 0

        second = client.get("/api/library/artists")
        assert second.status_code == 200
        assert second.json() == first.json()
        assert len(statements) == count_after_first
    finally:
        stop_listening()


def test_second_identical_tracks_request_is_served_from_cache(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    statements, stop_listening = _count_media_item_queries(db_session)
    try:
        first = client.get("/api/library/tracks")
        assert first.status_code == 200
        count_after_first = len(statements)
        assert count_after_first > 0

        second = client.get("/api/library/tracks")
        assert second.status_code == 200
        assert second.json() == first.json()
        assert len(statements) == count_after_first
    finally:
        stop_listening()


def test_tracks_cache_does_not_leak_favorite_state_between_users(
    client, db_session, library_dir, session_factory
):
    from app.security import hash_password
    from tests.conftest import DEFAULT_PASSWORD, login_as

    _scan_sample_library(db_session, library_dir)
    other = User(username="other", password_hash=hash_password(DEFAULT_PASSWORD), is_admin=True)
    db_session.add(other)
    db_session.commit()

    track = db_session.query(MediaItem).first()

    # Warm the cache as the other user first (no favorites at all yet).
    other_client = login_as(session_factory, client, "other", DEFAULT_PASSWORD)
    baseline = other_client.get("/api/library/tracks").json()
    assert all(not t["is_favorite"] for t in baseline)

    resp = client.put(f"/api/media/{track.id}/preference", json={"state": "liked"})
    assert resp.status_code == 200

    mine = client.get("/api/library/tracks").json()
    assert next(t for t in mine if t["id"] == track.id)["is_favorite"] is True

    still_others = other_client.get("/api/library/tracks").json()
    assert next(t for t in still_others if t["id"] == track.id)["is_favorite"] is False


def test_tag_edit_invalidates_the_albums_cache(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    before = client.get("/api/library/albums").json()
    assert {a["album"] for a in before} == {"LP"}

    for track in db_session.query(MediaItem).filter_by(album="LP").all():
        resp = client.patch(f"/api/admin/media/{track.id}/tags", json={"album": "Renamed"})
        assert resp.status_code == 200

    after = client.get("/api/library/albums").json()
    assert {a["album"] for a in after} == {"Renamed"}


def test_scan_invalidates_the_albums_cache(client, db_session, library_dir, session_factory, monkeypatch):
    from app import library_tasks

    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    root = _scan_sample_library(db_session, library_dir)

    before = client.get("/api/library/albums").json()
    assert {a["album"] for a in before} == {"LP"}

    make_audio_fixture(
        library_dir / "b1.wav", title="B1", artist="Band Two", album="New Album", genre="Jazz"
    )
    task = LibraryTask(library_root_id=root.id, kind="scan", status="running")
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    library_tasks._run_scan_task(root.id, task.id)

    after = client.get("/api/library/albums").json()
    assert {a["album"] for a in after} == {"LP", "New Album"}


def test_noop_rescan_does_not_bump_the_library_version(
    client, db_session, library_dir, session_factory, monkeypatch
):
    from app import library_tasks

    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    root = _scan_sample_library(db_session, library_dir)

    client.get("/api/library/albums")
    version_before = library_cache._version

    task = LibraryTask(library_root_id=root.id, kind="scan", status="running")
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    library_tasks._run_scan_task(root.id, task.id)

    assert library_cache._version == version_before


def test_last_played_sort_is_never_cached(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    user = db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()
    lp_track = db_session.query(MediaItem).filter_by(album="LP").first()

    before = client.get("/api/library/albums", params={"sort": "last_played"}).json()
    assert before[0]["album"] == "LP"

    db_session.add(PlayHistory(user_id=user.id, media_item_id=lp_track.id))
    db_session.commit()

    after = client.get("/api/library/albums", params={"sort": "last_played"}).json()
    assert after[0]["album"] == "LP"


def test_most_played_artist_sort_is_never_cached(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    user = db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()
    track = db_session.query(MediaItem).filter_by(artist="Band One").first()

    client.get("/api/library/artists", params={"sort": "most_played"})

    db_session.add(PlayHistory(user_id=user.id, media_item_id=track.id))
    db_session.commit()

    after = client.get("/api/library/artists", params={"sort": "most_played"}).json()
    assert after[0]["artist"] == "Band One"


def test_full_list_cache_invalidation_on_library_version_bump(client, db_session, library_dir):

    _scan_sample_library(db_session, library_dir)

    first = client.get("/api/library/albums", params={"full": 1}).json()
    assert {a["album"] for a in first} == {"LP"}

    # Second identical full request is served from cache.
    second = client.get("/api/library/albums", params={"full": 1}).json()
    assert second == first

    # A library change bumps the version; the full-list entry must be
    # recomputed, not served stale.
    for track in db_session.query(MediaItem).filter_by(album="LP").all():
        resp = client.patch(f"/api/admin/media/{track.id}/tags", json={"album": "Renamed"})
        assert resp.status_code == 200

    after = client.get("/api/library/albums", params={"full": 1}).json()
    assert {a["album"] for a in after} == {"Renamed"}


# --- bounded, single-flight, invalidated after commit ----------------------


def test_searches_are_never_cached_and_the_cache_stays_bounded(client, db_session, library_dir):
    _scan_sample_library(db_session, library_dir)
    for i in range(100):
        for listing in ("tracks", "albums", "artists"):
            assert client.get(f"/api/library/{listing}", params={"q": f"band {i}"}).status_code == 200
    assert library_cache._store == {}
    for offset in range(100):
        client.get("/api/library/tracks", params={"offset": offset, "limit": 1})
    assert len(library_cache._store) == library_cache.MAX_ENTRIES


def test_the_least_recently_used_entry_is_evicted_first(monkeypatch):
    monkeypatch.setattr(library_cache, "MAX_ENTRIES", 3)
    for key in ("a", "b", "c"):
        library_cache.get_or_compute((key,), lambda key=key: key)
    library_cache.get_or_compute(("a",), lambda: "recomputed")  # a is now the most recent
    library_cache.get_or_compute(("d",), lambda: "d")

    assert [key for (_version, key) in library_cache._store] == [("c",), ("a",), ("d",)]
    assert library_cache.get_or_compute(("a",), lambda: "recomputed") == "a"


def test_concurrent_requests_for_one_key_compute_it_once():
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def slow():
        calls.append("slow")
        started.set()
        assert release.wait(5)
        return "value"

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(library_cache.get_or_compute(("k",), slow)))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    assert started.wait(5)
    # Another key is not held up by the one being computed.
    assert library_cache.get_or_compute(("other",), lambda: "quick") == "quick"
    release.set()
    for thread in threads:
        thread.join(5)
    assert results == ["value"] * 4
    assert calls == ["slow"]


def test_a_value_computed_across_a_bump_is_not_stored():
    def racing():
        library_cache.bump_library_version()  # the library changed meanwhile
        return "stale"

    assert library_cache.get_or_compute(("k",), racing) == "stale"
    assert library_cache._store == {}
    assert library_cache.get_or_compute(("k",), lambda: "fresh") == "fresh"


def test_an_enrichment_run_refreshes_the_cached_album_list(
    client, db_session, library_dir, session_factory
):
    from unittest.mock import patch

    from app import library_tasks
    from app.models import Album

    root = _scan_sample_library(db_session, library_dir)
    assert client.get("/api/library/albums").json()[0]["primary_type"] is None

    def enrich(db, item, **_kwargs):
        db.get(Album, item.album_id).primary_type = "EP"
        return {"matched": True}

    task = LibraryTask(library_root_id=root.id, kind="enrich", status="running")
    db_session.add(task)
    db_session.commit()
    with patch("app.library_tasks.enrich_media_item", side_effect=enrich):
        library_tasks._run_enrichment_task(root.id, task.id)

    assert client.get("/api/library/albums").json()[0]["primary_type"] == "EP"


def test_a_listenbrainz_tags_run_invalidates_the_cache(db_session, library_dir):
    from unittest.mock import patch

    from app import library_tasks

    root = _scan_sample_library(db_session, library_dir)
    task = LibraryTask(library_root_id=root.id, kind="lb_tags", status="running")
    db_session.add(task)
    db_session.commit()
    version = library_cache._version
    with patch("app.library_tasks.listenbrainz.artist_tags", return_value={}):
        library_tasks._run_listenbrainz_tags_task(root.id, task.id)
    assert library_cache._version > version


def test_a_delete_is_never_served_from_the_cache_once_committed(
    client, db_session, library_dir, session_factory
):
    from app.cleanup import delete_media_records

    _scan_sample_library(db_session, library_dir)
    gone = db_session.query(MediaItem).filter_by(title="A1").one().id
    writer = session_factory()
    try:
        delete_media_records(writer, [gone])
        # A request between the delete and its commit still sees the row,
        # and caches what it saw...
        assert gone in {t["id"] for t in client.get("/api/library/tracks").json()}
        writer.commit()
    finally:
        writer.close()
    # ...but the commit drops that entry.
    assert gone not in {t["id"] for t in client.get("/api/library/tracks").json()}


def test_a_rolled_back_delete_does_not_invalidate(db_session, library_dir, session_factory):
    from app.cleanup import delete_media_records

    _scan_sample_library(db_session, library_dir)
    item_id = db_session.query(MediaItem).filter_by(title="A1").one().id
    version = library_cache._version
    writer = session_factory()
    try:
        delete_media_records(writer, [item_id])
        writer.rollback()
        writer.commit()  # a later commit of the same session has nothing to report
    finally:
        writer.close()
    assert library_cache._version == version


def test_a_savepoint_does_not_bump_early_nor_drop_the_pending_bump(db_session):
    """The after-commit/after-rollback listeners also fire for SAVEPOINTs;
    only the outer transaction's commit may bump (or its rollback forget)."""
    from sqlalchemy import text

    version = library_cache.library_version()
    library_cache.bump_library_version_after_commit(db_session)
    with db_session.begin_nested():
        db_session.execute(text("SELECT 1"))
    assert library_cache.library_version() == version  # released, not committed
    try:
        with db_session.begin_nested():
            raise ValueError
    except ValueError:
        pass
    assert library_cache.library_version() == version
    db_session.commit()
    assert library_cache.library_version() == version + 1  # the rolled-back savepoint kept it


def test_only_a_scan_that_redoes_known_tracks_bumps_the_art_epoch(
    client, db_session, library_dir, session_factory, monkeypatch
):
    """New files have no cached art anywhere, so adding music leaves the
    epoch (and every open app's art cache) alone; a full rescan moves it."""
    from app import library_tasks
    from app.art_epoch import art_epoch

    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    root = _scan_sample_library(db_session, library_dir)

    def run(mode: str) -> None:
        task = LibraryTask(library_root_id=root.id, kind="scan", status="running")
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        library_tasks._run_scan_task(root.id, task.id, mode)
        db_session.expire_all()

    make_audio_fixture(
        library_dir / "b1.wav", title="B1", artist="Band Two", album="New Album", genre="Jazz"
    )
    run("normal")
    assert art_epoch(db_session) == 0
    run("metadata")
    assert art_epoch(db_session) == 0
    run("full")
    assert art_epoch(db_session) > 0
    assert client.get("/api/version").json()["art_epoch"] == art_epoch(db_session)
