from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.models import LibraryRoot, MediaItem, MediaKind, PlaybackState
from tests.conftest import wait_until


def _make_tracks(db_session, count=3) -> list[int]:
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    ids = []
    for i in range(count):
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"t{i}.mp3",
            title=f"T{i}",
        )
        db_session.add(item)
        db_session.commit()
        ids.append(item.id)
    return ids


def _current_queue_item_id(client) -> int:
    current = client.get("/api/playback/state").json()["current_queue_item_id"]
    assert isinstance(current, int)
    return current


def _end_current_track(client, ws) -> None:
    """The active device's element reached the end of the current queue row."""
    ws.send_json({"type": "track_ended", "queue_item_id": _current_queue_item_id(client)})


def _seek_current(client, ws, seconds: float) -> None:
    ws.send_json(
        {
            "type": "seek",
            "queue_item_id": _current_queue_item_id(client),
            "position_seconds": seconds,
        }
    )


def _set_queue_context(ws, ids: list[int], start_media_item_id: int) -> dict:
    ws.send_json(
        {
            "type": "set_queue_context",
            "context": {"kind": "ids", "ids": ids},
            "start_media_item_id": start_media_item_id,
        }
    )
    return ws.receive_json()


def _close_code_after_accept(ws) -> int:
    """The refusal arrives as a close frame on an accepted socket: a browser
    sees its code (4401, 4403) instead of a failed handshake (1006)."""
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    return exc.value.code


def test_ws_rejects_unauthenticated_connection_with_4401_after_accepting(raw_client):
    # Entering the context manager waits for the accept: had the server
    # closed first, this would raise and a browser would get 1006.
    with raw_client.websocket_connect("/ws/playback") as ws:
        assert _close_code_after_accept(ws) == 4401


def test_ws_rejects_cross_origin_connection_with_4403_after_accepting(client):
    with client.websocket_connect(
        "/ws/playback", headers={"origin": "https://evil.example"}
    ) as ws:
        assert _close_code_after_accept(ws) == 4403


def test_a_refused_socket_never_handles_a_message(raw_client, db_session):
    track_ids = _make_tracks(db_session, count=1)
    with raw_client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.send_json({"type": "add_to_queue", "media_item_id": track_ids[0], "mode": "end"})
        assert _close_code_after_accept(ws) == 4401
    assert db_session.query(PlaybackState).count() == 0


