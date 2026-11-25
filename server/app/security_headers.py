"""Security headers on every response, and the cross-origin write check.

Two pure ASGI middlewares (not ``@app.middleware("http")``, whose own
responses and unhandled errors never pass back through the middleware that
should have decorated them):

* `SecurityHeadersMiddleware` is the outermost layer of the app, so the
  headers reach every response that leaves it: normal ones, 404s, the origin
  check's 403, the body limit's 413 and an unhandled 500 (which it answers
  itself, then re-raises so the server still logs the traceback).
* `OriginCheckMiddleware` sits inside the request logging so a rejected
  request is still logged.
"""

import logging
from collections.abc import Callable
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import is_https, settings

logger = logging.getLogger(__name__)

# No API response is meant to be rendered: art, media and downloads are
# user-controlled bytes, so if one is ever opened as a page it can run
# nothing and reach nothing.
API_CSP = "default-src 'none'; sandbox"

# The app shell and its static assets:
# * script-src 'self' - Vite emits only external module scripts; inline
#   scripts stay forbidden.
# * style-src 'unsafe-inline' - React sets style="" attributes.
# * img-src: blob:/data: for in-memory art and inlined assets; the remote
#   hosts are the images the browser loads directly - the art picker's
#   Cover Art Archive thumbnails (coverartarchive.org, which redirects to
#   *.archive.org), Wikimedia Commons artist photos (upload. and
#   thumb.wikimedia.org) and
#   Explore's Deezer covers (*.dzcdn.net, shown live, never stored).
#   Every other image is fetched by the server and served same-origin.
# * media-src: blob: for hls.js (MediaSource) and generated audio; Deezer's
#   30-second Explore previews play straight from *.dzcdn.net.
# * connect-src 'self' covers the same-origin fetches and the playback
#   WebSocket (CSP3 lets 'self' match ws/wss on the same host).
# * worker-src blob: - hls.js starts its transmuxer worker from a Blob;
#   'self' is the service worker.
APP_CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: coverartarchive.org *.archive.org upload.wikimedia.org thumb.wikimedia.org *.dzcdn.net",
        "media-src 'self' blob: *.dzcdn.net",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

# Swagger UI and ReDoc load scripts from a CDN and run inline code; they only
# exist in development (see app.main), where the shell CSP would break them.
_DOC_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


def _headers_for(path: str, https: bool) -> list[tuple[bytes, bytes]]:
    headers = {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "same-origin",
        "permissions-policy": "camera=(), microphone=(), geolocation=()",
    }
    if path.startswith("/api/"):
        headers["content-security-policy"] = API_CSP
    elif not (settings.web_dist_path is None and path in _DOC_PATHS):
        headers["content-security-policy"] = APP_CSP
    if https:
        # Only meaningful (and only safe to promise) when the site is HTTPS.
        headers["strict-transport-security"] = "max-age=31536000"
    return [(name.encode(), value.encode()) for name, value in headers.items()]


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        forwarded = dict(scope.get("headers") or ()).get(b"x-forwarded-proto")
        headers = _headers_for(
            scope["path"],
            is_https(scope.get("scheme", "http"), forwarded.decode("latin-1") if forwarded else None),
        )
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                present = {name.lower() for name, _ in message.get("headers", [])}
                # A route that sets one of these itself wins.
                message["headers"] = [
                    *message.get("headers", []),
                    *(pair for pair in headers if pair[0] not in present),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            # The server's own 500 page would bypass this middleware; answer
            # here so it carries the headers, then re-raise so the traceback
            # is still logged and the connection handled as usual.
            if not started:
                await PlainTextResponse("Internal Server Error", status_code=500)(
                    scope, receive, send_with_headers
                )
            raise


def origin_allowed(origin: str | None, host: str | None) -> bool:
    """Whether a request carrying this Origin header may act with the user's
    cookie: no Origin (same-origin navigation, non-browser clients), the
    page's own host over http(s), or one of ATRIOLYRIC_ALLOWED_ORIGINS. The
    one rule for API writes (OriginCheckMiddleware) and the playback
    WebSocket."""
    if not origin:
        return True
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"}:
        return False
    return parsed.netloc == host or origin in settings.allowed_origins


class OriginCheckMiddleware:
    """Refuse cross-origin cookie-authenticated writes (CSRF defence in depth
    on top of SameSite=Lax) and note once when a proxy's X-Forwarded-For is
    being ignored."""

    def __init__(self, app: ASGIApp, warn_forwarded: Callable[[str | None, str | None], object]) -> None:
        self.app = app
        self.warn_forwarded = warn_forwarded

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        client = scope.get("client")
        self.warn_forwarded(request_headers.get("x-forwarded-for"), client[0] if client else None)
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"} and not origin_allowed(
            request_headers.get("origin"), request_headers.get("host")
        ):
            await JSONResponse(
                {"detail": "Cross-origin request rejected"}, status_code=403
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)
