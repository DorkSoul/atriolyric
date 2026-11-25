import asyncio
import contextlib
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketState

from app.config import settings
from app.db import count_rows
from app.deps import (
    COOKIE_NAME,
    CurrentUser,
    DbSession,
    get_current_user_ws,
)
from app.models import PlaybackQueueItem, PlaybackState, User
from app.playback import (
    STALE_QUEUE_ITEM,
    add_many_to_queue,
    add_to_queue,
    build_state_snapshot,
    bump_revision,
    claim_output_for_requester,
    disconnect_device_for_user_id,
    get_or_create_state,
    jump_to_index,
    move_queue_item,
    next_track,
    prev_track,
    register_device,
    remove_device,
    remove_from_queue,
    reorder_queue_item,
    report_position,
    seek,
    set_playing,
    set_queue_context,
    set_repeat,
    set_shuffle,
    touch_device,
    track_ended,
    transfer_to,
)
from app.playback_protocol import PlaybackStateSnapshot
from app.schemas import PlaySourceIn
from app.security import session_token_digest
from app.security_headers import origin_allowed
from app.ws_manager import manager

router = APIRouter(tags=["playback"])
logger = logging.getLogger(__name__)


def _playback_diagnostic_state(db: Session, user_id: int) -> dict:
    state = db.get(PlaybackState, user_id)
    if state is None:
        return {"exists": False}
    queue_length = count_rows(db, select(PlaybackQueueItem.id).where(PlaybackQueueItem.user_id == user_id))
    return {
        "exists": True,
        "active_device_id": state.active_device_id,
        "media_item_id": state.media_item_id,
        "is_playing": state.is_playing,
        "position_seconds": round(float(state.position_seconds or 0), 3),
        "queue_index": state.queue_index,
        "queue_length": queue_length,
        "repeat_mode": state.repeat_mode,
    }


def _log_playback_event(
    event: str, user_id: int, device_id: int | None, **details
) -> None:
    """An operational playback line, written at every log setting. `details`
    must not carry a client address, a user agent or a media or queue item
    id: those belong in `_log_playback_diagnostic`."""
    record = {
        "event": event,
        "user_id": user_id,
        "device_id": device_id,
        **details,
    }
    logger.info("playback_diagnostic %s", json.dumps(record, sort_keys=True))


def _log_playback_diagnostic(
    event: str, user_id: int, device_id: int | None, **details
) -> None:
    """Playback tracing, written only with verbose logging on: where each
    socket connected from and what each user played when. Together that is
    a per-user listening history, which the default log promises not to
    hold."""
    if settings.verbose_logging:
        _log_playback_event(event, user_id, device_id, **details)


