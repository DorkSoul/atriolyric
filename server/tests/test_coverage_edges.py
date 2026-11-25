from __future__ import annotations

import asyncio
import builtins
import importlib
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app import art, cleanup, db, discovery, library_tasks, main, playback, radio
from app.artist_credits import rebuild_all_credits
from app.config import settings
from app.db import count_rows
from app.discovery import similar as discovery_similar
from app.library_query import filter_tracks_query
from app.metadata_providers import coverart, deezer, lyrics, musicbrainz
from app.models import (
    ArtistMetadataCache,
    Device,
    DiscoveryState,
    LibraryRoot,
    MediaItem,
    MediaKind,
    PlaybackQueueItem,
    PlayHistory,
    Playlist,
    PlaylistItem,
    User,
)
from app.routers import admin, playlists, stream
from app.routers import radio as radio_router
from app.routers.library import albums_artists, discovery_routes, folders, history
from app.routers.library import tracks as library_tracks
from app.routers.library import videos as library_videos
from app.security import hash_password, verify_password
from tests.conftest import startup_against

JPEG = b"\xff\xd8\xff\xe0body"
PNG = b"\x89PNG\r\n\x1a\nbody"
WEBP = b"RIFF\0\0\0\0WEBPbody"


def _item_stub(root_path: Path, relative_path: str = "track.mp3", item_id: int = 1):
    return SimpleNamespace(
        id=item_id,
        relative_path=relative_path,
        cover_art_url=None,
        library_root=SimpleNamespace(path=str(root_path)),
    )


def test_media_file_path_returns_none_for_resolution_errors_and_directories(tmp_path):
    missing = _item_stub(tmp_path / "does-not-exist")
    from app.access import media_file_path

    assert media_file_path(missing) is None

    directory = tmp_path / "directory"
    directory.mkdir()
    assert media_file_path(_item_stub(tmp_path, "directory")) is None


def test_a_cache_file_that_cannot_be_removed_does_not_stop_the_delete(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "app.db")
    root = LibraryRoot(path="/edge", display_name="Edge")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(library_root_id=root.id, relative_path="a.mp3", title="A")
    db_session.add(item)
    db_session.commit()

    cleanup.delete_media_records(db_session, [])
    db_session.commit()
    assert db_session.query(MediaItem).filter_by(id=item.id).count() == 1

    stuck = settings.thumbnails_dir / f"{item.id}.jpg"
    (stuck / "child").mkdir(parents=True)  # a directory: unlinking it fails
    others = [settings.art_thumb_dir / f"{item.id}.jpg", settings.transcode_dir / f"{item.id}_128k.mp3"]
    for path in others:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    cleanup.delete_media_records(db_session, [item.id])
    db_session.commit()
    assert db_session.query(MediaItem).filter_by(id=item.id).count() == 0
    assert stuck.is_dir()
    assert not any(path.exists() for path in others)


def test_get_db_yields_and_closes_a_session():
    generator = db.get_db()
    session = next(generator)
    assert session.is_active
    generator.close()
    with pytest.raises(StopIteration):
        next(generator)


def test_art_extraction_handles_empty_formats_and_picture_blocks(tmp_path):
    with patch("app.art.mutagen.File", return_value=None):
        assert art.extract_embedded_art(tmp_path / "none.mp3") is None

    picture = SimpleNamespace(data=JPEG, mime=None)
    flac = Mock(spec=FLAC)
    flac.pictures = [picture]
    with patch("app.art.mutagen.File", return_value=flac):
        assert art.extract_embedded_art(tmp_path / "cover.flac") == (
            JPEG,
            "image/jpeg",
        )

    empty_flac = Mock(spec=FLAC)
    empty_flac.pictures = []
    with patch("app.art.mutagen.File", return_value=empty_flac):
        assert art.extract_embedded_art(tmp_path / "empty.flac") is None

    class Cover:
        FORMAT_PNG = 1
        imageformat = 1

        def __bytes__(self):
            return PNG

    cover = Cover()
    mp4 = Mock(spec=MP4)
    mp4.tags = {"covr": [cover]}
    with patch("app.art.mutagen.File", return_value=mp4):
        assert art.extract_embedded_art(tmp_path / "cover.m4a") == (
            PNG,
            "image/png",
        )

    empty_mp4 = Mock(spec=MP4)
    empty_mp4.tags = {}
    with patch("app.art.mutagen.File", return_value=empty_mp4):
        assert art.extract_embedded_art(tmp_path / "empty.m4a") is None


def test_art_extraction_reads_vorbis_picture_block_and_rejects_bad_data(tmp_path):
    valid_picture = SimpleNamespace(data=WEBP, mime="image/webp")
    tags = {"metadata_block_picture": ["encoded-picture"]}
    audio = SimpleNamespace(tags=tags)
    with (
        patch("app.art.mutagen.File", return_value=audio),
        patch("app.art.Picture", return_value=valid_picture) as picture,
    ):
        with patch("app.art.base64.b64decode", return_value=b"decoded"):
            assert art.extract_embedded_art(tmp_path / "cover.ogg") == (
                WEBP,
                "image/webp",
            )
    picture.assert_called_once_with(b"decoded")

    with (
        patch("app.art.mutagen.File", return_value=audio),
        patch("app.art.base64.b64decode", side_effect=ValueError("bad block")),
    ):
        assert art.extract_embedded_art(tmp_path / "bad.ogg") is None


def test_art_helpers_cover_webp_remote_failures_and_resize_success(tmp_path):
    item = _item_stub(tmp_path, item_id=7)
    item.cover_art_url = "https://example.test/art"
    with (
        patch("app.art.find_art_path", return_value=None),
        patch("app.art.extract_embedded_art", return_value=None),
        patch("app.art._download_remote_art", side_effect=OSError("offline")),
    ):
        assert art.load_full_art(item, None) is None

    source = tmp_path / "source.jpg"
    output = tmp_path / "nested" / "thumb.jpg"
    # A real header: the size is read from it before ffmpeg is started.
    source.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10")

    def fake_ffmpeg(*args, **kwargs):
        Path(args[0][-1]).write_bytes(b"thumb")

    with patch("app.art.subprocess.run", side_effect=fake_ffmpeg):
        assert art._resize_art(source, output) is True
    assert output.read_bytes() == b"thumb"


def test_metadata_provider_failure_and_fallback_paths():
    with patch(
        "app.metadata_providers.coverart._client.get", side_effect=OSError("down")
    ):
        assert coverart.get_front_image_url("release") is None

    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {"data": "not-a-list"}
    with patch("app.metadata_providers.deezer.httpx.get", return_value=response):
        assert deezer.search_artist_track("Artist") is None



def test_lyrics_provider_covers_alternate_tags_exact_match_and_search_fallback(
    tmp_path,
):
    audio = SimpleNamespace(tags={"LYRICS": ["one\ntwo"]})
    with patch("app.metadata_providers.lyrics.mutagen.File", return_value=audio):
        assert lyrics.get_embedded_lyrics(tmp_path / "lyrics.mp3") == "one\ntwo"

    response = Mock(status_code=200)
    response.json.return_value = {
        "plainLyrics": "exact",
        "syncedLyrics": "[00:01] exact",
    }
    with patch("app.metadata_providers.lyrics.httpx.get", return_value=response) as get:
        result = lyrics.fetch_lrclib_lyrics("Artist", "Title", "Album", 91.4)
    assert result == {"plain": "exact", "synced": "[00:01] exact"}
    assert get.call_args.args[0].endswith("/get")

    exact_error = Mock(status_code=500)
    search_response = Mock(status_code=200)
    search_response.raise_for_status = Mock()
    search_response.json.return_value = [
        {"plainLyrics": "fallback", "syncedLyrics": None}
    ]
    with patch(
        "app.metadata_providers.lyrics.httpx.get",
        side_effect=[exact_error, search_response],
    ):
        assert lyrics.fetch_lrclib_lyrics("Artist", "Title", "Album", 91.4) == {
            "plain": "fallback",
            "synced": None,
        }

    with patch("app.metadata_providers.lyrics.httpx.get", side_effect=OSError("down")):
        assert lyrics.fetch_lrclib_lyrics("Artist", "Title", "Album", 91.4) is None

    empty_response = Mock(status_code=200)
    empty_response.raise_for_status = Mock()
    empty_response.json.return_value = []
    with patch("app.metadata_providers.lyrics.httpx.get", return_value=empty_response):
        # lrclib answered and has nothing: a definitive miss, not a failure.
        assert lyrics.fetch_lrclib_lyrics("Artist", "Title") == {}


