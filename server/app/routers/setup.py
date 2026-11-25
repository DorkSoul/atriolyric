"""First-run setup: create the first admin account from the browser.

On a database with no users and no ``ATRIOLYRIC_DEFAULT_PASSWORD``, startup
issues a one-time setup code and writes it to the server log. Whoever
creates the first account must supply it, so an instance that is reachable
before its owner gets to it can't be claimed by someone else.
"""

import logging
import secrets
import threading

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import login_limits
from app.deps import DbSession
from app.models import User
from app.routers.auth import UserOut, start_session
from app.schemas import (
    SetupIn,
    SetupStatusOut,
)
from app.security import hash_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/setup", tags=["setup"])

_setup_code: str | None = None
_setup_lock = threading.Lock()


def issue_setup_code() -> str:
    """Create the setup code (once per process) and log it."""
    global _setup_code
    with _setup_lock:
        if _setup_code is None:
            _setup_code = secrets.token_urlsafe(9)
            logger.warning(
                "No accounts exist yet. Open Atriolyric in a browser and enter "
                "this setup code to create the admin account: %s",
                _setup_code,
            )
        return _setup_code


def _needs_setup(db: Session) -> bool:
    return db.execute(select(User.id).limit(1)).first() is None


@router.get("/status", response_model=SetupStatusOut)
def setup_status(db: DbSession) -> dict:
    needs_setup = _needs_setup(db)
    if needs_setup:
        # Covers a database emptied while the server was running: there is
        # always a code to find in the log while setup is possible.
        issue_setup_code()
    return {"needs_setup": needs_setup}


@router.post("", response_model=UserOut)
def complete_setup(
    body: SetupIn,
    request: Request,
    response: Response,
    db: DbSession,
) -> User:
    global _setup_code
    key = login_limits.login_key(request)
    login_limits.check_login_rate_limit(key)
    with _setup_lock:
        if not _needs_setup(db):
            raise HTTPException(409, "Setup has already been completed")
        if _setup_code is None or not secrets.compare_digest(
            body.code.strip().encode(), _setup_code.encode()
        ):
            login_limits.record_login_result(key, False)
            raise HTTPException(403, "That setup code isn't right - check the server log")
        username = body.username.strip()
        if not username:
            raise HTTPException(422, "Username cannot be blank")
        try:
            password_hash = hash_password(body.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        user = User(username=username, password_hash=password_hash, is_admin=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        _setup_code = None
    login_limits.record_login_result(key, True)
    start_session(db, user, request, response)
    logger.info("Setup complete: created admin %r", username)
    return user
