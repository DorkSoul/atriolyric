import logging
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, undefer

from app.access import media_file_path
from app.activity import log_action
from app.activity_log import (
    download_line,
    iter_events,
    kinds_present,
    list_events,
    wait_for_writes,
)
from app.albums import (
    assign_album,
    prune_orphan_albums,
    rebuild_albums,
    refresh_album_details,
)
from app.artist_credits import (
    credits_from_columns,
    load_split_rules,
    prune_orphan_artists,
    rebuild_all_credits,
    save_split_rules,
    sync_track_credits,
)
from app.background_status import background_status
from app.cleanup import (
    delete_media_records,
    delete_root_media_records,
    delete_user_records,
)
from app.custom_art import purge_custom_art
from app.db import count_rows
from app.deps import AdminUser, DbSession, require_admin
from app.library_cache import bump_library_version
from app.library_tasks import TaskOptions, active_task, request_stop, start_task
from app.metadata_taxonomy import replace_item_genres
from app.models import (
    CustomArt,
    IgnoredLibraryFolder,
    LibraryRoot,
    LibraryTask,
    MediaItem,
    User,
    UserLibraryAccess,
    utcnow,
)
from app.models import (
    Session as SessionModel,
)
from app.scanner import title_or_stem
from app.schemas import (
    ActivityEventOut,
    ActivityLogOut,
    AdminUserOut,
    ArtistSplitRulesIn,
    ArtistSplitRulesOut,
    ArtistSplitRulesSavedOut,
    BackgroundStatusOut,
    BulkRemoveIn,
    BulkRemoveOut,
    BulkTagEditIn,
    BulkTagEditOut,
    LibraryRootIn,
    LibraryRootOut,
    LibraryRootUpdate,
    LibraryTaskOut,
    MetadataFetchIn,
    OkOut,
    PasswordResetIn,
    RemovedCountOut,
    TagEditIn,
    UnrecordedArtOut,
    UserCreateIn,
    UserUpdateIn,
)
from app.security import hash_password
from app.tag_writer import write_tags
from app.ws_manager import close_user_sessions

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)


# --- Users ---


def _user_out(db: Session, user: User) -> AdminUserOut:
    grants = db.scalars(
        select(UserLibraryAccess.library_root_id).where(UserLibraryAccess.user_id == user.id)
    ).all()
    out = AdminUserOut.model_validate(user)
    out.accessible_root_ids = list(grants)
    return out


# Library art the art picker could fill from online sources. Playlist art is
# only ever uploaded.
_PICKABLE_ART = ("artist", "album", "media_item")


def _unrecorded_art(db: Session):
    return select(CustomArt).where(CustomArt.entity_type.in_(_PICKABLE_ART), CustomArt.source.is_(None))


@router.get("/custom-art/unrecorded", response_model=UnrecordedArtOut)
def get_unrecorded_art(db: DbSession) -> dict:
    """How much library art has no recorded source. Some may be Deezer
    images, which Deezer's terms do not allow keeping."""
    return {"count": len(db.execute(_unrecorded_art(db)).scalars().all())}


@router.post("/custom-art/unrecorded/remove", response_model=RemovedCountOut)
def remove_unrecorded_art(db: DbSession) -> dict:
    """Remove that art (files included); each item falls back to its
    default art and can be picked again."""
    rows = db.execute(_unrecorded_art(db)).scalars().all()
    by_type: dict[str, list[str]] = {}
    for row in rows:
        by_type.setdefault(row.entity_type, []).append(row.entity_key)
    for entity_type, keys in by_type.items():
        purge_custom_art(db, entity_type, keys)
    db.commit()
    bump_library_version()
    log_action("art.remove_unrecorded", removed=len(rows))
    return {"removed": len(rows)}


@router.get("/users", response_model=list[AdminUserOut])
def list_users(db: DbSession) -> list[AdminUserOut]:
    return [_user_out(db, u) for u in db.scalars(select(User).order_by(User.id))]


