"""Recently added and played items, and the play-source history."""

import json
from typing import Any, cast

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.deps import CurrentUser, DbSession
from app.library_query import filter_tracks_query
from app.models import (
    MediaItem,
    MediaKind,
    PlayHistory,
    Playlist,
    PlaylistItem,
    PlaySourceHistory,
    User,
    utcnow,
)
from app.presenters import to_media_item_outs
from app.schemas import (
    MediaItemOut,
    PlaySourceIn,
    PlaySourceOut,
)

router = APIRouter()


# Keep this bounded - a long listening history shouldn't grow the table
# without limit, and only the most recent sources are ever shown anyway.
MAX_PLAY_SOURCE_HISTORY = 30
# Recently played reads the play history in windows of this many plays per
# track asked for (at least the minimum) until it has enough distinct tracks.
RECENT_PLAYS_WINDOW_FACTOR = 8
RECENT_PLAYS_MIN_WINDOW = 200


@router.get("/recently-added", response_model=list[MediaItemOut])
def recently_added(
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=25, ge=1, le=200),
) -> list[MediaItemOut]:
    items = list(
        db.scalars(
            select(MediaItem)
            .where(
                MediaItem.library_root_id.in_(accessible_root_ids(db, user)),
                MediaItem.kind == MediaKind.track,
            )
            .order_by(MediaItem.created_at.desc())
            .limit(limit)
        )
    )
    return to_media_item_outs(items, db, user)


@router.get("/recently-played", response_model=list[MediaItemOut])
def recently_played(
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=25, ge=1, le=200),
) -> list[MediaItemOut]:
    """The user's most recently played tracks, newest play first, each once.
    The history is read newest first in windows only until `limit` distinct
    tracks are found, instead of grouping the whole play history."""
    accessible = accessible_root_ids(db, user)
    window = max(limit * RECENT_PLAYS_WINDOW_FACTOR, RECENT_PLAYS_MIN_WINDOW)
    ids: list[int] = []
    seen: set[int] = set()
    offset = 0
    while len(ids) < limit:
        batch = db.scalars(
            select(PlayHistory.media_item_id)
            .join(MediaItem, MediaItem.id == PlayHistory.media_item_id)
            .where(
                PlayHistory.user_id == user.id,
                MediaItem.library_root_id.in_(accessible),
                MediaItem.kind == MediaKind.track,
            )
            .order_by(PlayHistory.played_at.desc(), PlayHistory.id.desc())
            .offset(offset)
            .limit(window)
        ).all()
        for media_item_id in batch:
            if media_item_id not in seen:
                seen.add(media_item_id)
                ids.append(media_item_id)
                if len(ids) == limit:
                    break
        if len(batch) < window:
            break
        offset += window
    by_id = {item.id: item for item in db.scalars(select(MediaItem).where(MediaItem.id.in_(ids)))} if ids else {}
    return to_media_item_outs([by_id[i] for i in ids if i in by_id], db, user)


@router.post("/play-source", status_code=204)
def record_play_source(
    body: PlaySourceIn,
    db: DbSession,
    user: CurrentUser,
) -> None:
    """Records *what* was played (an album, artist, playlist, genre mix, or
    radio station) - not the individual track, which /stream/{id}/played
    already covers - so "Recently played" can resurface the whole thing to
    resume instead of a single song stranded out of its original context.
    Starting the same source again just bumps its timestamp rather than
    growing a duplicate row, and the table is trimmed to the most recent
    MAX_PLAY_SOURCE_HISTORY per user on every write."""
    payload = dict(body.payload)
    if body.kind == "playlist" and payload.get("playlist_id"):
        playlist = db.get(Playlist, payload["playlist_id"])
        # Only the caller's own playlist: another user's id must not reveal
        # whether it exists or is a mix.
        if playlist is not None and playlist.user_id == user.id and playlist.auto_kind == "mix":
            payload["auto_kind"] = "mix"
    art_media_item_id = _resolve_play_source_art(db, user, body.kind, payload)
    preview_media_item_ids = (
        _library_source_preview_ids(db, user, payload)
        if body.kind == "library"
        else []
    )
    existing = db.scalars(
        select(PlaySourceHistory)
        .where(
            PlaySourceHistory.user_id == user.id,
            PlaySourceHistory.kind == body.kind,
            # Matches the stored text: the column encodes `payload` exactly as
            # it was encoded when the row was written (sorted keys).
            PlaySourceHistory.payload_json == payload,
        )
        .limit(1)
    ).first()
    if existing:
        existing.label = body.label
        existing.updated_at = utcnow()
        # Keep an existing cached cover if this source has disappeared since
        # it was first played (for example, a prior genre-mix refresh).
        if art_media_item_id is not None:
            existing.art_media_item_id = art_media_item_id
        existing.preview_media_item_ids_json = preview_media_item_ids
    else:
        db.add(
            PlaySourceHistory(
                user_id=user.id,
                kind=body.kind,
                label=body.label,
                payload_json=payload,
                art_media_item_id=art_media_item_id,
                preview_media_item_ids_json=preview_media_item_ids,
                updated_at=utcnow(),
            )
        )

    stale = db.scalars(
        select(PlaySourceHistory)
        .where(PlaySourceHistory.user_id == user.id)
        .order_by(PlaySourceHistory.updated_at.desc())
        .offset(MAX_PLAY_SOURCE_HISTORY)
    ).all()
    if stale:
        for row in stale:
            db.delete(row)
    # Persist the source snapshot and bounded-history cleanup together so the
    # cached fields cannot get out of step with the recorded source.
    db.commit()


