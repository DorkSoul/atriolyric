from __future__ import annotations

from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

from app.metadata_providers.musicbrainz import LookupResult
from app.models import (
    Favorite,
    IgnoredLibraryFolder,
    LibraryRoot,
    LibraryTask,
    MediaItem,
    MediaKind,
    Playlist,
    PlaylistItem,
    PlaySourceHistory,
    User,
    UserIntegration,
    utcnow,
)
from app.security import hash_password
from tests.conftest import login_as, wait_until


def _settled_last_task(client, root_id: int) -> dict | None:
    """The root's last task once it is no longer running, else None."""
    task = next(
        item["last_task"] for item in client.get("/api/admin/roots").json() if item["id"] == root_id
    )
    return task if task and task["status"] not in ("running", "stopping") else None

def _run_isrc_lookup(root_id: int, task_id: int) -> None:
    """The "mb_isrc" task's worker (ISRC lookups only)."""
    from app import library_tasks

    library_tasks.TASK_WORKERS["mb_isrc"](root_id, task_id, library_tasks.TaskOptions())


def _make_plain_user(db_session, username="plain", password="plainpass") -> User:
    user = User(username=username, password_hash=hash_password(password))
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_batch_metadata_workers_resolve_isrc_names_and_metadata(db_session, session_factory, monkeypatch):
    from app import library_tasks

    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    root = LibraryRoot(path="/batch", display_name="Batch")
    db_session.add(root)
    db_session.commit()
    isrc_item = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="isrc.mp3", title="I", artist="A", musicbrainz_isrc="GBABC1234567")
    name_item = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="name.mp3", title="N", artist="B", album="Album")
    metadata_item = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="meta.mp3", title="M", artist="C", musicbrainz_recording_mbid="rec-meta")
    lookup_item = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="lookup.mp3", title="L", artist="D")
    db_session.add_all([isrc_item, name_item, metadata_item, lookup_item])
    db_session.commit()

    isrc_task = LibraryTask(library_root_id=root.id, kind="mb_isrc", status="running")
    db_session.add(isrc_task); db_session.commit()
    monkeypatch.setattr(library_tasks.musicbrainz, "lookup_recording_by_isrc", lambda _isrc: LookupResult(value={"recording_mbid": "rec-isrc", "artist_mbid": "art-isrc"}))
    _run_isrc_lookup(root.id, isrc_task.id)
    db_session.refresh(isrc_item)
    assert isrc_item.musicbrainz_recording_mbid == "rec-isrc"

    mbid_task = LibraryTask(library_root_id=root.id, kind="mb_mbid", status="running")
    db_session.add(mbid_task); db_session.commit()
    monkeypatch.setattr(library_tasks.musicbrainz, "search_recording_result", lambda _artist, _title: LookupResult(value={"recording_mbid": "rec-name", "artist_mbid": "art-name"}))
    library_tasks._run_mbid_lookup_task(root.id, mbid_task.id)
    db_session.refresh(name_item)
    assert name_item.musicbrainz_recording_mbid == "rec-name"


def test_metadata_lookup_worker_skips_tracks_in_ignored_folders(
    db_session, session_factory, monkeypatch
):
    from app import library_tasks

    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    root = LibraryRoot(path="/ignored-metadata", display_name="Ignored metadata")
    ignored = MediaItem(
        library_root=root,
        kind=MediaKind.track,
        relative_path="Custom/no-provider-data.mp3",
        title="Private Track",
        artist="Local Artist",
    )
    included = MediaItem(
        library_root=root,
        kind=MediaKind.track,
        relative_path="Album/known-track.mp3",
        title="Known Track",
        artist="Known Artist",
    )
    db_session.add_all([root, ignored, included])
    db_session.commit()
    included_path = included.relative_path
    db_session.add(IgnoredLibraryFolder(library_root_id=root.id, relative_path="Custom"))
    task = LibraryTask(library_root_id=root.id, kind="mb_mbid", status="running")
    db_session.add(task)
    db_session.commit()

    looked_up_paths = []

    def lookup(item):
        looked_up_paths.append(item.relative_path)
        return LookupResult(value={"recording_mbid": "known-mbid"})

    lookup_mock = Mock(side_effect=lookup)
    monkeypatch.setattr(library_tasks, "_lookup_recording", lookup_mock)
    library_tasks._run_mbid_lookup_task(root.id, task.id)

    db_session.refresh(task)
    db_session.refresh(ignored)
    db_session.refresh(included)
    assert task.total == 1
    assert task.processed == 1
    lookup_mock.assert_called_once()
    assert looked_up_paths == [included_path]
    assert ignored.musicbrainz_lookup_status is None
    assert included.musicbrainz_recording_mbid == "known-mbid"


def test_batch_workers_handle_empty_stop_and_provider_errors(db_session, session_factory, monkeypatch):
    from app import library_tasks

    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    root = LibraryRoot(path="/batch-edges", display_name="Batch edges")
    item = MediaItem(library_root=root, kind=MediaKind.track, relative_path="x.mp3", title="X", artist="X", musicbrainz_isrc="GBX", musicbrainz_recording_mbid="rec")
    db_session.add_all([root, item]); db_session.commit()
    for worker, args in [
        (_run_isrc_lookup, (999, 999)),
    ]:
        worker(*args)

    for kind, worker, provider, args in [
        ("mb_isrc", _run_isrc_lookup, "lookup_recording_by_isrc", (root.id,)),
    ]:
        task = LibraryTask(library_root_id=root.id, kind=kind, status="running")
        db_session.add(task); db_session.commit()
        monkeypatch.setattr(library_tasks.musicbrainz, provider, lambda *_args: LookupResult(error="offline"))
        worker(*args, task.id)
        db_session.refresh(task)
        assert task.status in {"completed", "failed"}


