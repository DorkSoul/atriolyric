"""Genres, labels and the automatic and specified mixes."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, union
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.custom_art import annotate_playlists
from app.deps import CurrentUser, DbSession
from app.discovery import (
    build_specified_mix,
    list_genre_mix_playlists,
    refresh_all_genre_mixes,
    selector_display_name,
)
from app.label_counts import label_counts
from app.library_query import (
    filter_tracks_query,
    genre_rows,
    label_track_rows,
    tag_rows,
)
from app.models import (
    MediaItem,
    Playlist,
)
from app.schemas import (
    AllLabelsOut,
    LabelOut,
    LabelsOut,
    MixOptionOut,
    MixPlaylistOut,
    SpecifiedMixIn,
)

router = APIRouter()


@router.get("/genres", response_model=list[str])
def list_genres(
    db: DbSession, user: CurrentUser
) -> list[str]:
    # Names only; see label_counts for what counts as a track's genre.
    return sorted(name for _kind, name in label_counts(db, user, "genre"))


def _mix_playlist_out(p: Playlist) -> MixPlaylistOut:
    selector = p.mix_genre or ""
    display_name = selector_display_name(selector) if selector else ""
    # Same graceful decode as recently-played sources: a malformed or
    # missing snapshot just means an empty collage, never a failed request.
    preview_ids: list[int] = []
    decoded = p.preview_media_item_ids_json
    if isinstance(decoded, list) and all(type(item) is int for item in decoded):
        preview_ids = decoded
    return MixPlaylistOut(
        playlist_id=p.id,
        genre=display_name,
        name=p.name,
        track_count=len(p.items),
        preview_media_item_ids=preview_ids,
        is_specified=selector.startswith("specified:"),
    )


def _mix_playlist_outs(db: Session, playlists: list[Playlist]) -> list[MixPlaylistOut]:
    outs = [_mix_playlist_out(p) for p in playlists]
    annotate_playlists(db, outs, "playlist_id")
    return outs


@router.get("/mixes", response_model=list[MixPlaylistOut])
def list_mixes(
    db: DbSession, user: CurrentUser
) -> list[MixPlaylistOut]:
    """The user's current featured genre-mix playlists - real auto-managed
    playlists (see refresh_all_genre_mixes), not computed on the fly, so
    opening one is instant like any other playlist."""
    return _mix_playlist_outs(db, list_genre_mix_playlists(db, user))


@router.get("/labels", response_model=LabelsOut)
def get_labels(
    db: DbSession,
    user: CurrentUser,
    media_item_id: int | None = None,
    artist: Annotated[str | None, Query(max_length=1000)] = None,
    artist_is_null: bool = False,
    album: Annotated[str | None, Query(max_length=1000)] = None,
) -> LabelsOut:
    """Every genre and tag on one song, or (with artist/album) pooled across
    that artist's or album's tracks, each with how many of those tracks
    carry it - the "Genres/tags" view in Now Playing and album/artist pages."""
    if media_item_id is None and not artist and not artist_is_null and not album:
        raise HTTPException(422, "Give media_item_id, or artist and/or album")
    query = filter_tracks_query(
        db, user, {"artist": artist, "artist_is_null": artist_is_null, "album": album}
    )
    if media_item_id is not None:
        query = query.where(MediaItem.id == media_item_id)
    track_ids = query.with_only_columns(MediaItem.id).subquery()

    def pooled(rows) -> list[LabelOut]:
        # Case-insensitive so a legacy "rock" and a provider "Rock" merge.
        names: dict[str, str] = {}
        counts: dict[str, set[int]] = {}
        for name, item_id in rows:
            key = name.casefold()
            names.setdefault(key, name)
            counts.setdefault(key, set()).add(item_id)
        ranked = sorted(counts, key=lambda k: (-len(counts[k]), k))
        return [LabelOut(name=names[k], track_count=len(counts[k])) for k in ranked[:100]]

    in_tracks = MediaItem.id.in_(select(track_ids.c.id))

    def label_rows(selects) -> list:
        rows = union(*selects).subquery()
        return list(db.execute(select(rows.c.name, rows.c.media_item_id)))

    return LabelsOut(
        genres=pooled(label_rows(genre_rows(in_tracks))),
        tags=pooled(label_rows(tag_rows(in_tracks))),
    )


@router.get("/labels/all", response_model=AllLabelsOut)
def get_all_labels(db: DbSession, user: CurrentUser) -> AllLabelsOut:
    """Every genre and tag in the library, each with the ids of the tracks
    that carry it: what GET /labels and the genre and tag filters of
    GET /tracks answer from, in one piece. The Android app fetches it in the
    background so those work with no connection (web/src/native/
    offlineLibrary.ts). Names that differ only in case are one entry, as in
    GET /labels."""
    names: dict[tuple[str, str], str] = {}
    ids: dict[tuple[str, str], set[int]] = {}
    for kind, name, item_id in db.execute(label_track_rows(accessible_root_ids(db, user))):
        key = (kind, name.casefold())
        names.setdefault(key, name)
        ids.setdefault(key, set()).add(item_id)

    def of(kind: str) -> dict[str, list[int]]:
        return {names[key]: sorted(found) for key, found in sorted(ids.items()) if key[0] == kind}

    return AllLabelsOut(genres=of("genre"), tags=of("tag"))


@router.get("/mixes/options", response_model=list[MixOptionOut])
def list_mix_options(
    db: DbSession, user: CurrentUser
) -> list[MixOptionOut]:
    """Return selectable genres and custom library tags for a specified mix,
    each with how many of its tracks are not disliked (a mix leaves those
    out)."""
    options = [
        MixOptionOut(kind=kind, name=name, track_count=count)
        for kind in ("genre", "tag")
        for (_kind, name), count in label_counts(db, user, kind, mixable=True).items()
    ]
    return sorted(options, key=lambda option: (option.kind != "genre", option.name.casefold()))


@router.post("/mixes/refresh", response_model=list[MixPlaylistOut])
def refresh_mixes(
    db: DbSession, user: CurrentUser
) -> list[MixPlaylistOut]:
    """Regenerates the featured genre and tag mixes on demand - the same job that
    otherwise runs once a day alongside Discovery's auto-refresh."""
    return _mix_playlist_outs(db, refresh_all_genre_mixes(db, user))


@router.post("/mixes/specified", response_model=list[MixPlaylistOut])
def add_specified_mix(
    body: SpecifiedMixIn,
    db: DbSession, user: CurrentUser
) -> list[MixPlaylistOut]:
    """Append one temporary mix to the current featured mixes. Tracks match
    at least one `include` {kind, name} (those matching all of them are used
    first) and none of the `exclude`s; the legacy single {kind, name} body is
    one include. The new mix's entry says how many of its tracks match every
    include (full_match_count)."""
    include = [(o.kind, o.name) for o in body.include if o.name.strip()]
    if body.name and body.name.strip():
        include.append((body.kind or "", body.name))
    if not include:
        raise HTTPException(400, "Choose at least one genre or tag to include")
    try:
        built = build_specified_mix(
            db, user, include=include, exclude=[(o.kind, o.name) for o in body.exclude]
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    outs = _mix_playlist_outs(db, list_genre_mix_playlists(db, user))
    for out in outs:
        if out.playlist_id == built.playlist.id:
            out.full_match_count = built.full_match_count
    return outs
