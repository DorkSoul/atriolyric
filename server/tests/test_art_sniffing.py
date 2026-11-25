"""Art bytes are never trusted for their type: a tag's MIME, a file suffix or
a remote server's Content-Type can claim anything, so every path that stores
or serves art decides the type from the bytes (jpeg/png/webp/gif only)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from mutagen.id3 import APIC
from mutagen.wave import WAVE

from app import art
from app.config import settings
from app.image_sniff import sniff_mime
from app.models import LibraryRoot, MediaItem
from app.scanner import scan_library_root
from tests.conftest import make_audio_fixture
from tests.test_custom_art import _png

HTML = b"<html><script>alert(document.domain)</script></html>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-body"
CSP = "default-src 'none'; sandbox"


@pytest.fixture(autouse=True)
def _isolated_art_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "isolated.db")


def _track(db_session, library_dir, claimed_mime: str | None, data: bytes | None) -> MediaItem:
    """One scanned track whose APIC frame claims `claimed_mime` for `data`."""
    path = library_dir / "track.wav"
    make_audio_fixture(path, title="T", artist="A", album="B", genre="Rock")
    if data is not None:
        audio = WAVE(str(path))
        audio.tags.add(APIC(encoding=3, mime=claimed_mime, type=3, desc="c", data=data))
        audio.save()
    root = LibraryRoot(path=str(library_dir), display_name="Library")
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    return db_session.query(MediaItem).one()


def test_sniff_mime_recognises_only_the_four_image_types():
    assert sniff_mime(JPEG) == "image/jpeg"
    assert sniff_mime(_png()) == "image/png"
    assert sniff_mime(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert sniff_mime(b"GIF89a....") == "image/gif"
    assert sniff_mime(HTML) is None
    assert sniff_mime(SVG) is None
    assert sniff_mime(b"") is None
    assert art._checked(JPEG + b"x" * art.MAX_ART_BYTES) is None


@pytest.mark.parametrize(
    ("mime", "payload"), [("text/html", HTML), ("image/svg+xml", SVG), ("image/jpeg", b"")]
)
def test_non_image_embedded_picture_is_no_art_anywhere(client, db_session, library_dir, mime, payload):
    item = _track(db_session, library_dir, mime, payload)
    assert art.extract_embedded_art(library_dir / "track.wav") is None
    assert client.get("/api/art/local-candidate", params={"token": f"embedded:{item.id}"}).status_code == 404
    for url in (f"/api/stream/{item.id}/art", f"/api/stream/{item.id}/art/full", f"/api/stream/{item.id}/thumbnail"):
        resp = client.get(url)
        assert resp.status_code == 204, url
        assert "html" not in resp.headers.get("content-type", "") and "svg" not in resp.headers.get("content-type", "")
    listed = client.get(f"/api/art/media_item/{item.id}/candidates").json()
    assert [c for c in listed if c["source"] == "Embedded"] == []


def test_embedded_type_comes_from_the_bytes_not_the_tag(client, db_session, library_dir):
    item = _track(db_session, library_dir, "image/png", JPEG)
    resp = client.get(f"/api/art/local-candidate?token=embedded:{item.id}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    resp = client.get(f"/api/stream/{item.id}/art/full")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"


def test_a_real_png_claiming_html_is_served_as_png(client, db_session, library_dir):
    png = _png()
    item = _track(db_session, library_dir, "text/html", png)
    resp = client.get(f"/api/art/local-candidate?token=embedded:{item.id}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert resp.content == png
    resp = client.get(f"/api/stream/{item.id}/art/full")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"


@pytest.fixture()
def album_folder(db_session, library_dir):
    make_audio_fixture(library_dir / "1.wav", title="One", artist="Band", album="LP", genre=None)
    root = LibraryRoot(path=str(library_dir), display_name="Lib")
    db_session.add(root)
    db_session.commit()
    scan_library_root(db_session, root)
    return db_session.query(MediaItem).one()


def _folder_tokens(client, item) -> list[str]:
    listed = client.get(f"/api/art/media_item/{item.id}/candidates").json()
    return [c["id"] for c in listed if c["source"] == "Folder"]


def test_folder_symlink_escaping_the_root_is_not_found_or_listed(client, album_folder, library_dir, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(_png())
    (library_dir / "cover.jpg").symlink_to(secret)
    item = album_folder
    assert _folder_tokens(client, item) == []
    assert client.get("/api/art/local-candidate", params={"token": f"folder:{item.id}:cover.jpg"}).status_code == 404


def test_folder_symlink_inside_the_root_still_works(client, album_folder, library_dir):
    (library_dir / "real.bin").write_bytes(_png())
    (library_dir / "cover.jpg").symlink_to(library_dir / "real.bin")
    item = album_folder
    assert _folder_tokens(client, item) == [f"folder:{item.id}:cover.jpg"]
    assert client.get("/api/art/local-candidate", params={"token": f"folder:{item.id}:cover.jpg"}).status_code == 200


def test_folder_file_that_is_not_an_image_is_not_found_or_listed(client, album_folder, library_dir):
    (library_dir / "cover.jpg").write_bytes(HTML)
    item = album_folder
    assert _folder_tokens(client, item) == []
    assert client.get("/api/art/local-candidate", params={"token": f"folder:{item.id}:cover.jpg"}).status_code == 404


def test_folder_type_comes_from_the_bytes_not_the_suffix(client, album_folder, library_dir):
    (library_dir / "cover.jpg").write_bytes(_png())
    resp = client.get("/api/art/local-candidate", params={"token": f"folder:{album_folder.id}:cover.jpg"})
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"


def test_folder_file_vanishing_between_listing_and_fetch_is_not_found(client, album_folder):
    resp = client.get("/api/art/local-candidate", params={"token": f"folder:{album_folder.id}:cover.png"})
    assert resp.status_code == 404


def test_art_stream_thumbnail_and_download_responses_carry_the_csp(client, db_session, library_dir):
    item = _track(db_session, library_dir, "image/png", _png())
    urls = [
        f"/api/art/local-candidate?token=embedded:{item.id}",
        f"/api/stream/{item.id}/art",
        f"/api/stream/{item.id}/art/full",
        f"/api/stream/{item.id}/thumbnail",
        f"/api/stream/{item.id}",
        f"/api/download/media/{item.id}",
        f"/api/stream/{item.id}/art/nonexistent-is-404",
    ]
    for url in urls:
        resp = client.get(url)
        assert resp.headers["content-security-policy"] == CSP, url
    resp = client.get(f"/api/stream/{item.id}", headers={"Range": "bytes=0-9"})
    assert resp.status_code == 206
    assert resp.headers["content-security-policy"] == CSP
    assert client.get(f"/api/art/media_item/{item.id}").headers["content-security-policy"] == CSP


def test_cached_and_custom_art_is_only_ever_served_as_an_image_type(client, db_session, library_dir):
    from pathlib import Path

    from app.routers.stream import _image_content_type

    assert _image_content_type(Path("x.gif")) == "image/gif"
    assert _image_content_type(Path("1.html")) == "application/octet-stream"
    item = _track(db_session, library_dir, "image/jpeg", JPEG)
    # An old cache file written under a tag-chosen name is served by suffix.
    settings.art_dir.mkdir(parents=True, exist_ok=True)
    (settings.art_dir / f"{item.id}.jpg").write_bytes(HTML)
    resp = client.get(f"/api/stream/{item.id}/art/full")
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_cache_art_bytes_rejects_non_images_and_picks_the_extension_by_sniffing(tmp_path):
    for bad in (HTML, SVG, b""):
        with pytest.raises(ValueError):
            art.cache_art_bytes(1, bad)
    with pytest.raises(ValueError):
        art.cache_art_bytes(1, JPEG + b"x" * art.MAX_ART_BYTES)
    assert art.cache_art_bytes(2, _png()).suffix == ".png"
    assert art.cache_art_bytes(3, b"RIFF\0\0\0\0WEBPVP8 ").suffix == ".webp"
    assert art.cache_art_bytes(4, b"GIF89a....").suffix == ".gif"


class _FakeResponse:
    status_code = 200

    def __init__(self, content_type: str, body: bytes):
        self.headers = {"content-type": content_type}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size):
        yield self._body


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def stream(self, method, url):
        return self._response


def _download(content_type: str, body: bytes):
    with (
        patch("app.art._validate_remote_art_url"),
        patch("app.art.httpx.Client", return_value=_FakeClient(_FakeResponse(content_type, body))),
    ):
        return art._download_remote_art("https://example.com/a")


def test_remote_art_is_typed_by_its_bytes_and_non_images_are_rejected():
    assert _download("image/svg+xml", SVG) is None
    assert _download("image/png", HTML) is None
    assert _download("text/html", _png()) == (_png(), "image/png")


def test_folder_image_edge_cases_are_not_found(client, album_folder, library_dir, monkeypatch):
    item = album_folder
    (library_dir / "cover.jpg").mkdir()  # a directory with an image name
    assert _folder_tokens(client, item) == []
    (library_dir / "cover.jpg").rmdir()
    (library_dir / "cover.jpg").write_bytes(_png())
    monkeypatch.setattr("app.art_candidates.MAX_FOLDER_IMAGE_BYTES", 10)
    assert _folder_tokens(client, item) == []
    assert client.get("/api/art/local-candidate", params={"token": f"folder:{item.id}:cover.jpg"}).status_code == 404
