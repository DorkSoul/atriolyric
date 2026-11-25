from __future__ import annotations

from app.models import User
from app.security import hash_password
from tests.conftest import login_as, make_audio_fixture, wait_until


def _setup_shared_and_private(client, session_factory, db_session, tmp_path):
    shared_dir = tmp_path / "shared"
    private_dir = tmp_path / "private"
    shared_dir.mkdir()
    private_dir.mkdir()

    make_audio_fixture(
        shared_dir / "s.wav", title="Shared Song", artist="A", album="X", genre="Rock"
    )
    make_audio_fixture(
        private_dir / "p.wav", title="Private Song", artist="B", album="Y", genre="Jazz"
    )

    shared_root = client.post(
        "/api/admin/roots",
        json={"path": str(shared_dir), "display_name": "Shared", "is_shared": True},
    ).json()
    private_root = client.post(
        "/api/admin/roots",
        json={"path": str(private_dir), "display_name": "Private", "is_shared": False},
    ).json()

    root_ids = (shared_root["id"], private_root["id"])
    for root_id in root_ids:
        assert client.post(f"/api/admin/roots/{root_id}/scan").status_code == 202
    def scans_done() -> bool:
        roots = {root["id"]: root for root in client.get("/api/admin/roots").json()}
        return all(roots[root_id]["last_task"]["status"] != "running" for root_id in root_ids)

    wait_until(scans_done, what="both scans")

    plain = User(username="plain", password_hash=hash_password("plainpass"))
    db_session.add(plain)
    db_session.commit()
    db_session.refresh(plain)

    plain_client = login_as(session_factory, client, "plain", "plainpass")
    return shared_root, private_root, plain_client


def test_user_without_grant_only_sees_shared_root(
    session_factory, client, db_session, tmp_path
):
    _, private_root, plain_client = _setup_shared_and_private(
        client, session_factory, db_session, tmp_path
    )

    titles = {t["title"] for t in plain_client.get("/api/library/tracks").json()}
    assert titles == {"Shared Song"}


def test_granted_user_sees_both_roots(session_factory, client, db_session, tmp_path):
    _, private_root, plain_client = _setup_shared_and_private(
        client, session_factory, db_session, tmp_path
    )
    plain_id = plain_client.get("/api/auth/me").json()["id"]
    client.post(f"/api/admin/roots/{private_root['id']}/access/{plain_id}")

    titles = {t["title"] for t in plain_client.get("/api/library/tracks").json()}
    assert titles == {"Shared Song", "Private Song"}


def test_ungranted_user_cannot_stream_private_track(
    session_factory, client, db_session, tmp_path
):
    _, private_root, plain_client = _setup_shared_and_private(
        client, session_factory, db_session, tmp_path
    )
    admin_id = client.get("/api/auth/me").json()["id"]
    client.post(f"/api/admin/roots/{private_root['id']}/access/{admin_id}")
    private_track_id = next(
        t["id"]
        for t in client.get("/api/library/tracks").json()
        if t["title"] == "Private Song"
    )

    resp = plain_client.get(f"/api/stream/{private_track_id}")
    assert resp.status_code == 404


def test_ungranted_user_cannot_add_private_track_to_playlist(
    session_factory, client, db_session, tmp_path
):
    _, private_root, plain_client = _setup_shared_and_private(
        client, session_factory, db_session, tmp_path
    )
    admin_id = client.get("/api/auth/me").json()["id"]
    client.post(f"/api/admin/roots/{private_root['id']}/access/{admin_id}")
    private_track_id = next(
        t["id"]
        for t in client.get("/api/library/tracks").json()
        if t["title"] == "Private Song"
    )

    playlist_id = plain_client.post("/api/playlists", json={"name": "Mix"}).json()["id"]
    resp = plain_client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": [private_track_id]},
    )
    assert resp.status_code == 404


def test_ungranted_user_genre_list_excludes_private_root(
    session_factory, client, db_session, tmp_path
):
    _, private_root, plain_client = _setup_shared_and_private(
        client, session_factory, db_session, tmp_path
    )
    genres = plain_client.get("/api/library/genres").json()
    assert genres == ["Rock"]


def test_revoked_track_disappears_from_existing_playlist_and_queue(
    session_factory, client, db_session, tmp_path
):
    _, private_root, plain_client = _setup_shared_and_private(
        client, session_factory, db_session, tmp_path
    )
    plain_id = plain_client.get("/api/auth/me").json()["id"]
    client.post(f"/api/admin/roots/{private_root['id']}/access/{plain_id}")
    private_track = next(
        track
        for track in plain_client.get("/api/library/tracks").json()
        if track["title"] == "Private Song"
    )
    playlist_id = plain_client.post("/api/playlists", json={"name": "Private"}).json()[
        "id"
    ]
    plain_client.post(
        f"/api/playlists/{playlist_id}/items",
        json={"media_item_ids": [private_track["id"]]},
    )
    from app.models import PlaybackQueueItem, PlaybackState

    db_session.add(
        PlaybackQueueItem(
            user_id=plain_id, position=0, media_item_id=private_track["id"]
        )
    )
    db_session.add(
        PlaybackState(
            user_id=plain_id, media_item_id=private_track["id"], is_playing=True
        )
    )
    db_session.commit()

    client.delete(f"/api/admin/roots/{private_root['id']}/access/{plain_id}")

    assert plain_client.get(f"/api/playlists/{playlist_id}").json()["items"] == []
    state = plain_client.get("/api/playback/state").json()
    assert state["queue_media_item_ids"] == []
    assert state["media_item_id"] is None


def test_admin_only_sees_private_root_once_granted(
    session_factory, client, db_session, tmp_path
):
    _, private_root, _ = _setup_shared_and_private(
        client, session_factory, db_session, tmp_path
    )
    admin_id = client.get("/api/auth/me").json()["id"]

    titles = {t["title"] for t in client.get("/api/library/tracks").json()}
    assert titles == {"Shared Song"}
    assert client.get("/api/library/genres").json() == ["Rock"]
    # Admin endpoints are not scoped, so the root can still be managed.
    assert private_root["id"] in {r["id"] for r in client.get("/api/admin/roots").json()}

    client.post(f"/api/admin/roots/{private_root['id']}/access/{admin_id}")
    titles = {t["title"] for t in client.get("/api/library/tracks").json()}
    assert titles == {"Shared Song", "Private Song"}

    client.delete(f"/api/admin/roots/{private_root['id']}/access/{admin_id}")
    titles = {t["title"] for t in client.get("/api/library/tracks").json()}
    assert titles == {"Shared Song"}
