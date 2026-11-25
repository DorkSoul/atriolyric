"""Which of a playlist's items a user sees: shared by the playlist detail
and the playlist download."""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.access import accessible_root_ids
from app.models import Playlist, PlaylistItem, User
from app.preferences import visible_auto_items


def visible_playlist_items(
    db: Session, playlist: Playlist, user: User
) -> list[PlaylistItem]:
    """The playlist's items `user` may see and play, in order: songs in roots
    they can access, minus (for a generated playlist) ones disliked since."""
    roots = accessible_root_ids(db, user)
    # Every track in one go: reading item.media_item per row would be a query per
    # song of the playlist.
    rows = db.scalars(
        select(PlaylistItem)
        .options(selectinload(PlaylistItem.media_item))
        .where(PlaylistItem.playlist_id == playlist.id)
        .order_by(PlaylistItem.position, PlaylistItem.id)
    ).all()
    items = [
        item
        for item in rows
        if item.media_item is not None and item.media_item.library_root_id in roots
    ]
    # Generated playlists hide songs disliked since they were built. This is
    # also what plays them - the app queues a playlist from this detail.
    return visible_auto_items(db, playlist=playlist, items=items, user=user)
