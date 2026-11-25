"""The background audio-analysis pass (app.audio_analysis) and its table."""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import event, select, text

from app import audio_analysis as aa
from app.audio_features import (
    ANALYSIS_VERSION,
    FEATURE_COUNT,
    AnalysisError,
    FeatureVector,
)
from app.cleanup import delete_media_records
from app.models import (
    AudioFeatures,
    LibraryRoot,
    MediaItem,
    MediaKind,
    PlayHistory,
    User,
    utcnow,
)


def vector(seed: int = 0) -> FeatureVector:
    values = np.random.default_rng(seed).random(FEATURE_COUNT).astype(np.float32)
    values[0] = 120.0  # tempo
    values[13] = 7.0  # key
    values[14] = 1.0  # mode
    return FeatureVector(values=values, analysed_seconds=90.0)


class FakeAnalyser:
    """Stands in for analyse_file: records calls, fails for chosen names."""

    def __init__(self, fail: tuple[str, ...] = (), on_call=None):
        self.calls: list[str] = []
        self.fail = fail
        self.on_call = on_call
        self.lock = threading.Lock()

    def __call__(self, path: Path, duration, stop) -> FeatureVector:
        with self.lock:
            self.calls.append(path.name)
        if self.on_call is not None:
            self.on_call(path, stop)
        if path.name in self.fail:
            raise AnalysisError("ffmpeg exited 1")
        return vector(len(path.name))


@pytest.fixture()
def tracks(db_session, tmp_path):
    """Make `n` tracks with real (empty) files; returns their ids in order."""
    root = LibraryRoot(path=str(tmp_path), display_name="Music")
    db_session.add(root)
    db_session.commit()

    def make(n: int, kind: MediaKind = MediaKind.track) -> list[int]:
        start = db_session.query(MediaItem).count()
        items = []
        for i in range(start, start + n):
            (tmp_path / f"t{i:03}.flac").write_bytes(b"x")
            items.append(
                MediaItem(
                    library_root_id=root.id,
                    relative_path=f"t{i:03}.flac",
                    kind=kind,
                    title=f"t{i:03}",
                    duration_seconds=200.0,
                    file_mtime=1.0,
                    file_size=1,
                )
            )
        db_session.add_all(items)
        db_session.commit()
        return [item.id for item in items]

    return make


def rows(db_session) -> dict[int, AudioFeatures]:
    db_session.expire_all()
    return {r.media_item_id: r for r in db_session.scalars(select(AudioFeatures))}


def test_pass_stores_vectors_scalars_and_failures(db_session, session_factory, tracks):
    ids = tracks(3)
    analyser = FakeAnalyser(fail=("t001.flac",))
    assert aa.analyse_pending(session_factory, analyser) == (2, 1)
    stored = rows(db_session)
    good, bad = stored[ids[0]], stored[ids[1]]
    assert good.error is None and good.version == ANALYSIS_VERSION
    assert FeatureVector.values_from_blob(good.vector) is not None
    assert (good.tempo_bpm, good.musical_key, good.mode, good.analysed_seconds) == (120.0, 7, 1, 90.0)
    assert (good.source_mtime, good.source_size) == (1.0, 1)
    assert bad.error == "ffmpeg exited 1" and bad.vector is None and bad.tempo_bpm is None
    # Nothing is pending now: the failure is remembered, not retried.
    assert aa.pending_count(db_session) == 0
    assert aa.analyse_pending(session_factory, analyser) == (0, 0)
    assert len(analyser.calls) == 3


def test_changed_files_and_a_new_version_are_analysed_again(db_session, session_factory, tracks, monkeypatch):
    ids = tracks(3)
    aa.analyse_pending(session_factory, FakeAnalyser())
    db_session.execute(text("UPDATE media_items SET file_mtime = 2.0 WHERE id = :id"), {"id": ids[0]})
    db_session.execute(text("UPDATE media_items SET file_size = 5 WHERE id = :id"), {"id": ids[1]})
    db_session.commit()
    assert aa.pending_ids(db_session) == ids[:2]
    analyser = FakeAnalyser()
    aa.analyse_pending(session_factory, analyser)
    assert analyser.calls == ["t000.flac", "t001.flac"]
    assert rows(db_session)[ids[0]].source_mtime == 2.0
    monkeypatch.setattr(aa, "ANALYSIS_VERSION", ANALYSIS_VERSION + 1)
    assert aa.pending_count(db_session) == 3


