"""app.log_mirror copies the application log into the admin's stored
activity log: warnings, failed and slow requests, errors and what the
background work is doing - without the per-request and per-frame lines."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from app import log_mirror
from app.activity import begin_request, set_actor
from app.activity_log import record_event, wait_for_writes
from app.models import ActivityEvent


@pytest.fixture()
def mirror(db_session, monkeypatch):
    """The handler installed on the `app` logger, which lets INFO through."""
    app_logger = logging.getLogger("app")
    monkeypatch.setattr(app_logger, "level", logging.INFO)
    logging.getLogger("app").manager._clear_cache()  # type: ignore[attr-defined]
    log_mirror.install_log_mirror()
    yield
    log_mirror.remove_log_mirror()
    logging.getLogger("app").manager._clear_cache()  # type: ignore[attr-defined]


def _events(db_session) -> list[ActivityEvent]:
    assert wait_for_writes(10)
    db_session.expire_all()
    return list(db_session.scalars(select(ActivityEvent).order_by(ActivityEvent.id)))


def _stored(db_session) -> list[tuple[str, str, str]]:
    return [(e.kind, e.level, e.summary) for e in _events(db_session)]


def test_warnings_errors_and_background_info_are_stored(mirror, db_session):
    logging.getLogger("app.loudness").info("Loudness backfill finished: %d tracks", 12)
    logging.getLogger("app.metadata_providers.listenbrainz").warning("ListenBrainz did not answer (%s)", "timeout")
    logging.getLogger("app.routers.radio").error("Stream relay failed")

    assert _stored(db_session) == [
        ("log.loudness", "info", "Loudness backfill finished: 12 tracks"),
        ("log.metadata_providers", "warning", "ListenBrainz did not answer (timeout)"),
        ("log.radio", "error", "Stream relay failed"),
    ]


def test_an_exception_is_stored_as_its_type_and_message_not_a_traceback(mirror, db_session):
    try:
        raise ValueError("bad cover")
    except ValueError:
        logging.getLogger("app.art").exception("Could not make a thumbnail")

    assert _stored(db_session) == [("log.art", "error", "Could not make a thumbnail (ValueError: bad cover)")]


def test_request_lines_are_stored_only_when_failed_or_slow(mirror, db_session):
    requests = logging.getLogger("app.requests")
    requests.info("POST /api/stream/5/played -> 200 (4ms) user=a#1 req=aa")  # routine
    requests.warning('GET /api/stream/9/art -> 404 (3ms) user=a#1 req=bb detail="Media item not found"')
    requests.warning("GET /api/library/albums -> 200 (13418ms) SLOW user=a#1 req=cc")
    # A 5xx is already recorded by the request middleware as server.error.
    requests.error("GET /api/library/albums -> 500 (3ms) user=a#1 req=dd")

    assert [(kind, level) for kind, level, _summary in _stored(db_session)] == [
        ("request.failed", "warning"),
        ("request.slow", "warning"),
    ]


def test_routine_and_private_lines_are_left_out(mirror, db_session):
    """One line per request, socket frame or stream is not activity, and with
    verbose logging on those lines carry addresses and what was played."""
    logging.getLogger("app.routers.playback").info("playback_diagnostic %s", '{"media_item_id": 5}')
    logging.getLogger("app.routers.stream").info("stream_finished media_item_id=%d user_id=%d", 5, 1)
    logging.getLogger("app.ws_manager").info("ws_connect user_id=%d", 1)
    logging.getLogger("app.client").info("client_debug user_id=1 event=media.play")
    logging.getLogger("app.activity").info("auth.login user=a#1")  # stored as an event already
    logging.getLogger("app.activity_log").warning("Could not store 3 activity event(s)")  # never an event
    logging.getLogger("httpx").warning("not the app's own logger")
    logging.getLogger("app.requests").debug("GET /api/x -> 401 (1ms)")

    assert _stored(db_session) == []
    # Their warnings still count: a socket that could not be reached, say.
    logging.getLogger("app.ws_manager").warning("Could not send state to device %d", 4)
    assert _stored(db_session) == [("log.ws_manager", "warning", "Could not send state to device 4")]


def test_a_repeated_progress_line_is_limited_but_warnings_are_not(mirror, db_session, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(log_mirror.time, "monotonic", lambda: clock[0])
    progress = logging.getLogger("app.loudness")
    for done in range(40):
        progress.info("Loudness backfill: %d / %d tracks checked", done, 500)
    assert len(_events(db_session)) == log_mirror.INFO_BURST

    # One more is let through for every INFO_REFILL_SECONDS that pass...
    clock[0] += log_mirror.INFO_REFILL_SECONDS * 2
    for done in range(40, 50):
        progress.info("Loudness backfill: %d / %d tracks checked", done, 500)
    assert len(_events(db_session)) == log_mirror.INFO_BURST + 2

    # ...a different message has its own allowance, and warnings none at all.
    progress.info("Loudness backfill finished")
    for _ in range(30):
        progress.warning("Could not measure a track")
    assert len(_events(db_session)) == log_mirror.INFO_BURST + 2 + 1 + 30


def test_an_event_from_a_request_carries_who_and_which_request(mirror, db_session):
    context = begin_request()
    set_actor("dork_soul", 1)
    logging.getLogger("app.scanner").warning("Skipped an unreadable file")

    (event,) = _events(db_session)
    assert event.actor == "dork_soul"
    assert event.request_id == context["request_id"]


def test_a_failure_while_mirroring_never_reaches_the_code_that_logged(mirror, db_session, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("queue gone")

    monkeypatch.setattr(log_mirror, "record_event", broken)
    monkeypatch.setattr(logging, "raiseExceptions", False)
    logging.getLogger("app.scanner").warning("still logged elsewhere")  # does not raise
    assert _events(db_session) == []


def test_installing_twice_adds_one_handler_and_removing_takes_it_away(db_session):
    app_logger = logging.getLogger("app")
    before = len(app_logger.handlers)
    log_mirror.install_log_mirror()
    log_mirror.install_log_mirror()
    assert len(app_logger.handlers) == before + 1
    log_mirror.remove_log_mirror()
    log_mirror.remove_log_mirror()
    assert len(app_logger.handlers) == before


def test_kind_names():
    def record(name: str, message: str = "x") -> logging.LogRecord:
        return logging.LogRecord(name, logging.WARNING, __file__, 1, message, None, None)

    assert log_mirror.kind_for(record("app.discovery.mixes")) == "log.discovery"
    assert log_mirror.kind_for(record("app.routers.library.stats")) == "log.library"
    assert log_mirror.kind_for(record("app")) == "log.app"
    assert log_mirror.kind_for(record("app.requests", "GET /x -> 200 (9000ms) SLOW user=-")) == "request.slow"


def test_the_log_can_be_filtered_to_a_group_and_to_problems(client, db_session):
    record_event("log.loudness", "info", summary="pass finished")
    record_event("log.scanner", "warning", summary="skipped a file")
    record_event("request.failed", "warning", summary="GET /x -> 404")
    record_event("server.error", "error", summary="GET /y failed with 500")
    record_event("logistics.other", "info", summary="not in the log group")

    def summaries(**params) -> set[str]:
        response = client.get("/api/admin/activity", params=params)
        assert response.status_code == 200, response.text
        return {event["summary"] for event in response.json()["events"]}

    assert summaries(kind="log.") == {"pass finished", "skipped a file"}
    assert summaries(kind="log.scanner") == {"skipped a file"}
    assert summaries(level="problems") >= {"skipped a file", "GET /x -> 404", "GET /y failed with 500"}
    assert not summaries(level="problems") & {"pass finished", "not in the log group"}
    assert summaries(level="problems", kind="log.") == {"skipped a file"}
    text = client.get("/api/admin/activity/download", params={"level": "problems", "kind": "log."}).text
    assert "skipped a file" in text and "pass finished" not in text
