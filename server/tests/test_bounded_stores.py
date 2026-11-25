from __future__ import annotations

import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from app import catalog, external_cache, main
from app.cleanup import enforce_cache_budget, purge_expired_sessions
from app.config import settings
from app.models import ExternalCache, User, utcnow
from app.models import Session as SessionModel
from tests.conftest import DEFAULT_USERNAME


def test_tick_runs_every_store_step_and_one_failure_does_not_stop_the_others(db_session):
    with (
        patch.object(main, "enforce_cache_budget", side_effect=RuntimeError("disk")) as budget,
        patch.object(main, "prune_expired") as prune,
        patch.object(main, "purge_expired_sessions") as purge,
        patch.object(main, "prune_play_history") as prune_history,
        patch.object(main, "run_due_auto_refreshes"),
        patch.object(main.provider_queue, "submit"),
    ):
        main._run_due_auto_refreshes()
    budget.assert_called_once()
    prune.assert_called_once()
    purge.assert_called_once()
    prune_history.assert_called_once()


def test_tick_retries_queued_scrobbles_and_survives_its_failure(db_session):
    with (
        patch.object(main, "_run_store_maintenance"),
        patch.object(main, "retry_queued_scrobbles", side_effect=RuntimeError("locked")) as retry,
        patch.object(main, "run_due_auto_refreshes") as refreshes,
        patch.object(main.provider_queue, "submit"),
    ):
        main._run_due_auto_refreshes()
    retry.assert_called_once()
    refreshes.assert_called_once()  # the rest of the tick still ran


def test_tick_queues_the_want_list_jobs_instead_of_running_them_inline(session_factory):
    queued: dict[str, object] = {}
    with (
        patch.object(main, "SessionLocal", session_factory),
        patch.object(main, "_run_store_maintenance"),
        patch.object(main, "retry_queued_scrobbles"),
        patch.object(main, "run_due_auto_refreshes"),
        patch.object(main, "refresh_wanted_catalogs") as catalogs,
        patch.object(main, "backfill_wanted_metadata") as backfill,
        patch.object(main.provider_queue, "submit", side_effect=lambda key, job, **_: queued.update({key: job})),
    ):
        main._run_due_auto_refreshes()
        assert set(queued) == {"scheduled:want-catalogs", "scheduled:want-metadata"}
        catalogs.assert_not_called()
        backfill.assert_not_called()
        # The queue runs each later, in a session of its own.
        for job in queued.values():
            job()  # type: ignore[operator]
    (catalog_db,), _ = catalogs.call_args
    (backfill_db,), _ = backfill.call_args
    assert catalog_db is not backfill_db
    assert catalog_db.bind is backfill_db.bind is session_factory.kw["bind"]


def test_a_failing_tick_job_is_rolled_back_before_the_next(db_session, session_factory):
    """A job that fails mid-flush leaves the session needing a rollback;
    without one every later job in the tick fails on that session."""
    seen: list[str] = []

    def broken_scrobbles(db):
        db.add(User(username=DEFAULT_USERNAME, password_hash="x"))  # taken: the flush fails
        db.flush()

    def refreshes(db):
        seen.append(db.query(User).one().username)

    with (
        patch.object(main, "SessionLocal", session_factory),
        patch.object(main, "_run_store_maintenance"),
        patch.object(main, "retry_queued_scrobbles", side_effect=broken_scrobbles),
        patch.object(main, "run_due_auto_refreshes", side_effect=refreshes),
        patch.object(main.provider_queue, "submit") as submit,
    ):
        main._run_due_auto_refreshes()
    assert len(seen) == 1
    assert submit.call_count == 2


def test_a_failing_discovery_refresh_still_queues_the_want_list_jobs(session_factory):
    with (
        patch.object(main, "SessionLocal", session_factory),
        patch.object(main, "_run_store_maintenance"),
        patch.object(main, "retry_queued_scrobbles"),
        patch.object(main, "run_due_auto_refreshes", side_effect=RuntimeError("locked")),
        patch.object(main.provider_queue, "submit") as submit,
    ):
        main._run_due_auto_refreshes()
    assert sorted(call.args[0] for call in submit.call_args_list) == [
        "scheduled:want-catalogs",
        "scheduled:want-metadata",
    ]


def test_the_sqlite_temp_directory_exists_beside_the_database():
    from app import db

    assert db.SQLITE_TMP_DIR == settings.database_path.parent / "tmp"
    assert db.SQLITE_TMP_DIR.is_dir()


