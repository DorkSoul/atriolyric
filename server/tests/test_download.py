from __future__ import annotations

import io
import zipfile

import pytest

from app import custom_art
from app.models import LibraryRoot, MediaItem, MediaKind, Playlist, PlaylistItem, User
from app.routers import download
from app.scanner import scan_library_root
from app.security import hash_password
from tests.conftest import login_as, make_audio_fixture


@pytest.fixture()
def library(db_session, library_dir):
    specs = [
        ("one.wav", "First", "Band", "LP", 1),
        ("two.wav", "Second", "Band", "LP", 2),
        ("solo.wav", "Solo", "Other", "EP", 1),
        ("odd.wav", "A/B: C*?", "Ba/nd", "..", 3),
    ]
    for name, title, artist, album, number in specs:
        make_audio_fixture(
            library_dir / name, title=title, artist=artist, album=album,
            genre="Rock", track_number=number,
        )
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    return {i.title: i for i in db_session.query(MediaItem).all()}


def test_safe_component_strips_dangerous_characters():
    assert download.safe_component("a/b\\c:d\x00e", "x") == "a_b_c_d_e"
    assert download.safe_component("..", "fallback") == "fallback"
    assert download.safe_component("  ", "fallback") == "fallback"
    assert download.safe_component(None, "fallback") == "fallback"
    assert len(download.safe_component("x" * 500, "f")) == 100


def test_single_download_is_original_file_with_clean_name(client, library):
    item = library["First"]
    resp = client.get(f"/api/download/media/{item.id}")
    assert resp.status_code == 200
    assert resp.content[:4] == b"RIFF"
    assert "Band - First.wav" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].startswith("attachment")
    odd = client.get(f"/api/download/media/{library['A/B: C*?'].id}")
    assert "Ba_nd - A_B_ C_.wav" in odd.headers["content-disposition"]
    assert "/" not in odd.headers["content-disposition"].split("filename=")[1]


def test_non_ascii_name_gets_utf8_filename(client, db_session, library):
    item = library["Solo"]
    item.title, item.artist = "Café", None
    db_session.commit()
    header = client.get(f"/api/download/media/{item.id}").headers["content-disposition"]
    assert "filename*=UTF-8''Caf%C3%A9.wav" in header


def test_single_download_errors(client, library, db_session, library_dir):
    assert client.get("/api/download/media/9999").status_code == 404
    (library_dir / "one.wav").unlink()
    assert client.get(f"/api/download/media/{library['First'].id}").status_code == 404


def test_album_zip_layout_and_content(client, library):
    key = custom_art.album_art_key("Band", "LP")
    resp = client.get(f"/api/download/album/{key}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "Atriolyric-LP.zip" in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == [
            "Atriolyric/Band/LP/01 First.wav",
            "Atriolyric/Band/LP/02 Second.wav",
        ]
        assert all(i.compress_type == zipfile.ZIP_STORED for i in archive.infolist())
        assert archive.read("Atriolyric/Band/LP/01 First.wav")[:4] == b"RIFF"


def test_artist_zip_sanitises_and_manifest_matches(client, library):
    key = custom_art.artist_art_key("Ba/nd")
    manifest = client.get(f"/api/download/artist/{key}/manifest").json()
    assert [m["path"] for m in manifest] == ["Atriolyric/Ba_nd/Unknown Album/03 A_B_ C_.wav"]
    assert manifest[0]["url"] == f"/api/download/media/{manifest[0]['id']}"
    assert manifest[0]["size"] > 0
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/download/artist/{key}").content)) as z:
        assert z.namelist() == [manifest[0]["path"]]


def test_playlist_zip_dedupes_and_puts_videos_aside(client, library, db_session, library_dir):
    user = db_session.query(User).one()
    (library_dir / "clip.mp4").write_bytes(b"video-bytes")
    root = db_session.query(LibraryRoot).one()
    video = MediaItem(
        library_root_id=root.id, kind=MediaKind.video, relative_path="clip.mp4", title="Clip"
    )
    db_session.add(video)
    playlist = Playlist(user_id=user.id, name="Road/Trip")
    db_session.add(playlist)
    db_session.commit()
    for position, item in enumerate([library["First"], library["First"], video]):
        db_session.add(
            PlaylistItem(playlist_id=playlist.id, media_item_id=item.id, position=position)
        )
    db_session.commit()

    # What it holds, each item once.
    assert client.get(f"/api/download/playlist/{playlist.id}/ids").json() == [
        library["First"].id,
        video.id,
    ]
    assert client.get("/api/download/playlist/999999/ids").status_code == 404
    manifest = client.get(f"/api/download/playlist/{playlist.id}/manifest").json()
    assert [m["path"] for m in manifest] == [
        "Atriolyric/Band/LP/01 First.wav",
        "Atriolyric/Band/LP/01 First (2).wav",
        f"Atriolyric/Videos/{video.video_folder}/Clip.mp4",
    ]
    resp = client.get(f"/api/download/playlist/{playlist.id}")
    assert "Atriolyric-Road_Trip.zip" in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        assert z.read(manifest[2]["path"]) == b"video-bytes"
    assert client.get(f"/api/download/media/{video.id}").content == b"video-bytes"


