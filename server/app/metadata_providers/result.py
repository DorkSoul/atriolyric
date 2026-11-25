"""One answer shape for every provider call: the provider had something
(Found), definitively had nothing (NotFound), or could not be asked
(Failed - a timeout, a 5xx, a rate limit, a quota error dressed up as a
200). Callers cache Found and NotFound and never Failed: a failure cached as
"nothing" would hide real data for days, while a real "nothing" left
uncached would be asked for again on every play.

The public provider functions keep the convention app.external_cache.cached
already understands - the data, an empty list/dict for NotFound, None for
Failed - so `cache_value` is the only bridge most callers need.

Failures are also counted per context (`recording_failures`), so a caller
that makes several provider calls for one item (a bulk task step, a
catalog-art fetch) can tell "nothing anywhere" from "something failed"."""

from __future__ import annotations

import contextlib
import contextvars
import email.utils
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    import httpx

    from app.metadata_providers._throttle import Throttle

logger = logging.getLogger(__name__)

T = TypeVar("T")

# A Retry-After longer than this is not believed: a provider asking for an
# hour would otherwise stop every page that uses it for that long.
MAX_RETRY_AFTER = 120.0
# The pause after a rate-limit answer that names no Retry-After.
DEFAULT_RATE_LIMIT_PAUSE = 10.0


@dataclass(frozen=True, slots=True)
class Found(Generic[T]):
    data: T


@dataclass(frozen=True, slots=True)
class NotFound:
    pass


@dataclass(frozen=True, slots=True)
class Failed:
    reason: str
    retry_after: float | None = None


NOT_FOUND = NotFound()

ProviderResult = Found[T] | NotFound | Failed

_failures: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "provider_failures", default=None
)


@contextlib.contextmanager
def recording_failures() -> Iterator[list[str]]:
    """Collect the reason of every Failed produced inside the block."""
    reasons: list[str] = []
    token = _failures.set(reasons)
    try:
        yield reasons
    finally:
        _failures.reset(token)


def note_failure(reason: str) -> None:
    """Count a failure reported in another shape (MusicBrainz's LookupResult)."""
    reasons = _failures.get()
    if reasons is not None:
        reasons.append(reason)


def failed(reason: str, retry_after: float | None = None, throttle: Throttle | None = None) -> Failed:
    """A Failed, counted for `recording_failures`; with `retry_after` the
    provider's `throttle` is paused that long, so the next calls wait (bulk
    work) or give up at once (page requests) instead of hammering it."""
    if retry_after is not None:
        retry_after = max(0.0, min(retry_after, MAX_RETRY_AFTER))
        if throttle is not None:
            throttle.pause(retry_after)
    note_failure(reason)
    return Failed(reason, retry_after)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The response's Retry-After, in seconds (delta or HTTP date), capped."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - time.time()
    return max(0.0, min(seconds, MAX_RETRY_AFTER))


def cache_value(result: ProviderResult[Any], empty: Any) -> Any:
    """external_cache's convention: the data, `empty` for NotFound (cached
    for the shorter miss TTL), None for Failed (never cached)."""
    if isinstance(result, Found):
        return result.data
    if isinstance(result, NotFound):
        return empty
    return None
