from __future__ import annotations

import threading
from threading import Event

import pytest

from app import background


def test_spawned_threads_are_named_daemons_and_joined(monkeypatch):
    monkeypatch.setattr(background, "_threads", [])  # only this test's threads
    ran = Event()
    thread = background.spawn("test-worker", ran.set)
    assert thread.name == "test-worker" and thread.daemon
    assert background.join_all(timeout=5) == []
    assert ran.is_set()


def test_join_all_gives_up_after_the_timeout_and_names_what_still_runs(monkeypatch):
    monkeypatch.setattr(background, "_threads", [])
    release = Event()
    try:
        background.spawn("test-slow", release.wait, 10)
        assert background.join_all(timeout=0.05) == ["test-slow"]
    finally:
        release.set()
    assert background.join_all(timeout=5) == []


def test_finished_threads_are_forgotten(monkeypatch):
    monkeypatch.setattr(background, "_threads", [])
    first = background.spawn("test-first", lambda: None)
    first.join(5)
    background.spawn("test-second", lambda: None)
    assert first not in background._threads



def _record_spawns(monkeypatch, module: str, names: list[str]) -> None:
    monkeypatch.setattr(f"{module}.spawn", lambda name, target, *a: names.append(name))


@pytest.mark.real_background_jobs
def test_spawn_sites_go_through_the_registry(monkeypatch):
    """Every background thread is registered, so shutdown can wait for it."""
    from app import hls, listenbrainz_data, loudness, main

    names: list[str] = []
    for module in ("app.main", "app.loudness", "app.hls", "app.listenbrainz_data"):
        _record_spawns(monkeypatch, module, names)
    main._start_art_thumb_backfill()
    monkeypatch.setattr(loudness, "_run_lock", threading.Lock())
    loudness.start_loudness_backfill()
    monkeypatch.setattr(hls, "_janitor_started", False)
    hls._ensure_janitor()
    hls._ensure_janitor()  # started once only
    monkeypatch.setattr(listenbrainz_data, "_prefetching", set())
    listenbrainz_data.prefetch_similar_in_background(1)
    assert names == ["art-thumb-backfill", "loudness-backfill", "hls-janitor", "listenbrainz-similar-1"]


def test_shutdown_waits_for_background_threads_and_names_stragglers(
    session_factory, db_session, monkeypatch, caplog
):
    from fastapi.testclient import TestClient

    from app import main

    waited: list[float] = []

    def fake_join_all(timeout: float) -> list[str]:
        waited.append(timeout)
        return ["slow-scan"]

    monkeypatch.setattr(main, "join_all", fake_join_all)
    with caplog.at_level("WARNING", logger="app.main"), TestClient(main.app):
        assert waited == []
    assert waited == [10]
    assert "slow-scan" in caplog.text
