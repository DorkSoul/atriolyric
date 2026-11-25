from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import activity_log
from app.activity_log import record_event, summarize, wait_for_writes
from app.cleanup import prune_activity_events
from app.models import ActivityEvent, LibraryRoot, User, utcnow
from app.security import hash_password
from tests.conftest import DEFAULT_PASSWORD, login_as, make_audio_fixture, wait_until


def _events(db_session) -> list[ActivityEvent]:
    assert wait_for_writes(10)
    db_session.expire_all()
    return list(db_session.scalars(select(ActivityEvent).order_by(ActivityEvent.id)))


def _kinds(db_session) -> list[str]:
    return [event.kind for event in _events(db_session)]


def test_a_sign_in_is_stored_with_its_actor(client, db_session):
    login = next(e for e in _events(db_session) if e.kind == "auth.login")
    assert login.actor == "dork_soul"
    assert login.level == "info"
    assert login.summary.startswith("dork_soul signed in")
    assert login.request_id


def test_a_sign_out_names_who_signed_out(client, db_session):
    assert client.post("/api/auth/logout").status_code == 200
    logout = next(e for e in _events(db_session) if e.kind == "auth.logout")
    assert logout.actor == "dork_soul"
    assert logout.summary.startswith("dork_soul signed out")


def test_a_failed_sign_in_is_a_warning_without_the_password(raw_client, db_session):
    response = raw_client.post(
        "/api/auth/login", json={"username": "dork_soul", "password": "hunter2-secret"}
    )
    assert response.status_code == 401
    failed = next(e for e in _events(db_session) if e.kind == "auth.login_failed")
    assert failed.level == "warning"
    assert failed.actor is None
    assert "wrong password" in failed.summary
    assert "hunter2-secret" not in failed.summary


def test_a_scan_is_stored_with_its_counts(client, db_session, library_dir):
    make_audio_fixture(library_dir / "a.wav", title="A", artist="X", album="LP", genre=None)
    make_audio_fixture(library_dir / "b.wav", title="B", artist="X", album="LP", genre=None)
    root = client.post(
        "/api/admin/roots", json={"path": str(library_dir), "display_name": "Music", "is_shared": True}
    ).json()
    assert client.post(f"/api/admin/roots/{root['id']}/scan").status_code == 202

    def finish():
        return next((e for e in _events(db_session) if e.kind == "task.finish"), None)

    finished = wait_until(finish, what="the scan's finish event")
    assert finished.summary == "Scan of Music finished: 2 added, 0 updated, 0 removed"
    assert finished.root_id == root["id"]
    assert finished.context == "Music"
    started = next(e for e in _events(db_session) if e.kind == "task.start")
    assert started.summary == "Scan of Music started by dork_soul"
    assert started.actor == "dork_soul"
    created = next(e for e in _events(db_session) if e.kind == "root.create")
    assert created.summary == f"dork_soul added the library folder Music ({library_dir})"


def test_a_rescan_and_a_failure_are_described(db_session):
    root = LibraryRoot(path="/nowhere", display_name="Gone")
    db_session.add(root)
    db_session.commit()
    record_event("task.start", "info", fields={"root": root.id, "kind": "scan", "mode": "metadata"})
    record_event(
        "task.finish", "error",
        fields={"root": root.id, "kind": "scan", "status": "failed", "error": "No such folder"},
    )
    summaries = [e.summary for e in _events(db_session)]
    assert "Rescan metadata of Gone started" in summaries
    assert "Scan of Gone failed: No such folder" in summaries


def test_a_tag_edit_names_the_song(client, db_session, library_dir):
    make_audio_fixture(library_dir / "a.wav", title="Song", artist="Band", album="LP", genre=None)
    root = client.post(
        "/api/admin/roots", json={"path": str(library_dir), "display_name": "Music", "is_shared": True}
    ).json()
    client.post(f"/api/admin/roots/{root['id']}/scan")
    track = wait_until(lambda: client.get("/api/library/tracks").json(), what="the scan")[0]
    assert client.patch(f"/api/admin/media/{track['id']}/tags", json={"genre": "Pop"}).status_code == 200
    edit = next(e for e in _events(db_session) if e.kind == "media.tags_edit")
    assert edit.summary == "dork_soul edited the tags of Band - Song (genre)"


