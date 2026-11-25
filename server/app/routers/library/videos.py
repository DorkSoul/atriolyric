"""Video lists and video folders."""

from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.access import accessible_root_ids
from app.deps import CurrentUser, DbSession
from app.models import (
    MediaItem,
    MediaKind,
)
from app.presenters import to_media_item_outs
from app.schemas import (
    MediaItemOut,
    VideoFolderOut,
)

router = APIRouter()


@router.get("/videos", response_model=list[MediaItemOut])
def list_videos(
    db: DbSession,
    user: CurrentUser,
    q: Annotated[str | None, Query(max_length=200)] = None,
    dir: Annotated[str | None, Query(max_length=4096)] = None,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> list[MediaItemOut]:
    query = select(MediaItem).where(
        MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
        MediaItem.kind.in_([MediaKind.video, MediaKind.music_video]),
    )
    if q:
        like = f"%{q}%"
        query = query.where(
            (MediaItem.title.ilike(like))
            | (MediaItem.artist.ilike(like))
            | (MediaItem.performer.ilike(like))
        )
    if dir is not None:
        # The folder's own videos, filtered and paged in SQL. video_folder
        # (indexed) is the parent folder's name, which every direct child
        # shares.
        prefix = _video_dir_prefix(dir)
        query = query.where(
            MediaItem.video_folder == (PurePosixPath(prefix).name or "Unsorted"),
            MediaItem.relative_path.startswith(prefix, autoescape=True),
            func.instr(func.substr(MediaItem.relative_path, len(prefix) + 1), "/") == 0,
        )
    items = list(db.scalars(query.order_by(MediaItem.title, MediaItem.id).offset(offset).limit(limit)))
    return to_media_item_outs(items, db, user)


def _video_dir_prefix(path: str) -> str:
    path = path.strip("/")
    return f"{path}/" if path else ""


@router.get("/videos/folders", response_model=list[VideoFolderOut])
def list_video_folders(
    db: DbSession,
    user: CurrentUser,
    parent: Annotated[str, Query(max_length=4096)] = "",
) -> list[VideoFolderOut]:
    """Immediate sub-folders of `parent` (library-relative; "" = top level).

    Counts and the representative item cover everything beneath each folder.
    """
    prefix = _video_dir_prefix(parent)
    query = select(MediaItem.id, MediaItem.relative_path).where(
        MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
        MediaItem.kind.in_([MediaKind.video, MediaKind.music_video]),
    )
    if prefix:
        query = query.where(MediaItem.relative_path.startswith(prefix, autoescape=True))
    groups: dict[str, list[int]] = {}
    for media_id, relative_path in db.execute(query.order_by(MediaItem.id)).all():
        name, sep, _ = relative_path[len(prefix):].partition("/")
        if sep:
            groups.setdefault(name, []).append(media_id)
    return [
        VideoFolderOut(
            folder=name,
            path=f"{prefix}{name}",
            video_count=len(ids),
            representative_media_item_id=ids[0],
        )
        for name, ids in sorted(groups.items(), key=lambda kv: kv[0].lower())
    ]
