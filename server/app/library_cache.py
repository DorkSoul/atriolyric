"""Cached library listings (/api/library/{albums,artists,tracks}), shared by
every user with the same accessible roots.

Bounded (an LRU of MAX_ENTRIES), single-flight per key, and only for
listings, never free-text searches: callers compute a search directly
instead of storing it (see app.library_search for why Home search does
not use this cache either). Any change to what the listings show calls
bump_library_version() - after the change is committed, or a request in
between could cache what it saw just before the commit."""

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

# A handful of entries are hot: each root set's Albums, Artists and Songs
# tabs (full lists) and the first pages of what people browse. 64 keeps
# those warm while capping memory at a few full-library lists, whatever
# filter and page combinations requests send.
MAX_ENTRIES = 64

_lock = threading.Lock()
_version = 0
# (version, key) -> value, least recently used first.
_store: OrderedDict[tuple, Any] = OrderedDict()
# One lock per key being computed, so concurrent requests for the same
# uncached listing run its query once while other keys go ahead.
_key_locks: dict[tuple, threading.Lock] = {}
# Called after every bump (see add_bump_listener).
_bump_listeners: list[Callable[[], None]] = []
_BUMP_PENDING = "atriolyric_library_bump_pending"


def bump_library_version() -> None:
    """Invalidate every cached library listing.

    Called whenever a scan, tag edit, enrichment run or media deletion
    changes what /api/library/{albums,artists,tracks} could return. Clearing
    everything rather than tracking fine-grained keys is intentionally
    simple; the store is small and refills on the next requests."""
    global _version
    with _lock:
        _version += 1
        _store.clear()
        listeners = list(_bump_listeners)
    for listener in listeners:
        listener()


def add_bump_listener(listener: Callable[[], None]) -> None:
    """Call `listener()` after every bump, on the thread that bumped: it
    must be quick and must not raise (app.cache_warmup sets an event to
    wake its warmer). Adding the same listener twice keeps one."""
    with _lock:
        if listener not in _bump_listeners:
            _bump_listeners.append(listener)


def remove_bump_listener(listener: Callable[[], None]) -> None:
    with _lock:
        if listener in _bump_listeners:
            _bump_listeners.remove(listener)


def library_version() -> int:
    """The current version: it changes whenever the listings could have."""
    return _version


def bump_library_version_after_commit(db: Session) -> None:
    """Bump once `db` commits its current transaction (and not at all if
    it rolls back). Bumping before the commit lets a concurrent request
    re-cache the rows being changed under the new version."""
    db.info[_BUMP_PENDING] = True


@event.listens_for(Session, "after_commit")
def _bump_after_commit(session: Session) -> None:
    # Both events also fire when a SAVEPOINT (begin_nested) is released or
    # rolled back; only the outer transaction's outcome counts (as in
    # app.after_commit).
    if session.in_nested_transaction():
        return
    if session.info.pop(_BUMP_PENDING, False):
        bump_library_version()


@event.listens_for(Session, "after_rollback")
def _forget_bump_on_rollback(session: Session) -> None:
    if session.in_nested_transaction():
        return
    session.info.pop(_BUMP_PENDING, None)


def get_or_compute(key: tuple, compute: Callable[[], Any]) -> Any:
    with _lock:
        cache_key = (_version, key)
        if cache_key in _store:
            _store.move_to_end(cache_key)
            return _store[cache_key]
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:
        try:
            with _lock:
                version = _version
                cache_key = (version, key)
                if cache_key in _store:  # computed while this request waited
                    _store.move_to_end(cache_key)
                    return _store[cache_key]
            value = compute()
            with _lock:
                # A bump during the computation means the value may predate
                # the change: hand it to this caller, but never store it.
                if _version == version:
                    _store[cache_key] = value
                    while len(_store) > MAX_ENTRIES:
                        _store.popitem(last=False)
            return value
        finally:
            with _lock:
                if _key_locks.get(key) is key_lock:
                    del _key_locks[key]
