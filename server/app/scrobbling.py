import json
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.metadata_providers import listenbrainz
from app.models import MediaItem, ScrobbleQueueItem, User, UserIntegration, utcnow
from app.secrets import decrypt_credential, encrypt_credential

# Each user links their own ListenBrainz account with its user token
# (listenbrainz.org/settings); the token is stored encrypted.
PROVIDERS = ("listenbrainz",)
logger = logging.getLogger(__name__)

# (user id, provider) whose stored credential could not be read, already
# logged - once per process is enough to explain the silence.
_unreadable_logged: set[tuple[int, str]] = set()


def read_credential(integration: UserIntegration) -> dict | None:
    """The integration's decrypted keys, or None when they can't be read
    (the server's secret key changed, or the row is damaged). Then nothing
    can be sent with them: the integration needs reconnecting, which
    list_integrations reports, and it is logged once at WARNING."""
    try:
        keys = json.loads(decrypt_credential(integration.credential))
        if not isinstance(keys, dict) or not keys.get("token"):
            raise ValueError("incomplete credential")
        return keys
    except Exception as exc:  # noqa: BLE001 - any unreadable credential is the same answer
        marker = (integration.user_id, integration.provider)
        if marker not in _unreadable_logged:
            _unreadable_logged.add(marker)
            logger.warning(
                "Stored %s credential for user %s cannot be read (%s); it needs reconnecting",
                integration.provider, integration.user_id, type(exc).__name__,
            )
        return None


def list_integrations(db: Session, user: User) -> list[dict]:
    rows = db.scalars(select(UserIntegration).where(UserIntegration.user_id == user.id)).all()
    stored = {r.provider: r for r in rows}
    out = []
    for p in PROVIDERS:
        row = stored.get(p)
        # A credential that can't be decrypted sends nothing: it is not
        # "linked" (so the page offers to connect again), and says why. So
        # does a token ListenBrainz stopped accepting (revoked or reset).
        keys = read_credential(row) if row is not None else None
        readable = keys is not None and not keys.get("revoked")
        out.append(
            {
                "provider": p,
                "linked": readable,
                "needs_reconnect": row is not None and not readable,
                "external_username": row.external_username if row is not None else None,
                "available": True,
            }
        )
    return out


def link_listenbrainz(db: Session, user: User, token: str) -> str | None:
    """Checks the token with ListenBrainz and keeps it (encrypted) with the
    ListenBrainz user name it belongs to. Returns an error message, or None
    on success."""
    token = token.strip()
    found = listenbrainz.validate_token(token)
    if found is None:
        return "ListenBrainz couldn't be reached - try again shortly."
    if found is False:
        return (
            "ListenBrainz didn't accept that token - copy the user token from "
            "listenbrainz.org/settings."
        )
    assert isinstance(found, str)
    _upsert(db, user, "listenbrainz", json.dumps({"token": token}), external_username=found)
    return None


def unlink(db: Session, user: User, provider: str) -> None:
    db.execute(
        delete(UserIntegration).where(
            UserIntegration.user_id == user.id, UserIntegration.provider == provider
        )
    )
    db.commit()


def _upsert(
    db: Session,
    user: User,
    provider: str,
    credential: str,
    external_username: str | None,
) -> None:
    existing = db.scalars(
        select(UserIntegration)
        .where(UserIntegration.user_id == user.id, UserIntegration.provider == provider)
        .limit(1)
    ).first()
    if existing is None:
        db.add(
            UserIntegration(
                user_id=user.id,
                provider=provider,
                credential=encrypt_credential(credential),
                external_username=external_username,
            )
        )
    else:
        existing.credential = encrypt_credential(credential)
        existing.external_username = external_username
    db.commit()


