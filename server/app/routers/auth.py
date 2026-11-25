import hashlib
import logging
from datetime import timedelta

from fastapi import (
    APIRouter,
    BackgroundTasks,
    HTTPException,
    Request,
    Response,
)
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import login_limits
from app.activity import log_action, set_actor
from app.config import is_https
from app.deps import CurrentUser, DbSession
from app.models import Session as SessionModel
from app.models import User, utcnow
from app.schemas import (
    AccentColorIn,
    AudioSettingsIn,
    AudioSettingsOut,
    LoginIn,
    NoticeIn,
    OkOut,
    PasswordChangeIn,
    PrivacySettings,
    UsernameChangeIn,
    UserOut,
)
from app.security import (
    generate_session_token,
    hash_password,
    session_token_digest,
    verify_password,
)
from app.ws_manager import broadcast_to_user, close_user_sessions

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_TTL = timedelta(days=30)
COOKIE_NAME = "atriolyric_session"
DUMMY_PASSWORD_HASH = hash_password("atriolyric-invalid-login-placeholder")


def _username_log_label(username: str, known: bool) -> str:
    """Typed usernames are sometimes passwords: log a real username only when
    it belongs to an account, otherwise a short stable fingerprint."""
    if known:
        return username
    return "unknown:" + hashlib.sha256(username.encode()).hexdigest()[:8]


@router.post("/login", response_model=UserOut)
def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: DbSession,
) -> User:  # serialised through response_model=UserOut
    login_key = login_limits.login_key(request)
    login_limits.check_login_rate_limit(login_key, body.username)
    user = db.scalars(select(User).where(User.username == body.username).limit(1)).first()
    password_matches = verify_password(
        body.password, user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    )
    if user is None or not password_matches:
        login_limits.record_login_result(login_key, False, body.username)
        log_action(
            "auth.login_failed", level=logging.WARNING,
            username=_username_log_label(body.username, user is not None), ip=login_key,
            reason="unknown user" if user is None else "wrong password",
        )
        raise HTTPException(401, "Invalid username or password")
    login_limits.record_login_result(login_key, True, body.username)
    start_session(db, user, request, response)
    log_action("auth.login", user=f"{user.username}#{user.id}", ip=login_key)
    return user


def _secure(request: Request) -> bool:
    return is_https(request.url.scheme, request.headers.get("x-forwarded-proto"))


def start_session(db: Session, user: User, request: Request, response: Response) -> None:
    """Create a session for `user` and set its cookie on `response`."""
    token = generate_session_token()
    db.add(
        SessionModel(
            token=session_token_digest(token),
            user_id=user.id,
            expires_at=utcnow() + SESSION_TTL,
        )
    )
    db.commit()

    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_secure(request),
    )


@router.post("/logout", response_model=OkOut)
def logout(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    db: DbSession,
) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        digest = session_token_digest(token)
        row = db.scalars(select(SessionModel).where(SessionModel.token == digest).limit(1)).first()
        if row is not None:
            user_id = row.user_id
            # This endpoint reads the cookie itself rather than through
            # CurrentUser, so name the actor here - otherwise the activity log
            # can only say "Someone signed out".
            user = db.get(User, user_id)
            if user is not None:
                set_actor(user.username, user.id)
            db.delete(row)
            db.commit()
            background_tasks.add_task(close_user_sessions, user_id, [digest])
    log_action("auth.logout")
    response.delete_cookie(
        COOKIE_NAME, httponly=True, samesite="lax", secure=_secure(request)
    )
    return {"ok": True}


