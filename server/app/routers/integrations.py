from fastapi import APIRouter, HTTPException

from app.deps import CurrentUser, DbSession
from app.schemas import (
    IntegrationOut,
    ListenBrainzIn,
    OkOut,
)
from app.scrobbling import link_listenbrainz, list_integrations, unlink

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("", response_model=list[IntegrationOut])
def get_integrations(
    db: DbSession, user: CurrentUser
) -> list[dict]:
    return list_integrations(db, user)


@router.post("/listenbrainz", response_model=OkOut)
def connect_listenbrainz(
    body: ListenBrainzIn, db: DbSession, user: CurrentUser
) -> dict:
    error = link_listenbrainz(db, user, body.token)
    if error:
        raise HTTPException(400, error)
    return {"ok": True}


@router.delete("/listenbrainz", response_model=OkOut)
def disconnect_listenbrainz(
    db: DbSession, user: CurrentUser
) -> dict:
    unlink(db, user, "listenbrainz")
    return {"ok": True}