# A track of 30 seconds or less is never scrobbled (ListenBrainz follows the
# usual scrobbling rule: half the track or four minutes, and longer than 30 s,
# which the client checks), and a queued scrobble older than 14 days is
# dropped rather than retried forever.
MIN_SCROBBLE_SECONDS = 30
SCROBBLE_MAX_AGE = timedelta(days=14)
# A queued scrobble waits 2, 4, 8 ... minutes between attempts, at most 6 h.
_RETRY_BASE = timedelta(minutes=2)
_RETRY_MAX = timedelta(hours=6)
# Sent right after a successful scrobble for the same user: cheap, bounded.
OPPORTUNISTIC_RETRIES = 5


def _submit(keys: dict, artist: str, title: str, album: str | None,
            duration: float | None, started_at: datetime | None,
            recording_mbid: str | None = None, artist_mbids: list[str] | None = None) -> str:
    """One submission; never raises (an unexpected error counts as
    transient, like a network failure)."""
    timestamp = (
        int(started_at.replace(tzinfo=UTC).timestamp()) if started_at is not None else int(time.time())
    )
    try:
        return listenbrainz.submit_listen(
            keys["token"],
            listen_type="single",
            artist=artist,
            title=title,
            album=album,
            listened_at=timestamp,
            duration=duration,
            recording_mbid=recording_mbid,
            artist_mbids=artist_mbids or None,
        )
    except Exception:
        logger.warning("Scrobble to ListenBrainz failed", exc_info=True)
        return "transient"


def _listenbrainz_keys(db: Session, user_id: int) -> dict | None | bool:
    """The user's ListenBrainz token; None when linked but unreadable (it
    needs reconnecting); False when not linked at all."""
    integration = db.get(UserIntegration, (user_id, "listenbrainz"))
    if integration is None:
        return False
    keys = read_credential(integration)
    if keys is not None and keys.get("revoked"):
        return None  # needs linking again; queued scrobbles wait for that
    return keys


def listenbrainz_token(db: Session, user: User | None) -> str | None:
    """The user's linked ListenBrainz token for reads that need one (top
    recordings); None when not linked, unreadable or refused."""
    if user is None:
        return None
    keys = _listenbrainz_keys(db, user.id)
    return keys.get("token") if isinstance(keys, dict) else None


def _mark_revoked(db: Session, user_id: int) -> None:
    """ListenBrainz refused the token (401/403): the link shows "needs
    reconnecting" and nothing more is sent with it until the user links
    again (which stores a fresh credential without the mark)."""
    integration = db.get(UserIntegration, (user_id, "listenbrainz"))
    keys = read_credential(integration) if integration is not None else None
    if integration is None or keys is None or keys.get("revoked"):
        return
    integration.credential = encrypt_credential(json.dumps({**keys, "revoked": True}))
    db.commit()
    logger.warning("ListenBrainz no longer accepts user %s's token; it needs linking again", user_id)


