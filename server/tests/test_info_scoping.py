"""Artist/album info must not reveal what only a private root contains."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models import Album, Artist, LibraryRoot, User, UserLibraryAccess
from app.scanner import scan_library_root
from app.security import hash_password
from tests.conftest import login_as, make_audio_fixture


@pytest.fixture()
def private_library(db_session, library_dir):
    make_audio_fixture(
        library_dir / "p.wav", title="Hidden Song", artist="Secret Band",
        album="Secret Album", genre="Jazz",
    )
    root = LibraryRoot(path=str(library_dir), display_name="Private", is_shared=False)
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    artist = db_session.query(Artist).filter_by(name="Secret Band").one()
    artist.mbid = "11111111-1111-1111-1111-111111111111"
    artist.mbid_locked = True
    album = db_session.query(Album).one()
    album.release_group_mbid = "22222222-2222-2222-2222-222222222222"
    album.primary_type = "Album"
    album.release_locked = True
    db_session.commit()
    return root


@pytest.fixture()
def plain_client(client, db_session, session_factory):
    db_session.add(User(username="plain", password_hash=hash_password("plainpass")))
    db_session.commit()
    return login_as(session_factory, client, "plain", "plainpass")


def _album(c, artist, album):
    with patch("app.metadata_info.album_editions", return_value=[]):
        return c.get("/api/library/album-info", params={"artist": artist, "album": album})


def _artist(c, name):
    return c.get("/api/library/artist-info", params={"name": name})


def test_ungranted_user_gets_the_unknown_answer_for_a_private_album(
    private_library, plain_client
):
    real = _album(plain_client, "Secret Band", "Secret Album")
    made_up = _album(plain_client, "Nobody", "Nothing")
    assert real.status_code == made_up.status_code == 200
    expected = {**made_up.json(), "artist": "Secret Band", "album": "Secret Album"}
    assert real.json() == expected
    assert real.json()["release_group_mbid"] is None
    assert real.json()["release_locked"] is False


def test_ungranted_user_gets_the_unknown_answer_for_a_private_artist(
    private_library, plain_client
):
    real = _artist(plain_client, "Secret Band").json()
    made_up = _artist(plain_client, "Nobody Here").json()
    assert real == {**made_up, "name": "Secret Band"}
    assert real["mbid"] is None and real["mbid_locked"] is False


def test_granted_user_sees_the_private_album_and_artist(
    private_library, plain_client, client, db_session
):
    plain_id = plain_client.get("/api/auth/me").json()["id"]
    db_session.add(UserLibraryAccess(user_id=plain_id, library_root_id=private_library.id))
    db_session.commit()

    album = _album(plain_client, "Secret Band", "Secret Album").json()
    assert album["release_group_mbid"] == "22222222-2222-2222-2222-222222222222"
    assert album["release_locked"] is True
    artist = _artist(plain_client, "Secret Band").json()
    assert artist["mbid"] == "11111111-1111-1111-1111-111111111111"
    assert artist["mbid_locked"] is True


def test_artist_info_task_resolves_a_private_root_the_admin_has_not_granted(
    db_session, library_dir, client
):
    """The admin-started task is root-scoped and must resolve every artist of
    the root, while the API keeps hiding them from the (ungranted) admin."""
    from app.library_tasks import _run_artist_info_task
    from app.metadata_providers.musicbrainz import LookupResult
    from app.models import LibraryTask

    make_audio_fixture(
        library_dir / "p.wav", title="Hidden Song", artist="Secret Band", album="Secret Album", genre="Jazz"
    )
    root = LibraryRoot(path=str(library_dir), display_name="Private", is_shared=False)
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    admin = db_session.query(User).filter_by(is_admin=True).first()
    task = LibraryTask(library_root_id=root.id, kind="artist_info", status="running")
    db_session.add(task)
    db_session.commit()
    found = LookupResult(
        value={"artists": [{"id": "33333333-3333-3333-3333-333333333333", "name": "Secret Band", "score": 100}]}
    )
    with patch("app.metadata_info.musicbrainz.search_artists", return_value=found), patch(
        "app.metadata_info.musicbrainz.lookup_artist", return_value=LookupResult(value={})
    ):
        _run_artist_info_task(root.id, task.id, admin.id)
        db_session.expire_all()
        artist = db_session.query(Artist).filter_by(name="Secret Band").one()
        assert artist.mbid == "33333333-3333-3333-3333-333333333333"
        assert db_session.get(LibraryTask, task.id).status == "completed"
        assert _artist(client, "Secret Band").json()["mbid"] is None