def test_ws_accepts_configured_extra_origin(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allowed_origins", ("http://127.0.0.1:3000",))
    with client.websocket_connect(
        "/ws/playback", headers={"origin": "http://127.0.0.1:3000"}
    ) as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        assert ws.receive_json()["type"] == "registered"


def test_accent_color_change_broadcasts_to_connected_devices(client):
    """Changing the account accent color pushes the new color to every
    already-connected device over the playback WebSocket."""
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Other device"})
        assert ws.receive_json()["type"] == "registered"
        # Consume the post-registration state broadcast.
        assert ws.receive_json()["type"] == "state"

        resp = client.put("/api/auth/accent-color", json={"accent_color": "#10b981"})
        assert resp.status_code == 200
        assert resp.json()["accent_color"] == "#10b981"

        pushed = ws.receive_json()
        assert pushed["type"] == "accent_color_changed"
        assert pushed["accent_color"] == "#10b981"


def test_register_device_gets_registered_then_state(client):
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json(
            {"type": "register_device", "name": "Laptop", "device_type": "web"}
        )
        registered = ws.receive_json()
        assert registered["type"] == "registered"
        device_id = registered["device_id"]

        state_msg = ws.receive_json()
        assert state_msg["type"] == "state"
        assert state_msg["state"]["active_device_id"] == device_id
        assert state_msg["state"]["devices"][0]["name"] == "Laptop"


def test_set_queue_broadcasts_to_second_device(client, db_session):
    track_ids = _make_tracks(db_session)

    with client.websocket_connect("/ws/playback") as ws1:
        ws1.send_json(
            {"type": "register_device", "name": "Laptop", "device_type": "web"}
        )
        ws1.receive_json()  # registered
        ws1.receive_json()  # initial state

        with client.websocket_connect("/ws/playback") as ws2:
            ws2.send_json(
                {"type": "register_device", "name": "Phone", "device_type": "mobile"}
            )
            ws2.receive_json()  # registered
            ws2.receive_json()  # initial state for ws2
            ws1.receive_json()  # ws1 also gets a state update when ws2 registers

            state1 = _set_queue_context(ws1, track_ids, track_ids[0])
            state2 = ws2.receive_json()

    assert state1["type"] == "state"
    assert state1["state"]["media_item_id"] == track_ids[0]
    # The whole point: device 2 sees device 1's action without asking.
    assert state2["state"]["media_item_id"] == track_ids[0]
    assert len(state2["state"]["devices"]) == 2


def test_a_new_queue_can_start_at_a_position_and_paused(client, db_session):
    """A device hands over what it played with no connection: the queue
    starts where it is, playing or paused, in one change."""
    track_ids = _make_tracks(db_session)

    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Phone", "device_type": "mobile"})
        ws.receive_json()
        ws.receive_json()
        message = {
            "type": "set_queue_context",
            "context": {"kind": "ids", "ids": track_ids},
            "start_media_item_id": track_ids[1],
            "start_index": 1,
        }
        ws.send_json({**message, "position_seconds": 42.5})
        state = ws.receive_json()["state"]
        assert (state["media_item_id"], state["position_seconds"], state["is_playing"]) == (
            track_ids[1],
            42.5,
            True,
        )

        ws.send_json({**message, "position_seconds": 7, "paused": True})
        state = ws.receive_json()["state"]
        assert (state["position_seconds"], state["is_playing"]) == (7.0, False)

        for bad in ({"position_seconds": -1}, {"position_seconds": "3"}, {"paused": "yes"}):
            ws.send_json({**message, **bad})
            assert ws.receive_json() == {"type": "error", "message": "Invalid queue"}


def test_inactive_paused_device_play_request_claims_playback(client, db_session):
    with client.websocket_connect("/ws/playback") as first:
        first.send_json({"type": "register_device", "name": "Laptop"})
        first_id = first.receive_json()["device_id"]
        first.receive_json()
        with client.websocket_connect("/ws/playback") as second:
            second.send_json({"type": "register_device", "name": "Phone"})
            second_id = second.receive_json()["device_id"]
            second.receive_json()
            first.receive_json()
            second.send_json({"type": "play"})
            state = second.receive_json()["state"]
    assert first_id != second_id
    assert state["active_device_id"] == second_id


def test_play_creates_missing_playback_state_for_connected_requester(client, db_session):
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        device_id = ws.receive_json()["device_id"]
        ws.receive_json()
        db_session.query(PlaybackState).delete()
        db_session.commit()

        ws.send_json({"type": "play"})
        state = ws.receive_json()["state"]

    assert state["active_device_id"] == device_id
    stored = db_session.get(PlaybackState, 1)
    assert stored is not None
    assert stored.active_device_id == device_id


def test_inactive_device_play_does_not_steal_active_playback(client, db_session):
    track_ids = _make_tracks(db_session)
    with client.websocket_connect("/ws/playback") as first:
        first.send_json({"type": "register_device", "name": "Laptop"})
        first_id = first.receive_json()["device_id"]; first.receive_json()
        first.send_json({"type": "set_queue_context", "context": {"kind": "ids", "ids": track_ids}, "start_media_item_id": track_ids[0]})
        first.receive_json()
        with client.websocket_connect("/ws/playback") as second:
            second.send_json({"type": "register_device", "name": "Phone"})
            second.receive_json(); second.receive_json(); first.receive_json()
            second.send_json({"type": "play"})
            state = second.receive_json()["state"]
            first_state = first.receive_json()["state"]
    assert state["active_device_id"] == first_state["active_device_id"]
    assert state["active_device_id"] == first_id


def test_a_new_queue_from_another_device_claims_output_when_the_active_one_left(client, db_session):
    # A plays and its browser closes without pausing; C starts an album.
    track_ids = _make_tracks(db_session)
    a_cm, a = _open(client)
    a_id = _register_device(a, "tok-a", "Laptop")
    a.receive_json()
    _set_queue_context(a, track_ids, track_ids[0])
    a_cm.__exit__(None, None, None)
    _wait_for_teardowns()
    with client.websocket_connect("/ws/playback") as c:
        c_id = _register_device(c, "tok-c", "Phone")
        assert c.receive_json()["state"]["active_device_id"] == a_id  # a reconnect moves nothing
        state = _set_queue_context(c, track_ids, track_ids[1])["state"]
        assert state["active_device_id"] == c_id
        assert state["is_playing"] is True
        assert state["media_item_id"] == track_ids[1]
        # A comes back: it is not made active again, and is told C is.
        with client.websocket_connect("/ws/playback") as a_again:
            assert _register_device(a_again, "tok-a", "Laptop") == a_id
            assert a_again.receive_json()["state"]["active_device_id"] == c_id


def test_a_new_queue_from_a_remote_does_not_take_over_a_connected_active_device(client, db_session):
    track_ids = _make_tracks(db_session)
    with client.websocket_connect("/ws/playback") as a:
        a_id = _register_device(a, "tok-a", "Laptop")
        a.receive_json()
        _set_queue_context(a, track_ids, track_ids[0])
        with client.websocket_connect("/ws/playback") as c:
            _register_device(c, "tok-c", "Phone")
            c.receive_json()
            a.receive_json()
            state = _set_queue_context(c, track_ids, track_ids[1])["state"]
    assert state["active_device_id"] == a_id
    assert state["media_item_id"] == track_ids[1]


def test_play_from_another_device_claims_output_when_the_active_one_left(client, db_session):
    track_ids = _make_tracks(db_session)
    a_cm, a = _open(client)
    a_id = _register_device(a, "tok-a", "Laptop")
    a.receive_json()
    _set_queue_context(a, track_ids, track_ids[0])
    a_cm.__exit__(None, None, None)
    _wait_for_teardowns()
    with client.websocket_connect("/ws/playback") as c:
        c_id = _register_device(c, "tok-c", "Phone")
        assert c.receive_json()["state"]["active_device_id"] == a_id
        c.send_json({"type": "play"})
        state = c.receive_json()["state"]
    assert state["active_device_id"] == c_id
    assert state["is_playing"] is True


def test_transfer_to_updates_active_device_for_both(client, db_session):
    with client.websocket_connect("/ws/playback") as ws1:
        ws1.send_json(
            {"type": "register_device", "name": "Laptop", "device_type": "web"}
        )
        reg1 = ws1.receive_json()
        device1_id = reg1["device_id"]
        ws1.receive_json()  # state

        with client.websocket_connect("/ws/playback") as ws2:
            ws2.send_json(
                {"type": "register_device", "name": "Phone", "device_type": "mobile"}
            )
            reg2 = ws2.receive_json()
            device2_id = reg2["device_id"]
            ws2.receive_json()  # state
            ws1.receive_json()  # state update from ws2 registering

            ws1.send_json({"type": "transfer_to", "device_id": device2_id})
            state1 = ws1.receive_json()
            state2 = ws2.receive_json()

    assert state1["state"]["active_device_id"] == device2_id
    assert state2["state"]["active_device_id"] == device2_id
    assert device1_id != device2_id


def test_device_handoff_and_natural_end_emit_playback_diagnostics(
    client, db_session, caplog, monkeypatch
):
    from app.config import settings

    # The trail of what played where is verbose-only.
    monkeypatch.setattr(settings, "verbose_logging", True)
    track_ids = _make_tracks(db_session, count=2)
    caplog.set_level(logging.INFO, logger="app.routers.playback")

    with client.websocket_connect("/ws/playback") as desktop:
        desktop.send_json({"type": "register_device", "name": "Desktop"})
        desktop_id = desktop.receive_json()["device_id"]
        desktop.receive_json()

        with client.websocket_connect("/ws/playback") as phone:
            phone.send_json(
                {
                    "type": "register_device",
                    "name": "Phone",
                    "device_type": "mobile",
                }
            )
            phone_id = phone.receive_json()["device_id"]
            phone.receive_json()
            desktop.receive_json()

            _set_queue_context(desktop, track_ids, track_ids[0])
            phone.receive_json()

            desktop.send_json({"type": "transfer_to", "device_id": phone_id})
            desktop.receive_json()
            phone.receive_json()

            phone.send_json(
                {
                    "type": "client_diagnostic",
                    "event": "media_ended",
                    "media_item_id": track_ids[0],
                    "current_time": 181.25,
                    "duration": 181.25,
                    "paused": True,
                    "ready_state": 4,
                    "visibility": "visible",
                    "is_active_device": True,
                }
            )
            _end_current_track(client, phone)
            phone_state = phone.receive_json()["state"]
            desktop_state = desktop.receive_json()["state"]

    assert phone_state["media_item_id"] == track_ids[1]
    assert desktop_state["media_item_id"] == track_ids[1]
    assert '"event": "transfer_completed"' in caplog.text
    assert f'"target_device_id": {phone_id}' in caplog.text
    assert '"event": "client_media_ended"' in caplog.text
    assert f'"media_item_id": {track_ids[0]}' in caplog.text
    assert '"event": "track_ended_received"' in caplog.text
    assert '"event": "track_ended_processed"' in caplog.text
    assert f'"media_item_id": {track_ids[1]}' in caplog.text
    assert f'"delivered_device_ids": [{desktop_id}, {phone_id}]' in caplog.text


def test_disconnect_broadcasts_updated_device_list(client, db_session):
    with client.websocket_connect("/ws/playback") as ws1:
        ws1.send_json(
            {"type": "register_device", "name": "Laptop", "device_type": "web"}
        )
        ws1.receive_json()
        ws1.receive_json()

        with client.websocket_connect("/ws/playback") as ws2:
            ws2.send_json(
                {"type": "register_device", "name": "Phone", "device_type": "mobile"}
            )
            ws2.receive_json()
            ws2.receive_json()
            ws1.receive_json()  # ws1 sees ws2 join

        # ws2's context manager exit marks it offline.  Its stable device row
        # remains so a backgrounded phone can reconnect without handing the
        # active output to the laptop.
        after_disconnect = ws1.receive_json()

    assert after_disconnect["type"] == "state"
    devices = {device["name"]: device for device in after_disconnect["state"]["devices"]}
    assert devices["Laptop"]["is_connected"] is True
    assert devices["Phone"]["is_connected"] is False


def test_unknown_message_type_returns_error(client):
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()

        ws.send_json({"type": "not_a_real_command"})
        resp = ws.receive_json()
    assert resp["type"] == "error"


def test_malformed_commands_return_errors_without_dropping_connection(client):
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()

        for command in (
            {"type": "set_queue_context", "context": "not-a-dict"},
            {
                "type": "set_queue_context",
                "context": {"kind": "bogus"},
                "start_media_item_id": 1,
            },
            {"type": "seek", "position_seconds": "not-a-number"},
            {"type": "transfer_to", "device_id": "wrong"},
            {"type": "jump_to_index", "index": "not-an-int"},
            {"type": "set_shuffle", "enabled": "not-a-bool"},
            {"type": "set_repeat", "mode": 123},
            {"type": "remove_from_queue", "queue_item_id": "wrong"},
            {"type": "move_queue_item", "queue_item_id": 1, "direction": 5},
            {"type": "reorder_queue_item", "queue_item_id": "wrong", "target_index": 0},
            {"type": "remove_device", "device_id": "wrong"},
        ):
            ws.send_json(command)
            assert ws.receive_json()["type"] == "error"

        ws.send_text("{not-json")
        assert ws.receive_json()["type"] == "error"

        ws.send_json({"type": "heartbeat"})
        # Heartbeats are acknowledged to the sender, not broadcast.
        assert ws.receive_json() == {"type": "heartbeat_ack"}


def test_inactive_device_cannot_advance_playback(client, db_session):
    track_ids = _make_tracks(db_session, count=2)
    with client.websocket_connect("/ws/playback") as active:
        active.send_json({"type": "register_device", "name": "Laptop"})
        active.receive_json()
        active.receive_json()

        with client.websocket_connect("/ws/playback") as inactive:
            inactive.send_json({"type": "register_device", "name": "Phone"})
            inactive.receive_json()
            inactive.receive_json()
            active.receive_json()

            _set_queue_context(active, track_ids, track_ids[0])
            inactive.receive_json()
            _end_current_track(client, inactive)
            assert inactive.receive_json() == {
                "type": "error",
                "message": "Only the active device can end a track",
            }

    assert client.get("/api/playback/state").json()["media_item_id"] == track_ids[0]


def test_add_to_queue_command_broadcasts_updated_state(client, db_session):
    track_ids = _make_tracks(db_session, count=2)
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()
        _set_queue_context(ws, [track_ids[0]], track_ids[0])
        ws.send_json(
            {"type": "add_to_queue", "media_item_id": track_ids[1], "mode": "end"}
        )
        state = ws.receive_json()

    assert state["position_changed"] is False
    assert state["state"]["queue_media_item_ids"] == track_ids


def test_add_many_to_queue_is_one_change_one_broadcast_one_revision(client, db_session):
    track_ids = _make_tracks(db_session, count=4)
    a_cm, laptop, _ = _connect_registered(client, "Laptop")
    b_cm, phone, _ = _connect_registered(client, "Phone")
    try:
        laptop.receive_json()  # the laptop sees the phone join
        first = _set_queue_context(laptop, [track_ids[0]], track_ids[0])["state"]
        phone.receive_json()
        laptop.send_json(
            {"type": "add_many_to_queue", "media_item_ids": track_ids[1:], "mode": "next"}
        )
        sent = laptop.receive_json()
        seen = phone.receive_json()
        _round_trip(laptop)  # nothing else was broadcast in between
        _round_trip(phone)
    finally:
        b_cm.__exit__(None, None, None)
        a_cm.__exit__(None, None, None)
    for message in (sent, seen):
        assert message["type"] == "state"
        assert message["source_command"] == "add_many_to_queue"
        assert message["position_changed"] is False
        assert message["state"]["queue_media_item_ids"] == track_ids
        assert message["state"]["revision"] == first["revision"] + 1
        assert message["state"]["current_queue_item_id"] == first["current_queue_item_id"]


def test_add_many_to_queue_requires_its_fields_and_changes_nothing_when_refused(
    client, db_session
):
    from app.playback import MAX_EXPLICIT_IDS

    track_ids = _make_tracks(db_session, count=2)
    cm, ws, _ = _connect_registered(client)
    try:
        before = _set_queue_context(ws, [track_ids[0]], track_ids[0])["state"]
        invalid = "Invalid add-to-queue command"
        for command, message in (
            ({"type": "add_many_to_queue", "media_item_ids": [track_ids[1]]}, invalid),
            ({"type": "add_many_to_queue", "mode": "end"}, invalid),
            ({"type": "add_many_to_queue", "media_item_ids": [], "mode": "end"}, invalid),
            ({"type": "add_many_to_queue", "media_item_ids": track_ids[1], "mode": "end"}, invalid),
            ({"type": "add_many_to_queue", "media_item_ids": [track_ids[1], "2"], "mode": "end"}, invalid),
            ({"type": "add_many_to_queue", "media_item_ids": [True], "mode": "end"}, invalid),
            ({"type": "add_many_to_queue", "media_item_ids": [track_ids[1]], "mode": "last"}, invalid),
            ({"type": "add_many_to_queue", "media_item_ids": [track_ids[1]], "mode": None}, invalid),
            (
                {"type": "add_many_to_queue", "media_item_ids": [track_ids[1], 99999], "mode": "end"},
                "Media item 99999 not found",
            ),
            (
                {
                    "type": "add_many_to_queue",
                    "media_item_ids": [track_ids[1]] * (MAX_EXPLICIT_IDS + 1),
                    "mode": "end",
                },
                "Too many items to queue at once",
            ),
        ):
            ws.send_json(command)
            assert ws.receive_json() == {"type": "error", "message": message}, command
        _round_trip(ws)
        after = client.get("/api/playback/state").json()
    finally:
        cm.__exit__(None, None, None)
    for key in ("queue_media_item_ids", "queue_item_ids", "revision", "current_queue_item_id"):
        assert after[key] == before[key], key


def test_state_broadcast_names_the_device_and_command_that_caused_it(client, db_session):
    track_ids = _make_tracks(db_session, count=1)
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Phone"})
        device_id = ws.receive_json()["device_id"]
        ws.receive_json()
        _set_queue_context(ws, track_ids, track_ids[0])
        _seek_current(client, ws, 12.5)
        state = ws.receive_json()

    assert state["position_changed"] is True
    assert state["source_device_id"] == device_id
    assert state["source_command"] == "seek"


def test_threaded_playback_job_reports_deleted_user(db_session):
    from app.routers.playback import _run_playback_job

    assert _run_playback_job(db_session.get_bind(), 999_999, lambda *_args: None) == (
        "User not found"
    )


def test_set_queue_context_windows_a_large_library_instead_of_materializing_it(
    client, db_session
):
    # Playing from the Songs tab does not send or store every matching id
    # at once (an id cap would reject real libraries, and it would mean one
    # DB row per track). The windowed context handles a library far bigger
    # than the materialized window.
    track_ids = _make_tracks(db_session, count=1500)
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()

        ws.send_json(
            {
                "type": "set_queue_context",
                "context": {"kind": "library", "filters": {}},
                "start_media_item_id": track_ids[500],
            }
        )
        msg = ws.receive_json()
        assert msg["type"] == "state"
        state = msg["state"]
        assert state["media_item_id"] == track_ids[500]
        assert state["queue_context_count"] == 1500
        # The whole point: the materialized queue is a bounded window, not
        # all 1500 tracks.
        assert 0 < len(state["queue_media_item_ids"]) < 300


def test_open_socket_is_closed_after_session_logout(client):
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()
        assert client.post("/api/auth/logout").status_code == 200
        ws.send_json({"type": "heartbeat"})
        try:
            ws.receive_json()
        except WebSocketDisconnect as exc:
            assert exc.code == 4401
        else:
            raise AssertionError("WebSocket remained open after logout")


def test_rest_state_endpoint_matches_ws_state(client, db_session):
    track_ids = _make_tracks(db_session)
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()
        _set_queue_context(ws, track_ids, track_ids[0])

    rest_state = client.get("/api/playback/state").json()
    assert rest_state["media_item_id"] == track_ids[0]


def test_ws_shuffle_and_queue_management_round_trip(client, db_session):
    track_ids = _make_tracks(db_session, count=4)
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()  # registered
        ws.receive_json()  # initial state

        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]
        assert state["is_shuffled"] is False
        assert state["media_item_id"] == track_ids[0]

        ws.send_json({"type": "set_shuffle", "enabled": True})
        state = ws.receive_json()["state"]
        assert state["is_shuffled"] is True
        assert state["media_item_id"] == track_ids[0]  # unchanged by shuffling ahead
        # The currently-playing track must not also appear again in its own
        # freshly-shuffled upcoming tail (see _context_with_exclude).
        assert state["queue_media_item_ids"].count(track_ids[0]) == 1
        assert set(state["queue_media_item_ids"]) == set(track_ids)
        queue_item_ids = state["queue_item_ids"]

        ws.send_json({"type": "jump_to_index", "index": 2})
        state = ws.receive_json()["state"]
        assert state["queue_index"] == 2

        ws.send_json(
            {
                "type": "move_queue_item",
                "queue_item_id": queue_item_ids[0],
                "direction": 1,
            }
        )
        state = ws.receive_json()["state"]
        assert set(state["queue_media_item_ids"]) == set(track_ids)

        ws.send_json({"type": "remove_from_queue", "queue_item_id": queue_item_ids[-1]})
        state = ws.receive_json()["state"]
        assert len(state["queue_media_item_ids"]) == 3

        ws.send_json({"type": "set_shuffle", "enabled": False})
        state = ws.receive_json()["state"]
        assert state["is_shuffled"] is False

        ws.send_json(
            {
                "type": "reorder_queue_item",
                "queue_item_id": state["queue_item_ids"][-1],
                "target_index": 0,
            }
        )
        state = ws.receive_json()["state"]
        assert state["queue_media_item_ids"][0] in track_ids