def test_only_tracks_are_recorded_and_unusable_files_as_failures(
    db_session, session_factory, tracks, tmp_path, tmp_path_factory
):
    ids = tracks(2)
    tracks(1, kind=MediaKind.video)
    # There, but leading out of its library folder.
    outside = tmp_path_factory.mktemp("elsewhere") / "t001.flac"
    outside.write_bytes(b"x")
    (tmp_path / "t001.flac").unlink()
    (tmp_path / "t001.flac").symlink_to(outside)
    analyser = FakeAnalyser()
    assert aa.analyse_pending(session_factory, analyser) == (1, 1)
    assert analyser.calls == ["t000.flac"]
    assert rows(db_session)[ids[1]].error == "file missing or outside its library folder"
    assert set(rows(db_session)) == set(ids)


# --- an unreachable file is not a failed analysis ----------------------------


@pytest.fixture(autouse=True)
def _no_carried_over_pass_state(monkeypatch):
    monkeypatch.setattr(aa, "_timed_out_at", {})
    monkeypatch.setattr(aa, "_stuck", None)


def test_a_missing_file_stores_nothing_and_is_analysed_once_it_is_back(
    db_session, session_factory, tracks, tmp_path, caplog
):
    ids = tracks(3)
    (tmp_path / "t001.flac").unlink()
    seen: list[tuple[int, int, int]] = []
    caplog.set_level(logging.INFO, logger="app.audio_analysis")
    analyser = FakeAnalyser()
    assert aa.analyse_pending(
        session_factory, analyser, progress_cb=lambda d, t, s: seen.append((d, t, s))
    ) == (2, 0)
    assert set(rows(db_session)) == {ids[0], ids[2]}
    assert aa.pending_ids(db_session) == [ids[1]]
    assert seen == [(2, 2, 2)]
    assert "Audio analysis left 1 tracks pending: files missing" in caplog.text
    (tmp_path / "t001.flac").write_bytes(b"x")
    assert aa.analyse_pending(session_factory, analyser) == (1, 0)
    assert set(rows(db_session)) == set(ids)


def test_a_file_that_goes_away_while_it_is_read_stores_nothing(db_session, session_factory, tracks):
    ids = tracks(2)

    def share_drops_out(path, _stop):
        if path.name == "t000.flac":
            path.unlink()
            raise AnalysisError("ffmpeg exited 1")

    assert aa.analyse_pending(session_factory, FakeAnalyser(on_call=share_drops_out)) == (1, 0)
    assert set(rows(db_session)) == {ids[1]}


def test_a_timeout_stores_nothing_and_is_not_tried_again_straight_away(
    db_session, session_factory, tracks, monkeypatch
):
    ids = tracks(2)

    def times_out(path, _stop):
        if path.name == "t000.flac":
            raise AnalysisError(aa.TIMED_OUT)

    analyser = FakeAnalyser(on_call=times_out)
    assert aa.analyse_pending(session_factory, analyser) == (1, 0)
    assert set(rows(db_session)) == {ids[1]}
    assert aa.pending_ids(db_session) == [ids[0]]
    # The next pass (ten minutes later) leaves it alone...
    assert aa.analyse_pending(session_factory, analyser) == (0, 0)
    assert analyser.calls.count("t000.flac") == 1
    # ...a much later one tries again.
    monkeypatch.setattr(aa, "TIMEOUT_RETRY_SECONDS", 0.0)
    assert aa.analyse_pending(session_factory, FakeAnalyser()) == (1, 0)
    assert set(rows(db_session)) == set(ids)


