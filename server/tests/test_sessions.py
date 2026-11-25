"""Session lifecycle: digest-only lookup and revoked sessions losing their
playback sockets."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from starlette.websockets import WebSocketDisconnect

from app import migrations
from app.deps import _user_for_token
from app.migrations import purge_plaintext_sessions
from app.models import Session, utcnow
from app.security import session_token_digest
from app.ws_manager import close_user_sessions
from tests.conftest import DEFAULT_PASSWORD, DEFAULT_USERNAME, login_as


def _register(ws) -> None:
    ws.send_json({"type": "register_device", "name": "Test"})
    assert ws.receive_json()["type"] == "registered"
    assert ws.receive_json()["type"] == "state"


def _assert_closed_4401(ws) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_json()
    assert closed.value.code == 4401


def test_plaintext_session_row_does_not_authenticate_and_is_purged(
    db_session, session_factory
):
    expires = utcnow() + timedelta(hours=1)
    raw = "legacy-urlsafe-token_value"
    good = session_token_digest("fresh-cookie")
    upper = "A" * 64
    db_session.add_all(
        [
            Session(token=raw, user_id=1, expires_at=expires),
            Session(token=good, user_id=1, expires_at=expires),
            Session(token=upper, user_id=1, expires_at=expires),
            Session(token="g" * 64, user_id=1, expires_at=expires),
        ]
    )
    db_session.commit()

    assert _user_for_token(db_session, raw) is None
    assert _user_for_token(db_session, "fresh-cookie") is not None

    with patch.object(migrations, "engine", session_factory.kw["bind"]):
        purge_plaintext_sessions()
    db_session.expire_all()
    assert [row.token for row in db_session.query(Session)] == [good]


def test_logout_closes_a_silent_socket(client):
    with client.websocket_connect("/ws/playback") as ws:
        _register(ws)
        assert client.post("/api/auth/logout").status_code == 200
        _assert_closed_4401(ws)


def test_password_change_closes_other_sessions_sockets_only(client, session_factory):
    other = login_as(session_factory, client, DEFAULT_USERNAME, DEFAULT_PASSWORD)
    with client.websocket_connect("/ws/playback") as mine, other.websocket_connect(
        "/ws/playback"
    ) as theirs:
        _register(mine)
        _register(theirs)
        # Registering the second device broadcast a state to the first.
        assert mine.receive_json()["type"] == "state"
        resp = client.post(
            "/api/auth/password",
            json={"current_password": DEFAULT_PASSWORD, "new_password": "new-password"},
        )
        assert resp.status_code == 200
        _assert_closed_4401(theirs)
        # The session that changed the password keeps its socket.
        assert client.put(
            "/api/auth/accent-color", json={"accent_color": "#10b981"}
        ).status_code == 200
        assert mine.receive_json()["type"] == "accent_color_changed"


def test_logout_everywhere_signs_out_every_session_and_closes_sockets(
    client, session_factory
):
    other = login_as(session_factory, client, DEFAULT_USERNAME, DEFAULT_PASSWORD)
    with client.websocket_connect("/ws/playback") as mine, other.websocket_connect(
        "/ws/playback"
    ) as theirs:
        _register(mine)
        _register(theirs)
        # Registering the second device broadcast a state to the first.
        assert mine.receive_json()["type"] == "state"
        resp = client.post("/api/auth/logout-everywhere")
        assert resp.status_code == 200
        assert "atriolyric_session" in resp.headers["set-cookie"]
        _assert_closed_4401(mine)
        _assert_closed_4401(theirs)
    assert client.get("/api/auth/me").status_code == 401
    assert other.get("/api/auth/me").status_code == 401


def test_logout_everywhere_requires_authentication(raw_client):
    assert raw_client.post("/api/auth/logout-everywhere").status_code == 401


def test_close_user_sessions_closes_all_when_no_digests(client):
    """The admin password reset / user deletion paths call this helper."""
    with client.websocket_connect("/ws/playback") as ws:
        _register(ws)
        asyncio.run(close_user_sessions(1, None))
        _assert_closed_4401(ws)


def test_close_user_sessions_ignores_other_digests(client):
    with client.websocket_connect("/ws/playback") as ws:
        _register(ws)
        asyncio.run(close_user_sessions(1, [session_token_digest("someone-else")]))
        assert client.put(
            "/api/auth/accent-color", json={"accent_color": "#10b981"}
        ).status_code == 200
        assert ws.receive_json()["type"] == "accent_color_changed"


def test_session_logout_without_cookie_is_harmless(raw_client):
    assert raw_client.post("/api/auth/logout").status_code == 200


def _second_user_socket_client(client, session_factory):
    resp = client.post(
        "/api/admin/users", json={"username": "listener", "password": "listener-pass"}
    )
    assert resp.status_code in (200, 201), resp.text
    other = login_as(session_factory, client, "listener", "listener-pass")
    return resp.json()["id"], other


def test_admin_deleting_a_user_closes_their_sockets(client, session_factory):
    user_id, other = _second_user_socket_client(client, session_factory)
    with other.websocket_connect("/ws/playback") as ws:
        _register(ws)
        assert client.delete(f"/api/admin/users/{user_id}").status_code == 200
        _assert_closed_4401(ws)


def test_admin_password_reset_closes_the_users_sockets(client, session_factory):
    user_id, other = _second_user_socket_client(client, session_factory)
    with other.websocket_connect("/ws/playback") as ws:
        _register(ws)
        resp = client.post(
            f"/api/admin/users/{user_id}/password",
            json={"new_password": "brand-new-pass"},
        )
        assert resp.status_code == 200
        _assert_closed_4401(ws)
