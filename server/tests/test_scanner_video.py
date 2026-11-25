from __future__ import annotations

from unittest.mock import patch

from app.models import LibraryRoot, MediaItem, MediaKind
from app.scanner import read_video_metadata, scan_library_root


def _make_root(db_session, library_dir) -> LibraryRoot:
    root = LibraryRoot(path=str(library_dir), display_name="Video Library")
    db_session.add(root)
    db_session.commit()
    db_session.refresh(root)
    return root


def test_read_video_metadata_classifies_music_video_when_tagged():
    with (
        patch(
            "app.scanner.read_audio_tags",
            return_value={"title": "Song", "artist": "Band", "album": "LP", "duration_seconds": 200.0},
        ),
        patch("app.scanner.probe_duration_seconds") as mock_probe,
    ):
        result = read_video_metadata("/fake/path.mp4")

    assert result["kind"] == MediaKind.music_video
    assert result["duration_seconds"] == 200.0
    mock_probe.assert_not_called()  # mutagen already had a duration, no need to shell out


def test_read_video_metadata_classifies_plain_video_when_untagged():
    with (
        patch("app.scanner.read_audio_tags", return_value={}),
        patch("app.scanner.probe_duration_seconds", return_value=5400.0),
    ):
        result = read_video_metadata("/fake/concert.mkv")

    assert result["kind"] == MediaKind.video
    assert result["duration_seconds"] == 5400.0


def test_read_video_metadata_falls_back_to_ffprobe_when_mutagen_has_no_duration():
    with (
        patch("app.scanner.read_audio_tags", return_value={"title": "Untitled"}),
        patch("app.scanner.probe_duration_seconds", return_value=90.0) as mock_probe,
    ):
        result = read_video_metadata("/fake/clip.mp4")

    mock_probe.assert_called_once()
    assert result["duration_seconds"] == 90.0


def test_scan_creates_video_media_item(db_session, library_dir):
    (library_dir / "concert.mkv").write_bytes(b"not-a-real-video")
    root = _make_root(db_session, library_dir)

    with (
        patch("app.scanner.probe_duration_seconds", return_value=3600.0),
        patch("app.scanner.generate_thumbnail", return_value=True) as mock_thumb,
    ):
        result = scan_library_root(db_session, root)

    assert result == {"added": 1, "updated": 0, "removed": 0, "failed": 0, "art_redone": 0, "missing_held": 0, "unreadable_dirs": [], "stopped": False}
    item = db_session.query(MediaItem).one()
    assert item.kind == MediaKind.video
    assert item.title == "concert"  # falls back to filename, untagged MKV
    assert item.duration_seconds == 3600.0
    mock_thumb.assert_called_once()


def test_scan_missing_ffmpeg_does_not_crash(db_session, library_dir):
    (library_dir / "clip.mp4").write_bytes(b"not-a-real-video")
    root = _make_root(db_session, library_dir)

    with (
        patch("app.scanner.probe_duration_seconds", return_value=None),
        patch("app.scanner.generate_thumbnail", return_value=False),
    ):
        result = scan_library_root(db_session, root)

    assert result == {"added": 1, "updated": 0, "removed": 0, "failed": 0, "art_redone": 0, "missing_held": 0, "unreadable_dirs": [], "stopped": False}
    item = db_session.query(MediaItem).one()
    assert item.duration_seconds is None
