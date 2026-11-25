import asyncio
import logging
import time
from collections.abc import Callable, Collection

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# How long one device's socket may take to accept a broadcast before it is
# given up on, so one stalled connection cannot hold up the others for long.
BROADCAST_SEND_TIMEOUT_SECONDS = 5.0


class ConnectionManager:
    """Process-local registry of live playback WebSocket connections,
    keyed by user then device. Fine for a single-process dev/small-scale
    deployment; a multi-process production deployment would need this
    backed by pub/sub (e.g. Redis) instead - noted as a known limitation,
    not addressed here."""

    def __init__(self) -> None:
        self._connections: dict[int, dict[int, WebSocket]] = {}
        # The login session (its stored digest) each registered socket
        # authenticated with, so revoking a session can close its sockets.
        self._session_digests: dict[WebSocket, str] = {}
        # When each registered socket was registered (monotonic seconds).
        self._registered_at: dict[tuple[int, int], float] = {}
        # When each registered socket last delivered a message (monotonic
        # seconds; registering counts). A socket can stay open long after
        # the page behind it stopped running: a closed tab whose connection
        # never delivered a close, a frozen background tab whose browser
        # still answers the transport's pings. Only messages prove the app
        # is still there (see `silence`).
        self._last_heard: dict[tuple[int, int], float] = {}
        # Devices whose registered socket a broadcast could not deliver to
        # (see `broadcast`), and how to wake each registered socket's handler
        # when that happens. Both describe the currently registered socket
        # and are forgotten when it is replaced or unregistered.
        self._dropped: set[tuple[int, int]] = set()
        self._on_dropped: dict[tuple[int, int], Callable[[], None]] = {}

    def registered_age(self, user_id: int, device_id: int) -> float | None:
        """Seconds since the device's current socket registered, or None."""
        started = self._registered_at.get((user_id, device_id))
        if started is None or device_id not in self._connections.get(user_id, {}):
            return None
        return time.monotonic() - started

    def heard(self, user_id: int, device_id: int, websocket: WebSocket) -> None:
        """Note that `websocket` just delivered a message, if it is still the
        device's registered socket (a replaced one says nothing about the
        device)."""
        if self._connections.get(user_id, {}).get(device_id) is websocket:
            self._last_heard[(user_id, device_id)] = time.monotonic()

    def silence(self, user_id: int, device_id: int) -> float | None:
        """Seconds since the device's registered socket last delivered a
        message, or None when the device has no registered socket in this
        process (gone, or a `connected` row left from before a restart)."""
        if device_id not in self._connections.get(user_id, {}):
            return None
        heard = self._last_heard.get((user_id, device_id))
        return None if heard is None else time.monotonic() - heard

    def connect(
        self,
        user_id: int,
        device_id: int,
        websocket: WebSocket,
        session_digest: str | None = None,
        on_dropped: Callable[[], None] | None = None,
    ) -> WebSocket | None:
        """Register `websocket` for the device and return the socket it
        replaced (or None). A browser keeps its device id across
        reconnects, so the previous socket may still be registered.
        `on_dropped` is called (on the event loop) when a broadcast gives up
        on this socket; the socket's handler must then close it and
        unregister."""
        previous = self._connections.setdefault(user_id, {}).get(device_id)
        if previous is not None:
            self._session_digests.pop(previous, None)
        now = time.monotonic()
        self._registered_at[(user_id, device_id)] = now
        self._last_heard[(user_id, device_id)] = now
        self._dropped.discard((user_id, device_id))
        if on_dropped is not None:
            self._on_dropped[(user_id, device_id)] = on_dropped
        else:
            self._on_dropped.pop((user_id, device_id), None)
        if session_digest is not None:
            self._session_digests[websocket] = session_digest
        self._connections[user_id][device_id] = websocket
        logger.info(
            "ws_connect user_id=%s device_id=%s live_devices=%s replaced=%s",
            user_id,
            device_id,
            sorted(self._connections[user_id]),
            previous is not None,
        )
        return previous

    def disconnect(self, user_id: int, device_id: int, websocket: WebSocket) -> bool:
        """Unregister `websocket`, but only if it is still the registered
        socket for the device. A late close of a device's old socket must
        not evict the new socket that replaced it. Returns whether an entry
        was removed."""
        conns = self._connections.get(user_id)
        was_registered = bool(conns and conns.get(device_id) is websocket)
        logger.info(
            "ws_disconnect user_id=%s device_id=%s was_registered=%s",
            user_id,
            device_id,
            was_registered,
        )
        if not was_registered:
            return False
        assert conns is not None  # was_registered is only true when the user has connections
        del conns[device_id]
        self._session_digests.pop(websocket, None)
        self._registered_at.pop((user_id, device_id), None)
        self._last_heard.pop((user_id, device_id), None)
        self._dropped.discard((user_id, device_id))
        self._on_dropped.pop((user_id, device_id), None)
        if not conns:
            self._connections.pop(user_id, None)
        return True

    async def broadcast(self, user_id: int, message: dict) -> dict[str, list[int]]:
        """Send `message` to each of the user's registered sockets.

        A socket that fails or does not take the message in time is dropped:
        later broadcasts skip it and its handler is woken (`on_dropped`) to
        close it. It stays registered until that handler unregisters it,
        because only the handler that removes the registry entry marks the
        device offline and tells the other devices. Unregistering it here
        would make the handler skip that, so the device would stay
        "connected" for good; and a socket that had merely stalled would stay
        open without ever receiving state again."""
        conns = list(self._connections.get(user_id, {}).items())
        dead: list[int] = []
        delivered: list[int] = []
        for device_id, ws in conns:
            if (user_id, device_id) in self._dropped:
                continue
            try:
                await asyncio.wait_for(
                    ws.send_json(message), timeout=BROADCAST_SEND_TIMEOUT_SECONDS
                )
                delivered.append(device_id)
            except Exception as exc:  # noqa: BLE001 - a failing or stalled socket is logged and dropped, never fatal to the others
                logger.warning(
                    "playback_diagnostic event=state_send_failed user_id=%s device_id=%s message_type=%s error_type=%s",
                    user_id,
                    device_id,
                    message.get("type"),
                    type(exc).__name__,
                )
                dead.append(device_id)
                self._drop(user_id, device_id, ws)
        return {"delivered_device_ids": delivered, "failed_device_ids": dead}

    def _drop(self, user_id: int, device_id: int, websocket: WebSocket) -> None:
        # Only the socket still registered for the device: one replaced while
        # the send was in flight says nothing about its replacement.
        if self._connections.get(user_id, {}).get(device_id) is not websocket:
            return
        self._dropped.add((user_id, device_id))
        wake = self._on_dropped.get((user_id, device_id))
        if wake is not None:
            wake()

    async def close_sessions(
        self, user_id: int, digests: Collection[str] | None
    ) -> list[int]:
        """Close (4401) the user's sockets that authenticated with one of the
        session `digests`, or all of the user's sockets when `digests` is
        None. Without this a revoked session's socket stays open and keeps
        receiving broadcasts until it happens to send a message (which is
        re-authenticated). The socket's own handler unregisters it. A socket
        not registered yet (no device id) is not tracked; it fails its next
        message's authentication check. Returns the device ids closed."""
        conns = list(self._connections.get(user_id, {}).items())
        closed: list[int] = []
        for device_id, ws in conns:
            if digests is not None and self._session_digests.get(ws) not in digests:
                continue
            try:
                await asyncio.wait_for(ws.close(code=4401), timeout=5)
            except Exception as exc:  # noqa: BLE001 - an already-dead or stalled socket must not stop the others being closed
                logger.warning(
                    "ws_close_failed user_id=%s device_id=%s error_type=%s",
                    user_id,
                    device_id,
                    type(exc).__name__,
                )
            closed.append(device_id)
        return closed


manager = ConnectionManager()


# Called from sync endpoints through BackgroundTasks (account changes in the
# auth and admin routers), so they live here rather than in a router.


async def broadcast_to_user(user_id: int, message: dict) -> None:
    """Push a non-playback event (e.g. an account-preference change) to
    every connected device for a user. Separate from the playback
    router's state broadcast because it carries no playback snapshot."""
    await manager.broadcast(user_id, message)


async def close_user_sessions(user_id: int, digests: Collection[str] | None) -> None:
    """Close the user's playback sockets that belong to the revoked login
    sessions `digests` (all of the user's sockets when None). Schedule it
    from a sync endpoint with BackgroundTasks, like `broadcast_to_user`."""
    await manager.close_sessions(user_id, digests)
