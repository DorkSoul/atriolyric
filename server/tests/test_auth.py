from __future__ import annotations

from tests.conftest import DEFAULT_PASSWORD, DEFAULT_USERNAME


def test_oversized_password_is_rejected_without_server_error(raw_client):
    resp = raw_client.post(
        "/api/auth/login",
        json={"username": DEFAULT_USERNAME, "password": "x" * 1000},
    )
    assert resp.status_code == 401


def test_login_succeeds_and_sets_cookie(raw_client):
    resp = raw_client.post(
        "/api/auth/login",
        json={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == DEFAULT_USERNAME
    assert body["is_admin"] is True
    assert "atriolyric_session" in resp.cookies


def test_auto_cookie_is_secure_only_over_https(raw_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "cookie_secure", "auto")
    login = {"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD}
    plain = raw_client.post("/api/auth/login", json=login)
    assert plain.status_code == 200
    assert "secure" not in plain.headers["set-cookie"].lower()
    proxied = raw_client.post(
        "/api/auth/login", json=login, headers={"X-Forwarded-Proto": "https"}
    )
    assert proxied.status_code == 200
    assert "; secure" in proxied.headers["set-cookie"].lower()


def test_login_accepts_a_configured_extra_origin(raw_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allowed_origins", ("http://127.0.0.1:3000",))
    resp = raw_client.post(
        "/api/auth/login",
        headers={"Origin": "http://127.0.0.1:3000"},
        json={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
    )
    assert resp.status_code == 200


def test_login_rejects_an_unlisted_origin_by_default(raw_client):
    resp = raw_client.post(
        "/api/auth/login",
        headers={"Origin": "http://127.0.0.1:3000"},
        json={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
    )
    assert resp.status_code == 403


def test_login_wrong_password_rejected(raw_client):
    resp = raw_client.post(
        "/api/auth/login", json={"username": DEFAULT_USERNAME, "password": "wrong"}
    )
    assert resp.status_code == 401


def test_login_unknown_user_rejected(raw_client):
    resp = raw_client.post(
        "/api/auth/login", json={"username": "nobody", "password": "whatever"}
    )
    assert resp.status_code == 401


def test_me_requires_authentication(raw_client):
    resp = raw_client.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_returns_current_user_once_logged_in(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["username"] == DEFAULT_USERNAME


def test_set_accent_color_saves_on_account_and_returns_it(client):
    resp = client.put("/api/auth/accent-color", json={"accent_color": "#8B5CF6"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accent_color"] == "#8b5cf6"
    # Persisted on the account, so a fresh request (a different device
    # logging back in) reads the same value from /me.
    assert client.get("/api/auth/me").json()["accent_color"] == "#8b5cf6"


def test_set_accent_color_accepts_three_digit_hex(client):
    resp = client.put("/api/auth/accent-color", json={"accent_color": "#abc"})
    assert resp.status_code == 200
    assert resp.json()["accent_color"] == "#abc"


def test_set_accent_color_rejects_non_hex(client):
    for bad in ("blue", "8b5cf6", "#12345", "#1234567", ""):
        resp = client.put("/api/auth/accent-color", json={"accent_color": bad})
        assert resp.status_code == 422, bad


def test_set_accent_color_requires_authentication(raw_client):
    resp = raw_client.put("/api/auth/accent-color", json={"accent_color": "#8b5cf6"})
    assert resp.status_code == 401


def test_logout_invalidates_session(client):
    assert client.get("/api/auth/me").status_code == 200
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_password_change_invalidates_other_sessions(client, session_factory):
    from fastapi.testclient import TestClient

    other = TestClient(client.app)
    assert (
        other.post(
            "/api/auth/login",
            json={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
        ).status_code
        == 200
    )

    resp = client.post(
        "/api/auth/password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": "new-password"},
    )
    assert resp.status_code == 200
    assert client.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401


def test_username_change_succeeds_and_updates_me(client):
    resp = client.post(
        "/api/auth/username",
        json={"current_password": DEFAULT_PASSWORD, "new_username": "renamed_user"},
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "renamed_user"
    assert client.get("/api/auth/me").json()["username"] == "renamed_user"


def test_username_change_wrong_password_rejected(client):
    resp = client.post(
        "/api/auth/username",
        json={"current_password": "wrong", "new_username": "renamed_user"},
    )
    assert resp.status_code == 400
    assert client.get("/api/auth/me").json()["username"] == DEFAULT_USERNAME


def test_username_change_rejects_taken_username(client, db_session):
    from app.models import User
    from app.security import hash_password

    db_session.add(User(username="taken", password_hash=hash_password("x")))
    db_session.commit()

    resp = client.post(
        "/api/auth/username",
        json={"current_password": DEFAULT_PASSWORD, "new_username": "taken"},
    )
    assert resp.status_code == 409
    assert client.get("/api/auth/me").json()["username"] == DEFAULT_USERNAME


def test_username_change_to_same_name_is_a_no_op(client):
    resp = client.post(
        "/api/auth/username",
        json={"current_password": DEFAULT_PASSWORD, "new_username": DEFAULT_USERNAME},
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == DEFAULT_USERNAME


def test_session_token_is_not_stored_in_plaintext(client, db_session):
    from app.models import Session

    cookie = client.cookies.get("atriolyric_session")
    assert cookie
    assert db_session.get(Session, cookie) is None


def test_protected_endpoint_rejects_unauthenticated_requests(raw_client):
    resp = raw_client.get("/api/library/tracks")
    assert resp.status_code == 401


def test_stream_endpoint_requires_authentication(raw_client):
    resp = raw_client.get("/api/stream/1")
    assert resp.status_code == 401


def test_audio_settings_default_and_partial_updates_persist_on_account(client):
    me = client.get("/api/auth/me").json()
    assert (me["volume_leveling"], me["eq_preset"], me["eq_gains"]) == (False, "flat", [0.0] * 10)

    resp = client.put("/api/auth/audio-settings", json={"volume_leveling": True})
    assert resp.json() == {"volume_leveling": True, "eq_preset": "flat", "eq_gains": [0.0] * 10}
    gains = [3, -2, 0, 1.5, 0, 0, -4, 0, 2, 6]
    resp = client.put(
        "/api/auth/audio-settings", json={"eq_preset": "Custom", "eq_gains": gains}
    )
    assert resp.json() == {"volume_leveling": True, "eq_preset": "Custom", "eq_gains": gains}
    # Persisted on the account: visible from /me.
    me = client.get("/api/auth/me").json()
    assert (me["volume_leveling"], me["eq_gains"]) == (True, gains)


def test_audio_settings_clamp_and_validate(client):
    resp = client.put(
        "/api/auth/audio-settings", json={"eq_gains": [20, -20] + [0] * 8}
    )
    assert resp.json()["eq_gains"][:2] == [12.0, -12.0]
    for bad in (
        {"eq_gains": [0] * 9},
        {"eq_gains": [0] * 11},
        {"eq_gains": ["x"] * 10},
        {"eq_preset": ""},
        {"eq_preset": "bad/name"},
        {"volume_leveling": "maybe"},
    ):
        assert client.put("/api/auth/audio-settings", json=bad).status_code == 422, bad


def test_audio_settings_are_per_account(client, session_factory, db_session):
    from app.models import User
    from app.security import hash_password
    from tests.conftest import login_as

    client.put("/api/auth/audio-settings", json={"volume_leveling": True})
    db_session.add(User(username="two", password_hash=hash_password("twopassword")))
    db_session.commit()
    other = login_as(session_factory, client, "two", "twopassword")
    assert other.get("/api/auth/me").json()["volume_leveling"] is False


def test_audio_settings_require_authentication(raw_client):
    assert raw_client.put("/api/auth/audio-settings", json={}).status_code == 401


# --- login limiting (per address and per username) --------------------------------

import logging  # noqa: E402

import pytest  # noqa: E402

from app import (
    login_limits,
    main,  # noqa: E402
)
from app.routers import auth  # noqa: E402


@pytest.fixture()
def addr(monkeypatch):
    """Let a test choose the client address per request, as a trusted proxy
    would set it (uvicorn's proxy middleware is not in the TestClient path)."""
    monkeypatch.setattr(
        login_limits, "login_key", lambda request: request.headers.get("x-test-addr", "198.51.100.1")
    )


def _login(client, username, password, address="198.51.100.1"):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"x-test-addr": address},
    )


@pytest.fixture()
def second_user(db_session):
    from app.models import User
    from app.security import hash_password

    db_session.add(User(username="Bob", password_hash=hash_password("bobs-password")))
    db_session.commit()


def test_address_failures_block_that_address_not_another(raw_client, second_user, addr):
    for _ in range(login_limits.LOGIN_MAX_FAILURES):
        # Many different usernames, so only the address bucket can fill.
        assert _login(raw_client, f"guess{_}", "x", "203.0.113.9").status_code == 401
    blocked = _login(raw_client, "Bob", "bobs-password", "203.0.113.9")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert _login(raw_client, "Bob", "bobs-password", "203.0.113.77").status_code == 200


def test_username_failures_from_many_addresses_lock_that_user_only(raw_client, second_user, addr):
    for i in range(login_limits.USER_MAX_FAILURES):
        assert _login(raw_client, DEFAULT_USERNAME, "wrong", f"203.0.113.{i}").status_code == 401
    # Even the right password, from a fresh address, is refused while locked.
    locked = _login(raw_client, DEFAULT_USERNAME, DEFAULT_PASSWORD, "203.0.113.200")
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) >= 1
    # Case and surrounding spaces do not dodge the lock.
    assert _login(raw_client, f" {DEFAULT_USERNAME.upper()} ", "x", "203.0.113.201").status_code == 429
    assert _login(raw_client, "Bob", "bobs-password", "203.0.113.202").status_code == 200


def test_unknown_usernames_are_limited_the_same_way(raw_client, addr):
    for i in range(login_limits.USER_MAX_FAILURES):
        assert _login(raw_client, "ghost", "x", f"203.0.113.{i}").status_code == 401
    assert _login(raw_client, "ghost", "x", "203.0.113.99").status_code == 429


def test_a_success_clears_only_that_username(raw_client, second_user, addr):
    for _ in range(3):
        _login(raw_client, DEFAULT_USERNAME, "wrong")
    for _ in range(3):
        _login(raw_client, "Bob", "wrong")
    assert _login(raw_client, "Bob", "bobs-password").status_code == 200
    assert "bob" not in login_limits._user_failures
    assert len(login_limits._user_failures[DEFAULT_USERNAME.casefold()]) == 3
    # The address keeps all six failures: logging in as Bob did not reset it.
    assert len(login_limits._login_failures["198.51.100.1"]) == 6


def test_interleaved_successes_cannot_reset_the_address_counter(raw_client, second_user, addr):
    for _ in range(login_limits.LOGIN_MAX_FAILURES):
        assert _login(raw_client, f"u{_}", "x").status_code == 401
        # Alternating with a valid login does not help the attacker.
        login_limits.record_login_result("198.51.100.1", True, "Bob")
    assert _login(raw_client, "Bob", "bobs-password").status_code == 429


def test_refused_attempts_do_not_extend_the_block(raw_client, addr, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(login_limits.time, "monotonic", lambda: now[0])
    for i in range(login_limits.USER_MAX_FAILURES):
        assert _login(raw_client, DEFAULT_USERNAME, "wrong", f"203.0.113.{i}").status_code == 401
        now[0] += 1
    first_failure = 1000.0
    for _ in range(25):
        refused = _login(raw_client, DEFAULT_USERNAME, DEFAULT_PASSWORD, "203.0.113.200")
        assert refused.status_code == 429
        # The block ends when the oldest recorded failure leaves the window.
        expected = first_failure + login_limits.LOGIN_WINDOW_SECONDS - now[0]
        assert int(refused.headers["Retry-After"]) == max(1, int(expected + 0.999))
        now[0] += 1
    assert len(login_limits._user_failures[DEFAULT_USERNAME]) == login_limits.USER_MAX_FAILURES
    assert len(login_limits._login_failures["203.0.113.200"]) == 0
    now[0] = first_failure + login_limits.LOGIN_WINDOW_SECONDS + login_limits.USER_MAX_FAILURES + 1
    assert _login(raw_client, DEFAULT_USERNAME, DEFAULT_PASSWORD, "203.0.113.200").status_code == 200


def test_refused_attempts_do_not_extend_the_address_block(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(login_limits.time, "monotonic", lambda: now[0])
    for _ in range(login_limits.LOGIN_MAX_FAILURES):
        login_limits.record_login_result("a", False)
    for _ in range(10):
        now[0] += 10
        with pytest.raises(auth.HTTPException):
            login_limits.check_login_rate_limit("a")
    assert len(login_limits._login_failures["a"]) == login_limits.LOGIN_MAX_FAILURES
    now[0] = 1000.0 + login_limits.LOGIN_WINDOW_SECONDS + 1
    login_limits.check_login_rate_limit("a")


def test_both_tables_stay_bounded(monkeypatch):
    monkeypatch.setattr(login_limits, "LOGIN_MAX_TRACKED_KEYS", 50)
    for i in range(500):
        login_limits.check_login_rate_limit(f"addr{i}", f"user{i}")
        login_limits.record_login_result(f"addr{i}", False, f"user{i}")
        assert len(login_limits._login_failures) <= 50
        assert len(login_limits._user_failures) <= 50
    assert "user499" in login_limits._user_failures  # newest kept, oldest evicted
    assert "user0" not in login_limits._user_failures


def test_username_keys_are_bounded_in_length():
    login_limits.record_login_result("a", False, "x" * 100_000)
    assert all(len(key) <= login_limits.USER_KEY_MAX_LENGTH for key in login_limits._user_failures)


def test_unknown_username_is_logged_as_a_hash_not_as_typed(raw_client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    _login(raw_client, "my-secret-password-typed-here", "x")
    _login(raw_client, DEFAULT_USERNAME, "x")
    lines = [r.getMessage() for r in caplog.records if r.name == "app.activity"]
    failed = [line for line in lines if line.startswith("auth.login_failed")]
    assert len(failed) == 2
    assert "my-secret-password-typed-here" not in " ".join(lines)
    assert 'username="unknown:' in failed[0]
    assert f'username="{DEFAULT_USERNAME}"' in failed[1]


# --- the proxy warning --------------------------------------------------------------


@pytest.fixture()
def fresh_proxy_warning(monkeypatch):
    monkeypatch.setattr(main, "_forwarded_for_warned", False)


def test_untrusted_forwarded_for_warns_once(fresh_proxy_warning, caplog):
    caplog.set_level(logging.WARNING, logger="app")
    # The peer (the proxy) is not among the addresses in the header.
    assert main._warn_if_forwarded_for_untrusted("198.51.100.7", "172.18.0.3") is True
    assert main._warn_if_forwarded_for_untrusted("198.51.100.8", "172.18.0.3") is False
    warnings = [r.getMessage() for r in caplog.records if "FORWARDED_ALLOW_IPS" in r.getMessage()]
    assert len(warnings) == 1 and "172.18.0.3" in warnings[0]


def test_trusted_or_absent_forwarded_for_does_not_warn(fresh_proxy_warning, caplog):
    caplog.set_level(logging.WARNING, logger="app")
    # uvicorn already replaced the peer with the header's client address.
    assert main._warn_if_forwarded_for_untrusted("198.51.100.7, 10.0.0.2", "198.51.100.7") is False
    assert main._warn_if_forwarded_for_untrusted(None, "172.18.0.3") is False
    assert main._warn_if_forwarded_for_untrusted("", "172.18.0.3") is False
    assert not caplog.records


def test_the_middleware_triggers_the_warning(raw_client, fresh_proxy_warning, caplog):
    caplog.set_level(logging.WARNING, logger="app")
    raw_client.get("/api/health", headers={"X-Forwarded-For": "198.51.100.7"})
    raw_client.get("/api/health", headers={"X-Forwarded-For": "198.51.100.7"})
    assert sum("FORWARDED_ALLOW_IPS" in r.getMessage() for r in caplog.records) == 1
    assert main._forwarded_for_warned is True


def test_usernames_with_control_characters_are_rejected_on_rename(client):
    for bad in ("evil\nname", "tab\there", "zero​width"):
        resp = client.post(
            "/api/auth/username", json={"current_password": DEFAULT_PASSWORD, "new_username": bad}
        )
        assert resp.status_code == 422, bad
        assert "control" in resp.text
