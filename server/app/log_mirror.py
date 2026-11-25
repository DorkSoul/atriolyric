"""Copies the application log into the admin's stored activity log, so
Admin -> Server activity shows what `docker logs` would: warnings, failed
and slow requests, errors, and what the background work is doing.

A logging handler on the ``app`` logger hands each record worth keeping to
app.activity_log.record_event (an in-memory queue; nothing here waits for
the database). What is kept:

* every WARNING and ERROR from the app's own loggers - a failed request
  with its reason, a slow request, a provider that could not be reached, a
  background pass that gave up;
* INFO lines from the background work (scans, mixes, the loudness and
  analysis passes, cache warm-up, maintenance), with a limit per message so
  a progress line repeated for hours does not fill the log.

What is left out:

* ``app.activity`` - those actions are already stored as events, in words;
* ``app.activity_log`` and this module - a failure to store an event must
  not become an event;
* INFO from the request, playback, streaming and browser-report loggers:
  one line per request, socket frame or stream. They are routine at the
  default setting and, with verbose logging on, carry client addresses and
  a per-user listening history, which the stored log never holds;
* server errors (5xx) on the request logger, which the request middleware
  already records as ``server.error``.

Only the message is stored, as one line; a traceback is reduced to the
exception's type and message. The log itself never carries passwords or
API keys (see the README's "Logs" section), so neither does this."""

import logging
import re
import threading
import time

from app.activity import _request_context
from app.activity_log import actor_name, level_name, record_event

APP_LOGGER_NAME = "app"
# Never mirrored, at any level.
_SKIP_LOGGERS = ("app.activity", "app.activity_log", "app.log_mirror")
# Mirrored from WARNING up only: per-request, per-frame or per-stream lines.
_QUIET_LOGGERS = (
    "app.requests",
    "app.client",
    "app.routers.playback",
    "app.routers.stream",
    "app.routers.client_log",
    "app.playback",
    "app.ws_manager",
    "app.hls",
    "app.transcode",
)
# An INFO message (by its template, so "12 / 500 checked" and "13 / 500
# checked" are one) may be stored this many times in a row, then once per
# INFO_REFILL_SECONDS: a burst of distinct events gets through, a progress
# line repeated for hours becomes a few lines an hour.
INFO_BURST = 15
INFO_REFILL_SECONDS = 600.0
_SERVER_ERROR = re.compile(r" -> 5\d\d \(")


def _under(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


def kind_for(record: logging.LogRecord) -> str:
    """The event kind: "request.slow" / "request.failed" for the request
    log, else "log.<area>" from the logger's name ("app.loudness" ->
    "log.loudness", "app.routers.radio" -> "log.radio")."""
    if record.name == "app.requests":
        return "request.slow" if " SLOW " in record.getMessage() else "request.failed"
    parts = record.name.split(".")[1:]
    if len(parts) > 1 and parts[0] == "routers":
        parts = parts[1:]
    return f"log.{parts[0]}" if parts else "log.app"


class LogMirrorHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._lock_buckets = threading.Lock()
        # template -> (tokens left, when they were last topped up)
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}

    def _allowed(self, record: logging.LogRecord) -> bool:
        name = record.name
        if not _under(name, (APP_LOGGER_NAME,)) or _under(name, _SKIP_LOGGERS):
            return False
        if record.levelno >= logging.WARNING:
            return not (name == "app.requests" and _SERVER_ERROR.search(record.getMessage()))
        if _under(name, _QUIET_LOGGERS):
            return False
        return self._take_token((name, str(record.msg)))

    def _take_token(self, key: tuple[str, str]) -> bool:
        now = time.monotonic()
        with self._lock_buckets:
            if len(self._buckets) > 2000:  # messages built without a template
                self._buckets.clear()
            tokens, stamp = self._buckets.get(key, (float(INFO_BURST), now))
            tokens = min(float(INFO_BURST), tokens + (now - stamp) / INFO_REFILL_SECONDS)
            allowed = tokens >= 1.0
            self._buckets[key] = (tokens - 1.0 if allowed else tokens, now)
            return allowed

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self._allowed(record):
                return
            message = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                error = record.exc_info[1]
                message = f"{message} ({record.exc_info[0].__name__}: {error})"
            context = _request_context.get() or {}
            record_event(
                kind_for(record),
                level_name(record.levelno),
                actor=actor_name(context.get("user")),
                summary=message,
                request_id=context.get("request_id"),
            )
        except Exception:  # noqa: BLE001 - logging must never fail what it describes
            self.handleError(record)


_handler: LogMirrorHandler | None = None
_install_lock = threading.Lock()


def install_log_mirror() -> None:
    """Start copying the app log into the activity log (once)."""
    global _handler
    with _install_lock:
        if _handler is None:
            _handler = LogMirrorHandler()
            logging.getLogger(APP_LOGGER_NAME).addHandler(_handler)


def remove_log_mirror() -> None:
    global _handler
    with _install_lock:
        if _handler is not None:
            logging.getLogger(APP_LOGGER_NAME).removeHandler(_handler)
            _handler = None
