"""Answering a request from what is stored while its outside look-ups
(MusicBrainz, ListenBrainz, Wikipedia, LRCLIB) run in the background.

A handler wraps its normal computation in `answer_from_store`. Inside it,
every place that would call a provider - app.external_cache.cached() on a
missing or expired entry, the lyrics fetch -
asks `defer_fetch()` first; while deferring, that returns True and the
caller uses what is stored instead (or nothing). When anything was skipped,
the same computation is queued, with fetching allowed, on
app.provider_queue's interactive lane, and the answer is marked pending: the
client asks again shortly and then gets the filled-in answer from the store.
When nothing was skipped (a cache hit) the answer is exactly what the
computation returns.

A look-up that has just been tried in the background and still finds
nothing (a provider failure or timeout - misses that are real answers are
stored) is not queued again for RETRY_AFTER, and the answer is not pending,
so a client that re-asks while pending always comes to a stop.

The interactive lane is bounded (app.provider_queue). When it refuses a job
- it is full, or this user already has too many waiting - the answer is the
stored one and not pending: the page shows what there is, as it does when a
provider is down, and the next request for it tries to queue again.

The underlying functions stay synchronous; only handlers that opt in defer.
Tests switch deferring off (DEFER) to keep their synchronous expectations.
"""

import contextlib
import contextvars
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from sqlalchemy.orm import Session

from app import provider_queue
from app.db import SessionLocal

# Off in most tests (tests/conftest.py) so a handler fetches inline.
DEFER = True
RETRY_AFTER = 120.0
# Bounds the memory of the recently-tried keys.
MAX_REMEMBERED = 2000

_T = TypeVar("_T")


class _State:
    __slots__ = ("missed",)

    def __init__(self) -> None:
        self.missed = False


_state: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "deferred_fetch_state", default=None
)
_tried_lock = threading.Lock()
# key -> when its background look-up last ended (monotonic), oldest first.
_tried: OrderedDict[str, float] = OrderedDict()


@contextlib.contextmanager
def deferring() -> Iterator[_State]:
    state = _State()
    token = _state.set(state)
    try:
        yield state
    finally:
        _state.reset(token)


def defer_fetch() -> bool:
    """Called where a provider would be asked: True (and the miss noted)
    when the current request answers from the store instead."""
    state = _state.get()
    if state is None:
        return False
    state.missed = True
    return True


def _recently_tried(key: str) -> bool:
    with _tried_lock:
        ended = _tried.get(key)
        return ended is not None and time.monotonic() - ended < RETRY_AFTER


def _mark_tried(key: str) -> None:
    with _tried_lock:
        _tried[key] = time.monotonic()
        _tried.move_to_end(key)
        while len(_tried) > MAX_REMEMBERED:
            _tried.popitem(last=False)


def in_session(work: Callable[[Session], object]) -> Callable[[], None]:
    """A background job that runs `work` in a database session of its own."""

    def job() -> None:
        db = SessionLocal()
        try:
            work(db)
        finally:
            db.close()

    return job


def answer_from_store(
    key: str | Callable[[_T], str],
    compute: Callable[[], _T],
    job: Callable[[], None],
    *,
    user_id: int | None = None,
) -> tuple[_T, bool]:
    """(`compute()`'s answer from stored data only, whether it is pending).
    `job` re-runs the computation with fetching allowed (see `in_session`);
    it is queued under `key` (or `key(answer)`) when anything was missing.
    `user_id` is whose request this is, for the queue's per-user limit."""
    if not DEFER:
        return compute(), False
    with deferring() as state:
        answer = compute()
    if not state.missed:
        return answer, False
    name = key(answer) if callable(key) else key
    if provider_queue.pending(name):
        return answer, True
    if _recently_tried(name):
        return answer, False

    def run() -> None:
        try:
            job()
        finally:
            _mark_tried(name)

    queued = provider_queue.submit_interactive(name, run, user_id=user_id)
    # Not queued: someone queued the same key just now (pending), or the
    # lane refused it (not pending - the client must not keep polling).
    return answer, queued or provider_queue.pending(name)


def answer_with_pending(
    key: str | Callable[[dict[str, Any]], str],
    compute: Callable[[], dict[str, Any]],
    job: Callable[[], None],
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """`answer_from_store` for a dict answer: adds its `pending` flag."""
    answer, pending = answer_from_store(key, compute, job, user_id=user_id)
    return {**answer, "pending": pending}