def test_repeat_one_replays_same_track_without_advancing(client, db_session):
    track_ids = _make_tracks(db_session, count=3)
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()
        _set_queue_context(ws, track_ids, track_ids[0])

        ws.send_json({"type": "set_repeat", "mode": "one"})
        state = ws.receive_json()["state"]
        assert state["repeat_mode"] == "one"

        _end_current_track(client, ws)
        state = ws.receive_json()["state"]
        assert state["media_item_id"] == track_ids[0]
        assert state["position_seconds"] == 0.0
        assert state["is_playing"] is True

        # A manual skip still advances even with repeat-one on.
        ws.send_json({"type": "next"})
        state = ws.receive_json()["state"]
        assert state["media_item_id"] == track_ids[1]


def test_repeat_all_loops_context_from_the_start(client, db_session):
    track_ids = _make_tracks(db_session, count=2)
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()
        _set_queue_context(ws, track_ids, track_ids[0])

        ws.send_json({"type": "set_repeat", "mode": "all"})
        ws.receive_json()

        _end_current_track(client, ws)  # -> track_ids[1]
        state = ws.receive_json()["state"]
        assert state["media_item_id"] == track_ids[1]

        _end_current_track(client, ws)  # end of context, loops
        state = ws.receive_json()["state"]
        assert state["media_item_id"] == track_ids[0]


def test_repeat_off_autoplays_when_context_runs_out(client, db_session):
    # No artist set on these fixture tracks, so similar_tracks_for_seed
    # can't find anything (no network call is made) and there's no
    # same-artist fallback either - autoplay should still fall back to
    # whatever else is in the library rather than just stopping.
    track_ids = _make_tracks(db_session, count=2)
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()
        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]
        assert state["repeat_mode"] == "off"

        _end_current_track(client, ws)
        state = ws.receive_json()["state"]
        assert state["media_item_id"] == track_ids[1]

        _end_current_track(client, ws)
        state = ws.receive_json()["state"]
        assert (
            state["media_item_id"] == track_ids[0]
        )  # random-library fallback kept it going


def test_repeat_off_stops_only_when_nothing_at_all_is_left(client, db_session):
    # A single track, alone in the whole library - no similar-artist data,
    # no same-artist fallback, and no other track anywhere to fall back to.
    # This is the one case autoplay can't do anything about.
    track_ids = _make_tracks(db_session, count=1)
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()
        _set_queue_context(ws, track_ids, track_ids[0])

        _end_current_track(client, ws)
        state = ws.receive_json()["state"]
        assert (
            state["media_item_id"] == track_ids[0]
        )  # nothing anywhere to continue with
        # ...and it stops there instead of restarting the track forever.
        assert state["is_playing"] is False


def test_repeat_off_falls_back_to_same_artist_when_nothing_similar(client, db_session):
    # No genre/cached similar-artist data for "Queen" here, but the library
    # has another track by the same artist - that should be preferred over
    # the fully-random fallback.
    track_ids = _make_tracks_with_artist(db_session, "Queen", count=2)
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()
        _set_queue_context(ws, [track_ids[0]], track_ids[0])

        _end_current_track(client, ws)
        state = ws.receive_json()["state"]
        assert state["media_item_id"] == track_ids[1]  # the other Queen track


def _make_tracks_with_artist(db_session, artist, count=2) -> list[int]:
    root = LibraryRoot(path="/fake", display_name="Fake")
    db_session.add(root)
    db_session.commit()
    ids = []
    for i in range(count):
        item = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"{artist}-{i}.mp3",
            title=f"{artist} T{i}",
            artist=artist,
        )
        db_session.add(item)
        db_session.commit()
        ids.append(item.id)
    return ids


def test_repeat_off_autoplay_never_calls_external_apis_itself(client, db_session):
    # The end-of-queue autoplay continuation must only
    # ever use already-cached similar-artist data or a local same-artist/
    # random fallback - a live similar-artist call should only
    # ever happen from an explicit user refresh (Discovery/Explore/
    # "Generate playlist from this song") or the scheduled daily job,
    # never as a side effect of ordinary playback (both for responsiveness
    # and so this app doesn't hammer either API's rate limits). This
    # artist has a real name and *would* resolve to candidates if a live
    # call were allowed - it must not be attempted.
    track_ids = _make_tracks_with_artist(db_session, "Queen", count=2)
    with (
        patch("app.discovery.similar.listenbrainz.similar_artists") as mock_similar,
        client.websocket_connect("/ws/playback") as ws,
    ):
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()
        ws.receive_json()
        _set_queue_context(ws, track_ids, track_ids[0])

        _end_current_track(client, ws)
        ws.receive_json()
        _end_current_track(client, ws)
        state = ws.receive_json()["state"]

        # No genre/cached similar-artist match, but the same-artist local
        # fallback finds the other Queen track without ever touching the
        # (mocked) network.
        assert state["media_item_id"] == track_ids[0]
        mock_similar.assert_not_called()


def test_ws_remove_device_drops_other_device_from_list(client, db_session):
    with (
        client.websocket_connect("/ws/playback") as first,
        client.websocket_connect("/ws/playback") as second,
    ):
        first.send_json({"type": "register_device", "name": "Laptop"})
        first.receive_json()  # registered
        first.receive_json()  # state

        second.send_json({"type": "register_device", "name": "Phone"})
        second_device_id = second.receive_json()["device_id"]
        second.receive_json()  # state
        first.receive_json()  # state re-broadcast now that a 2nd device joined

        first.send_json({"type": "remove_device", "device_id": second_device_id})
        state = first.receive_json()["state"]
        assert len(state["devices"]) == 1
        assert state["devices"][0]["name"] == "Laptop"