def test_musicbrainz_recording_success_maps_top_match():
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {
        "recordings": [
            {
                "id": "recording-mbid",
                "title": "Track",
                "length": 123000,
                "artist-credit": [{"name": "Artist", "artist": {"id": "artist-mbid"}}],
                "releases": [{"title": "Album", "id": "release-mbid"}],
            }
        ]
    }
    with (
        patch("app.metadata_providers.musicbrainz._throttle.wait"),
        patch("app.metadata_providers.musicbrainz._client.get", return_value=response),
    ):
        assert musicbrainz.search_recording_result("Artist", "Track").value == {
            "recording_mbid": "recording-mbid",
            "title": "Track",
            "artist": "Artist",
            "artist_mbid": "artist-mbid",
            "album": "Album",
            "release_mbid": "release-mbid",
            "length_seconds": 123.0,
        }


def test_tag_writer_easy_and_raw_id3_paths_and_missing_audio(tmp_path):
    path = tmp_path / "song.wav"
    path.write_bytes(b"raw")
    easy_audio = MagicMock()
    easy_audio.tags = None
    easy_audio.__setitem__.side_effect = TypeError("raw ID3")
    easy_audio.add_tags = Mock()
    raw_audio = MagicMock()
    raw_audio.tags = Mock()
    raw_audio.save = Mock()
    with patch("app.tag_writer.mutagen.File", side_effect=[easy_audio, raw_audio]):
        from app.tag_writer import write_tags

        assert write_tags(path, title="Title", track_number=4) is True
    easy_audio.add_tags.assert_called_once()
    assert {call.args[0] for call in raw_audio.tags.setall.call_args_list} == {
        "TIT2",
        "TRCK",
    }

    with patch("app.tag_writer.mutagen.File", return_value=None):
        assert write_tags(path, title="No audio") is False


def test_tag_writer_returns_false_when_raw_path_fails(tmp_path):
    path = tmp_path / "song.wav"
    with patch(
        "app.tag_writer.mutagen.File", side_effect=[TypeError("easy"), OSError("raw")]
    ):
        from app.tag_writer import write_tags

        assert write_tags(path, title="Title") is False


def test_transcode_handles_missing_source_stat_for_both_modes(db_session, tmp_path):
    item = MediaItem(
        id=1,
        library_root_id=1,
        relative_path="missing.mp3",
        title="Missing",
        duration_seconds=10,
    )
    missing = tmp_path / "missing.mp3"
    with patch.object(settings, "database_path", tmp_path / "settings.db"):
        from app.transcode import ensure_transcoded

        with (
            patch("app.transcode.estimate_source_bitrate_kbps", return_value=999.0),
            patch("app.transcode.Path.stat", side_effect=OSError("missing")),
        ):
            assert ensure_transcoded(item, missing, 128) is None


def test_estimate_source_bitrate_returns_none_for_missing_file(tmp_path):
    from app.transcode import estimate_source_bitrate_kbps

    assert estimate_source_bitrate_kbps(tmp_path / "missing", 10) is None


def test_radio_url_validation_and_connection_paths(monkeypatch):
    with pytest.raises(ValueError, match="credentials"):
        radio.validate_station_url("http://user:pass@example.com/stream")
    with pytest.raises(ValueError, match="hostname"):
        radio.validate_station_url("http:///stream")
    with pytest.raises(ValueError, match="invalid port"):
        radio.validate_station_url("http://example.com:not-a-port/stream")

    host, port, path, addresses = radio.validate_station_url(
        "http://example.com/stream?a=1"
    )
    # Not resolved without resolve_dns: nothing to connect to yet.
    assert (host, port, path, addresses) == ("example.com", 80, "/stream?a=1", [])

    monkeypatch.setattr(
        "app.radio.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 80))],
    )
    assert (
        radio.validate_station_url("http://example.com/stream", resolve_dns=True)[3]
        == ["93.184.216.34"]
    )

    def dns_failure(*args, **kwargs):
        raise OSError("dns")

    monkeypatch.setattr("app.radio.socket.getaddrinfo", dns_failure)
    with pytest.raises(ValueError, match="resolved"):
        radio.validate_station_url("http://example.com/stream", resolve_dns=True)

    with pytest.raises(ValueError, match="private"):
        radio.validate_station_url("http://127.0.0.1/stream")

    monkeypatch.setattr(settings, "allow_private_radio_hosts", True)
    assert radio.validate_station_url("http://127.0.0.1/stream")[3] == ["127.0.0.1"]

    raw_socket = Mock()
    with (
        patch(
            "app.radio.validate_station_url",
            return_value=("example.com", 80, "/", ["93.184.216.34"]),
        ),
        patch("app.radio.socket.create_connection", return_value=raw_socket),
    ):
        assert radio._connect("http://example.com/") == (raw_socket, "example.com", 80, "/")

    wrapped_socket = Mock()
    ssl_context = Mock()
    ssl_context.wrap_socket.return_value = wrapped_socket
    with (
        patch(
            "app.radio.validate_station_url",
            return_value=("example.com", 443, "/", ["93.184.216.34"]),
        ),
        patch("app.radio.socket.create_connection", return_value=raw_socket),
        patch("app.radio.ssl.create_default_context", return_value=ssl_context),
    ):
        assert radio._connect("https://example.com/") == (
            wrapped_socket,
            "example.com",
            443,
            "/",
        )


class _RecvSocket:
    def __init__(self, *chunks: bytes):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False

    def recv(self, size):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


def test_radio_header_parser_error_paths():
    with pytest.raises(radio.RadioUnavailable, match="complete headers"):
        radio._read_headers(_RecvSocket(b"HTTP/1.0 200 OK\r\n"))
    with pytest.raises(radio.RadioUnavailable, match="oversized"):
        radio._read_headers(_RecvSocket(b"x" * (radio.MAX_HEADER_BYTES + 1)))
    with pytest.raises(radio.RadioUnavailable, match="invalid response"):
        radio._read_headers(_RecvSocket(b"garbage\r\n\r\n"))
    assert radio._read_headers(_RecvSocket(b"HTTP/1.0 500 Nope\r\n\r\n"))[0] == 500

    status, headers, body = radio._read_headers(
        _RecvSocket(b"HTTP/1.0 206 Partial\r\nX-Test: yes\r\n\r\nbody")
    )
    assert status == 206
    assert headers == {"x-test": "yes"}
    assert body == b"body"


