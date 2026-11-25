"""Discovery playlist, explore and Song Mix generation routes."""

from fastapi import APIRouter, HTTPException

from app.deps import CurrentUser, DbSession
from app.discovery import (
    InsufficientMatchesError,
    explore_new_artists,
    generate_playlist_from_collection,
    generate_playlist_from_track,
    get_discovery_settings,
    get_discovery_state,
    refresh_discovery_playlist,
    update_discovery_settings,
)
from app.schemas import (
    DiscoveryExploreOut,
    DiscoveryRefreshOut,
    DiscoverySettingsIn,
    DiscoverySettingsOut,
    DiscoveryStateOut,
    GenerateCollectionPlaylistIn,
    GeneratePlaylistIn,
    GeneratePlaylistOut,
)

router = APIRouter()


@router.post("/discovery/refresh", response_model=DiscoveryRefreshOut, response_model_exclude_unset=True)
def refresh_discovery(
    db: DbSession, user: CurrentUser
) -> dict:
    return refresh_discovery_playlist(db, user)


@router.post("/discovery/explore", response_model=DiscoveryExploreOut)
def explore_discovery(
    db: DbSession, user: CurrentUser
) -> DiscoveryExploreOut:
    return DiscoveryExploreOut(**explore_new_artists(db, user))


@router.post("/discovery/generate", response_model=GeneratePlaylistOut)
def generate_playlist(
    body: GeneratePlaylistIn,
    db: DbSession,
    user: CurrentUser,
) -> GeneratePlaylistOut:
    try:
        result = generate_playlist_from_track(db, user, body.media_item_id)
    except InsufficientMatchesError:
        raise HTTPException(
            422,
            "Not enough similar tracks in your library to generate a mix from this song.",
        ) from None
    if result is None:
        raise HTTPException(404, "Media item not found")
    return GeneratePlaylistOut(**result)


@router.post("/discovery/generate-collection", response_model=GeneratePlaylistOut)
def generate_collection_playlist(
    body: GenerateCollectionPlaylistIn,
    db: DbSession,
    user: CurrentUser,
) -> GeneratePlaylistOut:
    if body.artist is None and body.album is None:
        raise HTTPException(422, "An artist or album is required")
    try:
        result = generate_playlist_from_collection(
            db, user, body.label, body.artist, body.album
        )
    except InsufficientMatchesError:
        raise HTTPException(
            422,
            "Not enough similar tracks in your library to generate a mix from this.",
        ) from None
    if result is None:
        raise HTTPException(404, "Nothing found for that artist or album")
    return GeneratePlaylistOut(**result)


@router.get("/discovery/state", response_model=DiscoveryStateOut)
def get_discovery_state_route(
    db: DbSession, user: CurrentUser
) -> DiscoveryStateOut:
    """The persisted results of the last refresh/explore run (manual or
    scheduled) - lets the Home screen show what it found last time without
    re-running the discovery job on every page load/tab switch."""
    return DiscoveryStateOut(**get_discovery_state(db, user))


@router.get("/discovery/settings", response_model=DiscoverySettingsOut)
def get_discovery_settings_route(
    db: DbSession, user: CurrentUser
) -> DiscoverySettingsOut:
    return DiscoverySettingsOut(**get_discovery_settings(db, user))


@router.put("/discovery/settings", response_model=DiscoverySettingsOut)
def update_discovery_settings_route(
    body: DiscoverySettingsIn,
    db: DbSession,
    user: CurrentUser,
) -> DiscoverySettingsOut:
    return DiscoverySettingsOut(
        **update_discovery_settings(
            db, user, body.auto_refresh_enabled, body.auto_refresh_hour
        )
    )
