from __future__ import annotations

from unittest.mock import patch

from app.models import LibraryRoot, MediaItem, MediaKind
from app.scanner import scan_library_root
from tests.conftest import make_audio_fixture, played_now


def _make_video(
    db_session, library_dir, filename: str = "clip.mp4"
) -> tuple[int, bytes]:
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    raw = b"fake-mp4-bytes"
    (library_dir / filename).write_bytes(raw)
    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.video,
        relative_path=filename,
        title="Clip",
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item.id, raw


def _scan_one_track(db_session, library_dir) -> int:
    path = library_dir / "track1.wav"
    make_audio_fixture(
        path, title="Song One", artist="A", album="X", genre="Rock", seconds=1.0
    )
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    scan_library_root(db_session, root)
    from app.models import MediaItem

    return db_session.query(MediaItem).one().id, path.read_bytes()


def test_full_stream_returns_whole_file(client, db_session, library_dir):
    media_id, raw_bytes = _scan_one_track(db_session, library_dir)

    resp = client.get(f"/api/stream/{media_id}")

    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.content == raw_bytes
    assert "source-bitrate;desc=\"" in resp.headers["server-timing"]
    assert "stream-bitrate;desc=\"" in resp.headers["server-timing"]


def test_stream_timing_reports_capped_bitrate(client, db_session, library_dir):
    media_id, _ = _scan_one_track(db_session, library_dir)
    transcoded_path = library_dir / "transcoded.mp3"
    transcoded_path.write_bytes(b"encoded")

    with patch("app.routers.stream.ensure_transcoded", return_value=transcoded_path):
        resp = client.get(f"/api/stream/{media_id}?max_bitrate=192")

    assert resp.status_code == 200
    assert 'stream-bitrate;desc="192.000"' in resp.headers["server-timing"]


def test_range_request_returns_partial_content(client, db_session, library_dir):
    media_id, raw_bytes = _scan_one_track(db_session, library_dir)

    resp = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=10-19"})

    assert resp.status_code == 206
    assert resp.content == raw_bytes[10:20]
    assert resp.headers["content-length"] == "10"
    assert resp.headers["content-range"] == f"bytes 10-19/{len(raw_bytes)}"


def test_range_request_open_ended(client, db_session, library_dir):
    media_id, raw_bytes = _scan_one_track(db_session, library_dir)

    resp = client.get(
        f"/api/stream/{media_id}", headers={"Range": f"bytes={len(raw_bytes) - 5}-"}
    )

    assert resp.status_code == 206
    assert resp.content == raw_bytes[-5:]


def test_range_request_suffix_returns_last_bytes(client, db_session, library_dir):
    media_id, raw_bytes = _scan_one_track(db_session, library_dir)

    resp = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=-5"})

    assert resp.status_code == 206
    assert resp.content == raw_bytes[-5:]
    assert (
        resp.headers["content-range"]
        == f"bytes {len(raw_bytes) - 5}-{len(raw_bytes) - 1}/{len(raw_bytes)}"
    )


def test_malformed_range_is_rejected_in_full(client, db_session, library_dir):
    media_id, raw_bytes = _scan_one_track(db_session, library_dir)

    resp = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=0-1,4-5"})

    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(raw_bytes)}"


def test_unbounded_range_numbers_are_rejected_instead_of_raising(
    client, db_session, library_dir
):
    media_id, _ = _scan_one_track(db_session, library_dir)
    huge = "9" * 5000

    assert (
        client.get(
            f"/api/stream/{media_id}", headers={"Range": f"bytes=-{huge}"}
        ).status_code
        == 416
    )
    assert (
        client.get(
            f"/api/stream/{media_id}", headers={"Range": f"bytes={huge}-"}
        ).status_code
        == 416
    )