def test_radio_stream_relay_and_icy_partial_metadata(monkeypatch):
    raw = _RecvSocket(b"more", b"")
    with (
        patch("app.radio._connect", return_value=(raw, "station", 80, "/stream")),
        patch("app.radio._read_headers", return_value=(200, {}, b"initial")),
    ):
        assert list(radio.stream_audio_with_icy(10, "http://station/stream")) == [
            b"initial",
            b"more",
        ]
    assert raw.closed

    invalid_meta = _RecvSocket(b"")
    with (
        patch("app.radio._connect", return_value=(invalid_meta, "station", 80, "/")),
        patch("app.radio._read_headers", return_value=(200, {"icy-metaint": "bad"}, b"")),
    ):
        assert list(radio.stream_audio_with_icy(11, "http://station/")) == []

    zero_meta = _RecvSocket(b"")
    with (
        patch("app.radio._connect", return_value=(zero_meta, "station", 80, "/")),
        patch("app.radio._read_headers", return_value=(200, {"icy-metaint": "0"}, b"")),
    ):
        assert list(radio.stream_audio_with_icy(12, "http://station/")) == []

    title_metadata = b"StreamTitle='Artist - Track';"
    padded = title_metadata + b"\x00" * ((16 - len(title_metadata) % 16) % 16)
    body = b"abc" + bytes([len(padded) // 16]) + padded
    icy = _RecvSocket(b"c", b"d" + bytes([0]))
    with (
        patch("app.radio._connect", return_value=(icy, "station", 80, "/")),
        patch("app.radio._read_headers", return_value=(200, {"icy-metaint": "3"}, body)),
        patch("app.radio.time.time", return_value=123.0),
    ):
        chunks = list(radio.stream_audio_with_icy(13, "http://station/"))
    assert chunks[0] == b"abc"
    assert radio.get_now_playing(13) == {"title": "Artist - Track", "updated_at": 123.0}


def test_radio_stream_returns_cleanly_when_metadata_arrives_in_chunks():
    metadata = b"StreamTitle='A';" + b"\x00" * 0
    padded = metadata + b"\x00" * (16 - len(metadata))
    sock = _RecvSocket(b"x", bytes([1]), padded[:5], padded[5:], b"")
    with (
        patch("app.radio._connect", return_value=(sock, "station", 80, "/")),
        patch("app.radio._read_headers", return_value=(200, {"icy-metaint": "1"}, b"")),
    ):
        output = list(radio.stream_audio_with_icy(14, "http://station/"))
    assert output == [b"x"]
    assert radio.get_now_playing(14)["title"] == "A"


def test_radio_stream_returns_cleanly_when_metadata_is_truncated():
    # The metadata length byte announces a block larger than the remaining
    # socket data, so the iterator should stop cleanly when the socket closes.
    sock = _RecvSocket(b"short", b"")
    with (
        patch("app.radio._connect", return_value=(sock, "station", 80, "/")),
        patch(
            "app.radio._read_headers",
            return_value=(200, 
                {"icy-metaint": "1"},
                b"x",
            ),
        ),
    ):
        assert list(radio.stream_audio_with_icy(15, "http://station/")) == [b"x"]


def test_a_non_numeric_track_number_tag_reads_as_none(tmp_path):
    from app.scanner import read_audio_tags

    audio = SimpleNamespace(tags={"tracknumber": ["not-a-number"]}, info=None)
    with patch("app.scanner.mutagen.File", return_value=audio):
        assert read_audio_tags(tmp_path / "bad.mp3")["track_number"] is None


def test_a_scan_skips_a_file_whose_path_cannot_be_resolved(db_session, tmp_path, monkeypatch):
    from app.scanner import scan_library_root

    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    db_session.add(root)
    db_session.commit()
    (tmp_path / "broken.wav").write_bytes(b"x")
    real_resolve = Path.resolve

    def resolve_with_broken_file(self, strict=False):
        if self.name == "broken.wav":
            raise OSError("broken link")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr("app.scanner.Path.resolve", resolve_with_broken_file)
    assert scan_library_root(db_session, root) == {
        "added": 0,
        "updated": 0,
        "removed": 0,
        "failed": 0,
        "art_redone": 0,
        "missing_held": 0,
        "unreadable_dirs": [],
        "stopped": False,
    }


def test_track_filters_combine_artist_and_album(db_session, tmp_path):
    user = db_session.query(User).one()
    root = LibraryRoot(path=str(tmp_path), display_name="Other")
    db_session.add(root)
    db_session.commit()
    db_session.add_all([
        MediaItem(
            library_root_id=root.id, kind=MediaKind.track, relative_path="one.mp3",
            title="One", artist="Artist", album="Album",
        ),
        MediaItem(
            library_root_id=root.id, kind=MediaKind.track, relative_path="two.mp3",
            title="Two", artist="Artist", album="Other Album",
        ),
    ])
    db_session.commit()
    assert count_rows(db_session, filter_tracks_query(db_session, user, {"artist": "Artist", "album": "Album"})) == 1


def test_scanner_commits_removed_batch(db_session, tmp_path, monkeypatch):
    from app.scanner import scan_library_root

    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path="gone.mp3",
        title="Gone",
    )
    db_session.add(item)
    db_session.commit()
    monkeypatch.setattr("app.scanner.SCAN_COMMIT_BATCH_SIZE", 1)
    # Confirmed: a scan that finds no files at all otherwise removes nothing.
    assert scan_library_root(db_session, root, mode="confirm_removals") == {
        "added": 0,
        "updated": 0,
        "removed": 1,
        "failed": 0,
        "art_redone": 0,
        "missing_held": 0,
        "unreadable_dirs": [],
        "stopped": False,
    }


@pytest.mark.real_background_jobs
def test_discovery_scheduler_closes_session_after_a_failed_refresh(monkeypatch):
    class FakeSession:
        closed = False

        def close(self):
            self.closed = True

    session = FakeSession()
    sleeps = 0

    async def fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    async def run_scheduler():
        with (
            patch.object(main.asyncio, "sleep", side_effect=fake_sleep),
            patch.object(main, "SessionLocal", return_value=session),
            patch.object(
                main, "run_due_auto_refreshes", side_effect=RuntimeError("boom")
            ),
        ):
            await main.discovery_scheduler_loop()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_scheduler())
    assert session.closed


@pytest.mark.real_background_jobs
def test_start_art_thumb_backfill_logs_completion_and_failure(monkeypatch, caplog):
    monkeypatch.setattr(main, "spawn", lambda name, target, *args: target(*args))
    monkeypatch.setattr(main, "backfill_art_thumbs", lambda progress_cb=None: 4)
    caplog.set_level(logging.INFO, logger=main.logger.name)
    main._start_art_thumb_backfill()
    assert any(
        record.getMessage() == "Art thumbnail backfill complete: 4 generated"
        for record in caplog.records
    )

    def _boom(progress_cb=None):
        raise RuntimeError("no art")

    monkeypatch.setattr(main, "backfill_art_thumbs", _boom)
    caplog.clear()
    caplog.set_level(logging.ERROR, logger=main.logger.name)
    main._start_art_thumb_backfill()
    assert any(
        record.getMessage() == "Art thumbnail backfill failed"
        for record in caplog.records
    )