def test_client_media_error_is_logged_with_only_safe_fields(
    client, db_session, caplog, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "verbose_logging", True)
    caplog.set_level(logging.INFO, logger="app.routers.playback")
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Phone", "device_type": "mobile"})
        ws.receive_json()
        ws.receive_json()
        ws.send_json(
            {
                "type": "client_diagnostic",
                "event": "media_error",
                "source": "video_hls",
                "media_item_id": 7,
                "hls_type": "mediaError",
                "hls_details": "bufferAppendError",
                "fatal": True,
                "http_status": 404,
                "current_time": 12.5,
                "buffered_end": "not a number",
                "recovery": "free text with spaces <script>",
            }
        )
        # A heartbeat round-trips, proving the diagnostic was processed first.
        ws.send_json({"type": "heartbeat"})
        assert ws.receive_json() == {"type": "heartbeat_ack"}

    assert '"event": "client_media_error"' in caplog.text
    assert '"hls_details": "bufferAppendError"' in caplog.text
    assert '"http_status": 404' in caplog.text
    assert '"media_item_id": 7' in caplog.text
    assert '"current_time": 12.5' in caplog.text
    assert '"buffered_end": null' in caplog.text
    assert "<script>" not in caplog.text


def test_audio_settings_change_broadcasts_to_connected_devices(client):
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Other device"})
        assert ws.receive_json()["type"] == "registered"
        assert ws.receive_json()["type"] == "state"

        resp = client.put(
            "/api/auth/audio-settings",
            json={"volume_leveling": True, "eq_preset": "rock", "eq_gains": [1] * 10},
        )
        assert resp.status_code == 200

        pushed = ws.receive_json()
        assert pushed == {
            "type": "audio_settings_changed",
            "volume_leveling": True,
            "eq_preset": "rock",
            "eq_gains": [1.0] * 10,
        }


# --- A device's old socket must not evict its new one ----------------------


def _register_device(ws, token, name="Phone"):
    ws.send_json(
        {
            "type": "register_device",
            "name": name,
            "device_type": "web",
            "client_token": token,
        }
    )
    while True:
        msg = ws.receive_json()
        if msg.get("type") == "registered":
            return msg["device_id"]


def _wait_for_teardowns() -> None:
    """A closed socket's teardown runs as its own task (it must survive the
    handler being cancelled); wait for it before asserting on its effects."""
    import time

    from app.routers.playback import _TEARDOWN_TASKS

    deadline = time.monotonic() + 10
    while _TEARDOWN_TASKS and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _TEARDOWN_TASKS


def _open(client):
    cm = client.websocket_connect("/ws/playback")
    return cm, cm.__enter__()


def test_old_socket_close_does_not_evict_new_socket(client, session_factory):
    from app.models import Device
    from app.ws_manager import manager

    old_cm, old_ws = _open(client)
    d1 = _register_device(old_ws, "tok-1")
    new_cm, new_ws = _open(client)
    d2 = _register_device(new_ws, "tok-1")
    assert d1 == d2
    # Drain the registration broadcast so the next state is from the command.
    new_ws.receive_json()

    old_cm.__exit__(None, None, None)  # the stale socket finally goes away

    try:
        registered = next(iter(manager._connections.values()))
        assert len(registered) == 1
        with session_factory() as db:
            assert db.get(Device, d2).is_connected is True
        new_ws.send_json({"type": "pause", "position_seconds": None})
        assert new_ws.receive_json()["type"] == "state"
    finally:
        new_cm.__exit__(None, None, None)
    assert manager._connections == {}


def _receive_until_closed(ws) -> int:
    """Drain `ws` until the server closes it; return the close code."""
    from starlette.websockets import WebSocketDisconnect

    for _ in range(20):
        try:
            ws.receive_json()
        except WebSocketDisconnect as exc:
            return exc.code
    raise AssertionError("socket was not closed")


def test_replaced_socket_is_closed_with_4000_and_new_socket_stays_live(
    client, session_factory, monkeypatch
):
    # Two tabs of one browser share a device token. The client treats 4000 as
    # final, so the server closes the replaced socket instead of leaving it
    # registered but deaf. Flap guard disabled here (see the guard test).
    from app.models import Device

    monkeypatch.setattr("app.routers.playback._REPLACE_FLAP_SECONDS", 0.0)
    old_cm, old_ws = _open(client)
    d1 = _register_device(old_ws, "tok-2")
    new_cm, new_ws = _open(client)
    d2 = _register_device(new_ws, "tok-2")
    assert d1 == d2
    try:
        assert _receive_until_closed(old_ws) == 4000
        old_cm.__exit__(None, None, None)  # the old handler's teardown runs
        _wait_for_teardowns()
        with session_factory() as db:
            assert db.get(Device, d2).is_connected is True
        new_ws.send_json({"type": "pause", "position_seconds": None})
        for _ in range(10):
            msg = new_ws.receive_json()
            assert msg.get("type") != "device_disconnected"
            if msg["type"] == "state":
                assert any(d["is_connected"] for d in msg["state"]["devices"])
                break
        else:
            raise AssertionError("new socket received no state")
    finally:
        new_cm.__exit__(None, None, None)


def test_a_failing_close_of_the_replaced_socket_does_not_break_registration(
    client, monkeypatch
):
    monkeypatch.setattr("app.routers.playback._REPLACE_FLAP_SECONDS", 0.0)
    old_cm, old_ws = _open(client)
    _register_device(old_ws, "tok-3")
    new_cm, new_ws = _open(client)

    async def boom(self, code=1000, reason=None):
        raise RuntimeError("close failed")

    from starlette.websockets import WebSocket as StarletteWebSocket

    with patch.object(StarletteWebSocket, "close", boom):
        _register_device(new_ws, "tok-3")
        import time

        from app.routers.playback import _TEARDOWN_TASKS

        deadline = time.monotonic() + 10
        while _TEARDOWN_TASKS and time.monotonic() < deadline:
            time.sleep(0.01)
    try:
        new_ws.send_json({"type": "pause", "position_seconds": None})
        for _ in range(10):
            if new_ws.receive_json()["type"] == "state":
                break
        else:
            raise AssertionError("new socket received no state")
    finally:
        new_cm.__exit__(None, None, None)
        old_cm.__exit__(None, None, None)


def test_flap_guard_leaves_a_just_registered_socket_open(client, caplog):
    # A socket registered moments ago is not closed by its replacement: a
    # stale client bundle that reconnects after any close would loop.
    caplog.set_level(logging.INFO, logger="app.routers.playback")
    old_cm, old_ws = _open(client)
    _register_device(old_ws, "tok-4")
    new_cm, new_ws = _open(client)
    _register_device(new_ws, "tok-4")
    try:
        _wait_for_teardowns()
        old_ws.send_json({"type": "bogus"})
        for _ in range(10):
            if old_ws.receive_json()["type"] == "error":
                break
        else:
            raise AssertionError("replaced socket was closed despite the flap guard")
        assert "ws_replaced_close_skipped" in caplog.text
    finally:
        new_cm.__exit__(None, None, None)
        old_cm.__exit__(None, None, None)


def test_registering_another_device_does_not_close_existing_sockets(client, monkeypatch):
    monkeypatch.setattr("app.routers.playback._REPLACE_FLAP_SECONDS", 0.0)
    cm1, ws1 = _open(client)
    _register_device(ws1, "tok-x", "Laptop")
    cm2, ws2 = _open(client)
    _register_device(ws2, "tok-y", "Phone")
    try:
        _wait_for_teardowns()
        ws1.send_json({"type": "bogus"})
        for _ in range(10):
            if ws1.receive_json()["type"] == "error":
                break
        else:
            raise AssertionError("an unrelated device's socket was closed")
    finally:
        cm2.__exit__(None, None, None)
        cm1.__exit__(None, None, None)


def test_different_devices_still_disconnect_independently(client, session_factory):
    from app.models import Device

    cm1, ws1 = _open(client)
    d1 = _register_device(ws1, "tok-a", "Laptop")
    ws1.receive_json()
    cm2, ws2 = _open(client)
    d2 = _register_device(ws2, "tok-b", "Phone")
    ws2.receive_json()
    ws1.receive_json()
    assert d1 != d2

    cm2.__exit__(None, None, None)
    msg = ws1.receive_json()
    devices = {d["name"]: d for d in msg["state"]["devices"]}
    assert devices["Laptop"]["is_connected"] is True
    assert devices["Phone"]["is_connected"] is False
    with session_factory() as db:
        assert db.get(Device, d1).is_connected is True
        assert db.get(Device, d2).is_connected is False
    cm1.__exit__(None, None, None)


def test_teardown_survives_deleted_user(client, session_factory, caplog):
    from app.cleanup import delete_user_records
    from app.models import User
    from app.ws_manager import manager

    cm, ws = _open(client)
    _register_device(ws, "tok-del")
    with session_factory() as db:
        user_id = db.query(User).one().id
        delete_user_records(db, user_id)
        db.commit()
    with caplog.at_level("ERROR"):
        cm.__exit__(None, None, None)  # must not raise
        _wait_for_teardowns()
    assert manager._connections.get(user_id) is None
    # An expected case, not a failure: nothing to broadcast, nothing logged.
    assert "websocket teardown failed" not in caplog.text


def test_teardown_failure_is_logged_and_registry_still_cleaned(client, caplog):
    from app.ws_manager import manager

    cm, ws = _open(client)
    _register_device(ws, "tok-fail")
    with patch(
        "app.routers.playback.disconnect_device_for_user_id",
        side_effect=RuntimeError("db gone"),
    ):
        cm.__exit__(None, None, None)  # must not raise
        _wait_for_teardowns()
    assert manager._connections == {}
    assert "websocket teardown failed" in caplog.text