def test_range_end_beyond_file_is_clamped(client, db_session, library_dir):
    media_id, raw_bytes = _scan_one_track(db_session, library_dir)

    resp = client.get(
        f"/api/stream/{media_id}", headers={"Range": f"bytes=0-{len(raw_bytes) + 100}"}
    )

    assert resp.status_code == 206
    assert resp.content == raw_bytes
    assert (
        resp.headers["content-range"]
        == f"bytes 0-{len(raw_bytes) - 1}/{len(raw_bytes)}"
    )


def test_stream_missing_media_item_404s(client, db_session):
    resp = client.get("/api/stream/999999")
    assert resp.status_code == 404


def test_stream_rejects_symlink_that_escapes_library_root(
    client, db_session, library_dir, tmp_path
):
    outside = tmp_path / "outside.wav"
    make_audio_fixture(outside, title="Secret", artist="A", album="X", genre="Rock")
    link = library_dir / "link.wav"
    link.symlink_to(outside)
    root = LibraryRoot(path=str(library_dir), display_name="Test Library")
    db_session.add(root)
    db_session.commit()
    from app.models import MediaItem, MediaKind

    item = MediaItem(
        library_root_id=root.id,
        kind=MediaKind.track,
        relative_path="link.wav",
        title="Secret",
    )
    db_session.add(item)
    db_session.commit()

    assert client.get(f"/api/stream/{item.id}").status_code == 404


def test_record_played_adds_history_entry(client, db_session, library_dir):
    media_id, _ = _scan_one_track(db_session, library_dir)

    resp = client.post(f"/api/stream/{media_id}/played", json=played_now())
    assert resp.status_code == 200

    recent = client.get("/api/library/recently-played").json()
    assert len(recent) == 1
    assert recent[0]["id"] == media_id


def test_stream_uses_transcoded_file_when_bitrate_cap_applies(
    client, db_session, library_dir, tmp_path
):
    media_id, raw_bytes = _scan_one_track(db_session, library_dir)
    transcoded_path = tmp_path / "transcoded.mp3"
    transcoded_path.write_bytes(b"fake-transcoded-mp3-bytes")

    with patch(
        "app.routers.stream.ensure_transcoded", return_value=transcoded_path
    ) as mock_transcode:
        resp = client.get(f"/api/stream/{media_id}", params={"max_bitrate": 128})

    assert resp.status_code == 200
    assert resp.content == b"fake-transcoded-mp3-bytes"
    assert resp.headers["content-type"] == "audio/mpeg"
    mock_transcode.assert_called_once()
    assert mock_transcode.call_args.args[2] == 128


def test_stream_falls_back_to_original_when_transcode_not_needed(
    client, db_session, library_dir
):
    media_id, raw_bytes = _scan_one_track(db_session, library_dir)

    with patch(
        "app.routers.stream.ensure_transcoded", return_value=None
    ) as mock_transcode:
        resp = client.get(f"/api/stream/{media_id}", params={"max_bitrate": 128})

    assert resp.status_code == 200
    assert resp.content == raw_bytes
    mock_transcode.assert_called_once()


def test_stream_ignores_disallowed_bitrate_value(client, db_session, library_dir):
    media_id, raw_bytes = _scan_one_track(db_session, library_dir)

    with patch("app.routers.stream.ensure_transcoded") as mock_transcode:
        resp = client.get(f"/api/stream/{media_id}", params={"max_bitrate": 999})

    assert resp.status_code == 200
    assert resp.content == raw_bytes
    mock_transcode.assert_not_called()


def test_stream_video_serves_original_bytes_without_making_a_copy(
    client, db_session, library_dir
):
    media_id, raw_bytes = _make_video(db_session, library_dir)

    resp = client.get(f"/api/stream/{media_id}")

    assert resp.status_code == 200
    assert resp.content == raw_bytes


def test_record_played_triggers_scrobble(client, db_session, library_dir):
    media_id, _ = _scan_one_track(db_session, library_dir)

    with patch("app.routers.stream.scrobble_track") as mock_scrobble:
        resp = client.post(f"/api/stream/{media_id}/played", json=played_now())
    assert resp.status_code == 200
    mock_scrobble.assert_called_once()


