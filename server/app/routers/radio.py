from contextlib import ExitStack

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.deps import CurrentUser, DbSession
from app.models import RadioStation, RadioStationFavorite, User, WantListItem
from app.radio import (
    RadioBusy,
    RadioUnavailable,
    get_now_playing,
    iterate_then_cleanup,
    open_stream,
    relay_icy,
    stream_slot,
    validate_station_url,
)
from app.schemas import (
    IsFavoriteOut,
    NowPlayingOut,
    OkOut,
    StationIn,
    StationOut,
    WantListIn,
    WantListOut,
)
from app.want_list import add_entry

router = APIRouter(prefix="/api/radio", tags=["radio"])


@router.get("/stations", response_model=list[StationOut])
def list_stations(
    db: DbSession, user: CurrentUser
) -> list[StationOut]:
    stations = db.scalars(
        select(RadioStation).where(
            (RadioStation.is_shared.is_(True)) | (RadioStation.owner_user_id == user.id)
        )
    ).all()
    # One query for the user's favourites, not one per station.
    favorites = set(
        db.scalars(
            select(RadioStationFavorite.station_id).where(RadioStationFavorite.user_id == user.id)
        )
    )
    outs = []
    for station in stations:
        out = StationOut.model_validate(station)
        out.is_favorite = station.id in favorites
        outs.append(out)
    return outs


@router.post("/stations", response_model=StationOut)
def create_station(
    body: StationIn,
    db: DbSession,
    user: CurrentUser,
) -> RadioStation:
    if body.is_shared and not user.is_admin:
        raise HTTPException(403, "Only admins can add stations visible to everyone")
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Station name cannot be blank")
    try:
        validate_station_url(body.stream_url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    station = RadioStation(
        name=name,
        stream_url=body.stream_url,
        description=body.description.strip() if body.description else None,
        is_shared=body.is_shared,
        owner_user_id=None if body.is_shared else user.id,
    )
    db.add(station)
    db.commit()
    db.refresh(station)
    return station


@router.delete("/stations/{station_id}", response_model=OkOut)
def delete_station(
    station_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    station = db.get(RadioStation, station_id)
    if station is None:
        raise HTTPException(404, "Station not found")
    if station.owner_user_id != user.id and not user.is_admin:
        raise HTTPException(403, "Not your station")
    db.execute(delete(RadioStationFavorite).where(RadioStationFavorite.station_id == station_id))
    db.delete(station)
    db.commit()
    return {"ok": True}


def _get_visible_station(db: Session, user: User, station_id: int) -> RadioStation:
    station = db.get(RadioStation, station_id)
    if station is None:
        raise HTTPException(404, "Station not found")
    if not station.is_shared and station.owner_user_id != user.id:
        raise HTTPException(404, "Station not found")
    return station


@router.put("/stations/{station_id}/favorite", response_model=IsFavoriteOut)
def set_station_favorite(
    station_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    _get_visible_station(db, user, station_id)
    existing = db.scalars(
        select(RadioStationFavorite)
        .where(
            RadioStationFavorite.user_id == user.id,
            RadioStationFavorite.station_id == station_id,
        )
        .limit(1)
    ).first()
    if existing is None:
        db.add(RadioStationFavorite(user_id=user.id, station_id=station_id))
        db.commit()
    return {"is_favorite": True}


@router.delete("/stations/{station_id}/favorite", response_model=IsFavoriteOut)
def clear_station_favorite(
    station_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    _get_visible_station(db, user, station_id)
    db.execute(
        delete(RadioStationFavorite).where(
            RadioStationFavorite.user_id == user.id,
            RadioStationFavorite.station_id == station_id,
        )
    )
    db.commit()
    return {"is_favorite": False}


@router.get("/stations/{station_id}/stream")
def stream_station(
    station_id: int,
    db: DbSession,
    user: CurrentUser,
) -> StreamingResponse:
    station = _get_visible_station(db, user, station_id)
    url, user_id = station.stream_url, user.id
    # A stream lasts as long as someone listens: give the pooled database
    # connection back now rather than holding it for hours.
    db.close()
    slot = ExitStack()
    try:
        slot.enter_context(stream_slot(user_id))
    except RadioBusy as exc:
        raise HTTPException(429, str(exc)) from None
    try:
        # Connect and read the station's answer before any response exists,
        # so an unreachable or missing stream is a 502/404, not an empty 200.
        upstream = open_stream(url)
    except RadioUnavailable as exc:
        slot.close()
        raise HTTPException(exc.status_code, exc.message) from None
    except BaseException:
        slot.close()
        raise
    chunks = relay_icy(station_id, upstream)

    def cleanup() -> None:
        chunks.close()
        upstream.close()  # a generator never started has no finally to run
        slot.close()

    return StreamingResponse(iterate_then_cleanup(chunks, cleanup), media_type=upstream.media_type)


@router.get("/stations/{station_id}/now-playing", response_model=NowPlayingOut)
def now_playing(
    station_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    _get_visible_station(db, user, station_id)
    return get_now_playing(station_id) or {"title": None, "updated_at": None}


@router.post("/want-list", response_model=WantListOut)
def add_want_list_item(
    body: WantListIn,
    db: DbSession,
    user: CurrentUser,
) -> WantListItem:
    # A song entry like any other on the Want list (app.want_list).
    return add_entry(
        db,
        user,
        {"kind": "track", "artist": body.artist, "title": body.title, "station_name": body.station_name},
    )