def test_start_routes_new_metadata_tasks_and_linked_lookup(client, db_session, library_dir, monkeypatch):
    from app import library_tasks
    from tests.test_enrichment_endpoints import _scan_one_track

    _scan_one_track(db_session, library_dir)
    root = db_session.query(LibraryRoot).filter(LibraryRoot.path == str(library_dir)).one()
    monkeypatch.setattr(library_tasks, "spawn", lambda *_args: None)
    for mode, expected in [
        ("mbids", "mb_mbid"),
        ("musicbrainz_missing", "enrich"),
        ("isrc_mbid", "mb_isrc"),
    ]:
        body = {"mode": mode}
        response = client.post(f"/api/admin/roots/{root.id}/enrich", json=body)
        assert response.status_code == 202
        assert response.json()["kind"] == expected
        db_session.query(LibraryTask).filter(LibraryTask.id == response.json()["id"]).update({"status": "completed"}); db_session.commit()
    invalid = client.post(
        f"/api/admin/roots/{root.id}/enrich", json={"mode": "musicbrainz_local"}
    )
    assert invalid.status_code == 422


def test_batch_workers_cover_retry_and_graceful_stop_paths(db_session, session_factory, monkeypatch):
    from app import library_tasks

    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    root = LibraryRoot(path="/batch-retry", display_name="Batch retry")
    db_session.add(root)
    db_session.commit()

    isrc = MediaItem(library_root=root, kind=MediaKind.track, relative_path="i", title="I", artist="A", musicbrainz_isrc="GBI")
    metadata = MediaItem(library_root=root, kind=MediaKind.track, relative_path="m", title="M", artist="A", musicbrainz_recording_mbid="rec-m")
    lookup = MediaItem(library_root=root, kind=MediaKind.track, relative_path="l", title="L", artist="A")
    db_session.add_all([isrc, metadata, lookup]); db_session.commit()

    isrc_task = LibraryTask(library_root=root, kind="mb_isrc", status="running")
    db_session.add(isrc_task); db_session.commit()
    monkeypatch.setattr(library_tasks.musicbrainz, "lookup_recording_by_isrc", lambda _value: LookupResult(error="offline"))
    _run_isrc_lookup(root.id, isrc_task.id)
    db_session.refresh(isrc)
    assert isrc.musicbrainz_lookup_status == "retry"



def test_batch_workers_cover_remaining_outcomes(db_session, session_factory, monkeypatch):
    from app import library_tasks

    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    root = LibraryRoot(path="/batch-outcomes", display_name="Batch outcomes")
    db_session.add(root); db_session.commit()
    isrc = MediaItem(library_root=root, kind=MediaKind.track, relative_path="i", title="I", artist="A", musicbrainz_isrc="GBI")
    meta = MediaItem(library_root=root, kind=MediaKind.track, relative_path="m", title="M", artist="A", musicbrainz_recording_mbid="rec")
    lookup = MediaItem(library_root=root, kind=MediaKind.track, relative_path="l", title="L", artist="A")
    db_session.add_all([isrc, meta, lookup]); db_session.commit()

    task = LibraryTask(library_root=root, kind="mb_isrc", status="running"); db_session.add(task); db_session.commit()
    monkeypatch.setattr(library_tasks.musicbrainz, "lookup_recording_by_isrc", lambda _value: LookupResult())
    monkeypatch.setattr(library_tasks, "_task_stop_requested", lambda *_value: True)
    _run_isrc_lookup(root.id, task.id)
    db_session.refresh(task); assert task.status == "stopped"

    task = LibraryTask(library_root=root, kind="mb_isrc", status="running"); db_session.add(task); db_session.commit()
    monkeypatch.setattr(library_tasks.musicbrainz, "lookup_recording_by_isrc", lambda _value: (_ for _ in ()).throw(RuntimeError("boom")))
    _run_isrc_lookup(root.id, task.id)
    db_session.refresh(task); assert task.status == "failed"


def test_non_admin_gets_403_on_admin_routes(session_factory, client, db_session):
    _make_plain_user(db_session)
    plain_client = login_as(session_factory, client, "plain", "plainpass")

    resp = plain_client.get("/api/admin/users")
    assert resp.status_code == 403


def test_admin_can_create_and_list_users(client):
    resp = client.post(
        "/api/admin/users",
        json={"username": "newkid", "password": "hunter22", "is_admin": False},
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "newkid"

    users = client.get("/api/admin/users").json()
    usernames = {u["username"] for u in users}
    assert {"dork_soul", "newkid"} <= usernames


def test_admin_cannot_create_duplicate_username(client):
    client.post("/api/admin/users", json={"username": "dupe", "password": "password-x"})
    resp = client.post(
        "/api/admin/users", json={"username": "dupe", "password": "password-y"}
    )
    assert resp.status_code == 409


def test_admin_cannot_delete_self(client):
    me = client.get("/api/auth/me").json()
    resp = client.delete(f"/api/admin/users/{me['id']}")
    assert resp.status_code == 400


def test_admin_can_delete_other_user(client, db_session):
    other = _make_plain_user(db_session, username="throwaway")
    resp = client.delete(f"/api/admin/users/{other.id}")
    assert resp.status_code == 200
    assert other.username not in {
        u["username"] for u in client.get("/api/admin/users").json()
    }


def test_delete_user_removes_dependent_rows(client, db_session):
    other = _make_plain_user(db_session, username="with-data")
    playlist = Playlist(user_id=other.id, name="Mix")
    db_session.add_all(
        [
            playlist,
            UserIntegration(
                user_id=other.id, provider="listenbrainz", credential="secret"
            ),
            PlaySourceHistory(
                user_id=other.id, kind="library", label="Songs", payload_json={}
            ),
        ]
    )
    db_session.commit()

    assert client.delete(f"/api/admin/users/{other.id}").status_code == 200
    assert db_session.query(Playlist).filter(Playlist.user_id == other.id).count() == 0
    assert (
        db_session.query(UserIntegration)
        .filter(UserIntegration.user_id == other.id)
        .count()
        == 0
    )
    # Otherwise a later user who happens to reuse this deleted user's id (SQLite
    # reuses row ids by default) would inherit stale "Recently played" cards.
    assert (
        db_session.query(PlaySourceHistory)
        .filter(PlaySourceHistory.user_id == other.id)
        .count()
        == 0
    )


# --- a library is removed a batch at a time -----------------------------------


def _root_with_tracks(db_session, path: str, count: int, artist: str):
    from app.models import Album, Artist, TrackArtist

    root = LibraryRoot(path=path, display_name=path)
    album = Album(owner=artist, title=f"{artist} LP", owner_key=artist.lower(), title_key="lp")
    performer = Artist(name=artist, normalized_name=artist.lower())
    db_session.add_all([root, album, performer])
    db_session.commit()
    items = [
        MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"{n}.mp3",
            title=str(n),
            album_id=album.id,
        )
        for n in range(count)
    ]
    db_session.add_all(items)
    db_session.flush()
    db_session.add_all(
        TrackArtist(media_item_id=item.id, artist_id=performer.id, role="main", position=0)
        for item in items
    )
    db_session.commit()
    return root.id, [item.id for item in items], album.id, performer.id