# --- Messages name the queue item they refer to ------------------------------


def _connect_registered(client, name="Laptop"):
    cm = client.websocket_connect("/ws/playback")
    ws = cm.__enter__()
    ws.send_json({"type": "register_device", "name": name})
    device_id = ws.receive_json()["device_id"]
    ws.receive_json()  # the registration broadcast
    return cm, ws, device_id


def _round_trip(ws) -> None:
    """Everything sent before this has been processed once its error comes
    back (messages on one socket are handled in order)."""
    ws.send_json({"type": "bogus_round_trip"})
    while ws.receive_json()["type"] != "error":
        pass


def test_duplicate_track_ended_for_the_same_item_advances_once(client, db_session):
    track_ids = _make_tracks(db_session, count=3)
    cm, ws, _ = _connect_registered(client)
    try:
        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]
        ended_item = state["current_queue_item_id"]
        assert ended_item == state["queue_item_ids"][0]

        ws.send_json({"type": "track_ended", "queue_item_id": ended_item})
        advanced = ws.receive_json()["state"]
        assert advanced["media_item_id"] == track_ids[1]
        assert advanced["revision"] == state["revision"] + 1

        # The replay after a reconnect: nothing moves, the sender converges.
        ws.send_json({"type": "track_ended", "queue_item_id": ended_item})
        reply = ws.receive_json()
        assert reply["type"] == "state"
        assert reply["position_changed"] is False
        assert reply["state"]["media_item_id"] == track_ids[1]
        assert reply["state"]["revision"] == advanced["revision"]
    finally:
        cm.__exit__(None, None, None)
    assert client.get("/api/playback/state").json()["media_item_id"] == track_ids[1]


def test_track_ended_for_a_non_current_item_is_ignored_and_answered(
    client, db_session, caplog, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "verbose_logging", True)
    caplog.set_level(logging.INFO, logger="app.routers.playback")
    track_ids = _make_tracks(db_session, count=3)
    cm, ws, _ = _connect_registered(client)
    try:
        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]
        ws.send_json({"type": "track_ended", "queue_item_id": state["queue_item_ids"][2]})
        reply = ws.receive_json()
    finally:
        cm.__exit__(None, None, None)
    assert reply["type"] == "state"
    assert reply["state"]["media_item_id"] == track_ids[0]
    assert reply["state"]["revision"] == state["revision"]
    assert '"event": "track_ended_ignored_stale"' in caplog.text
    assert client.get("/api/playback/state").json()["media_item_id"] == track_ids[0]


def test_next_on_another_device_and_a_late_track_end_skip_nothing(client, db_session):
    track_ids = _make_tracks(db_session, count=3)
    a_cm, a, _ = _connect_registered(client, "Laptop")
    b_cm, b, _ = _connect_registered(client, "Phone")
    try:
        a.receive_json()  # A sees B join
        state = _set_queue_context(a, track_ids, track_ids[0])["state"]
        b.receive_json()
        ending_item = state["current_queue_item_id"]

        b.send_json({"type": "next"})
        assert b.receive_json()["state"]["media_item_id"] == track_ids[1]
        assert a.receive_json()["state"]["media_item_id"] == track_ids[1]

        # A's element reached the end of track 0 at the same moment.
        a.send_json({"type": "track_ended", "queue_item_id": ending_item})
        reply = a.receive_json()
        assert reply["state"]["media_item_id"] == track_ids[1]
        _round_trip(b)  # B got no broadcast for the ignored end
    finally:
        b_cm.__exit__(None, None, None)
        a_cm.__exit__(None, None, None)
    assert client.get("/api/playback/state").json()["media_item_id"] == track_ids[1]


def test_stale_position_saves_never_land_on_the_next_track(client, db_session):
    track_ids = _make_tracks(db_session, count=3)
    cm, ws, _ = _connect_registered(client)
    try:
        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]
        old_item = state["current_queue_item_id"]
        ws.send_json({"type": "next"})
        moved = ws.receive_json()["state"]

        ws.send_json({"type": "position_report", "queue_item_id": old_item, "position_seconds": 181.0})
        ws.send_json({"type": "seek", "queue_item_id": old_item, "position_seconds": 175.0})
        _round_trip(ws)  # neither was broadcast
        rest = client.get("/api/playback/state").json()
    finally:
        cm.__exit__(None, None, None)
    assert rest["media_item_id"] == track_ids[1]
    assert rest["position_seconds"] == 0.0
    assert rest["revision"] == moved["revision"]


def test_position_report_from_an_inactive_device_is_ignored(client, db_session):
    track_ids = _make_tracks(db_session, count=2)
    a_cm, a, _ = _connect_registered(client, "Laptop")
    b_cm, b, _ = _connect_registered(client, "Phone")
    try:
        a.receive_json()
        state = _set_queue_context(a, track_ids, track_ids[0])["state"]
        b.receive_json()
        b.send_json(
            {
                "type": "position_report",
                "queue_item_id": state["current_queue_item_id"],
                "position_seconds": 30.0,
            }
        )
        _round_trip(b)
    finally:
        b_cm.__exit__(None, None, None)
        a_cm.__exit__(None, None, None)
    assert client.get("/api/playback/state").json()["position_seconds"] == 0.0


def test_position_report_from_the_active_device_is_saved_and_broadcast(client, db_session):
    track_ids = _make_tracks(db_session, count=2)
    cm, ws, device_id = _connect_registered(client)
    try:
        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]
        ws.send_json(
            {
                "type": "position_report",
                "queue_item_id": state["current_queue_item_id"],
                "position_seconds": 12.5,
            }
        )
        msg = ws.receive_json()
    finally:
        cm.__exit__(None, None, None)
    assert msg["type"] == "state"
    assert msg["position_changed"] is True
    assert msg["source_device_id"] == device_id
    assert msg["source_command"] == "position_report"
    assert msg["state"]["position_seconds"] == 12.5
    assert msg["state"]["revision"] == state["revision"] + 1


def test_a_position_report_is_one_commit(client, db_session):
    # Sent every 5 s by every playing user: the position and the revision it
    # is broadcast with are saved together, not in two commits.
    from sqlalchemy import event

    track_ids = _make_tracks(db_session, count=2)
    engine = db_session.get_bind()
    commits: list[int] = []

    def count_commit(_conn) -> None:
        commits.append(1)

    cm, ws, _device_id = _connect_registered(client)
    try:
        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]
        _round_trip(ws)
        event.listen(engine, "commit", count_commit)
        try:
            for position in (5.0, 10.0):
                ws.send_json(
                    {
                        "type": "position_report",
                        "queue_item_id": state["current_queue_item_id"],
                        "position_seconds": position,
                    }
                )
                msg = ws.receive_json()
            _round_trip(ws)
        finally:
            event.remove(engine, "commit", count_commit)
    finally:
        cm.__exit__(None, None, None)
    assert msg["state"]["position_seconds"] == 10.0
    assert msg["state"]["revision"] == state["revision"] + 2
    assert len(commits) == 2


def test_the_snapshot_reads_only_what_the_access_check_needs_of_queued_items(
    client, db_session
):
    from sqlalchemy import event

    from app.models import User
    from app.playback import build_state_snapshot

    track_ids = _make_tracks(db_session, count=3)
    cm, ws, _device_id = _connect_registered(client)
    try:
        expected = _set_queue_context(ws, track_ids, track_ids[0])["state"]
    finally:
        cm.__exit__(None, None, None)
    _wait_for_teardowns()
    engine = db_session.get_bind()
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_rest):
        statements.append(statement)

    user = db_session.query(User).one()
    event.listen(engine, "before_cursor_execute", record)
    try:
        snapshot = build_state_snapshot(db_session, user)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert snapshot["queue_media_item_ids"] == expected["queue_media_item_ids"] == track_ids
    assert snapshot["queue_item_ids"] == expected["queue_item_ids"]
    media_reads = [s for s in statements if "FROM media_items" in s]
    assert media_reads
    assert not any("media_items.title" in s for s in media_reads)


def test_seek_naming_the_current_item_still_applies(client, db_session):
    track_ids = _make_tracks(db_session, count=2)
    cm, ws, _ = _connect_registered(client)
    try:
        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]
        ws.send_json(
            {"type": "seek", "queue_item_id": state["current_queue_item_id"], "position_seconds": 40}
        )
        msg = ws.receive_json()
    finally:
        cm.__exit__(None, None, None)
    assert msg["source_command"] == "seek"
    assert msg["state"]["position_seconds"] == 40.0


def test_pause_with_a_position_saves_it(client, db_session):
    track_ids = _make_tracks(db_session, count=2)
    cm, ws, _ = _connect_registered(client)
    try:
        _set_queue_context(ws, track_ids, track_ids[0])
        ws.send_json({"type": "pause", "position_seconds": 63.25})
        msg = ws.receive_json()
        ws.send_json({"type": "play"})
        ws.receive_json()
        # null: the sender has no position of its own - the saved one stays.
        ws.send_json({"type": "pause", "position_seconds": None})
        unknown = ws.receive_json()
    finally:
        cm.__exit__(None, None, None)
    assert msg["position_changed"] is False
    assert msg["state"]["is_playing"] is False
    assert msg["state"]["position_seconds"] == 63.25
    assert unknown["state"]["is_playing"] is False
    assert unknown["state"]["position_seconds"] == 63.25


