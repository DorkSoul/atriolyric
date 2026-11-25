from __future__ import annotations

from unittest.mock import patch

import mutagen
import pytest

from app.models import (
    Album,
    Artist,
    LibraryRoot,
    MediaItem,
    MediaItemGenre,
    TrackArtist,
    User,
    utcnow,
)
from app.routers import admin as admin_router
from app.scanner import scan_library_root
from app.security import hash_password
from tests.conftest import login_as, make_audio_fixture, unwritable_audio


def _scan_tracks(db_session, library_dir, count=2) -> list[MediaItem]:
    for i in range(count):
        make_audio_fixture(
            library_dir / f"t{i}.wav", title=f"Old {i}", artist="Old Artist", album="Old Album", genre="Old"
        )
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    # By path, so items[i] is t{i}.wav: ids follow the order the filesystem
    # lists the files in, which differs between machines.
    return db_session.query(MediaItem).order_by(MediaItem.relative_path).all()


def test_edit_tags_updates_file_and_db(client, db_session, library_dir):
    items = _scan_tracks(db_session, library_dir, count=1)
    resp = client.patch(f"/api/admin/media/{items[0].id}/tags", json={"artist": "New Artist"})
    assert resp.status_code == 200

    track = client.get("/api/library/tracks").json()[0]
    assert track["artist"] == "New Artist"
    assert track["title"] == "Old 0"  # untouched

    import mutagen

    on_disk = mutagen.File(library_dir / "t0.wav", easy=True)
    assert str(on_disk.tags.get("TPE1")) == "New Artist"


def test_edit_tags_requires_admin(session_factory, client, db_session, library_dir):
    items = _scan_tracks(db_session, library_dir, count=1)
    plain = User(username="plain", password_hash=hash_password("x"))
    db_session.add(plain)
    db_session.commit()
    plain_client = login_as(session_factory, client, "plain", "x")

    resp = plain_client.patch(f"/api/admin/media/{items[0].id}/tags", json={"artist": "X"})
    assert resp.status_code == 403


def test_edit_tags_on_readonly_file_returns_422_and_leaves_db_untouched(client, db_session, library_dir):
    items = _scan_tracks(db_session, library_dir, count=1)
    with unwritable_audio(library_dir / "t0.wav"):
        resp = client.patch(f"/api/admin/media/{items[0].id}/tags", json={"artist": "Should Not Land"})

    assert resp.status_code == 422
    track = client.get("/api/library/tracks").json()[0]
    assert track["artist"] == "Old Artist"