def _track_count(db_session, root_id: int) -> int:
    db_session.expire_all()
    return db_session.query(MediaItem).filter(MediaItem.library_root_id == root_id).count()


def test_delete_root_removes_its_tracks_in_batches_with_a_commit_each(
    client, db_session, monkeypatch
):
    from sqlalchemy import event

    from app import cleanup
    from app.models import Album, Artist, TrackArtist

    monkeypatch.setattr(cleanup, "MEDIA_DELETE_BATCH_SIZE", 3)
    root_id, media_ids, album_id, artist_id = _root_with_tracks(db_session, "/going", 8, "Gone")
    kept_root, kept_ids, kept_album, kept_artist = _root_with_tracks(db_session, "/staying", 2, "Kept")
    user_id = db_session.query(User).one().id
    db_session.add_all(Favorite(user_id=user_id, media_item_id=i) for i in media_ids + kept_ids)
    db_session.commit()

    batches: list[int] = []
    commits: list[int] = []
    real = cleanup.delete_media_records

    def recording(db, ids):
        ids = list(ids)
        batches.append(len(ids))
        return real(db, ids)

    def count_commit(_conn) -> None:
        commits.append(len(batches))

    monkeypatch.setattr(cleanup, "delete_media_records", recording)
    engine = db_session.get_bind()
    event.listen(engine, "commit", count_commit)
    try:
        response = client.delete(f"/api/admin/roots/{root_id}")
    finally:
        event.remove(engine, "commit", count_commit)
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert batches == [3, 3, 2]
    # Each batch was committed before the next began, so other writers get in.
    assert {1, 2, 3} <= set(commits)
    db_session.expire_all()
    assert db_session.get(LibraryRoot, root_id) is None
    assert _track_count(db_session, root_id) == 0
    assert db_session.query(Favorite).count() == 2
    assert db_session.query(TrackArtist).count() == 2
    # The artist and album only that library had go with it...
    assert db_session.get(Artist, artist_id) is None
    assert db_session.get(Album, album_id) is None
    # ...and nothing of the other library is touched.
    assert _track_count(db_session, kept_root) == 2
    assert db_session.get(Artist, kept_artist) is not None
    assert db_session.get(Album, kept_album) is not None


def test_a_root_delete_that_fails_midway_leaves_the_rest_with_its_root(
    client, db_session, monkeypatch
):
    from app import cleanup

    monkeypatch.setattr(cleanup, "MEDIA_DELETE_BATCH_SIZE", 3)
    root_id, media_ids, _album_id, _artist_id = _root_with_tracks(db_session, "/going", 8, "Gone")
    real = cleanup.delete_media_records
    calls: list[int] = []

    def fails_on_the_second_batch(db, ids):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(db, ids)

    monkeypatch.setattr(cleanup, "delete_media_records", fails_on_the_second_batch)
    with pytest.raises(RuntimeError, match="disk full"):
        client.delete(f"/api/admin/roots/{root_id}")
    db_session.expire_all()
    assert db_session.get(LibraryRoot, root_id) is not None
    remaining = sorted(
        item.id for item in db_session.query(MediaItem).filter(MediaItem.library_root_id == root_id)
    )
    assert remaining == media_ids[3:]

    # Asking again finishes the job.
    monkeypatch.setattr(cleanup, "delete_media_records", real)
    assert client.delete(f"/api/admin/roots/{root_id}").status_code == 200
    db_session.expire_all()
    assert db_session.get(LibraryRoot, root_id) is None


def test_a_task_started_while_a_root_is_being_removed_stops_the_removal(
    client, db_session, session_factory, monkeypatch
):
    from app import cleanup

    monkeypatch.setattr(cleanup, "MEDIA_DELETE_BATCH_SIZE", 3)
    root_id, _media_ids, _album_id, _artist_id = _root_with_tracks(db_session, "/going", 4, "Gone")
    real = cleanup.delete_media_records
    started: list[int] = []

    def a_scan_starts_meanwhile(db, ids):
        # Between two batches, when the first one's commit has let go of the
        # write lock.
        started.append(1)
        if len(started) == 2:
            with session_factory() as other:
                other.add(LibraryTask(library_root_id=root_id, kind="scan", status="running"))
                other.commit()
        real(db, ids)

    monkeypatch.setattr(cleanup, "delete_media_records", a_scan_starts_meanwhile)
    response = client.delete(f"/api/admin/roots/{root_id}")
    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.get(LibraryRoot, root_id) is not None


