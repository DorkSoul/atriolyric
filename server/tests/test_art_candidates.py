from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import settings
from app.custom_art import album_art_key, artist_art_key
from app.external_cache import store
from app.models import Album, CustomArt, LibraryRoot, MediaItem
from app.scanner import scan_library_root
from tests.conftest import make_audio_fixture
from tests.test_custom_art import _png

MB_BAND = "00000007-0000-4000-8000-000000000007"


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "isolated.db")


@pytest.fixture()
def album_dir(db_session, library_dir):
    folder = library_dir / "Band - LP"
    folder.mkdir()
    make_audio_fixture(folder / "1.wav", title="One", artist="Band", album="LP", genre=None, embed_art=_png(color=(10, 200, 10)))
    make_audio_fixture(folder / "2.wav", title="Two", artist="Band", album="LP", genre=None, track_number=2)
    (folder / "cover.jpg").write_bytes(_png(color=(10, 10, 200)))
    (folder / "notes.jpg").write_bytes(_png())
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    return folder


def test_album_candidates_include_files_and_caa(client, db_session, album_dir):
    album = db_session.query(Album).one()
    album.release_group_mbid = "rg-1"
    db_session.commit()
    store(db_session, "musicbrainz", "release-group", "rg-1", {"id": "rg-1", "releases": [{"id": "rel-1", "status": "Official", "date": "2001", "country": "GB"}]})
    images = [
        {"url": "https://coverartarchive.org/release/rel-1/back.jpg", "thumb": "https://coverartarchive.org/t/back.jpg", "types": ["Back"], "front": False},
        {"url": "https://coverartarchive.org/release/rel-1/front.jpg", "thumb": "https://coverartarchive.org/t/front.jpg", "types": ["Front"], "front": True},
    ]
    with patch("app.art_candidates.coverart.list_release_images", return_value=images):
        body = client.get(f"/api/art/album/{album_art_key('Band', 'LP')}/candidates").json()
    sources = [(c["source"], c["label"]) for c in body]
    assert sources == [
        ("Embedded", "In “One”"),
        ("Folder", "cover.jpg"),
        ("Cover Art Archive", "Front · 2001 · GB"),
        ("Cover Art Archive", "Back · 2001 · GB"),
    ]
    local = client.get(body[1]["url"])
    assert local.status_code == 200 and local.content == (album_dir / "cover.jpg").read_bytes()


def test_folder_tokens_only_serve_cover_files(client, db_session, album_dir):
    item = db_session.query(MediaItem).filter_by(title="One").one()
    for token in (f"folder:{item.id}:notes.jpg", f"folder:{item.id}:../cover.jpg", "folder:999:cover.jpg", "nonsense"):
        assert client.get("/api/art/local-candidate", params={"token": token}).status_code == 404
    assert client.get("/api/art/local-candidate", params={"token": f"embedded:{item.id}"}).status_code == 200


