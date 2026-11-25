"""Folder cover images count as a track's art, albums and artists
take their picture from a track that has one, and the Artist info task gives
artists without a picture their Wikimedia Commons photo (once)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.art import folder_cover, load_full_art
from app.models import CustomArt, LibraryRoot, MediaItem, MediaKind
from tests.test_custom_art import _png

MBID = "00000004-0000-4000-8000-000000000004"


def test_a_folder_cover_is_found_by_name_and_checked(tmp_path):
    track = tmp_path / "album" / "01.mp3"
    track.parent.mkdir()
    track.write_bytes(b"not audio")
    assert folder_cover(track) is None
    (track.parent / "notes.jpg").write_bytes(_png())  # not a cover name
    (track.parent / "artist.jpg").write_bytes(_png())  # an artist photo, not a cover
    assert folder_cover(track) is None
    (track.parent / "Folder.JPEG").write_bytes(b"<html>not an image</html>")
    assert folder_cover(track) is None  # the bytes decide
    (track.parent / "front.png").write_bytes(_png(color=(1, 2, 3)))
    (track.parent / "cover.jpg").write_bytes(_png(color=(9, 9, 9)))
    found = folder_cover(track)
    assert found is not None and found[1] == "image/png"
    assert found[0] == (track.parent / "cover.jpg").read_bytes()  # cover beats front


def test_a_folder_cover_symlink_is_never_followed(tmp_path):
    secret = tmp_path / "elsewhere" / "private.png"
    secret.parent.mkdir()
    secret.write_bytes(_png())
    track = tmp_path / "album" / "01.mp3"
    track.parent.mkdir()
    track.write_bytes(b"x")
    (track.parent / "cover.png").symlink_to(secret)
    assert folder_cover(track) is None


def test_a_track_without_a_picture_shows_its_folder_cover(db_session, tmp_path):
    root = LibraryRoot(path=str(tmp_path), display_name="r")
    db_session.add(root)
    db_session.commit()
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "01.mp3").write_bytes(b"not audio")
    (tmp_path / "a" / "cover.jpg").write_bytes(_png())
    item = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="a/01.mp3", title="T")
    db_session.add(item)
    db_session.commit()
    art = load_full_art(item, tmp_path / "a" / "01.mp3")
    assert art is not None and art.data == _png()  # type: ignore[union-attr]


def _album(db_session, tmp_path: Path):
    root = LibraryRoot(path=str(tmp_path), display_name="Music")
    db_session.add(root)
    db_session.commit()
    rows = []
    for folder, n, checked in (("disc-a", 2, True), ("disc-b", 2, False)):
        for i in range(n):
            rows.append(MediaItem(
                library_root_id=root.id, kind=MediaKind.track, relative_path=f"{folder}/{i}.mp3",
                title=f"{folder}{i}", artist="Some Artist", album_artist="Some Artist", album="Split Album",
                file_mtime=100.0, art_checked_mtime=100.0 if checked else None,
            ))
    db_session.add_all(rows)
    db_session.commit()
    from app.albums import rebuild_albums
    from app.artist_credits import rebuild_all_credits

    rebuild_all_credits(db_session)
    rebuild_albums(db_session)
    return rows


def test_albums_and_artists_take_their_picture_from_a_track_with_art(client, db_session, tmp_path):
    rows = _album(db_session, tmp_path)
    first_with_art = rows[2].id  # the first disc-b track; disc-a's were found without art
    album = next(a for a in client.get("/api/library/albums").json() if a["album"] == "Split Album")
    assert album["representative_media_item_id"] == first_with_art
    artist = next(a for a in client.get("/api/library/artists").json() if a["artist"] == "Some Artist")
    assert artist["representative_media_item_id"] == first_with_art
    # A file that changed since it was checked counts as not known to lack art.
    rows[0].file_mtime = 200.0
    db_session.commit()
    from app import library_cache

    library_cache.bump_library_version()
    album = next(a for a in client.get("/api/library/albums").json() if a["album"] == "Split Album")
    assert album["representative_media_item_id"] == rows[0].id


def test_a_group_with_no_art_anywhere_still_has_a_representative(client, db_session, tmp_path):
    rows = _album(db_session, tmp_path)
    for row in rows:
        row.art_checked_mtime = row.file_mtime
    db_session.commit()
    album = next(a for a in client.get("/api/library/albums").json() if a["album"] == "Split Album")
    assert album["representative_media_item_id"] == rows[0].id


# --- Wikimedia Commons photos from the Artist info task -------------------------------


IMAGE = {
    "url": "https://upload.wikimedia.org/a.jpg", "thumb": "https://upload.wikimedia.org/a.jpg",
    "author": "Pho Tographer", "license": "CC BY 4.0",
    "license_url": "https://creativecommons.org/licenses/by/4.0", "page_url": "https://commons.wikimedia.org/wiki/File:A.jpg",
}


def test_an_artist_without_art_gets_its_commons_photo_once(db_session, tmp_path, monkeypatch):
    from app.art_candidates import apply_commons_photo
    from app.config import settings
    from app.custom_art import delete_custom_art

    monkeypatch.setattr(settings, "database_path", tmp_path / "db.sqlite")
    with patch("app.art_candidates.artist_image", return_value=IMAGE), \
         patch("app.art_candidates._download_remote_art", return_value=(_png(), "image/png")) as download:
        assert apply_commons_photo(db_session, "Band", MBID) is True
        row = db_session.query(CustomArt).one()
        assert (row.entity_type, row.entity_key, row.source) == ("artist", "Band", "wikimedia")
        assert row.credit_json["author"] == "Pho Tographer"
        # Already has art: nothing is replaced.
        assert apply_commons_photo(db_session, "Band", MBID) is False
        # Removed with "Reset art": it is not put back.
        delete_custom_art(db_session, "artist", "Band")
        assert apply_commons_photo(db_session, "Band", MBID) is False
    assert download.call_count == 1


def test_art_someone_chose_is_never_replaced(db_session, tmp_path, monkeypatch):
    from app.art_candidates import apply_commons_photo
    from app.config import settings
    from app.custom_art import store_custom_art

    monkeypatch.setattr(settings, "database_path", tmp_path / "db.sqlite")
    store_custom_art(db_session, "artist", "Band", _png(), source="upload")
    with patch("app.art_candidates.artist_image") as image:
        assert apply_commons_photo(db_session, "Band", MBID) is False
        assert apply_commons_photo(db_session, "Nobody", None) is False
    image.assert_not_called()


def test_no_photo_or_a_failed_download_saves_nothing_and_tries_again_later(db_session, tmp_path, monkeypatch):
    from app.art_candidates import apply_commons_photo
    from app.config import settings

    monkeypatch.setattr(settings, "database_path", tmp_path / "db.sqlite")
    with patch("app.art_candidates.artist_image", return_value=None):
        assert apply_commons_photo(db_session, "Band", MBID) is False
    with patch("app.art_candidates.artist_image", return_value=IMAGE), \
         patch("app.art_candidates._download_remote_art", side_effect=OSError("down")):
        assert apply_commons_photo(db_session, "Band", MBID) is False
    with patch("app.art_candidates.artist_image", return_value=IMAGE), \
         patch("app.art_candidates._download_remote_art", return_value=(_png(), "image/png")):
        assert apply_commons_photo(db_session, "Band", MBID) is True


def test_the_artist_info_task_applies_photos(db_session, session_factory, monkeypatch):
    from app import library_tasks
    from app.models import LibraryTask, User

    monkeypatch.setattr(library_tasks, "SessionLocal", session_factory)
    root = LibraryRoot(path="/ai", display_name="ai")
    db_session.add(root)
    db_session.commit()
    db_session.add(MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path="a.mp3", title="A", artist="Band"))
    task = LibraryTask(library_root_id=root.id, kind="artist_info", status="running")
    db_session.add(task)
    db_session.commit()
    from app.artist_credits import rebuild_all_credits

    rebuild_all_credits(db_session)
    admin = db_session.query(User).first()
    with patch("app.library_tasks.artist_info", return_value={"mbid": MBID}), \
         patch("app.library_tasks.artist_top_tracks"), \
         patch("app.library_tasks.apply_commons_photo", return_value=True) as apply:
        library_tasks._run_artist_info_task(root.id, task.id, admin.id)
    apply.assert_called_once()
    assert apply.call_args.args[1:] == ("Band", MBID)


def test_the_folder_cover_backfill_clears_the_no_art_marks(tmp_path):
    from sqlalchemy import create_engine

    from app import migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE media_items (id INTEGER PRIMARY KEY, art_checked_mtime FLOAT)")
        conn.exec_driver_sql("INSERT INTO media_items (art_checked_mtime) VALUES (1.0), (NULL)")
    with patch("app.migrations.engine", engine):
        migrations.recheck_art_with_folder_covers()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM media_items WHERE art_checked_mtime IS NOT NULL").scalar() == 0
    assert "recheck_art_with_folder_covers" in [name for name, _run in migrations.DATA_BACKFILLS]


def test_a_cover_added_later_is_seen_and_each_folder_is_listed_once(tmp_path):
    import os
    from unittest.mock import patch as mock_patch

    folder = tmp_path / "f"
    folder.mkdir()
    tracks = []
    for n in range(5):
        track = folder / f"{n}.mp3"
        track.write_bytes(b"x")
        tracks.append(track)
    real = os.scandir
    with mock_patch("app.art.os.scandir", side_effect=real) as scandir:
        assert all(folder_cover(track) is None for track in tracks)
    assert scandir.call_count == 1  # once for the folder, not once per track
    (folder / "cover.png").write_bytes(_png())
    os.utime(folder, ns=(os.stat(folder).st_atime_ns, os.stat(folder).st_mtime_ns + 1_000_000))
    assert folder_cover(tracks[0]) is not None
