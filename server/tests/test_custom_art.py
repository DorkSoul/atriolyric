from __future__ import annotations

import struct
import zlib
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app import custom_art
from app.cleanup import delete_media_records, delete_user_records
from app.config import settings
from app.models import CustomArt, LibraryRoot, MediaItem, Playlist, User
from app.scanner import scan_library_root
from app.security import hash_password
from tests.conftest import login_as, make_audio_fixture


def _png(width=8, height=8, color=(200, 30, 30)) -> bytes:
    def chunk(tag, body):
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    row = b"\x00" + bytes(color) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "isolated.db")


@pytest.fixture()
def library(db_session, library_dir):
    for i, (title, artist, album) in enumerate(
        [("A1", "Band", "LP"), ("A2", "Band", "LP"), ("B1", "Other", "EP")], start=1
    ):
        make_audio_fixture(
            library_dir / f"t{i}.wav", title=title, artist=artist, album=album,
            genre="Rock", track_number=i,
        )
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    return {i.title: i.id for i in db_session.query(MediaItem).all()}


def _art_files():
    return sorted(p.name for p in settings.custom_art_dir.glob("*"))


def test_key_helpers_roundtrip_and_reject_garbage():
    key = custom_art.album_art_key("Band", "LP")
    assert custom_art.decode_key(key) == "Band\x1fLP"
    assert custom_art.album_art_key(None, "LP") != key
    with pytest.raises(HTTPException):
        custom_art.decode_key("\xff")
    with pytest.raises(HTTPException):
        custom_art.entity_key_for_url("bogus", "1")
    with pytest.raises(HTTPException):
        custom_art.entity_key_for_url("media_item", "abc")
    assert custom_art.entity_key_for_url("playlist", "007") == "7"


def test_sniff_image_formats():
    assert custom_art.sniff_image(b"\xff\xd8\xffxxxx") == ".jpg"
    assert custom_art.sniff_image(_png()) == ".png"
    assert custom_art.sniff_image(b"RIFF1234WEBPxxxx") == ".webp"
    assert custom_art.sniff_image(b"GIF89a....") == ".gif"
    assert custom_art.sniff_image(b"hello") is None


def test_song_art_upload_serve_replace_and_reset(client, library):
    song = library["A1"]
    assert client.get(f"/api/art/media_item/{song}").status_code == 204
    resp = client.put(
        f"/api/art/media_item/{song}", content=_png(), headers={"content-type": "image/png"}
    )
    assert resp.status_code == 200 and resp.json()["custom_art"] is True
    first_files = _art_files()
    assert len(first_files) == 2

    full = client.get(f"/api/art/media_item/{song}")
    assert full.headers["content-type"] == "image/png" and full.content == _png()
    thumb = client.get(f"/api/art/media_item/{song}/thumb")
    assert thumb.headers["content-type"] == "image/jpeg"
    # The regular stream art endpoints now serve the custom art.
    assert client.get(f"/api/stream/{song}/art/full").content == _png()
    assert client.get(f"/api/stream/{song}/art").content == thumb.content
    # Other songs are unaffected (no art in the wav fixtures -> 204).
    assert client.get(f"/api/stream/{library['B1']}/art/full").status_code == 204

    new = _png(color=(1, 2, 3))
    assert client.put(f"/api/art/media_item/{song}", content=new).status_code == 200
    assert len(_art_files()) == 2 and _art_files() != first_files
    assert client.get(f"/api/art/media_item/{song}").content == new

    assert client.delete(f"/api/art/media_item/{song}").json()["custom_art"] is False
    assert _art_files() == []
    assert client.get(f"/api/art/media_item/{song}").status_code == 204
    assert client.delete(f"/api/art/media_item/{song}").status_code == 200