def test_delete_root_removes_media_and_cross_user_dependents(
    client, db_session, tmp_path
):
    source_file = tmp_path / "gone.mp3"
    source_file.write_bytes(b"original music file")
    root = LibraryRoot(path=str(tmp_path), display_name="Disposable")
    db_session.add(root)
    db_session.commit()
    media = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path="gone.mp3",
        title="Gone",
    )
    playlist = Playlist(user_id=db_session.query(User).one().id, name="Mix")
    db_session.add_all([media, playlist])
    db_session.commit()
    db_session.add_all(
        [
            PlaylistItem(playlist_id=playlist.id, media_item_id=media.id, position=0),
            Favorite(user_id=playlist.user_id, media_item_id=media.id),
        ]
    )
    db_session.commit()
    root_id = root.id
    media_id = media.id

    assert client.delete(f"/api/admin/roots/{root_id}").status_code == 200
    db_session.expire_all()
    assert db_session.get(MediaItem, media_id) is None
    assert (
        db_session.query(PlaylistItem)
        .filter(PlaylistItem.media_item_id == media_id)
        .count()
        == 0
    )
    assert (
        db_session.query(Favorite).filter(Favorite.media_item_id == media_id).count()
        == 0
    )
    assert source_file.read_bytes() == b"original music file"


def test_admin_create_root_and_grant_revoke_access(client, db_session):
    plain = _make_plain_user(db_session)

    root_resp = client.post(
        "/api/admin/roots",
        json={
            "path": "/tmp/nonexistent",
            "display_name": "Private Stash",
            "is_shared": False,
        },
    )
    assert root_resp.status_code == 200
    root_id = root_resp.json()["id"]

    grant = client.post(f"/api/admin/roots/{root_id}/access/{plain.id}")
    assert grant.status_code == 200
    user_row = next(
        u for u in client.get("/api/admin/users").json() if u["id"] == plain.id
    )
    assert root_id in user_row["accessible_root_ids"]

    revoke = client.delete(f"/api/admin/roots/{root_id}/access/{plain.id}")
    assert revoke.status_code == 200
    user_row = next(
        u for u in client.get("/api/admin/users").json() if u["id"] == plain.id
    )
    assert root_id not in user_row["accessible_root_ids"]


def test_scan_of_missing_path_records_error(client, db_session, session_factory, monkeypatch):
    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    root = LibraryRoot(path="/tmp/this-path-does-not-exist-12345", display_name="Ghost")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)

    resp = client.post(f"/api/admin/roots/{root.id}/scan")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "running"

    # The scan runs in a background thread and branch coverage instrumentation
    # makes the first database refresh materially slower than a normal test
    # run. Give the task a bounded but realistic amount of time to finish.
    def finished_root() -> dict | None:
        roots = client.get("/api/admin/roots").json()
        found = next(r for r in roots if r["id"] == root.id)
        return found if found["last_task"]["status"] != "running" else None

    ghost = wait_until(finished_root, what="the scan task")
    assert ghost["last_task"]["status"] == "failed"
    assert ghost["last_task"]["error"] is not None
    assert ghost["last_scan_error"] is not None
    assert ghost["last_scanned_at"] is not None


def test_running_scan_reports_persistent_progress(client, db_session, session_factory, monkeypatch):
    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    root = LibraryRoot(path="/fake", display_name="Progress")
    db_session.add(root)
    db_session.commit()
    started = Event()
    release = Event()

    def slow_scan(_db, _root, report, _mode="normal", should_stop=None):
        report(2, 10)
        started.set()
        assert release.wait(1)
        report(10, 10)
        return {"added": 2, "updated": 0, "removed": 0}

    with patch("app.library_tasks.scan_library_root", side_effect=slow_scan):
        resp = client.post(f"/api/admin/roots/{root.id}/scan")
        assert resp.status_code == 202
        assert started.wait(1)
        progress_root = next(
            item for item in client.get("/api/admin/roots").json() if item["id"] == root.id
        )
        task = progress_root["active_task"]
        assert task["kind"] == "scan"
        assert task["processed"] == 2
        assert task["total"] == 10
        assert task["eta_seconds"] is not None
        release.set()

    completed = wait_until(lambda: _settled_last_task(client, root.id), what="the scan task")
    assert completed["status"] == "completed"


def test_stop_task_marks_running_task_as_stopping(client, db_session):
    from app.library_tasks import _task_stop_events

    root = LibraryRoot(path="/fake", display_name="Stop me")
    task = LibraryTask(library_root=root, kind="enrich", status="running")
    db_session.add_all([root, task])
    db_session.commit()
    _task_stop_events[task.id] = Event()
    try:
        response = client.post(f"/api/admin/roots/{root.id}/tasks/{task.id}/stop")
        assert response.status_code == 200
        assert response.json()["status"] == "stopping"
        assert _task_stop_events[task.id].is_set()
        active = next(
            item for item in client.get("/api/admin/roots").json() if item["id"] == root.id
        )
        assert active["active_task"]["status"] == "stopping"

        # The endpoint is idempotent while the worker is finishing its current item.
        again = client.post(f"/api/admin/roots/{root.id}/tasks/{task.id}/stop")
        assert again.status_code == 200
        assert again.json()["status"] == "stopping"
    finally:
        _task_stop_events.pop(task.id, None)

    no_event_task = LibraryTask(library_root_id=root.id, kind="enrich", status="running")
    db_session.add(no_event_task)
    db_session.commit()
    response = client.post(f"/api/admin/roots/{root.id}/tasks/{no_event_task.id}/stop")
    assert response.status_code == 200
    assert response.json()["status"] == "stopping"


def test_stop_task_rejects_unknown_or_mismatched_task(client, db_session):
    root = LibraryRoot(path="/fake", display_name="Stop errors")
    other_root = LibraryRoot(path="/other", display_name="Other")
    task = LibraryTask(library_root=root, kind="enrich", status="running")
    db_session.add_all([root, other_root, task])
    db_session.commit()

    missing = client.post(f"/api/admin/roots/{root.id}/tasks/99999/stop")
    assert missing.status_code == 404
    mismatched = client.post(
        f"/api/admin/roots/{other_root.id}/tasks/{task.id}/stop"
    )
    assert mismatched.status_code == 404