def test_the_timeout_reason_is_the_one_the_analyser_gives(tmp_path, monkeypatch):
    # TIMED_OUT is matched against audio_features' message.
    from app import audio_features

    source = Path(audio_features.__file__).read_text()
    assert f'raise AnalysisError("{aa.TIMED_OUT}")' in source


def test_an_unmounted_library_keeps_its_backlog_and_is_looked_at_once(
    db_session, session_factory, tracks, tmp_path, tmp_path_factory, caplog, monkeypatch
):
    here = tracks(3)
    # A second library whose share is not mounted: an existing, empty folder.
    nas = tmp_path_factory.mktemp("nas")
    root = LibraryRoot(path=str(nas), display_name="NAS")
    db_session.add(root)
    db_session.commit()
    away = [
        MediaItem(
            library_root_id=root.id,
            relative_path=f"n{i:03}.flac",
            kind=MediaKind.track,
            title=f"n{i:03}",
            file_mtime=1.0,
            file_size=1,
        )
        for i in range(60)
    ]
    db_session.add_all(away)
    # Played, so the pass comes to them first.
    user = db_session.query(User).one()
    db_session.flush()
    db_session.add_all(
        PlayHistory(user_id=user.id, media_item_id=item.id, played_at=utcnow()) for item in away
    )
    db_session.commit()
    looked: list[str] = []
    real = aa.media_file_path

    def counting(item, **kwargs):
        looked.append(item.relative_path)
        return real(item, **kwargs)

    monkeypatch.setattr(aa, "media_file_path", counting)
    seen: list[tuple[int, int, int]] = []
    caplog.set_level(logging.INFO, logger="app.audio_analysis")
    analyser = FakeAnalyser()
    assert aa.analyse_pending(
        session_factory, analyser, progress_cb=lambda d, t, s: seen.append((d, t, s))
    ) == (3, 0)
    assert set(rows(db_session)) == set(here)
    assert aa.pending_count(db_session) == 60
    assert len([name for name in looked if name.startswith("n")]) == 1
    assert seen == [(3, 3, 3)]
    assert f"Audio analysis left 60 tracks pending: library folder not reachable: {nas}" in caplog.text

    # The pass ten minutes later finds the same and does not say it again.
    caplog.clear()
    assert aa.analyse_pending(session_factory, analyser) == (0, 0)
    assert caplog.text == ""

    # The share is back.
    for item in away:
        (nas / item.relative_path).write_bytes(b"x")
    assert aa.analyse_pending(session_factory, analyser) == (60, 0)
    assert aa.pending_count(db_session) == 0


def test_missing_files_at_the_head_of_the_queue_do_not_hide_the_rest(
    db_session, session_factory, tracks, tmp_path, monkeypatch
):
    # Left pending, they stay first in line: more of them than one round
    # holds must not end the pass before it reaches the others.
    monkeypatch.setattr(aa, "ROUND_SIZE", 5)
    monkeypatch.setattr(aa, "BATCH_SIZE", 5)
    ids = tracks(15)
    for i in range(12):
        (tmp_path / f"t{i:03}.flac").unlink()
    analyser = FakeAnalyser()
    assert aa.analyse_pending(session_factory, analyser) == (3, 0)
    assert set(rows(db_session)) == set(ids[12:])


def test_a_run_of_missing_files_sets_the_folder_aside_for_the_pass(
    db_session, session_factory, tracks, tmp_path, monkeypatch
):
    monkeypatch.setattr(aa.loudness, "MISSING_STREAK_PAUSE", 4)
    monkeypatch.setattr(aa, "BATCH_SIZE", 2)
    ids = tracks(10)
    for i in range(6):
        (tmp_path / f"t{i:03}.flac").unlink()
    analyser = FakeAnalyser()
    assert aa.analyse_pending(session_factory, analyser) == (0, 0)
    assert analyser.calls == []
    assert aa.pending_count(db_session) == len(ids)


