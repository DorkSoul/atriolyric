from __future__ import annotations

import logging

import pytest

from app.config import settings
from app.logging_setup import _configured_level


def _lines(caplog, logger_name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger_name]


def test_writes_are_logged_with_user_and_request_id(client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    response = client.post(
        "/api/admin/roots",
        json={"path": "/tmp/activity-a", "display_name": "Activity A", "is_shared": True},
    )
    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]

    request_line = next(l for l in _lines(caplog, "app.requests") if "POST /api/admin/roots" in l)
    assert "-> 200" in request_line
    assert f"req={request_id}" in request_line
    assert "user=dork_soul#" in request_line

    action = next(l for l in _lines(caplog, "app.activity") if l.startswith("root.create"))
    assert f"req={request_id}" in action
    assert "actor=dork_soul#" in action
    assert '"/tmp/activity-a"' in action


def test_failures_log_the_reason(client, caplog, monkeypatch):
    monkeypatch.setattr(settings, "verbose_logging", False)
    caplog.set_level(logging.INFO, logger="app")
    client.post(
        "/api/admin/roots",
        json={"path": "/tmp/activity-b", "display_name": "B", "is_shared": True},
    )
    caplog.clear()

    clash = client.post(
        "/api/admin/roots",
        json={"path": "/tmp/activity-b", "display_name": "B2", "is_shared": True},
    )
    assert clash.status_code == 409
    bad = client.post("/api/admin/roots", json={"path": "", "display_name": ""})
    assert bad.status_code == 422

    lines = _lines(caplog, "app.requests")
    assert any("-> 409" in l and 'detail="Library root already exists"' in l for l in lines)
    assert any("-> 422" in l and "invalid request" in l for l in lines)
    assert all(r.levelno == logging.WARNING for r in caplog.records if r.name == "app.requests")


def test_successful_reads_and_health_are_not_logged(client, caplog, monkeypatch):
    monkeypatch.setattr(settings, "verbose_logging", False)
    caplog.set_level(logging.INFO, logger="app")
    client.get("/api/admin/roots")
    client.get("/api/health")
    assert _lines(caplog, "app.requests") == []