def test_enrichment_worker_stops_after_current_item(
    db_session, session_factory, monkeypatch
):
    from app.library_tasks import _run_enrichment_task

    root = LibraryRoot(path="/fake", display_name="Stop enrichment")
    item = MediaItem(
        library_root=root,
        relative_path="song.mp3",
        title="Song",
        kind=MediaKind.track,
    )
    task = LibraryTask(library_root=root, kind="enrich", status="stopping")
    db_session.add_all([root, item, task])
    db_session.commit()
    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    enrich = Mock(return_value={"matched": True})
    monkeypatch.setattr("app.library_tasks.enrich_media_item", enrich)

    _run_enrichment_task(root.id, task.id)

    db_session.expire_all()
    stopped = db_session.get(LibraryTask, task.id)
    assert stopped.status == "stopped"
    assert stopped.processed == 1
    enrich.assert_called_once()


def test_enrichment_worker_honors_stop_event(
    db_session, session_factory, monkeypatch
):
    from app.library_tasks import _run_enrichment_task, _task_stop_events

    root = LibraryRoot(path="/fake", display_name="Stop event")
    item = MediaItem(
        library_root=root,
        relative_path="song.mp3",
        title="Song",
        kind=MediaKind.track,
    )
    task = LibraryTask(library_root=root, kind="enrich", status="running")
    db_session.add_all([root, item, task])
    db_session.commit()
    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.library_tasks.enrich_media_item", Mock(return_value={"matched": False})
    )
    event = Event()
    event.set()
    _task_stop_events[task.id] = event

    _run_enrichment_task(root.id, task.id)

    db_session.expire_all()
    stopped = db_session.get(LibraryTask, task.id)
    assert stopped.status == "stopped"


def test_task_stop_event_can_be_pending(db_session):
    from app.library_tasks import _task_stop_events, _task_stop_requested

    root = LibraryRoot(path="/fake", display_name="Library")
    db_session.add(root)
    db_session.commit()
    task = LibraryTask(library_root_id=root.id, kind="enrich", status="running")
    db_session.add(task)
    db_session.commit()
    _task_stop_events[task.id] = Event()
    try:
        assert _task_stop_requested(db_session, task.id) is False
    finally:
        _task_stop_events.pop(task.id, None)
    assert _task_stop_requested(db_session, task.id) is False


def test_starting_a_second_task_for_a_root_is_rejected(client, db_session):
    root = LibraryRoot(path="/fake", display_name="Busy")
    db_session.add(root)
    db_session.commit()
    db_session.add(LibraryTask(library_root_id=root.id, kind="scan", status="running"))
    db_session.commit()

    response = client.post(f"/api/admin/roots/{root.id}/scan")

    assert response.status_code == 409


def test_task_workers_exit_when_their_task_or_root_is_gone(session_factory, monkeypatch):
    from app.library_tasks import (
        _run_cover_art_task,
        _run_enrichment_task,
        _run_scan_task,
    )

    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    _run_scan_task(999, 999)
    _run_enrichment_task(999, 999)
    _run_cover_art_task(999, 999)


def test_scan_worker_commits_root_error_when_task_disappears(monkeypatch):
    from app.library_tasks import _run_scan_task

    root = Mock()
    db = Mock()
    db.get.side_effect = [root, Mock(), None, root]
    monkeypatch.setattr("app.library_tasks.SessionLocal", lambda: db)
    monkeypatch.setattr("app.library_tasks.scan_library_root", Mock(side_effect=RuntimeError("boom")))

    _run_scan_task(1, 2)

    assert root.last_scan_error == "boom"
    assert db.commit.call_count == 2
    db.close.assert_called_once()


def test_enrichment_worker_records_failures(db_session, session_factory, monkeypatch):
    from app.library_tasks import _run_enrichment_task

    root = LibraryRoot(path="/fake", display_name="Enrichment")
    db_session.add(root)
    db_session.commit()
    task = LibraryTask(library_root_id=root.id, kind="enrichment", status="running")
    item = MediaItem(
        library_root_id=root.id,
        relative_path="song.mp3",
        title="Song",
        kind=MediaKind.track,
    )
    db_session.add_all([task, item])
    db_session.commit()
    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    monkeypatch.setattr("app.library_tasks.enrich_media_item", Mock(side_effect=RuntimeError("boom")))

    _run_enrichment_task(root.id, task.id)

    db_session.expire_all()
    failed = db_session.get(LibraryTask, task.id)
    assert failed.status == "failed"
    assert failed.error == "boom"


def test_enrichment_worker_skips_items_with_complete_metadata(
    db_session, session_factory, monkeypatch
):
    root = LibraryRoot(path="/fake", display_name="Enrichment")
    complete = MediaItem(
        library_root_id=1,
        relative_path="complete.mp3",
        title="Complete",
        kind=MediaKind.track,
        musicbrainz_metadata_json={"id": "already-fetched"},
        musicbrainz_fetched_at=utcnow(),
    )
    missing = MediaItem(
        library_root_id=1,
        relative_path="missing.mp3",
        title="Missing",
        kind=MediaKind.track,
    )
    root.media_items.extend([complete, missing])
    task = LibraryTask(library_root=root, kind="enrich", status="running")
    db_session.add_all([root, task])
    db_session.flush()
    missing_id = missing.id
    db_session.commit()

    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    seen_ids = []

    def record_enrichment(_db, item, *, commit, fetch_cover_art):
        assert commit is False
        assert fetch_cover_art is False
        seen_ids.append(item.id)
        return {"matched": True}

    enrich = Mock(side_effect=record_enrichment)
    monkeypatch.setattr("app.library_tasks.enrich_media_item", enrich)

    from app.library_tasks import _run_enrichment_task

    _run_enrichment_task(root.id, task.id)

    enrich.assert_called_once()
    assert seen_ids == [missing_id]