def test_new_message_fields_are_validated(client, db_session):
    track_ids = _make_tracks(db_session, count=2)
    cm, ws, _ = _connect_registered(client)
    try:
        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]
        current = state["current_queue_item_id"]
        for command, message in (
            ({"type": "track_ended", "queue_item_id": "1"}, "Invalid queue item id"),
            ({"type": "track_ended", "queue_item_id": True}, "Invalid queue item id"),
            ({"type": "track_ended", "queue_item_id": 0}, "Invalid queue item id"),
            ({"type": "track_ended", "queue_item_id": 2**70}, "Invalid queue item id"),
            ({"type": "seek", "queue_item_id": 1.5, "position_seconds": 1}, "Invalid queue item id"),
            ({"type": "position_report", "position_seconds": 1}, "Invalid position report"),
            ({"type": "position_report", "queue_item_id": current}, "Invalid position report"),
            (
                {"type": "position_report", "queue_item_id": current, "position_seconds": "1"},
                "Invalid position report",
            ),
            (
                {"type": "position_report", "queue_item_id": current, "position_seconds": -1},
                "Invalid position report",
            ),
            (
                {"type": "position_report", "queue_item_id": current, "position_seconds": True},
                "Invalid position report",
            ),
            ({"type": "pause", "position_seconds": "soon"}, "Invalid pause position"),
            ({"type": "pause", "position_seconds": -3}, "Invalid pause position"),
        ):
            ws.send_json(command)
            assert ws.receive_json() == {"type": "error", "message": message}, command
        ws.send_text(
            f'{{"type": "position_report", "queue_item_id": {current}, "position_seconds": NaN}}'
        )
        assert ws.receive_json() == {"type": "error", "message": "Invalid position report"}
        # null is "not given" (JSON.stringify turns NaN into null).
        ws.send_json({"type": "pause", "position_seconds": None})
        assert ws.receive_json()["state"]["is_playing"] is False
    finally:
        cm.__exit__(None, None, None)


def test_messages_without_their_required_fields_are_rejected_and_change_nothing(
    client, db_session
):
    # The shapes older pages send: track_ended and seek with no
    # queue_item_id and pause with no position_seconds key. Each gets the
    # usual error reply and leaves the state - revision included - exactly
    # as it was.
    track_ids = _make_tracks(db_session, count=3)
    cm, ws, _ = _connect_registered(client)
    try:
        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]
        current = state["current_queue_item_id"]
        ws.send_json(
            {"type": "position_report", "queue_item_id": current, "position_seconds": 30.0}
        )
        before = ws.receive_json()["state"]
        for command, message in (
            ({"type": "track_ended"}, "Invalid queue item id"),
            ({"type": "track_ended", "queue_item_id": None}, "Invalid queue item id"),
            ({"type": "seek", "position_seconds": 5}, "Invalid queue item id"),
            ({"type": "seek", "queue_item_id": None, "position_seconds": 5}, "Invalid queue item id"),
            ({"type": "pause"}, "Invalid pause position"),
        ):
            ws.send_json(command)
            assert ws.receive_json() == {"type": "error", "message": message}, command
        _round_trip(ws)
        after = client.get("/api/playback/state").json()
    finally:
        cm.__exit__(None, None, None)
    assert before["media_item_id"] == track_ids[0]
    assert before["is_playing"] is True
    for key in ("media_item_id", "queue_index", "current_queue_item_id", "is_playing",
                "position_seconds", "revision"):
        assert after[key] == before[key], key


def test_remote_pause_with_a_null_position_keeps_the_saved_position(client, db_session):
    track_ids = _make_tracks(db_session, count=2)
    a_cm, active, _ = _connect_registered(client, "Laptop")
    b_cm, remote, _ = _connect_registered(client, "Phone")
    try:
        active.receive_json()  # the laptop sees the phone join
        state = _set_queue_context(active, track_ids, track_ids[0])["state"]
        remote.receive_json()
        active.send_json(
            {
                "type": "position_report",
                "queue_item_id": state["current_queue_item_id"],
                "position_seconds": 42.5,
            }
        )
        active.receive_json()
        remote.receive_json()
        remote.send_json({"type": "pause", "position_seconds": None})
        paused = remote.receive_json()
    finally:
        b_cm.__exit__(None, None, None)
        a_cm.__exit__(None, None, None)
    assert paused["source_command"] == "pause"
    assert paused["state"]["is_playing"] is False
    assert paused["state"]["position_seconds"] == 42.5


def test_revision_counts_state_changes_only(client, db_session):
    track_ids = _make_tracks(db_session, count=3)
    cm, ws, _ = _connect_registered(client)
    try:
        first = _set_queue_context(ws, track_ids, track_ids[0])["state"]["revision"]
        _seek_current(client, ws, 5)
        assert ws.receive_json()["state"]["revision"] == first + 1
        ws.send_json({"type": "set_repeat", "mode": "all"})
        assert ws.receive_json()["state"]["revision"] == first + 2
        ws.send_json({"type": "set_repeat", "mode": 7})  # rejected
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "track_ended", "queue_item_id": 999_999})  # stale
        assert ws.receive_json()["state"]["revision"] == first + 2
        assert client.get("/api/playback/state").json()["revision"] == first + 2
    finally:
        cm.__exit__(None, None, None)
    # The socket closing marks the device offline: that is a change too.
    # Teardown runs as its own task, so wait for it before reading the state.
    _wait_for_teardowns()
    assert client.get("/api/playback/state").json()["revision"] == first + 3


# --- No database work on the event loop --------------------------------------


def test_a_held_write_lock_stalls_only_the_socket_that_writes(client, db_session, session_factory):
    import sqlite3
    import threading
    import time

    from app.models import Device, User
    from app.security import hash_password
    from tests.conftest import login_as

    track_ids = _make_tracks(db_session, count=2)
    db_session.add(User(username="other", password_hash=hash_password("other-pass")))
    db_session.commit()
    other_token = login_as(session_factory, client, "other", "other-pass").cookies[
        "atriolyric_session"
    ]

    cm, ws, _ = _connect_registered(client)
    other_cm = client.websocket_connect(
        "/ws/playback", headers={"cookie": f"atriolyric_session={other_token}"}
    )
    other = other_cm.__enter__()
    lock = sqlite3.connect(
        session_factory.kw["bind"].url.database, timeout=0, isolation_level=None,
        check_same_thread=False,
    )
    release = threading.Timer(3.0, lambda: lock.execute("ROLLBACK"))
    try:
        other.send_json({"type": "register_device", "name": "Other"})
        other_device = other.receive_json()["device_id"]
        other.receive_json()
        with session_factory() as db:
            assert db.get(Device, other_device).user_id != db.get(Device, _).user_id
        state = _set_queue_context(ws, track_ids, track_ids[0])["state"]

        lock.execute("BEGIN IMMEDIATE")  # another writer holds the lock
        release.start()
        # Timed from the first send: the test client's own sends also go
        # through the server's event loop, so a blocked loop blocks them too.
        started = time.monotonic()
        ws.send_json({"type": "heartbeat"})
        ws.send_json(
            {
                "type": "position_report",
                "queue_item_id": state["current_queue_item_id"],
                "position_seconds": 5.0,
            }
        )
        time.sleep(0.3)  # the heartbeat is now waiting for the lock
        assert client.get("/api/health").status_code == 200
        other.send_json({"type": "not_a_command"})
        assert other.receive_json()["type"] == "error"
        elapsed = time.monotonic() - started
        # Otherwise the event loop itself would wait for the lock (3 s
        # here, up to the 30 s busy timeout in production).
        assert elapsed < 1.5, elapsed

        # The stalled socket finishes once the lock is released, in order.
        assert ws.receive_json() == {"type": "heartbeat_ack"}
        saved = ws.receive_json()
        assert saved["source_command"] == "position_report"
        assert saved["state"]["position_seconds"] == 5.0
    finally:
        release.cancel()
        release.join()
        if lock.in_transaction:
            lock.execute("ROLLBACK")
        lock.close()
        other_cm.__exit__(None, None, None)
        cm.__exit__(None, None, None)


def test_commands_sent_back_to_back_are_answered_in_order(client, db_session):
    track_ids = _make_tracks(db_session, count=2)
    cm, ws, _ = _connect_registered(client)
    try:
        start = _set_queue_context(ws, track_ids, track_ids[0])["state"]["revision"]
        current = _current_queue_item_id(client)
        for seconds in (1, 2, 3):
            ws.send_json({"type": "seek", "queue_item_id": current, "position_seconds": seconds})
        states = [ws.receive_json()["state"] for _ in range(3)]
    finally:
        cm.__exit__(None, None, None)
    assert [s["position_seconds"] for s in states] == [1.0, 2.0, 3.0]
    assert [s["revision"] for s in states] == [start + 1, start + 2, start + 3]


def test_heartbeat_is_acknowledged_to_its_sender_only(client, db_session, session_factory):
    from app.models import Device

    a_cm, a, a_id = _connect_registered(client, "Laptop")
    b_cm, b, _ = _connect_registered(client, "Phone")
    try:
        a.receive_json()  # A sees B join
        with session_factory() as db:
            seen_before = db.get(Device, a_id).last_seen_at
        a.send_json({"type": "heartbeat"})
        assert a.receive_json() == {"type": "heartbeat_ack"}
        _round_trip(b)  # B got no state for A's heartbeat
        with session_factory() as db:
            assert db.get(Device, a_id).last_seen_at > seen_before
    finally:
        b_cm.__exit__(None, None, None)
        a_cm.__exit__(None, None, None)


