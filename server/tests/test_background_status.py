"""How far the background passes have got (app.background_status), the
lines they log as they start, and the admin endpoint."""

from __future__ import annotations

import logging
import threading

import pytest
from sqlalchemy import event

from app import audio_analysis, background_status, loudness
from app.audio_features import ANALYSIS_VERSION
from app.models import AudioFeatures, LibraryRoot, MediaItem, MediaKind, User, utcnow
from app.security import hash_password
from tests.conftest import login_as


@pytest.fixture()
def library(db_session):
    """Six tracks and a video: loudness 2 done, 1 failed, 3 pending; audio
    analysis 1 done, 1 failed, and 4 pending (one never analysed, one by an
    older version, one whose file changed, one with no row)."""
    root = LibraryRoot(path="/music", display_name="Music")
    db_session.add(root)
    db_session.commit()
    now = utcnow()

    def item(name, kind=MediaKind.track, **values):
        row = MediaItem(
            library_root_id=root.id, kind=kind, relative_path=name, title=name,
            file_mtime=1.0, file_size=10, **values,
        )
        db_session.add(row)
        db_session.flush()
        return row

    a = item("a", replaygain_track_gain=-3.0, loudness_scanned_at=now)
    b = item("b", replaygain_track_gain=-1.0, loudness_scanned_at=now)
    c = item("c", loudness_scanned_at=now)
    d = item("d")
    e = item("e")
    item("f")
    video = item("v", kind=MediaKind.video, replaygain_track_gain=1.0)

    def features(row, **values):
        db_session.add(
            AudioFeatures(
                media_item_id=row.id, version=values.pop("version", ANALYSIS_VERSION),
                source_mtime=values.pop("source_mtime", 1.0), source_size=10, **values,
            )
        )

    features(a, vector=b"\0" * 8)
    features(b, error="ffmpeg exited 1")
    features(c, vector=b"\0" * 8, version=ANALYSIS_VERSION - 1)
    features(d, vector=b"\0" * 8, source_mtime=2.0)
    features(e, error="too short", version=ANALYSIS_VERSION - 1)
    features(video, vector=b"\0" * 8)
    db_session.commit()
    return root


@pytest.fixture()
def idle_passes(monkeypatch):
    """Both passes not running, audio analysis on at concurrency 1."""
    monkeypatch.setattr(loudness, "_run_lock", threading.Lock())
    monkeypatch.setattr(audio_analysis, "_run_lock", threading.Lock())
    monkeypatch.setattr(loudness, "progress", background_status.RunProgress())
    monkeypatch.setattr(audio_analysis, "progress", background_status.RunProgress())
    monkeypatch.setattr(audio_analysis.settings, "audio_analysis", True)
    monkeypatch.setattr(audio_analysis.settings, "audio_analysis_concurrency", 1)


def test_counts_split_every_track_into_done_failed_and_pending(db_session, library):
    counts = background_status.library_counts(db_session)
    assert counts == background_status.LibraryCounts(
        tracks=6, loudness_done=2, loudness_failed=1, analysis_done=1, analysis_failed=1
    )
    assert (counts.loudness_pending, counts.analysis_pending) == (3, 4)
    # The same pending tracks the analysis pass itself would pick.
    assert counts.analysis_pending == audio_analysis.pending_count(db_session)


def test_counts_on_an_empty_library_are_zero(db_session):
    assert background_status.library_counts(db_session) == background_status.LibraryCounts(0, 0, 0, 0, 0)


def test_counts_are_one_query_and_cached(db_session, library, monkeypatch):
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        first = background_status.library_counts(db_session)
        assert len(statements) == 1
        assert background_status.library_counts(db_session) == first
        assert len(statements) == 1
        background_status.library_counts(db_session, fresh=True)
        assert len(statements) == 2
        # Once the cache is older than CACHE_SECONDS it is read again.
        clock = background_status.time.monotonic() + background_status.CACHE_SECONDS + 1
        monkeypatch.setattr(background_status.time, "monotonic", lambda: clock)
        background_status.library_counts(db_session)
        assert len(statements) == 3
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_status_when_nothing_runs(db_session, library, idle_passes):
    status = background_status.background_status(db_session)
    assert status["loudness"] == {
        "state": "idle", "total": 6, "done": 2, "failed": 1, "pending": 3,
        "run_checked": None, "run_stored": None, "rate_per_minute": None,
    }
    assert status["audio_analysis"]["state"] == "idle"
    assert (status["audio_analysis"]["done"], status["audio_analysis"]["pending"]) == (1, 4)


def test_status_states(db_session, library, idle_passes, monkeypatch):
    loudness._run_lock.acquire()
    audio_analysis._run_lock.acquire()
    try:
        status = background_status.background_status(db_session)
        assert status["loudness"]["state"] == "running"
        # At concurrency 1 analysis waits for the loudness pass.
        assert status["audio_analysis"]["state"] == "waiting"
        monkeypatch.setattr(audio_analysis.settings, "audio_analysis_concurrency", 2)
        audio_analysis.progress.begin()
        assert background_status.background_status(db_session)["audio_analysis"]["state"] == "running"
        monkeypatch.setattr(audio_analysis.settings, "audio_analysis", False)
        assert background_status.background_status(db_session)["audio_analysis"]["state"] == "disabled"
    finally:
        loudness._run_lock.release()
        audio_analysis._run_lock.release()