def _resolve_play_source_art(
    db: Session, user: User, kind: str, payload: dict
) -> int | None:
    """A representative media_item_id for a play-source card's cover art -
    same "first track" convention /api/library/albums and /artists already
    use. Radio stations and the whole-library shuffle have no single cover
    to show, so those kinds fall through to the caller's emoji fallback."""
    accessible = accessible_root_ids(db, user)
    if kind == "album" and payload.get("album"):
        item = db.scalars(
            select(MediaItem)
            .where(
                MediaItem.library_root_id.in_(accessible),
                MediaItem.kind == MediaKind.track,
                MediaItem.album == payload.get("album"),
                MediaItem.artist == payload.get("artist"),
            )
            .order_by(MediaItem.track_number)
            .limit(1)
        ).first()
        return item.id if item else None
    if kind == "artist" and payload.get("artist"):
        item = db.scalars(
            select(MediaItem)
            .where(
                MediaItem.library_root_id.in_(accessible),
                MediaItem.kind == MediaKind.track,
                MediaItem.artist == payload.get("artist"),
            )
            .order_by(MediaItem.album, MediaItem.track_number)
            .limit(1)
        ).first()
        return item.id if item else None
    if kind == "playlist" and payload.get("playlist_id"):
        pl_item = db.scalars(
            select(PlaylistItem)
            .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
            .where(
                PlaylistItem.playlist_id == payload.get("playlist_id"),
                Playlist.user_id == user.id,
            )
            .order_by(PlaylistItem.position)
            .limit(1)
        ).first()
        return pl_item.media_item_id if pl_item else None
    return None


def _library_source_preview_ids(
    db: Session, user: User, payload: dict, limit: int = 4
) -> list[int]:
    """Up to `limit` recently-played tracks that match this library-context
    play source's filters, distinct by artist where possible - a "library"
    source (e.g. shuffling the whole Songs tab, or a search) has no single
    cover of its own, so this gives its card the same real-art-collage
    treatment a playlist's cover gets (see app.collage) instead of a
    generic icon."""
    raw_filters = payload.get("filters")
    filters = raw_filters if isinstance(raw_filters, dict) else {}
    matching = (
        filter_tracks_query(db, user, filters).with_only_columns(MediaItem.id).subquery()
    )
    # unique(): the join gives a row per play; each track once.
    history = db.scalars(
        select(MediaItem)
        .join(PlayHistory, PlayHistory.media_item_id == MediaItem.id)
        .join(matching, matching.c.id == MediaItem.id)
        .where(PlayHistory.user_id == user.id)
        .order_by(PlayHistory.played_at.desc(), PlayHistory.id.asc())
        # Bound the work even when history is very large. The generous
        # window preserves artist diversity for a four-tile preview.
        .limit(200)
    ).unique().all()
    seen_artists: set[str] = set()
    seen_ids: set[int] = set()
    ids: list[int] = []
    for item in history:
        if item.id in seen_ids:
            continue
        key = item.artist or f"id:{item.id}"
        if key in seen_artists:
            continue
        seen_ids.add(item.id)
        seen_artists.add(key)
        ids.append(item.id)
        if len(ids) == limit:
            break
    return ids


def _mix_exists(db: Session, playlist_id: object, existing: set[int]) -> bool:
    if isinstance(playlist_id, int) and not isinstance(playlist_id, bool):
        return playlist_id in existing
    # Not a plain id (an old or odd payload): look it up in the database.
    return db.get(Playlist, playlist_id) is not None


@router.get("/recently-played-sources", response_model=list[PlaySourceOut])
def recently_played_sources(
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=12, ge=1, le=30),
) -> list[PlaySourceOut]:
    rows = db.scalars(
        select(PlaySourceHistory)
        .where(PlaySourceHistory.user_id == user.id)
        .order_by(PlaySourceHistory.updated_at.desc())
        .limit(limit)
    ).all()
    # The mixes these cards point at, in one query rather than one per card.
    mix_ids = {
        row.payload_json.get("playlist_id")
        for row in rows
        if row.kind == "playlist"
        and isinstance(row.payload_json, dict)
        and row.payload_json.get("auto_kind") == "mix"
    }
    mix_ids = {pid for pid in mix_ids if isinstance(pid, int) and not isinstance(pid, bool)}
    existing_mixes = set(
        db.scalars(select(Playlist.id).where(Playlist.id.in_(mix_ids)))
    ) if mix_ids else set()
    result = []
    invalid_rows = []
    for row in rows:
        raw_payload = row.payload_json
        # Generated mixes are disposable slots. Once their playlist identity
        # disappears, retaining the history card only creates a dead link.
        # Mix play-source records carry the marker; ordinary playlist
        # history is left untouched.
        if (
            row.kind == "playlist"
            and isinstance(raw_payload, dict)
            and raw_payload.get("auto_kind") == "mix"
            and not _mix_exists(db, raw_payload.get("playlist_id"), existing_mixes)
        ):
            db.delete(row)
            invalid_rows.append(row)
            continue
        try:
            validated = PlaySourceIn(
                # None/garbage payloads are rejected by validation, which is caught below.
                kind=row.kind, label=row.label, payload=cast(Any, raw_payload)
            )
            payload = validated.payload
        except (ValueError, TypeError, json.JSONDecodeError):
            invalid_rows.append(row)
            continue
        preview_media_item_ids: list[int] = []
        decoded = row.preview_media_item_ids_json
        if row.kind == "library" and isinstance(decoded, list) and all(type(item) is int for item in decoded):
            preview_media_item_ids = decoded
        result.append(
            PlaySourceOut(
                kind=row.kind,
                label=row.label,
                payload=payload,
                updated_at=row.updated_at,
                art_media_item_id=row.art_media_item_id,
                preview_media_item_ids=preview_media_item_ids,
            )
        )
    if invalid_rows:
        for row in invalid_rows:
            db.delete(row)
        db.commit()
    return result
