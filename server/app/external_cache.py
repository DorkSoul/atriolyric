"""A small TTL cache for provider responses, stored in ExternalCache.

`cached()` returns a fresh cached value, or calls `fetch` and stores what it
returns. `fetch` returns None for a failed request (never cached, so it is
retried next time) and an empty list/dict for "the provider had nothing",
which is cached for the shorter `miss_ttl`. With `allow_fetch=False` only
what is already cached (even stale) is used - for paths that must never
wait on the network, like autoplay.
"""

import contextlib
import contextvars
import functools
import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, ParamSpec, TypeVar, cast

from sqlalchemy import CursorResult, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deferred import defer_fetch
from app.models import ExternalCache, utcnow

logger = logging.getLogger(__name__)

DEFAULT_TTL = timedelta(days=30)
DEFAULT_MISS_TTL = timedelta(days=1)


# While set, a stored non-empty answer is served however old it is: pages for
# things the user keeps (the want list) read what is saved, and background
# jobs - not page views - refresh it.
_P = ParamSpec("_P")
_R = TypeVar("_R")

_serve_stale = contextvars.ContextVar("external_cache_serve_stale", default=False)


@contextlib.contextmanager
def serve_stale() -> Iterator[None]:
    token = _serve_stale.set(True)
    try:
        yield
    finally:
        _serve_stale.reset(token)


def stale_ok(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Decorator: run `func` inside serve_stale()."""

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with serve_stale():
            return func(*args, **kwargs)

    return wrapper


def _now() -> datetime:
    return utcnow()


def _age(row: ExternalCache) -> timedelta:
    fetched = row.fetched_at
    if fetched.tzinfo is not None:
        fetched = fetched.astimezone(UTC).replace(tzinfo=None)
    return _now() - fetched


def _empty(value: Any) -> bool:
    return value in ([], {}, "")


def peek(db: Session, provider: str, kind: str, key: str) -> tuple[Any, datetime] | None:
    row = db.get(ExternalCache, (provider, kind, key))
    if row is None:
        return None
    try:
        return json.loads(row.payload_json), row.fetched_at
    except (TypeError, json.JSONDecodeError):
        return None


def store(db: Session, provider: str, kind: str, key: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    row = db.get(ExternalCache, (provider, kind, key))
    if row is None:
        row = ExternalCache(provider=provider, kind=kind, key=key, payload_json=payload)
        db.add(row)
    else:
        row.payload_json = payload
        row.fetched_at = _now()
    try:
        db.commit()
    except IntegrityError:
        # Another request stored the same answer first; theirs is as good.
        db.rollback()


def cached(
    db: Session,
    provider: str,
    kind: str,
    key: str,
    fetch: Callable[[], Any] | None,
    *,
    ttl: timedelta = DEFAULT_TTL,
    miss_ttl: timedelta = DEFAULT_MISS_TTL,
    allow_fetch: bool = True,
    refresh: bool = False,
) -> Any:
    row = db.get(ExternalCache, (provider, kind, key))
    value = None
    if row is not None:
        try:
            value = json.loads(row.payload_json)
        except (TypeError, json.JSONDecodeError):
            value = None
        else:
            fresh = _age(row) < (miss_ttl if _empty(value) else ttl)
            kept = _serve_stale.get() and not _empty(value) and not refresh
            if not allow_fetch or fetch is None or kept or (fresh and not refresh):
                return value
    if not allow_fetch or fetch is None:
        return value
    if defer_fetch():
        # The request answers from what is stored (app.deferred); the
        # look-up runs in the background.
        return value
    fetched = fetch()
    if fetched is None:
        # A failed request: keep serving whatever (stale) answer we had.
        return value
    store(db, provider, kind, key, fetched)
    return fetched


# Nothing is kept past this however it was fetched: the longest TTL any kind
# uses is 30 days, and the want list's saved data is re-browsed weekly (which
# renews fetched_at). Past it a row could only be served as stale data, and
# is simply fetched again on demand.
MAX_AGE = timedelta(days=90)
# A remembered miss (empty payload) is only trusted for DEFAULT_MISS_TTL-ish
# periods anyway (the longest miss_ttl in use is 7 days).
MAX_MISS_AGE = timedelta(days=7)


def prune_expired(db: Session) -> int:
    """Delete rows older than MAX_AGE, and remembered misses older than
    MAX_MISS_AGE. Uses the fetched_at index."""
    now = _now()
    result = db.execute(
        delete(ExternalCache).where(
            (ExternalCache.fetched_at < now - MAX_AGE)
            | (
                (ExternalCache.fetched_at < now - MAX_MISS_AGE)
                & ExternalCache.payload_json.in_(("[]", "{}", '""'))
            )
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return cast("CursorResult[Any]", result).rowcount