def test_media_manifest(client, library):
    manifest = client.get(f"/api/download/media/{library['Solo'].id}/manifest").json()
    assert [m["path"] for m in manifest] == ["Atriolyric/Other/EP/01 Solo.wav"]


def test_errors_and_scoping(client, library, db_session, session_factory):
    good = custom_art.album_art_key("Band", "LP")
    assert client.get("/api/download/album/" + custom_art.album_art_key("No", "Pe")).status_code == 404
    assert client.get("/api/download/nope/1").status_code == 404
    assert client.get("/api/download/media/abc").status_code == 422
    assert client.get("/api/download/media/abc/manifest").status_code == 404
    assert client.get("/api/download/media/999").status_code == 404
    assert client.get("/api/download/playlist/abc").status_code == 404
    assert client.get("/api/download/playlist/999").status_code == 404
    assert client.get("/api/download/album/%FF").status_code == 404

    db_session.query(LibraryRoot).update({LibraryRoot.is_shared: False})
    db_session.add(User(username="plain", password_hash=hash_password("plainpass")))
    db_session.commit()
    plain = login_as(session_factory, client, "plain", "plainpass")
    assert plain.get(f"/api/download/album/{good}").status_code == 404
    assert plain.get(f"/api/download/media/{library['First'].id}").status_code == 404
    assert plain.get(f"/api/download/media/{library['First'].id}/manifest").status_code == 404


def test_manifest_and_zip_skip_missing_files(client, library, library_dir):
    (library_dir / "one.wav").unlink()
    key = custom_art.album_art_key("Band", "LP")
    manifest = client.get(f"/api/download/album/{key}/manifest").json()
    assert [m["path"] for m in manifest] == ["Atriolyric/Band/LP/02 Second.wav"]


def test_empty_playlist_has_nothing_to_download(client, db_session):
    user = db_session.query(User).one()
    playlist = Playlist(user_id=user.id, name="Empty")
    db_session.add(playlist)
    db_session.commit()
    assert client.get(f"/api/download/playlist/{playlist.id}").status_code == 404
    assert client.get(f"/api/download/playlist/{playlist.id}/manifest").json() == []


def test_zip_skips_files_that_vanish_while_streaming(library, tmp_path):
    present = tmp_path / "a.bin"
    present.write_bytes(b"data")
    entries = [
        download.Entry(1, "Atriolyric/gone.bin", tmp_path / "gone.bin", 0),
        download.Entry(2, "Atriolyric/a.bin", present, 4),
    ]
    blob = b"".join(download._stream_zip(entries))
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert z.namelist() == ["Atriolyric/a.bin"]


def test_zip_entries_before_1980_are_clamped_instead_of_truncating(client, library, library_dir):
    """zipfile raises for a pre-1980 date, which must not happen after the 200
    is sent."""
    import os

    os.utime(library_dir / "one.wav", (86400, 86400))  # 2 January 1970
    album = client.get("/api/library/albums", params={"full": 1}).json()
    key = next(a["art_key"] for a in album if a["album"] == "LP")
    resp = client.get(f"/api/download/album/{key}")
    assert resp.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    assert archive.testzip() is None
    dates = sorted(info.date_time for info in archive.infolist())
    assert dates[0] == (1980, 1, 1, 0, 0, 0) and len(dates) == 2


def test_zip_date_clamps_both_ends():
    assert download.zip_date(0) == (1980, 1, 1, 0, 0, 0)
    assert download.zip_date(5_000_000_000)[0] == 2107  # 2128: past what a ZIP holds
    assert download.zip_date(1e20) == (1980, 1, 1, 0, 0, 0)  # unrepresentable


def test_single_download_releases_its_database_connection(
    client, library, session_factory, monkeypatch
):
    engine = session_factory.kw["bind"]
    baseline = engine.pool.checkedout()
    seen = []
    real = download.FileResponse

    class Recording(real):
        async def __call__(self, scope, receive, send):
            seen.append(engine.pool.checkedout())
            await super().__call__(scope, receive, send)

    monkeypatch.setattr(download, "FileResponse", Recording)
    resp = client.get(f"/api/download/media/{library['First'].id}")
    assert resp.status_code == 200 and seen and max(seen) <= baseline