def test_album_art_cascades_to_tracks_and_song_wins(client, library):
    key = custom_art.album_art_key("Band", "LP")
    assert client.put(f"/api/art/album/{key}", content=_png()).status_code == 200
    assert client.get(f"/api/stream/{library['A1']}/art/full").content == _png()
    assert client.get(f"/api/stream/{library['A2']}/art").status_code == 200
    assert client.get(f"/api/stream/{library['B1']}/art").status_code == 204

    own = _png(color=(9, 9, 9))
    client.put(f"/api/art/media_item/{library['A2']}", content=own)
    assert client.get(f"/api/stream/{library['A2']}/art/full").content == own
    assert client.get(f"/api/stream/{library['A1']}/art/full").content == _png()
    # The media_item art route reports the effective (cascaded) art too.
    assert client.get(f"/api/art/media_item/{library['A1']}").content == _png()

    tracks = {t["title"]: t for t in client.get("/api/library/tracks").json()}
    assert tracks["A1"]["custom_art"] is False and tracks["A1"]["art_version"]
    assert tracks["A2"]["custom_art"] is True
    assert tracks["B1"]["art_version"] is None
    by_id = client.get(f"/api/library/media?ids={library['A1']}").json()
    assert by_id[0]["art_version"] == tracks["A1"]["art_version"]

    albums = {a["album"]: a for a in client.get("/api/library/albums").json()}
    assert albums["LP"]["art_key"] == key and albums["LP"]["custom_art"] is True
    assert albums["LP"]["art_version"] and albums["EP"]["custom_art"] is False
    played = client.get("/api/library/albums?sort=most_played").json()
    assert {a["album"]: a["custom_art"] for a in played} == {"LP": True, "EP": False}
    found = client.get("/api/library/search", params={"q": "LP"}).json()
    assert found["albums"][0]["custom_art"] is True

    assert client.delete(f"/api/art/album/{key}").status_code == 200
    assert client.get(f"/api/stream/{library['A1']}/art/full").status_code == 204


def test_artist_art_and_listing_flags(client, library):
    key = custom_art.artist_art_key("Band")
    assert client.put(f"/api/art/artist/{key}", content=_png()).status_code == 200
    assert client.get(f"/api/art/artist/{key}/thumb").status_code == 200
    artists = {a["artist"]: a for a in client.get("/api/library/artists").json()}
    assert artists["Band"]["custom_art"] is True and artists["Band"]["art_key"] == key
    assert artists["Other"]["custom_art"] is False
    played = client.get("/api/library/artists?sort=last_played").json()
    assert {a["artist"]: a["custom_art"] for a in played}["Band"] is True
    found = client.get("/api/library/search", params={"q": "Band"}).json()
    assert found["artists"][0]["custom_art"] is True


def test_playlist_art_owner_only_and_flags(client, library, session_factory, db_session):
    pid = client.post("/api/playlists", json={"name": "Mine"}).json()["id"]
    assert client.put(f"/api/art/playlist/{pid}", content=_png()).status_code == 200
    listed = client.get("/api/playlists").json()
    assert listed[0]["custom_art"] is True and listed[0]["art_version"]
    detail = client.get(f"/api/playlists/{pid}").json()
    assert detail["custom_art"] is True

    db_session.add(User(username="other", password_hash=hash_password("otherpass")))
    db_session.commit()
    other = login_as(session_factory, client, "other", "otherpass")
    assert other.get(f"/api/art/playlist/{pid}").status_code == 404
    assert other.put(f"/api/art/playlist/{pid}", content=_png()).status_code == 404
    assert other.delete(f"/api/art/playlist/{pid}").status_code == 404

    assert client.delete(f"/api/playlists/{pid}").status_code == 200
    assert _art_files() == []
    assert db_session.query(CustomArt).count() == 0


def test_mix_listing_reports_playlist_art(client, library, db_session):
    user = db_session.query(User).one()
    playlist = Playlist(user_id=user.id, name="Rock Mix", auto_kind="mix", mix_genre="Rock")
    db_session.add(playlist)
    db_session.commit()
    client.put(f"/api/art/playlist/{playlist.id}", content=_png())
    mixes = client.get("/api/library/mixes").json()
    assert mixes[0]["custom_art"] is True