def test_played_tracks_come_first(db_session, tracks):
    ids = tracks(5)
    user = db_session.scalars(select(User)).first()
    now = utcnow()
    plays = [
        (ids[3], now - timedelta(days=10)),
        (ids[3], now - timedelta(days=9)),
        (ids[1], now - timedelta(hours=1)),  # most recent
        (ids[4], now - timedelta(days=9)),  # same day as ids[3]'s last, fewer plays
    ]
    db_session.add_all(PlayHistory(user_id=user.id, media_item_id=m, played_at=at) for m, at in plays)
    db_session.commit()
    assert aa.pending_ids(db_session) == [ids[1], ids[3], ids[4], ids[0], ids[2]]
    assert aa.pending_ids(db_session, limit=2) == [ids[1], ids[3]]


def test_a_track_deleted_mid_pass_is_skipped(db_session, session_factory, tracks):
    ids = tracks(3)

    def delete_second(path, stop):
        if path.name == "t001.flac":
            other = session_factory()
            delete_media_records(other, [ids[1]])
            other.commit()
            other.close()

    assert aa.analyse_pending(session_factory, FakeAnalyser(on_call=delete_second)) == (3, 0)
    assert set(rows(db_session)) == {ids[0], ids[2]}


def test_cleanup_deletes_the_row_with_the_track(db_session, session_factory, tracks):
    ids = tracks(2)
    aa.analyse_pending(session_factory, FakeAnalyser())
    delete_media_records(db_session, [ids[0]])
    db_session.commit()
    assert set(rows(db_session)) == {ids[1]}


def test_stop_ends_the_pass_and_a_new_one_resumes(db_session, session_factory, tracks):
    ids = tracks(30)
    stop = threading.Event()

    def stop_at_third(path, stop_event):
        if path.name == "t002.flac":
            stop.set()
            raise AnalysisError("stopped")

    first = FakeAnalyser(on_call=stop_at_third)
    aa.analyse_pending(session_factory, first, stop=stop)
    assert first.calls == ["t000.flac", "t001.flac", "t002.flac"]
    # The two finished ones were committed; the interrupted one was not
    # recorded as a failure.
    assert set(rows(db_session)) == set(ids[:2])
    resumed = FakeAnalyser()
    assert aa.analyse_pending(session_factory, resumed) == (28, 0)
    assert resumed.calls[0] == "t002.flac"


def test_waits_while_loudness_runs_unless_concurrency_allows(db_session, session_factory, tracks, monkeypatch):
    tracks(2)
    monkeypatch.setattr(aa, "_loudness_busy", lambda: True)
    monkeypatch.setattr(aa, "SLOT_POLL_SECONDS", 0.01)
    stop = threading.Event()
    timer = threading.Timer(0.2, stop.set)
    timer.start()
    analyser = FakeAnalyser()
    assert aa.analyse_pending(session_factory, analyser, stop=stop, concurrency=1) == (0, 0)
    timer.join()
    assert analyser.calls == []
    # A second worker may run alongside the loudness backfill.
    assert aa.analyse_pending(session_factory, analyser, concurrency=2) == (2, 0)
    monkeypatch.setattr(aa, "_loudness_busy", lambda: False)
    tracks(40)
    parallel = FakeAnalyser()
    assert aa.analyse_pending(session_factory, parallel, concurrency=3) == (40, 0)
    assert len(set(parallel.calls)) == 40


def test_loudness_busy_reads_the_backfill_lock(monkeypatch):
    from app import loudness

    lock = threading.Lock()
    monkeypatch.setattr(loudness, "_run_lock", lock)
    assert not aa._loudness_busy()
    with lock:
        assert aa._loudness_busy()


def test_progress_is_reported_every_hundred_tracks(db_session, session_factory, tracks, caplog):
    tracks(130)
    seen: list[tuple[int, int]] = []
    aa.analyse_pending(session_factory, FakeAnalyser(), progress_cb=lambda d, t, s: seen.append((d, t, s)))
    assert seen == [(100, 130, 100), (130, 130, 130)]
    caplog.set_level(logging.INFO, logger="app.audio_analysis")
    aa._log_progress(100, 130, 98)
    assert "Audio analysis: 100 / 130 tracks, 98 analysed" in caplog.text


