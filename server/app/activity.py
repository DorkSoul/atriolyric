"""App-wide activity logging: who did what, and why a request failed.

Two layers feed ``docker logs``:

* ``app.requests`` - one line per interesting API request, written by the
  middleware in ``app.main`` (every write, every 4xx/5xx with its detail,
  anything slow). Noisy read paths such as art, streams and polling are
  skipped unless they fail.
* ``app.activity`` - named events (``user.create``, ``root.update``,
  ``task.finish``...) that say what an action meant, written with
  ``log_action``. These are also kept for the admin's Activity page, in
  the activity_events table (app.activity_log).

Both carry the request id (also returned as the ``X-Request-ID`` header, so a
user reporting a bug can quote it) and the signed-in username. Values are
JSON-encoded, which keeps a user-supplied string from forging extra log
lines. Never pass passwords, tokens or API keys here.
"""

import json
import logging
import secrets
from contextvars import ContextVar

from app.activity_log import actor_name, level_name, record_event

request_logger = logging.getLogger("app.requests")
activity_logger = logging.getLogger("app.activity")

# One mutable dict per request. Sync dependencies run in a copied context, so
# a ContextVar.set() there would never reach the middleware; mutating a
# shared dict does.
_request_context: ContextVar[dict | None] = ContextVar("request_context", default=None)


def begin_request() -> dict:
    context = {"request_id": secrets.token_hex(4), "user": None, "detail": None}
    _request_context.set(context)
    return context


def set_actor(username: str, user_id: int) -> None:
    context = _request_context.get()
    if context is not None:
        context["user"] = f"{username}#{user_id}"


def set_detail(detail: object) -> None:
    context = _request_context.get()
    if context is not None and context["detail"] is None:
        context["detail"] = detail


def _format(value: object) -> str:
    if isinstance(value, (bool, int, float)) or value is None:
        return json.dumps(value)
    return json.dumps(str(value), ensure_ascii=False)


def log_token(value: str) -> str:
    """`value` for a log line: as is when it is plain printable text without
    spaces or quotes (the usual case, and easy to grep), otherwise
    JSON-encoded so a name or path with a newline or other control character
    cannot forge or split a line."""
    if value.isprintable() and not any(char in value for char in ' "\\'):
        return value
    return json.dumps(value, ensure_ascii=False)


def log_action(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Record a named action, tagged with the current request and user."""
    context = _request_context.get() or {}
    parts = [event]
    if context.get("user"):
        parts.append(f"actor={log_token(context['user'])}")
    if context.get("request_id"):
        parts.append(f"req={context['request_id']}")
    parts.extend(f"{key}={_format(value)}" for key, value in fields.items())
    activity_logger.log(level, " ".join(parts))
    # The admin's stored activity log too (queued; never waits or raises).
    # A sign-in has no signed-in user in the context yet: it names its own.
    actor = context.get("user") or (fields.get("user") if event == "auth.login" else None)
    record_event(
        event,
        level_name(level),
        actor=actor_name(str(actor)) if actor else None,
        fields=fields,
        request_id=context.get("request_id"),
    )
