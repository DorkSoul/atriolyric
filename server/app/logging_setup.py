"""Give this package's own log records somewhere to go under uvicorn.

Uvicorn's built-in ``LOGGING_CONFIG`` attaches handlers only to the
``uvicorn``, ``uvicorn.access`` and ``uvicorn.error`` loggers, each with
``propagate: False``, and installs no root handler. A plain
``logging.getLogger(__name__)`` logger in this package therefore has no
handler anywhere in its chain, so everything below WARNING - the startup
art-thumbnail backfill progress, the discovery scheduler, the metadata
providers - is silently dropped and never reaches ``docker logs`` (only
WARNING and above surface, via logging's ``lastResort`` handler).

``configure_app_logging`` closes that gap with a stderr handler on the
``app`` logger. Two deliberate choices:

* Scoped to ``app`` rather than the root logger, so third-party chatter
  (httpx request lines, SQLAlchemy) stays out of the container log.
* Never installed when a handler already exists on ``app`` or on the root
  logger. Records propagate upwards either way, so in a process whose host
  configured root logging (a supervisor, gunicorn, pytest) that host already
  owns the output and stacking ours on top would print every line twice.
"""

import logging
import sys

from app.config import settings

APP_LOGGER_NAME = "app"
LOG_LEVEL = logging.INFO
# Matches the shape of uvicorn's default lines ("INFO:     Application
# startup complete.") so application records sit alongside uvicorn's own
# without looking like they came from somewhere else.
LOG_FORMAT = "%(levelname)s:     %(message)s"


def _build_stderr_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def _configured_level() -> int:
    """`ATRIOLYRIC_LOG_LEVEL` (validated at startup; INFO for anything unknown
    that reaches here, e.g. a setting patched at runtime)."""
    level = logging.getLevelName(settings.log_level.strip().upper())
    return level if isinstance(level, int) else LOG_LEVEL


def _install(app_logger: logging.Logger, handler: logging.Handler) -> None:
    app_logger.addHandler(handler)
    app_logger.setLevel(_configured_level())


def configure_app_logging() -> None:
    """Attach a stderr handler to the ``app`` logger if nobody else has.

    Safe to call more than once, and safe to call in a process whose host
    already configured logging - in that case it does nothing and lets the
    host own the output.
    """
    app_logger = logging.getLogger(APP_LOGGER_NAME)
    if app_logger.handlers or logging.getLogger().handlers:
        return
    _install(app_logger, _build_stderr_handler())
