from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import can_access_root
from app.collage import collage_media_item_ids
from app.custom_art import annotate_playlists, purge_custom_art
from app.deps import CurrentUser, DbSession
from app.discovery import add_more_tracks_to_auto_playlist, save_auto_playlist_as_new
from app.models import MediaItem, Playlist, PlaylistItem, User
from app.playlist_access import visible_playlist_items
from app.presenters import playlist_outs, to_media_item_outs
from app.schemas import (
    OkOut,
    PlaylistCreate,
    PlaylistDetailOut,
    PlaylistItemsAdd,
    PlaylistOut,
    PlaylistReorder,
    PlaylistUpdate,
    SaveAutoPlaylistIn,
)

router = APIRouter(prefix="/api/playlists", tags=["playlists"])


def _get_owned_playlist(db: Session, playlist_id: int, user: User) -> Playlist:
    playlist = db.get(Playlist, playlist_id)
    if playlist is None or playlist.user_id != user.id:
        raise HTTPException(404, "Playlist not found")
    return playlist


def _playlist_detail(
    db: Session, playlist: Playlist, user: User, *, offset: int = 0, limit: int | None = None
) -> PlaylistDetailOut:
    visible = visible_playlist_items(db, playlist, user)
    total = len(visible)
    page = visible[offset : None if limit is None else offset + limit]
    outs = to_media_item_outs([item.media_item for item in page], db, user)
    for out, item in zip(outs, page, strict=False):
        out.playlist_item_id = item.id
    detail = PlaylistDetailOut(
        id=playlist.id,
        name=playlist.name,
        description=playlist.description,
        created_at=playlist.created_at,
        items=outs,
        total_count=total,
    )
    annotate_playlists(db, [detail])
    return detail


@router.get("", response_model=list[PlaylistOut])
def list_playlists(
    db: DbSession, user: CurrentUser
) -> list[PlaylistOut]:
    playlists = list(
        db.scalars(
            select(Playlist).where(Playlist.user_id == user.id).order_by(Playlist.created_at, Playlist.id)
        )
    )
    return playlist_outs(db, user, playlists)


@router.post("", response_model=PlaylistOut)
def create_playlist(
    body: PlaylistCreate,
    db: DbSession,
    user: CurrentUser,
) -> PlaylistOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Playlist name cannot be blank")
    playlist = Playlist(user_id=user.id, name=name)
    db.add(playlist)
    db.commit()
    db.refresh(playlist)
    return PlaylistOut(
        id=playlist.id, name=playlist.name, created_at=playlist.created_at, item_count=0
    )


@router.post("/{playlist_id}/save-copy", response_model=PlaylistOut)
def save_copy(
    playlist_id: int,
    body: SaveAutoPlaylistIn,
    db: DbSession,
    user: CurrentUser,
) -> PlaylistOut:
    """Saves an auto-managed playlist (Discovery or a generated mix) as an
    ordinary playlist of its own, so the next refresh/generate doesn't wipe
    it out from under the user."""
    new_playlist = save_auto_playlist_as_new(db, user, playlist_id, body.name)
    if new_playlist is None:
        raise HTTPException(
            404, "Playlist not found, or it isn't an auto-managed playlist"
        )
    return PlaylistOut(
        id=new_playlist.id,
        name=new_playlist.name,
        created_at=new_playlist.created_at,
        item_count=len(new_playlist.items),
        is_auto_generated=False,
        auto_kind=None,
        preview_media_item_ids=collage_media_item_ids(new_playlist.items),
    )