def scrobble_track(
    db: Session, user: User, item: MediaItem, started_at: datetime | None = None
) -> None:
    """Fire-and-forget: push this play to whichever provider(s) the user
    has linked. Never raises - a scrobble failing must never affect
    playback or the /played response.

    `started_at` (naive UTC) is when the listening began - what ListenBrainz
    wants as the timestamp; without it (a bodyless report) it is now. A
    submission that fails for a reason that may pass (network, rate limit,
    5xx) is queued for `retry_queued_scrobbles`, as is one refused for the
    token (it goes out once the user links again, within 14 days); a
    permanent rejection is only logged. After a successful one, a few of
    this user's queued scrobbles go out too."""
    if not item.artist:
        return  # nothing meaningful to scrobble without an artist
    if item.duration_seconds is not None and item.duration_seconds <= MIN_SCROBBLE_SECONDS:
        return  # too short to count as a listen
    keys = _listenbrainz_keys(db, user.id)
    if not isinstance(keys, dict):
        return  # not linked, or its credential needs reconnecting
    recording_mbid = listenbrainz.first_mbid(item.musicbrainz_recording_mbid)
    artist_mbids = listenbrainz.valid_mbids(item.musicbrainz_artist_mbid)
    outcome = _submit(
        keys, item.artist, item.title, item.album, item.duration_seconds, started_at, recording_mbid, artist_mbids
    )
    if outcome in ("transient", "unauthorized"):
        if outcome == "unauthorized":
            _mark_revoked(db, user.id)
        now = utcnow()
        db.add(
            ScrobbleQueueItem(
                user_id=user.id,
                artist=item.artist,
                title=item.title,
                album=item.album,
                duration=item.duration_seconds,
                recording_mbid=recording_mbid,
                artist_mbids=",".join(artist_mbids) or None,
                started_at=started_at or now,
                attempts=1,
                next_attempt_at=now + _RETRY_BASE,
            )
        )
        db.commit()
        logger.info("ListenBrainz did not take a scrobble now (%s); queued for user %s", outcome, user.id)
        return
    if outcome != "accepted":
        logger.warning("ListenBrainz did not accept a scrobble for user %s (%s)", user.id, outcome)
        return
    try:
        retry_queued_scrobbles(db, user_id=user.id, limit=OPPORTUNISTIC_RETRIES)
    except Exception:
        logger.warning("Retrying queued scrobbles failed for user %s", user.id, exc_info=True)


def retry_queued_scrobbles(
    db: Session,
    user_id: int | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> int:
    """Send queued scrobbles that are due, oldest play first; returns how
    many ListenBrainz accepted. Called from the scheduler tick and, for one
    user, after a successful scrobble. Entries older than 14 days are
    dropped, as is one ListenBrainz rejects, or one whose user unlinked
    ListenBrainz. A transient failure backs that entry off exponentially
    and ends the pass: ListenBrainz is down or
    rate-limiting, and the rest would fail the same way. An unreadable
    credential keeps the user's entries until they reconnect or expire."""
    now = now or utcnow()
    expired = delete(ScrobbleQueueItem).where(ScrobbleQueueItem.started_at < now - SCROBBLE_MAX_AGE)
    if user_id is not None:
        expired = expired.where(ScrobbleQueueItem.user_id == user_id)
    db.execute(expired, execution_options={"synchronize_session": False})
    db.commit()
    due = select(ScrobbleQueueItem).where(ScrobbleQueueItem.next_attempt_at <= now)
    if user_id is not None:
        due = due.where(ScrobbleQueueItem.user_id == user_id)
    due = due.order_by(ScrobbleQueueItem.started_at, ScrobbleQueueItem.id)
    if limit is not None:
        due = due.limit(limit)
    keys_by_user: dict[int, dict | None | bool] = {}
    sent = 0
    for entry in db.scalars(due).all():
        if entry.user_id not in keys_by_user:
            keys_by_user[entry.user_id] = _listenbrainz_keys(db, entry.user_id)
        keys = keys_by_user[entry.user_id]
        if keys is False:
            db.delete(entry)
            db.commit()
            continue
        if not isinstance(keys, dict):
            continue
        outcome = _submit(
            keys, entry.artist, entry.title, entry.album, entry.duration, entry.started_at,
            entry.recording_mbid, listenbrainz.valid_mbids(entry.artist_mbids),
        )
        if outcome == "unauthorized":
            # Kept for when the user links again; their other entries wait too.
            _mark_revoked(db, entry.user_id)
            keys_by_user[entry.user_id] = None
            continue
        if outcome == "transient":
            entry.attempts += 1
            entry.next_attempt_at = now + min(_RETRY_MAX, _RETRY_BASE * 2 ** (entry.attempts - 1))
            db.commit()
            break
        if outcome == "accepted":
            sent += 1
        else:
            logger.warning(
                "ListenBrainz did not accept a queued scrobble for user %s (%s); dropped",
                entry.user_id, outcome,
            )
        db.delete(entry)
        db.commit()
    return sent
