from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import login_limits, main
from app.config import settings
from app.models import Base, User
from app.routers import setup
from tests.conftest import startup_against


@pytest.fixture(autouse=True)
def _fresh_setup_state():
    setup._setup_code = None
    login_limits._login_failures.clear()
    yield
    setup._setup_code = None
    login_limits._login_failures.clear()


@pytest.fixture()
def empty_client(raw_client, db_session):
    """An app client whose database has no users at all."""
    db_session.query(User).delete()
    db_session.commit()
    return raw_client


def _seed(tmp_path, *, users=(), password=None):
    engine = create_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as db:
        db.add_all(User(username=name, password_hash="unused", is_admin=True) for name in users)
        db.commit()
    with startup_against(factory), patch.object(settings, "default_password", password):
        main.seed_default_user()
    with factory() as db:
        return [(u.username, u.is_admin) for u in db.query(User).all()]


def test_empty_database_without_env_password_gets_a_logged_setup_code(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="app.routers.setup")
    assert _seed(tmp_path) == []
    assert setup._setup_code
    assert setup._setup_code in caplog.text
    # Issued once per process, not per call.
    code = setup._setup_code
    assert setup.issue_setup_code() == code


def test_env_password_still_bootstraps_headless_installs(tmp_path):
    assert _seed(tmp_path, password="from-the-env") == [("admin", True)]
    assert setup._setup_code is None


def test_existing_database_never_gets_a_setup_code(tmp_path):
    assert _seed(tmp_path, users=["owner"]) == [("owner", True)]
    assert setup._setup_code is None


def test_status_reports_whether_setup_is_needed(empty_client):
    assert empty_client.get("/api/setup/status").json() == {"needs_setup": True}
    # A status check on an empty database makes sure a code exists.
    assert setup._setup_code is not None


def test_status_is_false_once_a_user_exists(raw_client):
    # The client's own startup ran against the (empty) global test database.
    setup._setup_code = None
    assert raw_client.get("/api/setup/status").json() == {"needs_setup": False}
    assert setup._setup_code is None


def test_wrong_code_is_refused(empty_client):
    setup.issue_setup_code()
    resp = empty_client.post(
        "/api/setup", json={"code": "not-it", "username": "me", "password": "long-enough"}
    )
    assert resp.status_code == 403
    assert empty_client.get("/api/setup/status").json() == {"needs_setup": True}


def test_wrong_codes_are_rate_limited(empty_client, monkeypatch):
    setup.issue_setup_code()
    monkeypatch.setattr(login_limits, "LOGIN_MAX_FAILURES", 2)
    body = {"code": "nope", "username": "me", "password": "long-enough"}
    assert empty_client.post("/api/setup", json=body).status_code == 403
    assert empty_client.post("/api/setup", json=body).status_code == 403
    assert empty_client.post("/api/setup", json=body).status_code == 429


def test_right_code_creates_an_admin_and_logs_in(empty_client, db_session):
    code = setup.issue_setup_code()
    resp = empty_client.post(
        "/api/setup", json={"code": f" {code} ", "username": " owner ", "password": "long-enough"}
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "owner"
    assert resp.json()["is_admin"] is True
    me = empty_client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["username"] == "owner"
    assert setup._setup_code is None

    # A second attempt, even with the old code, is refused.
    again = empty_client.post(
        "/api/setup", json={"code": code, "username": "intruder", "password": "long-enough"}
    )
    assert again.status_code == 409
    assert db_session.query(User).count() == 1


def test_setup_validates_the_new_account(empty_client):
    code = setup.issue_setup_code()
    body = {"code": code, "username": "   ", "password": "long-enough"}
    assert empty_client.post("/api/setup", json=body).status_code == 422
    assert empty_client.post("/api/setup", json={**body, "username": "me", "password": "short"}).status_code == 422
    with patch("app.routers.setup.hash_password", side_effect=ValueError("too weak")):
        assert empty_client.post("/api/setup", json={**body, "username": "me"}).status_code == 422
    assert setup._setup_code == code


def test_setup_uses_only_the_address_bucket(empty_client, monkeypatch):
    setup.issue_setup_code()
    body = {"code": "nope", "username": "me", "password": "long-enough"}
    assert empty_client.post("/api/setup", json=body).status_code == 403
    assert list(login_limits._user_failures) == []
    assert sum(len(f) for f in login_limits._login_failures.values()) == 1


def test_setup_rejects_control_characters_in_the_username(empty_client):
    code = setup.issue_setup_code()
    resp = empty_client.post(
        "/api/setup", json={"code": code, "username": "ad\nmin", "password": "long-enough"}
    )
    assert resp.status_code == 422
    assert empty_client.get("/api/setup/status").json() == {"needs_setup": True}