def _finite_diagnostic_number(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(cast(float, value))  # type() above admits only int and float
    except (OverflowError, ValueError):
        return None
    return round(number, 3) if math.isfinite(number) else None


# SQLite integers are signed 64-bit; a larger id cannot name a row and
# would overflow when bound to a query.
_MAX_ROW_ID = 2**63 - 1


class _Invalid:
    """Marks a field that is present but malformed (None means absent)."""


INVALID = _Invalid()


def _queue_item_id(msg: dict) -> int | _Invalid:
    """`queue_item_id` on track_ended, seek and position_report: required, a
    positive row id. A message without one is invalid."""
    value = msg.get("queue_item_id")
    if type(value) is not int or not 0 < value <= _MAX_ROW_ID:
        return INVALID
    return value


def _optional_position(msg: dict) -> float | None | _Invalid:
    """A position in seconds: null (JSON.stringify turns NaN into null) means
    not known; otherwise a finite, non-negative number. Whether the key may
    be left out is the caller's business."""
    value = msg.get("position_seconds")
    if value is None:
        return None
    if type(value) not in (int, float):
        return INVALID
    number = float(cast(float, value))  # type() above admits only int and float
    if not math.isfinite(number) or number < 0:
        return INVALID
    return number


def _run_playback_job(bind, user_id: int, operation, *args):
    """Run a potentially blocking playback operation with a thread-owned session."""
    worker_db = Session(bind=bind, autoflush=False, expire_on_commit=False)
    try:
        worker_user = worker_db.get(User, user_id)
        if worker_user is None:
            return "User not found"
        return operation(worker_db, worker_user, *args)
    finally:
        worker_db.close()




def _in_session(bind, operation, *args):
    """`operation(db, *args)` with a session owned by the calling (worker)
    thread - for work that is not about one loaded user."""
    worker_db = Session(bind=bind, autoflush=False, expire_on_commit=False)
    try:
        return operation(worker_db, *args)
    finally:
        worker_db.close()


@router.get("/api/playback/state", response_model=PlaybackStateSnapshot)
def get_state(
    db: DbSession, user: CurrentUser
) -> dict:
    return build_state_snapshot(db, user)


# --- Database work for the socket, run in worker threads ---------------------
#
# Every message's database work (the re-authentication, the command, the
# snapshot) runs through asyncio.to_thread with a thread-owned session, so a
# write waiting behind another writer does not hold the event loop - and with
# it every socket and every async endpoint - for up to the 30-second busy
# timeout. Each job is awaited before the socket reads its next message, so one
# socket's messages are still handled strictly in order; only the waiting is
# off the loop. Broadcasts stay on the loop.


class _UserGone(Exception):
    """The account was deleted while the socket was open."""


def _authenticated_user_id(db: Session, websocket: WebSocket) -> int | None:
    user = get_current_user_ws(websocket, db)
    return user.id if user is not None else None


def _run_user_job(bind, user_id: int, operation, *args):
    """`operation(db, user, *args)` in the calling worker thread."""
    worker_db = Session(bind=bind, autoflush=False, expire_on_commit=False)
    try:
        worker_user = worker_db.get(User, user_id)
        if worker_user is None:
            raise _UserGone
        return operation(worker_db, worker_user, *args)
    finally:
        worker_db.close()


def _bump_and_snapshot(db: Session, user: User) -> dict:
    # Every broadcast follows an accepted change, so this is where the
    # revision is counted - except for a change that counted its own in the
    # commit that made it (see `revision_counted` in _broadcast_state).
    bump_revision(db, user)
    return build_state_snapshot(db, user)


def _register_job(
    db: Session, user: User, name: str, device_type: str, resume: bool, token: str | None
) -> tuple[int, dict, dict]:
    before_state = _playback_diagnostic_state(db, user.id)
    device = register_device(db, user, name, device_type, resume, token)
    return device.id, before_state, _playback_diagnostic_state(db, user.id)


def _touch_job(db: Session, _user: User, device_id: int) -> None:
    touch_device(db, device_id)


def _diagnostic_state_job(db: Session, user: User) -> dict:
    return _playback_diagnostic_state(db, user.id)


def _track_ended_job(
    db: Session, user: User, device_id: int, queue_item_id: int
) -> tuple[str, dict, dict | None]:
    before_state = _playback_diagnostic_state(db, user.id)
    _log_playback_diagnostic("track_ended_received", user.id, device_id, state=before_state)
    state = db.get(PlaybackState, user.id)
    if state is None or state.active_device_id != device_id:
        _log_playback_diagnostic(
            "track_ended_rejected_inactive_device", user.id, device_id, state=before_state
        )
        return "inactive", before_state, None
    # Can fall through to next_track -> _handle_context_exhaustion, the
    # slowest work any message does (the autoplay lookup).
    try:
        outcome = track_ended(db, user, queue_item_id)
    except Exception:
        # A failure is logged at every setting; what was playing only in
        # verbose mode.
        logger.exception(
            "playback_diagnostic event=track_ended_failed user_id=%s device_id=%s before_state=%s",
            user.id,
            device_id,
            json.dumps(before_state, sort_keys=True) if settings.verbose_logging else "-",
        )
        raise
    if outcome == STALE_QUEUE_ITEM:
        return "stale", before_state, None
    return "processed", before_state, _playback_diagnostic_state(db, user.id)


# --- Liveness ---------------------------------------------------------------
#
# A socket is only known to be gone when its close arrives, and some never
# do: a tab closed on a phone whose connection drops without a close (a proxy
# or relay keeping its half open), a frozen background tab whose browser
# still answers the transport-level pings (so uvicorn's keepalive never
# fires). Messages are the proof that the page is there: the web client sends
# a heartbeat every 20 s and, while it plays as the active device, a position
# report every 5 s.

# A playing active device not heard from for this long no longer blocks
# another device's explicit "play this" (claim_output_for_requester): two
# position reports missed, with slack for timer and network jitter.
ACTIVE_DEVICE_SILENT_SECONDS = 10.0
# A socket that delivers nothing for this long is closed with 4408 and its
# device marked offline. Generous: a hidden, silent tab's timers can be
# throttled to once a minute (Chrome's intensive throttling), so a live
# client may send a heartbeat only every 60 s. The client reconnects after
# any close but 4000 and 4401.
SILENT_SOCKET_CLOSE_SECONDS = 150.0
SILENT_SOCKET_CLOSE_CODE = 4408


def _active_is_live(user_id: int) -> Callable[[int], bool]:
    """The claim rule's view of whether a device's socket is alive: it is
    registered in this process and has delivered a message recently. Read
    from worker threads (plain dict reads; the registry only changes on the
    event loop)."""

    def is_live(device_id: int) -> bool:
        silence = manager.silence(user_id, device_id)
        live = silence is not None and silence <= ACTIVE_DEVICE_SILENT_SECONDS
        if not live:
            # None: no socket registered for the device in this process.
            _log_playback_event(
                "active_device_not_live",
                user_id,
                device_id,
                silent_seconds=None if silence is None else round(silence, 1),
            )
        return live

    return is_live


def _play_job(
    db: Session, user: User, device_id: int, active_is_live: Callable[[int], bool]
) -> None:
    # Claims output when stopped or when the active device is offline/gone
    # (see claim_output_for_requester); otherwise a remote's play.
    claim_output_for_requester(
        db, user, get_or_create_state(db, user), device_id, active_is_live
    )
    db.commit()
    set_playing(db, user, True)


def _pause_job(db: Session, user: User, device_id: int, position: float | None) -> None:
    # Only the active device's element knows where playback is; a remote
    # pause just pauses.
    state = db.get(PlaybackState, user.id)
    if state is None or state.active_device_id != device_id:
        position = None
    set_playing(db, user, False, position)


def _transfer_job(db: Session, user: User, target_device_id: int) -> tuple[str | None, dict, dict]:
    before_state = _playback_diagnostic_state(db, user.id)
    error = transfer_to(db, user, target_device_id)
    return error, before_state, _playback_diagnostic_state(db, user.id)


def _teardown_job(db: Session, user_id: int, device_id: int) -> bool:
    """Mark the device offline; returns whether the account still exists
    (an admin may have deleted it while the socket was open)."""
    before_state = _playback_diagnostic_state(db, user_id)
    disconnect_device_for_user_id(db, user_id, device_id)
    _log_playback_diagnostic(
        "device_disconnected",
        user_id,
        device_id,
        before_state=before_state,
        after_state=_playback_diagnostic_state(db, user_id),
    )
    return db.get(User, user_id) is not None


async def _broadcast_state(
    bind,
    user_id: int,
    *,
    position_changed: bool = False,
    diagnostic_event: str | None = None,
    source_device_id: int | None = None,
    source_command: str | None = None,
    revision_counted: bool = False,
) -> None:
    try:
        snapshot = await asyncio.to_thread(
            _run_user_job,
            bind,
            user_id,
            build_state_snapshot if revision_counted else _bump_and_snapshot,
        )
    except _UserGone:
        return  # nobody left to tell
    # Who caused this broadcast and with what command, so a device can tell
    # the echo of its own position report (which on a slow link can arrive
    # several reports late) from another device's seek.
    delivery = await manager.broadcast(
        user_id,
        {
            "type": "state",
            "state": snapshot,
            "position_changed": position_changed,
            "source_device_id": source_device_id,
            "source_command": source_command,
        },
    )
    if diagnostic_event is not None:
        _log_playback_diagnostic(
            "state_broadcast",
            user_id,
            source_device_id,
            trigger=diagnostic_event,
            position_changed=position_changed,
            state={
                "active_device_id": snapshot["active_device_id"],
                "media_item_id": snapshot["media_item_id"],
                "is_playing": snapshot["is_playing"],
                "queue_index": snapshot["queue_index"],
                "queue_length": len(snapshot["queue_media_item_ids"]),
            },
            delivered_device_ids=delivery["delivered_device_ids"],
            failed_device_ids=delivery["failed_device_ids"],
        )


# --- One socket and its message handlers --------------------------------------


@dataclass
class _Connection:
    websocket: WebSocket
    bind: Any
    user_id: int
    device_id: int | None = None
    # Set when a broadcast could not deliver to this socket (see
    # ConnectionManager.broadcast); the receive loop then closes it.
    dropped: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, operation, *args):
        """`operation(db, user, *args)` in a worker thread, awaited."""
        return await asyncio.to_thread(_run_user_job, self.bind, self.user_id, operation, *args)

    async def error(self, message: str) -> None:
        await self.websocket.send_json({"type": "error", "message": message})

    async def broadcast(
        self,
        command: str | None,
        *,
        position_changed: bool = False,
        diagnostic_event: str | None = None,
        revision_counted: bool = False,
    ) -> None:
        await _broadcast_state(
            self.bind,
            self.user_id,
            position_changed=position_changed,
            diagnostic_event=diagnostic_event,
            source_device_id=self.device_id,
            source_command=command,
            revision_counted=revision_counted,
        )

    async def send_state(self, command: str) -> None:
        """The current state to this socket only, for a message that changed
        nothing (so no revision is counted and nobody else is told)."""
        snapshot = await self.run(build_state_snapshot)
        await self.websocket.send_json(
            {
                "type": "state",
                "state": snapshot,
                "position_changed": False,
                "source_device_id": self.device_id,
                "source_command": command,
            }
        )


