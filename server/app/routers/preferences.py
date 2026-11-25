from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from app.access import accessible_root_ids
from app.deps import CurrentUser, DbSession
from app.models import User
from app.preferences import (
    album_owner,
    artist_exists,
    get_collection_state,
    set_collection_state,
)
from app.schemas import CollectionPreferenceOut, PreferenceIn

router = APIRouter(prefix="/api/preferences", tags=["preferences"])


def _album_key(db: Session, user: User, artist: str | None, album: str) -> str:
    """The stored album key's artist for the album page showing (artist,
    album) - its owner, see app.preferences.album_owner."""
    owner = album_owner(db, user, artist, album)
    if owner is None:
        raise HTTPException(404, "Album not found")
    return owner


def _album_out(db: Session, user: User, owner: str, album: str) -> CollectionPreferenceOut:
    return CollectionPreferenceOut(
        kind="album",
        artist=owner,
        album=album,
        state=get_collection_state(db, user, "album", owner, album),
    )


@router.get("/album", response_model=CollectionPreferenceOut)
def get_album_preference(
    album: Annotated[str, Query(max_length=1000)],
    db: DbSession,
    user: CurrentUser,
    artist: Annotated[str | None, Query(max_length=1000)] = None,
) -> CollectionPreferenceOut:
    return _album_out(db, user, _album_key(db, user, artist, album), album)


@router.put("/album", response_model=CollectionPreferenceOut)
def set_album_preference(
    body: PreferenceIn,
    album: Annotated[str, Query(max_length=1000)],
    db: DbSession,
    user: CurrentUser,
    artist: Annotated[str | None, Query(max_length=1000)] = None,
) -> CollectionPreferenceOut:
    owner = album_owner(db, user, artist, album)
    if owner is None:
        # Clearing must still work from Settings after the album has left
        # the library - the entry then carries its own stored owner.
        if body.state != "none" or artist is None:
            raise HTTPException(404, "Album not found")
        owner = artist
    set_collection_state(db, user, "album", owner, album, body.state)
    return _album_out(db, user, owner, album)


@router.get("/artist", response_model=CollectionPreferenceOut)
def get_artist_preference(
    artist: Annotated[str, Query(max_length=1000)],
    db: DbSession,
    user: CurrentUser,
) -> CollectionPreferenceOut:
    if not artist_exists(db, user, artist, accessible_root_ids(db, user)):
        raise HTTPException(404, "Artist not found")
    return CollectionPreferenceOut(
        kind="artist", artist=artist, state=get_collection_state(db, user, "artist", artist)
    )


@router.put("/artist", response_model=CollectionPreferenceOut)
def set_artist_preference(
    body: PreferenceIn,
    artist: Annotated[str, Query(max_length=1000)],
    db: DbSession,
    user: CurrentUser,
) -> CollectionPreferenceOut:
    if body.state != "none" and not artist_exists(
        db, user, artist, accessible_root_ids(db, user)
    ):
        raise HTTPException(404, "Artist not found")
    set_collection_state(db, user, "artist", artist, None, body.state)
    return CollectionPreferenceOut(kind="artist", artist=artist, state=body.state)