def test_main_middleware_rejects_cross_origin_writes_and_health_is_available(client):
    response = client.post(
        "/api/auth/logout", headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert client.get("/api/health").json() == {"status": "ok"}


def test_main_mounts_frontend_when_a_build_directory_exists(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    original_path = main.settings.web_dist_path
    try:
        main.settings.web_dist_path = tmp_path
        reloaded = importlib.reload(main)
        assert any(
            getattr(route, "name", None) == "web" for route in reloaded.app.routes
        )
    finally:
        main.settings.web_dist_path = original_path
        importlib.reload(main)


def test_frontend_shell_is_revalidated_and_hashed_assets_cached(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc.js").write_text("1")
    app = Starlette()
    app.mount("/", main.WebAppFiles(directory=tmp_path, html=True))
    web = TestClient(app)

    assert web.get("/").headers["cache-control"] == "no-cache"
    assert web.get("/index.html").headers["cache-control"] == "no-cache"
    assert "immutable" in web.get("/assets/index-abc.js").headers["cache-control"]
    assert web.get("/assets/missing.js").status_code == 404


def test_version_endpoint_reports_the_served_frontend_build(client, tmp_path):
    original_path = main.settings.web_dist_path
    try:
        main.settings.web_dist_path = None
        assert client.get("/api/version").json() == {"version": None, "debug_log": False, "android_app": None, "art_epoch": 0}

        main.settings.web_dist_path = tmp_path
        assert client.get("/api/version").json() == {"version": None, "debug_log": False, "android_app": None, "art_epoch": 0}

        (tmp_path / "version.json").write_text("not json")
        assert client.get("/api/version").json() == {"version": None, "debug_log": False, "android_app": None, "art_epoch": 0}

        (tmp_path / "version.json").write_text('{"version": "0.5.0"}')
        assert client.get("/api/version").json() == {"version": "0.5.0", "debug_log": False, "android_app": None, "art_epoch": 0}
    finally:
        main.settings.web_dist_path = original_path


def test_admin_validation_and_missing_resource_paths(db_session, tmp_path):
    user = db_session.query(User).one()

    with pytest.raises(HTTPException, match="blank"):
        admin.create_user(
            admin.UserCreateIn(username="   ", password="password"), db_session
        )
    with pytest.raises(HTTPException, match="taken"):
        admin.create_user(
            admin.UserCreateIn(username=user.username, password="password"), db_session
        )
    with pytest.raises(HTTPException, match="between 1 and 72"):
        admin.create_user(
            admin.UserCreateIn(username="long-password", password="x" * 73), db_session
        )

    with pytest.raises(HTTPException, match="not found"):
        admin.delete_user(99999, BackgroundTasks(), db_session, user)
    with pytest.raises(HTTPException, match="not found"):
        admin.delete_root(99999, db_session)
    with pytest.raises(HTTPException, match="not found"):
        admin.scan_root(99999, db_session)
    with pytest.raises(HTTPException, match="not found"):
        admin.enrich_root(99999, db_session, user)
    with pytest.raises(HTTPException, match="not found"):
        admin.grant_access(99999, user.id, db_session)

    root = LibraryRoot(path=str(tmp_path / "root"), display_name="Root")
    db_session.add(root)
    db_session.commit()
    with pytest.raises(HTTPException, match="absolute"):
        admin.create_root(
            admin.LibraryRootIn(path="relative", display_name="Root"), db_session
        )
    with pytest.raises(HTTPException, match="blank"):
        admin.create_root(
            admin.LibraryRootIn(path=str(tmp_path / "other"), display_name="   "),
            db_session,
        )
    with pytest.raises(HTTPException, match="already exists"):
        admin.create_root(
            admin.LibraryRootIn(path=root.path, display_name="Duplicate"), db_session
        )

    with patch.object(
        db_session, "commit", side_effect=IntegrityError("insert", {}, Exception())
    ):
        with pytest.raises(HTTPException, match="already exists"):
            admin.create_root(
                admin.LibraryRootIn(path=str(tmp_path / "race"), display_name="Race"),
                db_session,
            )


def test_admin_tag_edit_and_bulk_remove_failure_paths(db_session, tmp_path):
    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    db_session.add(root)
    db_session.commit()
    path = tmp_path / "song.mp3"
    path.write_bytes(b"song")
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path=path.name,
        title="Old",
        artist="Old Artist",
        album="Old Album",
        genre="Old Genre",
        track_number=1,
    )
    db_session.add(item)
    db_session.commit()

    with patch("app.routers.admin.write_tags", return_value=True):
        assert admin._apply_tag_edit(
            db_session,
            item,
            admin.TagEditIn(
                title="New",
                artist="New Artist",
                album="New Album",
                genre="New Genre",
                track_number=2,
            ),
        )
    assert (item.title, item.artist, item.album, item.genre, item.track_number) == (
        "New",
        "New Artist",
        "New Album",
        "New Genre",
        2,
    )

    with patch("app.routers.admin.media_file_path", return_value=None):
        assert not admin._apply_tag_edit(
            db_session, item, admin.TagEditIn(title="Nope")
        )
    assert admin.bulk_remove_media(
        admin.BulkRemoveIn(media_item_ids=[99999]), db_session
    ) == {"removed": [], "failed": [99999]}

    with patch(
        "app.routers.admin.delete_media_records",
        side_effect=RuntimeError("db failed"),
    ):
        result = admin.bulk_remove_media(
            admin.BulkRemoveIn(media_item_ids=[item.id]), db_session
        )
    assert result == {"removed": [], "failed": [item.id]}
    result = admin.bulk_remove_media(
        admin.BulkRemoveIn(media_item_ids=[item.id]), db_session
    )
    assert result == {"removed": [item.id], "failed": []}
    assert path.read_bytes() == b"song"

    with pytest.raises(HTTPException, match="not found"):
        admin.edit_tags(99999, admin.TagEditIn(title="Nope"), db_session)


def test_auth_rate_limit_and_change_validation(db_session):
    from app import login_limits
    from app.routers import auth

    login_limits._login_failures.clear()
    key = "coverage-test"
    login_limits._login_failures[key].extend(
        [login_limits.time.monotonic() - login_limits.LOGIN_WINDOW_SECONDS - 1, login_limits.time.monotonic()]
    )
    login_limits.check_login_rate_limit(key)
    login_limits._login_failures[key].extend([login_limits.time.monotonic()] * login_limits.LOGIN_MAX_FAILURES)
    with pytest.raises(HTTPException, match="Too many"):
        login_limits.check_login_rate_limit(key)
    # A success never clears the address's failures (see test_auth).
    login_limits.record_login_result(key, True, "someone")
    assert key in login_limits._login_failures

    with patch.object(login_limits, "LOGIN_MAX_TRACKED_KEYS", 2):
        login_limits._login_failures.clear()
        login_limits._login_failures["expired"].append(
            login_limits.time.monotonic() - login_limits.LOGIN_WINDOW_SECONDS - 1
        )
        login_limits._login_failures["active"].append(login_limits.time.monotonic())
        login_limits.record_login_result("third", False)
        assert "expired" not in login_limits._login_failures

        login_limits._login_failures.clear()
        login_limits._login_failures["first"].append(login_limits.time.monotonic())
        login_limits._login_failures["second"].append(login_limits.time.monotonic())
        login_limits.record_login_result("third", False)
        assert len(login_limits._login_failures) == 2

    user = db_session.query(User).one()
    with pytest.raises(HTTPException, match="blank"):
        auth.change_username(
            auth.UsernameChangeIn(current_password="changeme", new_username="   "),
            db_session,
            user,
        )
    with pytest.raises(HTTPException, match="incorrect"):
        auth.change_password(
            auth.PasswordChangeIn(current_password="wrong", new_password="newpass1"),
            Mock(cookies={}),
            BackgroundTasks(),
            db_session,
            user,
        )
    with patch(
        "app.routers.auth.hash_password", side_effect=ValueError("bad password")
    ):
        with pytest.raises(HTTPException, match="bad password"):
            auth.change_password(
                auth.PasswordChangeIn(
                    current_password="changeme", new_password="newpass1"
                ),
                Mock(cookies={}),
                BackgroundTasks(),
                db_session,
                user,
            )


def test_library_route_error_paths_and_sorting(db_session, tmp_path):
    user = db_session.query(User).one()
    root_path = tmp_path / "library"
    root_path.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.wav").write_bytes(b"secret")
    (root_path / "folder").mkdir()
    (root_path / "good.wav").write_bytes(b"good")
    (root_path / "broken.wav").write_bytes(b"broken")
    (root_path / "escape.wav").symlink_to(outside / "secret.wav")
    root = LibraryRoot(path=str(root_path), display_name="Library")
    db_session.add(root)
    db_session.commit()

    with pytest.raises(HTTPException, match="Directory not found"):
        folders.browse(root.id, db=db_session, user=user, path="../outside")

    real_resolve = Path.resolve

    def resolve_with_one_bad_entry(self, strict=False):
        if self.name == "broken.wav":
            raise OSError("unreadable link")
        return real_resolve(self, strict=strict)

    with patch("app.routers.library.folders.Path.resolve", resolve_with_one_bad_entry):
        result = folders.browse(root.id, db=db_session, user=user, path="")
    # The unreadable entry and the link out of the root are skipped; files
    # without a scanned row are not listed.
    assert [d.name for d in result.directories] == ["folder"]
    assert result.items == []

    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path="missing.wav",
        title="Missing",
        artist="Artist",
        album="Album",
        release_date="2020-01-01",
    )
    db_session.add(item)
    db_session.commit()
    rebuild_all_credits(db_session)
    with pytest.raises(HTTPException, match="Underlying file"):
        folders.get_lyrics(item.id, db_session, user)

    with patch(
        "app.routers.library.discovery_routes.refresh_discovery_playlist", return_value={"ok": True}
    ):
        assert discovery_routes.refresh_discovery(db_session, user) == {"ok": True}
    with patch("app.routers.library.discovery_routes.generate_playlist_from_track", return_value=None):
        with pytest.raises(HTTPException, match="not found"):
            discovery_routes.generate_playlist(
                discovery_routes.GeneratePlaylistIn(media_item_id=item.id), db_session, user
            )
    assert library_tracks.get_media_by_ids(ids="not-an-id", db=db_session, user=user) == []

    video = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.video,
        relative_path="folder/clip.mkv",
        title="A Clip",
    )
    db_session.add(video)
    db_session.commit()
    assert [
        v.title for v in library_videos.list_videos(q="clip", dir=None, limit=200, offset=0, db=db_session, user=user)
    ] == ["A Clip"]
    for sort, direction in ((albums_artists.BrowseSort.last_played, "asc"), (albums_artists.BrowseSort.date_published, "desc")):
        artists = albums_artists.list_artists(
            sort=sort, direction=direction, limit=100, offset=0, db=db_session, user=user
        )
        assert [(a.artist, a.track_count) for a in artists] == [("Artist", 1)]


def _history_library(db_session, tmp_path):
    """Four rock tracks by A, A, B and C (the first played twice, the rest
    once) and a playlist holding the first."""
    user = db_session.query(User).one()
    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    db_session.add(root)
    db_session.commit()
    tracks = []
    for index, artist_name in enumerate(("A", "A", "B", "C"), start=1):
        track = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"{index}.mp3",
            title=f"Track {index}",
            artist=artist_name,
            album="Album",
            genre="Rock",
        )
        db_session.add(track)
        tracks.append(track)
    db_session.commit()
    playlist = Playlist(user_id=user.id, name="Playlist")
    db_session.add(playlist)
    db_session.commit()
    db_session.add(PlaylistItem(playlist_id=playlist.id, media_item_id=tracks[0].id, position=0))
    now = history.utcnow()
    db_session.add_all(
        PlayHistory(user_id=user.id, media_item_id=track.id, played_at=now)
        for track in [tracks[0], *tracks]
    )
    db_session.commit()
    return user, tracks, playlist


def test_play_source_art_is_the_most_played_track_of_an_artist_or_playlist(db_session, tmp_path):
    user, tracks, playlist = _history_library(db_session, tmp_path)
    assert history._resolve_play_source_art(db_session, user, "artist", {"artist": "A"}) == tracks[0].id
    assert (
        history._resolve_play_source_art(db_session, user, "playlist", {"playlist_id": playlist.id})
        == tracks[0].id
    )


def test_library_source_previews_are_empty_for_no_match_and_one_per_artist(db_session, tmp_path):
    user, tracks, _playlist = _history_library(db_session, tmp_path)
    assert history._library_source_preview_ids(db_session, user, {"filters": {"genre": "does-not-exist"}}) == []
    assert history._library_source_preview_ids(db_session, user, {"filters": {}}, limit=2) == [
        tracks[0].id,
        tracks[2].id,
    ]


def test_playlist_validation(db_session, tmp_path):
    user = db_session.query(User).one()
    with pytest.raises(HTTPException, match="blank"):
        playlists.create_playlist(
            playlists.PlaylistCreate(name="   "), db_session, user
        )

    playlist = Playlist(user_id=user.id, name="Empty")
    db_session.add(playlist)
    db_session.commit()
    with pytest.raises(HTTPException, match="Playlist item"):
        playlists.remove_item(playlist.id, 99999, db_session, user)