def test_statements_per_batch_do_not_grow_with_tracks(db_session, session_factory, tracks):
    engine = session_factory.kw["bind"]
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    def run(n: int) -> int:
        tracks(n)
        statements.clear()
        event.listen(engine, "before_cursor_execute", record)
        try:
            aa.analyse_pending(session_factory, FakeAnalyser())
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    one_batch = run(aa.BATCH_SIZE)
    two_batches = run(2 * aa.BATCH_SIZE)
    # The second batch adds the same few statements (items, their roots,
    # their feature rows, one executemany upsert), none per track.
    per_batch = two_batches - one_batch
    assert per_batch <= 4
    assert one_batch <= 4 + per_batch


def test_migration_step_is_idempotent(db_session):
    from app import migrations

    migrations.ensure_audio_features_indexes()
    migrations.ensure_audio_features_indexes()
    indexes = {row[1] for row in db_session.execute(text("PRAGMA index_list(audio_features)"))}
    assert "ix_audio_features_analysed_at" in indexes


# --- the thread ---


def test_disabled_setting_starts_nothing(monkeypatch, caplog):
    spawned: list[str] = []
    monkeypatch.setattr(aa, "spawn", lambda name, target: spawned.append(name))
    monkeypatch.setattr(aa.settings, "audio_analysis", False)
    caplog.set_level(logging.INFO, logger="app.audio_analysis")
    aa.start_audio_analysis()
    assert spawned == []
    assert "turned off" in caplog.text


def test_start_spawns_one_registered_thread_and_stop_ends_it(monkeypatch):
    monkeypatch.setattr(aa, "_run_lock", threading.Lock())
    monkeypatch.setattr(aa, "_stop", threading.Event())
    monkeypatch.setattr(aa, "_wake", threading.Event())
    monkeypatch.setattr(aa, "_loudness_busy", lambda: False)
    runs = threading.Semaphore(0)

    def fake_pass(stop=None, progress_cb=None):
        runs.release()
        return (1, 0)

    monkeypatch.setattr(aa, "analyse_pending", fake_pass)
    threads: list[threading.Thread] = []

    def spawn(name, target):
        thread = threading.Thread(target=target, name=name, daemon=True)
        threads.append(thread)
        thread.start()
        return thread

    monkeypatch.setattr(aa, "spawn", spawn)
    aa.start_audio_analysis()
    assert runs.acquire(timeout=5)
    aa.start_audio_analysis()  # wakes the idle thread instead of a second one
    assert runs.acquire(timeout=5)
    assert [t.name for t in threads] == ["audio-analysis"]
    aa.stop_audio_analysis()
    threads[0].join(5)
    assert not threads[0].is_alive()
    assert not aa._run_lock.locked()


def test_the_thread_waits_for_loudness_before_any_query(monkeypatch):
    monkeypatch.setattr(aa, "_run_lock", threading.Lock())
    monkeypatch.setattr(aa, "_stop", threading.Event())
    monkeypatch.setattr(aa, "_wake", threading.Event())
    monkeypatch.setattr(aa, "LOUDNESS_POLL_SECONDS", 0.01)
    busy = threading.Event()
    busy.set()
    monkeypatch.setattr(aa, "_loudness_busy", busy.is_set)
    ran = threading.Event()
    monkeypatch.setattr(aa, "analyse_pending", lambda **kw: (ran.set(), (0, 0))[1])
    threads: list[threading.Thread] = []

    def spawn(name, target):
        threads.append(threading.Thread(target=target, daemon=True))
        threads[-1].start()

    monkeypatch.setattr(aa, "spawn", spawn)
    aa.start_audio_analysis()
    assert not ran.wait(0.2)
    busy.clear()
    assert ran.wait(5)
    aa.stop_audio_analysis()
    threads[0].join(5)  # before monkeypatch puts the real lock back
    assert not threads[0].is_alive()
