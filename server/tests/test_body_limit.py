from __future__ import annotations

import json

import pytest

from app import body_limit, login_limits
from app.custom_art import MAX_UPLOAD_BYTES
from tests.conftest import DEFAULT_PASSWORD, DEFAULT_USERNAME
from tests.test_custom_art import _isolated_dirs, _png, library  # noqa: F401 - fixtures

TWO_MIB = 2 * 1024 * 1024


@pytest.fixture()
def handler_spy(monkeypatch):
    """Records whether the login handler's first step ever ran."""
    calls = []
    real = login_limits.check_login_rate_limit

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(login_limits, "check_login_rate_limit", spy)
    return calls


def test_limit_for_defaults_and_the_explicit_override():
    assert body_limit.limit_for("POST", "/api/auth/login") == body_limit.DEFAULT_LIMIT == 1024 * 1024
    assert body_limit.limit_for("PUT", "/api/art/media_item/3") > MAX_UPLOAD_BYTES
    assert body_limit.limit_for("GET", "/api/art/media_item/3") == body_limit.DEFAULT_LIMIT
    assert body_limit.limit_for("POST", "/api/playlists/4/items") == body_limit.DEFAULT_LIMIT


def test_declared_oversized_body_is_refused_before_the_handler(raw_client, handler_spy):
    body = json.dumps({"username": DEFAULT_USERNAME, "password": "x" * TWO_MIB})
    resp = raw_client.post(
        "/api/auth/login", content=body, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413
    assert handler_spy == []


def test_chunked_body_over_the_limit_is_cut_off(raw_client, handler_spy):
    sent = []

    def chunks():
        for _ in range(32):
            sent.append(1)
            yield b" " * 65536

    resp = raw_client.post(
        "/api/auth/login", content=chunks(), headers={"content-type": "application/json"}
    )
    assert resp.status_code == 413
    assert handler_spy == []


def test_a_normal_body_still_works(raw_client):
    resp = raw_client.post(
        "/api/auth/login", json={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 200


def test_art_upload_over_the_default_but_under_its_own_cap_works(client, library):  # noqa: F811
    big = _png() + b"\0" * TWO_MIB
    assert len(big) > body_limit.DEFAULT_LIMIT
    resp = client.put(
        f"/api/art/media_item/{library['A1']}", content=big, headers={"content-type": "image/png"}
    )
    assert resp.status_code == 200


def test_art_upload_over_its_own_cap_is_still_refused(client, library):  # noqa: F811
    resp = client.put(
        f"/api/art/media_item/{library['A1']}",
        content=_png() + b"\0" * (MAX_UPLOAD_BYTES + 128 * 1024),
    )
    assert resp.status_code == 413


def test_websockets_pass_through_untouched(client):
    with client.websocket_connect("/ws/playback") as ws:
        ws.send_json({"type": "register_device", "name": "Laptop"})
        assert ws.receive_json()["type"] == "registered"


# --- input bounds ------------------------------------------------------------------

BAD_MBID = "not-a-uuid"
GOOD_MBID = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"


@pytest.mark.parametrize(
    "path",
    [
        "/api/library/tracks?q={long}",
        "/api/library/tracks?artist={long}",
        "/api/library/tracks?album={long}",
        "/api/library/tracks?genre={long}",
        "/api/library/albums?q={long}",
        "/api/library/artists?q={long}",
        "/api/library/videos?q={long}",
        "/api/library/tracks?offset=99999999",
        "/api/preferences/album?album={long}",
    ],
)
def test_overlong_or_absurd_query_parameters_are_422(client, path):
    assert client.get(path.format(long="x" * 1500)).status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        f"/api/catalog/artist?mbid={BAD_MBID}",
        f"/api/catalog/artist/refresh-status?mbid={BAD_MBID}",
        f"/api/catalog/release-group/{BAD_MBID}",
        f"/api/catalog/art/artist/{BAD_MBID}",
        f"/api/want-list/artist?name=x&mbid={BAD_MBID}",
    ],
)
def test_malformed_mbids_are_422(client, path):
    assert client.get(path).status_code == 422


def test_malformed_mbids_in_bodies_are_422(client):
    want = {"kind": "artist", "artist": "A", "artist_mbid": BAD_MBID}
    assert client.post("/api/want-list", json=want).status_code == 422
    assert client.put("/api/library/artist-match", json={"name": "A", "mbid": BAD_MBID}).status_code == 422
    edition = {"album": "A", "release_mbid": BAD_MBID}
    assert client.put("/api/library/album-edition", json=edition).status_code == 422


def test_a_well_formed_mbid_passes_validation(client):
    resp = client.get(f"/api/catalog/artist/refresh-status?mbid={GOOD_MBID}")
    assert resp.status_code == 200


def test_bounded_bodies(client):
    mix = {"include": [{"kind": "genre", "name": "x"}] * 201}
    assert client.post("/api/library/mixes/specified", json=mix).status_code == 422
    huge_source = {"kind": "library", "label": "x", "payload": {"filters": {"q": "x" * 5000}}}
    assert client.post("/api/library/play-source", json=huge_source).status_code == 422
    rules = {"separators": ["x" * 101], "exceptions": []}
    assert client.put("/api/admin/artist-split-rules", json=rules).status_code == 422
    debug = {"session": "s", "events": [{"at": "t", "event": "e", "fields": {str(i): 1 for i in range(61)}}]}
    assert client.post("/api/client-log/debug", json=debug).status_code == 422