def test_enrichment_worker_retries_persisted_catalogue_misses(
    db_session, session_factory, monkeypatch
):
    root = LibraryRoot(path="/fake", display_name="Enrichment")
    skipped = MediaItem(
        library_root=root,
        relative_path="no-match.mp3",
        title="No match",
        kind=MediaKind.track,
        musicbrainz_lookup_status="no_match",
    )
    retry = MediaItem(
        library_root=root,
        relative_path="retry.mp3",
        title="Retry",
        kind=MediaKind.track,
        musicbrainz_lookup_status="retry",
    )
    task = LibraryTask(library_root=root, kind="enrich", status="running")
    db_session.add_all([root, task, skipped, retry])
    db_session.commit()
    retry_id = retry.id

    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    seen_ids = []

    def enrich_item(_db, item, *, commit, fetch_cover_art):
        seen_ids.append(item.id)
        return {"matched": False, "metadata_saved": False}

    enrich = Mock(side_effect=enrich_item)
    monkeypatch.setattr("app.library_tasks.enrich_media_item", enrich)

    from app.library_tasks import _run_enrichment_task

    _run_enrichment_task(root.id, task.id)

    assert enrich.call_count == 2
    assert set(seen_ids) == {retry_id, skipped.id}
    db_session.refresh(task)
    assert task.total == 2
    assert task.matched == 0


def test_cover_art_worker_counts_only_found_art(db_session, session_factory, monkeypatch):
    from app.library_tasks import _run_cover_art_task

    root = LibraryRoot(path="/fake", display_name="Art")
    item = MediaItem(
        library_root=root,
        relative_path="song.mp3",
        title="Song",
        kind=MediaKind.track,
        musicbrainz_metadata_json={"releases": [{"id": "release-1"}]},
    )
    task = LibraryTask(library_root=root, kind="cover_art", status="running")
    db_session.add_all([root, item, task])
    db_session.commit()
    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.library_tasks.fetch_cover_art_for_item",
        Mock(return_value={"matched": True, "fetched": False}),
    )

    _run_cover_art_task(root.id, task.id)

    db_session.expire_all()
    completed = db_session.get(LibraryTask, task.id)
    assert completed.status == "completed"
    assert completed.total == 1
    assert completed.matched == 0


def test_cover_art_worker_stops_after_current_item(
    db_session, session_factory, monkeypatch
):
    from app.library_tasks import _run_cover_art_task

    root = LibraryRoot(path="/fake", display_name="Stop art")
    item = MediaItem(
        library_root=root,
        relative_path="song.mp3",
        title="Song",
        kind=MediaKind.track,
        musicbrainz_metadata_json={"releases": [{"id": "release-1"}]},
    )
    task = LibraryTask(library_root=root, kind="cover_art", status="stopping")
    db_session.add_all([root, item, task])
    db_session.commit()
    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    fetch = Mock(return_value={"matched": True, "fetched": True})
    monkeypatch.setattr("app.library_tasks.fetch_cover_art_for_item", fetch)

    _run_cover_art_task(root.id, task.id)

    db_session.expire_all()
    stopped = db_session.get(LibraryTask, task.id)
    assert stopped.status == "stopped"
    assert stopped.processed == 1
    fetch.assert_called_once()


def test_cover_art_worker_records_failures(db_session, session_factory, monkeypatch):
    from app.library_tasks import _run_cover_art_task

    root = LibraryRoot(path="/fake", display_name="Art failure")
    task = LibraryTask(library_root=root, kind="cover_art", status="running")
    db_session.add_all([root, task])
    db_session.commit()
    monkeypatch.setattr("app.library_tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.library_tasks.fetch_cover_art_for_item",
        Mock(side_effect=RuntimeError("art boom")),
    )
    item = MediaItem(
        library_root_id=root.id,
        relative_path="song.mp3",
        title="Song",
        kind=MediaKind.track,
        musicbrainz_metadata_json={"releases": [{"id": "release-1"}]},
    )
    db_session.add(item)
    db_session.commit()

    _run_cover_art_task(root.id, task.id)

    db_session.expire_all()
    failed = db_session.get(LibraryTask, task.id)
    assert failed.status == "failed"
    assert failed.error == "art boom"


def test_cover_art_worker_ignores_failure_when_task_is_gone(monkeypatch):
    from app.library_tasks import _run_cover_art_task

    class MissingTaskSession:
        def __init__(self):
            self.calls = 0

        def get(self, _model, _id):
            self.calls += 1
            return Mock() if self.calls == 1 else None

        def query(self, *_args):
            raise RuntimeError("query failed")

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        "app.library_tasks.SessionLocal", lambda: MissingTaskSession()
    )
    _run_cover_art_task(1, 2)


def test_admin_can_promote_and_demote_user(client, db_session):
    other = _make_plain_user(db_session, username="climber")
    resp = client.patch(f"/api/admin/users/{other.id}", json={"is_admin": True})
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True
    db_session.refresh(other)
    assert other.is_admin is True

    resp = client.patch(f"/api/admin/users/{other.id}", json={"is_admin": False})
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is False


def test_admin_cannot_change_own_role_or_unknown_user(client):
    me = client.get("/api/auth/me").json()
    assert client.patch(f"/api/admin/users/{me['id']}", json={"is_admin": False}).status_code == 400
    assert client.patch("/api/admin/users/9999", json={"is_admin": True}).status_code == 404
    assert (
        client.post(f"/api/admin/users/{me['id']}/password", json={"new_password": "x" * 8}).status_code
        == 400
    )
    assert client.post("/api/admin/users/9999/password", json={"new_password": "x" * 8}).status_code == 404


def test_demoting_the_last_admin_is_refused(db_session):
    from app.routers.admin import UserUpdateIn, update_user

    only_admin = db_session.query(User).filter(User.is_admin.is_(True)).one()
    # A caller that isn't in the table: the one path where no other admin
    # would remain after the change.
    with pytest.raises(HTTPException) as exc:
        update_user(only_admin.id, UserUpdateIn(is_admin=False), db_session, SimpleNamespace(id=-1))
    assert exc.value.status_code == 409
    db_session.refresh(only_admin)
    assert only_admin.is_admin is True


