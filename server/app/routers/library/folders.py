"""Folder browsing, file search, ignored folders and lyrics."""

from pathlib import Path, PurePosixPath
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, undefer_group

from app.access import accessible_root_ids, can_access_root, media_file_path
from app.config import settings
from app.deferred import answer_with_pending, in_session
from app.deps import AdminUser, CurrentUser, DbSession
from app.enrichment import get_or_fetch_lyrics
from app.folder_exclusions import (
    ignored_ancestor,
    ignored_folder_paths,
    is_path_ignored,
)
from app.library_cache import bump_library_version
from app.models import (
    IgnoredLibraryFolder,
    LibraryRoot,
    MediaItem,
)
from app.presenters import to_media_item_outs
from app.schemas import (
    BrowseDirOut,
    BrowseOut,
    FileSearchItemOut,
    FileSearchOut,
    IgnoredFolderIn,
    IgnoredFolderOut,
    LibraryRootSummary,
    LyricsOut,
    OkOut,
)

router = APIRouter()


FILE_SEARCH_RESULT_LIMIT = 500


@router.get("/roots", response_model=list[LibraryRootSummary])
def list_accessible_roots(
    db: DbSession, user: CurrentUser
) -> list[LibraryRootSummary]:
    roots = db.scalars(
        select(LibraryRoot)
        .where(LibraryRoot.id.in_(accessible_root_ids(db, user)))
        .order_by(LibraryRoot.display_name)
    ).all()
    return [LibraryRootSummary.model_validate(r) for r in roots]


@router.get("/browse", response_model=BrowseOut)
def browse(
    root_id: int,
    db: DbSession,
    user: CurrentUser,
    path: Annotated[str, Query(max_length=4096)] = "",
) -> BrowseOut:
    """A raw filesystem view of a library root - folders and playable files
    exactly as they sit on disk, for content (custom rips, oddly-tagged
    files) that doesn't group sensibly under the tag-based Albums/Artists
    views. Only surfaces files already picked up by a scan, same as every
    other browse endpoint - a brand new file needs a rescan first."""
    root = db.get(LibraryRoot, root_id)
    if root is None or not can_access_root(db, user, root_id):
        raise HTTPException(404, "Library root not found")

    try:
        root_path = Path(root.path).resolve(strict=True)
        target = (root_path / path).resolve(strict=True) if path else root_path
    except (OSError, RuntimeError):
        raise HTTPException(404, "Directory not found") from None

    if not target.is_relative_to(root_path) or not target.is_dir():
        raise HTTPException(404, "Directory not found")

    directories: list[BrowseDirOut] = []
    ignored_paths = ignored_folder_paths(db, root_id)
    file_relative_paths: list[str] = []
    for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        try:
            resolved = entry.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(root_path):
            continue
        rel = str(resolved.relative_to(root_path))
        if resolved.is_dir():
            ignored_by = ignored_ancestor(rel, ignored_paths)
            directories.append(
                BrowseDirOut(
                    name=entry.name,
                    path=rel,
                    is_ignored=ignored_by is not None,
                    ignored_directly=ignored_by == rel,
                    ignored_by=ignored_by,
                )
            )
        elif resolved.is_file() and resolved.suffix.lower() in settings.scan_extensions:
            file_relative_paths.append(rel)

    media_items = db.scalars(
        select(MediaItem).where(
            MediaItem.library_root_id == root_id,
            MediaItem.relative_path.in_(file_relative_paths),
        )
    ).all()
    by_path = {item.relative_path: item for item in media_items}
    ordered = [by_path[rel] for rel in file_relative_paths if rel in by_path]
    items = to_media_item_outs(ordered, db, user)

    normalized_path = str(target.relative_to(root_path)) if target != root_path else ""
    parent_path: str | None = None
    if normalized_path:
        parent = str(Path(normalized_path).parent)
        parent_path = "" if parent == "." else parent

    return BrowseOut(
        root_id=root_id,
        path=normalized_path,
        parent_path=parent_path,
        directories=directories,
        items=items,
    )


