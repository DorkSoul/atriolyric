"""The Want list tab: wanted artists/albums/songs, the external search, and
catalog pages for things that may not be in the library. See
app.want_list and app.catalog."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.config import settings
from app.custom_art import content_type
from app.deferred import answer_from_store, in_session
from app.deps import CurrentUser, DbSession
from app.metadata_info import find_artist, resolve_artist_mbid
from app.models import User, WantListItem
from app.presenters import ART_CACHE_HEADERS, no_art_response
from app.schemas import (
    MBID_PATTERN,
    CatalogAlbumOut,
    CatalogArtistOut,
    ImageCreditOut,
    OkOut,
    RefreshingOut,
    RemovedCountOut,
    WantedArtistDetailOut,
    WantedArtistOut,
    WantEntryOut,
    WantIn,
    WantSearchOut,
)
from app.want_list import (
    add_entry,
    artist_detail,
    artist_summaries,
    catalog_artist,
    catalog_artist_placeholder,
    catalog_release_group,
    catalog_release_group_placeholder,
    clear_owned,
    entry_out,
    export,
    rematch_in_background,
    rematch_running,
    rematch_visible_in_background,
    search,
)
from app.want_metadata import artist_refreshing, queue_catalog_art, stored_catalog_art

router = APIRouter(prefix="/api/want-list", tags=["want-list"])
# A catalog cover still being fetched: let the browser ask again soon.
CATALOG_ART_PENDING_HEADERS = {"Cache-Control": "private, max-age=30", "Vary": "Cookie"}
catalog_router = APIRouter(prefix="/api/catalog", tags=["want-list"])


# Want-list reads answer with the entries as last matched to the library;
# after a library or access change they queue a background re-match. The
# list routes then send this header (the artist page sets `matching`), and
# /match-status says when the re-match is done, so the page can reload.
MATCHING_HEADER = "X-Want-List-Matching"


def _job_user(db, user_id: int) -> User:
    found = db.get(User, user_id)
    assert found is not None  # a job is queued by the user's own request
    return found


@router.get("/artists", response_model=list[WantedArtistOut])
def list_wanted_artists(
    db: DbSession,
    user: CurrentUser,
    response: Response,
    all_users: bool = False,
) -> list[dict]:
    if rematch_visible_in_background(db, user, all_users=all_users):
        response.headers[MATCHING_HEADER] = "1"
    return artist_summaries(db, user, all_users=all_users)


@router.get("/match-status", response_model=RefreshingOut)
def get_match_status(db: DbSession, user: CurrentUser, all_users: bool = False) -> dict:
    """Whether a background re-match of the caller's want list (every
    user's, for an admin's all-users view) is queued or running. Poll it
    while a want-list page says `matching`; reload the page once false."""
    return {"refreshing": rematch_running(db, user, all_users=all_users)}


@router.get("/artist", response_model=WantedArtistDetailOut, response_model_exclude_unset=True)
def get_wanted_artist(
    db: DbSession,
    user: CurrentUser,
    name: str = Query(min_length=1, max_length=1000),
    mbid: str | None = Query(default=None, pattern=MBID_PATTERN),
    all_users: bool = False,
) -> dict:
    """`pending` (with `refreshing`): the artist's catalog has never been
    fetched and is being fetched now - poll /api/catalog/artist/refresh-status
    as for `refreshing`, then reload."""
    matching = rematch_visible_in_background(db, user, all_users=all_users)
    user_id = user.id
    detail, pending = answer_from_store(
        lambda out: f"refresh-artist:{out['artist_mbid']}",
        lambda: artist_detail(db, user, name, mbid, all_users=all_users),
        in_session(lambda job_db: artist_detail(job_db, _job_user(job_db, user_id), name, mbid, all_users=all_users)),
        user_id=user_id,
    )
    return {
        **detail,
        "refreshing": detail["refreshing"] or pending,
        "pending": pending,
        "matching": matching,
    }


@router.get("/entries", response_model=list[WantEntryOut], response_model_exclude_unset=True)
def list_entries(
    db: DbSession, user: CurrentUser, response: Response
) -> list[dict]:
    # The entries as last matched to the library. After a library or access
    # change this queues a background re-match; the next read shows it.
    rows = list(
        db.scalars(
            select(WantListItem)
            .where(WantListItem.user_id == user.id)
            .order_by(WantListItem.created_at.desc())
        )
    )
    if rematch_in_background(db, user, rows):
        response.headers[MATCHING_HEADER] = "1"
    return [entry_out(row) for row in rows]


@router.post("", response_model=WantEntryOut, response_model_exclude_unset=True)
def add_want(
    body: WantIn, db: DbSession, user: CurrentUser
) -> dict:
    if body.kind != "artist" and not (body.title or "").strip():
        raise HTTPException(422, "A title is required for an album or song")
    return entry_out(add_entry(db, user, body.model_dump()))


@router.delete("/{entry_id}", response_model=OkOut)
def remove_want(
    entry_id: int, db: DbSession, user: CurrentUser
) -> dict:
    entry = db.get(WantListItem, entry_id)
    if entry is None or entry.user_id != user.id:
        raise HTTPException(404, "Want-list entry not found")
    db.delete(entry)
    db.commit()
    return {"ok": True}


@router.post("/clear-owned", response_model=RemovedCountOut)
def clear_owned_wants(
    db: DbSession, user: CurrentUser
) -> dict:
    return {"removed": clear_owned(db, user)}


@router.get("/export")
def export_wants(
    db: DbSession,
    user: CurrentUser,
    format: Literal["csv", "json"] = "csv",
    all_users: bool = False,
) -> Response:
    body, media_type = export(db, user, format, all_users=all_users)
    return Response(
        body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="want-list.{format}"'},
    )


@router.get("/search", response_model=WantSearchOut)
def search_external(
    db: DbSession,
    user: CurrentUser,
    q: str = Query(min_length=1, max_length=200),
) -> dict:
    return search(db, user, q)


@catalog_router.get("/artist", response_model=CatalogArtistOut)
def get_catalog_artist(
    db: DbSession,
    user: CurrentUser,
    mbid: str | None = Query(default=None, pattern=MBID_PATTERN),
    name: str | None = Query(default=None, max_length=1000),
    refresh: bool = False,
) -> dict:
    """An artist's whole discography with owned/wanted state. A library
    artist can be asked for by name; its MBID is resolved first."""
    if not mbid and name:
        artist = find_artist(db, name)
        mbid = resolve_artist_mbid(db, artist) if artist else None
        if not mbid:
            raise HTTPException(404, "This artist couldn't be matched on MusicBrainz")
    if not mbid:
        raise HTTPException(422, "An artist MBID or name is required")
    if refresh:
        catalog = catalog_artist(db, user, mbid, name, refresh=True)
        pending = False
    else:
        # Never fetched: answer at once with `refreshing`/`pending` while the
        # first look-up runs; the page polls /artist/refresh-status (the
        # job's key is the same) and reloads.
        artist_mbid = mbid
        user_id = user.id
        catalog, pending = answer_from_store(
            f"refresh-artist:{artist_mbid}",
            lambda: catalog_artist(db, user, artist_mbid, name),
            in_session(lambda job_db: catalog_artist(job_db, _job_user(job_db, user_id), artist_mbid, name)),
            user_id=user_id,
        )
        if catalog is None and pending:
            catalog = catalog_artist_placeholder(db, user, artist_mbid, name)
    if catalog is None:
        raise HTTPException(502, "MusicBrainz couldn't be reached - try again shortly")
    return {**catalog, "refreshing": catalog["refreshing"] or pending, "pending": pending}


@catalog_router.get("/artist/refresh-status", response_model=RefreshingOut)
def get_artist_refresh_status(
    user: CurrentUser, mbid: str = Query(pattern=MBID_PATTERN)
) -> dict:
    """Whether a background re-check of this artist's data is still running
    (the page polls this while it shows saved data)."""
    return {"refreshing": artist_refreshing(mbid)}


@catalog_router.get("/release-group/{mbid}", response_model=CatalogAlbumOut)
def get_catalog_release_group(
    db: DbSession,
    user: CurrentUser,
    mbid: str = Path(pattern=MBID_PATTERN),
    artist: str | None = Query(default=None, max_length=1000),
    refresh: bool = False,
) -> dict:
    """`pending`: part (or all, then with no title or tracks) of the album
    has never been fetched and is being fetched now; ask again shortly."""
    if refresh:
        group = catalog_release_group(db, user, mbid, artist, refresh=True)
        pending = False
    else:
        user_id = user.id
        group, pending = answer_from_store(
            f"catalog-album:{mbid}",
            lambda: catalog_release_group(db, user, mbid, artist),
            in_session(lambda job_db: catalog_release_group(job_db, _job_user(job_db, user_id), mbid, artist)),
            user_id=user_id,
        )
        if group is None and pending:
            group = catalog_release_group_placeholder(db, user, mbid, artist)
    if group is None:
        raise HTTPException(502, "MusicBrainz couldn't be reached - try again shortly")
    return {**group, "pending": pending}


@catalog_router.get("/art/{kind}/{mbid}/credit", response_model=ImageCreditOut | None)
def get_catalog_art_credit(
    kind: Literal["artist", "album"],
    db: DbSession,
    user: CurrentUser,  # noqa: ARG001 - signed-in users only
    mbid: str = Path(pattern=MBID_PATTERN),
) -> dict | None:
    """The credit stored with a catalog artist's or album's art (a
    Wikimedia Commons photo's author and licence); null when none."""
    row, _missing = stored_catalog_art(db, kind, mbid)
    return row.credit_json if row is not None and row.credit_json else None


@catalog_router.get("/art/{kind}/{mbid}")
def get_catalog_art(
    kind: Literal["artist", "album"],
    db: DbSession,
    user: CurrentUser,
    mbid: str = Path(pattern=MBID_PATTERN),
    name: str | None = Query(default=None, max_length=1000),
    album: str | None = Query(default=None, max_length=1000),
    thumb: bool = True,
) -> Response:
    """Cover / artist picture for a catalog entry: the stored image, or 204.
    A first request queues the download on the background provider queue and
    answers 204 at once with a short cache life, so the tile can ask again
    shortly, so a page of uncached covers does not tie up request threads
    (the ones that also stream audio) on provider calls. 204 with the normal
    no-art cache life when no provider has one. The image is stored for
    everyone, so `name`/`album` are accepted for compatibility but ignored:
    MusicBrainz's names for the MBID decide what is searched."""
    mbid = mbid.lower()
    row, known_missing = stored_catalog_art(db, kind, mbid)
    if row is None:
        if known_missing:
            return no_art_response()
        # Refused when the queue is full or this user has too many covers
        # waiting: the same 204, and the tile's next ask queues it.
        queue_catalog_art(kind, mbid, user_id=user.id)
        return Response(status_code=204, headers=CATALOG_ART_PENDING_HEADERS)
    path = settings.custom_art_dir / (row.thumb_path if thumb else row.path)
    if not path.is_file():
        return no_art_response()
    return FileResponse(path, media_type=content_type(path), headers=ART_CACHE_HEADERS)
