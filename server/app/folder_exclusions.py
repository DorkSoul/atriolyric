from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models import IgnoredLibraryFolder, MediaItem


def not_in_ignored_folder() -> ColumnElement[bool]:
    """SQL predicate excluding media inside any ignored folder.

    The prefix test uses substr/length instead of LIKE so folder names
    containing SQL wildcard characters remain literal. A slash boundary
    prevents ignoring ``Rock`` from also excluding ``Rockabilly``.
    """
    ignored = aliased(IgnoredLibraryFolder)
    nested_path = (
        func.substr(
            MediaItem.relative_path,
            1,
            func.length(ignored.relative_path) + 1,
        )
        == ignored.relative_path + "/"
    )
    return ~exists(
        select(ignored.id).where(
            ignored.library_root_id == MediaItem.library_root_id,
            or_(MediaItem.relative_path == ignored.relative_path, nested_path),
        )
    )


def ignored_folder_paths(db: Session, root_id: int) -> set[str]:
    return set(
        db.execute(
            select(IgnoredLibraryFolder.relative_path).where(
                IgnoredLibraryFolder.library_root_id == root_id
            )
        ).scalars()
    )


def ignored_ancestor(path: str, ignored_paths: set[str]) -> str | None:
    """Return the deepest ignored directory containing ``path``."""
    matches = [
        ignored
        for ignored in ignored_paths
        if path == ignored or path.startswith(f"{ignored}/")
    ]
    return max(matches, key=len) if matches else None


def is_path_ignored(db: Session, root_id: int, path: str) -> bool:
    return ignored_ancestor(path, ignored_folder_paths(db, root_id)) is not None
