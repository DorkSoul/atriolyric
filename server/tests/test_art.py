from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from app.art import (
    ArtBytes,
    _resize_art,
    cache_art_bytes,
    ensure_art_thumb_cached,
    extract_embedded_art,
    load_full_art,
)
from app.config import art_thumb_path, find_art_path, settings
from app.models import LibraryRoot, MediaItem
from app.scanner import scan_library_root
from tests.conftest import make_audio_fixture

JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-body"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png-body"


@pytest.fixture(autouse=True)
def _isolated_art_dir(tmp_path, monkeypatch):
    """`art_dir` is derived from the shared `settings.database_path`, but
    each test's media items live in their own throwaway sqlite file with
    ids restarting at 1 - without this, two tests both minting item id 1
    would collide on the same cached art file on real disk."""
    monkeypatch.setattr(settings, "database_path", tmp_path / "isolated.db")


def _scan_one(db_session, library_dir, **fixture_kwargs) -> MediaItem:
    make_audio_fixture(library_dir / "track.wav", **fixture_kwargs)
    root = LibraryRoot(path=str(library_dir), display_name="Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    return db_session.query(MediaItem).one()


def test_extract_embedded_art_reads_apic_frame(library_dir):
    path = library_dir / "track.wav"
    make_audio_fixture(
        path, title="T", artist="A", album="B", genre="Rock", embed_art=JPEG_BYTES
    )
    found = extract_embedded_art(path)
    assert found is not None
    data, mime = found
    assert data == JPEG_BYTES
    assert mime == "image/jpeg"


def test_extract_embedded_art_none_when_untagged(library_dir):
    path = library_dir / "track.wav"
    make_audio_fixture(path, title="T", artist="A", album="B", genre="Rock")
    assert extract_embedded_art(path) is None


def test_scan_stores_the_thumbnail_but_no_full_size_copy(db_session, library_dir):
    """Embedded art already lives in the audio file - storing a second
    full-size copy of every picture would cost tens of GB for a large
    library."""
    with patch("app.art._resize_art", side_effect=_fake_successful_resize):
        item = _scan_one(
            db_session,
            library_dir,
            title="T",
            artist="A",
            album="B",
            genre="Rock",
            embed_art=JPEG_BYTES,
        )
    assert art_thumb_path(item.id).is_file()
    assert find_art_path(item.id) is None


def test_scan_leaves_no_art_file_when_untagged(db_session, library_dir):
    item = _scan_one(
        db_session, library_dir, title="T", artist="A", album="B", genre="Rock"
    )
    assert find_art_path(item.id) is None


def test_scan_pre_generates_the_resized_thumbnail_too(db_session, library_dir):
    """Not just the original - so a grid full of art is never waiting on
    first-time ffmpeg work on first page view, only ever serving an
    already-cached file. Same pre-generation video thumbnails already get."""
    with patch("app.art._resize_art", side_effect=_fake_successful_resize):
        item = _scan_one(
            db_session,
            library_dir,
            title="T",
            artist="A",
            album="B",
            genre="Rock",
            embed_art=JPEG_BYTES,
        )
    assert art_thumb_path(item.id).read_bytes() == b"resized-bytes"


def test_art_endpoint_serves_embedded_art(client, db_session, library_dir):
    item = _scan_one(
        db_session,
        library_dir,
        title="T",
        artist="A",
        album="B",
        genre="Rock",
        embed_art=JPEG_BYTES,
    )
    resp = client.get(f"/api/stream/{item.id}/art")
    assert resp.status_code == 200
    assert resp.content == JPEG_BYTES
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "private, max-age=86400"
    # Starlette's CORS middleware adds "Origin" to Vary on every response.
    assert "Cookie" in {v.strip() for v in resp.headers["vary"].split(",")}


def test_full_art_endpoint_preserves_webp_content_type(
    client, db_session, library_dir, monkeypatch
):
    item = _scan_one(
        db_session,
        library_dir,
        title="T",
        artist="A",
        album="B",
        genre="Rock",
    )
    webp_path = settings.art_dir / f"{item.id}.webp"
    webp_path.parent.mkdir(parents=True, exist_ok=True)
    webp_path.write_bytes(b"webp-artwork")
    monkeypatch.setattr("app.art.find_art_path", lambda _item_id: webp_path)

    resp = client.get(f"/api/stream/{item.id}/art/full")
    assert resp.status_code == 200
    assert resp.content == b"webp-artwork"
    assert resp.headers["content-type"] == "image/webp"


def test_art_endpoint_204_when_no_art_anywhere(client, db_session, library_dir):
    item = _scan_one(
        db_session, library_dir, title="T", artist="A", album="B", genre="Rock"
    )
    resp = client.get(f"/api/stream/{item.id}/art")
    # 204, not 404: a grid of art-less tiles must not look like path probing.
    assert resp.status_code == 204
    assert resp.content == b""
    assert "max-age" in resp.headers["cache-control"]
    resp = client.get(f"/api/stream/{item.id}/art/full")
    assert resp.status_code == 204


def test_art_endpoint_falls_back_to_a_sibling_song_of_the_album(
    client, db_session, library_dir
):
    make_audio_fixture(
        library_dir / "a.wav", title="A", artist="X", album="Alb", genre="Rock", embed_art=JPEG_BYTES
    )
    make_audio_fixture(library_dir / "b.wav", title="B", artist="X", album="Alb", genre="Rock")
    root = LibraryRoot(path=str(library_dir), display_name="Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    bare = db_session.query(MediaItem).filter(MediaItem.title == "B").one()
    resp = client.get(f"/api/stream/{bare.id}/art")
    assert resp.status_code == 200
    assert resp.content == JPEG_BYTES


def test_art_endpoint_falls_back_to_cover_art_url(client, db_session, library_dir):
    item = _scan_one(
        db_session, library_dir, title="T", artist="A", album="B", genre="Rock"
    )
    item.cover_art_url = "https://coverartarchive.org/release/fake-mbid/front"
    db_session.commit()

    with (
        patch(
            "app.art._download_remote_art",
            return_value=(PNG_BYTES, "image/png"),
        ),
    ):
        resp = client.get(f"/api/stream/{item.id}/art")
    assert resp.status_code == 200
    assert resp.content == PNG_BYTES
    assert resp.headers["content-type"] == "image/png"


def test_art_endpoint_404_for_inaccessible_item(
    client, db_session, library_dir, session_factory
):
    from tests.conftest import login_as

    item = _scan_one(
        db_session, library_dir, title="T", artist="A", album="B", genre="Rock"
    )
    db_session.query(LibraryRoot).update({"is_shared": False})
    db_session.commit()

    from app.models import User
    from app.security import hash_password

    other = User(username="other", password_hash=hash_password("pw"))
    db_session.add(other)
    db_session.commit()

    other_client = login_as(session_factory, client, "other", "pw")
    resp = other_client.get(f"/api/stream/{item.id}/art")
    assert resp.status_code == 404


def test_load_full_art_prefers_a_stored_file(db_session, library_dir):
    item = _scan_one(
        db_session, library_dir, title="T", artist="A", album="B", genre="Rock"
    )
    cache_art_bytes(item.id, JPEG_BYTES)
    item.cover_art_url = "https://example.com/should-not-be-fetched.jpg"

    with patch("app.art._download_remote_art") as mock_get:
        path = load_full_art(item, None)
        mock_get.assert_not_called()
    assert path is not None
    assert path.read_bytes() == JPEG_BYTES


def _fake_successful_resize(src, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b"resized-bytes")
    return True


def test_ensure_art_thumb_cached_serves_the_resized_copy(db_session, library_dir):
    item = _scan_one(
        db_session,
        library_dir,
        title="T",
        artist="A",
        album="B",
        genre="Rock",
        embed_art=JPEG_BYTES,
    )
    with patch(
        "app.art._resize_art", side_effect=_fake_successful_resize
    ) as mock_resize:
        path = ensure_art_thumb_cached(item, library_dir / "track.wav")
    # Resized straight from the embedded picture, without storing it first.
    assert mock_resize.call_args.args[0] == ArtBytes(JPEG_BYTES, "image/jpeg")
    assert path == art_thumb_path(item.id)
    assert path.read_bytes() == b"resized-bytes"
    assert find_art_path(item.id) is None


def test_ensure_art_thumb_cached_only_resizes_once(db_session, library_dir):
    item = _scan_one(
        db_session,
        library_dir,
        title="T",
        artist="A",
        album="B",
        genre="Rock",
        embed_art=JPEG_BYTES,
    )
    with patch(
        "app.art._resize_art", side_effect=_fake_successful_resize
    ) as mock_resize:
        ensure_art_thumb_cached(item, library_dir / "track.wav")
        ensure_art_thumb_cached(item, library_dir / "track.wav")
    mock_resize.assert_called_once()


def test_ensure_art_thumb_cached_falls_back_to_full_when_resize_fails(
    db_session, library_dir
):
    item = _scan_one(
        db_session,
        library_dir,
        title="T",
        artist="A",
        album="B",
        genre="Rock",
        embed_art=JPEG_BYTES,
    )
    file_path = library_dir / "track.wav"
    with patch("app.art._resize_art", return_value=False):
        art = ensure_art_thumb_cached(item, file_path)
    assert art == ArtBytes(JPEG_BYTES, "image/jpeg")
    assert find_art_path(item.id) is None


def test_art_endpoint_serves_the_resized_copy(client, db_session, library_dir):
    item = _scan_one(
        db_session,
        library_dir,
        title="T",
        artist="A",
        album="B",
        genre="Rock",
        embed_art=JPEG_BYTES,
    )
    with patch("app.art._resize_art", side_effect=_fake_successful_resize):
        resp = client.get(f"/api/stream/{item.id}/art")
    assert resp.status_code == 200
    assert resp.content == b"resized-bytes"


def test_art_full_endpoint_always_serves_the_original(client, db_session, library_dir):
    item = _scan_one(
        db_session,
        library_dir,
        title="T",
        artist="A",
        album="B",
        genre="Rock",
        embed_art=JPEG_BYTES,
    )
    with patch("app.art._resize_art", side_effect=_fake_successful_resize):
        client.get(f"/api/stream/{item.id}/art")  # populates the resized copy
        resp = client.get(f"/api/stream/{item.id}/art/full")
    assert resp.status_code == 200
    assert resp.content == JPEG_BYTES


def test_art_endpoint_releases_the_db_connection_before_resizing(
    client, db_session, library_dir
):
    """The resize step (an ffmpeg subprocess call) is the slow part of this
    request - a grid of a hundred tiles all requesting their art at once
    must not hold a DB connection each for that whole time, or the pool
    exhausts and every other request in the app (not just these) backs up
    behind it."""
    from sqlalchemy.orm import Session as SASession

    item = _scan_one(
        db_session,
        library_dir,
        title="T",
        artist="A",
        album="B",
        genre="Rock",
        embed_art=JPEG_BYTES,
    )
    order: list[str] = []
    real_close = SASession.close

    def tracking_close(self):
        order.append("close")
        return real_close(self)

    def tracking_ensure(item_, file_path):
        order.append("ensure_art_thumb_cached")
        return ArtBytes(JPEG_BYTES, "image/jpeg")

    with (
        patch.object(SASession, "close", tracking_close),
        patch(
            "app.routers.stream.ensure_art_thumb_cached", side_effect=tracking_ensure
        ),
    ):
        resp = client.get(f"/api/stream/{item.id}/art")

    assert resp.status_code == 200
    # The request's own connection is released (order[0]) before the resize
    # call runs - a second close may follow from the test client's own
    # per-request session teardown, which is fine.
    assert order[:2] == ["close", "ensure_art_thumb_cached"]


@pytest.mark.parametrize("path", ["art", "art/full"])
def test_art_endpoints_do_their_db_lookups_off_the_event_loop(
    client, db_session, library_dir, path
):
    """The item, access and custom-art lookups are blocking SQLite calls;
    run on the event loop they stall every other request (and the playback
    sockets) while the database is busy. They must run on a worker thread,
    where there is no running event loop."""
    import asyncio

    import app.routers.stream as stream_router

    item = _scan_one(
        db_session, library_dir, title="T", artist="A", album="B", genre="Rock", embed_art=JPEG_BYTES
    )
    on_loop: list[bool] = []

    def _on_event_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    real_access = stream_router.can_access_root
    real_find = stream_router.find_item_art

    def tracking_access(*args, **kwargs):
        on_loop.append(_on_event_loop())
        return real_access(*args, **kwargs)

    def tracking_find(*args, **kwargs):
        on_loop.append(_on_event_loop())
        return real_find(*args, **kwargs)

    with (
        patch("app.routers.stream.can_access_root", side_effect=tracking_access),
        patch("app.routers.stream.find_item_art", side_effect=tracking_find),
    ):
        resp = client.get(f"/api/stream/{item.id}/{path}")

    assert resp.status_code == 200
    assert on_loop == [False, False]


def test_backfill_art_thumbs_pages_skips_and_reports(
    db_session, library_dir, session_factory, monkeypatch
):
    """Drives the whole startup backfill candidate loop: an item whose thumb
    already exists is skipped, a candidate that vanished mid-pass is skipped,
    an item missing its thumb is regenerated and counted, and an item that has
    no art at all is left alone."""
    from app import art

    for title in ("A", "B", "C", "D"):
        make_audio_fixture(
            library_dir / f"{title}.wav",
            title=title,
            artist="Artist",
            album="Album",
            genre="Rock",
            embed_art=JPEG_BYTES,
        )
    # No art: ensure_art_thumb_cached returns None, so the backfill must not
    # count it.
    make_audio_fixture(
        library_dir / "E.wav",
        title="E",
        artist="Artist",
        album="Album",
        genre="Rock",
    )
    root = LibraryRoot(path=str(library_dir), display_name="Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    with patch("app.art._resize_art", side_effect=_fake_successful_resize):
        scan_library_root(db_session, root)

    by_title = {item.title: item for item in db_session.query(MediaItem).all()}
    assert all(art_thumb_path(by_title[title].id).is_file() for title in "ABCD")
    assert not art_thumb_path(by_title["E"].id).is_file()
    # Keep A's and D's thumbs (the skip path); drop B's and C's so the
    # backfill finds one vanished and regenerates the other.
    art_thumb_path(by_title["B"].id).unlink()
    art_thumb_path(by_title["C"].id).unlink()
    vanished_id = by_title["B"].id

    class _VanishingItemSession:
        """A session whose get() reports one candidate as concurrently deleted."""

        def __init__(self, session):
            self._session = session

        def get(self, entity, ident, *args, **kwargs):
            if entity is MediaItem and ident == vanished_id:
                return None
            return self._session.get(entity, ident, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._session, name)

    monkeypatch.setattr(
        art, "SessionLocal", lambda: _VanishingItemSession(session_factory())
    )

    reported: list[int] = []
    with patch("app.art._resize_art", side_effect=_fake_successful_resize):
        done = art.backfill_art_thumbs(progress_cb=reported.append)

    assert done == 1
    assert reported == [1]
    assert art_thumb_path(by_title["C"].id).is_file()

    # A pass with no callback (the reporter is optional) regenerates again
    # without touching the callback path.
    art_thumb_path(by_title["C"].id).unlink()
    with patch("app.art._resize_art", side_effect=_fake_successful_resize):
        assert art.backfill_art_thumbs() == 1


def test_art_endpoint_serves_a_ready_thumbnail_without_touching_the_library(
    client, db_session, library_dir
):
    """A cached thumbnail is served as-is: no art work and no resolving the
    track's path, which touches the (possibly busy or spun-down) library
    disk."""
    with patch("app.art._resize_art", side_effect=_fake_successful_resize):
        item = _scan_one(
            db_session,
            library_dir,
            title="T",
            artist="A",
            album="B",
            genre="Rock",
            embed_art=JPEG_BYTES,
        )
    with (
        patch("app.routers.stream.media_file_path") as mock_resolve,
        patch("app.routers.stream.ensure_art_thumb_cached") as mock_ensure,
    ):
        resp = client.get(f"/api/stream/{item.id}/art")
    assert resp.status_code == 200
    assert resp.content == b"resized-bytes"
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "private, max-age=86400"
    mock_resolve.assert_not_called()
    mock_ensure.assert_not_called()


def test_full_art_endpoint_reads_embedded_art_from_the_file(
    client, db_session, library_dir
):
    item = _scan_one(
        db_session,
        library_dir,
        title="T",
        artist="A",
        album="B",
        genre="Rock",
        embed_art=JPEG_BYTES,
    )
    resp = client.get(f"/api/stream/{item.id}/art/full")
    assert resp.status_code == 200
    assert resp.content == JPEG_BYTES
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == "private, max-age=86400"
    assert find_art_path(item.id) is None


def test_resize_art_pipes_in_memory_art_through_stdin(tmp_path):
    output = tmp_path / "thumb.jpg"
    # A real header: the size is read from it before ffmpeg is started.
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10"

    def fake_ffmpeg(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"thumb")

    with patch("app.art.subprocess.run", side_effect=fake_ffmpeg) as mock_run:
        assert _resize_art(ArtBytes(png_bytes, "image/png"), output) is True
    cmd = mock_run.call_args.args[0]
    assert cmd[cmd.index("-i") + 1] == "pipe:0"
    assert mock_run.call_args.kwargs["input"] == png_bytes
    assert output.read_bytes() == b"thumb"


# --- the backfill remembers what it probed; remote back-off -----------------


def test_backfill_skips_tracks_already_probed_until_the_file_changes(
    db_session, library_dir, session_factory, monkeypatch
):
    from app import art

    item = _scan_one(db_session, library_dir, title="T", artist="A", album="B", genre="Rock")
    assert not art_thumb_path(item.id).is_file()  # no art anywhere
    monkeypatch.setattr(art, "SessionLocal", session_factory)
    probes = []
    real = art.ensure_art_thumb_cached
    monkeypatch.setattr(art, "ensure_art_thumb_cached", lambda i, p: probes.append(i.id) or real(i, p))

    assert art.backfill_art_thumbs() == 0
    assert art.backfill_art_thumbs() == 0  # restart: not probed again
    assert probes == [item.id]
    db_session.expire_all()
    assert db_session.get(MediaItem, item.id).art_checked_mtime == item.file_mtime

    db_session.get(MediaItem, item.id).file_mtime = item.file_mtime + 5  # a rescan saw a change
    db_session.commit()
    art.backfill_art_thumbs()
    assert probes == [item.id, item.id]


def test_a_failed_remote_download_backs_off_and_is_not_marked_probed(
    db_session, library_dir, session_factory, monkeypatch
):
    import httpx

    from app import art

    item = _scan_one(db_session, library_dir, title="T", artist="A", album="B", genre="Rock")
    item.cover_art_url = "https://coverartarchive.org/release/x/front"
    db_session.commit()
    monkeypatch.setattr(art, "SessionLocal", session_factory)
    monkeypatch.setattr(art, "_remote_failures", art.OrderedDict())
    with patch("app.art._download_remote_art", side_effect=httpx.ConnectTimeout("slow")) as download:
        assert art.backfill_art_thumbs() == 0
        assert art.load_full_art(item, None) is None  # a tile request meanwhile
        assert download.call_count == 1  # backed off: not retried for an hour
    db_session.expire_all()
    assert db_session.get(MediaItem, item.id).art_checked_mtime is None  # tried again later

    art._remote_failures[item.id] = 0.0  # the hour has passed
    request = httpx.Request("GET", item.cover_art_url)
    gone = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
    with patch("app.art._download_remote_art", side_effect=gone):
        assert art.load_full_art(item, None) is None
    assert not art._remote_backed_off(item.id)  # a 404 is an answer, not a failure
    with patch("app.art._download_remote_art", side_effect=ValueError("not allowed")):
        assert art.load_full_art(item, None) is None
    assert not art._remote_backed_off(item.id)


def test_remote_failure_memory_is_bounded(monkeypatch):
    from app import art

    monkeypatch.setattr(art, "_remote_failures", art.OrderedDict())
    monkeypatch.setattr(art, "MAX_REMOTE_FAILURES", 2)
    for media_id in (1, 2, 3):
        art._remote_failed(media_id)
    assert list(art._remote_failures) == [2, 3]


# --- animated GIFs ----------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_two_frame_gif_is_thumbnailed_and_accepted_as_custom_art(tmp_path, db_session, monkeypatch):
    """Without -frames:v 1 ffmpeg refuses to write two frames to one .jpg,
    and an animated GIF upload would be rejected as "Not a decodable
    image"."""
    import subprocess

    from app.custom_art import store_custom_art

    gif = tmp_path / "two.gif"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         "testsrc=size=64x48:rate=2:duration=1", "-f", "gif", str(gif)],
        check=True,
    )
    frames = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "csv=p=0", str(gif)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert frames == "2"
    out = tmp_path / "thumb.jpg"
    assert _resize_art(ArtBytes(gif.read_bytes(), "image/gif"), out) is True
    assert out.read_bytes()[:2] == b"\xff\xd8"
    monkeypatch.setattr(settings, "database_path", tmp_path / "custom.db")
    row = store_custom_art(db_session, "album", "Band\x1fLP", gif.read_bytes())
    assert (settings.custom_art_dir / row.thumb_path).is_file()


def test_art_is_not_borrowed_from_another_artists_album_of_the_same_title(
    client, db_session, library_dir
):
    """No album artist tag on either file: "Same Title" by X and by Y are
    two albums, and Y's art-less song is not served X's picture."""
    make_audio_fixture(
        library_dir / "a.wav", title="A", artist="X", album="Same Title", genre="Rock",
        embed_art=JPEG_BYTES,
    )
    make_audio_fixture(library_dir / "b.wav", title="B", artist="Y", album="Same Title", genre="Rock")
    root = LibraryRoot(path=str(library_dir), display_name="Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    bare = db_session.query(MediaItem).filter(MediaItem.title == "B").one()
    assert client.get(f"/api/stream/{bare.id}/art").status_code == 204
    assert client.get(f"/api/stream/{bare.id}/art/full").status_code == 204


def test_the_art_epoch_starts_at_zero_and_only_grows(client, db_session):
    from app.art_epoch import art_epoch, bump_art_epoch

    assert client.get("/api/version").json()["art_epoch"] == 0
    bump_art_epoch(db_session)
    first = art_epoch(db_session)
    assert first > 0
    assert client.get("/api/version").json()["art_epoch"] == first
    bump_art_epoch(db_session)
    assert art_epoch(db_session) > first