def test_sqlite_tmpdir_is_created_before_the_first_query(tmp_path):
    """The image sets SQLITE_TMPDIR=/data/tmp: importing app.db creates it,
    so a sort that spills to temp files works from the first query."""
    import subprocess
    import sys

    data = tmp_path / "data"
    script = (
        "from sqlalchemy import text\n"
        "from app.db import engine\n"
        "with engine.connect() as c:\n"
        "    c.exec_driver_sql('PRAGMA temp_store=FILE')\n"
        "    c.exec_driver_sql('PRAGMA cache_size=-64')\n"
        "    rows = c.execute(text(\"WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<20000) \""
        "\"SELECT printf('%0500d', i) FROM n ORDER BY 1 DESC\")).fetchall()\n"
        "print(len(rows))\n"
    )
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("ATRIOLYRIC_")
    } | {"ATRIOLYRIC_DATABASE_PATH": str(data / "app.db"), "SQLITE_TMPDIR": str(data / "tmp")}
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "20000"
    assert (data / "tmp").is_dir()


def test_maintenance_survives_a_failing_prune(db_session):
    db = MagicMock()
    with (
        patch.object(main, "enforce_cache_budget") as budget,
        patch.object(main, "prune_expired", side_effect=RuntimeError("locked")),
        patch.object(main, "purge_expired_sessions") as purge,
        patch.object(main, "prune_activity_events") as prune_activity,
    ):
        main._run_store_maintenance(db)
    budget.assert_called_once()
    purge.assert_called_once()
    prune_activity.assert_called_once()
    db.rollback.assert_called_once()


def test_expired_sessions_are_deleted_and_live_ones_kept(db_session):
    user = db_session.query(User).one()
    now = utcnow()
    db_session.add_all(
        [
            SessionModel(token="old", user_id=user.id, expires_at=now - timedelta(minutes=1)),
            SessionModel(token="live", user_id=user.id, expires_at=now + timedelta(days=1)),
        ]
    )
    db_session.commit()
    assert purge_expired_sessions(db_session) == 1
    assert [s.token for s in db_session.query(SessionModel)] == ["live"]


def test_expired_cache_rows_are_deleted_and_live_ones_kept(db_session):
    now = external_cache._now()

    def row(key, age, payload):
        db_session.add(
            ExternalCache(provider="p", kind="k", key=key, payload_json=payload, fetched_at=now - age)
        )

    row("fresh", timedelta(days=1), '{"a":1}')
    row("old-but-in-ttl", timedelta(days=60), '{"a":1}')
    row("ancient", external_cache.MAX_AGE + timedelta(days=1), '{"a":1}')
    row("recent-miss", timedelta(days=1), "[]")
    row("stale-miss", external_cache.MAX_MISS_AGE + timedelta(days=1), "[]")
    db_session.commit()
    assert external_cache.prune_expired(db_session) == 2
    assert {r.key for r in db_session.query(ExternalCache)} == {"fresh", "old-but-in-ttl", "recent-miss"}