def test_upload_validation(client, library):
    song = library["A1"]
    url = f"/api/art/media_item/{song}"
    assert client.put(url, content=b"not an image").status_code == 415
    # Right magic bytes but undecodable.
    assert client.put(url, content=b"\x89PNG\r\n\x1a\n" + b"junk" * 20).status_code == 415
    assert client.put(url, content=_png() + b"\0" * custom_art.MAX_UPLOAD_BYTES).status_code == 413
    assert client.put("/api/art/media_item/99999", content=_png()).status_code == 404
    assert client.put("/api/art/album/" + custom_art.album_art_key("No", "Such"), content=_png()).status_code == 404
    assert client.put("/api/art/artist/" + custom_art.artist_art_key("Nobody"), content=_png()).status_code == 404
    assert client.put("/api/art/nope/1", content=_png()).status_code == 404
    assert client.put("/api/art/playlist/zzz", content=_png()).status_code == 404
    assert _art_files() == []


def test_oversized_declared_length_rejected(client, library):
    resp = client.put(
        f"/api/art/media_item/{library['A1']}",
        content=b"x",
        headers={"content-length": str(custom_art.MAX_UPLOAD_BYTES * 2)},
    )
    assert resp.status_code in (413, 400)


def test_multipart_upload(client, library):
    song = library["A1"]
    resp = client.put(
        f"/api/art/media_item/{song}", files={"file": ("cover.png", _png(), "image/png")}
    )
    assert resp.status_code == 200
    assert client.get(f"/api/art/media_item/{song}").content == _png()
    # A multipart body without a file part is a bad request.
    resp = client.put(f"/api/art/media_item/{song}", files={"x": (None, "y")})
    assert resp.status_code == 400