def test_failed_login_is_logged_without_the_password(raw_client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    response = raw_client.post(
        "/api/auth/login", json={"username": "nobody", "password": "hunter2-secret"}
    )
    assert response.status_code == 401
    assert "hunter2-secret" not in caplog.text
    line = next(l for l in _lines(caplog, "app.activity") if l.startswith("auth.login_failed"))
    # An unknown name is logged as a short hash: people type passwords there.
    assert 'username="nobody"' not in line
    assert 'username="unknown:' in line
    assert 'reason="unknown user"' in line


def test_log_values_cannot_forge_extra_lines(client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    client.post(
        "/api/admin/roots",
        json={"path": "/tmp/activity-c", "display_name": "x\nINFO: forged", "is_shared": True},
    )
    action = next(l for l in _lines(caplog, "app.activity") if l.startswith("root.create"))
    assert "\n" not in action


def test_client_errors_are_logged_and_rate_limited(client, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="app")
    from app.routers import client_log

    client_log._recent.clear()
    ok = client.post(
        "/api/client-log",
        json={"message": "TypeError: x is undefined", "source": "app.js:1:2", "page": "#/files"},
    )
    assert ok.status_code == 204
    line = next(l for l in _lines(caplog, "app.activity") if l.startswith("client.error"))
    assert 'message="TypeError: x is undefined"' in line

    monkeypatch.setattr(client_log, "MAX_PER_WINDOW", 2)
    client_log._recent.clear()
    codes = [client.post("/api/client-log", json={"message": f"e{i}"}).status_code for i in range(3)]
    assert codes == [204, 204, 429]


def test_client_log_requires_login(raw_client):
    assert raw_client.post("/api/client-log", json={"message": "x"}).status_code == 401


@pytest.mark.parametrize(
    ("value", "expected"),
    [("debug", logging.DEBUG), ("WARNING", logging.WARNING), (" error ", logging.ERROR), ("nonsense", logging.INFO)],
)
def test_log_level_comes_from_settings(monkeypatch, value, expected):
    monkeypatch.setattr(settings, "log_level", value)
    assert _configured_level() == expected


def test_client_debug_events_are_written_to_the_log(client, caplog, monkeypatch):
    monkeypatch.setattr(settings, "verbose_logging", True)
    caplog.set_level(logging.INFO)
    resp = client.post(
        "/api/client-log/debug",
        json={
            "session": "abc123",
            "events": [
                {"at": "2026-10-22T10:00:00.000Z", "event": "life.pagehide", "fields": {"vis": "hidden", "persisted": False}}
            ],
        },
    )
    assert resp.status_code == 204
    line = next(l for l in _lines(caplog, "app.client") if "life.pagehide" in l)
    assert "sid=abc123" in line
    assert '"vis": "hidden"' in line


def test_verbose_logging_logs_successful_reads(client, caplog, monkeypatch):
    monkeypatch.setattr(settings, "verbose_logging", True)
    caplog.set_level(logging.INFO, logger="app")
    client.get("/api/playback/state")
    lines = _lines(caplog, "app.requests")
    assert any("GET /api/playback/state -> 200" in l for l in lines)
    assert any("range=-" in l for l in lines)


# --- logging defaults and privacy --------------------------------------------------


def test_verbose_logging_is_off_by_default():
    from app.config import Settings

    assert Settings().verbose_logging is False
    assert settings.verbose_logging is False


def test_default_is_quiet_for_reads_but_not_writes(client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    client.get("/api/playback/state")
    assert not [l for l in _lines(caplog, "app.requests") if "/api/playback/state" in l]
    assert not any("ip=" in l or "ua=" in l for l in _lines(caplog, "app.requests"))
    client.post(
        "/api/admin/roots",
        json={"path": "/tmp/activity-quiet", "display_name": "Quiet", "is_shared": True},
    )
    assert any("POST /api/admin/roots" in l for l in _lines(caplog, "app.requests"))


def test_version_reports_the_debug_flag(raw_client, monkeypatch):
    assert raw_client.get("/api/version").json()["debug_log"] is False
    monkeypatch.setattr(settings, "verbose_logging", True)
    assert raw_client.get("/api/version").json()["debug_log"] is True


def test_debug_endpoint_is_a_no_op_when_disabled(client, caplog):
    caplog.set_level(logging.INFO)
    body = {"session": "s", "events": [{"at": "t", "event": "life.x", "fields": {}}]}
    assert client.post("/api/client-log/debug", json=body).status_code == 204
    assert not _lines(caplog, "app.client")


def test_debug_endpoint_enforces_its_limits_when_enabled(client, caplog, monkeypatch):
    from app import schemas
    from app.routers import client_log

    monkeypatch.setattr(settings, "verbose_logging", True)
    client_log._recent_debug.clear()
    assert client_log.MAX_DEBUG_BATCHES_PER_WINDOW == 90 and schemas.MAX_DEBUG_EVENTS == 50
    event = {"at": "t", "event": "life.x", "fields": {}}
    too_many_events = {"session": "s", "events": [event] * 51}
    assert client.post("/api/client-log/debug", json=too_many_events).status_code == 422
    body = {"session": "s", "events": [event] * 50}
    for _ in range(90):
        assert client.post("/api/client-log/debug", json=body).status_code == 204
    assert client.post("/api/client-log/debug", json=body).status_code == 429
    client_log._recent_debug.clear()


def test_debug_fields_with_newlines_stay_on_one_line(client, caplog, monkeypatch):
    monkeypatch.setattr(settings, "verbose_logging", True)
    caplog.set_level(logging.INFO)
    client.post(
        "/api/client-log/debug",
        json={
            "session": "s1\nWARNING forged",
            "events": [{"at": "t\nforged", "event": "e\nINFO forged", "fields": {"k": "v\nx"}}],
        },
    )
    lines = _lines(caplog, "app.client")
    assert len(lines) == 1
    assert "\n" not in lines[0]
    assert "forged" in lines[0]


def test_request_line_encodes_control_characters_in_the_path(client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    client.get("/api/nothing%0AINFO:%20forged")
    lines = _lines(caplog, "app.requests")
    assert lines and all("\n" not in l for l in lines)
    assert any("forged" in l and "-> 404" in l for l in lines)


def test_log_token_leaves_plain_values_and_encodes_the_rest():
    from app.activity import log_token

    assert log_token("dork_soul#1") == "dork_soul#1"
    assert log_token("a\nb") == '"a\\nb"'
    assert log_token("two words") == '"two words"'
    assert log_token('q"uote') == '"q\\"uote"'


def test_admin_cannot_create_a_username_with_control_characters(client):
    for bad in ("evil\nname", "tab\there"):
        resp = client.post(
            "/api/admin/users", json={"username": bad, "password": "long-enough-pw"}
        )
        assert resp.status_code == 422, bad
    ok = client.post("/api/admin/users", json={"username": "fine name", "password": "long-enough-pw"})
    assert ok.status_code == 200