def test_budget_evicts_least_recently_used_first(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    settings.transcode_dir.mkdir(parents=True)
    written_first_but_played_last = settings.transcode_dir / "a.mp3"
    written_second_never_played = settings.transcode_dir / "b.mp3"
    for path in (written_first_but_played_last, written_second_never_played):
        path.write_bytes(b"1234")
    now = os.stat(written_first_but_played_last).st_mtime
    os.utime(written_first_but_played_last, (now + 100, now - 100))  # atime newer
    os.utime(written_second_never_played, (now - 50, now - 50))
    enforce_cache_budget(4)
    assert written_first_but_played_last.exists()
    assert not written_second_never_played.exists()
    enforce_cache_budget(0)
    assert not written_first_but_played_last.exists()


def test_budget_falls_back_to_mtime_when_atime_is_stale(tmp_path, monkeypatch):
    """noatime: atime stays at creation, never later than mtime."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    settings.transcode_dir.mkdir(parents=True)
    older = settings.transcode_dir / "older.mp3"
    newer = settings.transcode_dir / "newer.mp3"
    older.write_bytes(b"1234")
    newer.write_bytes(b"1234")
    os.utime(older, (1000, 2000))
    os.utime(newer, (1000, 3000))
    enforce_cache_budget(4)
    assert not older.exists() and newer.exists()


def test_tracklist_prefetch_is_capped_and_says_so(caplog):
    import logging

    from app.want_metadata import MAX_GROUP_COVERS

    groups = [{"mbid": f"g{i}"} for i in range(MAX_GROUP_COVERS + 40)]
    fetched = []
    submitted = {}
    caplog.set_level(logging.INFO, logger="app")
    with (
        patch.object(catalog, "release_groups", return_value=groups),
        patch.object(catalog, "tracklist", side_effect=lambda db, mbid: fetched.append(mbid)),
        patch("app.provider_queue.submit", side_effect=lambda key, job: submitted.update(job=job)),
    ):
        catalog.prefetch_tracklists_in_background("artist-1")
        submitted["job"]()
    assert len(fetched) == MAX_GROUP_COVERS == 150
    assert fetched[-1] == f"g{MAX_GROUP_COVERS - 1}"
    assert any("capped at 150 of 190" in r.getMessage() for r in caplog.records)


def _history_fixture(db_session):
    """One track, and a play recorded `days` ago for each age given."""
    from app.models import LibraryRoot, MediaItem, MediaKind

    user = db_session.query(User).filter(User.username == DEFAULT_USERNAME).one()
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="t.mp3", title="T")
    db_session.add(item)
    db_session.commit()
    return user, item


def _add_plays(db_session, user, item, ages_in_days):
    from app.models import PlayHistory

    now = utcnow()
    for age in ages_in_days:
        db_session.add(PlayHistory(user_id=user.id, media_item_id=item.id, played_at=now - timedelta(days=age)))
    db_session.commit()


def _play_ages(db_session) -> list[int]:
    from app.models import PlayHistory

    now = utcnow()
    return sorted(round((now - p.played_at) / timedelta(days=1)) for p in db_session.query(PlayHistory))


def test_maintenance_refreshes_the_query_planner_statistics(db_session):
    # Nothing else ever runs ANALYZE.
    from sqlalchemy import event, text

    statements: list[str] = []

    def record(_conn, _cursor, statement, *_rest):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        main._run_store_maintenance(db_session)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert any(s.startswith("PRAGMA optimize") for s in statements)
    assert db_session.execute(text("SELECT count(*) FROM sqlite_stat1")).scalar() > 0


def test_maintenance_survives_a_failing_statistics_refresh(db_session):
    db = MagicMock()
    db.execute.side_effect = RuntimeError("locked")
    with (
        patch.object(main, "enforce_cache_budget"),
        patch.object(main, "prune_expired"),
        patch.object(main, "purge_expired_sessions"),
        patch.object(main, "prune_play_history"),
        patch.object(main, "prune_activity_events"),
    ):
        main._run_store_maintenance(db)  # must not raise
    db.rollback.assert_called_once()


def test_play_history_retention_is_off_by_default(db_session):
    from app.cleanup import prune_play_history

    assert settings.play_history_retention_days == 0
    user, item = _history_fixture(db_session)
    _add_plays(db_session, user, item, [1, 400, 4000])
    assert prune_play_history(db_session) == 0
    main._run_store_maintenance(db_session)
    assert _play_ages(db_session) == [1, 400, 4000]


def test_play_history_retention_rejects_a_negative_value(monkeypatch):
    import pytest
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.setenv("ATRIOLYRIC_PLAY_HISTORY_RETENTION_DAYS", "-1")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("ATRIOLYRIC_PLAY_HISTORY_RETENTION_DAYS", "30")
    assert Settings().play_history_retention_days == 30


def test_play_history_older_than_the_retention_is_deleted_and_newer_kept(db_session, monkeypatch, caplog):
    import logging

    user, item = _history_fixture(db_session)
    _add_plays(db_session, user, item, [0, 29, 31, 365])
    monkeypatch.setattr(settings, "play_history_retention_days", 30)
    with caplog.at_level(logging.INFO, logger="app.cleanup"):
        main._run_store_maintenance(db_session)
    assert _play_ages(db_session) == [0, 29]
    assert "Pruned 2 play history rows older than 30 days" in caplog.text


def test_play_history_is_pruned_in_batches(db_session, monkeypatch):
    from app import cleanup

    user, item = _history_fixture(db_session)
    _add_plays(db_session, user, item, [100] * 7 + [1])
    commits = MagicMock(wraps=db_session.commit)
    monkeypatch.setattr(db_session, "commit", commits)
    assert cleanup.prune_play_history(db_session, retention_days=10, batch_size=3) == 7
    # 3 + 3 + 1: one commit per batch, the short batch ends the loop.
    assert commits.call_count == 3
    assert _play_ages(db_session) == [1]


def test_play_history_prune_leaves_source_history_and_scrobble_queue(db_session):
    from app.cleanup import prune_play_history
    from app.models import PlaySourceHistory, ScrobbleQueueItem

    user, item = _history_fixture(db_session)
    _add_plays(db_session, user, item, [100])
    old = utcnow() - timedelta(days=100)
    db_session.add(PlaySourceHistory(user_id=user.id, kind="album", label="A", payload_json={}, updated_at=old))
    db_session.add(ScrobbleQueueItem(user_id=user.id, artist="A", title="T", started_at=old, created_at=old))
    db_session.commit()
    assert prune_play_history(db_session, retention_days=10) == 1
    assert _play_ages(db_session) == []
    assert db_session.query(PlaySourceHistory).count() == 1
    assert db_session.query(ScrobbleQueueItem).count() == 1