def test_stream_is_never_gzipped(client, db_session, library_dir):
    # A known audio type and the octet-stream fallback for an unknown
    # suffix both have to reach the player byte-for-byte, uncompressed.
    media_id, raw_bytes = _scan_one_track(db_session, library_dir)
    item = db_session.get(MediaItem, media_id)
    unknown = library_dir / "track1.weird"
    unknown.write_bytes(raw_bytes)
    other = MediaItem(
        library_root_id=item.library_root_id,
        kind=MediaKind.track,
        relative_path="track1.weird",
        title="Weird",
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    for stream_id in (media_id, other.id):
        resp = client.get(f"/api/stream/{stream_id}", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert "content-encoding" not in resp.headers
        assert resp.content == raw_bytes


# --- connection released, validators, If-Range -----------------------------


def test_stream_releases_its_database_connection_before_streaming(
    client, db_session, library_dir, session_factory, monkeypatch
):
    from app.routers import stream

    media_id, raw = _scan_one_track(db_session, library_dir)
    engine = session_factory.kw["bind"]
    baseline = engine.pool.checkedout()
    seen = []
    real = stream.StreamingResponse

    def recording(content, *args, **kwargs):
        def body():
            seen.append(engine.pool.checkedout())
            yield from content
        return real(body(), *args, **kwargs)

    monkeypatch.setattr(stream, "StreamingResponse", recording)
    with patch(
        "app.routers.stream.ensure_transcoded",
        side_effect=lambda *a: seen.append(engine.pool.checkedout()),
    ):
        resp = client.get(f"/api/stream/{media_id}", params={"max_bitrate": 128})
    assert resp.content == raw
    assert len(seen) == 2 and max(seen) <= baseline  # during transcode and stream


def test_stream_sends_a_strong_etag_and_last_modified(client, db_session, library_dir, tmp_path):
    media_id, _ = _scan_one_track(db_session, library_dir)
    original = client.get(f"/api/stream/{media_id}")
    etag = original.headers["etag"]
    assert etag.startswith('"') and not etag.startswith("W/") and "-t" not in etag
    assert original.headers["last-modified"].endswith("GMT")
    assert client.get(f"/api/stream/{media_id}").headers["etag"] == etag  # stable

    transcoded = tmp_path / "t.mp3"
    transcoded.write_bytes(b"capped")
    with patch("app.routers.stream.ensure_transcoded", return_value=transcoded):
        capped = client.get(f"/api/stream/{media_id}", params={"max_bitrate": 128})
    assert capped.headers["etag"].endswith('-t128"') and capped.headers["etag"] != etag


def test_if_range_mismatch_returns_the_full_representation(client, db_session, library_dir, tmp_path):
    media_id, raw = _scan_one_track(db_session, library_dir)
    etag = client.get(f"/api/stream/{media_id}").headers["etag"]
    last_modified = client.get(f"/api/stream/{media_id}").headers["last-modified"]

    same = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=0-9", "If-Range": etag})
    assert same.status_code == 206 and same.content == raw[:10]

    # Bytes from a transcode can't be continued from the original.
    other = client.get(
        f"/api/stream/{media_id}", headers={"Range": "bytes=0-9", "If-Range": '"abc-t128"'}
    )
    assert other.status_code == 200 and other.content == raw
    assert "content-range" not in other.headers
    weak = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=0-9", "If-Range": f"W/{etag}"})
    assert weak.status_code == 200

    assert client.get(
        f"/api/stream/{media_id}", headers={"Range": "bytes=0-9", "If-Range": last_modified}
    ).status_code == 206
    old = client.get(
        f"/api/stream/{media_id}",
        headers={"Range": "bytes=0-9", "If-Range": "Mon, 01 Jan 2001 00:00:00 GMT"},
    )
    assert old.status_code == 200 and old.content == raw
    garbage = client.get(f"/api/stream/{media_id}", headers={"Range": "bytes=0-9", "If-Range": "whenever"})
    assert garbage.status_code == 200
