"""Small, targeted tests for branches the broader suites do not reach."""

from __future__ import annotations

import logging
import time
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    LibraryRoot,
    LibraryTask,
    MediaItem,
    MediaKind,
    User,
    utcnow,
)
from tests.conftest import DEFAULT_USERNAME

# --- admin: library root editing ---------------------------------------


def test_changing_a_root_path_is_blocked_while_a_task_runs(client, db_session):
    root_id = client.post(
        "/api/admin/roots", json={"path": "/tmp/gap-a", "display_name": "A", "is_shared": True}
    ).json()["id"]
    db_session.add(LibraryTask(library_root_id=root_id, kind="scan", status="running"))
    db_session.commit()

    resp = client.patch(f"/api/admin/roots/{root_id}", json={"path": "/tmp/gap-moved"})

    assert resp.status_code == 409
    assert "running library task" in resp.json()["detail"]


def test_root_update_reports_a_commit_race_as_a_duplicate(client):
    root_id = client.post(
        "/api/admin/roots", json={"path": "/tmp/gap-b", "display_name": "B", "is_shared": True}
    ).json()["id"]

    real_commit = Session.commit

    def racing_commit(self):
        if any(isinstance(obj, LibraryRoot) for obj in self.dirty):
            raise IntegrityError("stmt", {}, Exception("dup"))
        return real_commit(self)

    with patch.object(Session, "commit", racing_commit):
        resp = client.patch(f"/api/admin/roots/{root_id}", json={"display_name": "Renamed"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Library root already exists"


# --- admin: ListenBrainz tag task ----------------------------------------


def _tags_root(db_session):
    root = LibraryRoot(path="/gap-tags", display_name="Gap")
    db_session.add(root)
    db_session.commit()
    items = [
        MediaItem(
            library_root_id=root.id, kind=MediaKind.track, relative_path="new.mp3", title="N",
            artist="A", musicbrainz_artist_mbid="00000009-0000-4000-8000-000000000009",
        ),
        MediaItem(
            library_root_id=root.id, kind=MediaKind.track, relative_path="done.mp3", title="D",
            artist="A", musicbrainz_artist_mbid="00000009-0000-4000-8000-000000000009", online_tags_fetched_at=utcnow(),
        ),
        # No artist MBID: nothing to ask ListenBrainz by.
        MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="anon.mp3", title="X"),
    ]
    db_session.add_all(items)
    db_session.commit()
    return root


def _new_task(db_session, root, status="running") -> LibraryTask:
    task = LibraryTask(library_root_id=root.id, kind="lb_tags", status=status)
    db_session.add(task)
    db_session.commit()
    return task


def test_tags_worker_fetches_missing_then_everything(db_session, session_factory, monkeypatch):
    from app import library_tasks

    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    root = _tags_root(db_session)
    seen: list[str] = []

    def fake_apply(_db, item, tags, split=None):
        seen.append(item.relative_path)

    monkeypatch.setattr(library_tasks, "apply_listenbrainz_tags", fake_apply)
    monkeypatch.setattr(library_tasks.listenbrainz, "artist_tags", lambda mbids: {"00000009-0000-4000-8000-000000000009": [("rock", 3)]})

    task = _new_task(db_session, root)
    library_tasks._run_listenbrainz_tags_task(root.id, task.id)
    db_session.refresh(task)
    assert (task.status, task.total, task.matched) == ("completed", 1, 1)
    assert seen == ["new.mp3"]

    seen.clear()
    task = _new_task(db_session, root)
    library_tasks._run_listenbrainz_tags_task(root.id, task.id, refresh_all=True)
    db_session.refresh(task)
    assert (task.status, task.total) == ("completed", 2)
    assert sorted(seen) == ["done.mp3", "new.mp3"]


def test_tags_worker_stops_gracefully_and_records_failures(db_session, session_factory, monkeypatch):
    from app import library_tasks

    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    root = _tags_root(db_session)
    monkeypatch.setattr(library_tasks.listenbrainz, "artist_tags", lambda mbids: {})

    stopping = _new_task(db_session, root, status="stopping")
    library_tasks._run_listenbrainz_tags_task(root.id, stopping.id, refresh_all=True)
    db_session.refresh(stopping)
    assert stopping.status == "stopped"

    def boom(*_a):
        raise RuntimeError("listenbrainz down")

    monkeypatch.setattr(library_tasks, "apply_listenbrainz_tags", boom)
    failing = _new_task(db_session, root)
    library_tasks._run_listenbrainz_tags_task(root.id, failing.id, refresh_all=True)
    db_session.refresh(failing)
    assert failing.status == "failed"
    assert "listenbrainz down" in (failing.error or "")

    library_tasks._run_listenbrainz_tags_task(root.id, 99999)  # task vanished: no-op


def test_enrich_listenbrainz_modes_start_the_worker(client, db_session, monkeypatch):
    from app import library_tasks

    root = _tags_root(db_session)
    started = []
    monkeypatch.setattr(
        library_tasks, "spawn", lambda _name, target, *args: started.append((target, args))
    )

    for mode, kind, refresh_all in [
        ("listenbrainz_missing", "lb_tags", False),
        ("listenbrainz_all", "lb_tags_all", True),
    ]:
        resp = client.post(f"/api/admin/roots/{root.id}/enrich", json={"mode": mode})
        assert resp.status_code == 202
        assert resp.json()["kind"] == kind
        target, args = started[-1]
        assert target is library_tasks._run_guarded
        worker, task_id, *worker_args = args
        run = Mock()
        monkeypatch.setattr(library_tasks, "_run_listenbrainz_tags_task", run)
        worker(*worker_args)
        run.assert_called_once_with(root.id, task_id, refresh_all)
        db_session.query(LibraryTask).filter(LibraryTask.id == resp.json()["id"]).update(
            {"status": "completed"}
        )
        db_session.commit()


# --- taxonomy --------------------------------------------------------------


def test_musicbrainz_refresh_takes_over_an_artist_tag(db_session):
    from app.metadata_taxonomy import (
        replace_item_listenbrainz_tags,
        replace_item_musicbrainz_tags,
    )
    from app.models import MediaItemMusicBrainzTag

    root = LibraryRoot(path="/gap-tax", display_name="T")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="a.mp3", title="T", artist="A")
    db_session.add(item)
    db_session.commit()

    replace_item_listenbrainz_tags(db_session, item, [("epic", 3)])
    db_session.commit()
    replace_item_musicbrainz_tags(db_session, item, [("epic", 9)])
    db_session.commit()

    # The recording's own tag takes precedence over its artist's.
    row = db_session.query(MediaItemMusicBrainzTag).filter_by(media_item_id=item.id).one()
    assert (row.source, row.vote_count) == ("musicbrainz", 9)