def _registered_device_id(conn: _Connection) -> int:
    # The dispatcher only calls handlers below once a device is registered.
    assert conn.device_id is not None
    return conn.device_id


async def _on_register_device(conn: _Connection, msg: dict) -> None:
    if conn.device_id is not None:
        await conn.error("Device is already registered")
        return
    name = msg.get("name", "Device")
    device_type = msg.get("device_type", "web")
    resume_playback = msg.get("resume_playback", False)
    client_token = msg.get("client_token")
    if (
        not isinstance(name, str)
        or not isinstance(device_type, str)
        or not isinstance(resume_playback, bool)
        or (client_token is not None and not isinstance(client_token, str))
        or (isinstance(client_token, str) and len(client_token) > 128)
    ):
        await conn.error("Invalid device registration")
        return
    client_token = client_token.strip() if isinstance(client_token, str) else None
    client_token = client_token or None
    device_type = device_type.strip()[:30] or "web"
    device_id, before_state, after_state = await conn.run(
        _register_job, name.strip()[:100] or "Device", device_type, resume_playback, client_token
    )
    conn.device_id = device_id
    # A browser keeps its device id across reconnects, so this may replace a
    # socket the server has not yet noticed is dead (or a background tab's).
    # The registry now points at this socket; the old handler's teardown fails
    # its identity check and leaves the device online. The replaced socket is
    # closed with 4000 "replaced" so it is not left registered-but-deaf. That
    # is safe because the client treats 4000 as final (no timed
    # reconnect; it reconnects only when its tab is shown or focused), so two
    # tabs of one browser do not evict each other in a loop. Flap guard: a
    # bundle that still reconnects after ANY close would loop, so when the
    # previous socket registered less than _REPLACE_FLAP_SECONDS ago it is
    # left open and the skip is logged. The close is a tracked
    # background task, so a slow or failing close never delays or breaks this
    # registration.
    cookie = conn.websocket.cookies.get(COOKIE_NAME)
    previous_age = manager.registered_age(conn.user_id, device_id)
    previous = manager.connect(
        conn.user_id,
        device_id,
        conn.websocket,
        session_digest=session_token_digest(cookie) if cookie else None,
        on_dropped=conn.dropped.set,
    )
    if previous is not None and previous is not conn.websocket:
        if previous_age is not None and previous_age < _REPLACE_FLAP_SECONDS:
            logger.info(
                "ws_replaced_close_skipped user_id=%s device_id=%s previous_age=%.2f",
                conn.user_id,
                device_id,
                previous_age,
            )
        else:
            task = asyncio.ensure_future(_close_replaced(previous, conn.user_id, device_id))
            _TEARDOWN_TASKS.add(task)
            task.add_done_callback(_TEARDOWN_TASKS.discard)
    _log_playback_diagnostic(
        "device_registered",
        conn.user_id,
        device_id,
        device_type=device_type,
        resume_playback=resume_playback,
        before_state=before_state,
        after_state=after_state,
    )
    await conn.websocket.send_json({"type": "registered", "device_id": device_id})
    await conn.broadcast(None, diagnostic_event="device_registered")


