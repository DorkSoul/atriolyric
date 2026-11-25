"""File removals that wait for the database commit.

A deleted row's derived files (art, thumbnails, transcodes, custom art) are
not unlinked inside the write transaction, where a rollback would keep the
row but lose its files. Callers queue the work here instead; it runs once
the session commits and is dropped if it rolls back - the same contract as
app.library_cache.bump_library_version_after_commit.
"""

import logging
from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_PENDING = "atriolyric_after_commit"


def run_after_commit(
    db: Session, key: str, action: Callable[[set[Any]], None], items: Iterable[Any]
) -> None:
    """Call `action(items)` once `db` commits. Items queued under the same
    key are merged, so a batch of deletes makes one call (one directory
    listing) rather than one per row."""
    pending: dict[str, tuple[Callable[[set[Any]], None], set[Any]]] = db.info.setdefault(
        _PENDING, {}
    )
    pending.setdefault(key, (action, set()))[1].update(items)


@event.listens_for(Session, "after_commit")
def _run_pending(session: Session) -> None:
    # Both events also fire when a SAVEPOINT (begin_nested) is released or
    # rolled back; only the outer transaction's outcome counts.
    if session.in_nested_transaction():
        return
    for action, items in session.info.pop(_PENDING, {}).values():
        try:
            action(items)
        except Exception:  # pragma: no cover - file cleanup must never fail a commit
            logger.exception("Post-commit file cleanup failed")


@event.listens_for(Session, "after_rollback")
def _drop_pending(session: Session) -> None:
    if session.in_nested_transaction():
        return
    session.info.pop(_PENDING, None)
