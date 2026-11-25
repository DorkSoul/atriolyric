"""Reject oversized request bodies before the application reads them.

FastAPI buffers a whole JSON body before it runs any dependency, so without a
cap an unauthenticated client can make the single worker hold gigabytes just
by POSTing to /api/auth/login. This is a pure ASGI middleware (not
``@app.middleware("http")``, which would read the body through a wrapper) that
sits outside the whole app:

* a declared ``Content-Length`` over the limit is answered 413 without the
  body being read;
* a body with no usable ``Content-Length`` (chunked) is counted as it
  streams; once it passes the limit the app is told the client disconnected
  and its answer is replaced with a 413.
"""

import logging
import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.custom_art import MAX_UPLOAD_BYTES

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 1024 * 1024
# The only route that legitimately takes more. Art upload is capped again by
# its own route while streaming (the extra covers multipart framing).
LIMIT_OVERRIDES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("PUT", re.compile(r"^/api/art/[^/]+/[^/]+$"), MAX_UPLOAD_BYTES + 64 * 1024),
)
_TOO_LARGE = "Request body is too large"


def limit_for(method: str, path: str) -> int:
    for override_method, pattern, limit in LIMIT_OVERRIDES:
        if method == override_method and pattern.match(path):
            return limit
    return DEFAULT_LIMIT


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # WebSockets and lifespan carry no request body.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope["method"], scope["path"])
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            logger.warning(
                "Rejected %s %r: declared body of %s bytes exceeds the %d byte limit",
                scope["method"], scope["path"], declared.decode(), limit,
            )
            response = JSONResponse({"detail": _TOO_LARGE}, status_code=413)
            await response(scope, receive, send)
            return

        received = 0
        exceeded = False
        replaced = False
        response_started = False

        async def counted_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Tell the app the client went away: it stops reading and
                    # whatever it answers is replaced with the 413 below.
                    # (Raising here instead would be rewritten into a generic
                    # error by the http middlewares between us and the route.)
                    exceeded = True
                    logger.warning(
                        "Cut off %s %r: body passed the %d byte limit",
                        scope["method"], scope["path"], limit,
                    )
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal replaced, response_started
            if message["type"] == "http.response.start" and not exceeded:
                response_started = True
            if not exceeded or response_started:
                await send(message)
            elif not replaced:
                replaced = True
                await JSONResponse({"detail": _TOO_LARGE}, status_code=413)(scope, receive, send)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            # A handler that read a body we cut off typically fails with a
            # client-disconnect error; the client is owed the 413, not a 500
            # and a traceback. Anything else is a real error.
            if not exceeded:
                raise
            if not replaced and not response_started:
                replaced = True
                await JSONResponse({"detail": _TOO_LARGE}, status_code=413)(scope, receive, send)