def test_every_command_error_is_reported_over_the_socket(client, db_session):
    # The command error paths of playback_ws, driven through the real
    # socket rather than with patched internals.
    cm = client.websocket_connect("/ws/playback")
    ws = cm.__enter__()
    try:
        ws.send_json({"type": "heartbeat"})  # ignored before registration
        for bad in ({"type": "register_device", "name": 5},
                    {"type": "register_device", "resume_playback": "yes"},
                    {"type": "register_device", "client_token": "x" * 129}):
            ws.send_json(bad)
            assert ws.receive_json() == {"type": "error", "message": "Invalid device registration"}
        ws.send_json([1, 2])
        assert ws.receive_json() == {"type": "error", "message": "Message must be an object"}
        ws.send_json({"type": "register_device", "name": "Laptop"})
        assert ws.receive_json()["type"] == "registered"
        ws.receive_json()
        ws.send_json({"type": "client_diagnostic", "event": "unrecognized"})  # no reply
        for command, message in (
            ({"type": "register_device", "name": "Again"}, "Device is already registered"),
            ({"type": "set_queue_context", "context": {}, "start_media_item_id": 1}, "Invalid queue"),
            ({"type": "set_repeat", "mode": "bad"}, "Invalid repeat mode"),
            ({"type": "add_to_queue", "media_item_id": "bad"}, "Invalid add-to-queue command"),
            ({"type": "add_to_queue", "media_item_id": 99999}, "Media item 99999 not found"),
            ({"type": "seek", "position_seconds": "bad"}, "Invalid seek position"),
            ({"type": "jump_to_index", "index": None}, "Invalid queue index"),
            ({"type": "set_shuffle", "enabled": 1}, "Invalid shuffle value"),
            ({"type": "remove_from_queue", "queue_item_id": 99999}, "Queue item not found"),
            ({"type": "move_queue_item", "queue_item_id": 99999, "direction": 1}, "Queue item not found"),
            ({"type": "reorder_queue_item", "queue_item_id": 99999, "target_index": 0}, "Queue item not found"),
            ({"type": "remove_device", "device_id": 99999}, "Device not found"),
            ({"type": "transfer_to", "device_id": 99999}, "Device not found"),
            ({"type": "unknown"}, "Unknown message type: unknown"),
        ):
            ws.send_json(command)
            assert ws.receive_json() == {"type": "error", "message": message}, command
        # Accepted commands on an empty queue still answer with state.
        for command in ({"type": "play"}, {"type": "pause", "position_seconds": None},
                        {"type": "next"}, {"type": "prev"},
                        {"type": "jump_to_index", "index": 0}, {"type": "set_shuffle", "enabled": True}):
            ws.send_json(command)
            assert ws.receive_json()["type"] == "state", command
    finally:
        cm.__exit__(None, None, None)


def test_a_failing_track_end_is_logged_and_closes_the_socket(client, db_session, caplog):
    import pytest

    track_ids = _make_tracks(db_session, count=2)
    caplog.set_level(logging.ERROR, logger="app.routers.playback")
    cm, ws, _ = _connect_registered(client)
    _set_queue_context(ws, track_ids, track_ids[0])
    with patch("app.routers.playback.track_ended", side_effect=RuntimeError("worker exploded")):
        _end_current_track(client, ws)
        with pytest.raises(Exception):  # noqa: B017 - the server dropped the socket
            ws.receive_json()
        # The handler's exception surfaces when the test client closes.
        with pytest.raises(RuntimeError, match="worker exploded"):
            cm.__exit__(None, None, None)
        _wait_for_teardowns()
    assert "playback_diagnostic event=track_ended_failed" in caplog.text


# --- A device whose socket went quiet stops blocking the others -------------
#
# A tab that played an album and was closed can leave its socket registered
# (no close arrives) and its row connected; the next tab's "play this album"
# must not become a remote control for a page that no longer exists. A
# socket is only known to be alive by what it says; these sockets simply
# stop talking, as such a page does, without ever closing.


def _silent_for(seconds: float) -> None:
    import time

    time.sleep(seconds)


def test_a_new_queue_claims_output_from_an_active_device_whose_socket_went_silent(
    client, db_session, monkeypatch, caplog
):
    monkeypatch.setattr("app.routers.playback.ACTIVE_DEVICE_SILENT_SECONDS", 0.3)
    caplog.set_level(logging.INFO, logger="app.routers.playback")
    track_ids = _make_tracks(db_session)
    a_cm, a = _open(client)
    try:
        a_id = _register_device(a, "tok-a", "Phone")
        a.receive_json()
        assert _set_queue_context(a, track_ids, track_ids[0])["state"]["active_device_id"] == a_id
        # A says nothing more and never closes.
        with client.websocket_connect("/ws/playback") as c:
            c_id = _register_device(c, "tok-c", "Phone")
            state = c.receive_json()["state"]
            assert state["active_device_id"] == a_id
            assert state["is_playing"] is True
            assert next(d for d in state["devices"] if d["id"] == a_id)["is_connected"] is True
            _silent_for(0.5)
            state = _set_queue_context(c, track_ids, track_ids[1])["state"]
            assert state["active_device_id"] == c_id
            assert state["is_playing"] is True
            assert state["media_item_id"] == track_ids[1]
    finally:
        a_cm.__exit__(None, None, None)
        _wait_for_teardowns()
    assert '"event": "active_device_not_live"' in caplog.text
    assert f'"device_id": {a_id}' in caplog.text


def test_play_claims_output_from_an_active_device_whose_socket_went_silent(
    client, db_session, monkeypatch
):
    monkeypatch.setattr("app.routers.playback.ACTIVE_DEVICE_SILENT_SECONDS", 0.3)
    track_ids = _make_tracks(db_session)
    a_cm, a = _open(client)
    try:
        a_id = _register_device(a, "tok-a", "Laptop")
        a.receive_json()
        _set_queue_context(a, track_ids, track_ids[0])
        with client.websocket_connect("/ws/playback") as c:
            c_id = _register_device(c, "tok-c", "Phone")
            assert c.receive_json()["state"]["active_device_id"] == a_id
            _silent_for(0.5)
            c.send_json({"type": "play"})
            state = c.receive_json()["state"]
            assert state["active_device_id"] == c_id
            assert state["is_playing"] is True
    finally:
        a_cm.__exit__(None, None, None)
        _wait_for_teardowns()


def test_an_active_device_that_keeps_talking_stays_remote_controlled(
    client, db_session, monkeypatch
):
    # The other side of the rule: a playing device that is heard from (its
    # position reports, its heartbeats) is remote-controlled, never replaced.
    monkeypatch.setattr("app.routers.playback.ACTIVE_DEVICE_SILENT_SECONDS", 0.3)
    track_ids = _make_tracks(db_session)
    with client.websocket_connect("/ws/playback") as a:
        a_id = _register_device(a, "tok-a", "Laptop")
        a.receive_json()
        _set_queue_context(a, track_ids, track_ids[0])
        with client.websocket_connect("/ws/playback") as c:
            _register_device(c, "tok-c", "Phone")
            c.receive_json()
            a.receive_json()
            for _ in range(4):
                _silent_for(0.15)
                a.send_json({"type": "heartbeat"})
                assert a.receive_json() == {"type": "heartbeat_ack"}
            state = _set_queue_context(c, track_ids, track_ids[1])["state"]
    assert state["active_device_id"] == a_id
    assert state["media_item_id"] == track_ids[1]


def test_a_connected_row_with_no_socket_does_not_block_a_new_queue(
    client, db_session, session_factory
):
    # A row left "connected" with no socket behind it (a teardown that never
    # ran, a crash) is not a live device.
    from app.models import Device

    track_ids = _make_tracks(db_session)
    a_cm, a = _open(client)
    a_id = _register_device(a, "tok-a", "Laptop")
    a.receive_json()
    _set_queue_context(a, track_ids, track_ids[0])
    a_cm.__exit__(None, None, None)
    _wait_for_teardowns()
    with session_factory() as db:
        db.get(Device, a_id).is_connected = True
        db.commit()
    with client.websocket_connect("/ws/playback") as c:
        c_id = _register_device(c, "tok-c", "Phone")
        state = c.receive_json()["state"]
        assert state["active_device_id"] == a_id
        assert next(d for d in state["devices"] if d["id"] == a_id)["is_connected"] is True
        state = _set_queue_context(c, track_ids, track_ids[1])["state"]
    assert state["active_device_id"] == c_id
    assert state["is_playing"] is True


def test_a_silent_socket_is_closed_with_4408_and_its_device_marked_offline(
    client, db_session, session_factory, monkeypatch, caplog
):
    from app.models import Device

    monkeypatch.setattr("app.routers.playback.SILENT_SOCKET_CLOSE_SECONDS", 0.4)
    caplog.set_level(logging.INFO, logger="app.routers.playback")
    track_ids = _make_tracks(db_session)
    a_cm, a = _open(client)
    a_id = _register_device(a, "tok-a", "Phone")
    a.receive_json()
    _set_queue_context(a, track_ids, track_ids[0])
    try:
        assert _receive_until_closed(a) == 4408
    finally:
        a_cm.__exit__(None, None, None)
    _wait_for_teardowns()
    with session_factory() as db:
        assert db.get(Device, a_id).is_connected is False
    assert '"event": "websocket_silent_closed"' in caplog.text
    assert '"reason": "silent"' in caplog.text
    # And the next device's album plays there (the ordinary offline rule).
    monkeypatch.setattr("app.routers.playback.SILENT_SOCKET_CLOSE_SECONDS", 30.0)
    with client.websocket_connect("/ws/playback") as c:
        c_id = _register_device(c, "tok-c", "Laptop")
        c.receive_json()
        assert _set_queue_context(c, track_ids, track_ids[1])["state"]["active_device_id"] == c_id


def test_a_socket_that_keeps_sending_heartbeats_is_not_closed(client, monkeypatch):
    monkeypatch.setattr("app.routers.playback.SILENT_SOCKET_CLOSE_SECONDS", 0.4)
    with client.websocket_connect("/ws/playback") as ws:
        _register_device(ws, "tok-hb", "Phone")
        ws.receive_json()
        for _ in range(5):
            _silent_for(0.2)
            ws.send_json({"type": "heartbeat"})
            assert ws.receive_json() == {"type": "heartbeat_ack"}
        ws.send_json({"type": "pause", "position_seconds": None})
        assert ws.receive_json()["type"] == "state"


