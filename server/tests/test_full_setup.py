from __future__ import annotations

from unittest.mock import patch

from app.models import LibraryRoot, LibraryTask, User
from tests.conftest import (
    DEFAULT_USERNAME,
    make_audio_fixture,
    startup_against,
    wait_until,
)


def _wait(db_session, task_id: int, done=("completed", "stopped", "failed"), timeout=30.0) -> LibraryTask:
    def settled() -> LibraryTask | None:
        db_session.expire_all()
        task = db_session.get(LibraryTask, task_id)
        # A full setup's last step marks the task completed, and the stage
        # becomes "Done" in a separate commit just after: a completed task is
        # only settled once that has landed too.
        if task.status in done and (task.status != "completed" or task.stage in (None, "Done")):
            return task
        return None

    return wait_until(settled, timeout, what=f"task {task_id} to settle")


def _root(db_session, library_dir) -> LibraryRoot:
    make_audio_fixture(library_dir / "a.wav", title="A", artist="Band", album="LP", genre=None)
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    return root


def test_full_setup_runs_every_step_in_order(client, db_session, library_dir):
    root = _root(db_session, library_dir)
    calls: list[str] = []
    with patch(
        "app.library_tasks.enrich_media_item", side_effect=lambda *a, **k: calls.append("mb") or {"matched": False}
    ), patch(
        "app.library_tasks._lookup_recording",
        side_effect=lambda item: calls.append("mbid") or type("R", (), {"value": None, "error": None})(),
    ), patch(
        "app.library_tasks._run_listenbrainz_tags_task",
        side_effect=lambda *a: calls.append("listenbrainz"),
    ), patch(
        "app.library_tasks.artist_info", side_effect=lambda *a, **k: calls.append("info") or {}
    ), patch("app.library_tasks.artist_top_tracks"):
        resp = client.post(f"/api/admin/roots/{root.id}/full-setup")
        assert resp.status_code == 202, resp.text
        assert resp.json()["kind"] == "full_setup"
        task = _wait(db_session, resp.json()["id"])
    assert task.status == "completed" and task.stage == "Done"
    assert calls == ["mbid", "mb", "listenbrainz", "info"]


def test_full_setup_new_only_limits_every_step_to_what_is_missing(client, db_session, library_dir):
    root = _root(db_session, library_dir)
    seen: dict[str, object] = {}
    with patch(
        "app.library_tasks._run_scan_task", side_effect=lambda r, t, mode: seen.update(scan=mode)
    ), patch("app.library_tasks._run_mbid_lookup_task"), patch(
        "app.library_tasks._run_enrichment_task",
        side_effect=lambda r, t, refresh_all=False: seen.update(mb=refresh_all),
    ), patch(
        "app.library_tasks._run_listenbrainz_tags_task",
        side_effect=lambda r, t, refresh_all: seen.update(lb=refresh_all),
    ), patch("app.library_tasks._run_cover_art_task"), patch("app.library_tasks._run_artist_info_task"):
        resp = client.post(f"/api/admin/roots/{root.id}/full-setup-new")
        assert resp.status_code == 202, resp.text
        assert resp.json()["kind"] == "full_setup_new"
        # The patched workers never finish the task themselves, so it stays
        # "running"; the run is over once the last step has handed over and
        # the stage reads "Done" - before the patches are undone.
        task_id = resp.json()["id"]

        def done() -> bool:
            db_session.expire_all()
            return db_session.get(LibraryTask, task_id).stage == "Done"

        wait_until(done, what="the full setup's last step")
    assert seen == {"scan": "normal", "mb": False, "lb": False}


def test_stopping_ends_the_whole_run(client, db_session, library_dir):
    root = _root(db_session, library_dir)

    def stop_during_mbids(item):
        client.post(f"/api/admin/roots/{root.id}/tasks/{task_id}/stop")
        return type("R", (), {"value": None, "error": None})()

    with patch(
        "app.library_tasks._lookup_recording", side_effect=stop_during_mbids
    ), patch("app.library_tasks.enrich_media_item") as enrich:
        task_id = client.post(f"/api/admin/roots/{root.id}/full-setup").json()["id"]
        task = _wait(db_session, task_id)
    assert task.status == "stopped"
    assert task.stage.startswith("2/")
    enrich.assert_not_called()


def test_never_show_again_notices_are_per_account(client, db_session):
    assert client.get("/api/auth/me").json()["dismissed_notices"] == []
    resp = client.post("/api/auth/dismissed-notices", json={"notice": "admin_lastfm_key"})
    assert resp.status_code == 200
    assert resp.json()["dismissed_notices"] == ["admin_lastfm_key"]
    client.post("/api/auth/dismissed-notices", json={"notice": "admin_lastfm_key"})
    user = db_session.query(User).filter_by(username=DEFAULT_USERNAME).one()
    db_session.refresh(user)
    assert user.dismissed_notices == ["admin_lastfm_key"]
    assert client.post("/api/auth/dismissed-notices", json={"notice": "Bad Id!"}).status_code == 422


def test_restart_settles_running_and_stopping_tasks(session_factory, db_session, library_dir):
    from app.main import mark_interrupted_library_tasks

    root = _root(db_session, library_dir)
    running = LibraryTask(library_root_id=root.id, kind="full_setup", status="running")
    stopping = LibraryTask(library_root_id=root.id, kind="full_setup", status="stopping")
    db_session.add_all([running, stopping])
    db_session.commit()

    with startup_against(session_factory):
        mark_interrupted_library_tasks()

    db_session.expire_all()
    assert db_session.get(LibraryTask, running.id).status == "failed"
    assert db_session.get(LibraryTask, stopping.id).status == "stopped"