def test_applying_a_candidate_stores_custom_art(client, db_session, album_dir):
    item = db_session.query(MediaItem).filter_by(title="One").one()
    key = album_art_key("Band", "LP")
    resp = client.post(f"/api/art/album/{key}/candidates", json={"candidate_id": f"folder:{item.id}:cover.jpg"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["custom_art"] is True
    row = db_session.query(CustomArt).one()
    assert (row.entity_type, row.entity_key) == ("album", "Band\x1fLP")
    assert (settings.custom_art_dir / row.thumb_path).is_file()


def test_remote_candidates_must_come_from_known_hosts(client, db_session, album_dir):
    from app.models import Artist

    key = artist_art_key("Band")
    db_session.query(Artist).filter_by(name="Band").one().mbid = MB_BAND
    db_session.commit()
    photo = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Band.jpg"
    store(db_session, "wikimedia", "artist-image", MB_BAND, {
        "url": photo, "thumb": photo, "author": "Pho Tographer", "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0", "page_url": "https://commons.wikimedia.org/wiki/File:Band.jpg",
    })
    with patch("app.art_candidates._download_remote_art", return_value=(_png(), "image/png")) as download:
        for refused in (
            "url:https://evil.example/x.jpg",
            # A Commons file other than the one the artist's Wikidata entry
            # names: its credit would be unknown.
            "url:https://upload.wikimedia.org/wikipedia/commons/x/xy/Someone_else.jpg",
            # Deezer images are never downloaded (its terms).
            "url:https://e-cdns-images.dzcdn.net/images/artist/x/1000x1000.jpg",
        ):
            resp = client.post(f"/api/art/artist/{key}/candidates", json={"candidate_id": refused})
            assert resp.status_code == 502, refused
        download.assert_not_called()
        resp = client.post(f"/api/art/artist/{key}/candidates", json={"candidate_id": f"url:{photo}"})
        assert resp.status_code == 200, resp.text
    row = db_session.query(CustomArt).one()
    assert (row.entity_type, row.source) == ("artist", "wikimedia")
    assert row.credit_json["author"] == "Pho Tographer"
    # The credit is served with the art, and shown in the artist's info.
    assert client.get(f"/api/art/artist/{key}/credit").json()["license"] == "CC BY 4.0"
    with patch("app.metadata_info.musicbrainz.lookup_artist", return_value=type("R", (), {"error": None, "value": {}})()):
        info = client.get("/api/library/artist-info", params={"name": "Band"}).json()
    assert info["image_credit"]["author"] == "Pho Tographer"


def test_uploads_and_file_pictures_record_their_source_and_no_credit(client, db_session, album_dir):
    item = db_session.query(MediaItem).filter_by(title="One").one()
    key = album_art_key("Band", "LP")
    assert client.post(f"/api/art/album/{key}/candidates", json={"candidate_id": f"folder:{item.id}:cover.jpg"}).status_code == 200
    row = db_session.query(CustomArt).one()
    assert (row.source, row.credit_json) == ("local", None)
    assert client.get(f"/api/art/album/{key}/credit").json() is None


def test_artist_candidates_come_from_wikimedia_commons_with_credit(client, db_session, album_dir):
    from app.models import Artist

    db_session.query(Artist).filter_by(name="Band").one().mbid = "00000007-0000-4000-8000-000000000007"
    db_session.commit()
    image = {
        "url": "https://upload.wikimedia.org/a.jpg", "thumb": "https://upload.wikimedia.org/a-s.jpg",
        "author": "Pho Tographer", "license": "CC BY-SA 4.0", "license_url": None, "page_url": None,
    }
    mb = {"relations": [{"type": "wikidata", "target-type": "url", "url": {"resource": "https://www.wikidata.org/wiki/Q1"}}]}
    with patch("app.metadata_info.musicbrainz.lookup_artist", return_value=type("R", (), {"error": None, "value": mb})()), patch(
        "app.metadata_info.wikipedia.image_from_links", return_value=image
    ) as fetch:
        body = client.get(f"/api/art/artist/{artist_art_key('Band')}/candidates").json()
        client.get(f"/api/art/artist/{artist_art_key('Band')}/candidates")
    assert fetch.call_count == 1  # cached server-wide
    assert body == [
        {
            "id": "url:https://upload.wikimedia.org/a.jpg", "source": "Wikimedia Commons",
            "label": "Pho Tographer · CC BY-SA 4.0",
            "url": "https://upload.wikimedia.org/a.jpg", "thumb_url": "https://upload.wikimedia.org/a-s.jpg",
        }
    ]


def test_artist_candidates_without_an_mbid_offer_no_photo(client, db_session, album_dir):
    with patch(
        "app.metadata_info.musicbrainz.search_artists",
        return_value=type("R", (), {"error": None, "value": {"artists": []}})(),
    ), patch("app.metadata_info.wikipedia.image_from_links") as fetch:
        body = client.get(f"/api/art/artist/{artist_art_key('Band')}/candidates").json()
    assert body == []
    fetch.assert_not_called()


def test_song_candidates_start_with_its_own_picture(client, db_session, album_dir):
    item = db_session.query(MediaItem).filter_by(title="One").one()
    body = client.get(f"/api/art/media_item/{item.id}/candidates").json()
    assert (body[0]["source"], body[0]["label"]) == ("Embedded", "In “One”")
    # A song without its own picture is offered its folder's and album's.
    other = db_session.query(MediaItem).filter_by(title="Two").one()
    labels = [c["label"] for c in client.get(f"/api/art/media_item/{other.id}/candidates").json()]
    assert labels == ["cover.jpg", "In “One”"]