def test_oversized_bodies_rejected_while_streaming(client, library):
    url = f"/api/art/media_item/{library['A1']}"
    # Just over the limit but inside the transport slack: caught after parsing.
    r = client.put(url, content=_png() + b"\0" * custom_art.MAX_UPLOAD_BYTES)
    assert r.status_code == 413
    just_over = b"\xff\xd8\xff" + b"\0" * custom_art.MAX_UPLOAD_BYTES
    assert client.put(url, content=just_over).status_code == 413

    def chunks():  # no Content-Length: the streaming cap must stop it
        for _ in range(custom_art.MAX_UPLOAD_BYTES // (1024 * 1024) + 5):
            yield b"\0" * (1024 * 1024)

    assert client.put(url, content=chunks()).status_code == 413


def test_album_artist_credit_cascades(client, library, db_session):
    db_session.query(MediaItem).filter(MediaItem.title == "A1").update(
        {MediaItem.album_artist: "Various"}
    )
    db_session.commit()
    key = custom_art.album_art_key("Various", "LP")
    assert client.put(f"/api/art/album/{key}", content=_png()).status_code == 200
    assert client.get(f"/api/stream/{library['A1']}/art/full").content == _png()
    assert client.get(f"/api/stream/{library['A2']}/art/full").status_code == 204


def test_missing_file_on_disk_serves_no_art(client, library):
    song = library["A1"]
    client.put(f"/api/art/media_item/{song}", content=_png())
    for p in settings.custom_art_dir.glob("*"):
        p.unlink()
    assert client.get(f"/api/art/media_item/{song}").status_code == 204
    assert client.get(f"/api/stream/{song}/art/full").status_code == 204


def test_scoping_hides_private_library_art(client, library, session_factory, db_session):
    db_session.query(LibraryRoot).update({LibraryRoot.is_shared: False})
    db_session.add(User(username="plain", password_hash=hash_password("plainpass")))
    db_session.commit()
    plain = login_as(session_factory, client, "plain", "plainpass")
    assert plain.put(f"/api/art/media_item/{library['A1']}", content=_png()).status_code == 403
    assert plain.get(f"/api/art/media_item/{library['A1']}").status_code == 404


def test_only_admins_change_shared_art_but_owners_change_playlist_art(
    client, library, session_factory, db_session
):
    plain_user = User(username="plain", password_hash=hash_password("plainpass"))
    db_session.add(plain_user)
    db_session.commit()
    plain = login_as(session_factory, client, "plain", "plainpass")
    song = library["A1"]
    album_key = custom_art.album_art_key("Band", "LP")
    targets = [
        ("media_item", str(song)),
        ("album", album_key),
        ("artist", custom_art.artist_art_key("Band")),
    ]
    client.put(f"/api/art/media_item/{song}", content=_png())
    for entity, key in targets:
        assert plain.put(f"/api/art/{entity}/{key}", content=_png()).status_code == 403
        assert plain.delete(f"/api/art/{entity}/{key}").status_code == 403
        with patch("app.routers.art.candidate_bytes", return_value=_png()) as fetched:
            resp = plain.post(f"/api/art/{entity}/{key}/candidates", json={"candidate_id": "x"})
        assert resp.status_code == 403
        assert fetched.call_count == 0
    # The admin's art is untouched, and the admin can still change it.
    assert client.get(f"/api/art/media_item/{song}").status_code == 200
    assert client.put(f"/api/art/album/{album_key}", content=_png()).status_code == 200

    playlist = Playlist(user_id=plain_user.id, name="Mine")
    db_session.add(playlist)
    db_session.commit()
    assert plain.put(f"/api/art/playlist/{playlist.id}", content=_png()).status_code == 200
    assert plain.delete(f"/api/art/playlist/{playlist.id}").status_code == 200


def test_video_thumbnail_uses_custom_art(client, db_session, library_dir):
    root = LibraryRoot(path=str(library_dir), display_name="V")
    db_session.add(root)
    db_session.commit()
    (library_dir / "v.mp4").write_bytes(b"x")
    video = MediaItem(
        library_root_id=root.id, kind="video", relative_path="v.mp4", title="Vid"
    )
    db_session.add(video)
    db_session.commit()
    assert client.get(f"/api/stream/{video.id}/thumbnail").status_code == 204
    assert client.put(f"/api/art/media_item/{video.id}", content=_png()).status_code == 200
    resp = client.get(f"/api/stream/{video.id}/thumbnail")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"


def test_cleanup_removes_art_when_entities_deleted(client, library, db_session):
    song = library["A1"]
    client.put(f"/api/art/media_item/{song}", content=_png())
    user = db_session.query(User).one()
    playlist = Playlist(user_id=user.id, name="P")
    db_session.add(playlist)
    db_session.commit()
    client.put(f"/api/art/playlist/{playlist.id}", content=_png())
    assert len(_art_files()) == 4
    delete_media_records(db_session, [song])
    db_session.commit()
    assert len(_art_files()) == 2
    delete_user_records(db_session, user.id)
    db_session.commit()
    assert _art_files() == []
    assert db_session.query(CustomArt).count() == 0


def test_stale_mix_deletion_purges_art(client, library, db_session):
    from app.discovery import refresh_all_genre_mixes

    user = db_session.query(User).one()
    stale = Playlist(user_id=user.id, name="Old Mix", auto_kind="mix", mix_genre="Ghost")
    db_session.add(stale)
    db_session.commit()
    client.put(f"/api/art/playlist/{stale.id}", content=_png())
    refresh_all_genre_mixes(db_session, user)
    assert _art_files() == []


def test_read_body_enforces_cap_on_streamed_chunks():
    import asyncio
    from types import SimpleNamespace

    from app.routers.art import _read_body

    def request(chunks, length=None):
        async def stream():
            for chunk in chunks:
                yield chunk

        headers = {} if length is None else {"content-length": str(length)}
        return SimpleNamespace(headers=headers, stream=stream)

    assert asyncio.run(_read_body(request([b"ab", b"cd"]))) == b"abcd"
    big = b"\0" * (custom_art.MAX_UPLOAD_BYTES + 100_000)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read_body(request([big])))
    assert exc.value.status_code == 413
    with pytest.raises(HTTPException):
        asyncio.run(_read_body(request([b"x"], length=len(big))))
