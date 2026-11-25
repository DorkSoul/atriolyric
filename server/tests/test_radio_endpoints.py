from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

from app import radio
from app.config import settings
from app.models import User
from app.security import hash_password
from tests.conftest import login_as


class FakeSock:
    """A connected socket that sends `data` (then EOF) and records requests."""

    def __init__(self, data: bytes = b""):
        self.data = data
        self.sent = b""
        self.closed = False
        self.on_recv = None

    def settimeout(self, _t):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        if self.on_recv:
            self.on_recv()
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk

    def close(self):
        self.closed = True


def _make_plain(db_session, session_factory, client, username="plain"):
    user = User(username=username, password_hash=hash_password("x"))
    db_session.add(user)
    db_session.commit()
    return login_as(session_factory, client, username, "x")


def test_admin_can_create_shared_station(client):
    resp = client.post(
        "/api/radio/stations",
        json={
            "name": "Groove Salad",
            "stream_url": "http://ice1.somafm.com/groovesalad-128-mp3",
            "is_shared": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["is_shared"] is True
    assert resp.json()["owner_user_id"] is None


def test_plain_user_cannot_create_shared_station(session_factory, client, db_session):
    plain = _make_plain(db_session, session_factory, client)
    resp = plain.post(
        "/api/radio/stations",
        json={
            "name": "Station",
            "stream_url": "http://example.com/stream",
            "is_shared": True,
        },
    )
    assert resp.status_code == 403


def test_plain_user_can_create_private_station(session_factory, client, db_session):
    plain = _make_plain(db_session, session_factory, client)
    resp = plain.post(
        "/api/radio/stations",
        json={
            "name": "My Station",
            "stream_url": "http://example.com/stream",
            "is_shared": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_shared"] is False
    assert body["owner_user_id"] is not None


def test_station_url_rejects_non_http_and_private_destinations(client):
    for url in ("file:///etc/passwd", "http://127.0.0.1:8000/stream", "http://[::1]/"):
        resp = client.post(
            "/api/radio/stations",
            json={"name": "Unsafe", "stream_url": url, "is_shared": True},
        )
        assert resp.status_code == 422


def test_private_station_invisible_to_other_users(session_factory, client, db_session):
    plain_a = _make_plain(db_session, session_factory, client, "plain_a")
    plain_a.post(
        "/api/radio/stations",
        json={
            "name": "Private A",
            "stream_url": "http://example.com/a",
            "is_shared": False,
        },
    )
    plain_b = _make_plain(db_session, session_factory, client, "plain_b")

    stations = plain_b.get("/api/radio/stations").json()
    assert "Private A" not in [s["name"] for s in stations]

    a_stations = plain_a.get("/api/radio/stations").json()
    assert "Private A" in [s["name"] for s in a_stations]


def test_shared_station_visible_to_everyone(session_factory, client, db_session):
    client.post(
        "/api/radio/stations",
        json={
            "name": "Shared One",
            "stream_url": "http://example.com/s",
            "is_shared": True,
        },
    )
    plain = _make_plain(db_session, session_factory, client)
    stations = plain.get("/api/radio/stations").json()
    assert "Shared One" in [s["name"] for s in stations]


def test_delete_station_requires_ownership_or_admin(
    session_factory, client, db_session
):
    plain_a = _make_plain(db_session, session_factory, client, "plain_a")
    created = plain_a.post(
        "/api/radio/stations",
        json={"name": "Mine", "stream_url": "http://example.com/a", "is_shared": False},
    ).json()

    plain_b = _make_plain(db_session, session_factory, client, "plain_b")
    resp = plain_b.delete(f"/api/radio/stations/{created['id']}")
    assert resp.status_code == 403

    resp = plain_a.delete(f"/api/radio/stations/{created['id']}")
    assert resp.status_code == 200


def test_now_playing_returns_cached_title(client):
    station = client.post(
        "/api/radio/stations",
        json={
            "name": "Station",
            "stream_url": "http://example.com/s",
            "is_shared": True,
        },
    ).json()

    with patch(
        "app.routers.radio.get_now_playing",
        return_value={"title": "Artist - Track", "updated_at": 123.0},
    ):
        resp = client.get(f"/api/radio/stations/{station['id']}/now-playing")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Artist - Track"


def test_now_playing_defaults_when_nothing_cached_yet(client):
    station = client.post(
        "/api/radio/stations",
        json={
            "name": "Station",
            "stream_url": "http://example.com/s",
            "is_shared": True,
        },
    ).json()
    resp = client.get(f"/api/radio/stations/{station['id']}/now-playing")
    assert resp.status_code == 200
    assert resp.json() == {"title": None, "updated_at": None}


def test_stream_endpoint_proxies_audio_bytes(client):
    station = client.post(
        "/api/radio/stations",
        json={
            "name": "Station",
            "stream_url": "http://example.com/s",
            "is_shared": True,
        },
    ).json()

    upstream = radio.Upstream(FakeSock(b"def"), {}, b"abc")
    with patch("app.routers.radio.open_stream", return_value=upstream):
        resp = client.get(f"/api/radio/stations/{station['id']}/stream")
    assert resp.status_code == 200
    assert resp.content == b"abcdef"
    assert resp.headers["content-type"] == "audio/mpeg"
    assert upstream.sock.closed


def test_radio_want_adds_a_song_to_the_want_list(client):
    # The radio "want" button; the list itself is read and edited through
    # /api/want-list.
    resp = client.post(
        "/api/radio/want-list",
        json={
            "artist": "Kalabi",
            "title": "Inside The Sun",
            "station_name": "Groove Salad",
        },
    )
    assert resp.status_code == 200
    item = resp.json()

    listed = client.get("/api/want-list/entries").json()
    assert [(e["id"], e["kind"], e["artist"], e["station_name"]) for e in listed] == [
        (item["id"], "track", "Kalabi", "Groove Salad")
    ]
    assert client.get("/api/radio/want-list").status_code == 405
    assert client.delete(f"/api/radio/want-list/{item['id']}").status_code == 404


def test_want_list_scoped_per_user(session_factory, client, db_session):
    client.post(
        "/api/radio/want-list", json={"artist": "A", "title": "T", "station_name": None}
    )
    plain = _make_plain(db_session, session_factory, client)
    assert plain.get("/api/want-list/entries").json() == []


def test_radio_endpoints_require_authentication(raw_client):
    assert raw_client.get("/api/radio/stations").status_code == 401
    assert raw_client.post(
        "/api/radio/want-list", json={"artist": "A", "title": "T"}
    ).status_code == 401


def test_list_stations_defaults_to_not_favorite(client):
    station = client.post(
        "/api/radio/stations",
        json={"name": "Station", "stream_url": "http://example.com/s", "is_shared": True},
    ).json()
    assert station["is_favorite"] is False
    listed = client.get("/api/radio/stations").json()
    assert next(s for s in listed if s["id"] == station["id"])["is_favorite"] is False


def test_favorite_and_unfavorite_a_station(client):
    station = client.post(
        "/api/radio/stations",
        json={"name": "Station", "stream_url": "http://example.com/s", "is_shared": True},
    ).json()

    resp = client.put(f"/api/radio/stations/{station['id']}/favorite")
    assert resp.status_code == 200
    assert resp.json() == {"is_favorite": True}
    listed = client.get("/api/radio/stations").json()
    assert next(s for s in listed if s["id"] == station["id"])["is_favorite"] is True

    resp = client.delete(f"/api/radio/stations/{station['id']}/favorite")
    assert resp.status_code == 200
    assert resp.json() == {"is_favorite": False}
    listed = client.get("/api/radio/stations").json()
    assert next(s for s in listed if s["id"] == station["id"])["is_favorite"] is False


def test_favoriting_a_station_is_idempotent(client):
    station = client.post(
        "/api/radio/stations",
        json={"name": "Station", "stream_url": "http://example.com/s", "is_shared": True},
    ).json()
    assert client.put(f"/api/radio/stations/{station['id']}/favorite").status_code == 200
    assert client.put(f"/api/radio/stations/{station['id']}/favorite").status_code == 200
    listed = client.get("/api/radio/stations").json()
    assert next(s for s in listed if s["id"] == station["id"])["is_favorite"] is True


def test_favorites_are_scoped_per_user(session_factory, client, db_session):
    station = client.post(
        "/api/radio/stations",
        json={"name": "Station", "stream_url": "http://example.com/s", "is_shared": True},
    ).json()
    client.put(f"/api/radio/stations/{station['id']}/favorite")

    plain = _make_plain(db_session, session_factory, client)
    listed = plain.get("/api/radio/stations").json()
    assert next(s for s in listed if s["id"] == station["id"])["is_favorite"] is False


def test_cannot_favorite_a_station_you_cannot_see(session_factory, client, db_session):
    plain_a = _make_plain(db_session, session_factory, client, "plain_a")
    private = plain_a.post(
        "/api/radio/stations",
        json={"name": "Private", "stream_url": "http://example.com/p", "is_shared": False},
    ).json()

    plain_b = _make_plain(db_session, session_factory, client, "plain_b")
    resp = plain_b.put(f"/api/radio/stations/{private['id']}/favorite")
    assert resp.status_code == 404



# --- address choice, errors before headers, redirects, caps ----------------

V4 = "93.184.216.34"
V6 = "2606:2800:220:1:248:1893:25c8:1946"


class Station:
    """Scripted DNS and servers: `servers[(address, port)]` is a list of raw
    responses, one per connection made to it."""

    def __init__(self, monkeypatch, dns: dict[str, list[str]], servers: dict):
        self.attempts: list[str] = []
        self.sockets: list[FakeSock] = []
        self.servers = servers

        def getaddrinfo(host, port, type=None):
            if host not in dns:
                raise OSError("no such host")
            return [(None, None, None, None, (address, port)) for address in dns[host]]

        def create_connection(address, timeout=None):
            self.attempts.append(address[0])
            responses = self.servers.get(address)
            if not responses:
                raise OSError("unreachable")
            sock = FakeSock(responses.pop(0))
            self.sockets.append(sock)
            return sock

        monkeypatch.setattr("app.radio.socket.getaddrinfo", getaddrinfo)
        monkeypatch.setattr("app.radio.socket.create_connection", create_connection)


def _station(client, url="http://radio.example/live"):
    return client.post(
        "/api/radio/stations", json={"name": "S", "stream_url": url, "is_shared": True}
    ).json()["id"]


OK = b"HTTP/1.0 200 OK\r\nContent-Type: audio/aacp\r\n\r\naudio-bytes"


def test_dual_stack_station_connects_over_ipv4_first(client, monkeypatch):
    station = _station(client)
    fake = Station(monkeypatch, {"radio.example": [V6, V4]}, {(V4, 80): [OK]})
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.status_code == 200 and resp.content == b"audio-bytes"
    assert fake.attempts == [V4]  # resolver listed IPv6 first; IPv4 is tried first
    assert resp.headers["content-type"] == "audio/aacp"
    request = fake.sockets[0].sent.decode()
    assert f"User-Agent: {settings.user_agent}\r\n" in request
    assert "Icy-MetaData: 1\r\n" in request and "Host: radio.example\r\n" in request


def test_unreachable_first_address_falls_back_to_the_next(client, monkeypatch):
    station = _station(client)
    fake = Station(monkeypatch, {"radio.example": [V4, V6]}, {(V6, 80): [OK]})
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.status_code == 200 and fake.attempts == [V4, V6]


def test_a_private_address_in_any_answer_is_refused(client, monkeypatch):
    station = _station(client)
    fake = Station(monkeypatch, {"radio.example": [V4, "fd00::1"]}, {(V4, 80): [OK]})
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.status_code == 502 and "private" in resp.json()["detail"]
    assert fake.attempts == []


def test_upstream_404_is_a_404_before_any_audio(client, monkeypatch):
    station = _station(client)
    fake = Station(
        monkeypatch, {"radio.example": [V4]}, {(V4, 80): [b"HTTP/1.0 404 Not Found\r\n\r\ngone"]}
    )
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/json"
    assert fake.sockets[0].closed


def test_connect_failure_and_dns_failure_are_502(client, monkeypatch):
    station = _station(client)
    Station(monkeypatch, {"radio.example": [V4]}, {})
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.status_code == 502 and resp.json()["detail"] == "Could not connect to the radio station"
    Station(monkeypatch, {}, {})
    assert client.get(f"/api/radio/stations/{station}/stream").status_code == 502


def test_upstream_error_status_and_timeout_are_502(client, monkeypatch):
    station = _station(client)
    fake = Station(monkeypatch, {"radio.example": [V4]}, {(V4, 80): [b"HTTP/1.0 500 Oops\r\n\r\n", OK]})
    assert client.get(f"/api/radio/stations/{station}/stream").status_code == 502

    def stalls():
        raise TimeoutError("timed out")

    fake.servers[(V4, 80)] = [b""]
    real = radio.socket.create_connection

    def stalling(address, timeout=None):
        sock = real(address, timeout)
        sock.on_recv = stalls
        return sock

    monkeypatch.setattr("app.radio.socket.create_connection", stalling)
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.status_code == 502 and fake.sockets[-1].closed


def test_redirects_are_followed_and_revalidated(client, monkeypatch):
    station = _station(client)
    fake = Station(
        monkeypatch,
        {"radio.example": [V4], "cdn.example": ["93.184.216.35"]},
        {
            (V4, 80): [b"HTTP/1.0 302 Found\r\nLocation: https://cdn.example/s\r\n\r\n"],
            ("93.184.216.35", 443): [OK],
        },
    )

    class Context:
        def wrap_socket(self, sock, server_hostname):
            assert server_hostname == "cdn.example"
            return sock

    monkeypatch.setattr("app.radio.ssl.create_default_context", Context)
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.status_code == 200 and resp.content == b"audio-bytes"
    assert fake.sockets[0].closed


def test_a_redirect_to_a_private_address_is_refused(client, monkeypatch):
    station = _station(client)
    fake = Station(
        monkeypatch,
        {"radio.example": [V4], "inside.example": ["192.168.1.10"]},
        {
            (V4, 80): [b"HTTP/1.0 301 Moved\r\nLocation: http://inside.example/admin\r\n\r\n"],
            ("192.168.1.10", 80): [OK],
        },
    )
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.status_code == 502 and "private" in resp.json()["detail"]
    assert fake.attempts == [V4]
    for location in ("file:///etc/passwd", "http://127.0.0.1/x", "http://u:p@radio.example/"):
        fake.servers[(V4, 80)] = [f"HTTP/1.0 302 Found\r\nLocation: {location}\r\n\r\n".encode()]
        assert client.get(f"/api/radio/stations/{station}/stream").status_code == 502


def test_a_redirect_chain_longer_than_three_is_refused(client, monkeypatch):
    station = _station(client)
    hop = b"HTTP/1.0 307 Again\r\nLocation: /next\r\n\r\n"
    fake = Station(monkeypatch, {"radio.example": [V4]}, {(V4, 80): [hop] * 4 + [OK]})
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.status_code == 502 and "redirected too many times" in resp.json()["detail"]
    assert len(fake.attempts) == 4 and all(sock.closed for sock in fake.sockets)
    fake.servers[(V4, 80)] = [hop] * 3 + [OK]
    assert client.get(f"/api/radio/stations/{station}/stream").status_code == 200
    fake.servers[(V4, 80)] = [b"HTTP/1.0 302 Found\r\n\r\n"]
    assert client.get(f"/api/radio/stations/{station}/stream").status_code == 502


def test_a_pls_playlist_resolves_to_its_first_stream(client, monkeypatch):
    station = _station(client, "http://radio.example/listen.pls")
    pls = (
        b"HTTP/1.0 200 OK\r\nContent-Type: audio/x-scpls\r\n\r\n"
        b"[playlist]\nNumberOfEntries=2\nFile1=http://stream.example:8000/live\nTitle1=Live\nFile2=http://x.example/\n"
    )
    fake = Station(
        monkeypatch,
        {"radio.example": [V4], "stream.example": ["93.184.216.36"]},
        {(V4, 80): [pls], ("93.184.216.36", 8000): [OK]},
    )
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.status_code == 200 and resp.content == b"audio-bytes"
    assert "Host: stream.example:8000\r\n" in fake.sockets[1].sent.decode()


def test_an_m3u_by_extension_resolves_and_bad_playlists_are_502(client, monkeypatch):
    station = _station(client, "http://radio.example/listen.m3u")
    m3u = b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\n#EXTM3U\n#EXTINF:-1,Live\nlive.mp3\n"
    Station(monkeypatch, {"radio.example": [V4]}, {(V4, 80): [m3u, OK]})
    assert client.get(f"/api/radio/stations/{station}/stream").status_code == 200

    for body in (b"#EXTM3U\n", b"#EXTM3U\n#EXT-X-TARGETDURATION:6\nseg0.ts\n", b"x" * (radio.PLAYLIST_MAX_BYTES + 10)):
        Station(monkeypatch, {"radio.example": [V4]}, {(V4, 80): [b"HTTP/1.0 200 OK\r\n\r\n" + body]})
        assert client.get(f"/api/radio/stations/{station}/stream").status_code == 502


def test_non_audio_content_type_falls_back_to_mpeg(client, monkeypatch):
    station = _station(client)
    Station(monkeypatch, {"radio.example": [V4]}, {(V4, 80): [b"ICY 200 OK\r\nContent-Type: text/html\r\n\r\nzz"]})
    resp = client.get(f"/api/radio/stations/{station}/stream")
    assert resp.headers["content-type"] == "audio/mpeg" and resp.content == b"zz"


def test_stream_caps_answer_429_and_release(client, monkeypatch):
    station = _station(client)
    Station(monkeypatch, {"radio.example": [V4]}, {(V4, 80): [OK, OK]})
    user_id = client.get("/api/auth/me").json()["id"]
    with radio.stream_slot(user_id), radio.stream_slot(user_id):
        resp = client.get(f"/api/radio/stations/{station}/stream")
        assert resp.status_code == 429 and "already listening to 2" in resp.json()["detail"]
    assert client.get(f"/api/radio/stations/{station}/stream").status_code == 200
    assert radio._streams_by_user == {}  # released when the stream ended
    with ExitStack() as stack:
        for other in range(100, 100 + radio.MAX_STREAMS_TOTAL):
            stack.enter_context(radio.stream_slot(other))
        resp = client.get(f"/api/radio/stations/{station}/stream")
        assert resp.status_code == 429 and "as many radio streams" in resp.json()["detail"]
    assert radio._streams_by_user == {}
    # A failed connection releases its slot too.
    Station(monkeypatch, {"radio.example": [V4]}, {})
    assert client.get(f"/api/radio/stations/{station}/stream").status_code == 502
    assert radio._streams_by_user == {}


def test_stream_releases_its_database_connection_while_streaming(client, monkeypatch, session_factory):
    """The stream does not hold its pooled connection while it plays."""
    station = _station(client)
    fake = Station(monkeypatch, {"radio.example": [V4]}, {(V4, 80): [OK]})
    engine = session_factory.kw["bind"]
    baseline = engine.pool.checkedout()
    seen = []
    real = radio.socket.create_connection

    def watching(address, timeout=None):
        sock = real(address, timeout)
        sock.on_recv = lambda: seen.append(engine.pool.checkedout())
        return sock

    monkeypatch.setattr("app.radio.socket.create_connection", watching)
    assert client.get(f"/api/radio/stations/{station}/stream").status_code == 200
    assert seen and max(seen) <= baseline
    assert fake.attempts == [V4]


def test_client_disconnect_closes_the_upstream_and_releases_the_slot():
    """Starlette abandons the body iterator when the client goes away; the
    cleanup must still close the socket and free the slot (not wait for GC)."""
    import asyncio

    sock = FakeSock(b"y" * 100_000)
    upstream = radio.Upstream(sock, {}, b"x")
    slot = ExitStack()
    slot.enter_context(radio.stream_slot(7))
    chunks = radio.relay_icy(1, upstream)

    def cleanup():
        chunks.close()
        upstream.close()
        slot.close()

    async def listen_briefly():
        body = radio.iterate_then_cleanup(chunks, cleanup)
        assert await anext(body) == b"x"
        await body.aclose()  # what the disconnect does to the response

    asyncio.run(listen_briefly())
    assert sock.closed and radio._streams_by_user == {}