def test_radio_router_missing_resource_paths(db_session):
    user = db_session.query(User).one()
    with pytest.raises(HTTPException, match="blank"):
        radio_router.create_station(
            radio_router.StationIn(name="   ", stream_url="http://example.com/stream"),
            db_session,
            user,
        )
    with pytest.raises(HTTPException, match="Station not found"):
        radio_router.delete_station(99999, db_session, user)
    with pytest.raises(HTTPException, match="Station not found"):
        radio_router._get_visible_station(db_session, user, 99999)


def _http_request(headers: dict[str, str] | None = None):
    raw_headers = [
        (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/stream/1",
            "query_string": b"",
            "headers": raw_headers,
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )


def _stream_item(db_session, tmp_path, name="song.mp3", data=b"abcdef"):
    root = db_session.query(LibraryRoot).filter_by(path=str(tmp_path)).one_or_none()
    if root is None:
        root = LibraryRoot(path=str(tmp_path), display_name="Library")
        db_session.add(root)
        db_session.commit()
    (tmp_path / name).write_bytes(data)
    item = MediaItem(library_root_id=root.id, kind=MediaKind.track, relative_path=name, title=name)
    db_session.add(item)
    db_session.commit()
    return item


def test_a_range_with_neither_start_nor_end_is_invalid(db_session, tmp_path):
    user = db_session.query(User).one()
    item = _stream_item(db_session, tmp_path)
    with pytest.raises(HTTPException, match="Invalid Range"):
        stream.stream_media(item.id, _http_request({"range": "bytes=-"}), db=db_session, user=user, max_bitrate=None)


def test_a_suffix_range_of_an_empty_file_is_not_satisfiable(db_session, tmp_path):
    user = db_session.query(User).one()
    item = _stream_item(db_session, tmp_path, "empty.mp3", b"")
    with pytest.raises(HTTPException, match="not satisfiable"):
        stream.stream_media(item.id, _http_request({"range": "bytes=-0"}), db=db_session, user=user, max_bitrate=None)


def test_a_stream_ends_when_the_file_reads_short(db_session, tmp_path):
    user = db_session.query(User).one()
    item = _stream_item(db_session, tmp_path)

    class EmptyFile:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def seek(self, _position):
            pass

        def read(self, _size):
            return b""

    response = stream.stream_media(item.id, _http_request(), db=db_session, user=user, max_bitrate=None)

    async def consume(body_iterator):
        return [chunk async for chunk in body_iterator]

    with patch.object(builtins, "open", return_value=EmptyFile()):
        assert asyncio.run(consume(response.body_iterator)) == []


def test_video_thumbnail_is_404_for_an_unknown_item_204_without_one_and_cached_with_one(
    db_session, tmp_path
):
    user = db_session.query(User).one()
    item = _stream_item(db_session, tmp_path)
    with pytest.raises(HTTPException, match="Media item"):
        stream.get_thumbnail(99999, db_session, user)
    assert stream.get_thumbnail(item.id, db_session, user).status_code == 204
    thumbnail = tmp_path / "thumb.jpg"
    thumbnail.write_bytes(b"thumb")
    with patch("app.routers.stream.thumbnail_path", return_value=thumbnail):
        response = stream.get_thumbnail(item.id, db_session, user)
    assert response.path == thumbnail
    assert response.headers["cache-control"] == "private, max-age=86400"


def test_stream_full_art_and_played_visibility_errors(db_session, tmp_path):
    user = db_session.query(User).one()
    # get_art_full is async (see app.routers.stream) - drive it through
    # asyncio.run like any coroutine.
    with pytest.raises(HTTPException, match="Media item"):
        asyncio.run(stream.get_art_full(99999, db_session, user))

    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    db_session.add(root)
    db_session.commit()
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path="missing.mp3",
        title="Missing",
    )
    db_session.add(item)
    db_session.commit()
    with patch("app.routers.stream.load_full_art", return_value=None):
        response = asyncio.run(stream.get_art_full(item.id, db_session, user))
        assert response.status_code == 204

    private_root = LibraryRoot(
        path=str(tmp_path / "private"), display_name="Private", is_shared=False
    )
    (tmp_path / "private").mkdir()
    db_session.add(private_root)
    db_session.commit()
    private_item = MediaItem(
        library_root_id=private_root.id,
        kind=MediaKind.track,
        relative_path="private.mp3",
        title="Private",
    )
    other = User(username="stream-other", password_hash=hash_password("password"))
    db_session.add_all([private_item, other])
    db_session.commit()
    with pytest.raises(HTTPException, match="Media item"):
        stream.record_played(private_item.id, Mock(), db_session, other)


def test_playback_internal_validation_and_fallback_paths(
    db_session, tmp_path, monkeypatch
):
    user = db_session.query(User).one()
    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    db_session.add(root)
    db_session.commit()
    tracks = []
    for index, artist_name in enumerate(("A", "A", "B"), start=1):
        track = MediaItem(
            library_root_id=root.id,
            kind=MediaKind.track,
            relative_path=f"{index}.mp3",
            title=f"Track {index}",
            artist=artist_name,
            album="Album",
            track_number=index,
        )
        db_session.add(track)
        tracks.append(track)
    db_session.commit()

    playback.touch_device(db_session, 99999)  # an unknown device is not created
    assert db_session.query(Device).count() == 0
    device = playback.register_device(db_session, user, "Laptop", "web")
    long_ago = datetime(2000, 1, 1)
    device.last_seen_at = long_ago
    db_session.commit()
    playback.touch_device(db_session, device.id)
    assert db_session.get(Device, device.id).last_seen_at > long_ago
    state = playback.get_or_create_state(db_session, user)
    assert playback._get_context(state) is None
    assert (
        playback._natural_index_of(db_session, user, {"kind": "ids", "ids": [1]}, None)
        is None
    )
    assert (
        playback._natural_index_of(db_session, user, {"kind": "ids", "ids": [1]}, 99999)
        is None
    )

    library_context = {"kind": "library", "filters": {}}
    assert playback._context_natural_batch(
        db_session, user, {**library_context, "exclude_ids": [tracks[0].id]}, 0, 10
    ) == [tracks[1].id, tracks[2].id]
    shuffled_state = playback.get_or_create_state(db_session, user)
    shuffled_state.is_shuffled = True
    shuffled_state.shuffle_seed = 42
    shuffled = playback._context_shuffled_batch(db_session, user, library_context, 0, 10, 42)
    assert sorted(shuffled) == sorted(t.id for t in tracks)
    # The seed fixes the order, and paging walks the same order.
    assert playback._context_shuffled_batch(db_session, user, library_context, 0, 10, 42) == shuffled
    assert playback._context_shuffled_batch(db_session, user, library_context, 1, 10, 42) == shuffled[1:]
    without_first = playback._context_shuffled_batch(
        db_session, user, {**library_context, "exclude_ids": [tracks[0].id]}, 0, 10, 42
    )
    assert without_first == [i for i in shuffled if i != tracks[0].id]

    with patch(
        "app.playback.similar_tracks_for_seed",
        return_value=[tracks[0].id, tracks[1].id],
    ):
        assert playback._continuation_tracks(db_session, user, tracks[0].id) == [
            tracks[1].id
        ]

    playback._handle_context_exhaustion(db_session, user, state)  # no context: nothing to do
    assert playback._get_context(state) is None
    # A context with tracks left is not exhausted yet: left as it is.
    playback._set_context(state, {"kind": "ids", "ids": [tracks[0].id]}, 3)
    state.tail_cursor = 0
    playback._handle_context_exhaustion(db_session, user, state)
    assert (playback._get_context(state), state.tail_cursor) == ({"kind": "ids", "ids": [tracks[0].id]}, 0)

    assert (
        playback.set_queue_context(
            db_session, user, {"kind": "ids", "ids": []}, tracks[0].id
        )
        == "Invalid queue"
    )
    assert (
        playback.set_queue_context(
            db_session, user, {"kind": "ids", "ids": ["bad"]}, tracks[0].id
        )
        == "Invalid queue"
    )
    assert (
        playback.set_queue_context(
            db_session, user, {"kind": "ids", "ids": list(range(5001))}, tracks[0].id
        )
        == "Invalid queue"
    )
    assert (
        playback.set_queue_context(db_session, user, {"kind": "library"}, tracks[0].id)
        == "Invalid queue"
    )
    assert (
        playback.set_queue_context(
            db_session,
            user,
            {"kind": "library", "filters": {"artist": "Nobody"}},
            tracks[0].id,
        )
        == "No matching tracks"
    )
    assert (
        playback.set_queue_context(
            db_session, user, {"kind": "ids", "ids": [tracks[0].id]}, 99999
        )
        == "Media item 99999 not found"
    )
    with patch("app.playback._natural_index_of", return_value=None):
        assert (
            playback.set_queue_context(
                db_session,
                user,
                {"kind": "ids", "ids": [tracks[0].id]},
                tracks[0].id,
            )
            == f"Media item {tracks[0].id} not found"
        )

    assert (
        playback.add_to_queue(db_session, user, tracks[0].id, "invalid")
        == "Queue mode must be 'next' or 'end'"
    )
    assert (
        playback.add_to_queue(db_session, user, 99999, "end")
        == "Media item 99999 not found"
    )
    empty_state = playback.get_or_create_state(db_session, user)
    empty_state.queue_index = 0
    playback._apply_index(db_session, user, empty_state, 0)
    assert empty_state.media_item_id is None
    playback.set_shuffle(db_session, user, False)
    assert playback.set_repeat(db_session, user, "invalid") == "Invalid repeat mode"

    playback.set_queue_context(
        db_session,
        user,
        {"kind": "ids", "ids": [tracks[0].id, tracks[1].id]},
        tracks[0].id,
    )
    assert playback.set_repeat(db_session, user, "all") is None
    assert (
        playback.move_queue_item(db_session, user, 123456, 0)
        == "Direction must be -1 or 1"
    )
    assert (
        playback.move_queue_item(db_session, user, 123456, 1) == "Queue item not found"
    )
    queue = playback._queue(db_session, user)
    current_id = queue[0].id
    assert playback.move_queue_item(db_session, user, current_id, 1) is None
    # The playing entry moved down, and the current index followed it.
    assert [q.media_item_id for q in playback._queue(db_session, user)] == [tracks[1].id, tracks[0].id]
    assert playback.get_or_create_state(db_session, user).queue_index == 1

    # Make one valid and one inaccessible queue row so snapshot cleanup also
    # has to renumber the surviving row.
    private_root = LibraryRoot(
        path=str(tmp_path / "private"), display_name="Private", is_shared=False
    )
    (tmp_path / "private").mkdir()
    db_session.add(private_root)
    db_session.commit()
    private_track = MediaItem(
        library_root_id=private_root.id,
        kind=MediaKind.track,
        relative_path="private.mp3",
        title="Private",
    )
    db_session.add(private_track)
    db_session.commit()
    db_session.add(
        PlaybackQueueItem(
            user_id=user.id,
            position=1,
            original_position=1,
            media_item_id=private_track.id,
            source="generated",
        )
    )
    db_session.commit()
    state = playback.get_or_create_state(db_session, user)
    state.queue_index = 1
    state.media_item_id = private_track.id
    db_session.commit()
    user.is_admin = False
    db_session.commit()
    snapshot = playback.build_state_snapshot(db_session, user)
    assert set(snapshot["queue_media_item_ids"]) == {tracks[0].id, tracks[1].id}