def test_bulk_edit_tags_applies_to_multiple_items(client, db_session, library_dir):
    items = _scan_tracks(db_session, library_dir, count=3)
    resp = client.post(
        "/api/admin/media/bulk-tags",
        json={"media_item_ids": [i.id for i in items], "genre": "Bulk Genre"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["succeeded"]) == {i.id for i in items}
    assert body["failed"] == []

    tracks = client.get("/api/library/tracks").json()
    assert all(t["genre"] == "Bulk Genre" for t in tracks)


def test_bulk_edit_tags_reports_missing_item_as_failed(client, db_session, library_dir):
    items = _scan_tracks(db_session, library_dir, count=1)
    resp = client.post(
        "/api/admin/media/bulk-tags",
        json={"media_item_ids": [items[0].id, 999999], "genre": "X"},
    )
    body = resp.json()
    assert body["succeeded"] == [items[0].id]
    assert body["updated"] == 1
    assert body["failed"] == [{"id": 999999, "reason": "Not found"}]


def test_bulk_remove_from_library_preserves_file_and_removes_db_row(
    client, db_session, library_dir
):
    # rglob() doesn't guarantee alphabetical order, so resolve the target
    # file from the item's own relative_path rather than assuming
    # items[0] means "t0.wav".
    items = _scan_tracks(db_session, library_dir, count=2)
    target = items[0]
    target_path = library_dir / target.relative_path
    assert target_path.exists()

    original_bytes = target_path.read_bytes()
    legacy_resp = client.post(
        "/api/admin/media/bulk-delete", json={"media_item_ids": [target.id]}
    )
    assert legacy_resp.status_code == 404
    assert target_path.read_bytes() == original_bytes

    resp = client.post("/api/admin/media/bulk-remove", json={"media_item_ids": [target.id]})
    assert resp.status_code == 200
    assert resp.json() == {"removed": [target.id], "failed": []}
    assert target_path.read_bytes() == original_bytes

    remaining_titles = {t["title"] for t in client.get("/api/library/tracks").json()}
    assert remaining_titles == {items[1].title}


def test_bulk_remove_missing_item_reported_as_failed(client):
    resp = client.post("/api/admin/media/bulk-remove", json={"media_item_ids": [999999]})
    assert resp.json() == {"removed": [], "failed": [999999]}


def test_bulk_remove_requires_admin(session_factory, client, db_session, library_dir):
    items = _scan_tracks(db_session, library_dir, count=1)
    plain = User(username="plain2", password_hash=hash_password("x"))
    db_session.add(plain)
    db_session.commit()
    plain_client = login_as(session_factory, client, "plain2", "x")

    resp = plain_client.post("/api/admin/media/bulk-remove", json={"media_item_ids": [items[0].id]})
    assert resp.status_code == 403


# --- clearing a field ------------------------------------------------------

_FRAMES = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "genre": "TCON"}


def _measured(db_session, item: MediaItem) -> None:
    """As if the ffmpeg loudness backfill had measured the file."""
    item.replaygain_track_gain = -5.5
    item.loudness_scanned_at = utcnow()
    db_session.commit()


@pytest.mark.parametrize("field", ["title", "artist", "album", "genre"])
def test_clearing_a_field_stores_null_and_removes_the_tag(client, db_session, library_dir, field):
    item = _scan_tracks(db_session, library_dir, count=1)[0]
    _measured(db_session, item)
    root = db_session.get(LibraryRoot, item.library_root_id)

    resp = client.patch(f"/api/admin/media/{item.id}/tags", json={field: ""})
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    item = db_session.get(MediaItem, item.id)
    # What a scan of the edited file stores: NULL, and the file name for a
    # missing title.
    assert getattr(item, field) == ("t0" if field == "title" else None)
    assert _FRAMES[field] not in mutagen.File(library_dir / "t0.wav").tags
    # The next ordinary scan sees the file as unchanged, so the loudness
    # measurement survives.
    assert scan_library_root(db_session, root)["updated"] == 0
    db_session.expire_all()
    item = db_session.get(MediaItem, item.id)
    assert getattr(item, field) == ("t0" if field == "title" else None)
    assert item.replaygain_track_gain == -5.5


def test_a_whitespace_only_value_counts_as_clearing(client, db_session, library_dir):
    item = _scan_tracks(db_session, library_dir, count=1)[0]
    assert client.patch(f"/api/admin/media/{item.id}/tags", json={"artist": "   "}).status_code == 200
    db_session.expire_all()
    assert db_session.get(MediaItem, item.id).artist is None
    assert "TPE1" not in mutagen.File(library_dir / "t0.wav").tags


def test_clearing_keeps_derived_data_as_a_scan_would(client, db_session, library_dir):
    item = _scan_tracks(db_session, library_dir, count=1)[0]
    assert item.album_id is not None

    resp = client.patch(
        f"/api/admin/media/{item.id}/tags", json={"artist": "", "album": "", "genre": ""}
    )
    assert resp.status_code == 200

    db_session.expire_all()
    item = db_session.get(MediaItem, item.id)
    assert item.album_id is None
    assert db_session.query(Album).count() == 0  # the emptied album is gone
    assert db_session.query(TrackArtist).filter_by(media_item_id=item.id).count() == 0
    assert db_session.query(Artist).count() == 0
    assert db_session.query(MediaItemGenre).filter_by(media_item_id=item.id).count() == 0
    track = client.get("/api/library/tracks").json()[0]
    assert (track["artist"], track["album"], track["genre"]) == (None, None, None)


