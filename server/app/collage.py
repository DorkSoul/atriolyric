"""Collage-preview selection shared by routers and discovery.

This deliberately lives in its own low-level module with no router or
discovery imports: app.routers.playlists imports app.discovery for its
auto-playlist endpoints, so app.discovery can never import back from the
router without a circular import.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import PlaylistItem


def collage_media_item_ids(
    items: Sequence[PlaylistItem], limit: int = 4
) -> list[int]:
    """Up to `limit` media item ids, distinct by artist where possible - same
    "gives a real sense of what's in it" reasoning as a genre mix's cover
    collage on Home, rather than one generic icon for every playlist.

    Items are read in playlist-position order; entries whose media item is
    missing (orphaned rows) are skipped rather than crashing a refresh or
    the startup backfill."""
    seen_artists: set[str] = set()
    ids: list[int] = []
    for item in items:
        media = item.media_item
        if media is None:
            continue
        key = media.artist or f"id:{media.id}"
        if key in seen_artists:
            continue
        seen_artists.add(key)
        ids.append(media.id)
        if len(ids) == limit:
            break
    return ids