def test_playback_window_trim_and_edge_exhaustion(db_session, tmp_path, monkeypatch):
    user = db_session.query(User).one()
    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    db_session.add(root)
    db_session.commit()
    track = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path="track.mp3",
        title="Track",
    )
    db_session.add(track)
    db_session.commit()
    state = playback.get_or_create_state(db_session, user)

    rows = [
        PlaybackQueueItem(
            user_id=user.id,
            position=index,
            original_position=index,
            media_item_id=track.id,
            source="generated",
        )
        for index in range(playback.HISTORY_KEEP + 2)
    ]
    db_session.add_all(rows)
    db_session.commit()
    state.queue_index = playback.HISTORY_KEEP + 1
    db_session.commit()
    playback._trim_history(db_session, user, state)
    assert state.queue_index == playback.HISTORY_KEEP

    queue_length = len(playback._queue(db_session, user))
    no_context = playback.get_or_create_state(db_session, user)
    no_context.queue_context_json = None
    # Nothing to draw from: neither the top-up nor the end-of-context step
    # adds anything or sets a context.
    playback._ensure_window(db_session, user, no_context)
    playback._handle_context_exhaustion(db_session, user, no_context)
    assert len(playback._queue(db_session, user)) == queue_length
    assert playback._get_context(no_context) is None

    playback._set_context(no_context, {"kind": "ids", "ids": [track.id]}, 1)
    no_context.tail_cursor = 0
    monkeypatch.setattr(playback, "WINDOW_TARGET", 0)
    playback._ensure_window(db_session, user, no_context)  # a full window: nothing drawn
    assert no_context.tail_cursor == 0
    assert len(playback._queue(db_session, user)) == queue_length


# The playback socket's command error paths, its track-end failure logging
# and its heartbeat handling are pinned at the protocol level in
# tests/test_playback_ws.py (test_every_command_error_is_reported_over_the_socket,
# test_a_failing_track_end_is_logged_and_closes_the_socket,
# test_heartbeat_is_acknowledged_to_its_sender_only): the handler runs its
# database work in worker threads, so driving it with a fake socket and
# patched internals would not exercise the real paths.


def test_playback_diagnostic_number_validation():
    from app.routers import playback as playback_router

    assert playback_router._finite_diagnostic_number("181") is None
    assert playback_router._finite_diagnostic_number(10**10000) is None
    assert playback_router._finite_diagnostic_number(181.23456) == 181.235
    assert playback_router._finite_diagnostic_number(float("inf")) is None


def test_connection_manager_stops_sending_to_dead_connections():
    from app.ws_manager import ConnectionManager

    manager = ConnectionManager()
    assert manager.disconnect(1, 1, object()) is False
    good = SimpleNamespace(send_json=AsyncMock())
    bad = SimpleNamespace(
        send_json=AsyncMock(side_effect=RuntimeError("socket closed"))
    )
    assert manager.connect(1, 1, good) is None
    assert manager.connect(1, 2, bad) is None
    asyncio.run(manager.broadcast(1, {"type": "state"}))
    good.send_json.assert_awaited_once()
    # The dead socket stays registered for its own handler to unregister,
    # but is not sent to again.
    assert manager._connections[1] == {1: good, 2: bad}
    asyncio.run(manager.broadcast(1, {"type": "state"}))
    bad.send_json.assert_awaited_once()
    asyncio.run(manager.broadcast(99, {"type": "state"}))


def test_connection_manager_disconnect_requires_the_registered_socket():
    from app.ws_manager import ConnectionManager

    manager = ConnectionManager()
    old, new = object(), object()
    assert manager.connect(1, 5, old) is None
    assert manager.connect(1, 5, new) is old
    # The replaced socket closing late must not evict its replacement.
    assert manager.disconnect(1, 5, old) is False
    assert manager._connections == {1: {5: new}}
    assert manager.disconnect(1, 5, new) is True
    assert manager._connections == {}


def test_discovery_scheduler_and_cache_edge_conditions(db_session):
    with patch("app.discovery.scheduler.refresh_discovery_playlist") as refresh:
        discovery.run_due_auto_refreshes(db_session, current_hour=None)  # nobody has a schedule
    refresh.assert_not_called()

    # A deleted user's schedule goes with them (foreign keys forbid a state
    # whose user is gone), so the scheduler never refreshes for them.
    gone = User(username="gone", password_hash=hash_password("x"))
    db_session.add(gone)
    db_session.commit()
    db_session.add(
        DiscoveryState(
            user_id=gone.id,
            auto_refresh_enabled=True,
            auto_refresh_hour=7,
            last_auto_refresh_date=None,
        )
    )
    db_session.commit()
    cleanup.delete_user_records(db_session, gone.id)
    db_session.commit()
    assert db_session.query(DiscoveryState).count() == 0
    with patch("app.discovery.scheduler.refresh_discovery_playlist") as refresh:
        discovery.run_due_auto_refreshes(db_session, current_hour=7)
    refresh.assert_not_called()

    cache = ArtistMetadataCache(artist="Cached")
    db_session.add(cache)
    db_session.commit()
    assert (
        discovery_similar._similar_artists_for(db_session, "Cached", "KEY", allow_fetch=False)
        == set()
    )
    assert discovery_similar._genres_by_artist(db_session, [], []) == {}


def test_discovery_explore_caps_results_and_spaces_provider_requests(db_session):
    user = db_session.query(User).one()
    names = {f"Artist {i}" for i in range(discovery.MAX_EXPLORE_RESULTS + 1)}
    def track_for_artist(name):
        return {"artist": name, "title": "Track", "preview_url": f"url:{name}"}

    with (
        patch("app.discovery.explore._top_played_artists", return_value=["Seed"]),
        patch("app.discovery.explore._similar_artist_names", return_value=names),
        patch("app.discovery.explore.deezer.search_artist_track", side_effect=track_for_artist),
        patch("app.discovery.explore.random.shuffle", side_effect=lambda values: None),
        patch("app.discovery.explore.time.sleep") as sleep,
    ):
        result = discovery.explore_new_artists(db_session, user)
    assert len(result["results"]) == discovery.MAX_EXPLORE_RESULTS
    assert sleep.call_count == discovery.MAX_EXPLORE_RESULTS - 1


def test_lyrics_returns_none_when_no_lyrics_tag_matches(tmp_path):
    audio = SimpleNamespace(tags={})
    with patch("app.metadata_providers.lyrics.mutagen.File", return_value=audio):
        assert lyrics.get_embedded_lyrics(tmp_path / "empty-tags.mp3") is None


