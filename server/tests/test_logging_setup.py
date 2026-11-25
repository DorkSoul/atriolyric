from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from app.logging_setup import (
    APP_LOGGER_NAME,
    LOG_LEVEL,
    _build_stderr_handler,
    _install,
    configure_app_logging,
)
from app.main import _art_backfill_progress_reporter

SERVER_ROOT = Path(__file__).resolve().parents[1]


def test_build_stderr_handler_writes_uvicorn_style_lines_to_stderr(capsys):
    handler = _build_stderr_handler()
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr

    # A private logger, so this asserts the handler's own wiring rather than
    # whatever the shared `app` logger happens to be carrying this session.
    logger = logging.Logger("logging-setup-test", level=LOG_LEVEL)
    logger.addHandler(handler)
    logger.info("Art thumbnail backfill: %d generated so far", 7)

    assert (
        capsys.readouterr().err
        == "INFO:     Art thumbnail backfill: 7 generated so far\n"
    )


def test_install_attaches_handler_and_info_level():
    logger = logging.Logger("logging-setup-install-test")
    handler = logging.NullHandler()

    _install(logger, handler)

    assert logger.handlers == [handler]
    assert logger.level == logging.INFO


def test_defers_to_an_existing_app_handler(monkeypatch):
    app_logger = logging.getLogger(APP_LOGGER_NAME)
    existing = logging.NullHandler()
    monkeypatch.setattr(app_logger, "handlers", [existing])
    monkeypatch.setattr(app_logger, "level", logging.WARNING)
    monkeypatch.setattr(logging.getLogger(), "handlers", [])

    configure_app_logging()

    assert app_logger.handlers == [existing]
    assert app_logger.level == logging.WARNING


def test_defers_to_a_configured_root_logger(monkeypatch):
    """An embedder (gunicorn, a supervisor, pytest) that owns the root
    logger already receives our records by propagation - adding our own
    handler would print every line twice."""
    app_logger = logging.getLogger(APP_LOGGER_NAME)
    monkeypatch.setattr(app_logger, "handlers", [])
    monkeypatch.setattr(app_logger, "level", logging.NOTSET)
    monkeypatch.setattr(logging.getLogger(), "handlers", [logging.NullHandler()])

    configure_app_logging()

    assert app_logger.handlers == []
    assert app_logger.level == logging.NOTSET


def test_backfill_progress_is_logged_only_when_the_count_advances(caplog):
    """The backfill calls back once per page of candidate ids, most of which
    generate nothing once a library is healthy - one line per advance keeps
    `docker logs` readable without hiding that the pass ran."""
    caplog.set_level(logging.INFO, logger=APP_LOGGER_NAME)
    report = _art_backfill_progress_reporter()

    for done in (0, 0, 5, 5, 5, 7):
        report(done)

    assert [record.getMessage() for record in caplog.records] == [
        "Art thumbnail backfill: 0 generated so far",
        "Art thumbnail backfill: 5 generated so far",
        "Art thumbnail backfill: 7 generated so far",
    ]


def test_importing_app_main_wires_application_logging(tmp_path):
    """End to end: a fresh interpreter that imports the uvicorn entrypoint
    must send an app-module logger.info to stderr, which uvicorn's own
    logging config does not allow."""
    script = textwrap.dedent(
        """
        import logging
        import sys

        import app.main  # noqa: F401 - the uvicorn entrypoint

        logging.getLogger("app.main").info(
            "Art thumbnail backfill: %d generated so far", 4242
        )
        # A nested module logger, as every logging.getLogger(__name__) in
        # this package produces, reaches the same handler by propagation.
        logging.getLogger("app.routers.playback").info(
            "playback_diagnostic event=%s", "test"
        )
        app_logger = logging.getLogger("app")
        print(
            "handlers=%d stderr=%s level=%s"
            % (
                len(app_logger.handlers),
                all(h.stream is sys.stderr for h in app_logger.handlers),
                app_logger.level,
            )
        )
        """
    )
    env = {
        **os.environ,
        "ATRIOLYRIC_DATABASE_PATH": str(tmp_path / "logging-smoke.db"),
    }

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=SERVER_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "INFO:     Art thumbnail backfill: 4242 generated so far" in result.stderr
    assert "INFO:     playback_diagnostic event=test" in result.stderr
    assert "Art thumbnail backfill" not in result.stdout
    assert f"handlers=1 stderr=True level={logging.INFO}" in result.stdout


def _passes_uvicorn_filters(logger_name: str, msg: str, *args: object) -> bool:
    logger = logging.getLogger(logger_name)
    record = logger.makeRecord(logger_name, logging.INFO, __file__, 1, msg, args, None)
    return bool(logger.filter(record))


def test_uvicorn_lines_naming_a_client_address_stay_out_of_the_default_log(monkeypatch):
    # uvicorn's access line records every stream request with the client's
    # address, and its WebSocket line every socket's address.
    from app.config import settings

    monkeypatch.setattr(settings, "verbose_logging", False)
    assert not _passes_uvicorn_filters(
        "uvicorn.access", '%s - "%s %s HTTP/%s" %d', "192.0.2.7:5000", "GET", "/api/stream/5", "1.1", 206
    )
    assert not _passes_uvicorn_filters(
        "uvicorn.error", '%s - "WebSocket %s" [accepted]', "192.0.2.7:5000", "/ws/playback"
    )
    assert not _passes_uvicorn_filters(
        "uvicorn.error", '%s - "WebSocket %s" 403', "192.0.2.7:5000", "/ws/playback"
    )
    # Everything else uvicorn says is kept.
    assert _passes_uvicorn_filters("uvicorn.error", "Application startup complete.")
    assert _passes_uvicorn_filters("uvicorn.error", "connection closed")
    assert _passes_uvicorn_filters("uvicorn.error", "Exception in ASGI application\n")


def test_verbose_logging_keeps_every_uvicorn_line(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "verbose_logging", True)
    assert _passes_uvicorn_filters(
        "uvicorn.access", '%s - "%s %s HTTP/%s" %d', "192.0.2.7:5000", "GET", "/assets/index.js", "1.1", 200
    )
    assert _passes_uvicorn_filters(
        "uvicorn.error", '%s - "WebSocket %s" [accepted]', "192.0.2.7:5000", "/ws/playback"
    )
