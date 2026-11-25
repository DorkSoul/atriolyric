"""The login rate limiter: failed password (and setup-code) verifications
counted per client address and per username, in process memory. Used by
the login and first-run setup endpoints."""

import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request

LOGIN_WINDOW_SECONDS = 5 * 60
# Two independent limits, because they stop different attacks:
# * per client address - one source hammering many accounts;
# * per username - many sources (or one account holder interleaving their own
#   successful logins) hammering one account. Unknown usernames get a bucket
#   too, so the response never says whether an account exists.
# A login is refused when EITHER is over its limit. Both are a plain sliding
# window over REAL failed verifications: a refused request is not recorded, so
# the block ends by itself when the oldest failure leaves the window and
# hammering a refused account cannot prolong it.
LOGIN_MAX_FAILURES = 20  # per address, per window
USER_MAX_FAILURES = 10  # per username, per window
USER_KEY_MAX_LENGTH = 64
LOGIN_MAX_TRACKED_KEYS = 10_000  # per table, so memory stays bounded
_login_failures: dict[str, deque[float]] = defaultdict(deque)  # by address
_user_failures: dict[str, deque[float]] = {}  # by normalised username
_login_failure_lock = threading.Lock()


def login_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _username_key(username: str) -> str:
    return username.strip().casefold()[:USER_KEY_MAX_LENGTH]


def _prune(times: deque[float], cutoff: float) -> None:
    while times and times[0] < cutoff:
        times.popleft()


def _make_room(
    table: dict, key: str, cutoff: float, live: Callable[[Any, float], bool]
) -> None:
    """Keep `table` under LOGIN_MAX_TRACKED_KEYS before `key` is added:
    drop expired entries first, then the oldest one."""
    if len(table) < LOGIN_MAX_TRACKED_KEYS or key in table:
        return
    for existing_key in list(table):
        if not live(table[existing_key], cutoff):
            table.pop(existing_key, None)
    if len(table) >= LOGIN_MAX_TRACKED_KEYS:
        table.pop(next(iter(table)))


def _too_many(retry_after: float) -> HTTPException:
    return HTTPException(
        429,
        "Too many failed login attempts; try again later",
        headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
    )


def _live(failures: deque[float], cutoff: float) -> bool:
    _prune(failures, cutoff)
    return bool(failures)


def _over_limit(table: dict[str, deque[float]], key: str, limit: int, now: float) -> None:
    failures = table.get(key)
    if failures is None:
        return
    cutoff = now - LOGIN_WINDOW_SECONDS
    if not _live(failures, cutoff):
        del table[key]
    elif len(failures) >= limit:
        # Retry-After: when the oldest failure leaves the window.
        raise _too_many(failures[0] - cutoff)


def check_login_rate_limit(key: str, username: str | None = None) -> None:
    """Raise 429 when the address `key` or `username` is over its limit.
    `username` is None where there is none yet (first-run setup). Checking
    records nothing."""
    now = time.monotonic()
    with _login_failure_lock:
        _over_limit(_login_failures, key, LOGIN_MAX_FAILURES, now)
        if username is not None:
            _over_limit(_user_failures, _username_key(username), USER_MAX_FAILURES, now)


def record_login_result(key: str, succeeded: bool, username: str | None = None) -> None:
    """Count a failed verification against the address and the username. A
    success clears only that username's failures, never the address's:
    otherwise anyone with an account could reset the counter between guesses
    at another account."""
    now = time.monotonic()
    cutoff = now - LOGIN_WINDOW_SECONDS
    user_key = _username_key(username) if username is not None else None
    with _login_failure_lock:
        if succeeded:
            if user_key is not None:
                _user_failures.pop(user_key, None)
            return
        _make_room(_login_failures, key, cutoff, _live)
        _login_failures[key].append(now)
        if user_key is not None:
            _make_room(_user_failures, user_key, cutoff, _live)
            _user_failures.setdefault(user_key, deque()).append(now)
