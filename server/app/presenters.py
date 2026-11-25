"""Turning rows into response models, shared by several routers and
services."""

from typing import cast

from fastapi import Response
from sqlalchemy import String, func, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.albums import section_for
from app.artist_credits import ROLE_ALBUM_ARTIST, annotate_credits, credits_for
from app.custom_art import annotate_items, annotate_playlists
from app.models import MediaItem, PlayHistory, Playlist, PlaylistItem, User
from app.preferences import playlist_item_visible, preference_overlay
from app.schemas import (
    AlbumOut,
    ArtistCreditOut,
    MediaItemOut,
    PlaylistAutoKind,
    PlaylistOut,
)

# --- art responses ------------------------------------------------------------

# Art is fetched ahead of display and set aside for later, so let the browser
# reuse it for a day without a round trip. Vary: Cookie for the same reason as
# the browse lists (see app.routers.library): one account's cached art must
# not be served to another account sharing the browser.
ART_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400", "Vary": "Cookie"}


# An item with no artwork is an ordinary answer, not a missing page: a grid of
# art-less tiles asks for each one, and a burst of 404s from one IP is exactly
# what CrowdSec's http-probing scenario bans. So "no art" is a 204 (the client
# shows its fallback), cached briefly so scrolling back doesn't ask again but a
# later rescan that finds art shows up within the hour. 404 stays for items
# that don't exist or that the user can't access.
NO_ART_CACHE_HEADERS = {"Cache-Control": "private, max-age=3600", "Vary": "Cookie"}


def no_art_response() -> Response:
    return Response(status_code=204, headers=NO_ART_CACHE_HEADERS)


# --- tracks, albums and playlists -----------------------------------------------


def apply_overlay(out: MediaItemOut, overlay: dict) -> MediaItemOut:
    out.is_favorite, out.is_disliked, out.excluded_by = overlay.get(
        out.id, (False, False, None)
    )
    return out


def annotate_play_counts(db: Session, user: User, outs: list[MediaItemOut]) -> None:
    """Fill each item's play_count with `user`'s plays of it (0 for never),
    in one grouped query for the whole page - not one per row."""
    ids = list({out.id for out in outs})
    counts: dict[int, int] = {}
    if ids:
        counts = {
            media_item_id: plays
            for media_item_id, plays in db.execute(
                select(PlayHistory.media_item_id, func.count(PlayHistory.id))
                .where(PlayHistory.user_id == user.id, PlayHistory.media_item_id.in_(ids))
                .group_by(PlayHistory.media_item_id)
            ).all()
        }
    for out in outs:
        out.play_count = counts.get(out.id, 0)


def to_media_item_outs(
    items: list[MediaItem], db: Session, user: User
) -> list[MediaItemOut]:
    """MediaItemOut for each item, with the user's like/dislike state,
    custom art and artist credits."""
    # One query for every item's per-user like/dislike state (see
    # app.preferences.preference_overlay) - a per-item version would mean several
    # thousand sequential round trips just to render the Songs tab.
    overlay = preference_overlay(db, user, [item.id for item in items])
    outs = [apply_overlay(MediaItemOut.model_validate(item), overlay) for item in items]
    annotate_items(db, outs)
    annotate_credits(db, outs)
    return outs


def album_dicts(db: Session, rows) -> list[dict]:
    """AlbumOut dicts for grouped album rows, with each album's owners split
    into artists (from its representative track's album-artist links)."""
    owners = credits_for(
        db, [row.representative_media_item_id for row in rows], (ROLE_ALBUM_ARTIST,)
    )
    return [
        AlbumOut(
            artist=row.artist,
            album=row.album,
            track_count=row.track_count,
            representative_media_item_id=row.representative_media_item_id,
            artists=[
                ArtistCreditOut(**credit)
                for credit in owners.get(row.representative_media_item_id, [])
            ],
            **album_type_fields(row),
        ).model_dump()
        for row in rows
    ]


def album_type_fields(row) -> dict:
    """Release type and original date for an album row, when the query
    selected them (search results don't)."""
    primary = getattr(row, "primary_type", None)
    secondary = getattr(row, "secondary_types_json", None) or []
    return {
        "primary_type": primary,
        "secondary_types": secondary,
        "section": section_for(primary, secondary),
        "release_date": getattr(row, "release_date", None),
        "release_group_mbid": getattr(row, "release_group_mbid", None),
    }


def playlist_outs(db: Session, user: User, playlists: list[Playlist]) -> list[PlaylistOut]:
    """Summaries for `playlists` (in the given order): item counts and
    collage previews, both limited to roots `user` can access. Shared by
    the playlist list and Home search."""
    playlist_ids = [playlist.id for playlist in playlists]
    roots = accessible_root_ids(db, user)
    counts: dict[int, int] = {}
    previews: dict[int, list[int]] = {}
    if playlist_ids and roots:
        counts = dict(
            db.execute(
                select(PlaylistItem.playlist_id, func.count(PlaylistItem.id))
                .join(MediaItem, MediaItem.id == PlaylistItem.media_item_id)
                .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
                .where(
                    PlaylistItem.playlist_id.in_(playlist_ids),
                    MediaItem.library_root_id.in_(roots),
                    playlist_item_visible(user.id),
                )
                .group_by(PlaylistItem.playlist_id)
            ).all()
        )

        artist_key = func.coalesce(MediaItem.artist, sql_cast(MediaItem.id, String))
        ranked_artists = (
            select(
                PlaylistItem.playlist_id.label("playlist_id"),
                PlaylistItem.media_item_id.label("media_item_id"),
                PlaylistItem.position.label("position"),
                PlaylistItem.id.label("playlist_item_id"),
                func.row_number()
                .over(
                    partition_by=(PlaylistItem.playlist_id, artist_key),
                    order_by=(PlaylistItem.position, PlaylistItem.id),
                )
                .label("artist_rank"),
            )
            .join(MediaItem, MediaItem.id == PlaylistItem.media_item_id)
            .where(
                PlaylistItem.playlist_id.in_(playlist_ids),
                MediaItem.library_root_id.in_(roots),
            )
            .subquery()
        )
        preview_candidates = (
            select(
                ranked_artists.c.playlist_id,
                ranked_artists.c.media_item_id,
                func.row_number()
                .over(
                    partition_by=ranked_artists.c.playlist_id,
                    order_by=(
                        ranked_artists.c.position,
                        ranked_artists.c.playlist_item_id,
                    ),
                )
                .label("preview_rank"),
            )
            .where(ranked_artists.c.artist_rank == 1)
            .subquery()
        )
        preview_rows = db.execute(
            select(
                preview_candidates.c.playlist_id,
                preview_candidates.c.media_item_id,
            )
            .where(preview_candidates.c.preview_rank <= 4)
            .order_by(
                preview_candidates.c.playlist_id,
                preview_candidates.c.preview_rank,
            )
        ).all()
        for playlist_id, media_item_id in preview_rows:
            previews.setdefault(playlist_id, []).append(media_item_id)

    result = []
    for p in playlists:
        result.append(
            PlaylistOut(
                id=p.id,
                name=p.name,
                description=p.description,
                created_at=p.created_at,
                item_count=counts.get(p.id, 0),
                is_auto_generated=p.is_auto_generated,
                # Checked by the model; the column is untyped text.
                auto_kind=cast(PlaylistAutoKind | None, p.auto_kind),
                preview_media_item_ids=previews.get(p.id, []),
            )
        )
    annotate_playlists(db, result)
    return result
