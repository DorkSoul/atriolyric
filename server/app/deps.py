from typing import Annotated

from fastapi import Depends, HTTPException, Request, WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.activity import set_actor
from app.db import get_db
from app.models import Session as SessionModel
from app.models import User, utcnow
from app.security import session_token_digest

COOKIE_NAME = "atriolyric_session"

# Dependency aliases for endpoint parameters: `db: DbSession`,
# `user: CurrentUser`, `admin: AdminUser`.
DbSession = Annotated[Session, Depends(get_db)]


def _user_for_token(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    # Sessions are stored as SHA-256 digests, so a database read alone does
    # not reveal reusable bearer cookies; a cookie is only ever matched by its
    # digest (a stored digest presented as a cookie hashes to something else).
    session = db.scalars(
        select(SessionModel).where(SessionModel.token == session_token_digest(token)).limit(1)
    ).first()
    if session is None or session.expires_at < utcnow():
        return None
    return db.scalars(select(User).where(User.id == session.user_id).limit(1)).first()


def get_current_user(request: Request, db: DbSession) -> User:
    user = _user_for_token(db, request.cookies.get(COOKIE_NAME))
    # Auth is the only database work a request is guaranteed to do before it
    # gets slow (an art request's first-time ffmpeg resize, a stream's
    # transcode, ...), and FastAPI holds this dependency's session open
    # until the response has fully finished - pinning one pooled connection
    # per in-flight request the whole time. Grid views fire hundreds of
    # near-simultaneous art requests, so on a cold cache that pinning alone
    # would exhaust the pool: every new request (including the next tile's
    # auth lookup) would wait out the 30s checkout timeout and fail with a
    # 500, taking the whole app down. Committing hands the connection straight back to the
    # pool without ending the session: the user stays attached (endpoints
    # may db.refresh(user)), and any endpoint that still needs the session
    # checks a connection out again transparently on its next query - the
    # slow art/stream endpoints close it explicitly before their long work
    # (see app.routers.stream).
    db.commit()
    if user is None:
        raise HTTPException(401, "Not authenticated")
    set_actor(user.username, user.id)
    return user


def get_current_user_ws(websocket: WebSocket, db: Session) -> User | None:
    """Same lookup as `get_current_user`, but returns None instead of
    raising - a WebSocket handshake can't be answered with an HTTP error
    body, so the caller closes the connection with a close code instead."""
    return _user_for_token(db, websocket.cookies.get(COOKIE_NAME))


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admin access required")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