async def _on_heartbeat(conn: _Connection, _msg: dict) -> None:
    await conn.run(_touch_job, _registered_device_id(conn))
    # Liveness only: answered to the sender, not broadcast (a broadcast
    # snapshot would be a chance for a stale snapshot to undo a local
    # advance). Clients ignore message types they do not know.
    await conn.websocket.send_json({"type": "heartbeat_ack"})


def _ident(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", value):
        return value
    return None


def _int_or_none(value: object) -> int | None:
    return value if type(value) is int else None


def _bool_or_none(value: object) -> bool | None:
    return value if type(value) is bool else None


async def _on_client_diagnostic(conn: _Connection, msg: dict) -> None:
    # This small allowlisted event lets a mobile reproduction be traced in
    # container logs even when browser developer tools are unavailable.
    # Never record arbitrary client text.
    event = msg.get("event")
    if event == "media_error":
        # Playback failures (a video's HLS error, a media element error).
        # Only short identifier-like strings and numbers are kept, never
        # free text. The failure itself is logged at every setting; which
        # item it was and where in it only in verbose mode.
        failure = {
            "source": _ident(msg.get("source")),
            "error_code": _int_or_none(msg.get("error_code")),
            "hls_type": _ident(msg.get("hls_type")),
            "hls_details": _ident(msg.get("hls_details")),
            "fatal": _bool_or_none(msg.get("fatal")),
            "recovery": _ident(msg.get("recovery")),
            "http_status": _int_or_none(msg.get("http_status")),
            "ready_state": _int_or_none(msg.get("ready_state")),
            "network_state": _int_or_none(msg.get("network_state")),
        }
        if not settings.verbose_logging:
            _log_playback_event(
                "client_media_error", conn.user_id, conn.device_id, client=failure
            )
            return
        _log_playback_diagnostic(
            "client_media_error",
            conn.user_id,
            conn.device_id,
            client={
                **failure,
                "media_item_id": _int_or_none(msg.get("media_item_id")),
                "segment": _int_or_none(msg.get("segment")),
                "current_time": _finite_diagnostic_number(msg.get("current_time")),
                "buffered_end": _finite_diagnostic_number(msg.get("buffered_end")),
            },
            server_state=await conn.run(_diagnostic_state_job),
        )
    elif event == "media_ended" and settings.verbose_logging:
        _log_playback_diagnostic(
            "client_media_ended",
            conn.user_id,
            conn.device_id,
            client={
                "media_item_id": _int_or_none(msg.get("media_item_id")),
                "current_time": _finite_diagnostic_number(msg.get("current_time")),
                "duration": _finite_diagnostic_number(msg.get("duration")),
                "paused": _bool_or_none(msg.get("paused")),
                "ready_state": _int_or_none(msg.get("ready_state")),
                "visibility": msg.get("visibility")
                if msg.get("visibility") in {"visible", "hidden"}
                else None,
                "is_active_device": _bool_or_none(msg.get("is_active_device")),
            },
            server_state=await conn.run(_diagnostic_state_job),
        )


def _queue_origin(raw: object) -> dict | None:
    """The `origin` of a set_queue_context: what the queue is started from,
    validated as a play source (a radio station never starts a queue). One
    that is missing or malformed is dropped - it only labels the queue, so
    it must never stop playback from starting."""
    if not isinstance(raw, dict):
        return None
    try:
        origin = PlaySourceIn.model_validate(raw)
    except ValueError:
        return None
    if origin.kind == "radio":
        return None
    return {"kind": origin.kind, "label": origin.label, "payload": origin.payload}


async def _on_set_queue_context(conn: _Connection, msg: dict) -> None:
    context = msg.get("context")
    start_media_item_id = msg.get("start_media_item_id")
    start_index = msg.get("start_index")
    if (
        not isinstance(context, dict)
        or type(start_media_item_id) is not int
        or (start_index is not None and type(start_index) is not int)
    ):
        await conn.error("Invalid queue")
        return
    # Optional: where the device already is in the start item, and whether it
    # is paused there (a queue played with no connection, handed over).
    position = _optional_position(msg)
    paused = msg.get("paused", False)
    if position is INVALID or type(paused) is not bool:
        await conn.error("Invalid queue")
        return
    error = await conn.run(
        set_queue_context,
        context,
        start_media_item_id,
        start_index,
        conn.device_id,
        _active_is_live(conn.user_id),
        _queue_origin(msg.get("origin")),
        cast(float | None, position) or 0.0,
        paused,
    )
    if error:
        await conn.error(error)
        return
    await conn.broadcast(
        "set_queue_context", position_changed=True, diagnostic_event="set_queue_context"
    )


async def _on_track_ended(conn: _Connection, msg: dict) -> None:
    ended_item_id = _queue_item_id(msg)
    if isinstance(ended_item_id, _Invalid):
        await conn.error("Invalid queue item id")
        return
    device_id = _registered_device_id(conn)
    outcome, before_state, after_state = await conn.run(
        _track_ended_job, device_id, ended_item_id
    )
    if outcome == "inactive":
        await conn.error("Only the active device can end a track")
        return
    if outcome == "stale":
        # The item that ended is no longer current: a replay after a
        # reconnect, or Next pressed elsewhere at the same moment. Nothing
        # changed, so nothing is broadcast; the sender gets the current state
        # to converge on.
        _log_playback_diagnostic(
            "track_ended_ignored_stale",
            conn.user_id,
            device_id,
            queue_item_id=ended_item_id,
            state=before_state,
        )
        await conn.send_state("track_ended")
        return
    _log_playback_diagnostic(
        "track_ended_processed",
        conn.user_id,
        device_id,
        before_state=before_state,
        after_state=after_state,
    )
    await conn.broadcast("track_ended", position_changed=True, diagnostic_event="track_ended")


async def _on_set_repeat(conn: _Connection, msg: dict) -> None:
    mode = msg.get("mode")
    if type(mode) is not str:
        await conn.error("Invalid repeat mode")
        return
    error = await conn.run(set_repeat, mode)
    if error:
        await conn.error(error)
        return
    await conn.broadcast("set_repeat")


async def _on_add_to_queue(conn: _Connection, msg: dict) -> None:
    media_item_id = msg.get("media_item_id")
    mode = msg.get("mode", "end")
    if type(media_item_id) is not int or type(mode) is not str or mode not in {"next", "end"}:
        await conn.error("Invalid add-to-queue command")
        return
    error = await conn.run(add_to_queue, media_item_id, mode)
    if error:
        await conn.error(error)
        return
    await conn.broadcast("add_to_queue")


async def _on_add_many_to_queue(conn: _Connection, msg: dict) -> None:
    """Several items in one change (a whole album or playlist queued at
    once). Both fields are required; the count and the items themselves are
    checked by add_many_to_queue, which refuses the batch as a whole."""
    media_item_ids = msg.get("media_item_ids")
    mode = msg.get("mode")
    if (
        type(media_item_ids) is not list
        or not media_item_ids
        or any(type(i) is not int for i in media_item_ids)
        or mode not in ("next", "end")
    ):
        await conn.error("Invalid add-to-queue command")
        return
    error = await conn.run(add_many_to_queue, cast(list[int], media_item_ids), mode)
    if error:
        await conn.error(error)
        return
    await conn.broadcast("add_many_to_queue")


async def _on_play(conn: _Connection, _msg: dict) -> None:
    await conn.run(_play_job, _registered_device_id(conn), _active_is_live(conn.user_id))
    await conn.broadcast("play")


async def _on_pause(conn: _Connection, msg: dict) -> None:
    # `position_seconds` is required: where the active device's element
    # stopped, or null when the sender has no usable position (a remote, no
    # metadata yet, a media error) - the saved position is then kept.
    paused_at = _optional_position(msg) if "position_seconds" in msg else INVALID
    if isinstance(paused_at, _Invalid):
        await conn.error("Invalid pause position")
        return
    await conn.run(_pause_job, _registered_device_id(conn), paused_at)
    await conn.broadcast("pause")


async def _on_seek(conn: _Connection, msg: dict) -> None:
    try:
        position = float(msg.get("position_seconds", 0))
    except (TypeError, ValueError):
        position = math.nan
    if not math.isfinite(position):
        await conn.error("Invalid seek position")
        return
    seek_item_id = _queue_item_id(msg)
    if isinstance(seek_item_id, _Invalid):
        await conn.error("Invalid queue item id")
        return
    if await conn.run(seek, position, seek_item_id) == STALE_QUEUE_ITEM:
        _log_playback_diagnostic(
            "seek_ignored_stale", conn.user_id, conn.device_id, queue_item_id=seek_item_id
        )
        return
    await conn.broadcast("seek", position_changed=True)


async def _on_position_report(conn: _Connection, msg: dict) -> None:
    """The active device's periodic position save, broadcast like a seek
    when applied."""
    report_item_id = _queue_item_id(msg)
    reported = _optional_position(msg)
    if not isinstance(report_item_id, int) or not isinstance(reported, float):
        await conn.error("Invalid position report")
        return
    device_id = _registered_device_id(conn)
    dropped = await conn.run(report_position, device_id, report_item_id, reported)
    if dropped is not None:
        # A save from a device that is not producing the audio, or for an
        # item that is no longer playing: never applied and never answered
        # (it is sent every few seconds).
        _log_playback_diagnostic(
            "position_report_ignored",
            conn.user_id,
            device_id,
            reason=dropped,
            queue_item_id=report_item_id,
        )
        return
    # report_position counted the revision in its own commit.
    await conn.broadcast("position_report", position_changed=True, revision_counted=True)


async def _on_next(conn: _Connection, _msg: dict) -> None:
    await conn.run(next_track)
    await conn.broadcast("next", position_changed=True, diagnostic_event="next")


async def _on_prev(conn: _Connection, _msg: dict) -> None:
    await conn.run(prev_track)
    await conn.broadcast("prev", position_changed=True)


async def _on_jump_to_index(conn: _Connection, msg: dict) -> None:
    index = msg.get("index")
    if type(index) is not int:
        await conn.error("Invalid queue index")
        return
    await conn.run(jump_to_index, index)
    await conn.broadcast("jump_to_index", position_changed=True)


async def _on_set_shuffle(conn: _Connection, msg: dict) -> None:
    enabled = msg.get("enabled")
    if type(enabled) is not bool:
        await conn.error("Invalid shuffle value")
        return
    await conn.run(set_shuffle, enabled)
    await conn.broadcast("set_shuffle")


async def _on_remove_from_queue(conn: _Connection, msg: dict) -> None:
    queue_item_id = msg.get("queue_item_id")
    if type(queue_item_id) is not int:
        await conn.error("Invalid queue item id")
        return
    error = await conn.run(remove_from_queue, queue_item_id)
    if error:
        await conn.error(error)
        return
    await conn.broadcast("remove_from_queue", position_changed=True)


async def _on_move_queue_item(conn: _Connection, msg: dict) -> None:
    queue_item_id = msg.get("queue_item_id")
    direction = msg.get("direction")
    if type(queue_item_id) is not int or direction not in (-1, 1):
        await conn.error("Invalid move-queue-item command")
        return
    error = await conn.run(move_queue_item, queue_item_id, direction)
    if error:
        await conn.error(error)
        return
    await conn.broadcast("move_queue_item")


async def _on_reorder_queue_item(conn: _Connection, msg: dict) -> None:
    queue_item_id = msg.get("queue_item_id")
    target_index = msg.get("target_index")
    if type(queue_item_id) is not int or type(target_index) is not int:
        await conn.error("Invalid reorder-queue-item command")
        return
    error = await conn.run(reorder_queue_item, queue_item_id, target_index)
    if error:
        await conn.error(error)
        return
    await conn.broadcast("reorder_queue_item")


async def _on_remove_device(conn: _Connection, msg: dict) -> None:
    target_device_id = msg.get("device_id")
    if type(target_device_id) is not int:
        await conn.error("Invalid device id")
        return
    error = await conn.run(remove_device, target_device_id)
    if error:
        await conn.error(error)
        return
    await conn.broadcast("remove_device")


async def _on_transfer_to(conn: _Connection, msg: dict) -> None:
    target_device_id = msg.get("device_id")
    if type(target_device_id) is not int:
        await conn.error("Invalid device id")
        return
    error, before_state, after_state = await conn.run(_transfer_job, target_device_id)
    if error:
        _log_playback_diagnostic(
            "transfer_rejected",
            conn.user_id,
            conn.device_id,
            target_device_id=target_device_id,
            error=error,
            state=before_state,
        )
        await conn.error(error)
        return
    _log_playback_diagnostic(
        "transfer_completed",
        conn.user_id,
        conn.device_id,
        target_device_id=target_device_id,
        before_state=before_state,
        after_state=after_state,
    )
    await conn.broadcast("transfer_to", position_changed=True, diagnostic_event="transfer_to")


# Every message a registered device may send. `register_device` is handled
# before this table; anything else arriving before it is ignored.
_HANDLERS: dict[str, Callable[[_Connection, dict], Awaitable[None]]] = {
    "heartbeat": _on_heartbeat,
    "client_diagnostic": _on_client_diagnostic,
    "set_queue_context": _on_set_queue_context,
    "track_ended": _on_track_ended,
    "set_repeat": _on_set_repeat,
    "add_to_queue": _on_add_to_queue,
    "add_many_to_queue": _on_add_many_to_queue,
    "play": _on_play,
    "pause": _on_pause,
    "seek": _on_seek,
    "position_report": _on_position_report,
    "next": _on_next,
    "prev": _on_prev,
    "jump_to_index": _on_jump_to_index,
    "set_shuffle": _on_set_shuffle,
    "remove_from_queue": _on_remove_from_queue,
    "move_queue_item": _on_move_queue_item,
    "reorder_queue_item": _on_reorder_queue_item,
    "remove_device": _on_remove_device,
    "transfer_to": _on_transfer_to,
}


async def _dispatch(conn: _Connection, msg: object) -> None:
    if not isinstance(msg, dict):
        await conn.error("Message must be an object")
        return
    msg_type = msg.get("type")
    if settings.verbose_logging:
        _log_playback_diagnostic(
            "ws_message",
            conn.user_id,
            conn.device_id,
            message_type=msg_type if isinstance(msg_type, str) else None,
            fields={
                k: v
                for k, v in msg.items()
                if k != "client_token" and isinstance(v, (int, float, bool))
            },
        )
    if msg_type == "register_device":
        await _on_register_device(conn, msg)
        return
    if conn.device_id is None:
        return  # nothing else is meaningful before the device is registered
    handler = _HANDLERS.get(msg_type) if isinstance(msg_type, str) else None
    if handler is None:
        await conn.error(f"Unknown message type: {msg_type}")
        return
    await handler(conn, msg)


# Strong references to running teardowns (the loop only keeps weak ones).
_TEARDOWN_TASKS: set[asyncio.Future] = set()


_REPLACE_FLAP_SECONDS = 3.0


async def _close_replaced(previous: WebSocket, user_id: int, device_id: int) -> None:
    """Best-effort close (4000 "replaced") of a device's replaced socket.
    Never raises and never waits longer than a few seconds."""
    try:
        await asyncio.wait_for(previous.close(code=4000, reason="replaced"), timeout=3)
    except Exception as exc:  # noqa: BLE001 - an already-dead or stalled socket must not matter
        logger.info(
            "ws_replaced_close_failed user_id=%s device_id=%s error_type=%s",
            user_id,
            device_id,
            type(exc).__name__,
        )


class _Dropped(Exception):
    """A broadcast could not deliver to this socket."""


# Sent to a socket a broadcast could not deliver to. Any code but 4000 and
# 4401 makes the client reconnect, which is what a stalled link needs.
UNDELIVERABLE_SOCKET_CLOSE_CODE = 1011


async def _next_message(conn: _Connection) -> object:
    """The socket's next message.

    Raises _Dropped as soon as a broadcast gives up on the socket, and
    TimeoutError when it stays silent (see Liveness). The drop is waited for
    alongside the receive because a stalled connection delivers neither a
    message nor a close, and closing it from the broadcast would wait on the
    same full write buffer the send did: the handler has to be woken some
    other way to unregister the socket and mark the device offline."""
    if conn.dropped.is_set():
        raise _Dropped
    receive = asyncio.ensure_future(conn.websocket.receive_json())
    dropped = asyncio.ensure_future(conn.dropped.wait())
    try:
        await asyncio.wait(
            {receive, dropped},
            timeout=SILENT_SOCKET_CLOSE_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        received = receive.done()
        receive.cancel()
        dropped.cancel()
    if conn.dropped.is_set():
        if received and not receive.cancelled():
            receive.exception()  # retrieved, so the loop does not report it as lost
        raise _Dropped
    if received:
        return receive.result()
    raise TimeoutError


async def _teardown(bind, user_id: int, device_id: int) -> None:
    """Mark a closed socket's device offline and tell the user's other
    devices. Never raises: it runs after the socket is gone."""
    try:
        if await asyncio.to_thread(_in_session, bind, _teardown_job, user_id, device_id):
            await _broadcast_state(
                bind,
                user_id,
                diagnostic_event="device_disconnected",
                source_device_id=device_id,
            )
    except Exception:
        logger.exception(
            "websocket teardown failed user_id=%s device_id=%s", user_id, device_id
        )


@router.websocket("/ws/playback")
async def playback_ws(websocket: WebSocket, db: DbSession) -> None:
    # A refusal is a close frame on an accepted socket: closed before the
    # accept, the handshake fails and a browser only ever sees 1006, so the
    # page could not tell "logged out" from a dropped network.
    # Nothing is read from a refused socket.
    if not origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.accept()
        await websocket.close(code=4403)
        return
    # The request-scoped session is only used for its engine: every query
    # runs in a worker thread with its own session (see above).
    bind = db.get_bind()
    user_id = await asyncio.to_thread(_in_session, bind, _authenticated_user_id, websocket)
    if user_id is None:
        await websocket.accept()
        await websocket.close(code=4401)
        return

    await websocket.accept()
    conn = _Connection(websocket=websocket, bind=bind, user_id=user_id)
    _log_playback_diagnostic(
        "websocket_accepted",
        user_id,
        None,
        ip=client.host if (client := getattr(websocket, "client", None)) else None,
        user_agent=(getattr(websocket, "headers", None) or {}).get("user-agent", "")[:120],
    )
    close_reason = "loop_ended"

    try:
        while True:
            try:
                msg = await _next_message(conn)
            except _Dropped:
                # Closed so the client reconnects and the device is marked
                # offline below; the close itself may never get through.
                close_reason = "undeliverable"
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        websocket.close(
                            code=UNDELIVERABLE_SOCKET_CLOSE_CODE, reason="undeliverable"
                        ),
                        timeout=3,
                    )
                return
            except TimeoutError:
                # Open but silent: the page behind it is gone or frozen (see
                # Liveness). Closed so the device is marked offline below.
                close_reason = "silent"
                _log_playback_event(
                    "websocket_silent_closed",
                    user_id,
                    conn.device_id,
                    silent_seconds=SILENT_SOCKET_CLOSE_SECONDS,
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        websocket.close(code=SILENT_SOCKET_CLOSE_CODE, reason="silent"),
                        timeout=3,
                    )
                return
            except (json.JSONDecodeError, UnicodeDecodeError):
                if conn.device_id is not None:
                    manager.heard(user_id, conn.device_id, websocket)
                await conn.error("Message must be valid JSON")
                continue
            except RuntimeError:
                # The client vanished while a state broadcast was being sent
                # to it, which leaves Starlette's socket state disconnected;
                # receive_json then raises this instead of WebSocketDisconnect.
                # It is an ordinary drop (a locked phone), not a server fault.
                if websocket.client_state == WebSocketState.CONNECTED:
                    raise
                break
            if conn.device_id is not None:
                manager.heard(user_id, conn.device_id, websocket)
            # Sessions can expire, be logged out, or be revoked by a password
            # change/admin deletion while a socket is open.
            if await asyncio.to_thread(
                _in_session, bind, _authenticated_user_id, websocket
            ) is None:
                await websocket.close(code=4401)
                return
            try:
                await _dispatch(conn, msg)
            except _UserGone:
                await websocket.close(code=4401)
                return
    except WebSocketDisconnect as exc:
        close_reason = f"client_disconnected code={exc.code}"
    except BaseException as exc:
        close_reason = f"exception {type(exc).__name__}"
        raise
    finally:
        # Nothing here may raise: that would skip the registry cleanup and
        # leave the device marked connected.  The registry goes first (no
        # database involved), and only the socket that is still registered
        # for the device may mark it offline - a stale socket closing after
        # its replacement connected must not.
        device_id = conn.device_id
        _log_playback_event("websocket_closed", user_id, device_id, reason=close_reason)
        was_registered = (
            device_id is not None and manager.disconnect(user_id, device_id, websocket)
        )
        if was_registered:
            assert device_id is not None  # was_registered requires a device id
            # Shielded: the handler can be cancelled right after the
            # disconnect (server shutdown; the test client does it every
            # time), and the device must still be marked offline and the
            # other devices told.
            task = asyncio.ensure_future(_teardown(bind, user_id, device_id))
            _TEARDOWN_TASKS.add(task)
            task.add_done_callback(_TEARDOWN_TASKS.discard)
            await asyncio.shield(task)
