import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.models import LibraryRoot, MediaItem, User, UserLibraryAccess


def accessible_root_ids(db: DbSession, user: User) -> set[int]:
    """Root ids this user can see: every shared root, plus any root
    explicitly granted to them via `user_library_access`. Admins are scoped
    the same way - a private root an admin has not ticked for themselves in
    the Admin panel stays out of their library (they can grant it to
    themselves at any time; the admin endpoints themselves are unscoped)."""
    shared = db.execute(
        select(LibraryRoot.id).where(LibraryRoot.is_shared.is_(True))
    ).scalars()
    granted = db.execute(
        select(UserLibraryAccess.library_root_id).where(
            UserLibraryAccess.user_id == user.id
        )
    ).scalars()
    return set(shared) | set(granted)


def can_access_root(db: DbSession, user: User, library_root_id: int) -> bool:
    return library_root_id in accessible_root_ids(db, user)


def media_file_path(item: MediaItem, *, require_file: bool = True) -> Path | None:
    """Resolve a media path without allowing a row or symlink to escape its root.

    Files can be replaced by symlinks after a scan, and old databases may contain
    manually-edited relative paths. Re-check containment at the point of use so a
    shared root can never become a way to read or modify an unrelated private file.
    """
    try:
        root = Path(item.library_root.path).resolve(strict=True)
        candidate = (root / item.relative_path).resolve(strict=require_file)
    except (OSError, RuntimeError):
        return None
    if not candidate.is_relative_to(root):
        return None
    if require_file and not candidate.is_file():
        return None
    return candidate


def media_file_absent(item: MediaItem) -> bool:
    """Whether `media_file_path` finds nothing for the item because its file,
    or its whole library folder, is not there - as opposed to there but not
    to be used (a symlink out of its root, something that is not a file).
    Absence can be temporary (a share that is not mounted right now), so
    background work must not record it as a failure of the file."""
    try:
        root = Path(item.library_root.path).resolve(strict=True)
        return not (root / item.relative_path).exists()
    except (OSError, RuntimeError):
        return True


def library_root_reachable(path: str) -> bool:
    """Whether a library folder is there with something in it. Existing is
    not enough: an unmounted share is an existing, empty folder, and an
    unreadable folder lists as empty (see scanner._removal_hold_reason)."""
    try:
        with os.scandir(Path(path).resolve(strict=True)) as entries:
            return next(entries, None) is not None
    except (OSError, RuntimeError):
        return False