def test_server_errors_are_stored(client, db_session, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("database password is swordfish")

    monkeypatch.setattr("app.routers.library.stats.accessible_root_ids", broken)
    lenient = TestClient(client.app, raise_server_exceptions=False)
    lenient.cookies.update(client.cookies)
    assert lenient.get("/api/library/stats?x=token123").status_code == 500
    error = next(e for e in _events(db_session) if e.kind == "server.error")
    assert error.level == "error"
    assert error.actor == "dork_soul"
    assert error.summary == "GET /api/library/stats failed with 500 (RuntimeError)"


def test_no_secret_reaches_a_summary(client, db_session):
    secrets = ["first-secret-pw", "second-secret-pw", "reset-secret-pw"]
    assert client.post(
        "/api/auth/password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": secrets[0]},
    ).status_code == 200
    user = client.post("/api/admin/users", json={"username": "plain", "password": secrets[1]}).json()
    client.post(f"/api/admin/users/{user['id']}/password", json={"new_password": secrets[2]})
    client.post("/api/auth/login", json={"username": "plain", "password": "wrong-secret-pw"})
    # A caller passing a secret-looking field by mistake: dropped.
    record_event("custom.thing", "info", fields={"api_key": "key-secret", "token": "tok-secret", "ok": 1})
    events = _events(db_session)
    kinds = {e.kind for e in events}
    assert {"auth.password_change", "user.create", "user.password_reset", "auth.login_failed"} <= kinds
    for event in events:
        for secret in [*secrets, "wrong-secret-pw", "key-secret", "tok-secret"]:
            assert secret not in event.summary, event.summary
    assert next(e for e in events if e.kind == "custom.thing").summary == "custom.thing: ok=1"


def test_summaries_stay_on_one_short_line():
    assert summarize("client.error", {"message": "a\nb"}, actor="x") == "App error for x: a\nb"
    record = activity_log._one_line("line one\nline two " + "x" * 400)
    assert "\n" not in record and len(record) == activity_log.SUMMARY_MAX_LENGTH


def _seed(db_session, count: int, **values) -> None:
    now = utcnow()
    for index in range(count):
        db_session.add(
            ActivityEvent(
                created_at=values.get("created_at", now - timedelta(minutes=count - index)),
                kind=values.get("kind", "test.kind"),
                level=values.get("level", "info"),
                summary=f"{values.get('kind', 'test.kind')} {index}",
            )
        )
    db_session.commit()


def test_listing_filters_and_pages(client, db_session):
    wait_for_writes(10)
    db_session.query(ActivityEvent).delete()
    db_session.commit()
    _seed(db_session, 5, kind="alpha", level="info")
    _seed(db_session, 3, kind="beta", level="error")
    three_days_ago = utcnow().replace(hour=12, minute=0) - timedelta(days=3)
    _seed(db_session, 2, kind="alpha", level="info", created_at=three_days_ago)

    first = client.get("/api/admin/activity", params={"kind": "alpha", "limit": 4}).json()
    assert [e["kind"] for e in first["events"]] == ["alpha"] * 4
    ids = [e["id"] for e in first["events"]]
    assert ids == sorted(ids, reverse=True)
    assert first["next_before_id"] == ids[-1]
    assert set(first["kinds"]) >= {"alpha", "beta"}
    second = client.get(
        "/api/admin/activity", params={"kind": "alpha", "limit": 4, "before_id": first["next_before_id"]}
    ).json()
    assert len(second["events"]) == 3 and second["next_before_id"] is None

    errors = client.get("/api/admin/activity", params={"level": "error"}).json()["events"]
    assert {e["kind"] for e in errors} == {"beta"} and len(errors) == 3

    old_day = three_days_ago.date().isoformat()
    on_day = client.get("/api/admin/activity", params={"day": old_day}).json()["events"]
    assert len(on_day) == 2
    assert client.get("/api/admin/activity", params={"day": "2026-13-40"}).status_code == 422
    assert client.get("/api/admin/activity", params={"level": "fatal"}).status_code == 422