@router.post("/logout-everywhere", response_model=OkOut)
def logout_everywhere(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """Sign the account out on every device, this one included (a lost phone,
    a shared computer): every session is deleted and every open playback
    socket closed."""
    db.execute(
        delete(SessionModel).where(SessionModel.user_id == user.id),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    background_tasks.add_task(close_user_sessions, user.id, None)
    log_action("auth.logout_everywhere")
    response.delete_cookie(
        COOKIE_NAME, httponly=True, samesite="lax", secure=_secure(request)
    )
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> User:  # serialised through response_model=UserOut
    return user


@router.post("/dismissed-notices", response_model=UserOut)
def dismiss_notice(
    body: NoticeIn,
    db: DbSession,
    user: CurrentUser,
) -> User:
    """Remember "Never show again" for one notice on this account."""
    notices = set(user.dismissed_notices)
    notices.add(body.notice)
    user.dismissed_notices_json = sorted(notices)
    db.commit()
    db.refresh(user)
    return user


@router.put("/accent-color", response_model=UserOut)
def set_accent_color(
    background_tasks: BackgroundTasks,
    body: AccentColorIn,
    db: DbSession,
    user: CurrentUser,
) -> User:
    """Save the accent color on the account so every device - current and
    future - picks it up, then push it live to connected devices."""
    user.accent_color = body.accent_color.lower()
    db.commit()
    db.refresh(user)

    # Fire-and-forget push to every open playback socket, on the event loop
    # after the response is sent (BackgroundTasks works from sync endpoints,
    # which otherwise run in a threadpool with no running loop to schedule
    # on). Devices that miss it (mid-reconnect, etc.) still get the saved
    # color from /api/auth/me on their next login or page load.
    background_tasks.add_task(
        broadcast_to_user,
        user.id,
        {"type": "accent_color_changed", "accent_color": user.accent_color},
    )
    return user


EQ_GAIN_LIMIT_DB = 12.0


def _audio_settings(user: User) -> dict:
    return {
        "volume_leveling": user.volume_leveling,
        "eq_preset": user.eq_preset,
        "eq_gains": user.eq_gains,
    }


@router.put("/audio-settings", response_model=AudioSettingsOut)
def set_audio_settings(
    background_tasks: BackgroundTasks,
    body: AudioSettingsIn,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """Save volume levelling and equaliser settings on the account (not the
    device) and push them live to the account's other connected devices."""
    if body.volume_leveling is not None:
        user.volume_leveling = body.volume_leveling
    if body.eq_preset is not None:
        user.eq_preset = body.eq_preset
    if body.eq_gains is not None:
        user.eq_gains_json = [max(-EQ_GAIN_LIMIT_DB, min(EQ_GAIN_LIMIT_DB, g)) for g in body.eq_gains]
    db.commit()
    db.refresh(user)
    settings_now = _audio_settings(user)
    background_tasks.add_task(
        broadcast_to_user, user.id, {"type": "audio_settings_changed", **settings_now}
    )
    return settings_now


@router.get("/privacy-settings", response_model=PrivacySettings)
def get_privacy_settings(user: CurrentUser) -> dict:
    return {"online_recommendations": user.online_recommendations}


@router.put("/privacy-settings", response_model=PrivacySettings)
def set_privacy_settings(
    body: PrivacySettings,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    user.online_recommendations = body.online_recommendations
    db.commit()
    log_action("privacy.online_recommendations", enabled=body.online_recommendations)
    return {"online_recommendations": user.online_recommendations}


@router.post("/username", response_model=UserOut)
def change_username(
    body: UsernameChangeIn,
    db: DbSession,
    user: CurrentUser,
) -> User:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    new_username = body.new_username.strip()
    if not new_username:
        raise HTTPException(422, "Username cannot be blank")
    if new_username == user.username:
        return user
    if db.scalars(select(User).where(User.username == new_username).limit(1)).first() is not None:
        raise HTTPException(409, "That username is already taken")
    old_username = user.username
    user.username = new_username
    db.commit()
    db.refresh(user)
    log_action("auth.username_change", user=user.id, old=old_username, new=new_username)
    return user


@router.post("/password", response_model=OkOut)
def change_password(
    body: PasswordChangeIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    try:
        user.password_hash = hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

    # Invalidate every other login after a password change, retaining only
    # the browser that performed it.
    token = request.cookies.get(COOKIE_NAME, "")
    keep = session_token_digest(token) if token else None
    criteria = [SessionModel.user_id == user.id]
    if keep is not None:
        criteria.append(SessionModel.token != keep)
    revoked = list(db.scalars(select(SessionModel.token).where(*criteria)))
    db.execute(delete(SessionModel).where(*criteria), execution_options={"synchronize_session": False})
    db.commit()
    # Their sockets would otherwise stay open (and keep receiving
    # broadcasts) until the next message they send.
    background_tasks.add_task(close_user_sessions, user.id, revoked)
    log_action("auth.password_change")
    return {"ok": True}