@router.get("/files/search", response_model=FileSearchOut)
def search_files(
    root_id: int,
    db: DbSession,
    user: CurrentUser,
    q: str = Query(min_length=1, max_length=200),
) -> FileSearchOut:
    """Search scanned filenames and containing folders throughout one root.

    Results are bounded so a broad query cannot send an entire large library
    to the browser. Files still need to have been picked up by a library scan.
    """
    root = db.get(LibraryRoot, root_id)
    if root is None or not can_access_root(db, user, root_id):
        raise HTTPException(404, "Library root not found")

    needle = q.strip()
    if not needle:
        return FileSearchOut(root_id=root_id, folders=[], items=[])

    try:
        root_path = Path(root.path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, "Library root not found") from None

    # Escape SQL LIKE metacharacters so searches for characters such as '_'
    # or '%' are literal filename searches, not wildcard expressions.
    escaped_needle = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = list(
        db.scalars(
            select(MediaItem)
            .where(
                MediaItem.library_root_id == root_id,
                MediaItem.relative_path.ilike(f"%{escaped_needle}%", escape="\\"),
            )
            .order_by(MediaItem.relative_path)
            .limit(FILE_SEARCH_RESULT_LIMIT + 1)
        )
    )
    truncated = len(rows) > FILE_SEARCH_RESULT_LIMIT
    rows = rows[:FILE_SEARCH_RESULT_LIMIT]

    matched_folders: set[str] = set()
    matched_files: list[MediaItem] = []
    folded_needle = needle.casefold()
    for item in rows:
        relative_path = PurePosixPath(item.relative_path)
        # Check the resolved target as browse() does, preventing stale scan
        # rows or symlinks from exposing files outside their library root.
        try:
            resolved = (root_path / item.relative_path).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(root_path) or not resolved.is_file():
            continue

        for index, component in enumerate(relative_path.parts[:-1], start=1):
            if folded_needle in component.casefold():
                matched_folders.add(PurePosixPath(*relative_path.parts[:index]).as_posix())
        if relative_path.name and folded_needle in relative_path.name.casefold():
            matched_files.append(item)

    ignored_paths = ignored_folder_paths(db, root_id)
    folders = []
    for folder_path in sorted(matched_folders, key=str.casefold):
        ignored_by = ignored_ancestor(folder_path, ignored_paths)
        folders.append(
            BrowseDirOut(
                name=PurePosixPath(folder_path).name,
                path=folder_path,
                is_ignored=ignored_by is not None,
                ignored_directly=ignored_by == folder_path,
                ignored_by=ignored_by,
            )
        )

    media_outs = to_media_item_outs(matched_files, db, user)
    items = [
        FileSearchItemOut.model_validate(
            {**media_out.model_dump(), "relative_path": item.relative_path}
        )
        for item, media_out in zip(matched_files, media_outs, strict=True)
    ]
    return FileSearchOut(
        root_id=root_id,
        folders=folders,
        items=items,
        truncated=truncated,
    )


def _normalise_relative_folder(path: str) -> str:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HTTPException(422, "Folder path must be relative to the library root")
    normalized = candidate.as_posix().strip("/")
    if not normalized or normalized == ".":
        raise HTTPException(422, "Select a folder inside the library root")
    return normalized


@router.post("/roots/{root_id}/ignored-folders", response_model=IgnoredFolderOut)
def ignore_library_folder(
    root_id: int,
    body: IgnoredFolderIn,
    db: DbSession,
    user: AdminUser,
) -> IgnoredFolderOut:
    root = db.get(LibraryRoot, root_id)
    if root is None or not can_access_root(db, user, root_id):
        raise HTTPException(404, "Library root not found")
    relative_path = _normalise_relative_folder(body.path)
    try:
        root_path = Path(root.path).resolve(strict=True)
        target = (root_path / relative_path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, "Directory not found") from None
    if (
        not target.is_relative_to(root_path)
        or not target.is_dir()
        or target == root_path
    ):
        raise HTTPException(404, "Directory not found")
    relative_path = target.relative_to(root_path).as_posix()
    existing = db.scalars(
        select(IgnoredLibraryFolder).where(
            IgnoredLibraryFolder.library_root_id == root_id,
            IgnoredLibraryFolder.relative_path == relative_path,
        )
    ).one_or_none()
    if existing is None:
        db.add(IgnoredLibraryFolder(library_root_id=root_id, relative_path=relative_path))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalars(
                select(IgnoredLibraryFolder).where(
                    IgnoredLibraryFolder.library_root_id == root_id,
                    IgnoredLibraryFolder.relative_path == relative_path,
                )
            ).one_or_none()
            if existing is None:
                raise
    # Genre lists and mix/Discovery candidates leave ignored folders out and
    # are cached per library version.
    bump_library_version()
    return IgnoredFolderOut(path=relative_path)


@router.delete("/roots/{root_id}/ignored-folders", response_model=OkOut)
def restore_library_folder(
    root_id: int,
    body: IgnoredFolderIn,
    db: DbSession,
    user: AdminUser,
) -> dict[str, bool]:
    if db.get(LibraryRoot, root_id) is None or not can_access_root(db, user, root_id):
        raise HTTPException(404, "Library root not found")
    relative_path = _normalise_relative_folder(body.path)
    db.execute(
        delete(IgnoredLibraryFolder).where(
            IgnoredLibraryFolder.library_root_id == root_id,
            IgnoredLibraryFolder.relative_path == relative_path,
        ),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    bump_library_version()
    return {"ok": True}


@router.get("/media/{media_item_id}/lyrics", response_model=LyricsOut)
def get_lyrics(
    media_item_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    item = db.get(MediaItem, media_item_id, options=[undefer_group("lyrics")])
    if item is None or not can_access_root(db, user, item.library_root_id):
        raise HTTPException(404, "Media item not found")
    file_path = media_file_path(item)
    if file_path is None:
        raise HTTPException(404, "Underlying file is missing")
    allow = not is_path_ignored(db, item.library_root_id, item.relative_path)
    # Stored or embedded lyrics answer at once. A first look-up at LRCLIB
    # never holds the request: the answer is empty with `pending` true while
    # it runs in the background (app.deferred); ask again shortly.

    def fetch(job_db: Session) -> None:
        job_item = job_db.get(MediaItem, media_item_id, options=[undefer_group("lyrics")])
        if job_item is not None:
            get_or_fetch_lyrics(job_db, job_item, file_path)

    return answer_with_pending(
        f"lyrics:{media_item_id}",
        lambda: get_or_fetch_lyrics(db, item, file_path, allow_provider_fetch=allow),
        in_session(fetch),
    )