def test_the_day_follows_the_viewers_time_zone(client, db_session):
    wait_for_writes(10)
    db_session.query(ActivityEvent).delete()
    db_session.commit()
    stamp = utcnow().replace(hour=23, minute=30, second=0, microsecond=0) - timedelta(days=1)
    _seed(db_session, 1, kind="late", created_at=stamp)
    utc_day = stamp.date().isoformat()
    next_day = (stamp + timedelta(days=1)).date().isoformat()
    # 23:30 UTC is already the next day at UTC+1 (getTimezoneOffset() = -60).
    assert len(client.get("/api/admin/activity", params={"day": utc_day, "kind": "late"}).json()["events"]) == 1
    plus_one = {"day": next_day, "kind": "late", "tz_offset_minutes": -60}
    assert len(client.get("/api/admin/activity", params=plus_one).json()["events"]) == 1
    text = client.get("/api/admin/activity/download", params=plus_one).text
    assert text == f"{next_day} 00:30:00  late  late 0\n"


def test_download_is_a_text_attachment(client, db_session):
    wait_for_writes(10)
    db_session.query(ActivityEvent).delete()
    db_session.commit()
    _seed(db_session, 3, kind="gamma")
    response = client.get("/api/admin/activity/download", params={"kind": "gamma"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"] == 'attachment; filename="atriolyric-activity-all.txt"'
    lines = response.text.splitlines()
    assert [line.split("  ", 2)[2] for line in lines] == ["gamma 2", "gamma 1", "gamma 0"]
    assert all(len(line.split("  ")[0]) == len("YYYY-MM-DD HH:MM:SS") for line in lines)


def test_only_admins_read_the_activity_log(client, session_factory, db_session):
    db_session.add(User(username="plain", password_hash=hash_password("plainpass")))
    db_session.commit()
    plain = login_as(session_factory, client, "plain", "plainpass")
    assert plain.get("/api/admin/activity").status_code == 403
    assert plain.get("/api/admin/activity/download").status_code == 403


def test_pruning_honours_retention_and_the_row_cap(db_session, monkeypatch):
    wait_for_writes(10)
    db_session.query(ActivityEvent).delete()
    db_session.commit()
    _seed(db_session, 4, kind="old", created_at=utcnow() - timedelta(days=40))
    _seed(db_session, 6, kind="new")

    assert prune_activity_events(db_session, retention_days=30, max_rows=1000, batch_size=3) == 4
    assert {e.kind for e in db_session.scalars(select(ActivityEvent))} == {"new"}

    assert prune_activity_events(db_session, retention_days=0, max_rows=4, batch_size=1) == 2
    kept = list(db_session.scalars(select(ActivityEvent.summary).order_by(ActivityEvent.id)))
    assert kept == ["new 2", "new 3", "new 4", "new 5"]

    # 0 keeps everything for age; the defaults come from the settings.
    from app.config import settings

    _seed(db_session, 2, kind="ancient", created_at=utcnow() - timedelta(days=4000))
    monkeypatch.setattr(settings, "activity_log_retention_days", 0)
    monkeypatch.setattr(settings, "activity_log_max_rows", 1000)
    assert prune_activity_events(db_session) == 0


def test_a_failed_write_never_reaches_the_caller(db_session, caplog):
    def broken_factory():
        raise RuntimeError("no database")

    pending = activity_log._Pending(
        created_at=utcnow(), kind="x", level="info", actor=None, fields={}, request_id=None,
        factory=broken_factory,
    )
    activity_log._write([pending])  # logs and returns
    assert "Could not store 1 activity event" in caplog.text