def test_playback_handles_missing_natural_index_and_exhaustion_edges(
    db_session, tmp_path, monkeypatch
):
    user = db_session.query(User).one()
    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    db_session.add(root)
    db_session.commit()
    track = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path="track.mp3",
        title="Track",
        artist="Artist",
    )
    db_session.add(track)
    db_session.commit()

    assert (
        playback._natural_index_of(
            db_session, user, {"kind": "library", "filters": {}}, 99999
        )
        is None
    )

    state = playback.get_or_create_state(db_session, user)
    playback._set_context(state, {"kind": "ids", "ids": [track.id]}, 1)
    state.queue_index = 0
    state.tail_cursor = 0
    db_session.add_all(
        [
            PlaybackQueueItem(
                user_id=user.id,
                position=0,
                original_position=0,
                media_item_id=track.id,
                source="generated",
            )
        ]
    )
    db_session.commit()
    state.queue_context_count = 1
    db_session.commit()
    # There are already enough generated rows ahead of the current row for
    # the refill guard to be the deciding condition.
    for position in range(1, playback.WINDOW_REFILL_THRESHOLD + 1):
        db_session.add(
            PlaybackQueueItem(
                user_id=user.id,
                position=position,
                original_position=position,
                media_item_id=track.id,
                source="generated",
            )
        )
    db_session.commit()
    playback._ensure_window(db_session, user, state)
    assert state.tail_cursor == 0  # enough ahead already: nothing drawn
    assert len(playback._queue(db_session, user)) == playback.WINDOW_REFILL_THRESHOLD + 1

    state.is_shuffled = True
    state.repeat_mode = "all"
    state.tail_cursor = state.queue_context_count
    with patch("app.playback.random.getrandbits", return_value=123):
        playback._handle_context_exhaustion(db_session, user, state)
    assert state.shuffle_seed == 123

    assert playback.set_queue_context(
        db_session, user, {"kind": "ids", "ids": [track.id]}, track.id
    ) is None
    assert playback.set_repeat(db_session, user, "all") is None


def test_admin_grant_access_reports_missing_user(db_session, tmp_path):
    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    db_session.add(root)
    db_session.commit()
    with pytest.raises(HTTPException, match="User not found"):
        admin.grant_access(root.id, 99999, db_session)


def test_tag_writer_covers_easy_success_and_raw_none_or_empty_tags(tmp_path):
    from app.tag_writer import write_tags

    path = tmp_path / "song.mp3"
    easy = MagicMock()
    easy.tags = MagicMock()
    with patch("app.tag_writer.mutagen.File", return_value=easy):
        assert write_tags(path, title="Title", track_number=4) is True
    assert easy.__setitem__.call_count == 2
    easy.save.assert_called_once()

    with patch("app.tag_writer.mutagen.File", side_effect=[TypeError("easy"), None]):
        assert write_tags(path, title="No raw audio") is False

    raw = MagicMock()
    raw.tags = None

    def add_raw_tags():
        raw.tags = MagicMock()

    raw.add_tags.side_effect = add_raw_tags
    with patch("app.tag_writer.mutagen.File", side_effect=[TypeError("easy"), raw]):
        assert write_tags(path, title="Raw title") is True
    raw.add_tags.assert_called_once()


def test_art_resize_reports_failure_when_the_thumbnail_folder_cannot_be_made(tmp_path):
    with patch.object(Path, "mkdir", side_effect=OSError("unwritable")):
        assert art._resize_art(tmp_path / "source.jpg", tmp_path / "thumb.jpg") is False


def test_cache_budget_removes_files_but_leaves_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "cache.db")
    cache_dir = settings.transcode_dir
    cache_dir.mkdir()
    (cache_dir / "nested").mkdir()
    (cache_dir / "7_128k.mp3").write_bytes(b"x")
    cleanup.enforce_cache_budget(0)
    # Over budget: the file goes; a folder is not a cache entry and stays.
    assert [p.name for p in cache_dir.iterdir()] == ["nested"]


def test_explore_new_artists_skips_an_artist_deezer_has_no_track_for(db_session):
    user = db_session.query(User).one()
    with (
        patch("app.discovery.explore._top_played_artists", return_value=["Seed"]),
        patch("app.discovery.explore._similar_artist_names", return_value={"A", "B"}),
        patch(
            "app.discovery.explore.deezer.search_artist_track",
            side_effect=lambda name: None if name == "A" else {"artist": "Deezer's B", "title": "Track"},
        ),
        patch("app.discovery.explore.time.sleep"),
    ):
        result = discovery.explore_new_artists(db_session, user)
    assert result["results"] == [{"artist": "B", "title": "Track"}]


def test_enrichment_keeps_the_file_genre_over_musicbrainz(db_session, tmp_path):
    from app.enrichment import enrich_media_item

    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    item = MediaItem(
        library_root=root,
        kind=MediaKind.track,
        relative_path="track.mp3",
        title="Track",
        artist="Artist",
        genre="Existing",
        musicbrainz_recording_mbid="rec-existing",
    )
    db_session.add(item)
    db_session.commit()
    detail = {"genres": [{"name": "Rock"}], "tags": []}
    with patch(
        "app.enrichment.musicbrainz.fetch_recording_result",
        return_value=musicbrainz.LookupResult(value=detail),
    ):
        result = enrich_media_item(db_session, item)
    assert result["matched"] is True
    assert item.genre == "Existing"


def test_musicbrainz_tags_differing_only_in_case_are_stored_once(db_session, tmp_path):
    from app.metadata_taxonomy import replace_item_musicbrainz_tags

    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    item = MediaItem(library_root=root, kind=MediaKind.track, relative_path="track.mp3", title="Track")
    db_session.add(item)
    db_session.commit()
    replace_item_musicbrainz_tags(db_session, item, [("Driving", 9), ("driving", 4)])
    db_session.commit()
    replace_item_musicbrainz_tags(db_session, item, [("driving", 4)])
    db_session.commit()
    assert len(item.musicbrainz_tags) == 1


def test_startup_seeds_the_configured_default_user_into_an_empty_database(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base

    bootstrap_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=bootstrap_engine)
    bootstrap_factory = sessionmaker(bind=bootstrap_engine)
    monkeypatch.setattr(settings, "default_username", "bootstrap")
    monkeypatch.setattr(settings, "default_password", "bootstrap-password")
    with startup_against(bootstrap_factory):
        main.seed_default_user()
    bootstrap_session = bootstrap_factory()
    assert bootstrap_session.query(User).one().username == "bootstrap"
    bootstrap_session.close()


def test_an_empty_embedded_lyrics_frame_counts_as_no_lyrics(tmp_path):
    empty_uslt = SimpleNamespace(tags={"USLT::eng": SimpleNamespace(text="")})
    with patch("app.metadata_providers.lyrics.mutagen.File", return_value=empty_uslt):
        assert lyrics.get_embedded_lyrics(tmp_path / "empty-uslt.mp3") is None


def test_radio_headers_skip_a_line_without_a_colon():
    _status, headers, body = radio._read_headers(
        _RecvSocket(b"HTTP/1.0 200 OK\r\nNoColon\r\nX-Test: yes\r\n\r\nbody")
    )
    assert headers == {"x-test": "yes"}
    assert body == b"body"


def test_radio_audio_passes_through_empty_and_titleless_icy_metadata():
    zero_metadata = _RecvSocket(b"")
    with (
        patch("app.radio._connect", return_value=(zero_metadata, "station", 80, "/")),
        patch("app.radio._read_headers", return_value=(200, {"icy-metaint": "1"}, b"x\x00")),
    ):
        assert list(radio.stream_audio_with_icy(101, "http://station/")) == [b"x"]

    no_title = b"NoTitle" + b"\x00" * 9
    with (
        patch("app.radio._connect", return_value=(_RecvSocket(b""), "station", 80, "/")),
        patch(
            "app.radio._read_headers",
            return_value=(200, {"icy-metaint": "1"}, b"x" + bytes([1]) + no_title),
        ),
    ):
        assert list(radio.stream_audio_with_icy(102, "http://station/")) == [b"x"]


def test_play_source_payloads_are_validated_per_kind():
    from app import schemas
    from app.schemas import PlaySourceIn

    with pytest.raises(ValueError, match="Invalid play source payload"):
        PlaySourceIn(kind="library", label="Library", payload={"filters": {"q": 3}})
    assert PlaySourceIn(kind="mix", label="Mix", payload={"genre": "Rock"}).kind == "mix"
    with patch.object(schemas, "PLAY_SOURCE_KINDS", schemas.PLAY_SOURCE_KINDS | {"other"}):
        with pytest.raises(ValueError, match="Invalid play source payload"):
            PlaySourceIn(kind="other", label="Other", payload={})