def test_admin_password_reset_revokes_sessions(session_factory, client, db_session):
    other = _make_plain_user(db_session, username="forgetful")
    other_client = login_as(session_factory, client, "forgetful", "plainpass")
    assert other_client.get("/api/auth/me").status_code == 200

    resp = client.post(f"/api/admin/users/{other.id}/password", json={"new_password": "brand-new-pass"})
    assert resp.status_code == 200
    assert other_client.get("/api/auth/me").status_code == 401

    # login_as asserts the new password is accepted.
    login_as(session_factory, client, "forgetful", "brand-new-pass")


def test_admin_password_reset_validates_length_and_hash_errors(client, db_session):
    other = _make_plain_user(db_session, username="shorty")
    assert client.post(f"/api/admin/users/{other.id}/password", json={"new_password": "short"}).status_code == 422
    with patch("app.routers.admin.hash_password", side_effect=ValueError("too weak")):
        resp = client.post(f"/api/admin/users/{other.id}/password", json={"new_password": "long-enough"})
    assert resp.status_code == 422


def test_non_admin_cannot_manage_users(session_factory, client, db_session):
    plain = _make_plain_user(db_session)
    victim = _make_plain_user(db_session, username="victim")
    plain_client = login_as(session_factory, client, "plain", "plainpass")
    assert plain_client.patch(f"/api/admin/users/{plain.id}", json={"is_admin": True}).status_code == 403
    assert (
        plain_client.post(f"/api/admin/users/{victim.id}/password", json={"new_password": "x" * 8}).status_code
        == 403
    )