@router.post("/users", response_model=AdminUserOut)
def create_user(body: UserCreateIn, db: DbSession) -> AdminUserOut:
    username = body.username.strip()
    if not username:
        raise HTTPException(422, "Username cannot be blank")
    if db.scalars(select(User).where(User.username == username).limit(1)).first() is not None:
        raise HTTPException(409, "Username already taken")
    try:
        password_hash = hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    user = User(username=username, password_hash=password_hash, is_admin=body.is_admin)
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action("user.create", target=f"{user.username}#{user.id}", is_admin=user.is_admin)
    return _user_out(db, user)


@router.delete("/users/{user_id}", response_model=OkOut)
def delete_user(
    user_id: int,
    background_tasks: BackgroundTasks,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    if user_id == admin.id:
        raise HTTPException(400, "Cannot delete your own account")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    username = user.username
    delete_user_records(db, user_id)
    db.commit()
    # Their login sessions are gone, but an open playback socket would keep
    # receiving the queue (and accepting commands) until it reconnected.
    background_tasks.add_task(close_user_sessions, user_id, None)
    log_action("user.delete", target=f"{username}#{user_id}")
    return {"ok": True}


def _other_user(db: Session, user_id: int, admin: User, action: str) -> User:
    if user_id == admin.id:
        raise HTTPException(400, f"Cannot {action} your own account here")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(
    user_id: int,
    body: UserUpdateIn,
    db: DbSession,
    admin: AdminUser,
) -> AdminUserOut:
    user = _other_user(db, user_id, admin, "change the role of")
    if user.is_admin and not body.is_admin:
        # The caller can't target themselves, so over HTTP another admin
        # always remains; this is a floor in case that ever stops holding.
        other_admins = count_rows(
            db, select(User.id).where(User.is_admin.is_(True), User.id != user.id)
        )
        if other_admins == 0:
            raise HTTPException(409, "Cannot remove the last admin")
    user.is_admin = body.is_admin
    db.commit()
    db.refresh(user)
    log_action("user.role", target=f"{user.username}#{user.id}", is_admin=user.is_admin)
    return _user_out(db, user)


@router.post("/users/{user_id}/password", response_model=OkOut)
def reset_user_password(
    user_id: int,
    body: PasswordResetIn,
    background_tasks: BackgroundTasks,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    user = _other_user(db, user_id, admin, "reset the password of")
    try:
        user.password_hash = hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    # Unlike a self-service change there's no browser to keep: every one of
    # the target's logins is revoked so the new password is the only way in.
    db.execute(
        delete(SessionModel).where(SessionModel.user_id == user.id),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    background_tasks.add_task(close_user_sessions, user.id, None)
    log_action("user.password_reset", target=f"{user.username}#{user.id}")
    return {"ok": True}


# --- Library roots ---


def _task_out(task: LibraryTask) -> LibraryTaskOut:
    now = utcnow()
    elapsed = max((now - task.started_at).total_seconds(), 0.001)
    rate = task.processed / elapsed if task.processed else None
    eta = None
    if rate and task.status == "running" and task.total >= task.processed:
        eta = max(0, round((task.total - task.processed) / rate))
    return LibraryTaskOut(
        id=task.id,
        kind=task.kind,
        status=task.status,
        total=task.total,
        processed=task.processed,
        added=task.added,
        updated=task.updated,
        removed=task.removed,
        matched=task.matched,
        error=task.error,
        started_at=task.started_at,
        updated_at=task.updated_at,
        completed_at=task.completed_at,
        stage=task.stage,
        missing_held=task.missing_held or 0,
        note=task.note,
        items_per_second=rate,
        eta_seconds=eta,
    )


def _last_task(db: Session, root_id: int) -> LibraryTask | None:
    return db.scalars(
        select(LibraryTask)
        .where(LibraryTask.library_root_id == root_id)
        .order_by(LibraryTask.started_at.desc())
        .limit(1)
    ).first()


def _root_out(db: Session, root: LibraryRoot) -> LibraryRootOut:
    task = active_task(db, root.id)
    # A background worker can commit between the roots query and this
    # conversion. Refresh after reading the task so the UI cannot observe a
    # failed/completed task alongside stale root scan fields.
    db.refresh(root)
    result = LibraryRootOut.model_validate(root)
    result.active_task = _task_out(task) if task else None
    last_task = _last_task(db, root.id)
    result.last_task = _task_out(last_task) if last_task else None
    return result


@router.get("/roots", response_model=list[LibraryRootOut])
def list_roots(db: DbSession) -> list[LibraryRootOut]:
    return [_root_out(db, r) for r in db.scalars(select(LibraryRoot))]


def _normalize_root_path(raw: str) -> str:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(422, "Library root path must be absolute")
    return str(path.resolve(strict=False))


@router.post("/roots", response_model=LibraryRootOut)
def create_root(body: LibraryRootIn, db: DbSession) -> LibraryRootOut:
    normalized_path = _normalize_root_path(body.path)
    if (
        db.scalars(select(LibraryRoot).where(LibraryRoot.path == normalized_path).limit(1)).first()
        is not None
    ):
        raise HTTPException(409, "Library root already exists")
    display_name = body.display_name.strip()
    if not display_name:
        raise HTTPException(422, "Display name cannot be blank")
    root = LibraryRoot(
        path=normalized_path, display_name=display_name, is_shared=body.is_shared
    )
    db.add(root)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Library root already exists") from None
    db.refresh(root)
    log_action(
        "root.create", root=root.id, name=root.display_name, path=root.path,
        shared=root.is_shared,
    )
    return _root_out(db, root)


@router.patch("/roots/{root_id}", response_model=LibraryRootOut)
def update_root(
    root_id: int, body: LibraryRootUpdate, db: DbSession
) -> LibraryRootOut:
    root = db.get(LibraryRoot, root_id)
    if root is None:
        raise HTTPException(404, "Library root not found")
    before = (root.display_name, root.path, root.is_shared)
    if body.display_name is not None:
        display_name = body.display_name.strip()
        if not display_name:
            raise HTTPException(422, "Display name cannot be blank")
        root.display_name = display_name
    if body.path is not None:
        normalized_path = _normalize_root_path(body.path)
        if normalized_path != root.path:
            if active_task(db, root_id) is not None:
                raise HTTPException(
                    409, "Wait for the running library task to finish before changing the path"
                )
            clash = db.scalars(
                select(LibraryRoot)
                .where(LibraryRoot.path == normalized_path, LibraryRoot.id != root_id)
                .limit(1)
            ).first()
            if clash is not None:
                raise HTTPException(409, "Library root already exists")
            # Tracks are stored relative to the root, so they follow the new
            # path; a rescan then reconciles anything that differs there.
            root.path = normalized_path
    if body.is_shared is not None:
        # Existing per-user grants are kept while a root is shared so that
        # switching back to private restores who had access.
        root.is_shared = body.is_shared
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Library root already exists") from None
    db.refresh(root)
    log_action(
        "root.update", root=root.id,
        before={"name": before[0], "path": before[1], "shared": before[2]},
        after={"name": root.display_name, "path": root.path, "shared": root.is_shared},
    )
    return _root_out(db, root)


@router.delete("/roots/{root_id}", response_model=OkOut)
def delete_root(root_id: int, db: DbSession) -> dict:
    root = db.get(LibraryRoot, root_id)
    if root is None:
        raise HTTPException(404, "Library root not found")
    busy = "Wait for the running library task to finish before removing the library"
    if active_task(db, root_id) is not None:
        # The worker would go on writing rows for a root that no longer exists.
        raise HTTPException(409, busy)
    # Removing an indexed library root must never mutate or delete user-owned
    # source files; only database rows and generated Atriolyric caches are cleaned.
    # The tracks go first, in committed batches; the root row goes last, so
    # whatever a failure leaves behind still belongs to a root.
    removed = delete_root_media_records(db, root_id)
    if active_task(db, root_id) is not None:
        # Started while the batches ran (each commit lets other writers in).
        raise HTTPException(409, busy)
    unsynced = {"synchronize_session": False}
    db.execute(
        delete(IgnoredLibraryFolder).where(IgnoredLibraryFolder.library_root_id == root_id),
        execution_options=unsynced,
    )
    db.execute(
        delete(UserLibraryAccess).where(UserLibraryAccess.library_root_id == root_id),
        execution_options=unsynced,
    )
    db.execute(delete(LibraryTask).where(LibraryTask.library_root_id == root_id), execution_options=unsynced)
    name, path = root.display_name, root.path
    try:
        db.execute(delete(LibraryRoot).where(LibraryRoot.id == root_id), execution_options=unsynced)
        db.commit()
    except IntegrityError:
        # A track added since the last batch (a scan that slipped in) still
        # points at the root.
        db.rollback()
        raise HTTPException(409, busy) from None
    # Artists and albums only this library had (the next scan of another
    # library would otherwise be what removes them).
    prune_orphan_artists(db)
    prune_orphan_albums(db)
    log_action("root.delete", root=root_id, name=name, path=path, tracks=removed)
    return {"ok": True}


def _start_task(
    db: Session,
    root_id: int,
    kind: str,
    scan_mode: str = "normal",
    user_id: int | None = None,
) -> LibraryTaskOut:
    root = db.get(LibraryRoot, root_id)
    if root is None:
        raise HTTPException(404, "Library root not found")
    options = TaskOptions(scan_mode=scan_mode, user_id=user_id)
    task = start_task(db, root_id, kind, options)
    if task is None:
        raise HTTPException(409, "A library task is already running for this root")
    return _task_out(task)


@router.post(
    "/roots/{root_id}/tasks/{task_id}/stop",
    response_model=LibraryTaskOut,
)
def stop_task(
    root_id: int, task_id: int, db: DbSession
) -> LibraryTaskOut:
    task = db.get(LibraryTask, task_id)
    if task is None or task.library_root_id != root_id:
        raise HTTPException(404, "Library task not found")
    if task.status == "running":
        task.status = "stopping"
        task.updated_at = utcnow()
        db.commit()
        log_action("task.stop_requested", task=task.id, root=root_id, kind=task.kind)
        request_stop(task.id)
    return _task_out(task)


@router.post("/roots/{root_id}/scan", response_model=LibraryTaskOut, status_code=202)
def scan_root(
    root_id: int,
    db: DbSession,
    mode: Literal["normal", "metadata", "full", "confirm_removals"] = Query(default="normal"),
) -> LibraryTaskOut:
    """normal: new/changed files only; metadata: also re-read the tags and
    embedded lyrics of every file; full: redo everything for every file;
    confirm_removals: a normal scan that also removes the missing tracks an
    earlier scan held back (never while a folder is unreadable)."""
    return _start_task(db, root_id, "scan", scan_mode=mode)


@router.post("/roots/{root_id}/enrich", response_model=LibraryTaskOut, status_code=202)
def enrich_root(
    root_id: int,
    db: DbSession,
    admin: AdminUser,
    body: MetadataFetchIn = MetadataFetchIn(),  # noqa: B008 - FastAPI default body; never mutated
) -> LibraryTaskOut:
    """Begin one explicit stage of the metadata pipeline for this root."""
    kinds = {
        "mbids": "mb_mbid",
        "musicbrainz_missing": "enrich",
        "isrc_mbid": "mb_isrc",
        "musicbrainz_fallback": "enrich",
    }
    if body.mode in {"listenbrainz_missing", "listenbrainz_all"}:
        return _start_task(db, root_id, "lb_tags_all" if body.mode == "listenbrainz_all" else "lb_tags")
    if body.mode == "artist_info":
        return _start_task(db, root_id, "artist_info", user_id=admin.id)
    return _start_task(db, root_id, kinds[body.mode])


@router.post("/roots/{root_id}/full-setup", response_model=LibraryTaskOut, status_code=202)
def full_setup_root(
    root_id: int,
    db: DbSession,
    admin: AdminUser,
) -> LibraryTaskOut:
    """Rescan the root and refetch everything from MusicBrainz and ListenBrainz."""
    return _start_task(db, root_id, "full_setup", user_id=admin.id)


@router.post("/roots/{root_id}/full-setup-new", response_model=LibraryTaskOut, status_code=202)
def full_setup_new_root(
    root_id: int,
    db: DbSession,
    admin: AdminUser,
) -> LibraryTaskOut:
    """Scan for new files, then fetch everything only for what is missing."""
    return _start_task(db, root_id, "full_setup_new", user_id=admin.id)


def _cleared_to_none(value: str) -> str | None:
    """An empty or whitespace-only edit clears the field: the scanner stores
    a missing tag as NULL, never as an empty string."""
    return value if value.strip() else None


def _apply_tag_edit(db: Session, item: MediaItem, body: TagEditIn) -> bool:
    """Writes to the real file first - only touches the DB row (and only
    the fields that were actually sent) once that succeeds, so a failed
    disk write (e.g. a read-only mount) never leaves the DB claiming a tag
    that isn't really on the file.

    The row and what is derived from it end up as a scan of the edited file
    would leave them, and the stored mtime and size are the file's new
    ones, so the next normal scan sees it as unchanged (and keeps, say, its
    loudness measurement)."""
    file_path = media_file_path(item)
    if file_path is None:
        logger.warning("Tag edit skipped: media %s has no readable file", item.id)
        return False
    ok = write_tags(
        file_path,
        title=body.title,
        artist=body.artist,
        album=body.album,
        genre=body.genre,
        track_number=body.track_number,
    )
    if not ok:
        logger.warning("Tag edit failed: could not write tags to media %s", item.id)
        return False
    previous_names = (item.title, item.artist)
    if body.title is not None:
        item.title = title_or_stem(_cleared_to_none(body.title), file_path)
    if body.artist is not None:
        item.artist = _cleared_to_none(body.artist)
    if body.album is not None:
        item.album = _cleared_to_none(body.album)
    if body.genre is not None:
        item.genre = _cleared_to_none(body.genre)
        # An editor's explicit genre value replaces the embedded-genre source
        # used by mix selection, while MusicBrainz genres remain additive.
        replace_item_genres(db, item, [item.genre], "embedded")
    if body.track_number is not None:
        item.track_number = body.track_number
    if body.title is not None or body.artist is not None:
        # The edit wrote a single-value artist tag, so the stored strings are
        # now the whole truth about who the track is by.
        if body.artist is not None:
            item.artist_tags_json = None
        rules = load_split_rules(db)
        sync_track_credits(db, item, credits_from_columns(item, rules))
        # As a rescan does: a track not matched yet is looked up again
        # under its new name.
        if item.musicbrainz_metadata_json is None and (item.title, item.artist) != previous_names:
            item.musicbrainz_lookup_status = None
            item.musicbrainz_last_error = None
    if body.album is not None or body.artist is not None:
        album = assign_album(db, item)
        if album is not None:
            refresh_album_details(db, album)
    if file_path.is_file():
        stat = file_path.stat()
        item.file_mtime = stat.st_mtime
        item.file_size = stat.st_size
    db.commit()
    if body.artist is not None or body.album is not None:
        # An artist or album this track was the last one of goes, as after a scan.
        prune_orphan_artists(db)
        prune_orphan_albums(db)
    bump_library_version()
    log_action(
        "media.tags_edit", media=item.id,
        fields=sorted(k for k, v in body.model_dump(exclude={"media_item_ids"}).items() if v is not None),
    )
    return True


@router.get("/background-status", response_model=BackgroundStatusOut)
def get_background_status(db: DbSession) -> dict:
    """How far the loudness pass and audio analysis have got, and whether
    each is running now. The counts are cached for 15 seconds."""
    return background_status(db)


def _split_rules_out(rules) -> dict:
    return {"separators": list(rules.separators), "exceptions": list(rules.exceptions)}


@router.get("/artist-split-rules", response_model=ArtistSplitRulesOut)
def get_artist_split_rules(db: DbSession) -> dict:
    """How artist tags are split into separate artists (app.artist_credits)."""
    return _split_rules_out(load_split_rules(db))


@router.put("/artist-split-rules", response_model=ArtistSplitRulesSavedOut)
def update_artist_split_rules(body: ArtistSplitRulesIn, db: DbSession) -> dict:
    """Save the rules, then re-link every track from its stored tags - no
    file is re-read, so this is quick even for a big library."""
    rules = save_split_rules(db, body.separators, body.exceptions)
    relinked = rebuild_all_credits(db)
    rebuild_albums(db)
    bump_library_version()
    log_action("library.artist_split_rules", separators=len(rules.separators), exceptions=len(rules.exceptions))
    return {**_split_rules_out(rules), "relinked": relinked}


# _apply_tag_edit reads both deferred columns: the stored multi-value artist
# tags to rebuild credits, and the recording to tell a matched track.
_TAG_EDIT_LOAD = [undefer(MediaItem.artist_tags_json), undefer(MediaItem.musicbrainz_metadata_json)]


@router.patch("/media/{media_item_id}/tags", response_model=OkOut)
def edit_tags(
    media_item_id: int, body: TagEditIn, db: DbSession
) -> dict:
    item = db.get(MediaItem, media_item_id, options=_TAG_EDIT_LOAD)
    if item is None:
        raise HTTPException(404, "Media item not found")
    if not _apply_tag_edit(db, item, body):
        raise HTTPException(
            422, "Couldn't write to the file - check the library mount is writable"
        )
    return {"ok": True}


@router.post("/media/bulk-tags", response_model=BulkTagEditOut)
def bulk_edit_tags(body: BulkTagEditIn, db: DbSession) -> dict:
    """Each item is its own transaction: one that fails is reported (and
    rolled back) without undoing or stopping the rest."""
    succeeded: list[int] = []
    failed: list[dict] = []
    for media_item_id in body.media_item_ids:
        item = db.get(MediaItem, media_item_id, options=_TAG_EDIT_LOAD)
        if item is None:
            failed.append({"id": media_item_id, "reason": "Not found"})
            continue
        try:
            written = _apply_tag_edit(db, item, body)
        except Exception:  # report per-track database failures
            logger.exception("Could not save the tag edit of media %s", media_item_id)
            db.rollback()
            failed.append({"id": media_item_id, "reason": "Couldn't save the change"})
            continue
        if written:
            succeeded.append(media_item_id)
        else:
            failed.append({"id": media_item_id, "reason": "Couldn't write to the file"})
    log_action("media.bulk_tags", succeeded=len(succeeded), failed=len(failed))
    return {"updated": len(succeeded), "succeeded": succeeded, "failed": failed}


@router.post("/media/bulk-remove", response_model=BulkRemoveOut)
def bulk_remove_media(body: BulkRemoveIn, db: DbSession) -> dict:
    """Remove tracks from Atriolyric's index without touching their source files."""
    removed, failed = [], []
    for media_item_id in body.media_item_ids:
        item = db.get(MediaItem, media_item_id)
        if item is None:
            failed.append(media_item_id)
            continue
        try:
            delete_media_records(db, [media_item_id])
            db.commit()
            removed.append(media_item_id)
        except Exception:  # report per-track database failures
            logger.exception("Could not remove media %s from the library", media_item_id)
            db.rollback()
            failed.append(media_item_id)
    if removed:
        # Artists and albums whose last track this was.
        prune_orphan_artists(db)
        prune_orphan_albums(db)
    log_action("media.bulk_remove", removed=len(removed), failed=len(failed))
    return {"removed": removed, "failed": failed}


@router.post("/roots/{root_id}/access/{user_id}", response_model=OkOut)
def grant_access(root_id: int, user_id: int, db: DbSession) -> dict:
    if db.get(LibraryRoot, root_id) is None:
        raise HTTPException(404, "Library root not found")
    if db.get(User, user_id) is None:
        raise HTTPException(404, "User not found")
    existing = db.scalars(
        select(UserLibraryAccess)
        .where(
            UserLibraryAccess.user_id == user_id,
            UserLibraryAccess.library_root_id == root_id,
        )
        .limit(1)
    ).first()
    if existing is None:
        db.add(UserLibraryAccess(user_id=user_id, library_root_id=root_id))
        db.commit()
        log_action("root.access_grant", root=root_id, user=user_id)
    return {"ok": True}


@router.delete("/roots/{root_id}/access/{user_id}", response_model=OkOut)
def revoke_access(root_id: int, user_id: int, db: DbSession) -> dict:
    db.execute(
        delete(UserLibraryAccess).where(
            UserLibraryAccess.user_id == user_id,
            UserLibraryAccess.library_root_id == root_id,
        )
    )
    db.commit()
    log_action("root.access_revoke", root=root_id, user=user_id)
    return {"ok": True}


# --- Activity log ---

# An exact kind ("task.finish"), or a group as its prefix with the dot ("log.").
ActivityKind = Annotated[str | None, Query(max_length=64)]
# "problems" is warnings and errors together.
ActivityLevel = Annotated[Literal["info", "warning", "error", "problems"] | None, Query()]
# The day as "YYYY-MM-DD" on the viewer's clock (see tz_offset_minutes).
ActivityDay = Annotated[date | None, Query()]
# JavaScript's Date.getTimezoneOffset(): minutes to add to local time for UTC
# (-60 in Central European winter). Sets which hours `day` covers and the
# times in the download.
TzOffset = Annotated[int, Query(ge=-840, le=840)]


@router.get("/activity", response_model=ActivityLogOut)
def get_activity(
    db: DbSession,
    kind: ActivityKind = None,
    level: ActivityLevel = None,
    day: ActivityDay = None,
    tz_offset_minutes: TzOffset = 0,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ActivityLogOut:
    """The stored activity log, newest first, a page at a time."""
    # Events are written by a background thread; what was just done (this
    # admin's own sign-in, say) should be in the page.
    wait_for_writes(2)
    rows, next_before = list_events(
        db, kind=kind, level=level, day=day, tz_offset_minutes=tz_offset_minutes,
        before_id=before_id, limit=limit,
    )
    return ActivityLogOut(
        events=[ActivityEventOut.model_validate(row) for row in rows],
        next_before_id=next_before,
        kinds=kinds_present(db),
    )


@router.get(
    "/activity/download",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}, "description": "One event per line"}},
)
def download_activity(
    db: DbSession,
    kind: ActivityKind = None,
    level: ActivityLevel = None,
    day: ActivityDay = None,
    tz_offset_minutes: TzOffset = 0,
) -> Response:
    """Every matching event as text, newest first:
    "YYYY-MM-DD HH:MM:SS  kind  summary" in the viewer's local time."""
    wait_for_writes(2)
    text = "".join(
        download_line(event, tz_offset_minutes) + "\n"
        for event in iter_events(db, kind=kind, level=level, day=day, tz_offset_minutes=tz_offset_minutes)
    )
    name = f"atriolyric-activity-{day.isoformat() if day else 'all'}.txt"
    return Response(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