def test_queue_origin_is_stored_broadcast_and_replaced(client, db_session):
    """Now Playing links back to what the queue was started from: the
    origin a set_queue_context names comes back in every state, a malformed
    one is dropped without stopping playback, and a queue started without
    one has none."""
    track_ids = _make_tracks(db_session)
    origin = {"kind": "playlist", "label": "Japanese mix", "payload": {"playlist_id": 7}}
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        ws.receive_json()  # registered
        ws.receive_json()  # initial state
        ws.send_json(
            {
                "type": "set_queue_context",
                "context": {"kind": "ids", "ids": track_ids},
                "start_media_item_id": track_ids[0],
                "origin": origin,
            }
        )
        assert ws.receive_json()["state"]["queue_origin"] == origin
        assert client.get("/api/playback/state").json()["queue_origin"] == origin

        ws.send_json(
            {
                "type": "set_queue_context",
                "context": {"kind": "ids", "ids": track_ids},
                "start_media_item_id": track_ids[1],
                "origin": {"kind": "playlist", "label": "x", "payload": {"playlist_id": "7"}},
            }
        )
        state = ws.receive_json()["state"]
        assert state["media_item_id"] == track_ids[1]
        assert state["queue_origin"] is None

        ws.send_json(
            {
                "type": "set_queue_context",
                "context": {"kind": "ids", "ids": track_ids},
                "start_media_item_id": track_ids[0],
                "origin": {"kind": "radio", "label": "FM", "payload": {"station_id": 1}},
            }
        )
        assert ws.receive_json()["state"]["queue_origin"] is None

        ws.send_json(
            {
                "type": "set_queue_context",
                "context": {"kind": "ids", "ids": track_ids},
                "start_media_item_id": track_ids[0],
                "origin": origin,
            }
        )
        assert ws.receive_json()["state"]["queue_origin"] == origin
        assert _set_queue_context(ws, track_ids, track_ids[2])["state"]["queue_origin"] is None


# --- A socket a broadcast cannot deliver to is torn down ---------------------


def _server_socket(client, device_id: int):
    from app.ws_manager import manager

    for sockets in manager._connections.values():
        if device_id in sockets:
            return sockets[device_id]
    raise AssertionError(f"device {device_id} has no registered socket")


@contextmanager
def _undeliverable(target, *, stall: bool = False):
    """Broadcasts to the server-side socket `target` fail, or with `stall`
    never finish (a connection whose write buffer is full)."""
    import asyncio

    from starlette.websockets import WebSocket as StarletteWebSocket

    real_send_json = StarletteWebSocket.send_json

    async def send_json(self, data, mode="text"):
        if self is target:
            if stall:
                await asyncio.sleep(3600)
            raise RuntimeError("connection lost")
        await real_send_json(self, data, mode)

    with patch.object(StarletteWebSocket, "send_json", send_json):
        yield


def _device_connected(session_factory, device_id: int) -> bool:
    from app.models import Device

    with session_factory() as db:
        return bool(db.get(Device, device_id).is_connected)


def _receive_state_showing_offline(ws, device_id: int) -> None:
    for _ in range(10):
        msg = ws.receive_json()
        if msg["type"] == "state" and any(
            d["id"] == device_id and not d["is_connected"] for d in msg["state"]["devices"]
        ):
            return
    raise AssertionError("no state showed the dropped device offline")


@pytest.mark.parametrize("stall", [False, True], ids=["send-fails", "send-stalls"])
def test_a_socket_a_broadcast_cannot_deliver_to_is_closed_and_marked_offline(
    client, session_factory, monkeypatch, stall
):
    from app.ws_manager import manager

    monkeypatch.setattr("app.ws_manager.BROADCAST_SEND_TIMEOUT_SECONDS", 0.2)
    laptop_cm, laptop, laptop_id = _connect_registered(client, "Laptop")
    phone_cm, phone, phone_id = _connect_registered(client, "Phone")
    laptop.receive_json()  # the phone joined
    try:
        with _undeliverable(_server_socket(client, phone_id), stall=stall):
            laptop.send_json({"type": "pause", "position_seconds": None})
            assert laptop.receive_json()["type"] == "state"
            # The phone's own handler notices, although nothing arrives on
            # its socket, and does the ordinary teardown.
            wait_until(
                lambda: not _device_connected(session_factory, phone_id),
                timeout=5,
                what="the dropped device to be marked offline",
            )
            _receive_state_showing_offline(laptop, phone_id)
            assert _receive_until_closed(phone) == 1011
        _wait_for_teardowns()
        assert [list(sockets) for sockets in manager._connections.values()] == [[laptop_id]]
        assert manager._dropped == set()
        assert _device_connected(session_factory, laptop_id)
    finally:
        phone_cm.__exit__(None, None, None)
        laptop_cm.__exit__(None, None, None)


def test_a_socket_that_cannot_receive_its_own_commands_state_is_torn_down(
    client, session_factory
):
    # A phone's connection dies while its own position
    # report is being processed.
    laptop_cm, laptop, _laptop_id = _connect_registered(client, "Laptop")
    phone_cm, phone, phone_id = _connect_registered(client, "Phone")
    laptop.receive_json()  # the phone joined
    try:
        with _undeliverable(_server_socket(client, phone_id)):
            phone.send_json({"type": "pause", "position_seconds": None})
            assert laptop.receive_json()["type"] == "state"
            wait_until(
                lambda: not _device_connected(session_factory, phone_id),
                timeout=5,
                what="the dropped device to be marked offline",
            )
            _receive_state_showing_offline(laptop, phone_id)
    finally:
        phone_cm.__exit__(None, None, None)
        laptop_cm.__exit__(None, None, None)
    _wait_for_teardowns()


def test_a_dropped_socket_is_torn_down_once_and_never_evicts_its_replacement():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.ws_manager import ConnectionManager

    manager = ConnectionManager()
    woken: list[str] = []
    good = SimpleNamespace(send_json=AsyncMock())
    bad = SimpleNamespace(send_json=AsyncMock(side_effect=RuntimeError("socket closed")))
    manager.connect(1, 1, good)
    manager.connect(1, 2, bad, on_dropped=lambda: woken.append("bad"))

    delivery = asyncio.run(manager.broadcast(1, {"type": "state"}))
    assert delivery == {"delivered_device_ids": [1], "failed_device_ids": [2]}
    # Still registered: its handler is the one that unregisters and tears down.
    assert manager._connections[1] == {1: good, 2: bad}
    assert woken == ["bad"]

    # Later broadcasts leave it alone instead of waiting on it again.
    delivery = asyncio.run(manager.broadcast(1, {"type": "state"}))
    assert delivery == {"delivered_device_ids": [1], "failed_device_ids": []}
    assert bad.send_json.await_count == 1
    assert woken == ["bad"]

    # The device reconnects before the dropped socket's handler has run: the
    # new socket gets broadcasts, and the old handler finds itself replaced.
    replacement = SimpleNamespace(send_json=AsyncMock())
    assert manager.connect(1, 2, replacement) is bad
    delivery = asyncio.run(manager.broadcast(1, {"type": "state"}))
    assert delivery == {"delivered_device_ids": [1, 2], "failed_device_ids": []}
    assert manager.disconnect(1, 2, bad) is False
    assert manager.disconnect(1, 2, replacement) is True
    assert manager.disconnect(1, 2, replacement) is False


# --- The default log holds no addresses and no listening trail ---------------


def _connect_play_and_end_a_track(client, db_session) -> None:
    track_ids = _make_tracks(db_session, count=2)
    with client.websocket_connect(
        "/ws/playback", headers={"user-agent": "AtrioTestAgent/9.9"}
    ) as ws:
        ws.send_json({"type": "register_device", "name": "Phone"})
        ws.receive_json()
        ws.receive_json()
        _set_queue_context(ws, track_ids, track_ids[0])
        ws.send_json(
            {"type": "client_diagnostic", "event": "media_ended", "media_item_id": track_ids[0]}
        )
        _end_current_track(client, ws)
        assert ws.receive_json()["state"]["media_item_id"] == track_ids[1]
    _wait_for_teardowns()


def test_default_logging_records_no_address_user_agent_or_played_item(
    client, db_session, caplog, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "verbose_logging", False)
    caplog.set_level(logging.DEBUG)
    _connect_play_and_end_a_track(client, db_session)
    assert "AtrioTestAgent" not in caplog.text
    assert "testclient" not in caplog.text
    assert "media_item_id" not in caplog.text
    assert "queue_item_id" not in caplog.text
    assert "queue_index" not in caplog.text
    assert "track_ended" not in caplog.text
    assert "state_broadcast" not in caplog.text
    # What an operator needs is still there: sockets opening and closing.
    assert "ws_connect user_id=" in caplog.text
    assert '"event": "websocket_closed"' in caplog.text


def test_verbose_logging_records_the_connection_and_the_playback_trail(
    client, db_session, caplog, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "verbose_logging", True)
    caplog.set_level(logging.DEBUG)
    _connect_play_and_end_a_track(client, db_session)
    assert '"event": "websocket_accepted"' in caplog.text
    assert '"user_agent": "AtrioTestAgent/9.9"' in caplog.text
    assert '"ip": "testclient"' in caplog.text
    for event in (
        "device_registered",
        "ws_message",
        "client_media_ended",
        "track_ended_received",
        "track_ended_processed",
        "state_broadcast",
        "device_disconnected",
        "websocket_closed",
    ):
        assert f'"event": "{event}"' in caplog.text
    assert '"media_item_id": 2' in caplog.text


def test_a_media_error_is_logged_without_the_item_at_the_default_setting(
    client, db_session, caplog, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "verbose_logging", False)
    caplog.set_level(logging.INFO, logger="app.routers.playback")
    cm, ws, _device_id = _connect_registered(client)
    try:
        ws.send_json(
            {
                "type": "client_diagnostic",
                "event": "media_error",
                "source": "hls",
                "media_item_id": 41,
                "hls_details": "fragLoadError",
                "fatal": True,
                "http_status": 503,
                "current_time": 12.5,
            }
        )
        _round_trip(ws)
    finally:
        cm.__exit__(None, None, None)
    assert '"event": "client_media_error"' in caplog.text
    assert '"hls_details": "fragLoadError"' in caplog.text
    assert '"http_status": 503' in caplog.text
    assert "media_item_id" not in caplog.text
    assert "current_time" not in caplog.text