def test_rate_is_reported_only_once_the_run_has_gone_on_long_enough(
    db_session, library, idle_passes, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr(background_status.time, "monotonic", lambda: clock[0])
    loudness._run_lock.acquire()
    try:
        loudness.progress.begin()
        clock[0] += 60
        loudness._log_progress(100, 1000, 90)
        status = background_status.background_status(db_session)["loudness"]
        assert (status["run_checked"], status["run_stored"], status["rate_per_minute"]) == (100, 90, None)
        clock[0] += 540  # ten minutes in, 400 tracks
        loudness._log_progress(400, 1000, 380)
        assert background_status.background_status(db_session)["loudness"]["rate_per_minute"] == 40.0
        loudness.progress.end()
    finally:
        loudness._run_lock.release()
    status = background_status.background_status(db_session)["loudness"]
    assert (status["run_checked"], status["rate_per_minute"]) == (None, None)


def test_loudness_pass_logs_where_the_library_stands_as_it_starts(
    db_session, session_factory, library, caplog
):
    caplog.set_level(logging.INFO, logger="app.loudness")
    loudness.backfill_loudness(session_factory, lambda _path: None)
    assert "Loudness: 2 of 6 tracks have a value, 1 could not be measured, 3 to go" in caplog.text


def test_loudness_start_line_groups_thousands(db_session, monkeypatch, caplog):
    big = background_status.LibraryCounts(135402, 14210, 318, 0, 0)
    monkeypatch.setattr(loudness, "library_counts", lambda db, fresh=False: big)
    caplog.set_level(logging.INFO, logger="app.loudness")
    loudness._log_state(db_session)
    assert "Loudness: 14,210 of 135,402 tracks have a value, 318 could not be measured, 120,874 to go" in caplog.text


def test_audio_pass_logs_where_the_library_stands_only_with_work_to_do(
    db_session, session_factory, library, caplog, monkeypatch
):
    caplog.set_level(logging.INFO, logger="app.audio_analysis")
    # Every file is there but unusable, so each is recorded as a failure.
    monkeypatch.setattr(audio_analysis, "media_file_path", lambda item: None)
    monkeypatch.setattr(audio_analysis, "media_file_absent", lambda item: False)
    audio_analysis.analyse_pending(session_factory, lambda *a: pytest.fail("no file to analyse"))
    assert "Audio analysis: 1 of 6 tracks analysed, 1 could not be analysed, 4 to go" in caplog.text
    caplog.clear()
    audio_analysis.analyse_pending(session_factory, lambda *a: pytest.fail("nothing pending"))
    assert "Audio analysis:" not in caplog.text


def test_progress_lines_include_values_stored(idle_passes, caplog):
    caplog.set_level(logging.INFO)
    loudness._log_progress(3000, 120298, 2987)
    audio_analysis._log_progress(400, 12873, 396)
    assert "Loudness backfill: 3000 / 120298 tracks checked, 2987 values stored" in caplog.text
    assert "Audio analysis: 400 / 12873 tracks, 396 analysed" in caplog.text


def test_endpoint_is_admin_only(client, raw_client, db_session, session_factory, library, idle_passes):
    response = client.get("/api/admin/background-status")
    assert response.status_code == 200
    body = response.json()
    assert body["loudness"]["pending"] == 3
    assert body["audio_analysis"] == {
        "state": "idle", "total": 6, "done": 1, "failed": 1, "pending": 4,
        "run_checked": None, "run_stored": None, "rate_per_minute": None,
    }
    db_session.add(User(username="plain", password_hash=hash_password("plainpass")))
    db_session.commit()
    plain = login_as(session_factory, raw_client, "plain", "plainpass")
    assert plain.get("/api/admin/background-status").status_code == 403


def test_a_polling_page_gets_the_counts_in_hand_and_stale_ones_are_replaced_in_the_background(
    db_session, monkeypatch
):
    """The Admin page never waits on the count query: counts older than
    CACHE_SECONDS are still what it gets, and a background thread fetches
    new ones for its next poll. Only the very first call runs the query."""
    queries: list[int] = []
    real = background_status._query_counts

    def counting(db):
        queries.append(1)
        return real(db)

    monkeypatch.setattr(background_status, "_query_counts", counting)
    background_status.clear_cache()

    first = background_status.polled_counts(db_session)  # nothing in hand yet
    assert len(queries) == 1
    assert background_status.polled_counts(db_session) is first  # fresh: no query
    assert len(queries) == 1

    stamp, counts = background_status._cache
    background_status._cache = (stamp - background_status.CACHE_SECONDS - 1, counts)
    assert background_status.polled_counts(db_session) is first  # stale, but at once
    assert background_status.wait_for_refresh()
    assert len(queries) == 2
    assert background_status._cache[0] > stamp - 1  # replaced for the next poll


def test_a_failed_background_refresh_keeps_the_old_counts(db_session, monkeypatch, caplog):
    first = background_status.polled_counts(db_session)
    stamp, counts = background_status._cache
    background_status._cache = (stamp - background_status.CACHE_SECONDS - 1, counts)

    def broken(db):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(background_status, "_query_counts", broken)
    assert background_status.polled_counts(db_session) is first
    assert background_status.wait_for_refresh()
    assert "Could not refresh" in caplog.text
    assert background_status.polled_counts(db_session) is first
    assert background_status.wait_for_refresh()