def test_admin_update_root_toggles_sharing_and_edits_fields(client, db_session):
    plain = _make_plain_user(db_session)
    root_id = client.post(
        "/api/admin/roots",
        json={"path": "/tmp/edit-a", "display_name": "A", "is_shared": True},
    ).json()["id"]
    other_id = client.post(
        "/api/admin/roots",
        json={"path": "/tmp/edit-b", "display_name": "B", "is_shared": True},
    ).json()["id"]

    resp = client.patch(f"/api/admin/roots/{root_id}", json={"is_shared": False})
    assert resp.status_code == 200
    assert resp.json()["is_shared"] is False
    assert client.post(f"/api/admin/roots/{root_id}/access/{plain.id}").status_code == 200

    resp = client.patch(
        f"/api/admin/roots/{root_id}",
        json={"display_name": "  Renamed  ", "path": "/tmp/edit-moved"},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed"
    assert resp.json()["path"] == "/tmp/edit-moved"
    assert resp.json()["is_shared"] is False

    assert (
        client.patch(f"/api/admin/roots/{root_id}", json={"path": "/tmp/edit-b"}).status_code
        == 409
    )
    assert (
        client.patch(f"/api/admin/roots/{root_id}", json={"path": "relative"}).status_code
        == 422
    )
    assert (
        client.patch(f"/api/admin/roots/{root_id}", json={"display_name": "  "}).status_code
        == 422
    )
    assert client.patch("/api/admin/roots/99999", json={"is_shared": True}).status_code == 404
    assert other_id != root_id


def test_scan_endpoint_passes_mode_to_the_scanner(client, db_session):
    root = LibraryRoot(path="/mode-root", display_name="Mode root")
    db_session.add(root)
    db_session.commit()
    seen: list[str] = []

    def fake_scan(_db, _root, _report, mode="normal", should_stop=None):
        seen.append(mode)
        return {"added": 0, "updated": 0, "removed": 0}

    with patch("app.library_tasks.scan_library_root", side_effect=fake_scan):
        assert client.post(f"/api/admin/roots/{root.id}/scan?mode=bogus").status_code == 422
        for mode in ("metadata", "full"):
            resp = client.post(f"/api/admin/roots/{root.id}/scan?mode={mode}")
            assert resp.status_code == 202
            # The next scan can only start once this task has ended.
            wait_until(lambda: _settled_last_task(client, root.id), what=f"the {mode} scan")
    assert seen == ["metadata", "full"]


def test_scan_endpoint_accepts_the_confirm_removals_mode(client, db_session):
    root = LibraryRoot(path="/confirm-root", display_name="Confirm root")
    db_session.add(root)
    db_session.commit()
    seen: list[str] = []

    def fake_scan(_db, _root, _report, mode="normal", should_stop=None):
        seen.append(mode)
        return {"added": 0, "updated": 0, "removed": 2}

    with patch("app.library_tasks.scan_library_root", side_effect=fake_scan):
        resp = client.post(f"/api/admin/roots/{root.id}/scan?mode=confirm_removals")
        assert resp.status_code == 202
        # Ended before the scanner patch is undone.
        wait_until(lambda: _settled_last_task(client, root.id), what="the scan")
    assert seen == ["confirm_removals"]


def test_scan_task_records_held_removals_without_failing(
    client, db_session, session_factory, library_dir
):
    from app.library_tasks import _run_scan_task
    from tests.conftest import make_audio_fixture

    for name in ("a.wav", "b.wav"):
        make_audio_fixture(library_dir / name, title=name, artist="A", album="X", genre=None)
    root = LibraryRoot(path=str(library_dir), display_name="Held")
    db_session.add(root)
    db_session.commit()
    first = LibraryTask(library_root_id=root.id, kind="scan", status="running")
    db_session.add(first)
    db_session.commit()
    _run_scan_task(root.id, first.id)
    for name in ("a.wav", "b.wav"):  # the share is no longer mounted
        (library_dir / name).unlink()
    task = LibraryTask(library_root_id=root.id, kind="scan", status="running")
    db_session.add(task)
    db_session.commit()

    _run_scan_task(root.id, task.id)

    last = next(r for r in client.get("/api/admin/roots").json() if r["id"] == root.id)["last_task"]
    assert last["status"] == "completed"
    assert last["missing_held"] == 2
    assert last["removed"] == 0
    assert "2 tracks look missing" in last["note"]
    assert "Nothing was removed" in last["note"]
    assert db_session.query(MediaItem).filter(MediaItem.library_root_id == root.id).count() == 2


def test_held_removal_note_names_unreadable_folders(db_session, session_factory):
    from app.library_tasks import _run_scan_task

    root = LibraryRoot(path="/held-root", display_name="Held")
    db_session.add(root)
    db_session.commit()
    task = LibraryTask(library_root_id=root.id, kind="scan", status="running")
    db_session.add(task)
    db_session.commit()
    result = {
        "added": 0, "updated": 0, "removed": 0, "failed": 0,
        "missing_held": 1, "unreadable_dirs": ["Rock", "Jazz/Live"],
    }

    with patch("app.library_tasks.scan_library_root", return_value=result):
        _run_scan_task(root.id, task.id)

    db_session.expire_all()
    task = db_session.get(LibraryTask, task.id)
    assert task.missing_held == 1
    assert task.note.startswith("1 track looks missing")
    assert "Could not read: Rock, Jazz/Live" in task.note


def _wait_for_task(db_session, task_id: int, timeout: float = 30.0) -> LibraryTask:
    def settled() -> LibraryTask | None:
        db_session.expire_all()
        task = db_session.get(LibraryTask, task_id)
        return task if task.status not in ("running", "stopping") else None

    return wait_until(settled, timeout, what=f"task {task_id}")


def test_stopping_a_scan_ends_it_where_it_is_and_frees_the_root(client, db_session, library_dir):
    from app.library_tasks import _run_scan_task, _task_stop_events
    from tests.conftest import make_audio_fixture

    for name in ("a.wav", "b.wav"):
        make_audio_fixture(library_dir / name, title=name, artist="A", album="X", genre=None)
    root = LibraryRoot(path=str(library_dir), display_name="Stoppable")
    db_session.add(root)
    db_session.commit()
    first = LibraryTask(library_root_id=root.id, kind="scan", status="running")
    db_session.add(first)
    db_session.commit()
    _run_scan_task(root.id, first.id)
    (library_dir / "a.wav").unlink()  # a complete scan would remove it
    for name in ("n1.wav", "n2.wav", "n3.wav", "n4.wav"):
        make_audio_fixture(library_dir / name, title=name, artist="N", album="Y", genre=None)
    reads: list[str] = []

    def lyrics(path):
        reads.append(path.name)
        if len(reads) == 2:
            response = client.post(f"/api/admin/roots/{root.id}/tasks/{task_id}/stop")
            assert response.json()["status"] == "stopping"
        return None

    with patch("app.scanner.get_embedded_lyrics", side_effect=lyrics):
        task_id = client.post(f"/api/admin/roots/{root.id}/scan").json()["id"]
        task = _wait_for_task(db_session, task_id)

    assert task.status == "stopped"
    assert (task.added, task.removed, task.missing_held) == (2, 0, 0)
    assert task.completed_at is not None
    # The worker drops its stop flag after it has saved the final status.
    wait_until(lambda: task_id not in _task_stop_events, what="the stop flag to be dropped")
    paths = {i.relative_path for i in db_session.query(MediaItem).filter_by(library_root_id=root.id)}
    assert "a.wav" in paths and len(paths) == 4
    # The root is free again: a new task starts and runs to the end.
    again = client.post(f"/api/admin/roots/{root.id}/scan")
    assert again.status_code == 202
    assert _wait_for_task(db_session, again.json()["id"]).status == "completed"


def test_a_root_cannot_be_deleted_while_a_task_runs(client, db_session):
    root = LibraryRoot(path="/busy-root", display_name="Busy")
    db_session.add(root)
    db_session.commit()
    task = LibraryTask(library_root_id=root.id, kind="scan", status="stopping")
    db_session.add(task)
    db_session.commit()

    assert client.delete(f"/api/admin/roots/{root.id}").status_code == 409
    db_session.expire_all()
    assert db_session.get(LibraryRoot, root.id) is not None

    task.status = "stopped"
    db_session.commit()
    assert client.delete(f"/api/admin/roots/{root.id}").status_code == 200


def test_a_task_whose_failure_handler_fails_still_ends_failed(client, db_session, caplog):
    """The worker's own failure handler commits too, and can hit a locked
    database as well; the task must not stay `running` for ever."""
    from app.library_tasks import _task_stop_events

    root = LibraryRoot(path="/fake", display_name="Art")
    item = MediaItem(
        library_root=root,
        relative_path="song.mp3",
        title="Song",
        kind=MediaKind.track,
    )
    db_session.add_all([root, item])
    db_session.commit()

    with patch(
        "app.library_tasks.enrich_media_item", side_effect=RuntimeError("lookup boom")
    ), patch("app.library_tasks._finish_task", side_effect=RuntimeError("database is locked")):
        task_id = client.post(
            f"/api/admin/roots/{root.id}/enrich", json={"mode": "musicbrainz_missing"}
        ).json()["id"]
        task = _wait_for_task(db_session, task_id)

    assert task.status == "failed"
    assert task.error == "database is locked"
    assert task.completed_at is not None
    wait_until(lambda: task_id not in _task_stop_events, what="the stop flag to be dropped")
    assert "could not record its outcome" in caplog.text


def test_the_last_resort_logs_when_even_it_cannot_write(monkeypatch, caplog):
    from app import library_tasks

    db = Mock()
    db.commit.side_effect = RuntimeError("still locked")
    monkeypatch.setattr(library_tasks, "SessionLocal", lambda: db)

    library_tasks._fail_task_last_resort(1, "boom")

    assert "could not be marked failed" in caplog.text
    db.close.assert_called_once()
