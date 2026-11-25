"""Artist/album/song info and ListenBrainz top tracks. See
app.metadata_info and app.listenbrainz_data."""

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.access import can_access_root
from app.activity import log_action
from app.deferred import answer_with_pending, in_session
from app.deps import AdminUser, CurrentUser, DbSession
from app.listenbrainz_data import artist_top_tracks
from app.metadata_info import (
    album_info,
    artist_candidates,
    artist_info,
    choose_edition,
    find_album,
    find_artist,
    fix_artist_match,
    track_info,
)
from app.models import MediaItem, User
from app.presenters import to_media_item_outs
from app.schemas import (
    AlbumEditionIn,
    AlbumInfoOut,
    ArtistCandidateOut,
    ArtistInfoOut,
    ArtistMatchIn,
    ArtistTopTracksOut,
    TrackInfoOut,
)

router = APIRouter(prefix="/api/library", tags=["metadata"])


def _as_user(db, user_id: int) -> User:
    found = db.get(User, user_id)
    assert found is not None  # a job is queued by the user's own request
    return found


# Answers built only from stored provider data come back with `pending`
# true while the missing look-ups run in the background (app.deferred): the
# client asks again (the same URL) every couple of seconds until it is
# false. `refresh=1` fetches before answering.


@router.get("/artist-info", response_model=ArtistInfoOut)
def get_artist_info(
    db: DbSession,
    user: CurrentUser,
    name: str = Query(min_length=1, max_length=500),
    refresh: bool = False,
) -> dict:
    if refresh:
        return artist_info(db, user, name, refresh=True)
    user_id = user.id
    return answer_with_pending(
        f"artist-info:{user_id}:{name}",
        lambda: artist_info(db, user, name),
        in_session(lambda job_db: artist_info(job_db, _as_user(job_db, user_id), name)),
        user_id=user_id,
    )


@router.get("/artist-top-tracks", response_model=ArtistTopTracksOut)
def get_artist_top_tracks(
    db: DbSession,
    user: CurrentUser,
    name: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=20, ge=1, le=50),
    refresh: bool = False,
) -> dict:
    """ListenBrainz's most listened songs by the artist; owned ones carry
    the library track (`media_item`), missing ones don't. `pending`:
    ListenBrainz is still being asked (see above)."""
    if refresh:
        result = {**artist_top_tracks(db, user, name, limit=limit, refresh=True), "pending": False}
    else:
        user_id = user.id
        result = answer_with_pending(
            f"artist-top-tracks:{user_id}:{name}",
            lambda: artist_top_tracks(db, user, name, limit=limit),
            in_session(lambda job_db: artist_top_tracks(job_db, _as_user(job_db, user_id), name, limit=50)),
            user_id=user_id,
        )
    ids = [row["media_item_id"] for row in result["tracks"] if row["media_item_id"]]
    items = {i.id: i for i in db.scalars(select(MediaItem).where(MediaItem.id.in_(ids)))} if ids else {}
    outs = {out.id: out for out in to_media_item_outs(list(items.values()), db, user)}
    return {
        "available": result["available"],
        "pending": result["pending"],
        "tracks": [
            {
                **{k: v for k, v in row.items() if k != "media_item_id"},
                "media_item": outs.get(row["media_item_id"]),
            }
            for row in result["tracks"]
        ],
    }


@router.get("/artist-match-candidates", response_model=list[ArtistCandidateOut])
def get_artist_match_candidates(
    db: DbSession,
    user: CurrentUser,
    name: str = Query(min_length=1, max_length=500),
) -> list[dict]:
    return artist_candidates(db, name)


@router.put("/artist-match", response_model=ArtistInfoOut)
def put_artist_match(
    body: ArtistMatchIn,
    db: DbSession,
    user: AdminUser,
) -> dict:
    """Fix which MusicBrainz artist a library artist is (shared data, so
    admins only)."""
    artist = find_artist(db, body.name)
    if artist is None:
        raise HTTPException(404, "Artist not found")
    fix_artist_match(db, artist, body.mbid)
    log_action("artist.match", artist=artist.id, mbid=body.mbid)
    return artist_info(db, user, artist.name, refresh=True)


@router.get("/album-info", response_model=AlbumInfoOut)
def get_album_info(
    db: DbSession,
    user: CurrentUser,
    album: str = Query(min_length=1, max_length=500),
    artist: str | None = Query(default=None, max_length=500),
    refresh: bool = False,
) -> dict:
    if refresh:
        return album_info(db, user, artist, album, refresh=True)
    user_id = user.id
    return answer_with_pending(
        f"album-info:{user_id}:{artist}\x1f{album}",
        lambda: album_info(db, user, artist, album),
        in_session(lambda job_db: album_info(job_db, _as_user(job_db, user_id), artist, album)),
        user_id=user_id,
    )


@router.put("/album-edition", response_model=AlbumInfoOut)
def put_album_edition(
    body: AlbumEditionIn,
    db: DbSession,
    user: AdminUser,
) -> dict:
    """Choose which edition of the release group an album is (decides its
    edition date, label, and the tracklist missing songs are judged by)."""
    album = find_album(db, body.artist, body.album)
    if album is None:
        raise HTTPException(404, "Album not found")
    choose_edition(db, album, body.release_mbid)
    return album_info(db, user, body.artist, body.album)


@router.get("/media/{media_item_id}/info", response_model=TrackInfoOut)
def get_track_info(
    media_item_id: int,
    db: DbSession,
    user: CurrentUser,
    refresh: bool = False,
    light: bool = Query(default=False, description="Ignored; kept for older apps."),  # noqa: ARG001
) -> dict:
    item = db.get(MediaItem, media_item_id)
    if item is None or not can_access_root(db, user, item.library_root_id):
        raise HTTPException(404, "Media item not found")
    if refresh:
        return track_info(db, user, item, refresh=True)
    user_id = user.id

    def fetch(job_db) -> None:
        job_item = job_db.get(MediaItem, media_item_id)
        if job_item is not None:
            track_info(job_db, _as_user(job_db, user_id), job_item)

    return answer_with_pending(
        f"track-info:{user_id}:{media_item_id}",
        lambda: track_info(db, user, item),
        in_session(fetch),
        user_id=user_id,
    )