@router.get("/{playlist_id}", response_model=PlaylistDetailOut)
def get_playlist(
    playlist_id: int,
    db: DbSession,
    user: CurrentUser,
    limit: int | None = Query(
        default=None, ge=1, le=5000,
        description="At most this many items, from `offset`; every item when left out.",
    ),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> PlaylistDetailOut:
    """The playlist and its items (those the user can see and play), in
    order. `total_count` is how many there are in all, whatever page."""
    playlist = _get_owned_playlist(db, playlist_id, user)
    return _playlist_detail(db, playlist, user, offset=offset, limit=limit)


@router.post("/{playlist_id}/add-more", response_model=PlaylistDetailOut)
def add_more_tracks(
    playlist_id: int,
    db: DbSession,
    user: CurrentUser,
) -> PlaylistDetailOut:
    playlist = _get_owned_playlist(db, playlist_id, user)
    if playlist.auto_kind in (None, "favorites"):
        raise HTTPException(422, "Only generated playlists can have more tracks added")
    add_more_tracks_to_auto_playlist(db, user, playlist)
    return _playlist_detail(db, playlist, user)


@router.patch("/{playlist_id}", response_model=PlaylistDetailOut)
def update_playlist(
    playlist_id: int,
    body: PlaylistUpdate,
    db: DbSession,
    user: CurrentUser,
) -> PlaylistDetailOut:
    playlist = _get_owned_playlist(db, playlist_id, user)
    if playlist.auto_kind is not None:
        # Generated, mix and Favourites playlists stay app-managed; save a
        # copy to get one that can be renamed.
        raise HTTPException(409, "Auto-managed playlists can't be edited - save a copy instead")
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Playlist name cannot be blank")
        playlist.name = name
    if "description" in body.model_fields_set:
        playlist.description = (body.description or "").strip() or None
    db.commit()
    return _playlist_detail(db, playlist, user)


@router.delete("/{playlist_id}", response_model=OkOut)
def delete_playlist(
    playlist_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    playlist = _get_owned_playlist(db, playlist_id, user)
    if playlist.auto_kind == "favorites":
        raise HTTPException(409, "Favourites is managed by favouriting tracks")
    purge_custom_art(db, "playlist", [playlist.id])
    db.delete(playlist)
    db.commit()
    return {"ok": True}


@router.post("/{playlist_id}/items", response_model=PlaylistDetailOut)
def add_items(
    playlist_id: int,
    body: PlaylistItemsAdd,
    db: DbSession,
    user: CurrentUser,
) -> PlaylistDetailOut:
    playlist = _get_owned_playlist(db, playlist_id, user)
    if playlist.auto_kind == "favorites":
        raise HTTPException(409, "Favourites is managed by favouriting tracks")

    # Normalize first so removal/recovery gaps cannot turn an append into an
    # insertion before the final item.
    ordered_items = sorted(playlist.items, key=lambda item: (item.position, item.id))
    for position, item in enumerate(ordered_items):
        item.position = position
    insert_at = (
        len(ordered_items)
        if body.position is None
        else min(body.position, len(ordered_items))
    )

    # Make room by shifting anything at/after insert_at back.
    for item in playlist.items:
        if item.position >= insert_at:
            item.position += len(body.media_item_ids)

    for offset, media_item_id in enumerate(body.media_item_ids):
        media_item = db.get(MediaItem, media_item_id)
        if media_item is None or not can_access_root(
            db, user, media_item.library_root_id
        ):
            raise HTTPException(404, f"Media item {media_item_id} not found")
        db.add(
            PlaylistItem(
                playlist_id=playlist.id,
                media_item_id=media_item_id,
                position=insert_at + offset,
            )
        )
    db.commit()
    db.refresh(playlist)
    return _playlist_detail(db, playlist, user)


@router.delete("/{playlist_id}/items/{playlist_item_id}", response_model=OkOut)
def remove_item(
    playlist_id: int,
    playlist_item_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    playlist = _get_owned_playlist(db, playlist_id, user)
    if playlist.auto_kind == "favorites":
        raise HTTPException(409, "Favourites is managed by favouriting tracks")
    item = next((i for i in playlist.items if i.id == playlist_item_id), None)
    if item is None:
        raise HTTPException(404, "Playlist item not found")
    db.delete(item)
    remaining = sorted(
        (candidate for candidate in playlist.items if candidate.id != item.id),
        key=lambda candidate: (candidate.position, candidate.id),
    )
    for position, candidate in enumerate(remaining):
        candidate.position = position
    db.commit()
    return {"ok": True}


@router.put("/{playlist_id}/reorder", response_model=PlaylistDetailOut)
def reorder_items(
    playlist_id: int,
    body: PlaylistReorder,
    db: DbSession,
    user: CurrentUser,
) -> PlaylistDetailOut:
    playlist = _get_owned_playlist(db, playlist_id, user)
    if playlist.auto_kind == "favorites":
        raise HTTPException(409, "Favourites is managed by favouriting tracks")
    current = sorted(playlist.items, key=lambda item: (item.position, item.id))
    requested = body.ordered_item_ids
    visible_ids = [item.id for item in visible_playlist_items(db, playlist, user)]
    # The client can only send what the detail showed it: songs on roots the
    # user cannot see, or disliked since a generated playlist was built, are
    # hidden there. Those keep their places; the shown ones fill the places
    # the shown ones had, in the new order. Every id is accepted too.
    if len(requested) != len(set(requested)) or set(requested) not in (
        {item.id for item in current},
        set(visible_ids),
    ):
        raise HTTPException(
            400, "ordered_item_ids must contain exactly the playlist's current items"
        )
    by_id = {item.id: item for item in current}
    moving = set(requested)
    reordered = iter(requested)
    for position, item in enumerate(current):
        target = by_id[next(reordered)] if item.id in moving else item
        target.position = position
    db.commit()
    db.refresh(playlist)
    return _playlist_detail(db, playlist, user)


@router.get("/{playlist_id}/export.m3u")
def export_m3u(
    playlist_id: int,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    playlist = _get_owned_playlist(db, playlist_id, user)
    lines = ["#EXTM3U"]
    for item in visible_playlist_items(db, playlist, user):
        media = item.media_item
        seconds = int(media.duration_seconds or 0)
        artist_title = (
            f"{media.artist} - {media.title}" if media.artist else media.title
        )
        lines.append(f"#EXTINF:{seconds},{artist_title}")
        # Do not disclose the server's host filesystem layout in a download
        # available to any library user. Prefix with the configured display
        # name so imports remain unambiguous across multiple roots.
        lines.append(f"{media.library_root.display_name}/{media.relative_path}")
    content = "\n".join(lines) + "\n"
    return Response(
        content=content,
        media_type="audio/x-mpegurl",
        headers={
            "Content-Disposition": (
                'attachment; filename="playlist.m3u"; '
                f"filename*=UTF-8''{quote(playlist.name + '.m3u')}"
            )
        },
    )