def test_a_play_is_not_scrobbled_without_a_listenbrainz_integration(db_session, tmp_path):
    from app import scrobbling
    from app.models import UserIntegration

    root = LibraryRoot(path=str(tmp_path / "music"), display_name="Music")
    item = MediaItem(
        library_root=root, kind=MediaKind.track, relative_path="track.mp3", title="Track", artist="Artist"
    )
    user = db_session.query(User).one()
    db_session.add_all([root, item, UserIntegration(user_id=user.id, provider="other", credential="token")])
    db_session.commit()
    with patch("app.scrobbling.listenbrainz.submit_listen") as submit:
        scrobbling.scrobble_track(db_session, user, item)
    submit.assert_not_called()


def test_writing_tags_through_the_easy_interface_sets_each_given_field(tmp_path):
    from app.tag_writer import write_tags

    easy = MagicMock()
    easy.tags = MagicMock()
    with patch("app.tag_writer.mutagen.File", return_value=easy):
        assert write_tags(tmp_path / "easy.mp3", title="Title") is True
    assert easy.__setitem__.call_count == 1


def test_task_progress_commits_are_throttled_except_for_the_last_item():
    task = SimpleNamespace(processed=0, total=0, updated_at=None)
    progress_db = Mock()
    saved_at = [0.0]
    with patch("app.library_tasks.time.monotonic", return_value=0.1):
        library_tasks._save_progress(progress_db, task, 1, 2, saved_at)
        # Too soon after the last save: recorded on the row, not committed.
        assert (task.processed, task.total) == (1, 2)
        progress_db.commit.assert_not_called()
        library_tasks._save_progress(progress_db, task, 2, 2, saved_at)
    progress_db.commit.assert_called_once()  # the last item always saves
    assert saved_at == [0.1]


def _admin_track(db_session, tmp_path) -> MediaItem:
    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    item = MediaItem(
        library_root=root, kind=MediaKind.track, relative_path="track.mp3", title="Track", artist="Artist"
    )
    db_session.add_all([root, item])
    db_session.commit()
    return item


class _MissingFile:
    def is_file(self):
        return False


def test_a_tag_edit_of_a_track_whose_file_is_missing_still_saves_the_row(db_session, tmp_path):
    item = _admin_track(db_session, tmp_path)
    with (
        patch("app.routers.admin.media_file_path", return_value=_MissingFile()),
        patch("app.routers.admin.write_tags", return_value=True),
    ):
        assert admin._apply_tag_edit(db_session, item, admin.TagEditIn(title="Changed")) is True


def test_bulk_remove_removes_a_track_whose_file_is_already_gone(db_session, tmp_path):
    item = _admin_track(db_session, tmp_path)
    with (
        patch("app.routers.admin.media_file_path", return_value=_MissingFile()),
        patch("app.routers.admin.delete_media_records"),
    ):
        assert admin.bulk_remove_media(admin.BulkRemoveIn(media_item_ids=[item.id]), db_session) == {
            "removed": [item.id],
            "failed": [],
        }


def test_granting_library_access_twice_is_harmless(db_session, tmp_path):
    user = db_session.query(User).one()
    root = LibraryRoot(path=str(tmp_path / "access"), display_name="Access")
    db_session.add(root)
    db_session.commit()
    assert admin.grant_access(root.id, user.id, db_session) == {"ok": True}
    assert admin.grant_access(root.id, user.id, db_session) == {"ok": True}


def _bare_request():
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})
    request._cookies = {}
    return request


def test_logout_without_a_session_cookie_still_clears_the_cookie(db_session):
    from app.routers import auth

    response = Mock()
    auth.logout(_bare_request(), response, BackgroundTasks(), db_session)
    response.delete_cookie.assert_called_once()


def test_changing_the_password_replaces_the_old_one(db_session):
    from app.routers import auth

    user = db_session.query(User).one()
    auth.change_password(
        auth.PasswordChangeIn(current_password="changeme", new_password="new-password"),
        _bare_request(),
        BackgroundTasks(),
        db_session,
        user,
    )
    db_session.refresh(user)
    assert verify_password("new-password", user.password_hash)
    assert not verify_password("changeme", user.password_hash)


def _playback_track(db_session, tmp_path) -> MediaItem:
    root = LibraryRoot(path=str(tmp_path), display_name="Library")
    item = MediaItem(
        library_root=root, kind=MediaKind.track, relative_path="track.mp3", title="Track", artist="Artist"
    )
    db_session.add_all([root, item])
    db_session.commit()
    return item


def test_liking_a_song_already_in_favourites_does_not_add_it_twice(db_session, tmp_path):
    from app.routers import favorites as favorites_router

    user = db_session.query(User).one()
    item = _playback_track(db_session, tmp_path)
    none = favorites_router.PreferenceIn(state="none")
    assert favorites_router.set_preference(item.id, none, db_session, user).state == "none"
    playlist = Playlist(user_id=user.id, name="Favourites", is_auto_generated=True, auto_kind="favorites")
    db_session.add(playlist)
    db_session.commit()
    db_session.add(PlaylistItem(playlist_id=playlist.id, media_item_id=item.id, position=0))
    db_session.commit()
    liked = favorites_router.PreferenceIn(state="liked")
    assert favorites_router.set_preference(item.id, liked, db_session, user).state == "liked"
    assert db_session.query(PlaylistItem).filter(PlaylistItem.playlist_id == playlist.id).count() == 1


def test_continuation_is_empty_when_there_are_no_similar_tracks(db_session, tmp_path):
    user = db_session.query(User).one()
    item = _playback_track(db_session, tmp_path)
    with patch("app.playback.similar_tracks_for_seed", return_value=[]):
        assert playback._continuation_tracks(db_session, user, item.id) == []


def test_repeat_one_keeps_the_context_when_it_is_exhausted(db_session, tmp_path):
    user = db_session.query(User).one()
    item = _playback_track(db_session, tmp_path)
    state = playback.get_or_create_state(db_session, user)
    playback._set_context(state, {"kind": "ids", "ids": [item.id]}, 1)
    state.tail_cursor = 1
    state.repeat_mode = "one"
    state.media_item_id = item.id
    db_session.commit()
    playback._handle_context_exhaustion(db_session, user, state)
    # "one" repeats the track itself (track_ended): the context is kept.
    assert (playback._get_context(state), state.tail_cursor) == ({"kind": "ids", "ids": [item.id]}, 1)


def test_shuffle_on_with_no_context_just_sets_the_flag(db_session, tmp_path):
    user = db_session.query(User).one()
    _playback_track(db_session, tmp_path)
    state = playback.get_or_create_state(db_session, user)
    state.queue_context_json = None
    state.is_shuffled = False
    db_session.commit()
    playback.set_shuffle(db_session, user, True)
    assert state.is_shuffled is True
    assert state.queue_context_json is None  # nothing to reshuffle


def test_reordering_with_nothing_current_invents_no_current_index(db_session, tmp_path):
    user = db_session.query(User).one()
    item = _playback_track(db_session, tmp_path)
    state = playback.get_or_create_state(db_session, user)
    queue_item = PlaybackQueueItem(
        user_id=user.id, position=0, original_position=0, media_item_id=item.id, source="user"
    )
    db_session.add(queue_item)
    state.queue_index = -1
    db_session.commit()
    assert playback.reorder_queue_item(db_session, user, queue_item.id, 5) is None
    assert (queue_item.position, state.queue_index) == (0, -1)


def test_snapshot_keeps_the_current_track_when_a_later_queue_item_is_inaccessible(
    db_session, tmp_path
):
    user = db_session.query(User).one()
    item = _playback_track(db_session, tmp_path)
    state = playback.get_or_create_state(db_session, user)
    private_root = LibraryRoot(path=str(tmp_path / "private"), display_name="Private", is_shared=False)
    private_item = MediaItem(
        library_root=private_root, kind=MediaKind.track, relative_path="private.mp3", title="Private"
    )
    db_session.add_all([private_root, private_item])
    db_session.commit()
    db_session.add_all([
        PlaybackQueueItem(user_id=user.id, position=0, original_position=0, media_item_id=item.id, source="user"),
        PlaybackQueueItem(
            user_id=user.id, position=1, original_position=1, media_item_id=private_item.id, source="generated"
        ),
    ])
    state.media_item_id = item.id
    state.queue_index = 0
    user.is_admin = False
    db_session.commit()
    assert playback.build_state_snapshot(db_session, user)["media_item_id"] == item.id


def test_broadcast_keeps_a_socket_that_replaced_the_failing_one():
    from app.ws_manager import ConnectionManager

    manager = ConnectionManager()
    replacement = SimpleNamespace()

    class ReplacedSocket:
        async def send_json(self, _message):
            manager._connections[1][1] = replacement
            raise RuntimeError("closed")

    manager.connect(1, 1, ReplacedSocket())
    asyncio.run(manager.broadcast(1, {"type": "state"}))
    assert manager._connections[1][1] is replacement