def _make_flac(path, **tags) -> None:
    """A tagged FLAC with no audio frames - enough for mutagen and the scanner."""
    packed = (44100 << 44) | (1 << 41) | (15 << 36)
    info = (4096).to_bytes(2, "big") * 2 + bytes(6) + packed.to_bytes(8, "big") + bytes(16)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info)
    audio = mutagen.File(path, easy=True)
    audio.add_tags()
    for key, value in tags.items():
        audio[key] = value
    audio.save()


def test_clearing_removes_vorbis_comments_from_a_flac(client, db_session, library_dir):
    path = library_dir / "song.flac"
    _make_flac(path, title="Song", artist="Band", album="LP", genre="Rock")
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    item = db_session.query(MediaItem).one()

    resp = client.patch(f"/api/admin/media/{item.id}/tags", json={"album": "", "genre": " "})
    assert resp.status_code == 200

    on_disk = mutagen.File(path, easy=True)
    assert "album" not in on_disk and "genre" not in on_disk
    assert on_disk["artist"] == ["Band"]
    db_session.expire_all()
    item = db_session.get(MediaItem, item.id)
    assert (item.album, item.genre) == (None, None)
    assert scan_library_root(db_session, root)["updated"] == 0


def test_bulk_edit_reports_each_failure_and_still_updates_the_rest(client, db_session, library_dir):
    items = _scan_tracks(db_session, library_dir, count=3)
    real_assign = admin_router.assign_album

    def assign(db, item, *args):
        if item.id == items[2].id:
            raise RuntimeError("database is locked")
        return real_assign(db, item, *args)

    with (
        unwritable_audio(library_dir / "t1.wav"),
        patch("app.routers.admin.assign_album", side_effect=assign),
    ):
        resp = client.post(
            "/api/admin/media/bulk-tags",
            json={"media_item_ids": [i.id for i in items], "album": "Bulk"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 1
    assert body["succeeded"] == [items[0].id]
    assert body["failed"] == [
        {"id": items[1].id, "reason": "Couldn't write to the file"},
        {"id": items[2].id, "reason": "Couldn't save the change"},
    ]
    db_session.expire_all()
    assert [db_session.get(MediaItem, i.id).album for i in items] == ["Bulk", "Old Album", "Old Album"]


def _deferred_row_loads(statements: list[str]) -> list[str]:
    """Lazy loads of one deferred column for one row."""
    return [
        s
        for s in statements
        if s.lstrip().startswith(("SELECT media_items.artist_tags_json", "SELECT media_items.musicbrainz_metadata_json"))
        and "WHERE media_items.id = ?" in s
    ]


def test_tag_edits_load_the_deferred_columns_with_the_row(client, db_session, library_dir, session_factory):
    from tests.test_large_columns import recorded_statements

    items = _scan_tracks(db_session, library_dir, count=3)
    for item in items:
        item.artist_tags_json = {"artists": ["Old Artist", "Guest"]}
    db_session.commit()
    with recorded_statements(session_factory.kw["bind"]) as statements:
        assert client.patch(f"/api/admin/media/{items[0].id}/tags", json={"title": "Solo"}).status_code == 200
        bulk = client.post(
            "/api/admin/media/bulk-tags",
            json={"media_item_ids": [items[1].id, items[2].id], "title": "Both"},
        )
    assert bulk.json()["updated"] == 2
    assert _deferred_row_loads(statements) == []
    # The stored multi-value artists were read: the guest is still credited.
    guests = {
        media_item_id
        for (media_item_id,) in db_session.query(TrackArtist.media_item_id)
        .join(Artist, Artist.id == TrackArtist.artist_id)
        .filter(Artist.name == "Guest")
    }
    assert guests == {item.id for item in items}