# --- migrations --------------------------------------------------------


def test_lastfm_columns_are_added_to_older_tables(tmp_path):
    from sqlalchemy import create_engine

    from app import migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE media_items (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql(
            "CREATE TABLE media_item_musicbrainz_tags (media_item_id INTEGER, musicbrainz_tag_id INTEGER)"
        )
    with patch("app.migrations.engine", engine):
        migrations.ensure_lastfm_metadata_columns()
    with engine.connect() as conn:
        items = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(media_items)")}
        tags = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(media_item_musicbrainz_tags)")}
    assert "lastfm_fetched_at" in items and "source" in tags


# --- request logging ----------------------------------------------------


def test_unhandled_errors_and_slow_requests_are_logged(client, monkeypatch, caplog):
    from app import main
    from app.deps import get_current_user

    def boom():
        raise RuntimeError("kaboom")

    client.app.dependency_overrides[get_current_user] = boom
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
            client.get("/api/auth/me")
    finally:
        client.app.dependency_overrides.pop(get_current_user, None)
    assert "unhandled error" in caplog.text

    caplog.clear()
    monkeypatch.setattr(main, "_SLOW_REQUEST_SECONDS", 0.0)
    with caplog.at_level(logging.WARNING):
        assert client.get("/api/auth/me").status_code == 200
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# --- client log rate window ---------------------------------------------


def test_client_log_forgets_reports_older_than_the_window(client, db_session):
    from collections import deque

    from app.routers import client_log

    user = db_session.query(User).filter(User.username == DEFAULT_USERNAME).one()
    client_log._recent[user.id] = deque([time.monotonic() - 10_000] * client_log.MAX_PER_WINDOW)
    try:
        assert client.post("/api/client-log", json={"message": "oops"}).status_code == 204
        assert len(client_log._recent[user.id]) == 1
    finally:
        client_log._recent.pop(user.id, None)


# --- playback websocket --------------------------------------------------


def test_ws_treats_a_vanished_client_as_an_ordinary_drop(client):
    from starlette.websockets import WebSocket, WebSocketState

    async def vanished(self):
        self.client_state = WebSocketState.DISCONNECTED
        raise RuntimeError('Cannot call "receive" once a disconnect message has been received.')

    with patch.object(WebSocket, "receive_json", vanished):
        with client.websocket_connect("/ws/playback"):
            pass


# --- HLS duration probing in the stream router ---------------------------


def test_hls_playlist_probes_duration_when_unknown(client, db_session, library_dir):
    root = LibraryRoot(path=str(library_dir), display_name="V")
    db_session.add(root)
    db_session.commit()
    (library_dir / "v.mkv").write_bytes(b"not really a video")
    item = MediaItem(
        library_root_id=root.id, kind=MediaKind.video, relative_path="v.mkv", title="V",
        duration_seconds=None,
    )
    db_session.add(item)
    db_session.commit()
    url = f"/api/stream/{item.id}/hls/index.m3u8"

    with patch("app.routers.stream.probe_duration_seconds", return_value=None):
        resp = client.get(url)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Video duration unknown"

    with patch("app.routers.stream.probe_duration_seconds", return_value=13.0):
        resp = client.get(url)
    assert resp.status_code == 200
    assert resp.text.count("#EXTINF") == 3


def test_ws_propagates_unexpected_runtime_errors_on_a_live_socket(client):
    from starlette.websockets import WebSocket

    async def broken(self):
        raise RuntimeError("genuine server fault")

    with patch.object(WebSocket, "receive_json", broken):
        with pytest.raises(RuntimeError, match="genuine server fault"):
            with client.websocket_connect("/ws/playback"):
                pass
