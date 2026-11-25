"""Let the web app report its own errors into the server log.

A bug that only shows in the browser (an uncaught exception, a failed render)
leaves nothing in ``docker logs`` otherwise. The web app posts those here;
they are written next to the request lines so one place tells the story.
Authenticated, size-capped and rate-limited per user so it cannot be used to
flood the log.
"""

import json
import logging
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException

from app.activity import log_action, log_token
from app.config import settings
from app.deps import CurrentUser
from app.schemas import (
    ClientDebugIn,
    ClientLogIn,
)

router = APIRouter(prefix="/api/client-log", tags=["client-log"])

WINDOW_SECONDS = 60
MAX_PER_WINDOW = 20
_recent: dict[int, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


@router.post("", status_code=204)
def report_client_error(
    body: ClientLogIn, user: CurrentUser
) -> None:
    now = time.monotonic()
    with _lock:
        recent = _recent[user.id]
        while recent and recent[0] < now - WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= MAX_PER_WINDOW:
            raise HTTPException(429, "Too many client reports")
        recent.append(now)
    log_action(
        "client.error" if body.level == "error" else "client.warning",
        level=logging.ERROR if body.level == "error" else logging.WARNING,
        message=body.message,
        source=body.source,
        page=body.page,
        stack=body.stack,
    )


# Tracing: the web app batches its lifecycle, audio element and socket events
# here so a "song restarted after unlocking" report can be lined up against
# the server's own request and WebSocket lines.
debug_logger = logging.getLogger("app.client")
# The web app sends a batch every two seconds while it has events (30 a
# minute), and one account is often open on two devices. A limit of 20 would
# refuse a busy minute on one device and lose its events.
MAX_DEBUG_BATCHES_PER_WINDOW = 90
_recent_debug: dict[int, deque[float]] = defaultdict(deque)


@router.post("/debug", status_code=204)
def report_client_debug(
    body: ClientDebugIn, user: CurrentUser
) -> None:
    if not settings.verbose_logging:
        return
    now = time.monotonic()
    with _lock:
        recent = _recent_debug[user.id]
        while recent and recent[0] < now - WINDOW_SECONDS:
            recent.popleft()
        if len(recent) >= MAX_DEBUG_BATCHES_PER_WINDOW:
            raise HTTPException(429, "Too many client reports")
        recent.append(now)
    for entry in body.events:
        fields = {
            key[:40]: (value[:200] if isinstance(value, str) else value)
            for key, value in list(entry.fields.items())[:30]
        }
        # session, at and event come from the browser. log_token leaves plain
        # values as they were and JSON-encodes anything with a newline or
        # other control character, so none can forge a log line.
        debug_logger.info(
            "client_debug user_id=%s sid=%s at=%s event=%s %s",
            user.id,
            log_token(body.session[:40]),
            log_token(entry.at),
            log_token(entry.event),
            json.dumps(fields, sort_keys=True, ensure_ascii=False),
        )
